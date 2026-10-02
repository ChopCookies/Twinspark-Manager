"""Central Controller service (spec §4.1): desired state, jobs, profile lifecycle.

Owns the profile store, one activation at a time, stand-alone weight staging,
the post-activation watchdog and the metrics sampler. Diagnostics, pinning and
model-file operations live in sibling modules and are wired in here.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import replace
from typing import Any, Optional

from ..gateway.gateway import Gateway
from ..metrics import MetricsSampler, combine_snapshots, scrape_metrics
from ..schemas.config import ControllerConfig
from ..schemas.enums import JobState
from ..schemas.job import AuditEntry, Job
from ..schemas.profile import _NAME_RE, ImmutableIdentity, Profile, ProfileDraft, ProfileRevision, _now
from .agent_client import AgentActionError, AgentClient
from .jobs import ActivationCoordinator
from .launch import LaunchPlan, LaunchPlanner
from .planner import MemoryPlanner, ModelSpec
from .state_machine import StageFailed
from .store import Store

log = logging.getLogger("twinspark.controller")


class DuplicateProfileError(ValueError):
    pass


class BusyError(RuntimeError):
    """Another activation/stop is running."""


class Controller:
    """Owns state, coordination and the desired-state loop for one cluster.

    One deployment at a time (one profile across one or both nodes).
    """

    def __init__(self, config: ControllerConfig, gateway: Gateway,
                 agents: Optional[dict[str, AgentClient]] = None,
                 store: Optional[Store] = None, poll_interval: float = 5.0,
                 hf_token: Optional[str] = None):
        self.config = config
        self.gateway = gateway
        self.store = store or Store(config.db_path)
        self.planner = MemoryPlanner()
        self.planner.mem_total_provider = self._mem_total
        self.agents: dict[str, AgentClient] = dict(agents or {})
        self.poll_interval = poll_interval
        self.hf_token = hf_token
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._background: list[asyncio.Task] = []
        self._cancel: set[str] = set()
        self._staging: dict[tuple[str, str], asyncio.Future] = {}
        self.current_job: Optional[str] = None
        self.busy_profile: Optional[str] = None      # profile named by the running activation/prepare
        self.sampler = MetricsSampler()
        self.telemetry: dict[str, dict] = {}
        self.telemetry_history: dict[str, list] = {}
        from .maintenance import Maintenance
        self.maintenance = Maintenance(self)
        self.watch_state: dict[str, Any] = {"unhealthy_streak": 0, "recoveries": [], "last": None}
        self._audit("system", "controller.init", "controller", {})

    # ---- audit ---------------------------------------------------------------
    def _audit(self, actor: str, action: str, resource: str, detail: dict) -> None:
        try:
            self.store.append_audit(AuditEntry(actor=actor, action=action, resource=resource,
                                               detail=detail))
        except Exception:  # audit must never break the control plane
            log.exception("audit write failed")

    # ---- profiles --------------------------------------------------------------
    def create_profile(self, draft: ProfileDraft) -> Profile:
        """Create the profile with its working draft and, if pinned, its first revision."""
        if self.store.load_profile(draft.name):
            raise DuplicateProfileError(f"profile already exists: {draft.name}")
        p = Profile(name=draft.name, description=draft.description, draft=draft)
        if draft.fully_pinned():
            p.add_revision(draft, draft.identity)
        self.store.save_profile(p)
        self._audit("user", "profile.create", f"profile/{p.name}",
                    {"source": draft.source.get("recipe")} if draft.source else {})
        return p

    def list_profiles(self) -> list[Profile]:
        return self.store.list_profiles()

    def get_profile(self, name: str) -> Optional[Profile]:
        return self.store.load_profile(name)

    def delete_profile(self, name: str) -> bool:
        active = self.active()
        if active and active.get("profile") == name:
            raise BusyError("profile is currently active — stop it first")
        if self._lock.locked() and self.busy_profile == name:
            # the activation would finish and mark a profile that no longer exists as active
            raise BusyError("profile is being activated or prepared right now — wait for the job")
        ok = self.store.delete_profile(name)
        if ok:
            self._audit("user", "profile.delete", f"profile/{name}", {})
        return ok

    def duplicate_profile(self, name: str, new_name: str) -> Profile:
        p = self.get_profile(name)
        if p is None:
            raise ValueError(f"unknown profile: {name}")
        if self.get_profile(new_name):
            raise DuplicateProfileError(f"profile already exists: {new_name}")
        if not _NAME_RE.match(new_name):
            raise ValueError("invalid profile name")
        clone = p.duplicate(new_name)
        self.store.save_profile(clone)
        self._audit("user", "profile.duplicate", f"profile/{new_name}", {"from": name})
        return clone

    def save_draft(self, name: str, draft: ProfileDraft) -> Profile:
        """Replace the working draft (no revision). Identity is kept if unchanged."""
        p = self.get_profile(name)
        if p is None:
            raise ValueError(f"unknown profile: {name}")
        if draft.name != name:
            raise ValueError("draft name must match the profile")
        p.draft = draft
        p.description = draft.description or p.description
        p.updated_at = _now()
        self.store.save_profile(p)
        self._audit("user", "profile.draft", f"profile/{name}", {})
        return p

    def resolve_identity(self, draft: ProfileDraft) -> ImmutableIdentity:
        if not draft.fully_pinned():
            raise ValueError("the draft is not pinned yet — pin it (`tsm pin <profile>`) or give "
                             "an explicit identity (model commit sha + image digest)")
        return draft.identity

    def save_revision(self, draft: ProfileDraft) -> ProfileRevision:
        p = self.get_profile(draft.name)
        if p is None:
            raise ValueError(f"unknown profile: {draft.name}")
        rev = p.add_revision(draft, self.resolve_identity(draft))
        self.store.save_profile(p)
        self._audit("user", "profile.revision", f"profile/{p.name}", {"revision": rev.label})
        return rev

    async def pin_profile(self, name: str, **kw) -> dict:
        from .pinning import pin_profile
        return await pin_profile(self, name, **kw)

    def compose_split(self, name: str, node_a: str, node_b: str,
                      alias_a: str, alias_b: str) -> Profile:
        """Snapshot two working recipes; future edits of the sources stay independent."""
        parts = []
        for source, node, alias in ((node_a, "A", alias_a), (node_b, "B", alias_b)):
            p = self.get_profile(source)
            d = p.working_draft() if p else None
            if d is None:
                raise ValueError(f"profile not found: {source}")
            if d.secondary:
                raise ValueError("choose two individual recipes, not an existing split")
            doc = d.model_dump(mode="json")
            doc["simple"].update(topology=f"single-{node.lower()}", api_alias=alias, extra_aliases=[])
            if d.simple.topology.value in ("tp2", "pp2", "tp-ep"):
                doc["source"].pop("kv_bytes_per_token", None)
                doc["source"]["notes"] = doc["source"].get("notes", []) + [
                    "Per-node KV measurements from the distributed recipe do not apply here."]
            doc["verification"] = "experimental"
            parts.append(doc)
        doc = parts[0]
        doc.update(name=name, description=f"{node_a} on A + {node_b} on B", secondary=parts[1])
        doc["simple"]["topology"] = "split"
        original_source = dict(doc.get("source", {}))
        doc["source"] = {**original_source, "recipe": "split", "profiles": [node_a, node_b],
                         "notes": original_source.get("notes", []) + [
                             "Both source recipes now run on a single node each. Check memory fit "
                             "and recipe requirements before preparing."],
                         "node_a": original_source, "node_b": parts[1].get("source", {})}
        return self.create_profile(ProfileDraft.model_validate(doc))

    def set_pinned(self, name: str, ref: str) -> ProfileRevision:
        p = self.get_profile(name)
        rev = p.get_revision(ref) if p else None
        if rev is None:
            raise ValueError("revision not found")
        for r in p.revisions:
            r.pinned = r.revision_id == rev.revision_id
        p.pinned_revision = rev.revision_id
        self.store.save_profile(p)
        return rev

    def restore_last_known_good(self, name: str) -> Optional[ProfileRevision]:
        p = self.get_profile(name)
        if not p:
            return None
        for rev in reversed(p.revisions):
            if rev.known_good:
                return rev
        return p.get_revision(p.pinned_revision) if p.pinned_revision else None

    def _mark_known_good(self, name: str, revision_id: str, facts: Optional[dict] = None) -> None:
        p = self.get_profile(name)
        rev = p.get_revision(revision_id) if p else None
        if rev and not rev.known_good:
            rev.known_good = True
            self.store.save_profile(p)
        if facts:
            self.store.kv_set(f"observed:{revision_id}", facts)

    # ---- model specs for the memory planner ------------------------------------
    def set_model_spec(self, repo: str, spec: ModelSpec) -> None:
        self.store.kv_set(f"model_spec:{repo}", spec.__dict__)

    def model_spec(self, repo: str) -> Optional[ModelSpec]:
        d = self.store.kv_get(f"model_spec:{repo}")
        if not d:
            return None
        return ModelSpec(**{k: v for k, v in d.items() if k in ModelSpec.__dataclass_fields__})

    def profile_fit(self, name: str, ref: Optional[str] = None) -> dict[str, Any]:
        """Will this profile fit, and roughly how many KV tokens does it leave?

        Uses the registered ModelSpec (real checkpoint size from the Hub, measured
        KV bytes/token from the recipe) and the nodes' real MemTotal. After a
        successful activation the observed KV pool from vLLM's boot log is shown
        next to the estimate.
        """
        p = self.get_profile(name)
        if p is None:
            raise ValueError(f"unknown profile: {name}")
        rev = p.get_revision(ref) if ref else None
        if ref and rev is None:
            raise ValueError(f"revision not found: {name}@{ref}")
        d = rev.draft if rev else p.working_draft()
        if d is None:
            raise ValueError("profile has no draft")
        if d.secondary:
            fits = {node: self._draft_fit(part, [node], recipe_kv=True)
                    for node, part in zip(("A", "B"), d.parts(), strict=True)}
            return {"profile": name, "topology": "split", "nodes": fits,
                    "known": all(f["known"] for f in fits.values()),
                    "fits": all(f.get("fits", False) for f in fits.values())}
        out = self._draft_fit(d, rev.required_nodes() if rev else None)
        out["profile"] = name
        if rev is None and p.latest():
            rev = p.latest()
        if rev is not None:
            out["observed"] = self.store.kv_get(f"observed:{rev.revision_id}")
        return out

    def _draft_fit(self, d: ProfileDraft, nodes=None, recipe_kv=False) -> dict:
        s, a = d.simple, d.advanced
        out = {"model": s.model, "known": False}
        spec = self.model_spec(s.model)
        if spec is None:
            out["note"] = "model size unknown — pin the profile (or Resolve the model) first"
            return out
        if recipe_kv:
            spec = replace(spec, kv_bytes_per_token=d.source.get("kv_bytes_per_token"))
        ctx, conc = s.planning_context(), a.max_num_seqs or s.concurrency
        budget = self.planner.estimate(spec=spec, quant=s.quantization, context_length=ctx,
                                       concurrency=conc, topology=s.topology,
                                       kv_dtype=a.kv_dtype, using_cuda_graphs=not a.eager_mode,
                                       mem_total_gib=self._mem_total(nodes))
        util = a.gpu_memory_utilization or self.planner.gpu_memory_utilization(budget)
        fits, headroom = self.planner.fits(budget, util)
        fixed = budget.vllm_needed - budget.kv_cache           # weights + scratch + graphs + comms
        pool = util * budget.mem_total - fixed
        per_tok = budget.kv_cache * 1024**3 / max(1, ctx * conc)
        out.update({
            # vLLM starts as long as weights + one max-length sequence fit in its pool
            "known": True, "fits": pool > 0 and pool * 1024**3 >= per_tok * ctx,
            "full_concurrency_fits": fits,
            "headroom_gib": round(headroom, 2), "level": self.planner.classify_headroom(headroom),
            "gpu_memory_utilization": util, "weights_gib_per_node": round(budget.model_weights, 2),
            "kv_pool_gib_per_node": round(max(0.0, pool), 2),
            "kv_needed_gib_per_node": round(budget.kv_cache, 2),
            "est_kv_tokens": int(max(0.0, pool) * 1024**3 / per_tok) if per_tok > 0 else None,
            "est_full_context_seqs": round(max(0.0, pool) * 1024**3 / per_tok / ctx, 1)
            if per_tok > 0 else None,
            "context_length": ctx, "concurrency": conc,
            "kv_bytes_per_token_source": "measured" if spec.kv_bytes_per_token else "formula",
            "budget": budget.as_dict(),
        })
        return out

    # ---- transparency ------------------------------------------------------------
    def launch_plan(self, rev: ProfileRevision) -> LaunchPlan:
        util = rev.draft.advanced.gpu_memory_utilization or self.planner.gpu_memory_utilization(
            self.planner.reserve_budget(mem_total_gib=self._mem_total(rev.parts()[0].required_nodes())))
        node_utils = {n: part.draft.advanced.gpu_memory_utilization or
                      self.planner.gpu_memory_utilization(self.planner.reserve_budget(
                          mem_total_gib=self._mem_total(part.required_nodes())))
                      for part in rev.parts() for n in part.required_nodes()}
        return LaunchPlanner(self.config).plan(rev, util, node_utilization=node_utils)

    def _mem_total(self, nodes=None) -> Optional[float]:
        totals = [(self.store.kv_get(f"hardware:{n}") or {}).get("mem_total_gib") for n in (nodes or self.agents)]
        totals = [t for t in totals if t]
        return min(totals) if totals else None

    async def refresh_hardware(self) -> dict:
        out = {}
        for n, a in self.agents.items():
            try:
                facts = await a.call("hardware_facts", timeout=30)
                self.store.kv_set(f"hardware:{n}", facts)
                out[n] = facts
            except Exception as exc:  # noqa: BLE001
                out[n] = {"error": str(exc)}
        return out

    # ---- state ------------------------------------------------------------------
    def active(self) -> Optional[dict]:
        return self.store.kv_get("active")

    def busy(self) -> bool:
        return self._lock.locked() or self.maintenance.blocking()

    def active_revision(self) -> Optional[ProfileRevision]:
        act = self.active()
        if not act:
            return None
        p = self.get_profile(act["profile"])
        return p.get_revision(act["revision_id"]) if p else None

    def status(self) -> dict:
        act = self.active()
        rev = self.active_revision()
        detail = None
        if rev:
            detail = {"model": rev.identity.model_repo, "model_revision": rev.identity.model_revision,
                      "image": rev.identity.image_ref, "topology": rev.draft.simple.topology.value,
                      "context_length": rev.draft.simple.context_length,
                      "observed": self.store.kv_get(f"observed:{rev.revision_id}")}
            detail["models"] = [{"node": part.required_nodes(), "model": part.identity.model_repo,
                                 "model_revision": part.identity.model_revision,
                                 "image": part.identity.image_ref, "aliases": part.draft.simple.aliases}
                                for part in rev.parts()]
        return {
            "active": act,
            "active_detail": detail,
            "busy": self.busy(),
            "current_job": self.current_job,
            "staging": [f"{r}@{s[:8]}" for (r, s) in self._staging],
            "nodes": {n: self.store.kv_get(f"hardware:{n}") for n in self.config.nodes},
            "telemetry": self.telemetry,
            "maintenance": self.maintenance.state(),
            "routes": [st.to_payload() for st in self.gateway.routes.values()],
            "metrics": self.sampler.latest(),
            "watchdog": {"enabled": self.config.watchdog.enabled,
                         "unhealthy_streak": self.watch_state["unhealthy_streak"],
                         "last_incident": self.store.kv_get("last_incident")},
        }

    # ---- activation ---------------------------------------------------------------
    async def activate(self, profile_name: str, ref: str = "latest", *,
                       _rollback_of: Optional[str] = None, _recovery: bool = False,
                       _maintenance: bool = False) -> Job:
        """Validate synchronously, then run the activation in the background."""
        p = self.get_profile(profile_name)
        if p is not None and not p.revisions:
            raise ValueError(f"profile {profile_name} is not pinned yet — pin it first "
                             f"(`tsm pin {profile_name}` or the Pin button)")
        rev = p.get_revision(ref) if p else None
        if rev is None:
            raise ValueError(f"revision not found: {profile_name}@{ref}")
        if self._lock.locked() or (self.maintenance.blocking() and not _maintenance):
            raise BusyError("another activation or stop is in progress")
        missing = [n for n in rev.required_nodes() if n not in self.agents]
        if missing:
            raise RuntimeError(f"{rev.draft.simple.topology.value} needs node(s) {missing}, "
                               f"which are not configured")
        kind = "rollback" if _rollback_of else ("recovery" if _recovery else "activation")
        job = Job(
            job_id=f"act-{secrets.token_hex(5)}", kind=kind,
            profile_revision=rev.revision_id,
            payload={"profile": profile_name, "revision_id": rev.revision_id,
                     "label": rev.label, "alias": rev.draft.simple.api_alias,
                     "rollback_of": _rollback_of},
        )
        self.store.save_job(job)
        await self._lock.acquire()           # taken now so a second request gets 409
        self.current_job = job.job_id
        self.busy_profile = profile_name
        self._spawn(self._run_activation(job, rev))
        self._audit("controller" if (_rollback_of or _recovery) else "user", "profile.activate",
                    f"profile/{profile_name}", {"revision": rev.revision_id, "job": job.job_id,
                                                "kind": kind})
        return job

    def cancel_job(self, job_id: str) -> dict:
        job = self.store.load_job(job_id)
        if job is None:
            raise ValueError("job not found")
        if job.state not in (JobState.RUNNING, JobState.PENDING):
            return {"cancelled": False, "note": f"job is {job.state.value}"}
        self._cancel.add(job_id)
        self._audit("user", "job.cancel", f"job/{job_id}", {})
        return {"cancelled": True, "note": "cancellation requested — activations stop before the "
                                           "old model is touched, or roll back during loading"}

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)                # keep a strong reference (asyncio docs)
        task.add_done_callback(self._tasks.discard)
        return task

    def _persist(self, job: Job) -> None:
        # the revision object rides along in payload for the handlers; never persist it
        rev = job.payload.pop("_revision", None)
        try:
            self.store.save_job(job)
        finally:
            if rev is not None:
                job.payload["_revision"] = rev

    def _staging_wait(self, repo: str, sha: str):
        fut = self._staging.get((repo, sha))
        return asyncio.shield(fut) if fut is not None and not fut.done() else None

    async def _run_activation(self, job: Job, rev: ProfileRevision) -> None:
        from .state_machine import ActivationStateMachine

        previous = self.active()
        rollback_target: Optional[tuple[str, str]] = None
        cancel_check = lambda: job.job_id in self._cancel   # noqa: E731
        try:
            coord = ActivationCoordinator(
                config=self.config, agents=self.agents, gateway=self.gateway,
                planner=self.planner, persist=self._persist,
                model_spec=self.model_spec(rev.identity.model_repo),
                model_specs={part.identity.model_repo: self.model_spec(part.identity.model_repo)
                             for part in rev.parts()},
                poll_interval=self.poll_interval, cancel_check=cancel_check,
                staging_wait=self._staging_wait,
            )
            job.payload["_revision"] = rev
            job.payload["mem_total_gib"] = self._mem_total()
            job.payload["node_mem_total"] = {n: self._mem_total([n]) for n in self.agents}
            try:
                await ActivationStateMachine(job, coord.handle, self._persist,
                                             cancel_check=cancel_check).run()
                job.state = JobState.COMPLETED
                self.store.kv_set("active", {"profile": rev.profile_name,
                                             "revision_id": rev.revision_id, "label": rev.label,
                                             "alias": rev.draft.simple.api_alias,
                                             "aliases": rev.draft.aliases,
                                             "since": time.time()})
                self._mark_known_good(rev.profile_name, rev.revision_id, {
                    "kv": job.payload.get("kv"), "max_model_len": job.payload.get("max_model_len"),
                    "models": job.payload.get("models"),
                    "load_seconds": _stage_seconds(job, "loading"), "at": time.time()})
                self.watch_state["unhealthy_streak"] = 0
            except StageFailed as exc:
                job.state = JobState.FAILED
                if not exc.destructive:
                    # old deployment is untouched — just reopen its aliases
                    for a in coord.drained:
                        self.gateway.cancel_drain(a)
                    job.rollback = "failed before stopping the old model; it keeps serving"
                else:
                    await self._stop_everything(coord.drained)
                    self.store.kv_set("active", None)
                    if (self.config.auto_rollback and previous and not job.payload.get("rollback_of")
                            and previous.get("revision_id") != rev.revision_id):
                        rollback_target = (previous["profile"], previous["revision_id"])
                        job.rollback = (f"stopped partial containers; restoring previous "
                                        f"{previous['profile']} {previous.get('label', '')}")
                    else:
                        job.rollback = "stopped partial containers; nothing to restore"
        except Exception as exc:  # noqa: BLE001 - bug in the controller itself
            log.exception("activation crashed")
            job.state, job.error = JobState.FAILED, f"internal error: {exc}"
        finally:
            self._persist(job)
            job.payload.pop("_revision", None)
            self.store.save_job(job)
            self._cancel.discard(job.job_id)
            self.current_job = None
            self.busy_profile = None
            self._lock.release()
        if rollback_target:
            try:
                await self.activate(*rollback_target, _rollback_of=job.job_id)
            except Exception:  # noqa: BLE001
                log.exception("automatic rollback could not start")

    async def _stop_everything(self, aliases: list[str],
                               failed_nodes: Optional[list[str]] = None) -> list[str]:
        stopped: list[str] = []
        for n, client in self.agents.items():
            try:
                stopped += (await client.call("containers_stop_owned", timeout=180))["stopped"]
            except Exception:  # noqa: BLE001
                log.exception("stop on node %s failed", n)
                if failed_nodes is not None:
                    failed_nodes.append(n)
        for a in set(aliases) | set(self.gateway.routes):
            self.gateway.clear_route(a)
            self.gateway.cancel_drain(a)
        return stopped

    async def stop(self, *, _maintenance: bool = False) -> Job:
        if self._lock.locked() or (self.maintenance.blocking() and not _maintenance):
            raise BusyError("an activation is in progress")
        async with self._lock:
            job = Job(job_id=f"stop-{secrets.token_hex(5)}", kind="stop")
            step = job.begin_step("stopping")
            aliases = [a for a, st in self.gateway.routes.items() if st.backends]
            for a in aliases:
                self.gateway.begin_drain(a)
            drained = await asyncio.gather(*(self.gateway.wait_idle(a, self.config.runtime.drain_timeout_s)
                                             for a in aliases))
            if _maintenance and not all(drained):
                for alias in aliases:
                    self.gateway.cancel_drain(alias)
                raise RuntimeError("requests did not drain within the timeout; maintenance has not stopped the model")
            failed_nodes: list[str] = []
            stopped = await self._stop_everything(aliases, failed_nodes)
            self.store.kv_set("active", None)
            if failed_nodes:
                # Be honest: containers on an unreachable node may still be running (and holding
                # its GPU memory). The reachable nodes are stopped and the route is closed.
                msg = (f"stopped {stopped}, but node(s) {', '.join(sorted(failed_nodes))} could not be "
                       f"reached — containers there may still be running; check with `tsm doctor` "
                       f"once the node is back")
                job.fail_step(step, msg)
                job.state = JobState.FAILED
                job.error = msg
            else:
                job.finish_step(step, f"stopped {stopped}")
                job.state = JobState.COMPLETED
            self.store.save_job(job)
            self._audit("user", "deployment.stop", "cluster",
                        {"stopped": stopped, "unreachable": sorted(failed_nodes)})
            return job

    # ---- boot / power-cut recovery (spec §36) --------------------------------------
    async def on_startup(self) -> Optional[Job]:
        for j in self.store.list_jobs(limit=50):
            if j.state in (JobState.RUNNING, JobState.PENDING):
                j.state, j.error = JobState.FAILED, "interrupted by controller restart"
                self.store.save_job(j)
        await self.refresh_hardware()
        if self.maintenance.blocking():
            self.maintenance.ensure_running()
            return None
        active = self.active()
        if not active:
            return None
        await self._wait_for_agents(self.config.startup_wait_s)
        if await self._adopt_running(active):
            return None
        if not self.config.autostart:
            return None
        try:
            return await self.activate(active["profile"], active["revision_id"])
        except Exception:  # noqa: BLE001
            log.exception("autostart of %s failed", active)
            return None

    async def _wait_for_agents(self, timeout_s: float) -> list[str]:
        """After a power cut both Sparks boot at once; the other node's agent may come up a minute
        after this controller. Wait (bounded) so autostart sees the real state of both nodes.
        Returns the nodes that are still unreachable when the wait ends."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            down = []
            for n, a in self.agents.items():
                try:
                    await a.call("hardware_facts", timeout=8)
                except Exception:  # noqa: BLE001 - not up yet is the expected case here
                    down.append(n)
            if not down or time.monotonic() >= deadline:
                if down:
                    log.warning("starting without node(s) %s: they did not answer within %ss",
                                ", ".join(sorted(down)), timeout_s)
                return down
            await asyncio.sleep(min(3.0, max(0.05, self.poll_interval)))

    async def _adopt_running(self, active: dict) -> bool:
        """Controller restarted but the containers kept running: re-attach routes
        instead of restarting a healthy model."""
        p = self.get_profile(active["profile"])
        rev = p.get_revision(active["revision_id"]) if p else None
        if rev is None:
            return False
        try:
            plan = self.launch_plan(rev)
            for c in plan.containers:
                r = await self.agents[c.node].call("health_probe", name=c.name, url=c.health_url)
                if not r.get("healthy"):
                    return False
        except Exception:  # noqa: BLE001
            return False
        observed = self.store.kv_get(f"observed:{rev.revision_id}") or {}
        for alias, backends in plan.routes.items():
            model = plan.route_models.get(alias, plan.served_model_name)
            self.gateway.set_route(alias, backends, model, rev.revision_id,
                                   max_model_len=(observed.get("models") or {}).get(model, {}).get(
                                       "max_model_len", observed.get("max_model_len")))
        self._audit("controller", "deployment.adopt", f"profile/{rev.profile_name}", {})
        return True

    # ---- background: watchdog + metrics ------------------------------------------
    def start_background(self) -> None:
        if self._background:
            return
        if self.config.watchdog.enabled:
            self._background.append(asyncio.create_task(self._watchdog_loop()))
        self._background.append(asyncio.create_task(self._metrics_loop()))
        self.maintenance.ensure_running()

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.watchdog.interval_s)
            try:
                await self.watchdog_tick()
            except Exception:  # noqa: BLE001
                log.exception("watchdog tick failed")

    async def watchdog_tick(self) -> Optional[str]:
        """One health check of the active deployment. Returns the incident, if any."""
        if self.busy():
            return None
        rev = self.active_revision()
        if rev is None:
            return None
        plan = self.launch_plan(rev)
        problem = None
        unhealthy = False
        unreachable = False
        bad: list = []                         # containers behind the problem (decides which routes go down)
        for c in plan.containers:
            try:
                r = await self.agents[c.node].call("health_probe", name=c.name, url=c.health_url)
            except AgentActionError as exc:
                problem = f"node {c.node} unreachable: {exc.detail}"
                unreachable = True
                bad = [x for x in plan.containers if x.node == c.node]
                break
            if r.get("exited"):
                oom = " (OOM-killed)" if r.get("oom_killed") else ""
                problem = f"container {c.name} on node {c.node} exited with code {r.get('exit_code')}{oom}"
                self.store.kv_set("last_incident_log", (r.get("log_tail") or "")[-4000:])
                bad = [c]
                break
            if not r.get("healthy"):
                unhealthy = True
                bad.append(c)
        # A probe can take seconds; an operator may have stopped or switched the model meanwhile.
        # Never "recover" something that was deliberately stopped.
        current = self.active_revision()
        if self.busy() or current is None or current.revision_id != rev.revision_id:
            return None
        if problem is None and unhealthy:
            self.watch_state["unhealthy_streak"] += 1
            if self.watch_state["unhealthy_streak"] >= 3:
                problem = "API health check failed 3 times in a row"
        elif problem is None:
            if self.watch_state["unhealthy_streak"] or any(
                    st.down_reason for st in self.gateway.routes.values()):
                for alias in plan.routes:
                    self.gateway.mark_up(alias)
            self.watch_state["unhealthy_streak"] = 0
            self.watch_state.pop("waiting_on", None)
            return None
        if problem is None:
            return None
        # In a split deployment only the routes served by the failing container go down; a worker
        # container (no API of its own) or an unknown cause takes every route of the revision.
        urls = {c.health_url for c in bad}
        scoped = bool(bad) and None not in urls
        for alias, backends in plan.routes.items():
            if not scoped or urls & set(backends):
                self.gateway.mark_down(alias, problem)
        incident = {"at": time.time(), "profile": rev.profile_name, "revision": rev.revision_id,
                    "problem": problem, "action": "none"}
        wd = self.config.watchdog
        now = time.time()
        recent = [t for t in self.watch_state["recoveries"] if now - t < 3600]
        self.watch_state["recoveries"] = recent
        if not unreachable:
            self.watch_state.pop("waiting_on", None)
        if unreachable and wd.auto_recover:
            # Restarting cannot succeed while a node of the deployment is away, and every failed
            # attempt would burn the hourly budget. The route stays marked down; the first tick
            # after the node is back sees the real state and recovers with the full budget.
            incident["action"] = "waiting for the node to come back (no restart attempted)"
            waiting_on = tuple(sorted({c.node for c in bad}))
            if self.watch_state.get("waiting_on") == waiting_on:
                # same outage as the previous tick: refresh the incident, keep the audit log quiet
                self.store.kv_set("last_incident", incident)
                return problem
            self.watch_state["waiting_on"] = waiting_on
        elif wd.auto_recover and len(recent) < wd.max_recoveries_per_hour:
            self.watch_state["recoveries"].append(now)
            try:
                job = await self.activate(rev.profile_name, rev.revision_id, _recovery=True)
                incident["action"] = f"restarting ({job.job_id})"
            except Exception as exc:  # noqa: BLE001
                incident["action"] = f"restart failed: {exc}"
        elif wd.auto_recover:
            incident["action"] = "recovery budget exhausted — manual action needed"
        self.store.kv_set("last_incident", incident)
        self._audit("controller", "deployment.incident", f"profile/{rev.profile_name}", incident)
        self.watch_state["unhealthy_streak"] = 0
        return problem

    async def _metrics_loop(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(self.config.metrics_interval_s)
            n += 1
            try:
                await self.metrics_tick(refresh_nodes=True)
            except Exception:  # noqa: BLE001
                log.exception("metrics tick failed")

    async def metrics_tick(self, refresh_nodes: bool = True) -> Optional[dict]:
        if refresh_nodes:
            async def sample_node(name, agent):
                try:
                    tel = await agent.call("system_telemetry", timeout=8)
                    available = tel.get("mem_available_gib")
                    tel["level"] = MemoryPlanner.classify_headroom(available) if available is not None else "UNKNOWN"
                    tel["at"] = time.time()
                    self.telemetry[name] = tel
                    history = self.telemetry_history.setdefault(name, [])
                    history.append(tel)
                    del history[:-120]
                except Exception as exc:  # noqa: BLE001
                    self.telemetry[name] = {**self.telemetry.get(name, {}), "error": str(exc), "stale": True}
            await asyncio.gather(*(sample_node(name, agent) for name, agent in self.agents.items()))
        act = self.active()
        if not act or self.busy():
            return None
        backends = {b for alias in act.get("aliases", [act.get("alias", "default")])
                    if alias in self.gateway.routes for b in self.gateway.routes[alias].backends}
        if not backends:
            return None
        snaps = [await scrape_metrics(b, headers=self.gateway.backend_headers()) for b in sorted(backends)]
        return self.sampler.add(combine_snapshots(snaps), act.get("revision_id"))

    # ---- delegated feature areas ---------------------------------------------------
    async def run_link_test(self, mode: str = "tcp", duration_s: float = 5.0, port: int = 29511,
                            streams: int = 4) -> dict:
        from .diagnostics import run_link_test
        return await run_link_test(self, mode=mode, duration_s=duration_s, port=port, streams=streams)

    async def rdma_report(self) -> dict:
        from .diagnostics import rdma_report
        return await rdma_report(self)

    async def doctor(self) -> dict:
        from .diagnostics import doctor
        return await doctor(self)

    async def headless_apply(self, mode: str, now: bool = False) -> dict:
        from .diagnostics import headless_apply
        return await headless_apply(self, mode, now)

    async def headless_status(self) -> dict:
        from .diagnostics import headless_status
        return await headless_status(self)

    async def foreign_containers(self) -> dict:
        from .diagnostics import foreign_containers
        return await foreign_containers(self)

    async def stop_foreign(self, node: str, name: str, confirm: str) -> dict:
        from .diagnostics import stop_foreign
        return await stop_foreign(self, node, name, confirm)

    async def model_files(self) -> dict:
        from .files import inventory
        return await inventory(self)

    async def delete_model_files(self, repo: str, revision: Optional[str], nodes: list[str],
                                 force: bool = False, preview: bool = False) -> dict:
        from .files import delete_model
        return await delete_model(self, repo, revision, nodes, force=force, preview=preview)

    async def stage(self, ref: str, nodes: Optional[list[str]] = None,
                    include: Optional[list[str]] = None, download_node: Optional[str] = None,
                    verify: Optional[bool] = None) -> Job:
        from .files import stage
        return await stage(self, ref, nodes=nodes, include=include,
                           download_node=download_node, verify=verify)

    async def mods(self) -> dict:
        from .files import mods_overview
        return await mods_overview(self)

    async def install_mod(self, name: str, archive_b64: str, nodes: Optional[list[str]] = None) -> dict:
        from .files import install_mod
        return await install_mod(self, name, archive_b64, nodes)

    async def remove_mod(self, name: str) -> dict:
        from .files import remove_mod
        return await remove_mod(self, name)

    async def aclose(self) -> None:
        for t in self._background + list(self._tasks):
            t.cancel()
        self._background = []
        for a in self.agents.values():
            await a.aclose()


def _stage_seconds(job: Job, stage: str) -> Optional[float]:
    from datetime import datetime
    for s in job.steps:
        if s.stage == stage and s.started_at and s.finished_at:
            try:
                return round((datetime.fromisoformat(s.finished_at) -
                              datetime.fromisoformat(s.started_at)).total_seconds(), 1)
            except ValueError:
                return None
    return None
