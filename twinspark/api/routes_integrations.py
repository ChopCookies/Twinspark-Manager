"""Agent client configuration, protocol checks and attributed harness reports."""
from __future__ import annotations

import json
import math
import re
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..controller.compatibility import check_alias
from ..controller.controller import BusyError, Controller
from ..controller.integrations import catalog, export_client
from ..schemas.enums import JobState
from ..schemas.job import Job
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"],
                   dependencies=[Depends(require_auth)])


@router.get("")
def integrations(ctrl: Controller = Depends(controller_dep)):
    return catalog(ctrl)


@router.get("/export")
def export(client: str = Query(max_length=64), alias: str = Query(max_length=63),
           base_url: str = Query(max_length=2048), secondary_alias: str | None = Query(None, max_length=63),
           ctrl: Controller = Depends(controller_dep)):
    try:
        return export_client(ctrl, client, alias, base_url, secondary_alias)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1, max_length=63)
    checks: list[Literal["chat", "streaming", "tools", "structured", "responses"]] | None = \
        Field(None, min_length=1, max_length=5)


@router.post("/check", status_code=202)
async def check(req: CheckRequest, ctrl: Controller = Depends(controller_dep)):
    try:
        return await check_alias(ctrl, req.alias, req.checks)
    except BusyError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1, max_length=63)
    revision_id: str = Field(min_length=1, max_length=128)
    results: dict


def _finite(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def record_evaluation(ctrl, req: EvaluationRequest) -> Job:
    selected = [(p, r) for p in ctrl.list_profiles() for r in p.revisions if r.revision_id == req.revision_id]
    if not selected or req.alias not in selected[0][1].draft.aliases:
        raise ValueError("select an existing immutable recipe revision that owns this alias")
    profile, revision = selected[0]
    document = req.results
    result_metrics = document.get("results")
    if not isinstance(result_metrics, dict) or not result_metrics or len(result_metrics) > 100:
        raise ValueError("expected an LM Evaluation Harness results object with at most 100 tasks")
    config = document.get("config", {})
    if not isinstance(config, dict):
        raise ValueError("invalid evaluation config")
    model_args = config.get("model_args", {})
    if isinstance(model_args, str):
        model_args = dict(item.split("=", 1) for item in model_args.split(",") if "=" in item)
        model_args = {k.strip(): v.strip() for k, v in model_args.items()}
    if not isinstance(model_args, dict):
        raise ValueError("invalid evaluation model_args")
    reported_model = model_args.get("model")
    if reported_model is not None and reported_model != req.alias:
        raise ValueError("evaluation model alias differs from the selected alias")
    metrics = {}
    for task, values in result_metrics.items():
        if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_.:/ -]{1,120}", task):
            raise ValueError("invalid evaluation task name")
        if not isinstance(values, dict) or len(values) > 100:
            raise ValueError("expected at most 100 metrics per task")
        task_metrics = {}
        for name, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not _finite(value) or not re.fullmatch(r"[A-Za-z0-9_.:,/ -]{1,120}", name):
                    raise ValueError("metric values must be finite and metric names must be plain text")
                task_metrics[name] = value
        if task_metrics:
            metrics[task] = task_metrics
    if not metrics:
        raise ValueError("report contains no finite numeric metrics")
    limit = config.get("limit")
    if not _finite(limit):
        limit = None
    job = Job(job_id=f"evaluation-{secrets.token_hex(5)}", kind="evaluation",
              profile_revision=revision.revision_id, payload={
                  "alias": req.alias, "profile": profile.name, "revision_id": revision.revision_id,
                  "source": "lm-evaluation-harness", "evidence": "reported", "verified": False,
                  "metrics": metrics, "sample_limit": limit,
                  "note": "Imported client report. Hardware execution and recipe attribution "
                          "were not independently verified."})
    step = job.begin_step("import-results")
    job.finish_step(step, "Recorded numeric metrics only; prompts, samples, "
                         "configuration and credentials were excluded.")
    job.state, job.stage = JobState.COMPLETED, "completed"
    ctrl.store.save_job(job)
    ctrl._audit("user", "integration.evaluation", f"profile/{profile.name}",
                {"job": job.job_id, "revision_id": revision.revision_id, "alias": req.alias,
                 "tasks": list(metrics), "evidence": "reported"})
    return job


@router.post("/evaluations", status_code=201)
async def evaluations(request: Request, ctrl: Controller = Depends(controller_dep)):
    # Harness reports can contain enormous samples; reject oversized input before decoding.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 1024 * 1024:
            raise HTTPException(413, "report too large (1 MiB maximum); use results JSON without sample logs")
        body.extend(chunk)
    try:
        req = EvaluationRequest.model_validate(json.loads(body))
        return record_evaluation(ctrl, req)
    except (ValueError, ValidationError, RecursionError):
        # Do not echo payload or validation input, which can include client credentials.
        raise HTTPException(422, "invalid report; select the exact recipe revision and matching model alias, "
                            "and supply finite numeric LM Evaluation Harness results")
