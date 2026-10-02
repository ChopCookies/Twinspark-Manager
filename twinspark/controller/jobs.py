"""Activation coordinator: the stage handlers behind the state machine (spec §19)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Callable, Optional

import httpx

from ..schemas.enums import ActivationStage
from ..schemas.job import Job, JobStep
from ..schemas.profile import ProfileRevision
from .agent_client import AgentActionError, AgentClient
from .launch import LaunchError, LaunchPlan, LaunchPlanner
from .planner import MemoryPlanner, ModelSpec
from .staging import StageError, WeightsStager

if TYPE_CHECKING:
    from ..gateway.gateway import Gateway
    from ..schemas.config import ControllerConfig

log = logging.getLogger("twinspark.controller")

# log line -> human phase shown while LOADING (checked newest-first on the tail)
_PHASES = [
    (re.compile(r"application startup complete|started server process|uvicorn running", re.I),
     "starting API server"),
    (re.compile(r"capturing cuda graph|cuda graph", re.I), "capturing CUDA graphs"),
    (re.compile(r"autotun", re.I), "autotuning kernels"),
    (re.compile(r"torch\.compile|compil|dynamo", re.I), "compiling kernels"),
    (re.compile(r"kv cache|num_gpu_blocks|gpu kv cache size", re.I), "allocating KV cache"),
    (re.compile(r"loading (safetensors|weights|model)|load(ed)? weights|instanttensor", re.I),
     "loading weights"),
    (re.compile(r"applying mod|\[twinspark\]", re.I), "applying mods"),
    (re.compile(r"waiting|rendezvous|init_process_group|nccl|ray", re.I), "connecting nodes"),
]
_KV_TOKENS = re.compile(r"GPU KV cache size:\s*([\d,]+)\s*tokens", re.I)
_MAX_CONC = re.compile(r"Maximum concurrency for\s*([\d,]+)\s*tokens per request:\s*([\d.]+)x", re.I)


def phase_from_log(tail: str) -> Optional[str]:
    lines = [ln for ln in tail.splitlines() if ln.strip()][::-1]
    for ln in lines[:12]:
        for rx, label in _PHASES:
            if rx.search(ln):
                return label
    return None


def kv_facts_from_log(text: str) -> dict:
    out = {}
    m = _KV_TOKENS.search(text)
    if m:
        out["kv_cache_tokens"] = int(m.group(1).replace(",", ""))
    m = _MAX_CONC.search(text)
    if m:
        out["max_concurrency"] = float(m.group(2))
        out["max_concurrency_at_tokens"] = int(m.group(1).replace(",", ""))
    return out


class ActivationCoordinator:
    def __init__(self, *, config: "ControllerConfig", agents: dict[str, AgentClient],
                 gateway: "Gateway", planner: MemoryPlanner,
                 persist: Callable[[Job], None],
                 model_spec: Optional[ModelSpec] = None,
                 model_specs: Optional[dict[str, ModelSpec]] = None,
                 poll_interval: float = 5.0,
                 cancel_check: Optional[Callable[[], bool]] = None,
                 staging_wait: Optional[Callable[[str, str], "asyncio.Future | None"]] = None):
        self.config = config
        self.agents = agents
        self.gateway = gateway
        self.planner = planner
        self.launch = LaunchPlanner(config)
        self.persist = persist
        self.model_spec = model_spec
        self.model_specs = model_specs or {}
        self.poll = poll_interval
        self.plan: Optional[LaunchPlan] = None
        self.dry_run = False
        self.drained: list[str] = []            # aliases we put into draining
        self.cancel_check = cancel_check or (lambda: False)
        self.staging_wait = staging_wait

    def agent(self, node: str) -> AgentClient:
        if node not in self.agents:
            raise RuntimeError(f"node {node} is not configured / agent unreachable")
        return self.agents[node]

    async def handle(self, job: Job, stage: ActivationStage, step: JobStep) -> Optional[str]:
        rev: ProfileRevision = job.payload["_revision"]
        handler = {
            ActivationStage.VALIDATING: self._validate,
            ActivationStage.RESOLVING: self._resolve,
            ActivationStage.DOWNLOADING: self._download,
            ActivationStage.SYNCING: self._sync,
            ActivationStage.DRAINING: self._drain,
            ActivationStage.STOPPING: self._stop,
            ActivationStage.RECLAIMING: self._reclaim,
            ActivationStage.STARTING_CLUSTER: self._start,
            ActivationStage.LOADING: self._wait_loaded,
            ActivationStage.TESTING: self._smoke_test,
            ActivationStage.ROUTING: self._route,
            ActivationStage.HEALTHY: self._healthy,
        }[stage]
        return await handler(job, rev, step)

    # ---- helpers -------------------------------------------------------------
    def _models(self, rev: ProfileRevision):
        return [(m["repo"], m["revision"], m["include"], m["nodes"])
                for m in self.plan.model_requirements]

    def _util(self, rev: ProfileRevision, mem_total: Optional[float]) -> float:
        return rev.draft.advanced.gpu_memory_utilization or self.planner.gpu_memory_utilization(
            self.planner.reserve_budget(mem_total_gib=mem_total))

    # ---- stages -----------------------------------------------------------
    async def _validate(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        notes, node_utils = [], {}
        for part in rev.parts():
            s, a = part.draft.simple, part.draft.advanced
            for ref in a.extra_models:
                if not re.match(r"^[^@\s]+/[^@\s]+@[0-9a-f]{40}$", ref):
                    raise RuntimeError(f"extra model {ref!r} is not pinned to a commit sha — pin the profile")
            spec = self.model_specs.get(part.identity.model_repo)
            if not rev.draft.secondary:
                spec = spec or self.model_spec
            nodes = part.required_nodes()
            totals = [(job.payload.get("node_mem_total") or {}).get(n) for n in nodes]
            totals = [t for t in totals if t]
            mem_total = min(totals) if totals else job.payload.get("mem_total_gib")
            util = self._util(part, mem_total)
            if spec is not None:
                if rev.draft.secondary:
                    spec = replace(spec, kv_bytes_per_token=part.draft.source.get("kv_bytes_per_token"))
                if s.topology.value in ("tp2", "tp-ep") and spec.num_attention_heads % 2:
                    raise RuntimeError(f"{part.profile_name}: {spec.num_attention_heads} attention heads "
                                       "cannot split over TP2 — use pp2 or a single node")
                common = dict(spec=spec, quant=s.quantization, topology=s.topology,
                              backend=self.launch.resolve_backend(part), kv_dtype=a.kv_dtype,
                              using_cuda_graphs=not a.eager_mode, mem_total_gib=mem_total)
                budget = self.planner.estimate(context_length=s.planning_context(),
                                               concurrency=a.max_num_seqs or s.concurrency, **common)
                util = a.gpu_memory_utilization or self.planner.gpu_memory_utilization(budget)
                fits, headroom = self.planner.fits(budget, util)
                job.payload.setdefault("memory_estimates", {})[part.profile_name] = budget.as_dict()
                if not rev.draft.secondary:
                    job.payload["memory_estimate"] = budget.as_dict()
                if not fits:
                    one_ctx = s.context_length if isinstance(s.context_length, int) else 4096
                    single = self.planner.estimate(context_length=one_ctx, concurrency=1, **common)
                    ok1, head1 = self.planner.fits(single, util)
                    if not ok1:
                        raise RuntimeError(f"{part.profile_name}: estimated not to fit: weights "
                                           f"{single.model_weights:.1f} GiB/node + one sequence are "
                                           f"{-head1:.1f} GiB short per node (out of memory expected)")
                    notes.append(f"{part.profile_name}: KV estimate exceeds pool by {-headroom:.1f} "
                                 "GiB/node — vLLM fits fewer parallel sequences")
                notes.append(f"{part.profile_name}: estimated headroom {headroom:.1f} GiB/node")
            else:
                notes.append(f"{part.profile_name}: no model spec registered — memory check skipped")
            node_utils.update({n: util for n in nodes})
        try:
            self.plan = self.launch.plan(rev, node_utils[rev.required_nodes()[0]],
                                         node_utilization=node_utils)
        except LaunchError as exc:
            raise RuntimeError(str(exc)) from exc
        job.payload["launch_plan"] = self.plan.model_dump(mode="json")
        job.payload["warnings"] = [n for n in self.plan.notes if n.startswith("WARNING")]
        return "plan ready (" + ", ".join(f"{n}: gpu_memory_utilization={u:.2f}"
                                          for n, u in node_utils.items()) + "); " + "; ".join(notes)

    async def _resolve(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        nodes = self.plan.nodes
        multi = len(nodes) > 1 and any(c.role == "worker" for c in self.plan.containers)
        facts = {}
        warnings = []
        for n in nodes:
            facts[n] = await self.agent(n).call("hardware_facts", timeout=30)
            hcas = self.config.nodes[n].rdma_hcas if multi else []
            need_disk = (0 if facts[n].get("runtime_mode") == "dry-run"
                         else await self._disk_needed_gib(n, rev))
            pre = await self.agent(n).call("preflight", allow_owned_port=True,
                                           need_disk_gib=need_disk,
                                           mods=sorted({m for c in self.plan.containers
                                                        if c.node == n for m in c.mods}),
                                           need_rdma=multi, hcas=hcas)
            if not pre["ok"]:
                raise RuntimeError(f"node {n} preflight: " + "; ".join(pre["problems"]))
            warnings += [f"{n}: {w}" for w in pre.get("warnings", [])]
        self.dry_run = any(f.get("runtime_mode") == "dry-run" for f in facts.values())
        if self.dry_run and any(f.get("runtime_mode") != "dry-run" for f in facts.values()):
            raise RuntimeError("cannot mix simulated and real nodes in the same deployment")
        job.payload["hardware"] = {n: {k: v for k, v in f.items() if k != "foreign_inference"}
                                   for n, f in facts.items()}
        job.payload["dry_run"] = self.dry_run
        if len(nodes) > 1:
            drivers = {n: f.get("driver_version") for n, f in facts.items()}
            if len({d for d in drivers.values() if d}) > 1:
                warnings.append(f"NVIDIA driver differs between nodes {drivers} — mismatched "
                                f"drivers have cost 2x+ throughput on dual-Spark setups")
            if multi and self.plan.mods:
                st = {n: (await self.agent(n).call("mods_status", names=self.plan.mods))["status"]
                      for n in nodes}
                for m in self.plan.mods:
                    hashes = {st[n][m]["hash"] for n in nodes}
                    if len(hashes) > 1:
                        raise RuntimeError(f"mod {m} differs between nodes — reinstall it with "
                                           f"`tsm mods install` so both run identical patches")
        step.message = "pulling image if needed"
        self.persist(job)
        for c in self.plan.containers:
            await self.agent(c.node).call("image_ensure", image_ref=c.image_ref, timeout=3600)
        if rev.identity.image_source == "local" and len(nodes) > 1 and not rev.draft.secondary:
            ids = {n: (await self.agent(n).call("image_inspect", ref=rev.identity.image_ref)).get("id")
                   for n in nodes}
            if len(set(ids.values())) > 1:
                raise RuntimeError(f"local image differs between nodes: {ids}")
        job.payload["warnings"] = job.payload.get("warnings", []) + warnings
        msg = "nodes reachable, preflight ok, image present"
        if warnings:
            msg += " — " + " | ".join(warnings)
        return msg + (" [DRY-RUN]" if self.dry_run else "")

    async def _disk_needed_gib(self, node: str, rev: ProfileRevision) -> int:
        """Free space the weights still to be fetched onto ``node`` will need (0 if all present or
        the size is unknown), so a nearly full disk is reported before anything is stopped."""
        need = 0.0
        for repo, sha, include, nodes in self._models(rev):
            if node not in nodes:
                continue
            spec = self.model_specs.get(repo) or (None if rev.draft.secondary else self.model_spec)
            weight_bytes = getattr(spec, "weight_bytes", None) if spec else None
            if not weight_bytes:
                continue
            present = (await self.agent(node).call("weights_present", repo=repo, revision=sha,
                                                   include=include or []))["present"]
            if not present:
                need += weight_bytes / 1024 ** 3
        return int(need * 1.05) + 2 if need else 0

    async def _download(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        """Make sure every node of the plan has the weights (download + QSFP copy)."""
        stager = WeightsStager(self.config, self.agents, self.persist, poll=min(self.poll, 2.0),
                               dry_run=self.dry_run)
        msgs = []
        for repo, sha, include, nodes in self._models(rev):
            if self.staging_wait:
                pending = self.staging_wait(repo, sha)
                if pending is not None:
                    step.message = f"waiting for the running stage job of {repo}@{sha[:8]}"
                    self.persist(job)
                    await pending
            try:
                msgs.append(await stager.ensure(job, step, repo, sha, nodes,
                                                include=include,
                                                download_node=self.config.download_node,
                                                cancel_check=self.cancel_check))
            except (StageError, AgentActionError) as exc:
                raise RuntimeError(str(exc)) from exc
        return "; ".join(msgs)

    async def _sync(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        # weights are staged on every plan node by DOWNLOADING; this double-checks
        # completeness right before the old model is stopped
        for repo, sha, include, nodes in self._models(rev):
            for n in nodes:
                ok = (await self.agent(n).call("weights_present", repo=repo, revision=sha, include=include))["present"]
                if not ok:
                    raise RuntimeError(f"{repo}@{sha[:8]} is not complete on node {n}")
        return f"weights verified present on {', '.join(self.plan.nodes)}"

    async def _drain(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        # One deployment at a time: stopping owned containers takes down every
        # alias, so every serving alias is drained.
        aliases = [a for a, st in self.gateway.routes.items() if st.backends]
        aliases = sorted(set(aliases) | set(rev.draft.aliases))
        for a in aliases:
            self.gateway.begin_drain(a, retry_after=30)
        self.drained = aliases
        timeout = self.config.runtime.drain_timeout_s
        idle = await asyncio.gather(*(self.gateway.wait_idle(a, timeout) for a in aliases))
        forced = [a for a, ok in zip(aliases, idle, strict=True) if not ok]
        return "drained " + ", ".join(aliases) + (f" (forced after {timeout}s: {forced})" if forced else "")

    async def _stop(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        stopped = []
        skipped = []
        for n in sorted(self.agents):
            try:
                res = await self.agent(n).call("containers_stop_owned", timeout=180)
            except AgentActionError:
                if n in self.plan.nodes:
                    raise                      # the target needs this node: a real failure
                # An unrelated node being down (rebooting, unplugged) must not take down a model
                # that runs only on the other one.
                log.warning("node %s is unreachable and not part of the plan; skipping its stop", n)
                skipped.append(n)
                continue
            stopped += res["stopped"]
        for a in self.drained:
            self.gateway.clear_route(a)
        return (f"stopped {len(stopped)} TwinSpark container(s)" + (f": {stopped}" if stopped else "")
                + (f" (node {', '.join(skipped)} unreachable, not part of this deployment)" if skipped else ""))

    async def _reclaim(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        """Unified memory is released asynchronously after a container exits: drop the
        page cache (privd), wait until MemAvailable settles, then check the free
        memory vLLM's own startup gate will see."""
        rt = self.config.runtime
        report = []
        for n in self.plan.nodes:
            rec = await self.agent(n).call("reclaim_memory", drop_caches=rt.drop_caches_before_start,
                                           timeout=180)
            last, stable = -1.0, 0
            deadline = time.monotonic() + (3 if self.dry_run else 90)
            tel = rec.get("after") or {}
            while time.monotonic() < deadline:
                tel = await self.agent(n).call("memory_telemetry")
                avail = tel["mem_available_gib"]
                stable = stable + 1 if abs(avail - last) < 0.5 else 0
                last = avail
                if stable >= 2:
                    break
                await asyncio.sleep(min(self.poll, 3))
            total = tel.get("mem_total_gib") or 0
            util = self.plan.node_utilization.get(n, self.plan.gpu_memory_utilization)
            need = util * total
            line = f"{n}: {tel.get('mem_available_gib', 0):.1f} GiB available"
            if rec.get("dropped"):
                freed = rec.get("privd", {}).get("freed_page_cache_gib", 0)
                line += f", page cache dropped ({freed:.1f} GiB)"
            elif rec.get("note"):
                line += f" ({rec['note']})"
            report.append(line)
            if not self.dry_run and total and rt.free_memory_gate:
                if tel["mem_available_gib"] + 0.5 < need:
                    raise RuntimeError(
                        f"node {n}: only {tel['mem_available_gib']:.1f} GiB available but vLLM will "
                        f"claim {need:.1f} GiB (gpu_memory_utilization "
                        f"{util:.2f} x {total:.1f} GiB) — out of memory "
                        f"expected; stop other processes or lower gpu_memory_utilization")
                if tel.get("mem_free_gib", need) + 0.5 < need:
                    report.append(f"{n}: WARNING free memory {tel['mem_free_gib']:.1f} GiB < "
                                  f"{need:.1f} GiB — vLLM's startup check may refuse "
                                  f"(page cache not dropped?)")
        return "; ".join(report)

    async def _start(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        by_name = {c.name: c for c in self.plan.containers}
        started = []
        for i, wave in enumerate(self.plan.start_order):
            for name in wave:
                spec = by_name[name]
                await self.agent(spec.node).call("container_start",
                                                 spec=spec.model_dump(mode="json"), timeout=180)
                started.append(name)
                job.payload.setdefault("started_containers", []).append(
                    {"node": spec.node, "name": name})
                self.persist(job)
            delay = self.plan.wave_delays_s[i] if i < len(self.plan.wave_delays_s) else 0.0
            if delay and not self.dry_run and i < len(self.plan.start_order) - 1:
                step.message = f"started {', '.join(wave)}; waiting {delay:.0f}s before the next wave"
                self.persist(job)
                await asyncio.sleep(delay)
        return "started " + ", ".join(started)

    async def _wait_loaded(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        deadline = time.monotonic() + self.config.runtime.health_timeout_s
        started = time.monotonic()
        head = next(c for c in self.plan.containers if c.health_url)
        while True:
            results = []
            for c in self.plan.containers:
                r = await self.agent(c.node).call("health_probe", name=c.name, url=c.health_url)
                if r.get("exited"):
                    reason = " — killed by the kernel OOM killer" if r.get("oom_killed") else ""
                    err = RuntimeError(f"container {c.name} on node {c.node} exited "
                                       f"(code {r.get('exit_code')}){reason}")
                    err.excerpt = r.get("log_tail", "")          # type: ignore[attr-defined]
                    raise err
                results.append(r["healthy"])
            if all(results):
                break
            head = next((c for c, healthy in zip(self.plan.containers, results, strict=True)
                         if c.health_url and not healthy), head)
            if time.monotonic() > deadline:
                tail = (await self.agent(head.node).call("container_logs", name=head.name, tail=80))["log"]
                err = RuntimeError(f"not healthy after {self.config.runtime.health_timeout_s}s")
                err.excerpt = tail[-4000:]                       # type: ignore[attr-defined]
                raise err
            if self.cancel_check():
                raise RuntimeError("cancelled by user during loading")
            try:
                tail = (await self.agent(head.node).call("container_logs", name=head.name, tail=40))["log"]
                phase = phase_from_log(tail)
                kv = kv_facts_from_log(tail)
                if kv:
                    job.payload.setdefault("kv", {}).update(kv)
            except AgentActionError:
                phase = None
            step.message = f"{phase or 'starting'} ({time.monotonic() - started:.0f}s)"
            step.progress = min(0.95, (time.monotonic() - started) /
                                max(60.0, self.config.runtime.health_timeout_s / 3))
            self.persist(job)
            await asyncio.sleep(self.poll)
        for container in [c for c in self.plan.containers if c.health_url]:
            try:
                full = (await self.agent(container.node).call(
                    "container_logs", name=container.name, tail=2000))["log"]
                kv = kv_facts_from_log(full)
                model = next(self.plan.route_models[a] for a, urls in self.plan.routes.items()
                             if container.health_url in urls)
                job.payload.setdefault("models", {}).setdefault(model, {})["kv"] = kv
                if kv and not rev.draft.secondary:
                    job.payload.setdefault("kv", {}).update(kv)
            except AgentActionError:
                pass
        if rev.draft.secondary:
            job.payload.pop("kv", None)
        kv = job.payload.get("kv") or {}
        extra = f"; KV pool {kv['kv_cache_tokens']:,} tokens" if kv.get("kv_cache_tokens") else ""
        return f"healthy after {time.monotonic() - started:.0f}s{extra}"

    async def _smoke_test(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        if self.dry_run:
            return "skipped in dry-run"
        headers = self.gateway.backend_headers()
        out = []
        async with httpx.AsyncClient(timeout=180) as client:
            endpoints = {(url, self.plan.route_models.get(alias, self.plan.served_model_name))
                         for alias, urls in self.plan.routes.items() for url in urls}
            for url, model in sorted(endpoints):
                t0 = time.perf_counter()
                r = await client.post(f"{url}/v1/completions", headers=headers, json={
                    "model": model, "prompt": "Hello", "max_tokens": 8,
                    "temperature": 0})
                if r.status_code in (400, 404, 405, 422) and "ompletions" in r.text:
                    # Pooling / embedding / rerank models answer "does not support Completions
                    # API": the server is up and has the model, which is what this stage proves.
                    listed = await client.get(f"{url}/v1/models", headers=headers)
                    if listed.status_code != 200 or model not in listed.text:
                        raise RuntimeError(f"smoke test on {url}: model {model!r} is not listed by /v1/models")
                    out.append("model listed (no text completions)")
                elif r.status_code != 200:
                    raise RuntimeError(f"smoke test on {url} returned {r.status_code}: {r.text[:300]}")
                else:
                    out.append(f"{(time.perf_counter() - t0) * 1000:.0f} ms")
                try:
                    m = (await client.get(f"{url}/v1/models", headers=headers)).json()
                    entry = next((item for item in m["data"] if item.get("id") == model), {})
                    mml = entry.get("max_model_len")
                    if mml:
                        job.payload.setdefault("models", {}).setdefault(model, {})["max_model_len"] = mml
                        if not rev.draft.secondary:
                            job.payload["max_model_len"] = mml
                except (httpx.HTTPError, ValueError, KeyError, IndexError):
                    pass
        return "test completion ok (" + ", ".join(out) + ")"

    async def _route(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        for alias, backends in self.plan.routes.items():
            model = self.plan.route_models.get(alias, self.plan.served_model_name)
            self.gateway.set_route(alias, backends, model, rev.revision_id,
                                   max_model_len=(job.payload.get("models") or {}).get(model, {}).get(
                                       "max_model_len", job.payload.get("max_model_len")))
        # Aliases the previous deployment had but this one does not use: reopen them (so they do
        # not stay "switching" forever) and forget idle ones, so clients get a clean 404.
        stale = [a for a in self.drained if a not in self.plan.routes]
        for a in stale:
            self.gateway.cancel_drain(a)
            st = self.gateway.routes.get(a)
            if st is not None and not st.backends and st.inflight == 0:
                self.gateway.routes.pop(a, None)
        self.drained = []
        return "routing " + ", ".join(self.plan.routes)

    async def _healthy(self, job: Job, rev: ProfileRevision, step: JobStep) -> str:
        return f"{rev.profile_name} {rev.label} serving as '{rev.draft.simple.api_alias}'"
