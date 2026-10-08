"""Reach the management GUI over Tailscale without exposing it anywhere else.

The controller listens on ``127.0.0.1:8443``. ``tailscale serve --bg --tcp=8443 tcp://127.0.0.1:8443``
makes that port reachable at the node's tailnet address only, survives reboots (``--bg`` stores it in
tailscaled's state) and needs no HTTPS certificate feature on the tailnet. The management key is still
required for every request. Nothing here runs unless ``apply`` / ``remove`` is called.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import httpx

from .access import RUN, Run, serve_forwards


def commands(port: int) -> dict[str, list[str]]:
    return {"apply": ["tailscale", "serve", "--bg", f"--tcp={port}", f"tcp://127.0.0.1:{port}"],
            "remove": ["tailscale", "serve", f"--tcp={port}", "off"]}


def state(port: int, run: Optional[Run] = None) -> dict[str, Any]:
    import json
    run = run or RUN
    rc, out = run(["tailscale", "status", "--json"], 8)
    if rc == 127:
        return {"installed": False, "running": False, "ip": None, "forwarded": None,
                "note": "tailscale is not installed (https://tailscale.com/download/linux)"}
    st: dict[str, Any] = {}
    if rc == 0:
        try:
            st = json.loads(out)
        except ValueError:
            st = {}
    ips = [ip for ip in (st.get("Self") or {}).get("TailscaleIPs") or [] if ":" not in ip]
    res: dict[str, Any] = {"installed": True, "running": st.get("BackendState") == "Running",
                           "ip": ips[0] if ips else None, "forwarded": None, "note": None}
    if rc != 0:
        res["note"] = "tailscale status failed: " + out[:200]
    rc, out = run(["tailscale", "serve", "status", "--json"], 8)
    if rc == 0:
        res["forwarded"] = any(str(f.get("port")) == str(port) for f in serve_forwards(out))
        res["forwards"] = serve_forwards(out)
    else:
        res["note"] = (res["note"] or "") + " serve status needs root or tailscale operator rights"
    return res


def apply(port: int, run: Optional[Run] = None) -> tuple[bool, str]:
    rc, out = (run or RUN)(commands(port)["apply"], 30)
    return rc == 0, out


def remove(port: int, run: Optional[Run] = None) -> tuple[bool, str]:
    rc, out = (run or RUN)(commands(port)["remove"], 30)
    return rc == 0, out


def verify(ip: str, port: int, get: Callable[..., Any] = httpx.get, scheme: str = "http") -> tuple[bool, str]:
    """Fetch the unauthenticated health endpoint through the tailnet address.

    Only reachability is checked: with TLS the certificate is usually issued for localhost, not for the
    tailnet address, so it is not verified here (the browser shows it to the user).
    """
    url = f"{scheme}://{ip}:{port}/api/v1/health"
    try:
        r = get(url, timeout=8, **({"verify": False} if scheme == "https" else {}))
    except httpx.HTTPError as exc:
        return False, f"{url} did not answer ({type(exc).__name__})"
    return r.status_code == 200, f"{url} answered {r.status_code}"
