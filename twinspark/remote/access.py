"""How this node can be reached when its desktop is gone: read-only facts, each checked separately.

Before a node goes headless, the person needs a way in that does not depend on the desktop. This
module answers, without changing anything:

* is SSH running, and will it start at boot (``ssh.service`` or socket-activated ``ssh.socket``)?
* is Tailscale installed, up, and what is the node's tailnet address?
* does ``tailscale serve`` forward the management port (node A), and is that forward persistent?
* are the TwinSpark services enabled at boot, and what is the default boot target?

Facts that cannot be read are ``None`` with a note — never guessed as "fine".
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Callable, Optional

Run = Callable[[list[str], float], tuple[int, str]]


def _run(argv: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


RUN: Run = _run


def _unit(run: Run, unit: str) -> dict[str, Optional[str]]:
    rc_a, active = run(["systemctl", "is-active", unit], 5)
    rc_e, enabled = run(["systemctl", "is-enabled", unit], 5)
    return {"active": (active.splitlines() or ["unknown"])[0] if rc_a != 127 else None,
            "enabled": (enabled.splitlines() or ["unknown"])[0] if rc_e != 127 else None}


def serve_forwards(status_json: str) -> list[dict[str, Any]]:
    """TCP/HTTP(S) forwards from ``tailscale serve status --json``: [{port, target}]."""
    try:
        doc = json.loads(status_json or "{}")
    except ValueError:
        return []
    out = []
    for port, h in (doc.get("TCP") or {}).items():
        target = h.get("TCPForward") or ("https" if h.get("HTTPS") else "http" if h.get("HTTP") else None)
        out.append({"port": int(port) if str(port).isdigit() else port, "target": target})
    for hostport, web in (doc.get("Web") or {}).items():
        for path, handler in (web.get("Handlers") or {}).items():
            out.append({"port": hostport.rsplit(":", 1)[-1], "path": path, "target": handler.get("Proxy")})
    return out


def access_facts(*, manager_port: Optional[int] = None, controller: bool = False,
                 run: Optional[Run] = None, which: Callable[[str], Optional[str]] = shutil.which) -> dict[str, Any]:
    run = run or RUN
    facts: dict[str, Any] = {"notes": []}
    ssh = _unit(run, "ssh.service")
    sock = _unit(run, "ssh.socket")
    facts["ssh"] = {
        "running": "active" in (ssh["active"], sock["active"]),
        "starts_at_boot": "enabled" in (ssh["enabled"], sock["enabled"]),
        "service": ssh, "socket": sock}
    ts: dict[str, Any] = {"installed": bool(which("tailscale")), "running": None, "ip": None, "serve": None}
    if ts["installed"]:
        rc, out = run(["tailscale", "status", "--json"], 8)
        if rc == 0:
            try:
                st = json.loads(out)
                ts["running"] = st.get("BackendState") == "Running"
                ips = [ip for ip in (st.get("Self") or {}).get("TailscaleIPs") or [] if ":" not in ip]
                ts["ip"] = ips[0] if ips else None
            except ValueError:
                facts["notes"].append("tailscale status was not readable")
        else:
            facts["notes"].append("tailscale status failed: " + out[:160])
        ts["service"] = _unit(run, "tailscaled.service")
        if controller and manager_port:
            rc, out = run(["tailscale", "serve", "status", "--json"], 8)
            if rc == 0:
                fwd = serve_forwards(out)
                hit = [f for f in fwd if str(f.get("port")) == str(manager_port)]
                ts["serve"] = {"forwards": fwd, "manager_forwarded": bool(hit),
                               # `serve --bg` writes the config into tailscaled's state: it survives a reboot
                               "persistent": bool(hit)}
            else:
                ts["serve"] = {"forwards": None, "manager_forwarded": None,
                               "note": "tailscale serve status needs operator rights (sudo): " + out[:120]}
    facts["tailscale"] = ts
    units = ["twinspark-agent.service"] + (["twinspark-controller.service"] if controller else [])
    facts["services"] = {u: _unit(run, u)["enabled"] for u in units}
    rc, target = run(["systemctl", "get-default"], 5)
    facts["default_target"] = target if rc == 0 else None
    paths = []
    if facts["ssh"]["running"] and facts["ssh"]["starts_at_boot"]:
        paths.append("ssh")
    # like SSH, a path only counts if it comes back after a reboot ("unknown" when systemctl cannot say)
    if ts.get("running") and ts.get("ip") and (ts.get("service") or {}).get("enabled") not in ("disabled", "masked"):
        paths.append("tailscale")
    facts["remote_paths"] = paths
    return facts


def summary(facts: dict[str, Any]) -> str:
    """One line for a confirmation dialog or `tsm headless status`."""
    ssh, ts = facts.get("ssh") or {}, facts.get("tailscale") or {}
    bits = [("SSH running, starts at boot" if ssh.get("starts_at_boot") else "SSH running, NOT enabled at boot")
            if ssh.get("running") else "SSH not running"]
    if ts.get("installed"):
        serve = ts.get("serve") or {}
        fw = (" · manager forwarded on the tailnet" if serve.get("manager_forwarded")
              else " · manager NOT forwarded (tsm remote tailscale-serve)" if serve.get("manager_forwarded") is False
              else "")
        boot = (", NOT enabled at boot" if (ts.get("service") or {}).get("enabled") in ("disabled", "masked")
                else "")
        bits.append(f"Tailscale {'up ' + str(ts.get('ip') or '') if ts.get('running') else 'not running'}{boot}{fw}")
    else:
        bits.append("Tailscale not installed")
    if facts.get("default_target"):
        bits.append(f"boots into {facts['default_target']}")
    return "; ".join(bits)
