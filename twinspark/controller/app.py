"""FastAPI application assembly for the Controller (spec §4.1, §43)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..api import (
    routes_activation,
    routes_cookbook,
    routes_diagnostics,
    routes_files,
    routes_integrations,
    routes_profiles,
    routes_system,
)
from .controller import Controller

log = logging.getLogger("twinspark.app")
WEB_ASSETS = Path(__file__).resolve().parent.parent / "web" / "dist"


def create_app(controller: Controller, management_key: str, run_startup: bool = True,
               background: Optional[bool] = None) -> FastAPI:
    """``background`` (watchdog + metrics loops) defaults to ``run_startup``."""
    background = run_startup if background is None else background

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_startup:
            try:
                await controller.on_startup()
            except Exception:  # noqa: BLE001 - a broken agent must not keep the UI down
                log.exception("controller startup failed")
        if background:
            controller.start_background()
        yield
        await controller.aclose()

    app = FastAPI(title="TwinSpark Manager", version=__version__, docs_url="/mgmt/docs",
                  lifespan=lifespan)
    app.state.controller = controller
    app.state.management_key = management_key
    app.state.rate_buckets = {}

    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": f"internal error ({type(exc).__name__})"})

    for r in (routes_profiles.router, routes_activation.router, routes_system.router,
              routes_cookbook.router, routes_diagnostics.router, routes_files.router,
              routes_integrations.router):
        app.include_router(r)

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "version": __version__, "nodes": sorted(controller.agents),
                "busy": controller.busy()}

    if WEB_ASSETS.exists():
        app.mount("/", StaticFiles(directory=str(WEB_ASSETS), html=True), name="web")
    return app
