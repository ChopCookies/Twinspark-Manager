#!/usr/bin/env python3
"""Measure the resident memory of TwinSpark's own services, the way a GB10 budget needs it.

Starts the real processes (`tsm serve agent`, `tsm serve controller`, optionally `tsm serve termd`)
from a sandbox install, lets them settle, exercises the API the way the GUI does, and prints per
process RSS, PSS and USS from /proc/<pid>/smaps_rollup:

* RSS counts shared libraries in full in every process, so summing RSS over-counts.
* PSS splits shared pages between the processes that map them: the sum is a fair total.
* USS is what the process alone holds (anonymous + private file pages). It is the closest figure to
  systemd's MemoryCurrent for a service, which charges shared library pages to whoever faulted them
  in first.

Nothing here touches the real /etc, systemd or Docker: the sandbox runs in dry-run mode.

    python scripts/measure_memory.py                    # builds a sandbox in a temp dir
    python scripts/measure_memory.py --root /tmp/stage  # reuse one made by `tsm setup --root ... --no-start`
    python scripts/measure_memory.py --json             # machine-readable

Run it on the machine you want numbers for: an x86 laptop and an aarch64 GB10 differ.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def smaps(pid: int) -> dict[str, float]:
    """RSS / PSS / USS in MiB from smaps_rollup (Linux 4.14+)."""
    vals: dict[str, int] = {}
    with open(f"/proc/{pid}/smaps_rollup") as f:
        for line in f:
            m = re.match(r"(\w+):\s+(\d+) kB", line)
            if m:
                vals[m.group(1)] = int(m.group(2))
    uss = vals.get("Private_Clean", 0) + vals.get("Private_Dirty", 0)
    return {"rss_mib": vals.get("Rss", 0) / 1024, "pss_mib": vals.get("Pss", 0) / 1024,
            "uss_mib": uss / 1024, "anon_mib": vals.get("Anonymous", 0) / 1024}


def make_sandbox(tsm: str, root: Path) -> None:
    subprocess.run([tsm, "setup", "--root", str(root), "--yes", "--no-start"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, timeout=300)


def rewrite_ports(root: Path) -> dict[str, int]:
    """Give every listener a free port so the measurement never collides with a real install."""
    etc = root / "etc" / "twinspark"
    ports = {"agent": free_port(), "manager": free_port(), "gateway": free_port(), "terminal": free_port()}
    agent = (etc / "agent.yaml").read_text()
    agent = re.sub(r"listener: \{bind: ([^,]+), port: \d+\}", rf"listener: {{bind: \1, port: {ports['agent']}}}", agent)
    agent = re.sub(r"terminal_port: \d+", f"terminal_port: {ports['terminal']}", agent)
    (etc / "agent.yaml").write_text(agent)
    ctrl = (etc / "controller.yaml").read_text()
    ctrl = re.sub(r"(?m)^listener: \{bind: ([^,]+), port: \d+\}",
                  rf"listener: {{bind: \1, port: {ports['manager']}}}", ctrl)
    ctrl = re.sub(r"(?m)^gateway_listener: \{bind: [^,]+, port: \d+\}",
                  f"gateway_listener: {{bind: 127.0.0.1, port: {ports['gateway']}}}", ctrl)
    ctrl = re.sub(r"agent_url: http://127\.0\.0\.1:\d+", f"agent_url: http://127.0.0.1:{ports['agent']}", ctrl)
    ctrl = re.sub(r"terminal_port: \d+", f"terminal_port: {ports['terminal']}", ctrl)
    (etc / "controller.yaml").write_text(ctrl)
    return ports


def wait_http(url: str, timeout: float = 60.0, headers: dict | None = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2, headers=headers).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise SystemExit(f"{url} did not answer within {timeout:.0f} s")


def management_key(tsm: str, root: Path) -> str:
    out = subprocess.run([tsm, "--secrets-dir", str(root / "etc/twinspark/secrets"), "init", "--show"],
                         capture_output=True, text=True, timeout=60).stdout
    m = re.search(r"(?m)^management_api_key\s+(\S+)", out)
    return m.group(1) if m else ""


def exercise(manager: str, key: str, gateway: str) -> None:
    """What an open GUI does for a minute: status, jobs, profiles, metrics, models."""
    h = {"X-API-Key": key}
    with httpx.Client(timeout=10) as c:
        for _ in range(5):
            for path in ("/api/v1/status", "/api/v1/jobs?limit=60", "/api/v1/profiles?summary=true",
                         "/api/v1/system/metrics/series", "/api/v1/system/telemetry"):
                c.get(manager + path, headers=h)
            c.get(gateway + "/v1/models")
            time.sleep(0.5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", help="existing sandbox root (default: a new temp sandbox)")
    ap.add_argument("--settle", type=float, default=20.0, help="seconds to idle before measuring")
    ap.add_argument("--termd", action="store_true", help="also start the terminal service")
    ap.add_argument("--privd", action="store_true", help="also start the root helper (needs root)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not sys.platform.startswith("linux"):
        raise SystemExit("needs Linux /proc")
    tsm = shutil.which("tsm") or str(Path(sys.executable).with_name("tsm"))
    tmp = None
    if args.root:
        root = Path(args.root)
    else:
        tmp = tempfile.mkdtemp(prefix="tsm-mem-")
        root = Path(tmp)
        make_sandbox(tsm, root)
    ports = rewrite_ports(root)
    etc = root / "etc" / "twinspark"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    procs: dict[str, subprocess.Popen] = {}
    try:
        procs["agent"] = subprocess.Popen([tsm, "serve", "agent", "--config", str(etc / "agent.yaml")], env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_http(f"http://127.0.0.1:{ports['agent']}/v1/actions")
        procs["controller+gateway"] = subprocess.Popen(
            [tsm, "serve", "controller", "--config", str(etc / "controller.yaml")], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        manager, gateway = f"http://127.0.0.1:{ports['manager']}", f"http://127.0.0.1:{ports['gateway']}"
        wait_http(manager + "/api/v1/health")
        if args.termd:
            procs["terminal"] = subprocess.Popen([tsm, "serve", "termd", "--config", str(etc / "agent.yaml")],
                                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
        if args.privd:
            if os.geteuid() != 0:
                raise SystemExit("--privd needs root (the helper is a root service)")
            procs["privd (root)"] = subprocess.Popen(
                [tsm, "serve", "privd", "--socket", str(root / "privd.sock"), "--group", "root",
                 "--unsafe-policy-owner", "--remote-policy", str(etc / "remote-policy.json")],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
        exercise(manager, management_key(tsm, root), gateway)
        time.sleep(args.settle)
        rows = {name: smaps(p.pid) for name, p in procs.items() if p.poll() is None}
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    total = {k: sum(r[k] for r in rows.values()) for k in ("rss_mib", "pss_mib", "uss_mib", "anon_mib")}
    from twinspark import __version__
    report = {"version": __version__, "machine": os.uname().machine, "python": sys.version.split()[0],
              "processes": rows, "total": total}
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"TwinSpark {__version__} on {report['machine']}, Python {report['python']} (dry-run sandbox)")
    print(f"{'process':22}{'RSS':>9}{'PSS':>9}{'USS':>9}   MiB")
    for name, r in rows.items():
        print(f"{name:22}{r['rss_mib']:9.1f}{r['pss_mib']:9.1f}{r['uss_mib']:9.1f}")
    print(f"{'total':22}{total['rss_mib']:9.1f}{total['pss_mib']:9.1f}{total['uss_mib']:9.1f}"
          "   (sum PSS for a fair total; RSS double-counts shared libraries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
