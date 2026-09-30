"""Durable job model (spec §19, §35, §36). Jobs are persisted, resumable, and audited."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from .enums import ActivationStage, JobState


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStep(BaseModel):
    stage: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    progress: float = 0.0            # 0..1
    status: str = "pending"          # pending | running | ok | failed | skipped
    message: str = ""
    log_excerpt: Optional[str] = None


class Job(BaseModel):
    job_id: str
    kind: str = "activation"         # activation | stop | download | sync | benchmark
    state: JobState = JobState.PENDING
    stage: Optional[str] = None      # free-form so non-activation jobs can use it too
    profile_revision: Optional[str] = None
    steps: list[JobStep] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    guidance: list[str] = Field(default_factory=list)
    rollback: Optional[str] = None   # what the controller did after a failure
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    def touch(self) -> None:
        self.updated_at = _now()

    def begin_step(self, stage: str | ActivationStage) -> JobStep:
        name = stage.value if isinstance(stage, ActivationStage) else stage
        step = JobStep(stage=name, started_at=_now(), status="running")
        self.steps.append(step)
        self.stage = name
        self.state = JobState.RUNNING
        self.touch()
        return step

    def finish_step(self, step: JobStep, message: str = "") -> None:
        step.status = "ok"
        step.progress = 1.0
        step.finished_at = _now()
        if message:
            step.message = message
        self.touch()

    def fail_step(self, step: JobStep, error: str, excerpt: Optional[str] = None) -> None:
        step.status = "failed"
        step.finished_at = _now()
        step.message = error
        step.log_excerpt = excerpt
        self.error = error
        self.state = JobState.FAILED
        self.stage = ActivationStage.FAILED.value
        self.touch()

    def completed_stages(self) -> set[str]:
        return {s.stage for s in self.steps if s.status == "ok"}


class AuditEntry(BaseModel):
    """Immutable audit log record (spec §40)."""

    ts: str = Field(default_factory=_now)
    actor: str
    action: str
    resource: str
    detail: dict[str, Any] = Field(default_factory=dict)
