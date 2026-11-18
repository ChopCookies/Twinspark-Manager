"""Kits: model set-ups that run with their author's own scripts next to TwinSpark (twinspark.kits).

``tsm kit start`` calls handover, runs the kit's start.sh as the operator, then calls attach; ``tsm kit stop``
runs the kit's stop.sh, then calls detach. The GUI's Stop calls detach with ``stop: true``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import kits
from ..controller import kits as kit_ctl
from ..controller.controller import BusyError, Controller
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1/kits", tags=["kits"], dependencies=[Depends(require_auth)])


class Handover(BaseModel):
    confirm: str                                   # the kit's name: TwinSpark's model stops
    aliases: Optional[list[str]] = Field(default=None, max_length=16)
    port: Optional[int] = Field(default=None, ge=1024, le=65535)


class Detach(BaseModel):
    stop: bool = False                             # also stop the kit's containers on both Sparks
    kit: Optional[str] = None


@router.get("")
def list_kits(ctrl: Controller = Depends(controller_dep)):
    try:
        catalog = [k.as_dict() for k in kits.load_all().values()]
    except kits.KitError as exc:
        raise HTTPException(500, f"the kit catalog is broken: {exc}") from exc
    return {"kits": catalog, "external": kit_ctl.state(ctrl)}


@router.post("/{name}/handover")
async def handover(name: str, req: Handover, ctrl: Controller = Depends(controller_dep)):
    if req.confirm != name:
        raise HTTPException(422, f"confirm must be the kit's name ({name}): TwinSpark's model stops")
    try:
        return await kit_ctl.handover(ctrl, name, req.aliases, req.port)
    except (BusyError, kit_ctl.KitStateError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except kit_ctl.HandoverError as exc:
        raise HTTPException(502, str(exc)) from exc
    except kits.KitError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{name}/attach")
async def attach(name: str, ctrl: Controller = Depends(controller_dep)):
    try:
        return await kit_ctl.attach(ctrl, name)
    except kit_ctl.KitStateError as exc:
        raise HTTPException(409, str(exc)) from exc
    except kits.KitError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/detach")
async def detach(req: Detach, ctrl: Controller = Depends(controller_dep)):
    if ctrl.busy():
        raise HTTPException(409, "an activation is in progress")
    try:
        return await kit_ctl.detach(ctrl, req.stop, name=req.kit)
    except kit_ctl.KitStateError as exc:
        raise HTTPException(409, str(exc)) from exc
