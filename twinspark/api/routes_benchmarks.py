"""Benchmark results, kept on node A against the exact revision they measured (master plan §2.1).

The runs themselves happen on the workstation client (twinspark.client.bench), against the gateway apps
use; node A only stores the finished result document, like an evaluation record. Loaded lazily.
"""
from __future__ import annotations

import json
import math
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ..controller.controller import Controller
from ..schemas.enums import JobState
from ..schemas.job import Job
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1/benchmarks", tags=["benchmarks"], dependencies=[Depends(require_auth)])

MAX_BYTES = 1024 * 1024
PRESETS = ("max-throughput", "latency-slo", "context", "quality", "fixed")


def _finite(doc: Any, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(doc, float):
        return math.isfinite(doc)
    if isinstance(doc, dict):
        return len(doc) <= 400 and all(isinstance(k, str) and len(k) <= 80 and _finite(v, depth + 1)
                                       for k, v in doc.items())
    if isinstance(doc, list):
        return len(doc) <= 2000 and all(_finite(v, depth + 1) for v in doc)
    return doc is None or isinstance(doc, (str, int, bool)) and (not isinstance(doc, str) or len(doc) <= 4000)


def _find_revision(ctrl: Controller, revision_id: Optional[str]):
    if not revision_id:
        return None, None
    for p in ctrl.store.list_profiles():
        for r in p.revisions:
            if r.revision_id == revision_id:
                return p, r
    return None, None


def _row(job: Job, full: bool = False) -> dict:
    out = {"id": job.job_id, "created_at": job.created_at, "revision_id": job.profile_revision,
           **{k: job.payload.get(k) for k in ("profile", "label", "preset", "alias", "summary", "valid",
                                               "invalid_reason", "evidence")}}
    if full:
        out["result"] = job.payload.get("result")
    return out


@router.post("", status_code=201)
async def record(request: Request, ctrl: Controller = Depends(controller_dep)):
    """Store one result document (``twinspark-bench/1``) from the client."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "benchmark result too large (1 MiB maximum)")
        body.extend(chunk)
    try:
        doc = json.loads(body)
    except ValueError:
        raise HTTPException(422, "send the result document as JSON") from None
    if not isinstance(doc, dict) or doc.get("format") != "twinspark-bench/1":
        raise HTTPException(422, "not a twinspark-bench/1 result")
    if doc.get("preset") not in PRESETS or not isinstance(doc.get("points"), list) or not _finite(doc):
        raise HTTPException(422, "the result document is malformed (preset, points, finite numbers, sizes)")
    profile, rev = _find_revision(ctrl, doc.get("revision_id"))
    if rev is None:
        raise HTTPException(404, f"the result measured revision {str(doc.get('revision_id'))[:80]!r}, which node A "
                                 "does not know: results are kept against a revision")
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else {}
    job = Job(job_id=f"benchmark-{secrets.token_hex(5)}", kind="benchmark",
              profile_revision=rev.revision_id, payload={
                  "profile": profile.name, "label": rev.label,
                  "preset": doc["preset"], "alias": str(doc.get("alias") or "")[:63],
                  "summary": summary, "valid": bool(doc.get("valid")), "invalid_reason": doc.get("invalid_reason"),
                  # measured from the workstation through the gateway: real requests, but the client's own report
                  "evidence": "measured-by-client", "result": doc})
    step = job.begin_step("record-results")
    job.finish_step(step, str(summary.get("text") or "recorded")[:300])
    job.state, job.stage = JobState.COMPLETED, "completed"
    ctrl.store.save_job(job)
    ctrl._audit("user", "benchmark.record", f"profile/{profile.name}",
                {"job": job.job_id, "revision_id": job.profile_revision, "preset": doc["preset"]})
    return _row(job)


@router.get("")
def listing(profile: Optional[str] = None, revision: Optional[str] = None, limit: int = 50,
            ctrl: Controller = Depends(controller_dep)):
    jobs = [j for j in ctrl.store.list_jobs(limit=500) if j.kind == "benchmark"]
    if profile:
        jobs = [j for j in jobs if j.payload.get("profile") == profile]
    if revision:
        jobs = [j for j in jobs if j.profile_revision == revision or j.payload.get("label") == revision]
    return [_row(j) for j in jobs[:max(1, min(limit, 200))]]


@router.get("/{bench_id}")
def one(bench_id: str, ctrl: Controller = Depends(controller_dep)):
    job = ctrl.store.load_job(bench_id)
    if job is None or job.kind != "benchmark":
        raise HTTPException(404, "no such benchmark")
    return _row(job, full=True)
