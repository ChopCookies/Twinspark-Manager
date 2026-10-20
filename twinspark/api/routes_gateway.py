"""Gateway routing settings (master plan §3.3): balance, per-node weights, session affinity, fallbacks."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..controller.controller import Controller
from ..schemas.config import GatewaySettings
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1/gateway", tags=["gateway"], dependencies=[Depends(require_auth)])


def _view(ctrl: Controller) -> dict:
    return {"settings": ctrl.gateway_settings().model_dump(mode="json"),
            "source": "runtime" if ctrl.store.kv_get("gateway_settings") else "controller.yaml",
            "fallback_key_set": bool(ctrl.gateway.fallback_api_key),
            "aliases": sorted(ctrl.gateway.routes)}


@router.get("")
def settings(ctrl: Controller = Depends(controller_dep)):
    return _view(ctrl)


@router.put("")
def update(req: GatewaySettings, ctrl: Controller = Depends(controller_dep)):
    ctrl.set_gateway_settings(req)
    return _view(ctrl)


@router.delete("")
def reset(ctrl: Controller = Depends(controller_dep)):
    """Back to the settings in controller.yaml."""
    ctrl.store.kv_set("gateway_settings", None)
    ctrl._apply_gateway_settings()
    ctrl._audit("user", "gateway.settings.reset", "gateway", {})
    return _view(ctrl)


class FallbackKey(BaseModel):
    key: str = Field("", max_length=4096, pattern=r"^[\x21-\x7e]*$")    # empty removes it


@router.put("/fallback-key")
def fallback_key(req: FallbackKey, ctrl: Controller = Depends(controller_dep)):
    ctrl.set_fallback_key(req.key)
    return {"fallback_key_set": bool(ctrl.gateway.fallback_api_key)}
