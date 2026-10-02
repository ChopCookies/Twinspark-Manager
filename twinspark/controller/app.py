"""FastAPI application assembly for the Controller (spec §4.1, §43)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..api import (
    routes_activation,
    routes_cookbook,
    routes_diagnostics,
    routes_files,
    routes_profiles,
    routes_remote,
    routes_system,
)
from ..api.deps import check_key
from .controller import Controller
from .remote import RemoteError

log = logging.getLogger("twinspark.app")
WEB_ASSETS = Path(__file__).resolve().parent.parent / "web" / "dist"

_BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_JSON_LIMIT = 4 * 1024 ** 2                       # ordinary API bodies are a few KiB
_UPLOAD_LIMIT = 100 * 1024 ** 2                   # mod archives travel base64-encoded
_UPLOAD_PATHS = ("/api/v1/mods",)


def _clean(value: object, limit: int) -> str:
    """Printable, JSON/UTF-8 safe, bounded text for error payloads."""
    text = str(value)[:limit]
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


def create_app(controller: Controller, management_key: str, run_startup: bool = True,
               background: Optional[bool] = None) -> FastAPI:
    """``background`` (watchdog + metrics loops) defaults to ``run_startup``."""
    background = run_startup if background is None else background

    async def _startup() -> None:
        try:
            await controller.on_startup()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken agent must not keep the UI down
            log.exception("controller startup failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Recovery after a boot can wait for the other node (startup_wait_s). It runs in the
        # background so the GUI and the CLI answer immediately and show what is going on.
        task = asyncio.create_task(_startup()) if run_startup else None
        if background:
            controller.start_background()
        yield
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await controller.aclose()

    # The interactive docs and the schema are off: they were served without a key and the GUI/CLI
    # do not use them. The API surface is documented in docs/ and in `tsm --help`.
    app = FastAPI(title="TwinSpark Manager", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.controller = controller
    app.state.management_key = management_key
    app.state.rate_buckets = {}

    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.middleware("http")
    async def guard_and_headers(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/v1/health" and request.method in _BODY_METHODS:
            # Authenticate and size-check from the headers *before* FastAPI reads the body, so an
            # unauthenticated caller cannot make the controller buffer or parse a large payload.
            try:
                check_key(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                    headers=exc.headers)
            declared = request.headers.get("content-length", "")
            limit = _UPLOAD_LIMIT if path.startswith(_UPLOAD_PATHS) else _JSON_LIMIT
            if declared.isdigit() and int(declared) > limit:
                return JSONResponse({"detail": f"request body larger than {limit // 1024} KiB"},
                                    status_code=413)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # FastAPI's default handler echoes the submitted value back, which breaks on NaN /
        # lone surrogates / huge strings (500 instead of 422) and can reflect secrets.
        errors = [{"loc": [_clean(x, 60) for x in e.get("loc", ())], "msg": _clean(e.get("msg", ""), 300),
                   "type": _clean(e.get("type", ""), 60)} for e in exc.errors()[:20]]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(RemoteError)
    async def remote_problem(request: Request, exc: RemoteError):
        return JSONResponse(status_code=exc.status, content={"detail": _clean(exc, 600)})

    @app.exception_handler(RecursionError)
    async def too_deep(request: Request, exc: RecursionError):
        return JSONResponse(status_code=400, content={"detail": "request body is nested too deeply"})

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": f"internal error ({type(exc).__name__})"})

    for r in (routes_profiles.router, routes_activation.router, routes_system.router,
              routes_cookbook.router, routes_diagnostics.router, routes_files.router,
              routes_remote.router, routes_remote.ws_router):
        app.include_router(r)

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "version": __version__, "nodes": sorted(controller.agents),
                "busy": controller.busy()}

    if WEB_ASSETS.exists():
        app.mount("/", StaticFiles(directory=str(WEB_ASSETS), html=True), name="web")
    return app
