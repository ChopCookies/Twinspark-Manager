"""Shared FastAPI dependencies: auth, CSRF, rate limiting, controller access."""

from __future__ import annotations

import secrets
import time
from typing import Optional

from fastapi import Header, HTTPException, Request

from ..controller.controller import Controller

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def controller_dep(request: Request) -> Controller:
    return request.app.state.controller


def require_auth(request: Request, x_api_key: Optional[str] = Header(default=None),
                 authorization: Optional[str] = Header(default=None)) -> None:
    """Management-plane auth + CSRF + rate limit, applied to every /api router."""
    ctrl: Controller = request.app.state.controller
    cfg = ctrl.config

    # rate limit: fixed 60 s window per client
    now = int(time.time() // 60)
    key = request.client.host if request.client else "?"
    bucket = request.app.state.rate_buckets.setdefault(key, [now, 0])
    if bucket[0] != now:
        bucket[:] = [now, 0]
    bucket[1] += 1
    if bucket[1] > cfg.rate_limit_per_min:
        raise HTTPException(429, "rate limit exceeded")

    if cfg.csrf_protection and request.method in _MUTATING:
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            if origin.split("://", 1)[-1].rstrip("/") != host:
                raise HTTPException(403, "cross-origin request rejected")

    if (request.method in _MUTATING and ctrl.maintenance.blocking()
            and not request.url.path.startswith("/api/v1/system/maintenance/")):
        raise HTTPException(409, "cluster is reserved for maintenance; open Updates to review progress")

    if cfg.management_auth == "none":
        return
    expected: str = request.app.state.management_key
    if not expected:
        raise HTTPException(503, "management key not configured — run `tsm init`")
    provided = x_api_key
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not secrets.compare_digest(provided or "", expected):
        raise HTTPException(401, "invalid management API key")
