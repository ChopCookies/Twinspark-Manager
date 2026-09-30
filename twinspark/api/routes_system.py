"""Planning / advisory routes (spec §9-§11). Headless mode lives in routes_diagnostics."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..controller.autopilot import ContextAdvisor, MemoryAutopilot, QuantizationAdvisor
from ..controller.controller import Controller
from ..controller.planner import ModelSpec, make_spec_from_hf
from ..schemas.enums import MemoryStrategy, Quantization, Topology
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1/system", tags=["system"], dependencies=[Depends(require_auth)])


@router.get("/telemetry")
def telemetry(ctrl: Controller = Depends(controller_dep)):
    return {"nodes": ctrl.telemetry, "history": ctrl.telemetry_history,
            "interval_s": ctrl.config.metrics_interval_s}


@router.get("/maintenance")
def maintenance_status(ctrl: Controller = Depends(controller_dep)):
    return ctrl.maintenance.state()


@router.post("/maintenance/plan")
async def maintenance_plan(ctrl: Controller = Depends(controller_dep)):
    return await ctrl.maintenance.plan()


class MaintenanceRequest(BaseModel):
    confirm: str
    firmware: bool = False


@router.post("/maintenance/start")
async def maintenance_start(req: MaintenanceRequest, ctrl: Controller = Depends(controller_dep)):
    if req.confirm != "UPDATE AND REBOOT":
        raise HTTPException(422, "explicit update and reboot confirmation is required")
    try:
        return await ctrl.maintenance.start(req.firmware)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/maintenance/resume")
async def maintenance_resume(ctrl: Controller = Depends(controller_dep)):
    try:
        return await ctrl.maintenance.resume()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/maintenance/release")
async def maintenance_release(ctrl: Controller = Depends(controller_dep)):
    try:
        return await ctrl.maintenance.release()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


class SpecSource(BaseModel):
    """Either a registered repo, or an inline HF config.json (+ safetensors total_size)."""

    repo: Optional[str] = None
    hf_config: Optional[dict[str, Any]] = None
    weight_bytes: Optional[int] = None


class PlanRequest(SpecSource):
    quantization: Quantization = Quantization.NVFP4
    topology: Topology = Topology.SINGLE_A
    context_length: int = Field(32768, ge=1024)
    concurrency: int = Field(1, ge=1)
    kv_dtype: Optional[str] = None
    strategy: MemoryStrategy = MemoryStrategy.BALANCED
    contexts: list[int] = Field(default_factory=lambda: [32768, 65536, 131072, 262144])


def _spec(ctrl: Controller, src: SpecSource) -> ModelSpec:
    if src.hf_config:
        try:
            return make_spec_from_hf(src.hf_config, src.weight_bytes)
        except ValueError as e:
            raise HTTPException(422, str(e))
    if src.repo:
        spec = ctrl.model_spec(src.repo)
        if spec:
            return spec
    # never silently fall back to some other model's numbers
    raise HTTPException(422, "unknown model: register it via PUT /api/v1/system/model-specs "
                             "or send hf_config + weight_bytes")


@router.put("/model-specs")
def register_spec(src: SpecSource, ctrl: Controller = Depends(controller_dep)):
    if not (src.repo and src.hf_config):
        raise HTTPException(422, "repo and hf_config are required")
    spec = _spec(ctrl, src)
    ctrl.set_model_spec(src.repo, spec)
    return {"ok": True, "spec": spec.__dict__}


@router.post("/memory/estimate")
def estimate(req: PlanRequest, ctrl: Controller = Depends(controller_dep)):
    spec = _spec(ctrl, req)
    budget = ctrl.planner.estimate(spec=spec, quant=req.quantization,
                                   context_length=req.context_length, concurrency=req.concurrency,
                                   topology=req.topology, kv_dtype=req.kv_dtype)
    util = ctrl.planner.gpu_memory_utilization(budget)
    fits, headroom = ctrl.planner.fits(budget, util)
    return {"fits": fits, "headroom_gib": round(headroom, 2),
            "level": ctrl.planner.classify_headroom(headroom),
            "gpu_memory_utilization": util, "per_node": budget.as_dict()}


@router.post("/autopilot/plan")
def autopilot(req: PlanRequest, ctrl: Controller = Depends(controller_dep)):
    a = MemoryAutopilot(ctrl.planner, _spec(ctrl, req), req.quantization, req.topology).plan(
        req.strategy, req.context_length, req.concurrency)
    return a.__dict__


@router.post("/context/max-safe")
def context_advisory(req: PlanRequest, ctrl: Controller = Depends(controller_dep)):
    adv = ContextAdvisor(ctrl.planner, _spec(ctrl, req), req.quantization, req.topology, req.kv_dtype)
    return {"rows": [r.as_row() for r in adv.find_max_safe(req.concurrency, req.contexts)]}


@router.post("/quantization/variants")
def quantization_variants(req: PlanRequest, ctrl: Controller = Depends(controller_dep)):
    qa = QuantizationAdvisor(ctrl.planner, _spec(ctrl, req), req.topology)
    return [{**v.__dict__, "quant": v.quant.value, "status": v.status.value}
            for v in qa.variants(req.context_length, req.concurrency)]


@router.get("/model-specs/{org}/{name}")
def get_spec(org: str, name: str, ctrl: Controller = Depends(controller_dep)):
    spec = ctrl.model_spec(f"{org}/{name}")
    if spec is None:
        raise HTTPException(404, "no spec registered — pin a profile using this model or resolve it")
    return spec.__dict__


@router.get("/hardware")
async def hardware(refresh: bool = False, ctrl: Controller = Depends(controller_dep)):
    """Hardware facts per node (driver, memory, RDMA devices, desktop, disk)."""
    if refresh:
        return await ctrl.refresh_hardware()
    return {n: ctrl.store.kv_get(f"hardware:{n}") for n in ctrl.config.nodes}
