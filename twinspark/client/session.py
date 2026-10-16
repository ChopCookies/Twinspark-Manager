"""The workstation client's live state: one connection to a pair of Sparks at a time.

``ClientSession`` ties the connection layer together — saved connections, the credential store, the
supervised SSH tunnel and the version handshake — and answers the three questions the GUI shows
separately: is the *client connected* (transport), are *both agents compatible* (versions, runtime
mode), and is a *model ready* (a route with backends on node A's gateway). A transport problem is never
reported as a model failure.

Node A owns the deployment. Nothing here keeps state the Sparks need: closing the laptop only ends the
forwards; an accepted switch, the gateway and the model carry on, and reconnecting picks them up again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from .. import __version__
from .connections import Connection, ConnectionStore
from .discovery import CredentialAccessError, DiscoveryError, fetch_credentials, read_controller_nodes
from .paths import client_home
from .secrets import CredentialStore
from .ssh import SSHError, Tunnel, known_keys, plan_forwards, run_remote

log = logging.getLogger("twinspark.client")

CONNECT_TIMEOUT_S = 60.0
NODE_CACHE_S = 4.0


def _minor(version: Optional[str]) -> Optional[tuple[int, int]]:
    try:
        parts = str(version).split(".")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError, TypeError):
        return None


def compatibility(client_version: str, node_version: Optional[str], capabilities: Optional[list]) -> dict[str, Any]:
    """Compare this client with node A's TwinSpark. Same minor version = ok; the client works with an
    older node (it proxies the node's API) but says which 0.6 features that node lacks."""
    c, n = _minor(client_version), _minor(node_version)
    if n is None:
        return {"state": "unknown", "note": "node A did not report its TwinSpark version"}
    if c == n:
        return {"state": "ok", "note": f"client and node A both run TwinSpark {n[0]}.{n[1]}"}
    if n < c:
        return {"state": "older-node",
                "note": (f"node A runs TwinSpark {node_version}, this client {client_version}. The GUI works; the "
                         f"lean services, the activation journal and the startup timeline need {c[0]}.{c[1]} on "
                         f"both Sparks — update them (docs/client.md, 'Updating the Sparks').")}
    return {"state": "newer-node",
            "note": (f"node A runs TwinSpark {node_version}, newer than this client ({client_version}). "
                     f"Update the client: pipx upgrade twinspark-manager")}


class ClientSession:
    """One live connection plus everything saved about the others. Thread model: one asyncio loop."""

    def __init__(self, home: Optional[Path] = None, *, run: Callable = run_remote,
                 tunnel_factory: Callable[..., Tunnel] = Tunnel, http: Optional[httpx.AsyncClient] = None,
                 connect_timeout: float = CONNECT_TIMEOUT_S):
        self.home = Path(home) if home else client_home()
        self.connections = ConnectionStore(self.home)
        self.creds = CredentialStore(self.home)
        self.run = run
        self.tunnel_factory = tunnel_factory
        self.connect_timeout = connect_timeout
        self.current: Optional[Connection] = None
        self.tunnel: Optional[Tunnel] = None
        self.node_cfg: dict[str, Any] = {}
        self.phase = "idle"                    # idle | connecting | connected | needs-action | error
        self.need: Optional[str] = None       # host-key | auth | keys | None
        self.message: Optional[str] = None
        self._http = http
        self._own_http = http is None
        self._node: dict[str, Any] = {}
        self._node_at = 0.0
        self._node_lock = asyncio.Lock()
        self._connect_task: Optional[asyncio.Task] = None

    # ---- addresses ------------------------------------------------------------------------------
    def _forward(self, label: str) -> Optional[int]:
        if self.tunnel is None:
            return None
        return next((f.local_port for f in self.tunnel.forwards if f.label == label), None)

    def node_url(self) -> Optional[str]:
        """Where node A's management API answers from this computer, or None when not connected."""
        c = self.current
        if c is None:
            return None
        if not c.uses_ssh:
            return (c.direct_url or "").rstrip("/") or None
        port = self._forward("api")
        scheme = "https" if self.node_cfg.get("manager_tls") else "http"
        return f"{scheme}://127.0.0.1:{port}" if port and self.tunnel and self.tunnel.state == "up" else None

    def gateway_url(self) -> Optional[str]:
        """The OpenAI-compatible base URL local apps use (``…/v1``)."""
        c = self.current
        if c is None:
            return None
        if not c.uses_ssh:
            host = c.host if ":" not in c.host else f"[{c.host}]"
            return f"http://{host}:{c.gateway_port}/v1"
        port = self._forward("gateway")
        scheme = "https" if self.node_cfg.get("gateway_tls") else "http"
        return f"{scheme}://127.0.0.1:{port}/v1" if port else None

    def management_key(self) -> Optional[str]:
        return self.creds.get(self.current.id, "management_key") if self.current else None

    def inference_key(self) -> Optional[str]:
        return self.creds.get(self.current.id, "inference_key") if self.current else None

    @property
    def http(self) -> httpx.AsyncClient:
        """The HTTP client for node A. Through an SSH forward the target is loopback at the end of an
        authenticated tunnel, and node A's own certificate (if any) names localhost, so it is not
        verified there. A direct connection crosses the LAN: its certificate must verify."""
        direct = bool(self.current and not self.current.uses_ssh)
        if self._http is not None and not self._own_http:
            return self._http                    # injected (tests)
        if self._http is None or getattr(self._http, "_tsm_direct", None) != direct:
            if self._http is not None and self._own_http:
                old = self._http
                asyncio.get_running_loop().create_task(old.aclose())
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0), trust_env=False,
                                           verify=direct)
            self._http._tsm_direct = direct      # type: ignore[attr-defined]
            self._own_http = True
        return self._http

    # ---- connecting -----------------------------------------------------------------------------
    def _set(self, phase: str, need: Optional[str] = None, message: Optional[str] = None) -> None:
        self.phase, self.need, self.message = phase, need, message

    async def connect(self, conn_id: str, *, wait: bool = True) -> dict[str, Any]:
        """Connect to a saved connection (ends the current one). ``wait=False`` returns at once and the
        connection is made in the background; poll :meth:`status`."""
        conn = self.connections.get(conn_id)
        if conn is None:
            raise KeyError(conn_id)
        await self.disconnect()
        self.current = conn
        self._set("connecting", None, f"connecting to {conn.name} ({conn.host})")
        self._connect_task = asyncio.create_task(self._connect(conn))
        if wait:
            await asyncio.shield(self._connect_task)
        return await self.status()

    async def _connect(self, conn: Connection) -> None:
        try:
            if conn.uses_ssh:
                if not known_keys(self.home, conn.host, conn.ssh_port):
                    self._set("needs-action", "host-key", f"Check the host key of {conn.host} before connecting.")
                    return
                try:
                    self.node_cfg = await read_controller_nodes(conn, home=self.home, run=self.run)
                except SSHError as exc:
                    self._ssh_problem(exc)
                    return
                except DiscoveryError as exc:
                    self._set("error", None, str(exc))
                    return
                changes = {k: self.node_cfg[k] for k, attr in (("manager_port", "manager_port"),
                                                                ("gateway_port", "gateway_port"))
                           if self.node_cfg.get(k) and self.node_cfg[k] != getattr(conn, attr)}
                if changes:
                    conn = self.connections.update(conn.id, **changes)
                forwards = plan_forwards(conn, manager_host=self.node_cfg.get("manager_forward_host", "127.0.0.1"),
                                         gateway_host=self.node_cfg.get("gateway_forward_host", "127.0.0.1"))
                ports = {f.label: f.local_port for f in forwards}
                # keep the picked local ports: apps configured with http://127.0.0.1:<port> keep working
                if (conn.local_api_port, conn.local_gateway_port) != (ports["api"], ports["gateway"]):
                    conn = self.connections.update(conn.id, local_api_port=ports["api"],
                                                   local_gateway_port=ports["gateway"])
                self.current = conn
                self.tunnel = self.tunnel_factory(conn, forwards, home=self.home, on_change=self._tunnel_changed)
                await self.tunnel.start()
                try:
                    state = await self.tunnel.wait_for("up", "auth-failed", "host-key-problem", "unreachable",
                                                       timeout=self.connect_timeout)
                except asyncio.TimeoutError:
                    state = self.tunnel.state
                if state != "up":
                    self._tunnel_problem()
                    return
                if not self.management_key():
                    try:
                        keys = await fetch_credentials(conn, home=self.home, run=self.run)
                    except CredentialAccessError as exc:
                        self._set("needs-action", "keys", str(exc))
                        return
                    except SSHError as exc:
                        self._ssh_problem(exc)
                        return
                    for name in ("management_key", "inference_key"):
                        if keys.get(name):
                            self.creds.set(conn.id, name, keys[name])
            elif not self.management_key():
                self._set("needs-action", "keys", "Paste the management key of node A (`sudo tsm init --show` "
                                                  "there) to use a direct connection.")
                return
            node = await self.refresh_node(force=True)
            if node.get("key_refused"):
                self._set("needs-action", "keys", node["error"])
                return
            if not node.get("reachable"):
                self._set("error", None, node.get("error") or "node A's management API did not answer")
                return
            self.connections.mark_connected(conn.id)
            self._set("connected", None, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the GUI shows it; the client keeps running
            log.exception("connecting to %s failed", conn.host)
            self._set("error", None, f"connecting failed: {type(exc).__name__}: {exc}"[:300])

    def _ssh_problem(self, exc: SSHError) -> None:
        if exc.kind == "auth":
            self._set("needs-action", "auth", str(exc))
        elif exc.kind in ("host-key-unknown", "host-key-changed"):
            self._set("needs-action", "host-key", str(exc))
        else:
            self._set("error", None, str(exc))

    def _tunnel_problem(self) -> None:
        t = self.tunnel
        state = t.state if t else "stopped"
        if state == "auth-failed":
            self._set("needs-action", "auth", t.last_error if t else None)
        elif state == "host-key-problem":
            self._set("needs-action", "host-key", t.last_error if t else None)
        else:
            self._set("error", None, (t.last_error if t else None) or f"the tunnel is {state}")

    async def _tunnel_changed(self, tunnel: Tunnel) -> None:
        if tunnel is not self.tunnel or self.phase == "connecting":
            return
        if tunnel.state in ("auth-failed", "host-key-problem"):
            self._tunnel_problem()
        self._node_at = 0.0                   # re-ask node A after any transport change

    async def disconnect(self) -> None:
        task, self._connect_task = self._connect_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        tunnel, self.tunnel = self.tunnel, None
        if tunnel is not None:
            await tunnel.stop()
        self.current = None
        self.node_cfg = {}
        self._node, self._node_at = {}, 0.0
        self._set("idle")

    async def aclose(self) -> None:
        await self.disconnect()
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---- node A ---------------------------------------------------------------------------------
    async def node_request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """One request to node A's management API with the stored key (raises ConnectionError when the
        transport is down, so callers can tell that apart from an answer of node A)."""
        base, key = self.node_url(), self.management_key()
        if not base:
            raise ConnectionError(self.transport_note())
        headers = dict(kw.pop("headers", None) or {})
        if key:
            headers["x-api-key"] = key
        try:
            return await self.http.request(method, base + path, headers=headers, **kw)
        except httpx.TransportError as exc:
            raise ConnectionError(f"node A did not answer through the connection ({type(exc).__name__})") from exc

    def transport_note(self) -> str:
        if self.current is None:
            return "not connected to the Sparks — open Connection and choose a Spark"
        if self.tunnel is not None and self.tunnel.state != "up":
            return (f"the connection to {self.current.host} is {self.tunnel.state}"
                    + (f": {self.tunnel.last_error}" if self.tunnel.last_error else ""))
        return self.message or f"connecting to {self.current.host}"

    async def refresh_node(self, *, force: bool = False) -> dict[str, Any]:
        """Node A's version and capabilities, both agents, and the serving state (cached briefly)."""
        async with self._node_lock:
            if not force and time.monotonic() - self._node_at < NODE_CACHE_S:
                return self._node
            out: dict[str, Any] = {"reachable": False}
            try:
                h = await self.node_request("GET", "/api/v1/health", timeout=10)
                health = h.json() if h.status_code == 200 else {}
                out.update(reachable=h.status_code == 200, version=health.get("version"), api=health.get("api", 1),
                           capabilities=health.get("capabilities") or [], busy=health.get("busy"))
                out["compat"] = compatibility(__version__, out.get("version"), out.get("capabilities"))
                st = await self.node_request("GET", "/api/v1/status", timeout=15)
                if st.status_code == 401:
                    out["error"] = "node A refused the stored management key — paste it again (Connection)"
                    out["key_refused"] = True
                elif st.status_code == 200:
                    out.update(self._summarize(st.json(), out.get("version")))
            except ConnectionError as exc:
                out["error"] = str(exc)
            except (ValueError, httpx.HTTPError) as exc:
                out["error"] = f"node A answered something unexpected ({type(exc).__name__})"
            self._node, self._node_at = out, time.monotonic()
            return out

    @staticmethod
    def _summarize(st: dict[str, Any], node_version: Optional[str]) -> dict[str, Any]:
        agents = {}
        for n, facts in (st.get("nodes") or {}).items():
            facts = facts or {}
            v = facts.get("agent_version")
            agents[n] = {"version": v, "runtime_mode": facts.get("runtime_mode"),
                         "reachable": bool(facts) and "error" not in facts,
                         "ok": bool(v) and _minor(v) == _minor(node_version)}
        routes = st.get("routes") or []
        ready = [r for r in routes if r.get("backends")]
        active = st.get("active")
        return {"agents": agents,
                "agents_ok": bool(agents) and all(a["ok"] for a in agents.values()),
                "model": {"active": active, "ready": bool(active and ready), "busy": st.get("busy"),
                          "aliases": [r.get("alias") for r in ready],
                          "dry_run": any(a.get("runtime_mode") == "dry-run" for a in agents.values())}}

    async def models(self) -> list[dict[str, Any]]:
        """``/v1/models`` through the gateway forward, as local apps will see it."""
        url, key = self.gateway_url(), self.inference_key()
        if not url:
            raise ConnectionError(self.transport_note())
        r = await self.http.get(url + "/models", headers={"authorization": f"Bearer {key}"} if key else {},
                                timeout=10)
        r.raise_for_status()
        return list((r.json() or {}).get("data") or [])

    # ---- the view ------------------------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        c = self.current
        node = await self.refresh_node() if self.phase == "connected" else (self._node or {})
        transport = (self.tunnel.snapshot() if self.tunnel else
                     {"state": "direct" if c and not c.uses_ssh else "stopped"})
        connected = self.phase == "connected" and (transport["state"] in ("up", "direct"))
        return {
            "client_version": __version__,
            "connection": c.to_dict() if c else None,
            "phase": self.phase, "need": self.need, "message": self.message,
            "states": {
                "client_connected": connected,
                "agents_compatible": bool(connected and node.get("agents_ok")),
                "model_ready": bool(connected and (node.get("model") or {}).get("ready")),
            },
            "transport": transport,
            "node": node,
            "endpoint": {"base_url": self.gateway_url(), "manager_url": self.node_url(),
                         "has_inference_key": bool(self.inference_key())},
            "credentials": {"backend": self.creds.backend, "management_key": bool(self.management_key()),
                            "inference_key": bool(self.inference_key())},
        }

    def last_connection(self) -> Optional[Connection]:
        used = [c for c in self.connections.list() if c.last_connected_at]
        return max(used, key=lambda c: c.last_connected_at) if used else None
