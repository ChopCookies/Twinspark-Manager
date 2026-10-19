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


# ---- per-unit and per-process memory (0.6 planner inputs) -------------------------------------------
TSM_UNITS = ("twinspark-agent", "twinspark-controller", "twinspark-privd", "twinspark-terminal")
CGROUP_ROOT = "/sys/fs/cgroup"
_MIB = 1024 ** 2
# a process in one of these cgroups belongs to a container (its memory is the container's, not the host's)
_CONTAINER_CGROUP = re.compile(r"docker|containerd|kubepods|libpod|/lxc|machine\.slice")
_RUNTIME_PROCS = {"dockerd", "containerd", "containerd-shim", "containerd-shim-runc-v2", "docker-proxy", "runc"}
_INTERPRETERS = re.compile(r"^(?:python[0-9.]*|node|java|ruby|perl|bash|sh|uv|deno|bun)\Z")


def _int_text(text: Optional[str]) -> Optional[int]:
    return int(text) if text is not None and text.isdigit() else None


def cgroup_memory(path: str | Path) -> Optional[dict[str, Any]]:
    """memory.current/peak/events/max of one cgroup v2 directory, or None when it cannot be read.

    ``limit_mib`` is None for an unlimited cgroup (memory.max = "max"); ``peak_mib`` is None on kernels
    without memory.peak (< 5.19). Counts from memory.events are None when the file is missing.
    """
    p = Path(path)
    cur = _int_text(_read(p / "memory.current"))
    if cur is None:
        return None
    peak = _int_text(_read(p / "memory.peak"))
    events: dict[str, int] = {}
    for line in (_read(p / "memory.events") or "").splitlines():
        key, _, val = line.partition(" ")
        if val.strip().isdigit():
            events[key] = int(val.strip())
    limit = _int_text(_read(p / "memory.max"))
    have_events = bool(events)
    return {"current_mib": round(cur / _MIB, 1), "peak_mib": None if peak is None else round(peak / _MIB, 1),
            "oom": events.get("oom") if have_events else None,
            "oom_kills": events.get("oom_kill") if have_events else None,
            "limit_mib": None if limit is None else round(limit / _MIB, 1)}


def services_memory(root: str = CGROUP_ROOT) -> dict[str, dict[str, Any]]:
    """The TwinSpark systemd units present on this node with their cgroup memory (values None when unreadable)."""
    out: dict[str, dict[str, Any]] = {}
    base = Path(root) / "system.slice"
    for unit in TSM_UNITS:
        d = base / f"{unit}.service"
        if not d.is_dir():
            continue
        m = cgroup_memory(d) or {}
        out[unit] = {"current_mib": m.get("current_mib"), "peak_mib": m.get("peak_mib"),
                     "oom_kills": m.get("oom_kills")}
    return out


def _display_name(comm: str, cmdline: str) -> str:
    """``python3 /opt/hermes/gateway.py`` -> ``gateway.py``: an interpreter's name says little."""
    args = [a for a in cmdline.split("\0") if a]
    if args and _INTERPRETERS.match(comm):
        rest = args[1:]
        for i, a in enumerate(rest):
            if a == "-m" and i + 1 < len(rest):
                return rest[i + 1][:60]
            if not a.startswith("-"):
                return Path(a).name[:60] or comm
    return comm


