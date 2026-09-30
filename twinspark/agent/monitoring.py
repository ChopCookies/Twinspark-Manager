"""Bounded, read-only host samples. Unsupported sensors are unknown, never zero."""

from __future__ import annotations

import csv
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .sysinfo import memory_snapshot


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def number(value: str) -> float | None:
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (ValueError, TypeError):
        return None


def gpu_sample() -> dict:
    fields = ["utilization.gpu", "temperature.gpu", "power.draw", "power.limit", "memory.used", "memory.total"]
    keys = ["utilization_pct", "temperature_c", "power_w", "power_limit_w", "memory_used_mib", "memory_total_mib"]
    result = dict.fromkeys(keys)
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {**result, "available": False, "note": "nvidia-smi is unavailable"}
    try:
        p = subprocess.run(
            [exe, "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if p.returncode:
            return {**result, "available": False, "note": p.stderr.strip()[:200] or "GPU query failed"}
        rows = list(csv.reader(p.stdout.splitlines()))
        if not rows or len(rows[0]) != len(keys):
            return {**result, "available": False, "note": "GPU query returned no usable sample"}
        return {
            **dict(zip(keys, (number(x.strip()) for x in rows[0]), strict=True)),
            "available": True,
            "power_source": "nvidia-smi GPU sensor",
            "power_scope": "gpu",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {**result, "available": False, "note": type(exc).__name__}


class HostSampler:
    def __init__(self, proc: str = "/proc", sysfs: str = "/sys"):
        self.proc, self.sysfs = Path(proc), Path(sysfs)
        self.previous: dict | None = None
        self.cached: dict | None = None
        self.lock = threading.Lock()

    def sample(self) -> dict:
        with self.lock:
            now = time.monotonic()
            if self.cached and self.previous and now - self.previous["at"] < 2:
                return self.cached
            stat = read(self.proc / "stat").splitlines()
            cpu = next((line.split()[1:9] for line in stat if line.startswith("cpu ")), [])
            counters = [int(x) for x in cpu] if cpu and all(x.isdigit() for x in cpu) else []
            total = sum(counters) if counters else None
            idle = sum(counters[3:5]) if len(counters) >= 5 else None
            previous = self.previous or {}
            elapsed = now - previous["at"] if previous else None
            cpu_pct = None
            if (
                total is not None
                and idle is not None
                and previous.get("total") is not None
                and previous.get("idle") is not None
            ):
                dt, di = total - previous["total"], idle - previous["idle"]
                if dt > 0 and 0 <= di <= dt:
                    cpu_pct = round((1 - di / dt) * 100, 1)
            net = {}
            for line in read(self.proc / "net/dev").splitlines()[2:]:
                name, sep, values = line.partition(":")
                fields = values.split()
                name = name.strip()
                if not sep or name == "lo" or len(fields) < 16:
                    continue
                try:
                    rx, tx = int(fields[0]), int(fields[8])
                except ValueError:
                    continue
                old = previous.get("network", {}).get(name)
                rates = {"rx_bytes_s": None, "tx_bytes_s": None}
                if old and elapsed and elapsed > 0:
                    for key, value in (("rx", rx), ("tx", tx)):
                        if value >= old[key + "_bytes"]:
                            rates[key + "_bytes_s"] = round((value - old[key + "_bytes"]) / elapsed, 1)
                root = self.sysfs / "class/net" / name
                net[name] = {
                    "rx_bytes": rx,
                    "tx_bytes": tx,
                    **rates,
                    "speed_mbps": number(read(root / "speed")),
                    "state": read(root / "operstate") or "unknown",
                }
            gpu = gpu_sample()
            memory = memory_snapshot()
            if not memory["mem_total_gib"]:
                memory = dict.fromkeys(memory)
            load = read(self.proc / "loadavg").split()
            sample = {
                **memory,
                "at": time.time(),
                "cpu_pct": cpu_pct,
                "load_1m": number(load[0]) if load else None,
                "gpu": gpu,
                "network": net,
                "memory_scope": "shared CPU/GPU system memory",
                "wall_power_w": None,
                "wall_power_note": "A wall-power meter is required for total system consumption.",
                "sample_interval_s": round(elapsed, 2) if elapsed else None,
            }
            self.previous = {"at": now, "total": total, "idle": idle, "network": net}
            self.cached = sample
            return sample
