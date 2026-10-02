"""tsm-agent HTTP service (spec §4.2).

One endpoint, ``POST /v1/action``, dispatching only to the typed actions in
``AgentActions``. Authenticated with a shared bearer token (``agent_token`` in
the vault, identical on both nodes; set up by ``tsm init``). mTLS can be layered
on top via the listener's TLS settings.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from .. import __version__
from ..schemas.config import AgentConfig
from ..security import tokens_equal
from .actions import ActionError, AgentActions

# Largest accepted request: a 64 MiB mod archive travels base64-encoded inside the JSON.
MAX_BODY_BYTES = 100 * 1024 ** 2


class ActionRequest(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


def build_agent_app(config: AgentConfig, actions: Optional[AgentActions] = None,
                    token: Optional[str] = None) -> FastAPI:
    actions = actions or AgentActions(config)
    expected = token if token is not None else actions.vault.get("agent_token")
    if not expected:
        raise RuntimeError("agent_token is not set — run `tsm init --role agent` first")

    # no /openapi.json, /docs or /redoc: an agent has exactly two authenticated endpoints
    app = FastAPI(title="tsm-agent", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.actions = actions

    def _auth(authorization: Optional[str]) -> None:
        scheme, _, tok = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not tokens_equal(tok.strip(), expected):
            raise HTTPException(401, "invalid agent token")

    async def _read_body(request: Request) -> bytes:
        """Read the request body, but only after authentication and never more than the cap."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise HTTPException(413, "request body too large")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise HTTPException(413, "request body too large")
            chunks.append(chunk)
        return b"".join(chunks)

    @app.post("/v1/action")
    async def do_action(request: Request, authorization: Optional[str] = Header(default=None)):
        _auth(authorization)                 # before the body is read: unauthenticated callers cost nothing
        try:
            req = ActionRequest.model_validate_json(await _read_body(request))
        except ValidationError as exc:
            return JSONResponse({"ok": False, "error": "invalid action request: "
                                 + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                             for e in exc.errors()[:5])}, status_code=422)
        try:
            result = await actions.dispatch(req.action, req.params)
        except ActionError as exc:
            # same shape the controller's AgentClient expects
            return JSONResponse({"ok": False, "error": str(exc), "log_excerpt": exc.excerpt},
                                status_code=exc.status)
        except ValueError as exc:                      # pydantic validation etc.
            return JSONResponse({"ok": False, "error": str(exc)[:2000]}, status_code=422)
        return {"ok": True, "result": result}

    @app.get("/v1/actions")
    def list_actions(authorization: Optional[str] = Header(default=None)):
        _auth(authorization)
        return {"actions": sorted(actions.registry), "node": config.node.node_id,
                "runtime_mode": config.runtime_mode, "version": __version__}

    return app
