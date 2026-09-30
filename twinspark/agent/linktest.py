"""Two-node link tests over the QSFP connection.

* ``tcp``  — pure-Python multi-stream TCP throughput + TCP ping-pong RTT. Always
  available; CPU-bound, so it shows what TCP (rsync, Gloo, the gateway) gets,
  not what the NICs can do.
* ``rdma`` — ``ib_write_bw`` (perftest) per RoCE device, all device pairs at
  once. This is the number that matters for NCCL: ~95-110 Gb/s per PCIe half,
  ~185-195 Gb/s with both halves. A result around 13-16 Gb/s is the known
  ConnectX-7 firmware throttle (fix: firmware update + full power drain).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import statistics
import struct
import time
from typing import Any, Optional

CHUNK = 1024 * 1024


# ---- TCP -------------------------------------------------------------------------------
async def tcp_responder(host: str, port: int, lifetime: float) -> dict[str, Any]:
    received: list[int] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            mode = await reader.readexactly(1)
            if mode == b"B":
                n = 0
                while True:
                    chunk = await reader.read(CHUNK)
                    if not chunk:
                        break
                    n += len(chunk)
                received.append(n)
                writer.write(struct.pack("!Q", n))
                await writer.drain()
            elif mode == b"P":
                while True:
                    b = await reader.read(1)
                    if not b:
                        break
                    writer.write(b)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port, reuse_address=True)
    try:
        await asyncio.sleep(lifetime)
    finally:
        server.close()
        await server.wait_closed()
    return {"role": "responder", "mode": "tcp", "bind": f"{host}:{port}",
            "bytes_received": sum(received), "streams": len(received)}


async def _rtt(peer: str, port: int, samples: int = 200) -> dict[str, float]:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(peer, port), 10)
    writer.write(b"P")
    await writer.drain()
    rtts = []
    try:
        for _ in range(samples):
            t0 = time.perf_counter()
            writer.write(b"x")
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(1), 5)
            rtts.append((time.perf_counter() - t0) * 1e6)
    finally:
        writer.close()
    rtts.sort()
    return {"rtt_us_median": round(statistics.median(rtts), 1),
            "rtt_us_p99": round(rtts[int(len(rtts) * 0.99) - 1], 1)}


async def _bulk(peer: str, port: int, duration: float) -> tuple[int, float]:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(peer, port), 10)
    buf = b"\0" * CHUNK
    writer.write(b"B")
    start = time.perf_counter()
    while time.perf_counter() - start < duration:
        writer.write(buf)
        await writer.drain()
    writer.write_eof()
    acked = struct.unpack("!Q", await asyncio.wait_for(reader.readexactly(8), 30))[0]
    elapsed = time.perf_counter() - start
    writer.close()
    return acked, elapsed


async def tcp_initiator(peer: str, port: int, duration: float, streams: int) -> dict[str, Any]:
    rtt = await _rtt(peer, port)
    results = await asyncio.gather(*(_bulk(peer, port, duration) for _ in range(streams)))
    total = sum(b for b, _ in results)
    elapsed = max(e for _, e in results)
    return {"role": "initiator", "mode": "tcp", "peer": peer, "port": port, "streams": streams,
            "bandwidth_gbps": round(total * 8 / elapsed / 1e9, 2),
            "bytes_transferred": total, "duration_s": round(elapsed, 2), **rtt,
            "note": "TCP from Python is CPU-bound; use the rdma mode for the NIC's real speed"}


# ---- RDMA (perftest) ---------------------------------------------------------------------
_BW_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s*$", re.M)


def parse_ib_write_bw(text: str) -> Optional[float]:
    """Average Gb/s from ``ib_write_bw --report_gbits`` output."""
    rows = _BW_LINE.findall(text)
    return float(rows[-1][3]) if rows else None


def perftest_available() -> bool:
    return shutil.which("ib_write_bw") is not None


def _ib_argv(hca: str, gid: Optional[int], port: int, duration: float, peer: Optional[str]) -> list[str]:
    argv = ["ib_write_bw", "-d", hca, "--report_gbits", "-q", "4", "-F",
            "-D", str(int(max(2, duration))), "-p", str(port)]
    if gid is not None:
        argv += ["-x", str(gid)]
    if peer:
        argv.append(peer)
    return argv


async def rdma_run(hcas: list[str], gid: Optional[int], base_port: int, duration: float,
                   peer: Optional[str], register=None) -> dict[str, Any]:
    """Server side when ``peer`` is None, client side otherwise; all HCAs in parallel."""
    if not perftest_available():
        raise RuntimeError("ib_write_bw not found — install perftest (sudo apt install perftest)")
    if not hcas:
        raise RuntimeError("no RDMA devices configured for this node (nodes.X.rdma_hcas)")

    async def one(i: int, hca: str) -> dict[str, Any]:
        argv = _ib_argv(hca, gid, base_port + i, duration, peer)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        if register:
            register(proc)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=duration + 60)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"ib_write_bw on {hca} timed out") from None
        text = out.decode(errors="replace")
        return {"hca": hca, "port": base_port + i, "rc": proc.returncode,
                "gbps": parse_ib_write_bw(text), "tail": text[-800:]}

    rows = await asyncio.gather(*(one(i, h) for i, h in enumerate(hcas)))
    agg = sum(r["gbps"] or 0 for r in rows)
    out: dict[str, Any] = {"mode": "rdma", "role": "initiator" if peer else "responder",
                           "per_hca": rows, "bandwidth_gbps": round(agg, 2)}
    if peer:
        notes = []
        if any(r["gbps"] is not None and r["gbps"] < 25 for r in rows):
            notes.append("a device reports < 25 Gb/s — this is the known CX-7 firmware "
                         "throttle; update NIC firmware and fully power-drain both Sparks")
        if len(rows) == 1 and (rows[0]["gbps"] or 0) > 80:
            notes.append("one PCIe half measured; add the second RoCE device to reach ~200 Gb/s")
        out["notes"] = notes
    return out


def simulated(mode: str, role: str, streams: int = 4) -> dict[str, Any]:
    if role == "responder":
        return {"role": "responder", "mode": mode, "dry_run": True}
    if mode == "rdma":
        return {"role": "initiator", "mode": "rdma", "dry_run": True, "bandwidth_gbps": 188.4,
                "per_hca": [{"hca": "rocep1s0f1", "gbps": 94.3}, {"hca": "roceP2p1s0f1", "gbps": 94.1}],
                "note": "simulated dry-run values"}
    return {"role": "initiator", "mode": "tcp", "dry_run": True, "streams": streams,
            "bandwidth_gbps": 41.7, "rtt_us_median": 38.0, "rtt_us_p99": 61.0,
            "bytes_transferred": int(26e9), "note": "simulated dry-run values"}
