"""Status, jobs (activation / stage / stop), cancel, logs and audit (spec §19, §21, §29)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from ..controller.agent_client import AgentActionError
from ..controller.controller import BusyError, Controller
from ..controller.planner import MemoryPlanner
from ..schemas.job import AuditEntry, Job
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1", tags=["activation"], dependencies=[Depends(require_auth)])

_CONTAINER = re.compile(r"^tsm-[a-z0-9._-]+\Z")


@router.get("/status")
def status(ctrl: Controller = Depends(controller_dep)):
    return ctrl.status()


@router.get("/jobs", response_model=list[Job])
def list_jobs(limit: int = 30, kind: str | None = None, ctrl: Controller = Depends(controller_dep)):
    jobs = ctrl.store.list_jobs(limit=min(max(limit, 1), 200) * (3 if kind else 1))
    if kind:
        jobs = [j for j in jobs if j.kind == kind][:limit]
    return [_brief(j) for j in jobs]


def _brief(job: Job) -> Job:
    """The list is polled every few seconds: startup evidence (tens of KB per failed job) stays on /jobs/{id}."""
    ev = job.payload.get("startup_evidence")
    if not isinstance(ev, dict) or not ev.get("containers"):
        return job
    return job.model_copy(update={"payload": {**job.payload, "startup_evidence": {
        "containers": len(ev["containers"]), "detail": f"/api/v1/jobs/{job.job_id}"}}})


@router.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: str, ctrl: Controller = Depends(controller_dep)):
    job = ctrl.store.load_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@router.get("/jobs/{job_id}/evidence/{fname}")
def job_evidence_file(job_id: str, fname: str, ctrl: Controller = Depends(controller_dep)):
    """A saved container log of a failed activation (redacted when it was saved)."""
    from fastapi.responses import FileResponse

    from ..controller import evidence

    job = ctrl.store.load_job(job_id)
    ev = (job.payload.get("startup_evidence") if job else None) or {}
    ranks = (ev.get("containers") or []) if isinstance(ev, dict) else []
    files = {c.get("file") for c in ranks if isinstance(c, dict)}
    if not job or fname not in files:
        raise HTTPException(404, "no saved log with that name for this job")
    try:
        path = evidence.open_file(evidence.evidence_dir(ctrl.config.db_path), job_id, fname)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "the saved log was removed (only the newest failed jobs keep theirs)")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f"{job_id}-{fname}")


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, ctrl: Controller = Depends(controller_dep)):
    try:
        return ctrl.cancel_job(job_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/stop", response_model=Job)
async def stop(ctrl: Controller = Depends(controller_dep)):
    try:
        return await ctrl.stop()
    except BusyError as e:
        raise HTTPException(409, str(e))


@router.get("/headroom")
async def headroom(ctrl: Controller = Depends(controller_dep)):
    """Live headroom gauge per node (spec §29) — measured, not stored."""
    out = {}
    for n, agent in ctrl.agents.items():
        try:
            m = await agent.call("memory_telemetry", timeout=5)
            m["level"] = MemoryPlanner.classify_headroom(m["mem_available_gib"])
            out[n] = m
        except AgentActionError as exc:
            out[n] = {"error": exc.detail}
    return out


@router.get("/logs/{node}/{container}")
async def logs(node: str, container: str, tail: int = 300,
               ctrl: Controller = Depends(controller_dep)):
    if node not in ctrl.agents:
        raise HTTPException(404, "unknown node")
    if not _CONTAINER.match(container):
        raise HTTPException(422, "only TwinSpark containers (tsm-*) can be read here")
    try:
        return await ctrl.agents[node].call("container_logs", name=container,
                                            tail=max(1, min(tail, 20000)))
    except AgentActionError as e:
        raise HTTPException(502, e.detail)


@router.get("/active/logs")
async def active_logs(tail: int = 300, ctrl: Controller = Depends(controller_dep)):
    """Logs of every container of the active deployment (or the last one tried)."""
    rev = ctrl.active_revision()
    if rev is None:
        job = next((j for j in ctrl.store.list_jobs(limit=10)
                    if j.kind in ("activation", "rollback", "recovery")), None)
        p = ctrl.get_profile(job.payload.get("profile", "")) if job else None
        rev = p.get_revision(job.payload.get("revision_id")) if p and job else None
    if rev is None:
        return {"containers": []}
    plan = ctrl.launch_plan(rev)
    out = []
    for c in plan.containers:
        try:
            res = await ctrl.agents[c.node].call("container_logs", name=c.name,
                                                 tail=max(1, min(tail, 20000)))
            out.append({"node": c.node, "name": c.name, "role": c.role, "log": res.get("log", "")})
        except (AgentActionError, KeyError) as e:
            out.append({"node": c.node, "name": c.name, "role": c.role,
                        "error": getattr(e, "detail", str(e))})
    return {"profile": rev.profile_name, "revision": rev.label, "containers": out}


@router.get("/audit", response_model=list[AuditEntry])
def audit(limit: int = 100, ctrl: Controller = Depends(controller_dep)):
    return ctrl.store.audit_log(limit=max(1, min(limit, 1000)))
