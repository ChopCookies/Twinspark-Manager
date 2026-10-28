"""``tsm serve gateway``: the stable gateway in its own process (master plan §3.2, twinspark-gateway.service).

It serves the OpenAI-compatible endpoint from the state file the controller writes (gateway.state) and
answers the controller on a Unix socket. It does not import the controller, FastAPI or pydantic: a
controller restart, update or crash leaves it and the streams it carries running.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .app import build_gateway_app
from .gateway import Gateway
from .state import StateFollower

log = logging.getLogger("twinspark.gateway")
_LOCAL = ("127.0.0.1", "::1", "localhost")
DEFAULTS = {"state_path": "/var/lib/twinspark/gateway-state.json",
            "control_socket": "/run/twinspark-gateway/control.sock"}


def read_config(path: str) -> dict[str, Any]:
    """The few settings of controller.yaml this process needs (no pydantic here: it stays small)."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = doc.get("gateway_listener")
    gl: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return {"bind": str(gl.get("bind") or "0.0.0.0"), "port": int(gl.get("port") or 8000),
            "tls_cert": gl.get("tls_cert"), "tls_key": gl.get("tls_key"),
            "secrets_dir": str(doc.get("secrets_dir") or "/etc/twinspark/secrets"),
            "state_path": str(doc.get("gateway_state_path") or DEFAULTS["state_path"]),
            "control_socket": str(doc.get("gateway_control_socket") or DEFAULTS["control_socket"])}


def control_app(gw: Gateway, follower: StateFollower) -> Starlette:
    started = time.time()

    async def stats(request: Request):
        return JSONResponse({
            "version": follower.version, "pid": os.getpid(), "started_at": started,
            "routes": {a: {"inflight": st.inflight, "oldest_inflight_s": st.oldest_inflight_s(),
                           "requests": st.requests, "errors": st.errors, "last_error": st.last_error,
                           "backend_inflight": st.to_payload()["backend_inflight"]}
                       for a, st in gw.routes.items()},
            "usage": gw.take_usage()})

    return Starlette(routes=[Route("/control/stats", stats, methods=["GET"])])


def build(config_path: str, *, interval: float = 0.5) -> tuple[dict, Gateway, StateFollower]:
    from ..security import SecretsVault
    cfg = read_config(config_path)
    vault = SecretsVault(cfg["secrets_dir"])
    gw = Gateway(vault.get("inference_api_key") or None, vault.get("backend_api_key") or None)

    def before_apply(doc: dict) -> None:
        # the external fallback's key can change at runtime (tsm gateway fallback-key)
        gw.fallback_api_key = vault.get("fallback_api_key") or None

    follower = StateFollower(gw, Path(cfg["state_path"]), interval=interval, on_apply=before_apply)
    follower.check()
    if cfg["bind"] not in _LOCAL and not gw.requires_key():
        raise SystemExit("the gateway listens on a non-local address but has no inference key — run `tsm init` "
                         "and start the controller once")
    return cfg, gw, follower


async def run(config_path: str) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg, gw, follower = build(config_path)
    sock = Path(cfg["control_socket"])
    sock.parent.mkdir(parents=True, exist_ok=True)
    if sock.exists():
        sock.unlink()                       # a stale socket of an earlier run
    common: dict[str, Any] = dict(log_level="info", proxy_headers=False, access_log=False)
    public = uvicorn.Server(uvicorn.Config(build_gateway_app(gw), host=cfg["bind"], port=cfg["port"],
                                           ssl_certfile=cfg["tls_cert"], ssl_keyfile=cfg["tls_key"], **common))
    control = uvicorn.Server(uvicorn.Config(control_app(gw, follower), uds=str(sock), **common))
    follow = asyncio.create_task(follower.run())

    async def restrict() -> None:
        while not control.started:
            await asyncio.sleep(0.05)
        os.chmod(sock, 0o660)               # the service user and its group (the controller) only

    try:
        await asyncio.gather(public.serve(), control.serve(), restrict())
    finally:
        follow.cancel()
        await gw.aclose()
