"""Stable API Gateway (spec §15, §16, §43).

One stable OpenAI-compatible endpoint (``http://spark-a:8000/v1``). Clients use
aliases (``default``, ``coder`` ...) as the ``model`` name; the gateway rewrites
it to the served name of whatever revision currently backs the alias.

* During a switch the alias is *draining*: new requests get ``503`` +
  ``Retry-After``; in-flight requests (including open SSE streams) finish.
* Streaming is proxied chunk by chunk (no buffering, no re-encoding).
* Replicated topologies round-robin across backends and skip a backend that
  just refused connections.
* The controller's watchdog can mark a route *down* (model crashed) so clients
  get a clean 503 instead of a proxy error.
"""

from __future__ import annotations

import asyncio
import itertools
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

BACKEND_BACKOFF_S = 10.0


@dataclass
class RouteState:
    alias: str
    backends: list[str] = field(default_factory=list)
    served_model: Optional[str] = None
    revision_id: Optional[str] = None
    max_model_len: Optional[int] = None
    draining: bool = False
    down_reason: Optional[str] = None
    retry_after: int = 10
    inflight: int = 0
    requests: int = 0
    errors: int = 0
    last_error: Optional[str] = None
    since: float = field(default_factory=time.time)
    _rr: itertools.count = field(default_factory=itertools.count, repr=False)
    _idle: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _down_until: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._idle.set()

    @property
    def status(self) -> str:
        if self.draining:
            return "switching"
        if self.down_reason:
            return "down"
        return "serving" if self.backends else "idle"

    def candidates(self) -> list[str]:
        """Backends in round-robin order, recently failing ones last."""
        if not self.backends:
            return []
        start = next(self._rr) % len(self.backends)
        order = self.backends[start:] + self.backends[:start]
        now = time.monotonic()
        healthy = [b for b in order if self._down_until.get(b, 0) <= now]
        return healthy + [b for b in order if b not in healthy]

    def next_backend(self) -> str:
        return self.candidates()[0]

    def backend_failed(self, backend: str) -> None:
        self._down_until[backend] = time.monotonic() + BACKEND_BACKOFF_S

    def acquire(self) -> None:
        self.inflight += 1
        self.requests += 1
        self._idle.clear()

    def release(self) -> None:
        self.inflight = max(0, self.inflight - 1)
        if self.inflight == 0:
            self._idle.set()

    def to_payload(self) -> dict:
        return {"alias": self.alias, "status": self.status, "model": self.served_model,
                "revision": self.revision_id, "inflight": self.inflight,
                "retry_after": self.retry_after, "requests": self.requests,
                "errors": self.errors, "last_error": self.last_error,
                "max_model_len": self.max_model_len, "down_reason": self.down_reason,
                "backends": len(self.backends), "since": self.since}


class Gateway:
    """Alias routing + proxying. HTTP-framework agnostic apart from httpx."""

    def __init__(self, inference_api_key: Optional[str], backend_api_key: Optional[str] = None,
                 client: Optional[httpx.AsyncClient] = None):
        # None disables inference auth — only acceptable on localhost-only setups;
        # the CLI refuses to start a non-local gateway without a key.
        self.inference_api_key = inference_api_key
        self.backend_api_key = backend_api_key
        self.routes: dict[str, RouteState] = {}
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=1800, write=60, pool=10),
            limits=httpx.Limits(max_connections=512, max_keepalive_connections=64),
        )

    # ---- controller-facing -------------------------------------------------
    def set_route(self, alias: str, backends: list[str], served_model: str,
                  revision_id: Optional[str] = None, max_model_len: Optional[int] = None) -> None:
        st = self.routes.setdefault(alias, RouteState(alias))
        st.backends = list(backends)
        st.served_model = served_model
        st.revision_id = revision_id
        st.max_model_len = max_model_len
        st.draining = False
        st.down_reason = None
        st.since = time.time()
        st._down_until.clear()

    def begin_drain(self, alias: str, retry_after: int = 10) -> None:
        st = self.routes.setdefault(alias, RouteState(alias))
        st.draining = True
        st.retry_after = retry_after

    def cancel_drain(self, alias: str) -> None:
        if alias in self.routes:
            self.routes[alias].draining = False

    def mark_down(self, alias: str, reason: str, retry_after: int = 30) -> None:
        if alias in self.routes:
            self.routes[alias].down_reason = reason
            self.routes[alias].retry_after = retry_after

    def mark_up(self, alias: str) -> None:
        if alias in self.routes:
            self.routes[alias].down_reason = None

    async def wait_idle(self, alias: str, timeout: float) -> bool:
        """True if all in-flight requests on ``alias`` finished within ``timeout``."""
        st = self.routes.get(alias)
        if st is None or st.inflight == 0:
            return True
        try:
            await asyncio.wait_for(st._idle.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def clear_route(self, alias: str) -> None:
        st = self.routes.get(alias)
        if st:
            st.backends = []
            st.served_model = None
            st.revision_id = None
            st.max_model_len = None
            st.down_reason = None

    def aliases_for_revision_prefix(self, profile_name: str) -> list[str]:
        return [a for a, st in self.routes.items() if st.served_model == profile_name]

    # ---- request handling ----------------------------------------------------
    def check_key(self, authorization: Optional[str]) -> bool:
        if self.inference_api_key is None:
            return True
        scheme, _, token = (authorization or "").partition(" ")
        return scheme.lower() == "bearer" and secrets.compare_digest(
            token.strip(), self.inference_api_key
        )

    def resolve(self, model: Optional[str]) -> Optional[RouteState]:
        name = model or "default"
        if name in self.routes:
            return self.routes[name]
        # clients that use the served (profile) name directly
        return next((st for st in self.routes.values() if st.served_model == name), None)

    def backend_headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self.backend_api_key:
            h["authorization"] = f"Bearer {self.backend_api_key}"
        return h

    async def aclose(self) -> None:
        await self._client.aclose()
