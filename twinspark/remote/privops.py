"""Privileged remote-management operations (run by ``tsm-privd`` as root).

Same rules as the rest of privd: a fixed set of operations, fixed argv, validated parameters,
a timeout on every command. Operations that change the machine also check the root-owned
policy (:mod:`twinspark.remote.policy`) *inside* privd, so even a fully compromised agent cannot
reboot a node that never opted in.

``_do_*`` functions perform an action without consulting the policy. They exist for
``sudo tsm node …`` on the machine itself — root can reboot anyway — and are never reachable
over the socket.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from . import policy as _policy

Run = Callable[[list[str], float], "tuple[int, str, str]"]


def _run(argv: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out after {timeout:.0f}s"
    return p.returncode, p.stdout, p.stderr


RUN: Run = _run                      # tests replace this
SYS_NET = Path("/sys/class/net")     # ... and this
PROC = Path("/proc")                 # ... and this

_IFACE_RE = re.compile(r"^[A-Za-z0-9._-]{1,15}\Z")
_BOOTNUM_RE = re.compile(r"^[0-9A-Fa-f]{4}\Z")
_ENTRY_RE = re.compile(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+(.*?)\s*$")
_NETWORK_RE = re.compile(r"(?i)\b(pxe|network|netboot|ipv4|ipv6|http|https|lan)\b")
_USB_RE = re.compile(r"(?i)\busb\b")
_PKG_MANAGERS = {"dpkg", "apt", "apt-get", "aptitude", "unattended-upgr", "fwupd", "fwupdmgr", "packagekitd"}
JOURNAL_UNITS = {
    "agent": "twinspark-agent", "controller": "twinspark-controller", "privd": "twinspark-privd",
    "terminal": "twinspark-terminal", "docker": "docker", "ssh": "ssh", "containerd": "containerd",
    "network": "NetworkManager", "networkd": "systemd-networkd", "nvidia": "nvidia-persistenced",
}
MAX_LINES = 2000
POWER_UNIT = "twinspark-remote-{action}"


def _which(name: str) -> str:
    return shutil.which(name, path="/usr/sbin:/usr/bin:/sbin:/bin") or name


def _need(policy_feature: str) -> None:
    pol = _policy.PRIVD_POLICY.current()
    if not pol.allows(policy_feature):
        why = f" ({pol.error})" if pol.error else ""
        raise RuntimeError(
            f"'{policy_feature}' is not enabled on this node{why} — "
            f"on the node run: sudo tsm remote enable {policy_feature.replace('_', '-')}")


def _lines_param(params: dict[str, Any], default: int = 200) -> int:
    n = params.get("lines", default)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("lines must be a positive integer")
    return min(n, MAX_LINES)


# ---- logs (read-only) -------------------------------------------------------------------------
def _since_arg(params: dict[str, Any]) -> list[str]:
    since = params.get("since_s")
    if since is None:
        return []
    if isinstance(since, bool) or not isinstance(since, int) or not 1 <= since <= 30 * 86400:
        raise ValueError("since_s must be between 1 and 2592000 seconds")
    return [f"--since=-{since}s"]


def journal_argv(params: dict[str, Any]) -> list[str]:
    """journalctl argv for one allow-listed source (shared with the unprivileged first attempt)."""
    source = str(params.get("source", ""))
    n = _lines_param(params)
    base = [_which("journalctl"), "--no-pager", "-o", "short-iso", f"--lines={n}", *_since_arg(params)]
    if source == "kernel":
        return [*base, "-k"]
    if source == "previous-boot":
        return [*base, "-b", "-1", "-p", "warning"]
    if source in JOURNAL_UNITS:
        return [*base, "-u", JOURNAL_UNITS[source]]
    raise ValueError(f"unknown log source '{source}' (known: kernel, previous-boot, "
                     f"{', '.join(sorted(JOURNAL_UNITS))})")


def op_journal(params: dict[str, Any]) -> dict[str, Any]:
    """Last lines of one allow-listed unit, the previous boot's warnings, or the kernel ring."""
    source = str(params.get("source", ""))
    n = _lines_param(params)
    rc, out, err = RUN(journal_argv(params), 20)
    if rc != 0 and not out.strip():
        if source == "kernel":                       # no journal on this box: fall back to dmesg
            rc, out, err = RUN([_which("dmesg"), "--ctime", "--time-format=iso"], 15)
            if rc == 0:
                out = "\n".join(out.splitlines()[-n:])
        if rc != 0:
            raise RuntimeError(f"journalctl failed: {(err or out).strip()[:300]}")
    return {"source": source, "text": out[-2_000_000:]}


