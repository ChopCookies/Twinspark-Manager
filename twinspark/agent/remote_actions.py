"""Typed agent actions for remote management (diagnostics, power, boot, Wake-on-LAN setting).

Reading actions (status, logs, bundle) are always available. Actions that change the machine are
forwarded to ``tsm-privd``, which checks the root-owned policy itself — this module never decides
whether a reboot is allowed, it only asks.
"""

from __future__ import annotations

import base64
import os
import platform
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import __version__, hostprobe
from ..remote import bundle, logs, privops
from ..remote.policy import PolicySource, RemotePolicy
from ..security import SECRET_SLOTS

if TYPE_CHECKING:
    from .actions import AgentActions

MAX_BUNDLE_B64 = 12 * 1024 ** 2


def _uptime_s() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build(actions: "AgentActions") -> dict[str, Callable[[dict], Any]]:
    cfg = actions.config
    rm = cfg.remote_mgmt
    source = PolicySource(path=Path(rm.policy_path), require_root=rm.require_root_owned)

    def policy() -> RemotePolicy:
        return source.current()

    def priv(op: str, params: dict[str, Any]) -> Any:
        return actions.privd.call(op, params)

    def status(params: dict) -> dict:
        pol = policy()
        which = privops.shutil.which
        try:
            ifaces = [i.as_dict() for i in hostprobe.list_interfaces() if not i.virtual]
        except Exception:  # noqa: BLE001 - interface listing is a courtesy
            ifaces = []
        wol: Any = None
        if actions.privd.available():
            try:
                wol = priv("remote_wol_status", {})
            except Exception as exc:  # noqa: BLE001
                wol = {"available": False, "reason": str(exc)[:200]}
        for i in ifaces:
            info = ((wol or {}).get("interfaces") or {}).get(i["name"])
            if info:
                i["wol"] = info.get("mode")
                i["wol_supported"] = info.get("supports")
        pending = []
        for action in ("reboot", "poweroff"):
            rc, out, _ = privops.RUN([privops._which("systemctl"), "is-active",
                                      f"twinspark-remote-{action}.timer"], 4)
            if rc == 0 and out.strip() == "active":
                pending.append(action)
        host = cfg.listener.bind if cfg.listener.bind not in ("0.0.0.0", "::") else "127.0.0.1"
        return {
            "node": cfg.node.node_id, "hostname": platform.node(), "version": __version__,
            "kernel": platform.release(), "boot_id": _boot_id(), "uptime_s": _uptime_s(),
            "booted_at": time.time() - (_uptime_s() or 0), "loadavg": list(os.getloadavg()),
            "policy": pol.as_dict(), "enabled": pol.enabled,
            "terminal_service": _tcp_open(host, rm.terminal_port),
            "privd": actions.privd.available(),
            "tools": {n: bool(which(n, path="/usr/sbin:/usr/bin:/sbin:/bin"))
                      for n in ("efibootmgr", "ethtool", "systemd-run", "journalctl")},
            "interfaces": ifaces, "power_pending": pending,
            "wol_available": (wol or {}).get("available") if isinstance(wol, dict) else None,
        }

    def get_logs(params: dict) -> dict:
        return logs.read_logs(str(params.get("source", "agent")), params.get("lines", 200), params.get("since_s"),
                              params.get("grep"), priv=priv if actions.privd.available() else None)

    def get_bundle(params: dict) -> dict:
        secret_values = []
        for slot in SECRET_SLOTS:
            try:
                v = actions.vault.get(slot)
            except Exception:  # noqa: BLE001 - an unreadable slot cannot be redacted, nor leaked
                continue
            if v:
                secret_values.append(v)
        slots = []
        try:
            slots = sorted(p.stem for p in Path(cfg.secrets_dir).glob("*.enc"))
        except OSError:
            pass
        try:
            facts = actions.hardware_facts({})
        except Exception as exc:  # noqa: BLE001
            facts = {"error": str(exc)[:200]}
        name, data, problems = bundle.build_bundle(
            cfg.node.node_id, version=__version__, facts=facts, config_dump=cfg.model_dump(mode="json"),
            vault_slots=slots, secret_values=secret_values, priv=priv if actions.privd.available() else None)
        b64 = base64.b64encode(data).decode()
        if len(b64) > MAX_BUNDLE_B64:
            raise ValueError("support bundle too large to send")
        return {"name": name, "size": len(data), "problems": problems, "b64": b64}

    def power(params: dict) -> dict:
        args = {"action": params.get("action"), "delay_s": params.get("delay_s", 5)}
        if params.get("force") is True:               # only honoured by `sudo tsm node …` (LocalPriv)
            args["force"] = True
        return priv("remote_power", args)

    def boot_next(params: dict) -> dict:
        return priv("remote_boot_next", {"target": params.get("target")})

    def wol_set(params: dict) -> dict:
        return priv("remote_wol_set", {"iface": params.get("iface"), "mode": params.get("mode", "g")})

    return {
        "remote_status": status,
        "remote_logs": get_logs,
        "remote_bundle": get_bundle,
        "remote_power": power,
        "remote_power_cancel": lambda p: priv("remote_power_cancel", {}),
        "remote_boot_status": lambda p: priv("remote_boot_status", {}),
        "remote_boot_next": boot_next,
        "remote_boot_next_clear": lambda p: priv("remote_boot_next_clear", {}),
        "remote_wol_status": lambda p: priv("remote_wol_status", {}),
        "remote_wol_set": wol_set,
    }