def other_processes(proc: str = "/proc", limit: int = 5, min_mib: int = 100,
                    max_pids: int = 8192) -> list[dict[str, Any]]:
    """The largest processes above ``min_mib`` RSS that are neither TwinSpark, a desktop session, the
    container runtime nor inside a container (those are counted elsewhere). Bounded: at most
    ``max_pids`` entries of /proc are looked at."""
    rows = []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()][:max_pids]
    except OSError:
        return []
    for pid in pids:
        status = _read(f"{proc}/{pid}/status")
        if not status:
            continue
        m = re.search(r"^VmRSS:\s+(\d+)", status, re.M)
        if not m or int(m.group(1)) < min_mib * 1024:          # kernel threads have no VmRSS
            continue
        nm = re.search(r"^Name:\s*(.+)$", status, re.M)
        comm = nm.group(1).strip() if nm else "?"
        if comm in DESKTOP_PROCS or comm in _RUNTIME_PROCS:
            continue
        cgroup = _read(f"{proc}/{pid}/cgroup") or ""
        if _CONTAINER_CGROUP.search(cgroup) or "twinspark-" in cgroup:
            continue
        try:
            cmdline = Path(f"{proc}/{pid}/cmdline").read_bytes()[:4096].decode("utf-8", "replace")
        except OSError:
            cmdline = ""
        if "twinspark" in cmdline:                               # a manual install without systemd units
            continue
        rows.append({"name": _display_name(comm, cmdline), "pid": int(pid),
                     "rss_mib": round(int(m.group(1)) / 1024)})
    rows.sort(key=lambda r: r["rss_mib"], reverse=True)
    return rows[:limit]


def container_cgroup_dir(container_id: str, root: str = CGROUP_ROOT) -> Optional[Path]:
    """The cgroup v2 directory of a Docker container (systemd or cgroupfs driver), or None."""
    if not re.fullmatch(r"[0-9a-f]{64}", container_id or ""):
        return None
    for rel in (f"system.slice/docker-{container_id}.scope", f"docker/{container_id}",
                f"system.slice/docker/{container_id}"):
        d = Path(root) / rel
        if d.is_dir():
            return d
    return None


def read_vmstat(path: str = "/proc/vmstat") -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (_read(path) or "").splitlines():
        key, _, val = line.partition(" ")
        if key in ("pswpin", "pswpout") and val.strip().isdigit():
            out[key] = int(val.strip())
    return out


def read_psi(path: str = "/proc/pressure/memory") -> Optional[dict[str, float]]:
    """Memory pressure stall info (``some``/``full`` avg10, percent), or None without PSI support."""
    out: dict[str, float] = {}
    for line in (_read(path) or "").splitlines():
        kind, _, rest = line.partition(" ")
        m = re.search(r"avg10=([0-9.]+)", rest)
        if kind in ("some", "full") and m:
            out[f"{kind}_avg10"] = float(m.group(1))
    return out or None


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


# Interfaces made by container runtimes, VPNs, bridges and overlays. Some of them have a ``device`` link in
# sysfs (a macvlan or a VLAN on the QSFP port, a VF) and can even carry RoCE GIDs of the real port: never a
# QSFP port, an NCCL interface or the GID NCCL should use. GPUStack's discovery learned the same list.
VIRTUAL_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "vnet", "cni", "flannel", "cali", "weave", "cilium",
                    "kube", "tailscale", "tun", "tap", "wg", "zt", "vxlan", "genev", "lxc", "lxdbr", "podman",
                    "vmnet", "dummy", "ifb", "nerdctl", "macvlan", "macvtap", "ipvlan")


def is_virtual_name(name: str) -> bool:
    """A name that only a virtual interface has: the prefixes above, or a VLAN/alias (``x.100``, ``x@y``)."""
    n = (name or "").lower()
    return n.startswith(VIRTUAL_PREFIXES) or "." in n or "@" in n or ":" in n


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
            netdevs = sorted(p.name for p in (dev / "device" / "net").iterdir()
                             if not is_virtual_name(p.name)) if (dev / "device" / "net").is_dir() else []
            gids = []
            gdir, tdir, ndir = port / "gids", port / "gid_attrs" / "types", port / "gid_attrs" / "ndevs"
            for g in sorted(gdir.iterdir(), key=lambda p: int(p.name)) if gdir.is_dir() else []:
                gid = _read(g) or ""
                if not gid or set(gid.replace(":", "")) == {"0"}:
                    continue
                gids.append({"index": int(g.name), "gid": gid,
                             "type": _read(tdir / g.name), "ndev": _read(ndir / g.name),
                             "ipv4": _gid_ipv4(gid)})
            # a GID of a macvlan, VLAN or container interface on top of the port is not the port's own address
            v2 = [g for g in gids if g["ipv4"] and (g["type"] or "").lower().startswith("roce v2")
                  and not is_virtual_name(g["ndev"] or "")]
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