# ---- boot entries (UEFI) ----------------------------------------------------------------------
def parse_efibootmgr(text: str) -> dict[str, Any]:
    info: dict[str, Any] = {"current": None, "next": None, "order": [], "entries": []}
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("BootCurrent:"):
            info["current"] = line.split(":", 1)[1].strip().upper()
        elif line.startswith("BootNext:"):
            info["next"] = line.split(":", 1)[1].strip().upper()
        elif line.startswith("BootOrder:"):
            info["order"] = [x.strip().upper() for x in line.split(":", 1)[1].split(",") if x.strip()]
        else:
            m = _ENTRY_RE.match(line)
            if m:
                label = m.group(3)
                kind = "usb" if _USB_RE.search(label) else "network" if _NETWORK_RE.search(label) else "disk"
                info["entries"].append({"num": m.group(1).upper(), "active": m.group(2) == "*",
                                        "label": label[:160], "kind": kind})
    return info


def op_boot_status(params: dict[str, Any]) -> dict[str, Any]:
    rc, out, err = RUN([_which("efibootmgr")], 10)
    if rc == 127:
        return {"available": False, "reason": "efibootmgr is not installed (sudo apt install efibootmgr)"}
    if rc != 0:
        return {"available": False, "reason": (err or out).strip()[:300] or "efibootmgr failed"}
    return {"available": True, **parse_efibootmgr(out)}


def _pick_network_entry(info: dict[str, Any]) -> dict[str, Any]:
    cands = [e for e in info["entries"] if e["kind"] == "network" and e["active"]]
    ipv4 = [e for e in cands if re.search(r"(?i)ipv4", e["label"]) and not re.search(r"(?i)http", e["label"])]
    pool = ipv4 or cands
    if not pool:
        raise RuntimeError("the firmware lists no network boot entry — run `tsm node boot` "
                           "to see what it offers")
    if len(pool) > 1:
        names = "; ".join(f"{e['num']} {e['label']}" for e in pool)
        raise RuntimeError(f"several network entries match — pick one by number: {names}")
    return pool[0]


def _do_boot_next(target: str) -> dict[str, Any]:
    status = op_boot_status({})
    if not status.get("available"):
        raise RuntimeError(status["reason"])
    if target == "network":
        entry = _pick_network_entry(status)
    elif _BOOTNUM_RE.match(target):
        entry = next((e for e in status["entries"] if e["num"] == target.upper()), None)
        if entry is None:
            raise RuntimeError(f"no boot entry {target.upper()} on this machine")
    else:
        raise ValueError("target must be 'network' or a 4-digit boot entry number such as 0003")
    rc, out, err = RUN([_which("efibootmgr"), "--bootnext", entry["num"]], 10)
    if rc != 0:
        raise RuntimeError(f"efibootmgr --bootnext failed: {(err or out).strip()[:300]}")
    after = op_boot_status({})
    if after.get("next") != entry["num"]:
        raise RuntimeError("efibootmgr accepted the command but BootNext did not change — the firmware "
                           "may not allow it")
    return {"next": entry["num"], "label": entry["label"], "kind": entry["kind"]}


def op_boot_next(params: dict[str, Any]) -> dict[str, Any]:
    _need("boot_next")
    return _do_boot_next(str(params.get("target", "")))


