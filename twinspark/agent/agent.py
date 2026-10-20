"""tsm-agent HTTP service (spec §4.2).

One endpoint, ``POST /v1/action``, dispatching only to the typed actions in
``AgentActions``. Authenticated with a shared bearer token (``agent_token`` in
the vault, identical on both nodes; set up by ``tsm init``). mTLS can be layered
on top via the listener's TLS settings.

Plain Starlette, not FastAPI: the agent has two endpoints and runs on every Spark next to the model,
so it does not carry FastAPI's routing and schema machinery (about 10 MiB per process, measured with
``scripts/measure_memory.py``). The wire format is unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import __version__
from ..schemas.config import AgentConfig
from ..security import tokens_equal
from .actions import ActionError, AgentActions

# Largest accepted request: a 64 MiB mod archive travels base64-encoded inside the JSON.
MAX_BODY_BYTES = 100 * 1024 ** 2


class _Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _parse_action(raw: bytes) -> tuple[str, dict[str, Any]]:
    """``{"action": str, "params": {...}}`` — the same contract the FastAPI model enforced."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("invalid action request: body: not valid JSON") from None
    if not isinstance(body, dict):
        raise ValueError("invalid action request: body: must be an object")
    action, params = body.get("action"), body.get("params", {})
    if not isinstance(action, str):
        raise ValueError("invalid action request: action: must be a string")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("invalid action request: params: must be an object")
    return action, params


def build_agent_app(config: AgentConfig, actions: Optional[AgentActions] = None,
                    token: Optional[str] = None) -> Starlette:
    actions = actions or AgentActions(config)
    expected = token if token is not None else actions.vault.get("agent_token")
    if not expected:
        raise RuntimeError("agent_token is not set — run `tsm init --role agent` first")

    def _auth(request: Request) -> None:
        scheme, _, tok = (request.headers.get("authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not tokens_equal(tok.strip(), expected):
            raise _Refused(401, "invalid agent token")

    async def _read_body(request: Request) -> bytes:
        """Read the request body, but only after authentication and never more than the cap."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise _Refused(413, "request body too large")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise _Refused(413, "request body too large")
            chunks.append(chunk)
        return b"".join(chunks)

    async def do_action(request: Request) -> Response:
        try:
            _auth(request)               # before the body is read: unauthenticated callers cost nothing
            raw = await _read_body(request)
        except _Refused as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status)
        try:
            action, params = _parse_action(raw)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        try:
            result = await actions.dispatch(action, params)
        except ActionError as exc:
            # same shape the controller's AgentClient expects
            return JSONResponse({"ok": False, "error": str(exc), "log_excerpt": exc.excerpt},
                                status_code=exc.status)
        except ValueError as exc:                      # pydantic validation etc.
            return JSONResponse({"ok": False, "error": str(exc)[:2000]}, status_code=422)
        return JSONResponse({"ok": True, "result": result})

    async def list_actions(request: Request) -> Response:
        try:
            _auth(request)
        except _Refused as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status)
        return JSONResponse({"actions": sorted(actions.registry), "node": config.node.node_id,
                             "runtime_mode": config.runtime_mode, "version": __version__})

    # no /openapi.json, /docs or /redoc: an agent has exactly two authenticated endpoints
    app = Starlette(routes=[Route("/v1/action", do_action, methods=["POST"]),
                            Route("/v1/actions", list_actions, methods=["GET"])])
    app.state.actions = actions
    return app
