"""Prepare an immutable recipe without draining or stopping the current deployment."""
from __future__ import annotations

import secrets

from ..schemas.enums import ActivationStage, JobState
from ..schemas.job import Job
from .jobs import ActivationCoordinator
from .state_machine import guidance_for


async def prepare_steps(ctrl, job: Job, rev) -> None:
    """Shared preparation stages; the caller owns the cluster lock and job lifetime."""
    job.payload.update(_revision=rev, mem_total_gib=ctrl._mem_total(),
                       node_mem_total={n: ctrl._mem_total([n]) for n in ctrl.agents})
    coord = ActivationCoordinator(
        config=ctrl.config, agents=ctrl.agents, gateway=ctrl.gateway,
        planner=ctrl.planner, persist=ctrl._persist,
        model_specs={part.identity.model_repo: ctrl.model_spec(part.identity.model_repo)
                     for part in rev.parts()},
        poll_interval=ctrl.poll_interval,
        cancel_check=lambda: job.job_id in ctrl._cancel, staging_wait=ctrl._staging_wait)
    for stage in (ActivationStage.VALIDATING, ActivationStage.RESOLVING,
                  ActivationStage.DOWNLOADING, ActivationStage.SYNCING):
        step = job.begin_step(stage)
        ctrl._persist(job)
        try:
            if job.job_id in ctrl._cancel:
                raise RuntimeError("cancelled by user")
            msg = await coord.handle(job, stage, step)
            if job.job_id in ctrl._cancel:
                raise RuntimeError("cancelled by user")
        except Exception as exc:
            job.fail_step(step, str(exc), getattr(exc, "excerpt", None))
            raise
        job.finish_step(step, msg or step.message)
        ctrl._persist(job)


async def prepare(ctrl, name: str, revision: str = "latest") -> Job:
    from .controller import BusyError

    if ctrl.busy():
        raise BusyError("cluster is busy — wait for the current operation")
    p = ctrl.get_profile(name)
    rev = p.get_revision(revision) if p else None
    if rev is None:
        raise ValueError("pinned revision not found — pin the recipe first")
    missing = [n for n in rev.required_nodes() if n not in ctrl.agents]
    if missing:
        raise ValueError(f"nodes not configured: {', '.join(missing)}")
    job = Job(job_id=f"prepare-{secrets.token_hex(5)}", kind="prepare",
              profile_revision=rev.revision_id,
              payload={"profile": name, "revision": rev.label})
    ctrl.store.save_job(job)              # before the lock: a failing save must not leave it held
    await ctrl._lock.acquire()
    ctrl.current_job = job.job_id
    ctrl.busy_profile = name

    async def run():
        try:
            await prepare_steps(ctrl, job, rev)
            job.state = JobState.COMPLETED
            job.payload["ready"] = True
        except Exception as exc:
            job.state = JobState.FAILED
            job.error = str(exc)
            job.guidance = guidance_for(str(exc))
        finally:
            job.payload.pop("_revision", None)
            ctrl._persist(job)
            ctrl._cancel.discard(job.job_id)
            ctrl.current_job = None
            ctrl.busy_profile = None
            ctrl._lock.release()
            ctrl._audit("user", "profile.prepare", f"profile/{name}",
                        {"revision": rev.revision_id, "state": job.state.value})

    ctrl._spawn(run())
    return job