def _do_boot_next_clear() -> dict[str, Any]:
    rc, out, err = RUN([_which("efibootmgr"), "--delete-bootnext"], 10)
    if rc != 0:
        raise RuntimeError(f"efibootmgr failed: {(err or out).strip()[:300]}")
    return {"next": None}


def op_boot_next_clear(params: dict[str, Any]) -> dict[str, Any]:
    _need("boot_next")
    return _do_boot_next_clear()


# ---- power ------------------------------------------------------------------------------------
def busy_package_manager(proc: Optional[Path] = None) -> Optional[str]:
    """Name of a running package manager, if any (a reboot mid-upgrade can break the install)."""
    try:
        for d in (proc or PROC).iterdir():
            if not d.name.isdigit():
                continue
            try:
                comm = (d / "comm").read_text().strip()
            except OSError:
                continue
            if comm in _PKG_MANAGERS:
                return comm
    except OSError:
        pass
    return None


def _maintenance_running() -> bool:
    from ..agent import maintenance
    try:
        for p in maintenance.ROOT.glob("*.json"):
            if json.loads(p.read_text()).get("state") not in ("completed", "failed"):
                return True
    except (OSError, ValueError):
        pass
    return False


def _do_power(action: str, delay_s: int, force: bool = False) -> dict[str, Any]:
    """``force`` skips the two safety checks and exists only for ``sudo tsm node … --force``; the
    socket operation :func:`op_power` never passes it."""
    if action not in ("reboot", "poweroff"):
        raise ValueError("action must be 'reboot' or 'poweroff'")
    if isinstance(delay_s, bool) or not isinstance(delay_s, int) or not 2 <= delay_s <= 600:
        raise ValueError("delay_s must be between 2 and 600 seconds")
    if not force and _maintenance_running():
        raise RuntimeError("a coordinated maintenance run is in progress on this node — "
                           "finish or release it first (Updates page), or use --force")
    busy = None if force else busy_package_manager()
    if busy:
        raise RuntimeError(f"{busy} is running; rebooting now could leave the OS half-upgraded — wait for it "
                           f"(or use --force)")
    unit = POWER_UNIT.format(action=action)
    rc, out, _ = RUN([_which("systemctl"), "is-active", f"{unit}.timer"], 5)
    if rc == 0 and out.strip() == "active":
        return {"scheduled": action, "in_s": None, "already_pending": True}
    rc, out, err = RUN([_which("systemd-run"), f"--unit={unit}", f"--on-active={delay_s}s",
                        "--timer-property=AccuracySec=1s", f"--description=TwinSpark remote {action}",
                        _which("systemctl"), action], 10)
    if rc != 0:
        raise RuntimeError(f"could not schedule the {action}: {(err or out).strip()[:300]}")
    return {"scheduled": action, "in_s": delay_s, "already_pending": False}


