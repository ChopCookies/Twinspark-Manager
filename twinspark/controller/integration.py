"""Automate a reviewed recipe snapshot into a pinned, prepared experiment."""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..schemas.enums import JobState
from ..schemas.job import Job
from ..schemas.profile import ProfileDraft
from .controller import BusyError, DuplicateProfileError
from .preparation import prepare_steps
from .state_machine import guidance_for


class NodePins(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_ref: Optional[str] = Field(None, max_length=512)
    image: Optional[str] = Field(None, max_length=1024)
    local_image: Optional[str] = Field(None, max_length=1024)

    @model_validator(mode="after")
    def one_image(self):
        if self.image and self.local_image:
            raise ValueError("choose image or local_image, not both")
        return self


class IntegrationPins(NodePins):
    secondary: Optional[NodePins] = None


class IntegrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: ProfileDraft
    existing: bool = False
    pins: IntegrationPins = Field(default_factory=IntegrationPins)
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class RetryRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


def _fingerprint(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def integrate(ctrl, req: IntegrationRequest, *, resume_revision=None, retry_of=None) -> Job:
    """Start once per request ID. Never fetch the recipe again or activate it."""
    req = req.model_copy(deep=True)
    snapshot = req.draft.model_dump(mode="json")
    pins = req.pins.model_dump(exclude_none=True)
    fingerprint = _fingerprint({"draft": snapshot, "pins": pins, "existing": req.existing,
                                "resume_revision": resume_revision, "retry_of": retry_of})
    job_id = "integrate-" + hashlib.sha256(req.request_id.encode()).hexdigest()[:24]
    prior = ctrl.store.load_job(job_id)
    if prior:
        if prior.kind != "integration" or prior.payload.get("input_fingerprint") != fingerprint:
            raise ValueError("request_id already used with different recipe settings")
        return prior
    if ctrl.busy():
        raise BusyError("cluster is busy — wait for the current operation")
    if pins.get("secondary") and not req.draft.secondary:
        raise ValueError("node B pin overrides require a split profile")
    required = set(n for part in req.draft.parts()
                   for n in (["A"] if part.simple.topology.value == "single-a" else
                             ["B"] if part.simple.topology.value == "single-b" else ["A", "B"]))
    missing = required - ctrl.agents.keys()
    if missing:
        raise ValueError(f"nodes not configured: {', '.join(sorted(missing))}")
    if resume_revision is None:                 # a retry reuses pins that already name an image
        from .pinning import missing_image
        sec = pins.get("secondary") or {}
        problems = [x for x in (
            missing_image(req.draft, pins.get("image"), pins.get("local_image"),
                          "node A: " if req.draft.secondary else ""),
            missing_image(req.draft.secondary, sec.get("image"), sec.get("local_image"), "node B: ",
                          req.draft.name)
            if req.draft.secondary else None) if x]
        if problems:
            raise ValueError("; ".join(problems))
    profile = ctrl.get_profile(req.draft.name)
    if req.existing:
        if not profile:
            raise ValueError("profile not found")
        if profile.working_draft().model_dump(mode="json") != snapshot:
            raise ValueError("profile changed since review — reload it before preparing")
    elif profile:
        raise DuplicateProfileError(f"profile already exists: {req.draft.name}")
    job = Job(job_id=job_id, kind="integration", payload={
        "profile": req.draft.name, "snapshot": snapshot, "pins": pins,
        "input_fingerprint": fingerprint, "retry_of": retry_of,
    })
    await ctrl._lock.acquire()
    try:
        if not req.existing:
            ctrl.create_profile(req.draft.model_copy(deep=True))
        ctrl.store.save_job(job)
        ctrl.current_job = job_id
        ctrl.busy_profile = req.draft.name
    except BaseException:
        ctrl._lock.release()
        raise

    async def run():
        step = None
        try:
            step = job.begin_step("pinning")
            ctrl._persist(job)

            def progress(message):
                step.message = message
                ctrl._persist(job)

            if job_id in ctrl._cancel:
                raise ValueError("cancelled by user")
            if resume_revision:
                rev = ctrl.get_profile(req.draft.name).get_revision(resume_revision)
                if rev is None:
                    raise ValueError("prepared revision no longer exists")
                job.finish_step(step, f"Reusing exact pins from {rev.label}")
            else:
                result = await ctrl.pin_profile(req.draft.name, **pins, progress=progress,
                                              cancel_check=lambda: job_id in ctrl._cancel)
                rev = ctrl.get_profile(req.draft.name).get_revision(result["revision"]["revision_id"])
                job.payload.update(resolved=result["resolved"], pin_notes=result["notes"])
                job.finish_step(step, f"Pinned both models to {rev.label}" if req.draft.secondary
                                else f"Pinned recipe to {rev.label}")
            job.profile_revision = rev.revision_id
            job.payload.update(revision=rev.label, pinned_snapshot=rev.draft.model_dump(mode="json"))
            ctrl._persist(job)
            step = None
            await prepare_steps(ctrl, job, rev)
            job.state = JobState.COMPLETED
            job.payload["ready"] = True
        except Exception as exc:
            if step and step.status == "running":
                job.fail_step(step, str(exc))
            job.state = JobState.FAILED
            job.error = str(exc)
            job.guidance = guidance_for(str(exc)) + [
                "Repair the reported prerequisite, then retry this job to reuse the same recipe and pins.",
                "To change recipe settings, edit the profile and start a new preparation instead.",
            ]
        finally:
            try:
                job.payload.pop("_revision", None)
                job.touch()
                ctrl._persist(job)
            finally:
                ctrl._cancel.discard(job_id)
                ctrl.current_job = None
                ctrl.busy_profile = None
                ctrl._lock.release()
            ctrl._audit("user", "recipe.integrate", f"profile/{req.draft.name}",
                        {"job": job_id, "revision": job.profile_revision, "state": job.state.value,
                         "retry_of": retry_of})

    ctrl._spawn(run())
    return job


async def retry(ctrl, job_id: str, req: RetryRequest) -> Job:
    job = ctrl.store.load_job(job_id)
    if not job or job.kind != "integration":
        raise ValueError("recipe integration job not found")
    if job.state != JobState.FAILED:
        raise ValueError("only failed or interrupted integration jobs can be retried")
    expected = job.payload.get("pinned_snapshot") or job.payload["snapshot"]
    return await integrate(ctrl, IntegrationRequest(
        draft=ProfileDraft.model_validate(expected), existing=True,
        pins=IntegrationPins.model_validate(job.payload["pins"]), request_id=req.request_id),
        resume_revision=job.profile_revision, retry_of=job_id)
