"""Model files and mods on both Sparks: inventory, staging, deletion, mod install."""

from __future__ import annotations

import base64
import binascii
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..controller.controller import Controller
from ..controller.files import DependencyError, FilesError
from ..schemas.job import Job
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1", tags=["files"], dependencies=[Depends(require_auth)])

_MAX_MOD_B64 = 90 * 1024 * 1024          # ~64 MiB archive after base64


def _raise_if_all_failed(result: dict) -> dict:
    """A request that failed on every node is an error, not a 200 with errors inside."""
    per_node = result.get("results") if isinstance(result, dict) else None
    if per_node and all(isinstance(r, dict) and r.get("error") for r in per_node.values()):
        first = next(iter(per_node.values()))["error"]
        raise HTTPException(422, f"failed on every node: {first}")
    return result


def _nodes(ctrl: Controller, nodes: Optional[list[str]]) -> list[str]:
    out = nodes or sorted(ctrl.agents)
    bad = [n for n in out if n not in ctrl.agents]
    if bad:
        raise HTTPException(422, f"unknown node(s): {bad}")
    return out


@router.get("/models/files")
async def model_files(ctrl: Controller = Depends(controller_dep)):
    """Every model snapshot on both nodes, which profiles use it, what is missing."""
    return await ctrl.model_files()


class StageRequest(BaseModel):
    ref: str = Field(..., description="org/model, org/model@branch or org/model@<40-char sha>")
    nodes: Optional[list[str]] = None
    include: Optional[list[str]] = None
    download_node: Optional[str] = None
    verify: Optional[bool] = None


@router.post("/models/stage", response_model=Job, status_code=202)
async def stage(req: StageRequest, ctrl: Controller = Depends(controller_dep)):
    """Download once (+ verify), then copy to the other node over QSFP — in the background."""
    nodes = _nodes(ctrl, req.nodes)
    if req.download_node and req.download_node not in ctrl.agents:
        raise HTTPException(422, "download_node is not configured")
    try:
        return await ctrl.stage(req.ref, nodes=nodes, include=req.include,
                                download_node=req.download_node, verify=req.verify)
    except FilesError as e:
        raise HTTPException(422, str(e))


class DeleteRequest(BaseModel):
    repo: str
    revision: Optional[str] = None      # None = every snapshot of the repo
    nodes: Optional[list[str]] = None
    force: bool = False                 # delete even though profiles reference it
    preview: bool = False               # report what would be freed, delete nothing


@router.post("/models/files/delete")
async def delete_files(req: DeleteRequest, ctrl: Controller = Depends(controller_dep)):
    nodes = _nodes(ctrl, req.nodes)
    try:
        return _raise_if_all_failed(await ctrl.delete_model_files(
            req.repo, req.revision, nodes, force=req.force, preview=req.preview))
    except DependencyError as e:
        raise HTTPException(409, {"message": str(e), "dependents": e.dependents})
    except FilesError as e:
        raise HTTPException(409, str(e))


# ---- mods -----------------------------------------------------------------------------
@router.get("/mods")
async def mods(ctrl: Controller = Depends(controller_dep)):
    return await ctrl.mods()


class ModInstall(BaseModel):
    name: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    archive_b64: str = Field(..., description="zip or tar(.gz) of the mod directory (run.sh at its root)")
    nodes: Optional[list[str]] = None

    @field_validator("archive_b64")
    @classmethod
    def _b64(cls, v: str) -> str:
        if len(v) > _MAX_MOD_B64:
            raise ValueError("mod archive too large (max 64 MiB)")
        try:
            base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("archive_b64 is not valid base64")
        return v


@router.post("/mods")
async def install_mod(req: ModInstall, ctrl: Controller = Depends(controller_dep)):
    """Install (or replace) a mod on every node — identical content everywhere."""
    try:
        return _raise_if_all_failed(
            await ctrl.install_mod(req.name, req.archive_b64, _nodes(ctrl, req.nodes)))
    except FilesError as e:
        raise HTTPException(409, str(e))


@router.delete("/mods/{name}")
async def remove_mod(name: str, ctrl: Controller = Depends(controller_dep)):
    try:
        return _raise_if_all_failed(await ctrl.remove_mod(name))
    except FilesError as e:
        raise HTTPException(409, str(e))
