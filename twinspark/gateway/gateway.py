"""Stable API Gateway (spec §15, §16, §43).

One stable OpenAI-compatible endpoint (``http://spark-a:8000/v1``). Clients use
aliases (``default``, ``coder`` ...) as the ``model`` name; the gateway rewrites
it to the served name of whatever revision currently backs the alias.

* During a switch the alias is *draining*: new requests get ``503`` +
  ``Retry-After``; in-flight requests (including open SSE streams) finish.
* Streaming is proxied chunk by chunk (no buffering, no re-encoding).
* Replicated topologies send a request to the backend with the fewest requests in flight (ties in turn,
  weighted per node), skip a backend that just refused connections, and keep a *session* on one backend
  (``session-id`` header or ``prompt_cache_key``) so its prefix cache stays warm (master plan §3.3).
* The controller's watchdog can mark a route *down* (model crashed) so clients
  get a clean 503 instead of a proxy error.
* A *fallback* per alias (another alias, or an external OpenAI-compatible endpoint) answers while the
  alias is switching or down; such responses carry ``X-TwinSpark-Fallback``.
* *Named keys* (master plan §4.1): one per app, each limited to aliases, expirable and revocable. Only
  SHA-256 hashes are kept here. Tokens per key and alias are counted per day (``take_usage``).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import itertools
import math
import secrets as _secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from ..security import tokens_equal

BACKEND_BACKOFF_S = 10.0
BALANCE = ("least-inflight", "round-robin")


def _rendezvous(key: str, backend: str, weight: float) -> float:
    """Weighted rendezvous hashing: the same key picks the same backend while it is up, and a backend
    leaving moves only its own sessions."""
    if weight <= 0:
        return 0.0
    h = int.from_bytes(hashlib.sha256(f"{key}\0{backend}".encode()).digest()[:8], "big")
    u = (h + 1) / (2 ** 64 + 1)                  # (0, 1)
    return -weight / math.log(u)


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
    # request token -> monotonic start: how long the oldest in-flight request has been running
    _active: dict = field(default_factory=dict, repr=False)
    _tok: itertools.count = field(default_factory=itertools.count, repr=False)
    _down_until: dict = field(default_factory=dict, repr=False)
    backend_nodes: dict = field(default_factory=dict)              # backend URL -> node (for weights)
    _per_backend: dict = field(default_factory=dict, repr=False)   # backend URL -> requests in flight
    _credit: dict = field(default_factory=dict, repr=False)        # smooth weighted round robin
    _remote: Optional[dict] = field(default=None, repr=False)      # a standalone gateway's report (state.py)

    def __post_init__(self) -> None:
        self._idle.set()

    @property
    def status(self) -> str:
        if self.draining:
            return "switching"
        if self.down_reason:
            return "down"
        return "serving" if self.backends else "idle"

    def candidates(self, affinity: Optional[str] = None, balance: str = "least-inflight",
                   weights: Optional[dict[str, float]] = None) -> list[str]:
        """Backends in the order to try them, recently failing ones last.

        ``affinity`` (a session key) keeps a session on one backend; otherwise ``least-inflight`` picks the
        backend with the fewest requests in flight, ties shared by weight (``weights`` per node).
        ``round-robin`` is the plain rotation of 0.6."""
        if not self.backends:
            return []
        start = next(self._rr) % len(self.backends)
        order = self.backends[start:] + self.backends[:start]
        now = time.monotonic()
        healthy = [b for b in order if self._down_until.get(b, 0) <= now]
        rest = [b for b in order if b not in healthy]
        if len(healthy) > 1:
            def w(b: str) -> float:
                return float((weights or {}).get(self.backend_nodes.get(b, ""), 1.0))
            if affinity:
                healthy.sort(key=lambda b: -_rendezvous(affinity, b, w(b)))
            elif balance == "least-inflight":
                load = {b: self._per_backend.get(b, 0) for b in healthy}
                low = min(load.values())
                tied = [b for b in healthy if load[b] == low and w(b) > 0] or [b for b in healthy if load[b] == low]
                total = sum(max(w(b), 0.0) for b in tied) or 1.0
                for b in tied:
                    self._credit[b] = self._credit.get(b, 0.0) + max(w(b), 0.0)
                best = max(tied, key=lambda b: self._credit[b])
                self._credit[best] -= total
                healthy = [best] + sorted((b for b in healthy if b != best), key=lambda b: load[b])
        return healthy + rest

    def backend_started(self, backend: str) -> None:
        self._per_backend[backend] = self._per_backend.get(backend, 0) + 1

    def backend_done(self, backend: str) -> None:
        n = self._per_backend.get(backend, 0) - 1
        if n > 0:
            self._per_backend[backend] = n
        else:
            self._per_backend.pop(backend, None)

    def next_backend(self) -> str:
        return self.candidates()[0]

    def backend_failed(self, backend: str) -> None:
        self._down_until[backend] = time.monotonic() + BACKEND_BACKOFF_S

    def acquire(self) -> int:
        tok = next(self._tok)
        self._active[tok] = time.monotonic()
        self.inflight += 1
        self.requests += 1
        self._idle.clear()
        return tok

    def release(self, tok: Optional[int] = None) -> None:
        if tok is not None:
            self._active.pop(tok, None)
        elif self._active:                       # a caller without its token: forget the oldest
            self._active.pop(next(iter(self._active)))
        self.inflight = max(0, self.inflight - 1)
        if self.inflight == 0:
            self._active.clear()
            self._idle.set()

    def oldest_inflight_s(self) -> Optional[float]:
        if not self._active:
            return (self._remote or {}).get("oldest")
        return round(time.monotonic() - min(self._active.values()), 1)

    def to_payload(self) -> dict:
        return {"alias": self.alias, "status": self.status, "model": self.served_model,
                "revision": self.revision_id, "inflight": self.inflight,
                "oldest_inflight_s": self.oldest_inflight_s(),
                "retry_after": self.retry_after, "requests": self.requests,
                "errors": self.errors, "last_error": self.last_error,
                "max_model_len": self.max_model_len, "down_reason": self.down_reason,
                "backends": len(self.backends), "since": self.since,
                "backend_inflight": (self._remote or {}).get("backend_inflight") or
                {self.backend_nodes.get(b) or b: n for b, n in self._per_backend.items()}}


class Gateway:
    """Alias routing + proxying. HTTP-framework agnostic apart from httpx."""

    def __init__(self, inference_api_key: Optional[str], backend_api_key: Optional[str] = None,
                 client: Optional[httpx.AsyncClient] = None):
        # None disables inference auth — only acceptable on localhost-only setups;
        # the CLI refuses to start a non-local gateway without a key.
        self.inference_api_key = inference_api_key
        self.backend_api_key = backend_api_key
        self.routes: dict[str, RouteState] = {}
        # routing settings (schemas.config.GatewaySettings as a plain dict: this module stays lean)
        self.settings: dict[str, Any] = {"affinity": True, "balance": "least-inflight", "weights": {},
                                         "fallbacks": {}}
        self.fallback_api_key: Optional[str] = None
        self.fallback_routes: dict[str, RouteState] = {}     # external fallbacks, one per alias
        # named keys by SHA-256 of the token; while there are none, inference_api_key alone is accepted
        self.keys: dict[str, dict[str, Any]] = {}
        # TwinSpark's own checks through the in-process gateway (compatibility, smoke): never leaves the process
        self.internal_token = _secrets.token_urlsafe(32)
        self.usage: dict[tuple[str, str, str], dict[str, int]] = {}   # (day, key id, alias) -> counters
        self.last_used: dict[str, float] = {}
        # standalone gateway (state.GatewayLink): every change is published; the process reports back
        self.on_change = None
        self.remote = False
        self.remote_ok = False
        self.published_version = 0
        self.remote_version = 0
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=1800, write=60, pool=10),
            limits=httpx.Limits(max_connections=512, max_keepalive_connections=64),
        )

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    # ---- controller-facing -------------------------------------------------
    def configure(self, settings: dict[str, Any], fallback_api_key: Optional[str] = None) -> None:
        """New routing settings; applied to the next request (requests in flight keep their backend)."""
        self.settings = {"affinity": bool(settings.get("affinity", True)),
                         "balance": settings.get("balance") if settings.get("balance") in BALANCE else "least-inflight",
                         "weights": dict(settings.get("weights") or {}),
                         "fallbacks": dict(settings.get("fallbacks") or {})}
        if fallback_api_key is not None:
            self.fallback_api_key = fallback_api_key or None
        self.fallback_routes = {a: st for a, st in self.fallback_routes.items()
                                if (self.settings["fallbacks"].get(a) or {}).get("url") and
                                st.backends == [_external_base(self.settings["fallbacks"][a]["url"])]}
        self._changed()

    def set_route(self, alias: str, backends: list[str], served_model: str,
                  revision_id: Optional[str] = None, max_model_len: Optional[int] = None,
                  nodes: Optional[dict[str, str]] = None) -> None:
        st = self.routes.setdefault(alias, RouteState(alias))
        if nodes is not None:
            st.backend_nodes = {b: nodes[b] for b in backends if b in nodes}
        st.backends = list(backends)
        st.served_model = served_model
        st.revision_id = revision_id
        st.max_model_len = max_model_len
        st.draining = False
        st.down_reason = None
        st.since = time.time()
        st._down_until.clear()
        self._changed()

    def begin_drain(self, alias: str, retry_after: int = 10) -> None:
        st = self.routes.setdefault(alias, RouteState(alias))
        st.draining = True
        st.retry_after = retry_after
        self._changed()

    def cancel_drain(self, alias: str) -> None:
        if alias in self.routes and self.routes[alias].draining:
            self.routes[alias].draining = False
            self._changed()

    def mark_down(self, alias: str, reason: str, retry_after: int = 30) -> None:
        if alias in self.routes:
            changed = self.routes[alias].down_reason != reason
            self.routes[alias].down_reason = reason
            self.routes[alias].retry_after = retry_after
            if changed:
                self._changed()

    def mark_up(self, alias: str) -> None:
        if alias in self.routes and self.routes[alias].down_reason is not None:
            self.routes[alias].down_reason = None
            self._changed()

    def remove_route(self, alias: str) -> None:
        if self.routes.pop(alias, None) is not None:
            self._changed()

    def _unacknowledged(self) -> bool:
        """A standalone gateway has not applied the last published state yet (it may still accept requests the
        new state would refuse). A gateway that does not answer has nothing in flight."""
        return self.remote and self.remote_ok and self.remote_version < self.published_version

    def inflight(self, aliases: list[str]) -> tuple[int, Optional[float]]:
        """(requests in flight, seconds the oldest of them has been running) over ``aliases``."""
        sts = [self.routes[a] for a in aliases if a in self.routes]
        oldest = [s.oldest_inflight_s() for s in sts]
        oldest = [o for o in oldest if o is not None]
        busy = sum(s.inflight for s in sts)
        if self._unacknowledged():
            busy = max(busy, 1)
        return busy, (max(oldest) if oldest else None)

    async def wait_idle(self, alias: str, timeout: float) -> bool:
        """True if all in-flight requests on ``alias`` finished within ``timeout``."""
        st = self.routes.get(alias)
        if self.remote:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.inflight([alias])[0] == 0:
                    return True
                await asyncio.sleep(0.25)
            return self.inflight([alias])[0] == 0
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
            self._changed()

    def aliases_for_revision_prefix(self, profile_name: str) -> list[str]:
        return [a for a, st in self.routes.items() if st.served_model == profile_name]

    # ---- keys and usage (master plan §4.1) ------------------------------------
    def set_keys(self, records: list[dict[str, Any]]) -> None:
        """The named keys (``{id, sha256, aliases, expires_at, revoked_at, …}``; never a secret)."""
        self.keys = {r["sha256"]: dict(r) for r in records if r.get("sha256")}
        self._changed()

    def authenticate(self, authorization: Optional[str]) -> Optional[dict[str, Any]]:
        """The key a request uses, or None (401). Without any key configured the gateway is open (only allowed
        on a loopback listener; serve.py refuses otherwise)."""
        if self.inference_api_key is None and not self.keys:
            return {"id": "open", "aliases": []}
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        if tokens_equal(token, self.internal_token):
            return {"id": "internal", "aliases": []}
        rec = self.keys.get(hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest())
        if rec is not None:
            now = time.time()
            if rec.get("revoked_at") or (rec.get("expires_at") and rec["expires_at"] <= now):
                return None
            return rec
        if not self.keys and self.inference_api_key and tokens_equal(token, self.inference_api_key):
            return {"id": "default", "aliases": []}
        return None

    def check_key(self, authorization: Optional[str]) -> bool:
        return self.authenticate(authorization) is not None

    @staticmethod
    def allows(key: dict[str, Any], alias: str) -> bool:
        return not key.get("aliases") or alias in key["aliases"]

    def record_usage(self, key_id: str, alias: str, prompt: int, completion: int, estimated: bool) -> None:
        if key_id in ("internal",):
            return
        day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        row = self.usage.setdefault((day, key_id, alias), {"requests": 0, "prompt_tokens": 0,
                                                           "completion_tokens": 0, "estimated_requests": 0})
        row["requests"] += 1
        row["prompt_tokens"] += max(0, int(prompt))
        row["completion_tokens"] += max(0, int(completion))
        row["estimated_requests"] += int(bool(estimated))
        self.last_used[key_id] = time.time()

    def take_usage(self) -> dict[str, Any]:
        """Counters since the last call (the controller adds them to its daily totals), and when each key was
        last used."""
        rows = [{"day": d, "key_id": k, "alias": a, **v} for (d, k, a), v in self.usage.items()]
        out = {"rows": rows, "last_used": dict(self.last_used)}
        self.usage, self.last_used = {}, {}
        return out

    # ---- request handling ----------------------------------------------------

    def resolve(self, model: Optional[str]) -> Optional[RouteState]:
        if model is not None and not isinstance(model, str):
            return None                      # a list/dict "model" from a client is simply unknown
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

    def order(self, st: RouteState, affinity: Optional[str]) -> list[str]:
        """The backends of ``st`` to try, by the routing settings."""
        return st.candidates(affinity=affinity if self.settings.get("affinity", True) else None,
                             balance=self.settings.get("balance", "least-inflight"),
                             weights=self.settings.get("weights") or {})

    def fallback_for(self, alias: str) -> Optional[tuple[RouteState, str, dict[str, str], str]]:
        """Where a request for ``alias`` goes while it is switching or down: ``(route, model name, headers,
        label)``, or None (the client gets 503). Another alias is used only while it is serving itself."""
        cfg = (self.settings.get("fallbacks") or {}).get(alias) or {}
        if cfg.get("alias"):
            t = self.routes.get(cfg["alias"])
            if t is None or t.alias == alias or not t.backends or t.draining or t.down_reason:
                return None
            return t, t.served_model or t.alias, self.backend_headers(), f"alias:{t.alias}"
        if cfg.get("url") and cfg.get("model"):
            base = _external_base(cfg["url"])
            t = self.fallback_routes.get(alias)
            if t is None or t.backends != [base]:
                t = self.fallback_routes[alias] = RouteState(f"fallback:{alias}", backends=[base],
                                                             served_model=cfg["model"])
            h = {"content-type": "application/json"}
            if self.fallback_api_key:
                h["authorization"] = f"Bearer {self.fallback_api_key}"
            return t, cfg["model"], h, "external"
        return None

    async def aclose(self) -> None:
        await self._client.aclose()


def _external_base(url: str) -> str:
    """``https://host/api/v1`` -> ``https://host/api``: requests go to ``<base>/v1/<endpoint>``."""
    u = url.rstrip("/")
    return u[:-3] if u.endswith("/v1") else u
