"""``GET /metrics``: the cluster in Prometheus text format (controller.exporter). Loaded lazily.

A scraper authenticates with the read-only metrics key (``Authorization: Bearer …``; `tsm prometheus key`
creates it), which opens nothing but this page, or with the management key. With ``management_auth: none``
only loopback names are accepted, as for the rest of the API.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from ..security import SecretsVault, tokens_equal
from .deps import check_key

router = APIRouter(tags=["metrics"])
log = logging.getLogger("twinspark.metrics")


def metrics_key(ctrl) -> str:
    try:
        return SecretsVault(ctrl.config.secrets_dir).get("metrics_api_key")
    except Exception:  # noqa: BLE001 - an unreadable vault means no metrics key, not a 500
        log.debug("metrics key not readable", exc_info=True)
        return ""


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> PlainTextResponse:
    ctrl = request.app.state.controller
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not (bearer and tokens_equal(bearer, metrics_key(ctrl))):
        check_key(request)                 # the management key (or loopback without auth), else 401
    from ..controller.exporter import render
    return PlainTextResponse(render(ctrl), media_type="text/plain; version=0.0.4; charset=utf-8",
                             headers={"Cache-Control": "no-store"})
