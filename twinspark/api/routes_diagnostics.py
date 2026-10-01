"""Diagnostics routes: doctor, RDMA discovery, link tests, metrics, headless, foreign vLLM.

None of these touch the running model except the explicit, confirmed
``foreign/stop`` (for a vLLM container started outside TwinSpark).
"""

from __future__ import annotations

import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..controller import onboarding
from ..controller.agent_client import AgentActionError
from ..controller.controller import Controller
from ..metrics import combine_snapshots, scrape_metrics
from ..resolver import resolve_hf_revision, resolve_image_digest, spec_from_resolved
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1/system", tags=["diagnostics"],
                   dependencies=[Depends(require_auth)])


class ResolveRequest(BaseModel):
    ref: str                       # org/model@branch
    image: Optional[str] = None    # registry/org/repo[:tag] -> pin to digest


@router.post("/resolve")
async def resolve(req: ResolveRequest, ctrl: Controller = Depends(controller_dep)):
    """Resolve a model ref to its commit sha (+ size/arch) and an image to its digest."""
    try:
        resolved = await asyncio.to_thread(resolve_hf_revision, req.ref, ctrl.hf_token)
        out = {"repo": resolved["repo"], "branch": resolved["branch"],
               "revision": resolved["revision"], "weight_bytes": resolved["weight_bytes"]}
        try:
            spec = spec_from_resolved(resolved)
            out["spec"] = spec.__dict__
            if not ctrl.model_spec(resolved["repo"]):
                ctrl.set_model_spec(resolved["repo"], spec)   # cache for later planning
        except ValueError as e:
            out["spec_error"] = str(e)
        if req.image:
            out["image_ref"] = await asyncio.to_thread(resolve_image_digest, req.image)
        return {"ok": True, **out}
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/doctor")
async def doctor(ctrl: Controller = Depends(controller_dep)):
    """Every precondition for a fast, stable dual-Spark deployment, with the fix."""
    return await ctrl.doctor()


@router.get("/onboarding")
async def get_started(ctrl: Controller = Depends(controller_dep)):
    """The first-run checklist: what is done, what is next, how to do it."""
    return await onboarding.build(ctrl)


@router.get("/rdma")
async def rdma(ctrl: Controller = Depends(controller_dep)):
    """Discovered RoCE HCAs / GID index per node vs. controller.yaml."""
    return await ctrl.rdma_report()


class LinkTestRequest(BaseModel):
    mode: Literal["tcp", "rdma"] = "tcp"
    duration_s: float = Field(5.0, gt=0, le=60)
    port: int = Field(29511, gt=1023, lt=65536)
    streams: int = Field(4, ge=1, le=16)


@router.post("/link-test")
async def link_test(req: LinkTestRequest, ctrl: Controller = Depends(controller_dep)):
    if ctrl.busy():
        raise HTTPException(409, "an activation is running — link tests would disturb it")
    try:
        return await ctrl.run_link_test(mode=req.mode, duration_s=req.duration_s,
                                        port=req.port, streams=req.streams)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except AgentActionError as e:
        raise HTTPException(502, e.detail)


@router.get("/metrics")
async def metrics(live: bool = False, ctrl: Controller = Depends(controller_dep)):
    """Latest serving metrics (sampled every metrics_interval_s); ``live`` scrapes now."""
    act = ctrl.active()
    if not act:
        return {"ok": False, "active": False, "note": "no model is active"}
    if not live and ctrl.sampler.latest():
        return {"ok": True, "active": True, **ctrl.sampler.latest()}
    st = ctrl.gateway.routes.get(act.get("alias", "default"))
    if not st or not st.backends:
        return {"ok": False, "active": True, "note": "no backend routed yet"}
    snaps = [await scrape_metrics(b, headers=ctrl.gateway.backend_headers()) for b in st.backends]
    return {"active": True, **combine_snapshots(snaps)}


@router.get("/metrics/series")
def metrics_series(ctrl: Controller = Depends(controller_dep)):
    return ctrl.sampler.series()


@router.get("/telemetry")
async def telemetry(refresh: bool = False, ctrl: Controller = Depends(controller_dep)):
    """Per-node memory/GPU telemetry (refreshed by the background loop)."""
    if refresh or not ctrl.telemetry:
        await ctrl.metrics_tick(refresh_nodes=True)
    return ctrl.telemetry


class HeadlessApply(BaseModel):
    mode: Literal["desktop", "headless-safe", "headless-max"]
    now: bool = False      # also stop the display manager right away (not just at next boot)


@router.get("/headless")
async def headless_status(ctrl: Controller = Depends(controller_dep)):
    return await ctrl.headless_status()


@router.post("/headless")
async def headless_apply(req: HeadlessApply, ctrl: Controller = Depends(controller_dep)):
    if req.now and ctrl.busy():
        raise HTTPException(409, "an activation is running")
    try:
        return await ctrl.headless_apply(req.mode, now=req.now)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/foreign")
async def foreign(ctrl: Controller = Depends(controller_dep)):
    """vLLM/SGLang containers running outside TwinSpark (they hold GPU memory)."""
    return await ctrl.foreign_containers()


class ForeignStop(BaseModel):
    node: str
    name: str
    confirm: str = Field(..., description="must repeat the container name")


@router.post("/foreign/stop")
async def foreign_stop(req: ForeignStop, ctrl: Controller = Depends(controller_dep)):
    try:
        return await ctrl.stop_foreign(req.node, req.name, req.confirm)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except AgentActionError as e:          # subclass of RuntimeError — keep it first
        raise HTTPException(422, e.detail)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
