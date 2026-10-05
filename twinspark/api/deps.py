"""Shared FastAPI dependencies: auth, CSRF, rate limiting, controller access."""

from __future__ import annotations

import ipaddress
import time
from typing import Optional

from fastapi import Header, HTTPException, Request

from ..controller.controller import Controller
from ..security import tokens_equal

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_MAINTENANCE_OK = ("/api/v1/system/maintenance/", "/api/v1/remote/")
_FAILED_PER_MIN = 120          # wrong-key requests tolerated per client per minute


def controller_dep(request: Request) -> Controller:
    return request.app.state.controller


def _bucket(request: Request, name: str) -> list[int]:
    """Fixed 60 s window counter per (name, client address)."""
    now = int(time.time() // 60)
    host = request.client.host if request.client else "?"
    bucket = request.app.state.rate_buckets.setdefault(f"{name}:{host}", [now, 0])
    if bucket[0] != now:
        bucket[:] = [now, 0]
    return bucket


def _too_many() -> HTTPException:
    wait = 60 - int(time.time() % 60)
    return HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(max(1, wait))})


def _loopback_host(host_header: str) -> bool:
    """True when the Host header names this machine's loopback (``localhost``, ``127.x``, ``[::1]``)."""
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_key(request: Request) -> None:
    """CSRF + management-key check, from headers only (safe to run before the body is read)."""
    ctrl: Controller = request.app.state.controller
    cfg = ctrl.config

    if cfg.csrf_protection and request.method in _MUTATING:
        origin = request.headers.get("origin")
        if origin is not None:             # an empty or "null" Origin is a mismatch, not "absent"
            host = request.headers.get("host", "")
            if origin.split("://", 1)[-1].rstrip("/") != host:
                raise HTTPException(403, "cross-origin request rejected")
        elif request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(403, "cross-site request rejected")

    if cfg.management_auth == "none":
        # Without a key, a web page could reach the API through DNS rebinding (its own name resolving to
        # 127.0.0.1); such a request carries that name in Host, so only loopback names are accepted.
        if not _loopback_host(request.headers.get("host", "")):
            raise HTTPException(403, "management_auth is 'none': only http://localhost / 127.0.0.1 is accepted")
    else:
        expected: str = request.app.state.management_key
        if not expected:
            raise HTTPException(503, "management key not configured — run `tsm init`")
        provided = request.headers.get("x-api-key")
        authorization = request.headers.get("authorization")
        if not provided and authorization and authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        if not tokens_equal(provided, expected):
            failed = _bucket(request, "fail")
            failed[1] += 1
            if failed[1] > _FAILED_PER_MIN:
                raise _too_many()
            raise HTTPException(401, "invalid management API key")


def require_auth(request: Request, x_api_key: Optional[str] = Header(default=None),
                 authorization: Optional[str] = Header(default=None)) -> None:
    """Management-plane auth + CSRF + rate limit, applied to every /api router.

    Order matters: nothing about the cluster (maintenance state, validation of the body) is
    revealed before the key has been checked, and callers with a wrong key cannot use up the
    request budget of the real operator (behind an SSH tunnel every client is 127.0.0.1).
    """
    ctrl: Controller = request.app.state.controller
    cfg = ctrl.config
    check_key(request)

    ok = _bucket(request, "ok")
    ok[1] += 1
    if ok[1] > cfg.rate_limit_per_min:
        raise _too_many()

    # Remote management stays reachable while a maintenance run is in progress or has failed: that is
    # when a terminal or a power cycle is needed most. Reboot/poweroff/plug-off still refuse a busy
    # cluster unless the operator repeats them with force (see controller/remote.py).
    if (request.method in _MUTATING and ctrl.maintenance.blocking()
            and not request.url.path.startswith(_MAINTENANCE_OK)):
        raise HTTPException(409, "cluster is reserved for maintenance; open Updates to review progress")
