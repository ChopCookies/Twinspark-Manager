"""Central Controller service (spec §4.1): desired state, jobs, profile lifecycle.

Owns the profile store, one activation at a time, stand-alone weight staging,
the post-activation watchdog and the metrics sampler. Diagnostics, pinning and
model-file operations live in sibling modules and are wired in here.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import replace
from typing import Any, Optional

from ..gateway.gateway import Gateway
from ..metrics import (
    HISTORY_KEYS,
    NODE_KEYS,
    MetricsHistory,
    MetricsSampler,
    combine_snapshots,
    scrape_metrics,
    windowed,
)
from ..schemas.config import ControllerConfig, GatewaySettings
from ..schemas.enums import JobState
from ..schemas.job import AuditEntry, Job
from ..schemas.profile import _NAME_RE, ImmutableIdentity, Profile, ProfileDraft, ProfileRevision, _now
from . import halves as split_halves
from .agent_client import AgentActionError, AgentClient
from .jobs import ActivationCoordinator
from .launch import LaunchPlan, LaunchPlanner
from .measurements import Measurements, resolve_kv
from .planner import MemoryPlanner, ModelSpec
from .state_machine import StageFailed
from .store import Store

log = logging.getLogger("twinspark.controller")
EVIDENCE_TIMEOUT_S = 150      # saving startup logs never holds up the cleanup longer than this


class DuplicateProfileError(ValueError):
    pass


class StaleViewError(RuntimeError):
    """The caller decided on a deployment that is no longer the active one (two clients raced)."""


_ANY = object()            # expected_active not given: no precondition
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,80}\Z")
JOURNAL_KEY = "inflight_activation"


class BusyError(RuntimeError):
    """Another activation/stop is running."""




def load_model_spec(store, repo: str, sha: Optional[str] = None) -> Optional[ModelSpec]:
    """The registered spec of ``repo`` at commit ``sha`` (else the latest registered one). A KV size
    stored on a spec by 0.6 is dropped: it belonged to one profile's settings, not to the model."""
    d = (store.kv_get(f"model_spec:{repo}@{sha}") if sha else None) or store.kv_get(f"model_spec:{repo}")
    if not d:
        return None
    fields = set(ModelSpec.__dataclass_fields__) - {"kv_bytes_per_token"}
    return ModelSpec(**{k: v for k, v in d.items() if k in fields})


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
        self.measurements = Measurements(self.store)      # fingerprinted KV sizes and startup calibrations
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
        self.current_scope: Optional[dict] = None    # the halves the running switch touches (None = all)
        self.sampler = MetricsSampler()
        self.half_samplers: dict[str, MetricsSampler] = {}   # split deployments: one per half (node)
        self.history = MetricsHistory(self.store)             # minute averages, 24 h, kept across restarts
        # the last raw scrape of every backend, for /metrics (controller.exporter) and windowed percentiles
        self.backend_samples: dict[str, dict] = {}
        self.telemetry: dict[str, dict] = {}
        self.telemetry_history: dict[str, list] = {}
        from .maintenance import Maintenance
        from .remote import RemoteService
        self.maintenance = Maintenance(self)
        self.remote = RemoteService(self)
        self.watch_state: dict[str, Any] = {"unhealthy_streak": 0, "recoveries": [], "last": None}
        self._apply_gateway_settings()
        self._audit("system", "controller.init", "controller", {})

    # ---- gateway routing settings (master plan §3.3) ------------------------------
    def gateway_settings(self) -> GatewaySettings:
        """controller.yaml's ``gateway:`` unless changed at runtime (GUI, `tsm gateway set`)."""
        saved = self.store.kv_get("gateway_settings")
        if saved:
            try:
                return GatewaySettings.model_validate(saved)
            except ValueError:
                log.warning("stored gateway settings are invalid; using controller.yaml's")
        return self.config.gateway

    def set_gateway_settings(self, settings: GatewaySettings) -> GatewaySettings:
        self.store.kv_set("gateway_settings", settings.model_dump(mode="json"))
        self._apply_gateway_settings()
        self._audit("user", "gateway.settings", "gateway", settings.model_dump(mode="json"))
        return settings

    def set_fallback_key(self, key: str) -> None:
        from ..security import SecretsVault
        SecretsVault(self.config.secrets_dir).set("fallback_api_key", key)
        self._apply_gateway_settings()
        self._audit("user", "gateway.fallback_key", "gateway", {"set": bool(key)})

    def _apply_gateway_settings(self) -> None:
        key = ""
        try:
            from ..security import SecretsVault
            key = SecretsVault(self.config.secrets_dir).get("fallback_api_key")
        except Exception:  # noqa: BLE001 - an unreadable vault only means no key for the external fallback
            log.debug("fallback key not readable", exc_info=True)
        self.gateway.configure(self.gateway_settings().model_dump(mode="json"), fallback_api_key=key)

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
            # merge: other observations (e.g. the startup timeline summary) live in the same record
            merged = {**(self.store.kv_get(f"observed:{revision_id}") or {}), **facts}
            self.store.kv_set(f"observed:{revision_id}", merged)

    # ---- model specs for the memory planner ------------------------------------
    def set_model_spec(self, repo: str, spec: ModelSpec, sha: Optional[str] = None) -> None:
        """Architecture and weight size of ``repo`` (at commit ``sha``). Never a KV measurement: those are
        kept per experiment (controller.measurements), not per repository."""
        doc = {**spec.__dict__, "kv_bytes_per_token": None}
        if sha:
            self.store.kv_set(f"model_spec:{repo}@{sha}", doc)
        self.store.kv_set(f"model_spec:{repo}", doc)          # the latest registered, for unpinned drafts

    def model_spec(self, repo: str, sha: Optional[str] = None) -> Optional[ModelSpec]:
        return load_model_spec(self.store, repo, sha)

    def profile_fit(self, name: str, ref: Optional[str] = None) -> dict[str, Any]:
        """Will this profile fit, and roughly how many KV tokens does it leave?

        Uses the registered ModelSpecs (real checkpoint sizes from the Hub for the model and its
        drafter, measured KV bytes/token from the recipe) and each node's last hardware facts. Every
        node gets its own plan with three verdicts (startup peak, one max-length request, the full
        requested concurrency). After a successful activation the observed KV pool from vLLM's boot
        log and the observed startup footprint calibrate the plan of the same settings.
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
        obs_rev = rev or p.latest()
        # an observation only calibrates the settings it was made with
        same = obs_rev is not None and (rev is not None or obs_rev.draft == d)
        observed = self.store.kv_get(f"observed:{obs_rev.revision_id}") if obs_rev is not None else None
        startup_obs = self.store.kv_get(f"observed_startup:{obs_rev.revision_id}") if same else None
        # the pinned identity the measurements are keyed by: a working draft that differs has none yet
        identity = rev.identity if rev else (d.identity if d.identity is not None else
                                             (obs_rev.identity if same and obs_rev is not None else None))
        if d.secondary:
            fits = {node: self._draft_fit(part, [node], identity=part.identity, legacy_startup=startup_obs)
                    for node, part in zip(("A", "B"), d.parts(), strict=True)}
            out = {"profile": name, "topology": "split", "nodes": fits,
                   "known": all(f["known"] for f in fits.values()),
                   "fits": all(f.get("fits", False) for f in fits.values())}
            if startup_obs:
                out["observed"] = {**(observed or {}), "startup": startup_obs}
            return out
        kv_tokens = ((observed or {}).get("kv") or {}).get("kv_cache_tokens") if same else None
        out = self._draft_fit(d, rev.required_nodes() if rev else None, identity=identity, legacy_startup=startup_obs,
                              legacy_kv_tokens=kv_tokens if isinstance(kv_tokens, int) else None)
        out["profile"] = name
        if obs_rev is not None:
            obs = dict(observed) if isinstance(observed, dict) else None
            if startup_obs:
                obs = {**(obs or {}), "startup": startup_obs}
            out["observed"] = obs
        return out

    def _draft_fit(self, d: ProfileDraft, nodes=None, *, identity=None, legacy_startup: Optional[dict] = None,
                   legacy_kv_tokens: Optional[int] = None) -> dict:
        """``identity`` keys the measurements (controller.measurements); ``legacy_*`` are the 0.6 per-revision
        observations, used only when no measurement of these exact settings exists yet."""
        from .planner import drafter_inputs
        s, a = d.simple, d.advanced
        out = {"model": s.model, "known": False}
        spec = self.model_spec(s.model, identity.model_revision if identity is not None else None)
        if spec is None:
            out["note"] = "model size unknown — pin the profile (or Resolve the model) first"
            return out
        if nodes is None:
            t = s.topology.value
            nodes = ["A"] if t == "single-a" else ["B"] if t == "single-b" else ["A", "B"]
        kv = {n: resolve_kv(self.measurements, d, identity, n) for n in nodes}
        cal = {n: self.measurements.startup(d, identity, n) for n in nodes}
        spec = replace(spec, kv_bytes_per_token=kv[nodes[0]][0])
        ctx, conc = s.planning_context(), a.max_num_seqs or s.concurrency
        budget = self.planner.estimate(spec=spec, quant=s.quantization, context_length=ctx,
                                       concurrency=conc, topology=s.topology,
                                       kv_dtype=a.kv_dtype, using_cuda_graphs=not a.eager_mode,
                                       mem_total_gib=self._mem_total(nodes))
        util = a.gpu_memory_utilization or self.planner.gpu_memory_utilization(budget)
        drafters = drafter_inputs(a, self.model_spec)
        backend = "ray" if str(getattr(d.distributed_backend, "value", d.distributed_backend)) == "ray" else "native"
        plans = {}
        for n in nodes:
            facts = self.store.kv_get(f"hardware:{n}") or {}
            if cal[n]:
                observed, kv_tokens = {"nodes": {n: cal[n]}, "at": cal[n]["at"]}, cal[n].get("kv_tokens")
            else:
                observed, kv_tokens = legacy_startup, legacy_kv_tokens
            plans[n] = self.planner.plan_node(
                spec=replace(spec, kv_bytes_per_token=kv[n][0]), kv_source=(kv[n][1], kv[n][2]),
                quant=s.quantization, context_length=ctx, concurrency=conc, topology=s.topology,
                node_id=n, backend=backend, kv_dtype=a.kv_dtype, using_cuda_graphs=not a.eager_mode,
                gpu_memory_utilization=util, facts=facts if isinstance(facts, dict) else {}, drafters=drafters,
                max_num_batched_tokens=a.max_num_batched_tokens, chunked_prefill=a.chunked_prefill,
                observed=observed, observed_kv_tokens=kv_tokens)
        # the 0.5 keys, now from the per-node plans: the worst node decides
        startup = min((p.startup for p in plans.values()), key=lambda v: v["margin_gib"])
        one = [p.one_context for p in plans.values()]
        full = [p.full_concurrency for p in plans.values()]
        kv_space = min(p.one_context["kv_space_gib"] for p in plans.values())
        tokens = [f["est_kv_tokens"] for f in full if f.get("est_kv_tokens") is not None]
        seqs = [f["est_full_length_seqs"] for f in full if f.get("est_full_length_seqs") is not None]
        measured = sum(p.confidence["measured_inputs"] for p in plans.values())
        inputs = sum(p.confidence["inputs"] for p in plans.values())

        def worst(items: list[dict]) -> dict:
            known = [v for v in items if v.get("margin_gib") is not None]
            return min(known, key=lambda v: v["margin_gib"]) if known else items[0]
        first = next(iter(plans.values()))
        out.update({
            "known": True,
            # starts = the startup peak fits on every node and vLLM's claim holds one max-length sequence
            "fits": all(p.startup["ok"] and p.one_context["ok"] is not False for p in plans.values()),
            "full_concurrency_fits": all(f["ok"] is not False for f in full),
            # headroom = the smallest startup margin of any node (the risk the 0.5 single verdict hid)
            "headroom_gib": startup["margin_gib"], "level": startup["level"],
            "gpu_memory_utilization": util,
            "weights_gib_per_node": round(first.bucket("main_weights").gib, 2),
            "drafter_gib_per_node": max((getattr(p.bucket("drafter_weights"), "gib", None) or 0.0)
                                        for p in plans.values()),
            "kv_pool_gib_per_node": round(max(0.0, kv_space), 2),
            "kv_needed_gib_per_node": first.full_concurrency["needed_gib"],
            "est_kv_tokens": min(tokens) if tokens else None,
            "est_full_context_seqs": min(seqs) if seqs else None,
            "context_length": ctx, "concurrency": conc,
            "kv_bytes_per_token_source": "measured" if spec.kv_bytes_per_token else "formula",
            "kv_source": {n: {"source": v[1], "note": v[2], "bytes_per_token": v[0]} for n, v in kv.items()},
            # which measurements back this forecast (shown on the plan): none, or N starts of these exact settings
            "calibration": {n: ({k: c[k] for k in ("samples", "worst_gib", "median_gib", "spread_gib", "margin_gib",
                                                   "at")} if c else None) for n, c in cal.items()},
            "fingerprinted": identity is not None,
            "budget": budget.as_dict(),
            "plan": {n: p.as_dict() for n, p in plans.items()},
            "verdicts": {
                "startup": {**{k: startup[k] for k in ("ok", "level", "margin_gib", "source")},
                            "node": next(n for n, p in plans.items() if p.startup is startup)},
                "one_context": {k: worst(one).get(k) for k in ("ok", "level", "margin_gib", "source")},
                "full_concurrency": {**{k: worst(full).get(k) for k in ("ok", "level", "margin_gib", "source")},
                                     "est_full_length_seqs": min(seqs) if seqs else None},
            },
            "confidence": f"measured inputs: {measured}/{inputs}",
            "drafters": [x.repo for x in drafters],
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
            "halves": self.halves_status(),
            "gateway": {k: self.gateway.settings.get(k) for k in ("balance", "affinity", "weights", "fallbacks")},
            # what the running switch touches: a client measuring another half's alias is not disturbed
            "switch": None if not (self.busy() and self.current_job) else {
                "job": self.current_job, "nodes": (self.current_scope or {}).get("nodes"),
                "aliases": (self.current_scope or {}).get("aliases")},
            "drain": {"policy": self.config.runtime.drain_policy, "timeout_s": self.config.runtime.drain_timeout_s,
                      "wait_max_s": self.config.runtime.drain_wait_max_s},
            "metrics": self.sampler.latest(),
            "watchdog": {"enabled": self.config.watchdog.enabled,
                         "unhealthy_streak": self.watch_state["unhealthy_streak"],
                         "last_incident": self.store.kv_get("last_incident")},
        }

    # ---- activation ---------------------------------------------------------------
    async def activate(self, profile_name: str, ref: str = "latest", *,
                       request_id: Optional[str] = None, expected_active: Any = _ANY,
                       drain: Optional[dict] = None, nodes: Optional[list[str]] = None,
                       _rollback_of: Optional[str] = None, _recovery: bool = False,
                       _maintenance: bool = False) -> Job:
        """Validate synchronously, then run the activation in the background.

        ``request_id`` makes the request idempotent: the same id returns the job it already started
        (a client that lost its connection mid-request can safely send it again). ``expected_active``
        is the revision id the caller saw as active (None = nothing active); when the deployment changed
        since, the request is refused instead of switching on a stale view (two clients racing).
        ``drain`` is ``{"policy": "abort"|"wait"|"force", "timeout_s": n}`` for requests still running when
        the switch starts (default: ``runtime.drain_policy``). Switches the controller makes itself (a
        rollback, a recovery, maintenance) force: the model they replace has already failed or must go.
        ``nodes`` restarts only those halves of the active split revision; without it a switch between
        revisions of a split profile touches only the halves that changed (controller.halves).
        """
        if drain is not None:
            if not isinstance(drain, dict) or drain.get("policy") not in (None, "abort", "wait", "force"):
                raise ValueError("drain policy must be abort, wait or force")
            t = drain.get("timeout_s")
            if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t <= 86400):
                raise ValueError("drain timeout_s must be between 0 and 86400 seconds")
        if request_id is not None:
            if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
                raise ValueError("request_id must be 8-80 characters: letters, digits and . _ : -")
            known = self.store.kv_get(f"activation_request:{request_id}")
            if known:
                job = self.store.load_job(known.get("job_id", ""))
                if job is not None:
                    if known.get("profile") != profile_name:
                        raise ValueError(f"request_id {request_id} was already used for another profile")
                    return job
        if expected_active is not _ANY:
            now = (self.active() or {}).get("revision_id")
            if (expected_active or None) != now:
                raise StaleViewError(f"the deployment changed since you looked: "
                                     f"{now or 'nothing'} is active now (you saw {expected_active or 'nothing'}) "
                                     f"— reload and decide again")
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
        scope = split_halves.decide_scope(active=self.active(), active_rev=self.active_revision(), rev=rev,
                                          nodes=nodes, serving=self._half_serving)
        kind = "rollback" if _rollback_of else ("recovery" if _recovery else "activation")
        job = Job(
            job_id=f"act-{secrets.token_hex(5)}", kind=kind,
            profile_revision=rev.revision_id,
            payload={"profile": profile_name, "revision_id": rev.revision_id,
                     "label": rev.label, "alias": rev.draft.simple.api_alias,
                     "rollback_of": _rollback_of, "scope": scope,
                     "drain": self._drain_settings(drain, controller_made=bool(_rollback_of or _recovery
                                                                                   or _maintenance))},
        )
        self.store.save_job(job)
        await self._lock.acquire()           # taken now so a second request gets 409
        self.current_job = job.job_id
        self.busy_profile = profile_name
        self.current_scope = scope
        if request_id is not None:
            self.store.kv_set(f"activation_request:{request_id}", {"job_id": job.job_id, "profile": profile_name,
                                                                   "at": time.time()})
        # the journal: what a controller restart in the middle of this switch has to clean up
        self.store.kv_set(JOURNAL_KEY, {"job_id": job.job_id, "profile": profile_name,
                                        "revision_id": rev.revision_id, "previous": self.active(),
                                        "rollback_of": _rollback_of, "scope": scope, "accepted_at": time.time()})
        self._spawn(self._run_activation(job, rev))
        self._audit("controller" if (_rollback_of or _recovery) else "user", "profile.activate",
                    f"profile/{profile_name}", {"revision": rev.revision_id, "job": job.job_id,
                                                "kind": kind, "nodes": scope["nodes"] if scope else None})
        return job

    def _half_serving(self, node: str) -> bool:
        """Every alias of the active revision's half on ``node`` has a route that is up."""
        aliases = split_halves.half_aliases(self.active_revision(), [node])
        return bool(aliases) and all(
            (st := self.gateway.routes.get(a)) is not None and st.backends and not st.down_reason
            and not st.draining for a in aliases)

    def serving_plan(self, rev: ProfileRevision) -> LaunchPlan:
        """The launch plan of ``rev`` as it really runs: halves kept from an earlier revision under their
        container names, halves that are down left out."""
        return split_halves.serving_plan(self.launch_plan(rev), rev, self.active())

    def _drain_settings(self, drain: Optional[dict], *, controller_made: bool) -> dict:
        rt = self.config.runtime
        if controller_made:
            return {"policy": "force", "timeout_s": rt.drain_timeout_s}
        drain = drain or {}
        timeout = drain.get("timeout_s")
        return {"policy": drain.get("policy") or rt.drain_policy,
                "timeout_s": rt.drain_timeout_s if timeout is None else timeout}

    def cancel_job(self, job_id: str) -> dict:
        job = self.store.load_job(job_id)
        if job is None:
            raise ValueError("job not found")
        if job.state not in (JobState.RUNNING, JobState.PENDING):
            return {"cancelled": False, "note": f"job is {job.state.value}"}
        if any(s.status == "failed" for s in job.steps):
            return {"cancelled": False, "note": "the start already failed: its logs are being saved and the "
                                                "partial containers removed, which cannot be cancelled"}
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
        if job.state in (JobState.FAILED, JobState.CANCELLED):
            from .phases import settle
            settle(job, job.error)
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
        scope = job.payload.get("scope") or None
        rollback_target: Optional[tuple[str, str]] = None
        cancel_check = lambda: job.job_id in self._cancel   # noqa: E731
        try:
            coord = ActivationCoordinator(
                config=self.config, agents=self.agents, gateway=self.gateway,
                planner=self.planner, persist=self._persist,
                model_spec=self.model_spec(rev.identity.model_repo, rev.identity.model_revision),
                model_specs={part.identity.model_repo: self.model_spec(part.identity.model_repo,
                                                                       part.identity.model_revision)
                             for part in rev.parts()},
                poll_interval=self.poll_interval, cancel_check=cancel_check,
                staging_wait=self._staging_wait, store=self.store,
            )
            job.payload["_revision"] = rev
            job.payload["mem_total_gib"] = self._mem_total()
            job.payload["node_mem_total"] = {n: self._mem_total([n]) for n in self.agents}
            try:
                await ActivationStateMachine(job, coord.handle, self._persist,
                                             cancel_check=cancel_check).run()
                job.state = JobState.COMPLETED
                if scope and previous:
                    # the kept half is still the container an earlier revision started
                    self.store.kv_set("active", split_halves.merged_record(previous, rev, scope, time.time()))
                else:
                    self.store.kv_set("active", {"profile": rev.profile_name,
                                                 "revision_id": rev.revision_id, "label": rev.label,
                                                 "alias": rev.draft.simple.api_alias,
                                                 "aliases": rev.draft.aliases,
                                                 "since": time.time()})
                models = job.payload.get("models")
                if scope and previous:
                    # what the kept half's start observed, under this revision too
                    kept_from = (previous.get("half_revisions") or {}).get(scope["kept"][0], previous["revision_id"])
                    models = {**((self.store.kv_get(f"observed:{kept_from}") or {}).get("models") or {}),
                              **(models or {})}
                self._mark_known_good(rev.profile_name, rev.revision_id, {
                    "kv": job.payload.get("kv"), "max_model_len": job.payload.get("max_model_len"),
                    "models": models,
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
                    # Clients stop following a job once it is FAILED, so it stays RUNNING until the logs are
                    # saved, the partial containers are gone and the rollback is decided.
                    job.state = JobState.RUNNING
                    job.rollback = "saving the startup logs of every rank, then stopping the partial containers"
                    self._persist(job)
                    try:
                        # keep the logs and state of every started container before cleanup removes them;
                        # best effort and bounded: nothing here may keep the cleanup from running
                        await asyncio.wait_for(self._capture_evidence(job, rev), EVIDENCE_TIMEOUT_S)
                    except Exception as cap_exc:  # noqa: BLE001 - includes the timeout
                        log.exception("startup evidence could not be saved")
                        why = (f"took longer than {EVIDENCE_TIMEOUT_S} s" if isinstance(cap_exc, asyncio.TimeoutError)
                               else f"{type(cap_exc).__name__}: {cap_exc}")
                        job.payload.setdefault("evidence_error", f"startup logs not saved: {why}"[:300])
                    if scope and previous:
                        # only this switch's halves are cleaned up: the kept half goes on serving
                        await self._stop_everything(coord.drained, nodes=scope["nodes"])
                        self.store.kv_set("active", split_halves.down_record(previous, scope["nodes"],
                                                                             job.error or str(exc)))
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
                    job.state = JobState.FAILED
        except Exception as exc:  # noqa: BLE001 - bug in the controller itself
            log.exception("activation crashed")
            job.state, job.error = JobState.FAILED, f"internal error: {exc}"
        finally:
            self._persist(job)
            job.payload.pop("_revision", None)
            self.store.save_job(job)
            if (self.store.kv_get(JOURNAL_KEY) or {}).get("job_id") == job.job_id:
                self.store.kv_set(JOURNAL_KEY, None)
            self._cancel.discard(job.job_id)
            self.current_job = None
            self.busy_profile = None
            self.current_scope = None
            self._lock.release()
        if rollback_target:
            try:
                await self.activate(*rollback_target, _rollback_of=job.job_id)
            except Exception:  # noqa: BLE001
                log.exception("automatic rollback could not start")

    def _redactor(self):
        from ..remote.bundle import Redactor
        from ..security import SECRET_SLOTS, SecretsVault
        values = []
        try:
            vault = SecretsVault(self.config.secrets_dir)
            for slot in SECRET_SLOTS:
                try:
                    if v := vault.get(slot):
                        values.append(v)
                except Exception:  # noqa: BLE001 - an unreadable slot is not added
                    continue
        except Exception:  # noqa: BLE001
            pass
        return Redactor(values)

    async def _capture_evidence(self, job: Job, rev: ProfileRevision) -> None:
        """Save each started container's state and log (both ranks) before the containers are removed,
        each node's startup memory timeline, and what the evidence says the failure was."""
        from . import evidence as ev
        from . import timeline as tl
        from .state_machine import guidance_for
        seen, started = set(), []
        for item in job.payload.get("started_containers") or []:
            key = (item.get("node"), item.get("name"))
            if key not in seen and item.get("node") in self.agents:
                seen.add(key)
                started.append(item)
        recorded = (job.payload.get("startup_timeline") or {}).get("nodes") or []
        tl_nodes = [n for n in recorded if n in self.agents]
        if not started and not tl_nodes:
            return
        root = ev.evidence_dir(self.config.db_path)
        redact = self._redactor()

        async def timeline() -> dict[str, dict]:
            if not tl_nodes:
                return {}
            try:
                return tl.stored(await tl.collect(self.agents, job.job_id, tl_nodes, tl.EVIDENCE_POINTS, stop=True))
            except Exception as exc:  # noqa: BLE001 - the timeline is best effort like the rest
                log.exception("startup timeline could not be read")
                return {n: {"error": f"{type(exc).__name__}: {exc}"[:300]} for n in tl_nodes}

        async def one(item: dict) -> dict[str, Any]:
            node, name = item["node"], item["name"]
            rec: dict[str, Any] = {"node": node, "name": name}
            try:
                raw = await self.agents[node].call("container_evidence", name=name, timeout=120)
            except Exception as exc:  # noqa: BLE001 - evidence is best effort, never blocks cleanup
                rec["error"] = str(exc)[:300]
                return rec
            kernel = raw.get("kernel") or {}
            rec.update({k: raw.get(k) for k in ("status", "exit_code", "oom_killed", "started_at",
                                                "finished_at", "role")})
            rec.update(memory=raw.get("memory"), kernel_gpu_mem_errors=kernel.get("count"),
                       kernel_count_at_least=bool(kernel.get("at_least")),
                       kernel_note=kernel.get("note"), log_truncated=bool(raw.get("log_truncated")))

            def digest() -> dict[str, Any]:
                # megabytes of text: redact, summarise and write it off the event loop
                log = redact(raw.get("log") or "")
                out: dict[str, Any] = {"kernel_samples": [redact(str(x)) for x in kernel.get("samples") or []]}
                out["log_truncated"] = rec["log_truncated"] or len(log) >= ev.MAX_LOG_CHARS
                out.update(ev.error_summary(log))
                try:
                    out["file"], out["bytes"] = ev.save(root, job.job_id, node, name, log)
                except (OSError, ValueError) as exc:
                    out["save_error"] = str(exc)[:200]
                return out

            rec.update(await asyncio.to_thread(digest))
            return rec

        gathered, timelines = await asyncio.gather(asyncio.gather(*(one(i) for i in started)), timeline())
        records = list(gathered)
        summaries = {n: d["summary"] for n, d in timelines.items() if isinstance(d.get("summary"), dict)}
        job.payload["startup_evidence"] = {
            "containers": records, "profile": rev.profile_name, "revision_id": rev.revision_id,
            "images": list(dict.fromkeys(p.identity.image_ref for p in rev.parts() if p.identity)),
            "captured_at": time.time(),
            "timeline": timelines,
            "classification": ev.classify(records, summaries, job),
            "limits": {"max_log_chars": ev.MAX_LOG_CHARS, "kept_jobs": ev.KEEP_JOBS,
                       "timeline_points_per_node": tl.EVIDENCE_POINTS}}
        failed = next((st for st in reversed(job.steps) if st.status == "failed"), None)
        text = ev.excerpt(records)
        if failed is not None and text:
            failed.log_excerpt = text
        note = ev.gpu_memory_note(records)
        job.guidance = list(dict.fromkeys(([note] if note else []) + guidance_for(
            (job.error or "") + " " + text) + (job.guidance or [])))
        self._persist(job)
        try:
            await asyncio.to_thread(ev.prune, root)
        except OSError:
            log.exception("could not prune old startup evidence")

    async def _stop_everything(self, aliases: list[str], failed_nodes: Optional[list[str]] = None,
                               nodes: Optional[list[str]] = None) -> list[str]:
        """Stop TwinSpark's containers and close their routes. With ``nodes`` only those nodes (the halves a
        failed switch touched) and only ``aliases``: the other half of a split goes on serving."""
        stopped: list[str] = []
        for n, client in self.agents.items():
            if nodes is not None and n not in nodes:
                continue
            try:
                stopped += (await client.call("containers_stop_owned", timeout=180))["stopped"]
            except Exception:  # noqa: BLE001
                log.exception("stop on node %s failed", n)
                if failed_nodes is not None:
                    failed_nodes.append(n)
        for a in set(aliases) | (set() if nodes is not None else set(self.gateway.routes)):
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
        journal = self.store.kv_get(JOURNAL_KEY)
        interrupted: Optional[Job] = None
        for j in self.store.list_jobs(limit=50):
            if j.state in (JobState.RUNNING, JobState.PENDING):
                j.state = JobState.FAILED
                j.error = (f"{j.error} (cleanup interrupted by controller restart)" if j.error
                           else "interrupted by controller restart")
                from .phases import settle
                settle(j, "interrupted by controller restart")
                self.store.save_job(j)
                if journal and j.job_id == journal.get("job_id"):
                    interrupted = j
        await self.refresh_hardware()
        if self.maintenance.blocking():
            self.maintenance.ensure_running()
            return None
        if journal:
            return await self._recover_interrupted(journal, interrupted)
        return await self._resume_active()

    async def _recover_interrupted(self, journal: dict, job: Optional[Job]) -> Optional[Job]:
        """The controller stopped in the middle of an accepted switch (crash, reboot, power cut).

        Deterministic: if the old model had not been touched yet, nothing changed and the usual
        start-up path (re-attach what still runs) follows. If the switch had begun stopping the old
        model, the partial containers are stopped and — as an automatic rollback would have done — the
        previous deployment is started again; without auto_rollback nothing is started.
        """
        from .state_machine import DESTRUCTIVE_FROM, STAGE_ORDER
        order = {st.value: i for i, st in enumerate(STAGE_ORDER)}
        destructive = job is not None and any(order.get(st.stage, -1) >= order[DESTRUCTIVE_FROM.value]
                                              for st in job.steps)
        if job is None or not destructive:
            self.store.kv_set(JOURNAL_KEY, None)
            if job is not None:
                job.rollback = "the controller restarted before the old model was touched; it keeps serving"
                self.store.save_job(job)
            return await self._resume_active()
        # the single writer: while the agents come back and the partial containers are stopped, the API is
        # already answering, and a switch started meanwhile would be torn down here — refuse it (409)
        async with self._lock:
            self.current_job, self.busy_profile = job.job_id, journal.get("profile")
            try:
                down = await self._wait_for_agents(self.config.startup_wait_s)
                scope = journal.get("scope") or None
                previous_rec = journal.get("previous") or None
                if scope and previous_rec:
                    # a switch of one half: only that half was being replaced, the other one kept serving
                    stopped = await self._stop_everything(list(scope.get("aliases") or []), nodes=scope["nodes"])
                    self.store.kv_set("active", split_halves.down_record(
                        previous_rec, scope["nodes"], "the controller restarted during the switch of this half"))
                else:
                    stopped = await self._stop_everything([])
                    self.store.kv_set("active", None)
                # cleared only now: a restart during the wait or the stop runs this recovery again
                self.store.kv_set(JOURNAL_KEY, None)
            finally:
                self.current_job, self.busy_profile = None, None
        if journal.get("scope") and journal.get("previous"):
            await self._adopt_running(self.active())      # the kept half: its container kept running
        previous = journal.get("previous") or None
        target = journal.get("revision_id")
        restore = bool(self.config.auto_rollback and previous and not journal.get("rollback_of")
                       and previous.get("revision_id") != target)
        job.rollback = (f"the controller restarted during the switch: stopped {len(stopped)} partial "
                        f"container(s)" + (f" (node(s) {', '.join(down)} did not answer)" if down else "")
                        + (f"; restoring previous {previous['profile']} {previous.get('label', '')}".rstrip()
                           if restore else "; nothing to restore"))
        self.store.save_job(job)
        self._audit("controller", "deployment.recover", f"job/{job.job_id}",
                    {"stopped": stopped, "restore": previous if restore else None})
        if not restore or self.busy():
            return None
        try:
            return await self.activate(previous["profile"], previous["revision_id"], _rollback_of=job.job_id)
        except Exception:  # noqa: BLE001
            log.exception("restoring %s after an interrupted switch failed", previous)
            return None

    async def _resume_active(self) -> Optional[Job]:
        """Start-up without an interrupted switch: re-attach what still runs, autostart otherwise."""
        active = self.active()
        if not active:
            return None
        await self._wait_for_agents(self.config.startup_wait_s)
        if self.busy() or self.active() != active:
            return None
        missing: list[str] = []
        if await self._adopt_running(active, missing=missing):
            if not missing or not self.config.autostart or self.busy():
                return None
            # one half of a split came back, the other did not: restart only that one
            try:
                return await self.activate(active["profile"], active["revision_id"], nodes=missing, _recovery=True)
            except Exception:  # noqa: BLE001
                log.exception("restarting node(s) %s of %s failed", missing, active)
                return None
        if self.busy() or self.active() != active:
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

    async def _adopt_running(self, active: dict, missing: Optional[list[str]] = None) -> bool:
        """Controller restarted but the containers kept running: re-attach routes
        instead of restarting a healthy model.

        A split deployment is adopted half by half: a half that still runs gets its routes back even when
        the other one is gone; the nodes of the halves that are not healthy are added to ``missing``."""
        p = self.get_profile(active["profile"])
        rev = p.get_revision(active["revision_id"]) if p else None
        if rev is None:
            return False
        bad: set[str] = set()
        try:
            plan = split_halves.serving_plan(self.launch_plan(rev), rev, active)
            for c in plan.containers:
                try:
                    r = await self.agents[c.node].call("health_probe", name=c.name, url=c.health_url)
                except Exception:  # noqa: BLE001 - that node is away: for a split, only its half
                    r = {}
                if not r.get("healthy"):
                    bad.add(c.node)
        except Exception:  # noqa: BLE001
            return False
        if bad:
            if not split_halves.is_split(rev) or bad >= set(plan.nodes):
                return False
            plan = split_halves.scoped_plan(plan, [n for n in plan.nodes if n not in bad])
            if missing is not None:
                missing.extend(sorted(bad))
        # Startup probes run while the GUI is available. Respect a stop, switch
        # or maintenance request that arrived while waiting for node health.
        if self.busy() or self.active() != active:
            return False
        observed = self.store.kv_get(f"observed:{rev.revision_id}") or {}
        started = active.get("half_revisions") or {}
        node_of = {c.health_url: c.node for c in plan.containers if c.health_url}
        for alias, backends in plan.routes.items():
            model = plan.route_models.get(alias, plan.served_model_name)
            # a kept half's route names the revision that started its container
            served_by = started.get(next((node_of[b] for b in backends if b in node_of), ""), rev.revision_id)
            self.gateway.set_route(alias, backends, model, served_by,
                                   max_model_len=(observed.get("models") or {}).get(model, {}).get(
                                       "max_model_len", observed.get("max_model_len")), nodes=node_of)
        self._audit("controller", "deployment.adopt", f"profile/{rev.profile_name}",
                    {"halves_missing": sorted(bad)} if bad else {})
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
        plan = self.serving_plan(rev)          # a half that is down (its switch failed) is not watched
        if not plan.containers:
            return None
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
                    "problem": problem, "action": "none", "nodes": sorted({c.node for c in bad})}
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
            # a split deployment restarts only the half that failed; the other one keeps serving
            failed = sorted({c.node for c in bad})
            half = (failed if split_halves.is_split(rev) and bad and None not in urls
                    and len(failed) < len(split_halves.HALF_NODES) else None)
            try:
                job = await self.activate(rev.profile_name, rev.revision_id, nodes=half, _recovery=True)
                incident["action"] = (f"restarting node {', '.join(half)}'s half ({job.job_id})" if half
                                      else f"restarting ({job.job_id})")
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
        nodes = {f"{n}.{k}": (t.get("gpu") or {}).get("power_w") if k == "gpu_power_w" else t.get(k)
                 for n, t in self.telemetry.items() if isinstance(t, dict) and not t.get("stale") for k in NODE_KEYS}
        act = self.active()
        # while one half of a split switches, the other half goes on being measured
        switching = set((self.current_scope or {}).get("nodes") or []) if self.busy() else set()
        if not act or (self.busy() and not switching):
            self.history.add(nodes)
            return None
        groups = [g for g in self._metric_groups(act) if g["key"] not in switching]
        if switching:
            for g in groups:
                if g["key"] != "all":
                    await self._scrape_group(g, act)
            self.history.add(nodes)
            return None
        every: list[dict] = []
        all_pairs: list[tuple] = []
        seen: set[str] = set()
        result: Optional[dict] = None
        for g in groups:
            snaps, pairs, combined = await self._scrape_group(g, act)
            seen.update(g["backends"])
            all_pairs += pairs
            every += snaps
            if g["key"] == "all":
                result = self.sampler.add(combined, act.get("revision_id"))
        for b in [b for b in self.backend_samples if b not in seen]:
            self.backend_samples.pop(b, None)
        if not any(g["key"] == "all" for g in groups):
            for node in [n for n in self.half_samplers if n not in {g["key"] for g in groups}]:
                self.half_samplers.pop(node, None)
            if every:
                combined = combine_snapshots(every)
                if combined.get("ok"):
                    combined.update(windowed(all_pairs))
                result = self.sampler.add(combined, act.get("revision_id"))
        else:
            self.half_samplers.clear()
        point = self.sampler.latest() if result is not None else None
        self.history.add({**nodes, **({k: point.get(k) for k in HISTORY_KEYS} if point else {})})
        return result

    async def _scrape_group(self, g: dict, act: dict) -> tuple[list[dict], list[tuple], dict]:
        """Scrape one group's backends; keeps their raw samples for /metrics. A split half's numbers go to
        its own sampler (its own model, alias and revision, so its own history)."""
        snaps, pairs = [], []
        for b, labels in g["backends"].items():
            snap = await scrape_metrics(b, headers=self.gateway.backend_headers(), raw=True)
            samples = snap.pop("samples", None)
            if samples is not None:
                prev = self.backend_samples.get(b)
                # a window only spans scrapes of the same model: a switch starts a new one
                pairs.append((prev["samples"] if prev and prev["labels"] == labels else None, samples))
                self.backend_samples[b] = {"samples": samples, "ts": snap["ts"], "labels": labels}
            snaps.append(snap)
        combined = combine_snapshots(snaps)
        if combined.get("ok"):
            combined.update(windowed(pairs))
        if g["key"] != "all":
            self.half_samplers.setdefault(g["key"], MetricsSampler()).add(combined, g["revision"])
        return snaps, pairs, combined

    def _metric_groups(self, act: dict) -> list[dict]:
        """What to scrape: one group per split half (``key`` = its node), else one for the deployment
        (``key`` "all"). ``backends`` maps each backend URL to the labels its samples carry on /metrics."""
        rev = self.active_revision()
        node_of: dict[str, str] = {}
        if rev is not None:
            try:
                node_of = {c.health_url: c.node for c in self.serving_plan(rev).containers if c.health_url}
            except Exception:  # noqa: BLE001 - labels are best effort; the scrape itself must not fail
                node_of = {}

        def group(key: str, aliases: list[str], revision: Optional[str]) -> Optional[dict]:
            backends: dict[str, dict] = {}
            for alias in aliases:
                st = self.gateway.routes.get(alias)
                for b in (st.backends if st else []):
                    backends.setdefault(b, {"alias": alias, "node": node_of.get(b, ""), "model": st.served_model or "",
                                            "revision": st.revision_id or revision or ""})
            return {"key": key, "aliases": aliases, "revision": revision, "backends": backends} if backends else None

        parts = split_halves.halves(rev)
        if parts:
            started = act.get("half_revisions") or {}
            out = [group(n, part.draft.simple.aliases, started.get(n, act.get("revision_id")))
                   for n, part in parts.items()]
        else:
            out = [group("all", act.get("aliases") or [act.get("alias", "default")], act.get("revision_id"))]
        return [g for g in out if g]

    def halves_status(self) -> Optional[list[dict]]:
        """Each half of the active split deployment on its own: what runs, since when, and how it is."""
        act, rev = self.active(), self.active_revision()
        parts = split_halves.halves(rev)
        if not parts or not act:
            return None
        p = self.get_profile(rev.profile_name)
        started = act.get("half_revisions") or {}
        down = set(act.get("halves_down") or [])
        since = act.get("half_since") or {}
        switching = set((self.current_scope or {}).get("nodes") or (split_halves.HALF_NODES if self.busy() else []))
        out = []
        for node, part in parts.items():
            rid = started.get(node, rev.revision_id)
            started_rev = p.get_revision(rid) if p else None
            routes = [self.gateway.routes.get(a) for a in part.draft.simple.aliases]
            route_down = next((r.down_reason for r in routes if r is not None and r.down_reason), None)
            if node in down:
                state = "down"
            elif node in switching and self.busy():
                state = "switching"
            elif route_down:
                state = "unhealthy"
            elif all(r is not None and r.backends for r in routes):
                state = "serving"
            else:
                state = "starting"
            sampler = self.half_samplers.get(node)
            out.append({"node": node, "name": part.profile_name, "model": part.identity.model_repo,
                        "model_revision": part.identity.model_revision, "image": part.identity.image_ref,
                        "aliases": part.draft.simple.aliases, "revision_id": rid,
                        "label": started_rev.label if started_rev else None, "since": since.get(node, act.get("since")),
                        "state": state, "reason": (act.get("down_reasons") or {}).get(node) or route_down,
                        "metrics": sampler.latest() if sampler else None})
        return out

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
        self.history.flush()
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