def op_power(params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action", ""))
    if action not in ("reboot", "poweroff"):
        raise ValueError("action must be 'reboot' or 'poweroff'")
    _need(action)
    return _do_power(action, params.get("delay_s", 5))


def _do_power_cancel() -> dict[str, Any]:
    stopped = []
    for action in ("reboot", "poweroff"):
        unit = POWER_UNIT.format(action=action)
        rc, out, _ = RUN([_which("systemctl"), "is-active", f"{unit}.timer"], 5)
        if rc == 0 and out.strip() == "active":
            RUN([_which("systemctl"), "stop", f"{unit}.timer"], 10)
            stopped.append(action)
    return {"cancelled": stopped}


def op_power_cancel(params: dict[str, Any]) -> dict[str, Any]:
    # cancelling is always allowed: it can only prevent a power change
    return _do_power_cancel()


# ---- Wake-on-LAN setting ----------------------------------------------------------------------
def _physical_ifaces() -> list[str]:
    try:
        return sorted(p.name for p in SYS_NET.iterdir() if (p / "device").exists())
    except OSError:
        return []


def parse_ethtool_wol(text: str) -> dict[str, Optional[str]]:
    supports = wake = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Supports Wake-on:"):
            supports = s.split(":", 1)[1].strip()
        elif s.startswith("Wake-on:"):
            wake = s.split(":", 1)[1].strip()
    return {"supports": supports, "mode": wake}


def op_wol_status(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in _physical_ifaces():
        rc, text, _ = RUN([_which("ethtool"), name], 8)
        if rc == 127:
            return {"available": False, "reason": "ethtool is not installed (sudo apt install ethtool)",
                    "interfaces": {}}
        info = parse_ethtool_wol(text) if rc == 0 else {"supports": None, "mode": None}
        try:
            info["mac"] = (SYS_NET / name / "address").read_text().strip()
        except OSError:
            info["mac"] = None
        out[name] = info
    return {"available": True, "interfaces": out}


def _do_wol_set(iface: str, mode: str) -> dict[str, Any]:
    if not _IFACE_RE.match(iface) or iface not in _physical_ifaces():
        raise ValueError(f"'{iface}' is not a physical network interface of this machine")
    if mode not in ("g", "d"):
        raise ValueError("mode must be 'g' (wake on magic packet) or 'd' (off)")
    rc, out, err = RUN([_which("ethtool"), "-s", iface, "wol", mode], 8)
    if rc != 0:
        raise RuntimeError(f"ethtool -s {iface} wol {mode} failed: {(err or out).strip()[:300]}")
    rc, text, _ = RUN([_which("ethtool"), iface], 8)
    state = parse_ethtool_wol(text)
    if state["mode"] and mode not in state["mode"]:
        raise RuntimeError(f"{iface} did not take the setting (reports Wake-on: {state['mode']}); "
                           f"the driver or firmware may not support Wake-on-LAN")
    return {"interface": iface, **state,
            "persist": "this lasts until the next reboot; make it permanent with netplan "
                       f"(`{iface}: {{wakeonlan: true}}`) — see docs/remote-management.md"}


def op_wol_set(params: dict[str, Any]) -> dict[str, Any]:
    _need("wol")
    return _do_wol_set(str(params.get("iface", "")), str(params.get("mode", "g")))


REMOTE_PRIV_OPS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "remote_journal": op_journal,
    "remote_boot_status": op_boot_status,
    "remote_boot_next": op_boot_next,
    "remote_boot_next_clear": op_boot_next_clear,
    "remote_power": op_power,
    "remote_power_cancel": op_power_cancel,
    "remote_wol_status": op_wol_status,
    "remote_wol_set": op_wol_set,
}


class LocalPriv:
    """The privileged helper's job, done in-process for ``sudo tsm node …`` on the machine itself.

    The policy gates *remote* callers. Someone who is already root in a shell on the box can reboot
    it, flip BootNext or change Wake-on-LAN regardless, so these calls skip the policy check. This
    class is only ever constructed by the CLI and never listens on anything.
    """

    def available(self) -> bool:
        return os.geteuid() == 0

    def call(self, op: str, params: Optional[dict[str, Any]] = None) -> Any:
        p = params or {}
        table: dict[str, Callable[[], Any]] = {
            "remote_journal": lambda: op_journal(p),
            "remote_boot_status": lambda: op_boot_status(p),
            "remote_boot_next": lambda: _do_boot_next(str(p.get("target", ""))),
            "remote_boot_next_clear": _do_boot_next_clear,
            "remote_power": lambda: _do_power(str(p.get("action", "")), p.get("delay_s", 5), bool(p.get("force"))),
            "remote_power_cancel": _do_power_cancel,
            "remote_wol_status": lambda: op_wol_status(p),
            "remote_wol_set": lambda: _do_wol_set(str(p.get("iface", "")), str(p.get("mode", "g"))),
        }
        if op not in table:
            raise ValueError(f"unknown privileged op: {op}")
        return table[op]()
