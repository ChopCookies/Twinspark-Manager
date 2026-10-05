"""Read-only host facts for the agent: memory, GPU/driver, desktop, RDMA.

Everything here reads /proc, /sys or runs a short, fixed, read-only command
(``nvidia-smi``). Nothing mutates the host. Missing files/tools degrade to
``None`` so the same code runs in tests, containers and on real Sparks.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

_GIB_KB = 1024 ** 2

# processes that make up a graphical session on DGX OS / Ubuntu
DESKTOP_PROCS = {
    "gnome-shell", "Xorg", "Xwayland", "gdm", "gdm3", "gdm-session-wor",
    "gdm-wayland-ses", "gdm-x-session", "gnome-session-b", "gnome-session-c",
    "gnome-software", "evolution-data-", "tracker-miner-f", "plasmashell", "kwin_x11",
    "kwin_wayland", "sddm", "lightdm", "nautilus", "gsd-xsettings", "ibus-daemon",
    "mutter-x11-fram", "xdg-desktop-por", "gnome-remote-de",
}
DISPLAY_MANAGERS = {"gdm", "gdm3", "sddm", "lightdm"}


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open(path) as fh:
            for line in fh:
                key, _, val = line.partition(":")
                out[key.strip()] = int(val.split()[0])          # kB
    except (OSError, ValueError, IndexError):
        pass
    return out


def kb_to_gib(v: int) -> float:
    return round(v / _GIB_KB, 2)


def memory_snapshot() -> dict[str, float]:
    mem = read_meminfo()
    return {
        "mem_total_gib": kb_to_gib(mem.get("MemTotal", 0)),
        "mem_available_gib": kb_to_gib(mem.get("MemAvailable", 0)),
        "mem_free_gib": kb_to_gib(mem.get("MemFree", 0)),
        "page_cache_gib": kb_to_gib(mem.get("Cached", 0) + mem.get("Buffers", 0)),
        "anon_gib": kb_to_gib(mem.get("AnonPages", 0)),
        "swap_used_gib": kb_to_gib(mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)),
    }


def _read(path: str | Path) -> Optional[str]:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


_NV_CACHE: dict[str, Any] = {}


def nvidia_facts(ttl: float = 300.0) -> dict[str, Optional[str]]:
    """Driver / CUDA / GPU name. Cached — none of these change without a reboot."""
    now = time.monotonic()
    if _NV_CACHE and now - _NV_CACHE.get("_at", 0) < ttl:
        return {k: v for k, v in _NV_CACHE.items() if not k.startswith("_")}
    facts: dict[str, Optional[str]] = {"driver_version": None, "cuda_version": None, "gpu_name": None}
    txt = _read("/proc/driver/nvidia/version")
    if txt:
        m = re.search(r"Kernel Module(?: for [^ ]+)?\s+([0-9.]+)", txt)
        if m:
            facts["driver_version"] = m.group(1)
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            p = subprocess.run([smi], capture_output=True, text=True, timeout=8)
            m = re.search(r"CUDA Version:\s*([0-9.]+)", p.stdout)
            if m:
                facts["cuda_version"] = m.group(1)
            m = re.search(r"Driver Version:\s*([0-9.]+)", p.stdout)
            if m and not facts["driver_version"]:
                facts["driver_version"] = m.group(1)
            q = subprocess.run([smi, "--query-gpu=name", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=8)
            if q.returncode == 0 and q.stdout.strip():
                facts["gpu_name"] = q.stdout.strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError):
            pass
    _NV_CACHE.clear()
    _NV_CACHE.update(facts)
    _NV_CACHE["_at"] = now
    return facts


def _proc_rss_kb(pid: str) -> int:
    txt = _read(f"/proc/{pid}/status") or ""
    m = re.search(r"^VmRSS:\s+(\d+)", txt, re.M)
    return int(m.group(1)) if m else 0


def desktop_facts(proc: str = "/proc") -> dict[str, Any]:
    """Is a graphical session running, and how much memory does it hold?"""
    rss = 0
    names: set[str] = set()
    dm = False
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        comm = _read(f"{proc}/{pid}/comm")
        if not comm:
            continue
        if comm in DESKTOP_PROCS:
            names.add(comm)
            rss += _proc_rss_kb(pid)
        if comm in DISPLAY_MANAGERS:
            dm = True
    target = None
    try:
        link = os.readlink("/etc/systemd/system/default.target")
        target = Path(link).name
    except OSError:
        target = None
    return {
        "desktop_running": bool(names),
        "display_manager_running": dm,
        "desktop_processes": sorted(names),
        "desktop_rss_gib": round(rss / _GIB_KB, 2),
        "default_target": target,
    }


def swappiness() -> Optional[int]:
    v = _read("/proc/sys/vm/swappiness")
    return int(v) if v and v.isdigit() else None


def disk_free_gib(path: str) -> float:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return round(shutil.disk_usage(p).free / 1024 ** 3, 1)


# ---- RDMA / RoCE discovery -----------------------------------------------------------
def _gid_ipv4(gid: str) -> Optional[str]:
    """``0000:0000:0000:0000:0000:ffff:c0a8:6401`` -> ``192.168.100.1``."""
    try:
        addr = ipaddress.IPv6Address(gid)
    except ValueError:
        return None
    mapped = addr.ipv4_mapped
    return str(mapped) if mapped else None


def rdma_devices(sysfs: str = "/sys/class/infiniband") -> list[dict[str, Any]]:
    """Every RoCE/IB device with state, rate, netdev and its RoCE v2 IPv4 GIDs."""
    root = Path(sysfs)
    out = []
    if not root.is_dir():
        return out
    for dev in sorted(root.iterdir(), key=lambda p: p.name):
        ports = dev / "ports"
        for port in sorted(ports.iterdir()) if ports.is_dir() else []:
            state = _read(port / "state") or ""
            rate = _read(port / "rate") or ""
            m = re.match(r"([0-9.]+)", rate)
            netdevs = sorted(p.name for p in (dev / "device" / "net").iterdir()) \
                if (dev / "device" / "net").is_dir() else []
            gids = []
            gdir, tdir, ndir = port / "gids", port / "gid_attrs" / "types", port / "gid_attrs" / "ndevs"
            for g in sorted(gdir.iterdir(), key=lambda p: int(p.name)) if gdir.is_dir() else []:
                gid = _read(g) or ""
                if not gid or set(gid.replace(":", "")) == {"0"}:
                    continue
                gids.append({"index": int(g.name), "gid": gid,
                             "type": _read(tdir / g.name), "ndev": _read(ndir / g.name),
                             "ipv4": _gid_ipv4(gid)})
            v2 = [g for g in gids if g["ipv4"] and (g["type"] or "").lower().startswith("roce v2")]
            out.append({
                "hca": dev.name, "port": int(port.name),
                "state": state.split(":", 1)[-1].strip() if ":" in state else state,
                "active": "ACTIVE" in state.upper(),
                "rate_gbps": float(m.group(1)) if m else None,
                "link_layer": _read(port / "link_layer"),
                "netdevs": netdevs,
                "roce_v2_ipv4": [{"index": g["index"], "ipv4": g["ipv4"], "ndev": g["ndev"]} for g in v2],
            })
    return out


# enp1s0f1np1 (first PCIe half) and enP2p1s0f1np1 (second half, PCI domain 2) are the twins of port f1np1
TWIN_RE = re.compile(r"^en(?:P(?P<dom>\d+))?p(?P<bus>\d+)s(?P<slot>\d+)f(?P<fn>\d+)np(?P<port>\d+)$")


def port_key(iface: str) -> Optional[str]:
    """``enP2p1s0f1np1`` and ``enp1s0f1np1`` are the same physical QSFP port: both give ``f1np1``."""
    m = TWIN_RE.match(iface or "")
    return f"f{m.group('fn')}np{m.group('port')}" if m else None


def _device_port_key(dev: dict[str, Any]) -> Optional[str]:
    for nd in dev.get("netdevs") or []:
        key = port_key(nd)
        if key:
            return key
    return None


def suggest_rdma(devices: list[dict[str, Any]], qsfp_ip: Optional[str],
                 prefix_len: int = 24) -> dict[str, Any]:
    """Pick the HCAs + GID index NCCL should use for traffic to the peer.

    A GB10 QSFP port appears as two RoCE devices (one per PCIe x4 half, ~100 Gb/s each). The layout
    recommended by NVIDIA and by eugr/spark-vllm-docker gives each half its *own* /24, so the half that
    does not carry ``qsfp_ip`` is on a different subnet. Every ACTIVE device on the same physical port as
    a device that has an address in the ``qsfp_ip`` subnet is therefore taken, whatever subnet its own
    address is in; ``tsm qsfp apply`` sets that layout up.
    """
    if not qsfp_ip:
        return {"hcas": [], "gid_index": None, "note": "qsfp_ip is not configured"}
    try:
        net = ipaddress.ip_network(f"{qsfp_ip}/{prefix_len}", strict=False)
    except ValueError:
        return {"hcas": [], "gid_index": None, "note": f"bad qsfp_ip {qsfp_ip!r}"}
    active = [d for d in devices if d["active"]]
    in_net = {d["hca"]: [g for g in d["roce_v2_ipv4"] if ipaddress.ip_address(g["ipv4"]) in net] for d in active}
    keys = {_device_port_key(d) for d in active if in_net[d["hca"]]} - {None}
    hcas, idx, nets = [], [], []
    for d in active:
        hits = in_net[d["hca"]]
        pick = hits[0] if hits else None
        if pick is None and keys and _device_port_key(d) in keys and d["roce_v2_ipv4"]:
            pick = d["roce_v2_ipv4"][0]               # the other twin: its address is on its own subnet
        if pick is None:
            continue
        hcas.append(d["hca"])
        idx.append(pick["index"])
        nets.append(ipaddress.ip_network(f"{pick['ipv4']}/{prefix_len}", strict=False))
    gid = idx[0] if idx and all(i == idx[0] for i in idx) else None
    note = ""
    if len(hcas) == 1:
        note = ("only one RoCE device has an address — NCCL tops out around 100 Gb/s. Give the second PCIe "
                "half's netdev (enP2p1s0f*np*) an address on its own subnet to reach ~200 Gb/s: "
                "`sudo tsm qsfp apply` does this.")
    elif not hcas:
        note = "no ACTIVE RoCE v2 device with an address on the QSFP subnet"
    elif gid is None:
        note = "RoCE devices use different GID indices; NCCL_IB_GID_INDEX cannot cover both"
    elif len(set(nets)) < len(nets):
        note = ("both PCIe halves share one subnet. eugr/spark-vllm-docker and NVIDIA's playbook use a "
                "different subnet per half (192.168.100.x and 192.168.101.x), because the kernel then "
                "sends everything out of one of them. `sudo tsm qsfp plan` shows the layout.")
    return {"hcas": hcas, "gid_index": gid, "note": note}
