"""The gateway's routing state as a file, and the controller's link to a standalone gateway (master plan §3.2).

With ``gateway_mode: standalone`` the gateway runs in its own process (``tsm serve gateway``,
twinspark-gateway.service). A controller restart then no longer drops the connections of running
streams. The two processes share two things:

* **the state file** (``gateway_state_path``): routes, routing settings and the named keys' hashes,
  written by the controller atomically (temporary file, fsync, rename) with a rising ``version``. The
  gateway re-reads it when it changes and applies it in one step: a request sees the old or the new
  routing, never half of it.
* **the control socket** (``gateway_control_socket``, a Unix socket only the service user can open):
  the gateway answers which state version it applied, the requests in flight per route and the token
  counters since the last question. A switch drains only after the gateway applied the draining state.

This module imports nothing heavier than the gateway itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .gateway import Gateway, RouteState

FORMAT = "twinspark-gateway/1"
log = logging.getLogger("twinspark.gateway.state")
_ROUTE_FIELDS = ("backends", "served_model", "revision_id", "max_model_len", "draining", "down_reason",
                 "retry_after", "backend_nodes")


def export_state(gw: Gateway, version: int) -> dict[str, Any]:
    return {"format": FORMAT, "version": version, "written_at": time.time(),
            "routes": {a: {f: getattr(st, f) for f in _ROUTE_FIELDS} for a, st in gw.routes.items()},
            "settings": gw.settings, "keys": list(gw.keys.values()), "keys_managed": gw.keys_managed}


def apply_state(gw: Gateway, doc: dict[str, Any]) -> None:
    """Replace the routing in one step (no await in here). A route keeps its object, so its counters and
    the requests in flight on it carry over; a removed alias's requests in flight finish on theirs."""
    routes: dict[str, RouteState] = {}
    for alias, r in (doc.get("routes") or {}).items():
        st = gw.routes.get(alias) or RouteState(alias)
        new_backends = list(r.get("backends") or [])
        if new_backends != st.backends:
            st._down_until.clear()
            st.since = time.time()
        st.backends = new_backends
        st.served_model, st.revision_id = r.get("served_model"), r.get("revision_id")
        st.max_model_len, st.draining = r.get("max_model_len"), bool(r.get("draining"))
        st.down_reason, st.retry_after = r.get("down_reason"), int(r.get("retry_after") or 10)
        st.backend_nodes = dict(r.get("backend_nodes") or {})
        routes[alias] = st
    keys = [k for k in doc.get("keys") or [] if isinstance(k, dict)]
    on_change, gw.on_change = gw.on_change, None
    try:
        gw.routes = routes
        gw.configure(doc.get("settings") or {})
        gw.set_keys(keys, managed=bool(doc.get("keys_managed")))
    finally:
        gw.on_change = on_change


def write_atomic(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gateway-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_state(path: Path) -> Optional[dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("gateway state %s is unreadable: %s", path, exc)
        return None
    return doc if isinstance(doc, dict) and doc.get("format") == FORMAT else None


class StateFollower:
    """The standalone gateway's side: apply the state file whenever it changes."""

    def __init__(self, gw: Gateway, path: Path, *, interval: float = 0.5, on_apply=None):
        self.gw, self.path, self.interval, self.on_apply = gw, Path(path), interval, on_apply
        self.version = 0
        self._stamp: Optional[tuple] = None

    def check(self) -> bool:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return False
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        if stamp == self._stamp:
            return False
        doc = read_state(self.path)
        if doc is None:
            return False
        self._stamp = stamp
        # a changed file is applied even with the version it had before: never skip what the controller wrote
        if self.on_apply is not None:
            self.on_apply(doc)
        apply_state(self.gw, doc)
        self.version = int(doc.get("version") or 0)
        return True

    async def run(self) -> None:
        while True:
            try:
                self.check()
            except Exception:  # noqa: BLE001 - a bad file must not stop the gateway; it keeps the last state
                log.exception("could not apply the gateway state")
            await asyncio.sleep(self.interval)


class GatewayLink:
    """The controller's side: publish every change of the routing, and follow what the gateway reports."""

    def __init__(self, gw: Gateway, state_path: str | Path, socket_path: str | Path, *, interval: float = 0.5,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.gw, self.path, self.socket = gw, Path(state_path), str(socket_path)
        self.interval = interval
        # publish() runs on the event loop and in API worker threads: one writer at a time, so every
        # write gets its own version and the gateway never takes a later state for one it applied
        self._publishing = threading.Lock()
        self._transport = transport
        self._task: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None
        self.last_seen: Optional[float] = None

    def attach(self) -> None:
        """Take over what the gateway is serving (a controller restart must not clear it), then publish."""
        doc = read_state(self.path)
        if doc is not None:
            on_change, self.gw.on_change = self.gw.on_change, None
            try:
                routes = {a: r for a, r in (doc.get("routes") or {}).items()}
                apply_state(self.gw, {"routes": routes, "settings": self.gw.settings,
                                      "keys": list(self.gw.keys.values()), "keys_managed": self.gw.keys_managed})
            finally:
                self.gw.on_change = on_change
            self.gw.published_version = int(doc.get("version") or 0)
        self.gw.remote = True
        self.gw.on_change = self.publish
        self.publish()

    def publish(self) -> None:
        with self._publishing:
            version = self.gw.published_version + 1
            try:
                write_atomic(self.path, export_state(self.gw, version))
                self.gw.published_version = version
            except OSError as exc:
                self.last_error = f"could not write {self.path}: {exc}"
                log.error("%s", self.last_error)

    def _client(self) -> httpx.AsyncClient:
        transport = self._transport or httpx.AsyncHTTPTransport(uds=self.socket)
        return httpx.AsyncClient(transport=transport, base_url="http://gateway", timeout=3)

    async def poll(self) -> bool:
        """One question to the gateway; False when it does not answer (then nothing is in flight through it)."""
        try:
            async with self._client() as c:
                r = await c.get("/control/stats")
            doc = r.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self.gw.remote_ok = False
            self.last_error = f"the gateway process does not answer ({type(exc).__name__})"
            for st in self.gw.routes.values():
                st.remote_inflight, st._remote = 0, None
            return False
        self.gw.remote_ok, self.last_seen, self.last_error = True, time.time(), None
        self.gw.remote_version = int(doc.get("version") or 0)
        for alias, s in (doc.get("routes") or {}).items():
            st = self.gw.routes.get(alias)
            if st is None:
                continue
            # the process's requests go to remote_inflight: the controller's own (the playground) stay counted
            st.remote_inflight, st.requests, st.errors = int(s.get("inflight") or 0), \
                int(s.get("requests") or 0), int(s.get("errors") or 0)
            st.last_error = s.get("last_error")
            st._remote = {"oldest": s.get("oldest_inflight_s"), "backend_inflight": s.get("backend_inflight") or {}}
        for row in (doc.get("usage") or {}).get("rows") or []:
            cell = self.gw.usage.setdefault((row["day"], row["key_id"], row["alias"]),
                                            {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                             "estimated_requests": 0})
            for k in cell:
                cell[k] += int(row.get(k) or 0)
        for key_id, t in ((doc.get("usage") or {}).get("last_used") or {}).items():
            self.gw.last_used[key_id] = max(t, self.gw.last_used.get(key_id, 0))
        return True

    async def run(self) -> None:
        while True:
            await self.poll()
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def status(self) -> dict[str, Any]:
        return {"mode": "standalone", "reachable": self.gw.remote_ok, "published_version": self.gw.published_version,
                "applied_version": self.gw.remote_version, "last_seen": self.last_seen, "error": self.last_error}
