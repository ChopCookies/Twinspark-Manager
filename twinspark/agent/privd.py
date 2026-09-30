"""tsm-privd: narrow privileged helper (spec §4.3).

A minimal, root-only service over a Unix socket with an ALLOWLIST of specific
privileged operations. There is deliberately no generic command execution:
every operation has fixed argv, validated parameters, and a timeout.

Operations
----------
* ``status``          — boot target, display-manager state, swappiness (read-only)
* ``drop_caches``     — ``sync`` + ``echo 3 > /proc/sys/vm/drop_caches``
* ``boot_target``     — ``systemctl set-default multi-user.target|graphical.target``
* ``display_manager`` — ``systemctl stop|start display-manager`` (headless *now*)
* ``swappiness``      — ``vm.swappiness`` 0..100 (GB10 recipes use 0)

Run with ``tsm serve privd`` from a root systemd unit
(``deploy/systemd/twinspark-privd.service``). The socket is ``0660
root:<group>``; the agent user must be in that group.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import maintenance

log = logging.getLogger("twinspark.privd")

PRIV_SOCKET = "/run/twinspark/privd.sock"
_TARGETS = {"multi-user": "multi-user.target", "graphical": "graphical.target"}


def _systemctl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    exe = shutil.which("systemctl") or "/usr/bin/systemctl"
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)


def _meminfo_gib() -> dict[str, float]:
    from .sysinfo import memory_snapshot
    return memory_snapshot()


def op_status(params: dict[str, Any]) -> dict[str, Any]:
    target = _systemctl("get-default", timeout=10).stdout.strip() or None
    dm = _systemctl("is-active", "display-manager", timeout=10).stdout.strip() or "unknown"
    try:
        swap = int(Path("/proc/sys/vm/swappiness").read_text().strip())
    except (OSError, ValueError):
        swap = None
    return {"default_target": target, "display_manager": dm, "swappiness": swap,
            "uid": os.geteuid()}


def op_drop_caches(params: dict[str, Any]) -> dict[str, Any]:
    before = _meminfo_gib()
    os.sync()
    with open("/proc/sys/vm/drop_caches", "w") as fh:
        fh.write("3\n")
    after = _meminfo_gib()
    return {"freed_page_cache_gib": round(before["page_cache_gib"] - after["page_cache_gib"], 2),
            "mem_free_before_gib": before["mem_free_gib"], "mem_free_after_gib": after["mem_free_gib"]}


def op_boot_target(params: dict[str, Any]) -> dict[str, Any]:
    target = _TARGETS.get(str(params.get("target")))
    if not target:
        raise ValueError("target must be 'multi-user' or 'graphical'")
    p = _systemctl("set-default", target)
    if p.returncode != 0:
        raise RuntimeError(f"systemctl set-default failed: {p.stderr.strip()[:300]}")
    return {"default_target": target}


def op_display_manager(params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action"))
    if action not in ("stop", "start"):
        raise ValueError("action must be 'stop' or 'start'")
    before = _meminfo_gib()
    p = _systemctl(action, "display-manager", timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"systemctl {action} display-manager failed: {p.stderr.strip()[:300]}")
    after = _meminfo_gib()
    return {"display_manager": action, "reclaimed_gib": round(
        after["mem_available_gib"] - before["mem_available_gib"], 2)}


def op_swappiness(params: dict[str, Any]) -> dict[str, Any]:
    value = int(params.get("value", -1))
    if not 0 <= value <= 100:
        raise ValueError("swappiness must be 0..100")
    with open("/proc/sys/vm/swappiness", "w") as fh:
        fh.write(f"{value}\n")
    return {"swappiness": value}


# Allowlist of privileged ops — the ONLY things privd will ever do.
PRIV_OPS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "maintenance_probe": maintenance.probe,
    "maintenance_start": maintenance.start,
    "maintenance_status": maintenance.status,
    "maintenance_reboot": maintenance.reboot,
    "maintenance_verify": maintenance.verify,
    "status": op_status,
    "drop_caches": op_drop_caches,
    "boot_target": op_boot_target,
    "display_manager": op_display_manager,
    "swappiness": op_swappiness,
}


class PrivdUnavailable(RuntimeError):
    pass


class PrivClient:
    """Local privileged-helper client used by the agent."""

    def __init__(self, socket_path: str = PRIV_SOCKET, timeout: float = 150.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def available(self) -> bool:
        return os.path.exists(self.socket_path) and os.access(self.socket_path, os.R_OK | os.W_OK)

    def call(self, op: str, params: dict[str, Any] | None = None) -> Any:
        if op not in PRIV_OPS:
            raise ValueError(f"unknown privileged op: {op}")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                sock.sendall(json.dumps({"op": op, "params": params or {}}).encode() + b"\n")
                sock.shutdown(socket.SHUT_WR)
                payload = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    payload += chunk
                    if len(payload) > 1 << 20:
                        raise RuntimeError("privd response too large")
        except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
            raise PrivdUnavailable(
                f"tsm-privd is not reachable at {self.socket_path} ({type(exc).__name__}) — "
                "install deploy/systemd/twinspark-privd.service") from exc
        resp = json.loads(payload.decode() or "{}")
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "privd error"))
        return resp.get("result")


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        data = await asyncio.wait_for(reader.readline(), timeout=5)   # one JSON line per request
        req = json.loads(data.decode())
        op = req.get("op")
        params = req.get("params") or {}
        handler = PRIV_OPS.get(op)
        if handler is None or not isinstance(params, dict):
            raise ValueError(f"unknown privileged op: {op}")
        result = await asyncio.wait_for(asyncio.to_thread(handler, params), timeout=140)
        log.info("privd op %s ok", op)
        writer.write(json.dumps({"ok": True, "result": result}).encode())
    except Exception as exc:  # noqa: BLE001
        log.warning("privd op failed: %s", exc)
        writer.write(json.dumps({"ok": False, "error": str(exc)[:500]}).encode())
    finally:
        try:
            await writer.drain()
        finally:
            writer.close()


async def privd_serve(socket_path: str = PRIV_SOCKET, group: str = "twinspark") -> None:
    """Serve the Unix socket loop. Root only."""
    import grp

    if os.geteuid() != 0:
        raise SystemExit("tsm-privd must run as root")
    os.makedirs(os.path.dirname(socket_path), mode=0o750, exist_ok=True)
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    old = os.umask(0o117)                 # socket is created 0660, never world-accessible
    try:
        server = await asyncio.start_unix_server(_handle, socket_path, limit=65536)
    finally:
        os.umask(old)
    # only root and the agent's group may connect
    gid = grp.getgrnam(group).gr_gid
    os.chown(os.path.dirname(socket_path), 0, gid)
    os.chmod(os.path.dirname(socket_path), 0o750)
    os.chown(socket_path, 0, gid)
    os.chmod(socket_path, 0o660)
    log.info("privd listening on %s (group %s)", socket_path, group)
    async with server:
        await server.serve_forever()
