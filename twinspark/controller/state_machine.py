"""Durable activation state machine (spec §19, §21, §36).

Runs a Job through a fixed stage pipeline. Every stage transition is persisted
(``persist`` callback) so the GUI can show live progress and a crashed
controller leaves an honest record. A failing stage records error + log
excerpt and raises ``StageFailed`` — the controller decides about rollback.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from ..schemas.enums import ActivationStage
from ..schemas.job import Job, JobStep

# vLLM's /health only turns 200 after weights are loaded AND CUDA graphs are
# captured, so LOADING covers "loading / compiling / warming"; the handler
# reports the current sub-phase from the container log.
STAGE_ORDER: list[ActivationStage] = [
    ActivationStage.VALIDATING,
    ActivationStage.RESOLVING,
    ActivationStage.DOWNLOADING,
    ActivationStage.SYNCING,
    ActivationStage.DRAINING,
    ActivationStage.STOPPING,
    ActivationStage.RECLAIMING,
    ActivationStage.STARTING_CLUSTER,
    ActivationStage.LOADING,
    ActivationStage.TESTING,
    ActivationStage.ROUTING,
    ActivationStage.HEALTHY,
]
# Failing at or after this stage means the previous deployment is already gone.
DESTRUCTIVE_FROM = ActivationStage.STOPPING

Handler = Callable[[Job, ActivationStage, JobStep], Awaitable[Optional[str]]]


class StageFailed(Exception):
    def __init__(self, stage: ActivationStage, cause: BaseException):
        super().__init__(f"{stage.value}: {cause}")
        self.stage = stage
        self.cause = cause
        self.excerpt = getattr(cause, "excerpt", None)

    @property
    def destructive(self) -> bool:
        return STAGE_ORDER.index(self.stage) >= STAGE_ORDER.index(DESTRUCTIVE_FROM)


class ActivationStateMachine:
    def __init__(self, job: Job, handler: Handler,
                 persist: Callable[[Job], None] = lambda j: None,
                 cancel_check: Callable[[], bool] = lambda: False):
        self.job, self.handler, self.persist = job, handler, persist
        self.cancel_check = cancel_check

    async def run(self) -> Job:
        done = self.job.completed_stages()
        for stage in STAGE_ORDER:
            if stage.value in done:
                continue
            step = self.job.begin_step(stage)
            self.persist(self.job)
            try:
                # cancelling is only honoured while the old deployment is untouched
                if self.cancel_check() and \
                        STAGE_ORDER.index(stage) < STAGE_ORDER.index(DESTRUCTIVE_FROM):
                    raise RuntimeError("cancelled by user")
                message = await self.handler(self.job, stage, step)
            except Exception as exc:  # noqa: BLE001
                self.job.fail_step(step, str(exc), excerpt=getattr(exc, "excerpt", None))
                self.job.guidance = guidance_for(str(exc) + " " + (step.log_excerpt or ""))
                self.persist(self.job)
                raise StageFailed(stage, exc) from exc
            self.job.finish_step(step, message or step.message)
            self.persist(self.job)
        return self.job


FAILURE_ADVICE: dict[str, str] = {
    "out of memory": "reduce context length or concurrency, use an fp8 KV cache, "
                     "or lower gpu_memory_utilization",
    "oom killer": "the kernel killed the container: lower gpu_memory_utilization, stop the "
                  "desktop (headless mode) or other processes, check that page cache was dropped",
    "free memory": "drop the page cache before starting (install tsm-privd) and go headless",
    "less than desired": "vLLM saw too little free memory at startup: drop the page cache "
                         "(tsm-privd), go headless, or lower gpu_memory_utilization",
    "cuda graph": "enable eager mode to skip CUDA graph capture",
    "port ": "another process uses the vLLM port — stop your old vLLM or change runtime.vllm_port",
    "agent unreachable": "check that tsm-agent runs on that node and the agent_url is right",
    "nccl": "run `tsm link --mode rdma`; check rdma_hcas / ib_gid_index / qsfp_iface on both nodes",
    "not configured": "add the missing node under `nodes:` in controller.yaml",
    "secret slot": "run `tsm init` on that node so the vault has the shared secrets",
    "revision": "pin the model to a full 40-char commit sha (`tsm pin <profile>`)",
    "trust-remote-code": "this model needs trust_remote_code — review and enable it explicitly",
    "trust_remote_code": "this model needs trust_remote_code — review and enable it explicitly",
    "no space": "free disk space (`tsm models ls` / `tsm models rm`) or move hf_cache_dir",
    "disk space": "free disk space (`tsm models ls` / `tsm models rm`) or move hf_cache_dir",
    "mod(s) not installed": "install the recipe's mods on both nodes: `tsm mods install <dir>`",
    "mod ": "install the recipe's mods on both nodes: `tsm mods install <dir>`",
    "unrecognized arguments": "a flag is not supported by the pinned image — check the recipe "
                              "against the image, or pin the image the recipe was written for",
    "no module named": "the image lacks a component the recipe expects — use the recipe's image "
                       "or its mods",
    "ssh": "set up key-based SSH from the source node to nodes.<X>.ssh_user@qsfp_ip "
           "(`tsm doctor` checks it)",
    "cancelled": "the previous deployment was left untouched",
}


def guidance_for(text: str) -> list[str]:
    t = text.lower()
    return list(dict.fromkeys(advice for key, advice in FAILURE_ADVICE.items() if key in t))
