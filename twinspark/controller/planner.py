"""UMA-aware unified memory planner (spec §8, §29).

DGX Spark is a single unified-memory system, NOT a GPU with separate VRAM. The
planner works per node — two Sparks are never treated as one 256 GiB pool.

Three outputs matter:

* ``NodeBudget`` — the 0.5 estimate per node (kept for the launch path and old callers).
* ``gpu_memory_utilization`` — the value handed to vLLM. vLLM *pre-allocates*
  ``util * total`` for weights + activations + KV cache. On a UMA system that
  pool competes with the OS, Docker and this manager, so it must be derived from
  the non-vLLM reserve, never left at vLLM's default of 0.9.
* ``NodePlan`` (``plan_node``) — every bucket of one node with its source
  (measured / calibrated / estimated / unknown) and range, and three separate
  verdicts: startup peak, one max-length request, full requested concurrency.

docs/memory-planning.md explains the formulas and which numbers are measured.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Optional

from ..schemas.enums import DistributedBackend, Quantization, Topology

# Nominal size. The OS usually reports a little less; the planner prefers the
# MemTotal reported by the agent's hardware_facts when available.
NODE_MEM_TOTAL_GIB = 128.0
NODE_MEM_TOTAL_BYTES = int(NODE_MEM_TOTAL_GIB * 1024**3)
MAX_GPU_MEMORY_UTILIZATION = 0.88

_BYTES_PER_PARAM = {
    Quantization.BF16: 2.0,
    Quantization.FP8: 1.0,
    # 4-bit formats carry block scales; ~4.5 bits/param is closer to reality than 4.0
    Quantization.NVFP4: 0.5625,
    Quantization.MXFP4: 0.53,
    Quantization.INT4: 0.5625,
    Quantization.AWQ: 0.5625,
    Quantization.GPTQ: 0.5625,
    Quantization.AUTOROUND: 0.5625,
}
_KV_BYTES = {"auto": 2.0, "bf16": 2.0, "fp16": 2.0, "fp8": 1.0, "fp8_e4m3": 1.0,
             "fp8_e5m2": 1.0, "fp8_ds_mla": 1.0, "nvfp4": 0.5625, "nvfp4_ds_mla": 0.5625,
             "fp4": 0.5625}
_SHARDED = (Topology.TP2, Topology.PP2, Topology.TP_EP)


@dataclass
class ModelSpec:
    """Model characteristics needed for estimation."""

    num_params: int                    # total parameters (all experts for MoE)
    layers: int
    num_kv_heads: int
    head_dim: int
    weight_bytes: Optional[int] = None # exact checkpoint size (safetensors index) — preferred
    is_moe: bool = False
    num_experts: int = 0
    kv_lora_rank: int = 0              # MLA (DeepSeek-style): compressed KV
    qk_rope_head_dim: int = 0
    # Measured KV bytes per token per node *as served* (recipe / boot log). Hybrid
    # attention (DeepSeek V4 CSA/HCA, GLM-5.3 KDA+DSA, Qwen3.8 DeltaNet, MiMo SWA)
    # makes the textbook formula useless, so a measured number always wins.
    kv_bytes_per_token: Optional[float] = None
    num_attention_heads: int = 0
    # Sizes the first-prefill activation peak (0 = unknown: the 0.5 rule of thumb is used instead).
    hidden_size: int = 0
    # config.json's num_hidden_layers; ``layers`` carries the same number for the KV formula. Kept as
    # its own field so specs registered before 0.6 (which lack it) still load.
    num_hidden_layers: int = 0

    def __post_init__(self) -> None:
        if not self.num_hidden_layers:
            self.num_hidden_layers = self.layers

    @property
    def is_mla(self) -> bool:
        return self.kv_lora_rank > 0


@dataclass
class NodeBudget:
    """Per-node memory accounting in GiB (spec §8)."""

    node_id: str
    mem_total: float = NODE_MEM_TOTAL_GIB
    system_usage: float = 0.0          # OS + desktop/headless
    runtime: float = 0.0               # docker / containerd
    control_plane: float = 0.0         # TwinSpark agent/controller/gateway
    safety_reserve: float = 0.0
    model_weights: float = 0.0
    activations: float = 0.0           # loading peak / activation scratch
    cuda_graphs: float = 0.0
    distributed_backend: float = 0.0
    nccl: float = 0.0
    kv_cache: float = 0.0
    total: float = 0.0
    observed_total: Optional[float] = None

    @property
    def non_vllm(self) -> float:
        return self.system_usage + self.runtime + self.control_plane + self.safety_reserve

    @property
    def vllm_needed(self) -> float:
        return self.total - self.non_vllm

    def compute(self) -> "NodeBudget":
        self.total = (
            self.system_usage + self.runtime + self.control_plane + self.safety_reserve
            + self.model_weights + self.activations + self.cuda_graphs
            + self.distributed_backend + self.nccl + self.kv_cache
        )
        return self

    def headroom_gib(self) -> float:
        return self.mem_total - self.total

    def as_dict(self) -> dict:
        d = asdict(self)
        d["vllm_needed"] = round(self.vllm_needed, 2)
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}


def _kv_bytes_per_token(spec: ModelSpec, kv_elem_bytes: float) -> float:
    if spec.is_mla:
        # MLA caches one compressed latent (+ rope part) per layer, shared by all heads.
        return spec.layers * (spec.kv_lora_rank + spec.qk_rope_head_dim) * kv_elem_bytes
    return 2.0 * spec.layers * spec.num_kv_heads * spec.head_dim * kv_elem_bytes


class MemoryPlanner:
    """Produces Estimated / Observed / Calibrated memory plans per node."""

    def __init__(self, calibration_db: Optional[dict] = None):
        # {(repo, quant, topology): [observed totals GiB, ...]}
        self.calibration_db = calibration_db or {}
        # Returns the real MemTotal the nodes report (~121.7 GiB on GB10, not a nominal 128), so
        # the advisory endpoints agree with the check an activation applies. Set by the controller.
        self.mem_total_provider: Optional[Callable[[], Optional[float]]] = None

    def estimate(
        self,
        *,
        spec: ModelSpec,
        quant: Quantization,
        context_length: int,
        concurrency: int,
        topology: Topology,
        backend: DistributedBackend | str = DistributedBackend.NATIVE,
        node_id: str = "A",
        headless: bool = True,
        kv_dtype: Optional[str] = None,
        using_cuda_graphs: bool = True,
        mem_total_gib: Optional[float] = None,
        headless_system_usage_gib: Optional[float] = None,
    ) -> NodeBudget:
        backend = DistributedBackend(backend)
        shards = 2 if topology in _SHARDED else 1

        if spec.weight_bytes:
            weights_gib = spec.weight_bytes / 1024**3 / shards
        else:
            weights_gib = spec.num_params * _BYTES_PER_PARAM[quant] / 1024**3 / shards

        # KV cache: TP splits KV heads across nodes (unless there are fewer KV heads
        # than ranks, then they are replicated); PP splits layers; MLA latent is
        # replicated under TP.
        kv_elem = _KV_BYTES.get((kv_dtype or "auto").lower(), 2.0)
        if spec.kv_bytes_per_token:
            kv_gib = spec.kv_bytes_per_token * context_length * concurrency / 1024**3
        else:
            kv_total = _kv_bytes_per_token(spec, kv_elem) * context_length * concurrency / 1024**3
            if topology == Topology.PP2:
                kv_gib = kv_total / 2
            elif topology in (Topology.TP2, Topology.TP_EP) and not spec.is_mla and spec.num_kv_heads >= 2:
                kv_gib = kv_total / 2
            else:
                kv_gib = kv_total

        if shards == 1:
            backend_gib, nccl_gib = 0.0, 0.0
        elif backend == DistributedBackend.RAY:
            backend_gib, nccl_gib = 1.2, 0.5
        else:
            backend_gib, nccl_gib = 0.3, 0.5

        if mem_total_gib is None and self.mem_total_provider is not None:
            mem_total_gib = self.mem_total_provider()
        b = self.reserve_budget(node_id, headless, mem_total_gib, headless_system_usage_gib)
        return NodeBudget(
            node_id=node_id,
            mem_total=b.mem_total,
            system_usage=b.system_usage,
            runtime=b.runtime,
            control_plane=b.control_plane,
            safety_reserve=b.safety_reserve,
            model_weights=weights_gib,
            activations=max(1.0, weights_gib * 0.05) + 0.25 * min(concurrency, 16),
            cuda_graphs=1.2 if using_cuda_graphs else 0.0,
            distributed_backend=backend_gib,
            nccl=nccl_gib,
            kv_cache=kv_gib,
        ).compute()

    @staticmethod
    def reserve_budget(node_id: str = "A", headless: bool = True,
                       mem_total_gib: Optional[float] = None,
                       system_usage_gib: Optional[float] = None) -> NodeBudget:
        """Only the non-vLLM part of a node (OS, Docker, TwinSpark, safety reserve)."""
        system = system_usage_gib if system_usage_gib is not None else (6.0 if headless else 9.0)
        return NodeBudget(node_id=node_id, mem_total=mem_total_gib or NODE_MEM_TOTAL_GIB,
                          system_usage=system, runtime=0.4, control_plane=0.2,
                          safety_reserve=2.0).compute()

    # ---- the value handed to vLLM ---------------------------------------
    @staticmethod
    def gpu_memory_utilization(budget: NodeBudget) -> float:
        """Largest safe --gpu-memory-utilization for this node, rounded down."""
        allowed = (budget.mem_total - budget.non_vllm) / budget.mem_total
        return max(0.10, min(MAX_GPU_MEMORY_UTILIZATION, int(allowed * 100) / 100))

    def fits(self, budget: NodeBudget, util: Optional[float] = None) -> tuple[bool, float]:
        """(fits, headroom_gib). Checks both the node total and vLLM's own pool."""
        util = util if util is not None else self.gpu_memory_utilization(budget)
        pool_headroom = util * budget.mem_total - budget.vllm_needed
        headroom = min(budget.headroom_gib(), pool_headroom)
        return headroom >= 0, headroom

    @staticmethod
    def classify_headroom(headroom_gib: float) -> str:
        if headroom_gib >= 4.0:
            return "SAFE"
        if headroom_gib >= 1.5:
            return "LOW"
        return "CRITICAL"

    # ---- calibration -----------------------------------------------------
    @staticmethod
    def observed(budget: NodeBudget, observed_total_gib: float) -> NodeBudget:
        budget.observed_total = observed_total_gib
        return budget

    def calibrated_estimate(self, budget: NodeBudget, key: tuple) -> NodeBudget:
        history = self.calibration_db.get(key, [])
        if not history:
            return budget
        alpha = min(0.7, 0.1 + 0.1 * len(history))
        blended = (1 - alpha) * budget.total + alpha * statistics.mean(history)
        budget.system_usage += blended - budget.total
        return budget.compute()

    # ---- per-node plan with sources and three verdicts (0.6) -------------------------------
    def plan_node(
        self,
        *,
        spec: ModelSpec,
        quant: Quantization,
        context_length: int,
        concurrency: int,
        topology: Topology,
        node_id: str = "A",
        backend: DistributedBackend | str = DistributedBackend.NATIVE,
        kv_dtype: Optional[str] = None,
        using_cuda_graphs: bool = True,
        gpu_memory_utilization: Optional[float] = None,
        facts: Optional[dict] = None,
        drafters: Iterable["DrafterInput"] = (),
        max_num_batched_tokens: Optional[int] = None,
        chunked_prefill: Optional[bool] = None,
        observed: Optional[dict] = None,
        observed_kv_tokens: Optional[int] = None,
        mem_total_gib: Optional[float] = None,
        kv_source: Optional[tuple[str, str]] = None,
    ) -> "NodePlan":
        """Everything one node holds for this deployment, bucket by bucket, and three verdicts.

        ``facts`` are the node's last ``hardware_facts``; ``observed`` is the observed startup of these
        exact settings (``{"nodes": {node: calibration}}``, see controller.measurements) and
        ``observed_kv_tokens`` the KV pool vLLM reported. ``kv_source`` is ``(source, note)`` for a
        ``spec.kv_bytes_per_token`` that was resolved for this setup. See docs/memory-planning.md.
        """
        return _plan_node(self, spec=spec, quant=quant, context_length=context_length,
                          concurrency=concurrency, topology=topology, node_id=node_id,
                          backend=DistributedBackend(backend), kv_dtype=kv_dtype,
                          using_cuda_graphs=using_cuda_graphs, util=gpu_memory_utilization,
                          facts=facts or {}, drafters=list(drafters),
                          batched_tokens=max_num_batched_tokens, chunked_prefill=chunked_prefill,
                          observed=observed or {}, observed_kv_tokens=observed_kv_tokens,
                          mem_total_gib=mem_total_gib, kv_source=kv_source)


# ---- per-node plan ------------------------------------------------------------------------------
# Defaults are the 0.5 reserves, now shown as "estimated" so a measured reading visibly replaces them.
GB10_MEM_TOTAL_GIB = 121.6       # MemTotal DGX OS reports on a GB10 (the nominal 128 GB minus firmware carve-outs)
OS_DOCKER_GIB = 6.4              # OS, kernel and Docker on a headless node: 0.5's 6.0 system + 0.4 runtime
SERVICES_GIB = 0.2               # TwinSpark agent, controller, privd and terminal (0.5's control plane)
CUDA_GRAPHS_GIB = 1.2            # captured graphs; 0 in eager mode
COMM_GIB = {"native": 0.8, "ray": 1.7}   # NCCL buffers + distributed backend (0.5: 0.3/1.2 backend + 0.5 NCCL)
DEFAULT_BATCHED_TOKENS = 8192    # vLLM's default max_num_batched_tokens for online serving with chunked prefill
ACT_BUFFERS = 24                 # hidden-sized buffers alive per prefill token (see docs/memory-planning.md)
ACT_ELEM_BYTES = 2               # bf16/fp16 activations
LOADER_MIN_GIB = 0.5             # repacking holds the old and the new copy of one layer; at least this much
LOADER_FIXED_GIB = 1.0           # when the layer count is unknown
DRAFTER_SCRATCH_MIN_GIB = 0.25
DRAFTER_SCRATCH_FRACTION = 0.10  # of the drafter's weights, when its hidden size is unknown
SOURCES = ("measured", "calibrated", "estimated", "unknown")
# speculative methods that bring no separate drafter checkpoint (they use the target's own layers or none)
_NO_DRAFTER_METHODS = {"ngram", "suffix", "mtp", "deepseek_mtp", "qwen3_next_mtp", "glm4_moe_mtp", "ernie_mtp"}


@dataclass
class DrafterInput:
    """A separate drafter checkpoint the deployment loads next to the main model."""

    repo: str
    spec: Optional[ModelSpec] = None
    draft_tensor_parallel_size: Optional[int] = None
    method: Optional[str] = None


def drafter_inputs(advanced: Any, spec_lookup: Callable[[str], Optional[ModelSpec]]) -> list[DrafterInput]:
    """The separate drafter checkpoints a draft's advanced settings load, with their registered specs.

    ``advanced.extra_models`` (pinned "org/repo@sha" or plain "org/repo") plus a Hub repo named by
    ``speculative_config.model``. An extra model that ``speculative_config`` does not name is loaded by
    something else (raw flags, a mod): where it lives is unknown, so it is counted like a one-rank drafter.
    """
    import re as _re
    sc = getattr(advanced, "speculative_config", None)
    sc = sc if isinstance(sc, dict) else {}
    spec_model = sc.get("model") if isinstance(sc.get("model"), str) else None
    raw = sc.get("draft_tensor_parallel_size")
    dtp: Optional[int] = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        dtp = raw
    elif isinstance(raw, str) and raw.isdigit():
        dtp = int(raw)
    repos = [str(ref).partition("@")[0] for ref in getattr(advanced, "extra_models", None) or []]
    if spec_model and _re.match(r"^[^/\s]+/[^/\s]+$", spec_model) and spec_model not in repos:
        repos.append(spec_model)
    out = []
    for repo in dict.fromkeys(repos):
        if repo == spec_model:
            out.append(DrafterInput(repo, spec_lookup(repo), dtp, sc.get("method")))
        else:
            out.append(DrafterInput(repo, spec_lookup(repo), 1, None))
    return out


@dataclass
class Bucket:
    """One part of a node's memory. ``gib`` None means unknown: it is shown, never counted as 0."""

    key: str
    label: str
    gib: Optional[float]
    source: str
    note: str = ""
    low: Optional[float] = None
    high: Optional[float] = None
    # resident (before vLLM) | pool (inside vLLM's claim) | outside (next to it) | load | kv
    phase: str = "resident"

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"unknown bucket source {self.source!r}")
        if self.gib is not None:
            self.low = self.gib if self.low is None else min(self.low, self.gib)
            self.high = self.gib if self.high is None else max(self.high, self.gib)

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "gib": _r(self.gib), "low": _r(self.low),
                "high": _r(self.high), "source": self.source, "note": self.note, "phase": self.phase}


@dataclass
class NodePlan:
    node_id: str
    mem_total_gib: float
    mem_total_source: str
    gpu_memory_utilization: float
    vllm_claim_gib: float
    buckets: list[Bucket]
    startup: dict
    one_context: dict
    full_concurrency: dict
    steady_state: dict
    context_length: int
    concurrency: int
    kv_bytes_per_token: Optional[float]
    drafter: Optional[dict]
    confidence: dict
    notes: list[str]

    def bucket(self, key: str) -> Optional[Bucket]:
        return next((b for b in self.buckets if b.key == key), None)

    def as_dict(self) -> dict:
        return {"node": self.node_id, "mem_total_gib": round(self.mem_total_gib, 2),
                "mem_total_source": self.mem_total_source,
                "gpu_memory_utilization": self.gpu_memory_utilization,
                "vllm_claim_gib": round(self.vllm_claim_gib, 2),
                "buckets": [b.as_dict() for b in self.buckets],
                "startup": self.startup, "one_context": self.one_context,
                "full_concurrency": self.full_concurrency, "steady_state": self.steady_state,
                "context_length": self.context_length, "concurrency": self.concurrency,
                "kv_bytes_per_token": None if self.kv_bytes_per_token is None else round(self.kv_bytes_per_token, 1),
                "drafter": self.drafter, "confidence": self.confidence, "notes": self.notes}


def _r(v: Optional[float], d: int = 2) -> Optional[float]:
    return None if v is None else round(v, d)


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)


def _host_buckets(facts: dict) -> list[Bucket]:
    """OS + Docker, TwinSpark services, desktop and other processes, from the node's last facts.

    When MemTotal/MemAvailable were read, their difference *is* everything resident on the node, so a
    part that could not be read separately is included in "OS, kernel + Docker" (0 here) rather than
    guessed twice. Without any reading, the 0.5 defaults are used and desktop / other processes stay
    unknown.
    """
    dry = facts.get("runtime_mode") == "dry-run"
    total, avail = _num(facts.get("mem_total_gib")), _num(facts.get("mem_available_gib"))
    have_used = bool(total) and avail is not None and not dry
    age = _num(facts.get("read_at"))
    when = ""
    if age:
        import time as _time
        hours = (_time.time() - age) / 3600
        when = f" (read {hours:.0f} h ago)" if hours >= 2 else ""

    # TwinSpark services: cgroup v2 memory.current of each unit
    svc = facts.get("services_memory") if isinstance(facts.get("services_memory"), dict) else {}
    units = {k: v for k, v in svc.items() if isinstance(v, dict)}
    cur = {k: _num(v.get("current_mib")) for k, v in units.items()}
    known = {k: v for k, v in cur.items() if v is not None}
    simulated = any(v.get("simulated") for v in units.values())
    if known:
        gib = sum(known.values()) / 1024
        peak = sum((_num(units[k].get("peak_mib")) or known[k]) for k in known) / 1024
        src = "estimated" if (simulated or dry) else "measured"
        note = ", ".join(f"{k.replace('twinspark-', '')} {v:.0f} MiB" for k, v in sorted(known.items()))
        note = ("simulated by the dry-run agent: " if simulated else "cgroup memory.current: ") + note
        if len(known) < len(units):
            note += f"; {len(units) - len(known)} unit(s) unreadable"
        services = Bucket("twinspark_services", "TwinSpark services", gib, src, note + when, high=max(gib, peak))
    elif have_used:
        services = Bucket("twinspark_services", "TwinSpark services", 0.0, "measured",
                          "no per-unit reading (older agent or no systemd units): included in OS, kernel + Docker")
    else:
        services = Bucket("twinspark_services", "TwinSpark services", SERVICES_GIB, "estimated",
                          "no reading: the 0.5 default", low=0.1, high=0.6)

    # desktop session
    if not dry and isinstance(facts.get("desktop_running"), bool) and _num(facts.get("desktop_rss_gib")) is not None:
        gib = float(facts["desktop_rss_gib"]) if facts["desktop_running"] else 0.0
        names = ", ".join(facts.get("desktop_processes") or [])[:120]
        desktop = Bucket("desktop", "Desktop session", gib, "measured",
                         (f"RSS of {names}" if facts["desktop_running"] and names else
                          "no graphical session running" if not facts["desktop_running"] else "desktop RSS") + when)
    elif dry:
        desktop = Bucket("desktop", "Desktop session", 0.0, "estimated", "dry-run: assumed headless")
    elif have_used:
        desktop = Bucket("desktop", "Desktop session", 0.0, "measured",
                         "not read separately: included in OS, kernel + Docker")
    else:
        desktop = Bucket("desktop", "Desktop session", None, "unknown",
                         "not read yet: a desktop session (about 3 GiB) would come on top")

    # other processes: the largest few above 100 MiB
    procs = facts.get("other_processes")
    if isinstance(procs, list) and not dry:
        rows = [p for p in procs if isinstance(p, dict) and _num(p.get("rss_mib")) is not None]
        gib = sum(float(p["rss_mib"]) for p in rows) / 1024
        note = ", ".join(f"{str(p.get('name'))[:40]} {float(p['rss_mib']):.0f} MiB" for p in rows[:5]) or \
            "none above 100 MiB"
        other = Bucket("other_apps", "Other processes", gib, "measured",
                       f"RSS, largest above 100 MiB: {note}" + when)
    elif have_used:
        other = Bucket("other_apps", "Other processes", 0.0, "measured",
                       "not listed by this agent: included in OS, kernel + Docker")
    else:
        other = Bucket("other_apps", "Other processes", None, "unknown",
                       "not read (dry-run or no hardware facts yet)" if dry or not facts else "not read by this agent")

    # OS, kernel + Docker: what MemTotal - MemAvailable leaves after the parts above
    if have_used:
        used = total - avail
        owned = facts.get("owned_containers") if isinstance(facts.get("owned_containers"), dict) else {}
        running = int(_num(owned.get("running")) or 0)
        owned_mib = _num(owned.get("current_mib"))
        if running and owned_mib is None:
            os_b = Bucket("os_kernel_docker", "OS, kernel + Docker", OS_DOCKER_GIB, "estimated",
                          f"a TwinSpark model was running when the node was read{when}; its memory cannot be "
                          f"separated, so the 0.5 default is used", low=4.5, high=8.0)
        else:
            parts = sum(b.gib or 0.0 for b in (services, desktop, other) if b.source == "measured")
            gib = used - parts - (owned_mib or 0.0) / 1024
            note = f"MemTotal - MemAvailable = {used:.1f} GiB minus the parts listed separately"
            if running:
                note += f" and the running model ({(owned_mib or 0) / 1024:.1f} GiB)"
            if gib < 0:
                note += "; the readings disagree (taken at different moments), shown as 0"
                gib = 0.0
            os_b = Bucket("os_kernel_docker", "OS, kernel + Docker", gib, "measured", note + when,
                          low=max(0.0, gib - max(0.5, gib * 0.1)), high=gib + max(0.5, gib * 0.1))
    else:
        os_b = Bucket("os_kernel_docker", "OS, kernel + Docker", OS_DOCKER_GIB, "estimated",
                      "dry-run: the 0.5 default for a headless Spark" if dry else
                      "no reading yet: the 0.5 default for a headless node", low=4.5, high=8.0)
    return [os_b, services, desktop, other]


def _prefill_gib(spec: ModelSpec, tokens: int) -> Optional[float]:
    """First-prefill activation peak: ``tokens x hidden_size x 2 bytes x ACT_BUFFERS``.

    Inference keeps one layer's activations at a time, so the layer count does not multiply this.
    ACT_BUFFERS = 24 hidden-sized buffers per token covers q/k/v/o, the MLP or MoE gate/up/down
    intermediates including top-k token copies, residual and norm buffers. Under tensor parallelism
    the hidden states are replicated on every rank, so the figure is not halved.
    """
    if not spec.hidden_size or tokens <= 0:
        return None
    return tokens * spec.hidden_size * ACT_ELEM_BYTES * ACT_BUFFERS / 1024 ** 3


def _drafter_bucket(drafters: list[DrafterInput], sharded: bool, node_id: str, observed_nodes: dict,
                    batched: int) -> tuple[Optional[Bucket], Optional[Bucket], Optional[dict]]:
    """Drafter weights + scratch on this node, and the placement shown to the user.

    ``draft_tensor_parallel_size: 1`` means a one-rank drafter. Which rank holds it under TP2 is not
    something TwinSpark can confirm before a start, so the whole drafter is counted on every node that
    can host it until a startup timeline shows otherwise.
    """
    real = [d for d in drafters if (d.method or "").lower() not in _NO_DRAFTER_METHODS]
    if not real:
        return None, None, None
    weights: list[float] = []
    scratch: list[float] = []
    unknown, placements = [], []
    calibrated = False
    for d in real:
        if d.spec is None or not (d.spec.weight_bytes or d.spec.num_params):
            unknown.append(d.repo)
            continue
        full = (d.spec.weight_bytes or d.spec.num_params * 2.0) / 1024 ** 3
        dtp = d.draft_tensor_parallel_size
        split = sharded and (dtp is None or dtp >= 2)
        gib = full / 2 if split else full
        placement = ("split over both nodes (" + ("draft_tensor_parallel_size 2" if dtp else
                     "vLLM default: the drafter uses the target's tensor parallel size") + ")") if split else \
            ("on this node" if not sharded else "unconfirmed (counted on both nodes)")
        if sharded and not split:
            cal = _drafter_calibration(observed_nodes, full)
            if cal is not None:
                placement, on_this = cal(node_id)
                calibrated = True
                if not on_this:
                    gib = 0.0
        weights.append(gib)
        placements.append(f"{d.repo}: {placement}")
        act = _prefill_gib(d.spec, batched)
        scratch.append(act if act is not None else max(DRAFTER_SCRATCH_MIN_GIB, full * DRAFTER_SCRATCH_FRACTION))
    if unknown and not weights:
        wb = Bucket("drafter_weights", "Drafter weights", None, "unknown",
                    f"size of {', '.join(unknown)} unknown: pin the profile so its config and checkpoint size are "
                    f"registered", phase="pool")
        sb = Bucket("drafter_scratch", "Drafter scratch", None, "unknown", "drafter size unknown", phase="outside")
    else:
        note = "; ".join(placements) + (f"; size of {', '.join(unknown)} unknown, not counted" if unknown else "")
        wb = Bucket("drafter_weights", "Drafter weights", sum(weights), "calibrated" if calibrated else "measured",
                    "checkpoint size; " + note, high=sum(weights) * 1.05, phase="pool")
        sb = Bucket("drafter_scratch", "Drafter scratch", sum(scratch), "estimated",
                    "the drafter's own working memory for a prefill batch; not every speculative method's dummy "
                    "run is profiled by vLLM, so it is counted next to vLLM's claim",
                    low=sum(scratch) * 0.5, high=sum(scratch) * 2, phase="outside")
    info = {"repos": [d.repo for d in real], "placement": "; ".join(placements) or "unknown",
            "confirmed": calibrated, "unknown_size": unknown}
    return wb, sb, info


def _drafter_calibration(observed_nodes: dict, drafter_gib: float):
    """Placement from the last startup timeline: node A's and B's vLLM footprints differ by about the
    drafter's size when only one rank holds it. Ambiguous differences leave it unconfirmed."""
    fa = _num((observed_nodes.get("A") or {}).get("footprint_gib"))
    fb = _num((observed_nodes.get("B") or {}).get("footprint_gib"))
    if fa is None or fb is None or drafter_gib < 0.5:
        return None
    diff = fa - fb
    if abs(diff) <= 0.25 * drafter_gib:
        return lambda node: ("on both nodes (calibrated from the last startup)", True)
    if abs(diff) >= 0.75 * drafter_gib:
        holder = "A" if diff > 0 else "B"
        return lambda node: (f"node {holder} only (calibrated: its startup footprint was {abs(diff):.1f} GiB "
                             f"larger)", node == holder)
    return None


def _level(margin: Optional[float]) -> str:
    return "UNKNOWN" if margin is None else MemoryPlanner.classify_headroom(margin)


def _plan_node(planner: MemoryPlanner, *, spec: ModelSpec, quant: Quantization, context_length: int,
               concurrency: int, topology: Topology, node_id: str, backend: DistributedBackend,
               kv_dtype: Optional[str], using_cuda_graphs: bool, util: Optional[float], facts: dict,
               drafters: list[DrafterInput], batched_tokens: Optional[int], chunked_prefill: Optional[bool],
               observed: dict, observed_kv_tokens: Optional[int], mem_total_gib: Optional[float],
               kv_source: Optional[tuple[str, str]] = None) -> NodePlan:
    notes: list[str] = []
    sharded = topology in _SHARDED
    shards = 2 if sharded else 1
    observed_nodes = observed.get("nodes") if isinstance(observed.get("nodes"), dict) else {}

    # ---- the node itself ----------------------------------------------------------------------
    total = _num(facts.get("mem_total_gib"))
    if total:
        mem_total, mem_src = total, "measured"
    elif mem_total_gib:
        mem_total, mem_src = float(mem_total_gib), "measured"
    else:
        mem_total, mem_src = GB10_MEM_TOTAL_GIB, "estimated"
        notes.append(f"node {node_id} has not reported its MemTotal yet: {GB10_MEM_TOTAL_GIB} GiB (a GB10) is assumed")
    if util is None:
        util = planner.gpu_memory_utilization(planner.reserve_budget(node_id, mem_total_gib=mem_total))
    claim = util * mem_total                     # vLLM sizes its KV cache to fill this
    buckets = _host_buckets(facts)

    # ---- inside vLLM's claim ------------------------------------------------------------------
    if spec.weight_bytes:
        weights = spec.weight_bytes / 1024 ** 3 / shards
        wnote = "checkpoint size (safetensors index)" + (", split over 2 nodes" if sharded else "")
        wsrc = "measured"
    else:
        weights = spec.num_params * _BYTES_PER_PARAM[quant] / 1024 ** 3 / shards
        wnote = f"parameters x {_BYTES_PER_PARAM[quant]} bytes ({quant.value})" + \
            (", split over 2 nodes" if sharded else "")
        wsrc = "estimated"
    buckets.append(Bucket("main_weights", "Model weights", weights, wsrc, wnote,
                          low=weights * 0.98, high=weights * 1.05, phase="pool"))

    tokens = batched_tokens or (context_length if chunked_prefill is False else DEFAULT_BATCHED_TOKENS)
    tok_note = (f"max_num_batched_tokens {tokens:,}" if batched_tokens else
                f"chunked prefill off: one full {tokens:,}-token prefill" if chunked_prefill is False else
                f"vLLM's default max_num_batched_tokens {tokens:,} assumed")
    drafter_w, drafter_s, drafter_info = _drafter_bucket(drafters, sharded, node_id, observed_nodes, tokens)
    if drafter_w is not None:
        buckets.append(drafter_w)

    act = _prefill_gib(spec, tokens)
    if act is not None:
        buckets.append(Bucket("first_prefill", "First-prefill peak", act, "estimated",
                              f"{tok_note} x hidden {spec.hidden_size} x {ACT_ELEM_BYTES} bytes x "
                              f"{ACT_BUFFERS} buffers",
                              low=act * 0.5, high=act * 1.5, phase="pool"))
    else:
        old = max(1.0, weights * 0.05) + 0.25 * min(concurrency, 16)
        buckets.append(Bucket("first_prefill", "First-prefill peak", old, "estimated",
                              "hidden size unknown: 0.5's rule (5% of the weights + 0.25 GiB per sequence)",
                              low=old * 0.5, high=old * 2, phase="pool"))
    graphs = CUDA_GRAPHS_GIB if using_cuda_graphs else 0.0
    buckets.append(Bucket("cuda_graphs", "CUDA graphs", graphs, "estimated",
                          "captured graphs (fixed allowance)" if using_cuda_graphs else "eager mode: no CUDA graphs",
                          low=graphs * 0.4, high=graphs * 2.5, phase="pool"))

    # ---- next to vLLM's claim -----------------------------------------------------------------
    comm = 0.0 if not sharded else COMM_GIB["ray" if backend == DistributedBackend.RAY else "native"]
    buckets.append(Bucket("comm_buffers", "NCCL / backend buffers", comm, "estimated",
                          "single node: no inter-node communication" if not sharded else
                          f"NCCL buffers + {'Ray' if backend == DistributedBackend.RAY else 'native'} backend "
                          f"(fixed allowance)", low=comm * 0.5, high=comm * 1.5, phase="outside"))
    if drafter_s is not None:
        buckets.append(drafter_s)
    layers = spec.num_hidden_layers or spec.layers
    if layers:
        loader = max(LOADER_MIN_GIB, weights / layers)
        lnote = f"one layer's weights on this node ({weights / layers:.2f} GiB): repacking briefly holds two copies"
    else:
        loader, lnote = LOADER_FIXED_GIB, "layer count unknown: fixed allowance"
    buckets.append(Bucket("loader_transient", "Loader transient", loader, "estimated", lnote,
                          low=min(loader, 0.25), high=loader * 2, phase="load"))

    # ---- KV ------------------------------------------------------------------------------------
    kv_elem = _KV_BYTES.get((kv_dtype or "auto").lower(), 2.0)
    if spec.kv_bytes_per_token:
        per_tok = float(spec.kv_bytes_per_token)
        kv_src, kv_note = kv_source or ("measured", "KV bytes per token measured by the recipe (per node, as served)")
    else:
        per_tok = _kv_bytes_per_token(spec, kv_elem)
        if (topology == Topology.PP2 or (topology in (Topology.TP2, Topology.TP_EP) and not spec.is_mla
                                         and spec.num_kv_heads >= 2)):
            per_tok /= 2
        kv_src, kv_note = "estimated", f"textbook KV formula ({kv_dtype or 'auto'} cache)"
        if kv_source and kv_source[1]:
            # say why no measurement was used, e.g. "the recipe measured its KV size with other settings"
            kv_note += f"; {kv_source[1]}"
    if per_tok > 0:
        kv_one = per_tok * context_length / 1024 ** 3
        kv_full = kv_one * concurrency
        buckets.append(Bucket("kv_one_sequence", "KV: one max-length sequence", kv_one, kv_src,
                              f"{kv_note}; {context_length:,} tokens", phase="kv"))
        buckets.append(Bucket("kv_full_concurrency", "KV: requested concurrency", kv_full, kv_src,
                              f"{concurrency} x {context_length:,} tokens", phase="kv"))
    else:
        kv_one = kv_full = None
        why = "no KV size: the model's attention layout is not known"
        buckets.append(Bucket("kv_one_sequence", "KV: one max-length sequence", None, "unknown", why, phase="kv"))
        buckets.append(Bucket("kv_full_concurrency", "KV: requested concurrency", None, "unknown", why, phase="kv"))

    # ---- verdicts ------------------------------------------------------------------------------
    def total_of(phases: tuple[str, ...], attr: str, skip: tuple[str, ...] = ()) -> float:
        return sum(getattr(b, attr) for b in buckets
                   if b.phase in phases and b.gib is not None and b.key not in skip)

    not_counted = [b.label for b in buckets if b.gib is None and b.phase != "kv"]
    rng = {}
    for attr in ("gib", "low", "high"):
        resident = total_of(("resident",), attr)
        pool_fixed = total_of(("pool",), attr)
        outside = total_of(("outside",), attr)
        prefill = next(getattr(b, attr) for b in buckets if b.key == "first_prefill")
        comm_part = next(getattr(b, attr) for b in buckets if b.key == "comm_buffers")
        load_part = total_of(("pool",), attr, skip=("first_prefill", "cuda_graphs"))
        one = 0.0 if kv_one is None else kv_one
        # vLLM fills its claim with KV cache; it never holds less than its fixed part plus one sequence
        vllm = max(claim, pool_fixed + one)
        serve_peak = resident + outside + vllm
        # while loading, no KV cache exists yet: weights + one layer being repacked
        load_peak = resident + comm_part + load_part + total_of(("load",), attr)
        rng[attr] = {"resident": resident, "pool_fixed": pool_fixed, "serve": serve_peak, "load": load_peak,
                     "peak": max(serve_peak, load_peak), "steady": serve_peak - prefill,
                     "kv_space": claim - pool_fixed}
    est = rng["gib"]
    # the pessimistic peak uses the high ends of every range and the optimistic one the low ends
    peak, peak_hi, peak_lo = est["peak"], rng["high"]["peak"], rng["low"]["peak"]
    phase = "weight loading" if est["load"] > est["serve"] else "first prefill"
    startup = {"peak_gib": _r(peak), "peak_low_gib": _r(peak_lo), "peak_high_gib": _r(peak_hi),
               "estimate_gib": _r(peak), "mem_total_gib": _r(mem_total), "phase": phase,
               "source": "estimated", "not_counted": not_counted}
    obs = observed_nodes.get(node_id) if isinstance(observed_nodes.get(node_id), dict) else None
    footprint = _num((obs or {}).get("footprint_gib"))
    if footprint is not None:
        # what this revision's containers took on this node at the startup's lowest MemAvailable,
        # on top of what is resident now
        peak = est["resident"] + footprint
        peak_lo, peak_hi = rng["low"]["resident"] + footprint, rng["high"]["resident"] + footprint
        startup.update(peak_gib=_r(peak), peak_low_gib=_r(peak_lo), peak_high_gib=_r(peak_hi), source="calibrated",
                       observed_footprint_gib=_r(footprint), observed_at=(observed or {}).get("at"),
                       observed_min_available_gib=_r(_num(obs.get("min_mem_available_gib"))),
                       observed_min_stage=obs.get("min_stage"))
    margin = mem_total - peak
    startup.update(margin_gib=_r(margin), margin_low_gib=_r(mem_total - peak_hi),
                   margin_high_gib=_r(mem_total - peak_lo), ok=margin >= 0, level=_level(margin))
    if startup["source"] == "calibrated" and isinstance(obs.get("samples"), int):
        n = obs["samples"]
        startup.update(calibration_samples=n, calibration_median_gib=_r(_num(obs.get("median_gib"))),
                       calibration_spread_gib=_r(_num(obs.get("spread_gib"))),
                       calibration_margin_gib=_r(_num(obs.get("margin_gib"))))
        extra = (f" + {obs['margin_gib']:.1f} GiB allowance while there are fewer than 3 starts"
                 if _num(obs.get("margin_gib")) else "")
        worst, median = _num(obs.get("worst_gib")) or footprint, _num(obs.get("median_gib")) or footprint
        spread = _num(obs.get("spread_gib")) or 0.0
        startup["note"] = (f"{n} earlier start(s) with exactly these settings took up to {worst:.1f} GiB here at their "
                           f"lowest MemAvailable (median {median:.1f}, spread {spread:.1f}){extra}; with what is "
                           f"resident now that peaks at {peak:.1f} of {mem_total:.1f} GiB "
                           f"(estimate: {est['peak']:.1f} GiB)")
    elif startup["source"] == "calibrated":
        startup["note"] = (f"last startup of this revision took {footprint:.1f} GiB here at its lowest MemAvailable; "
                           f"with what is resident now that peaks at {peak:.1f} of {mem_total:.1f} GiB "
                           f"(estimate: {est['peak']:.1f} GiB)")
    else:
        startup["note"] = (f"peak during {phase}: {peak:.1f} of {mem_total:.1f} GiB "
                           f"(vLLM claims {util:.2f} x {mem_total:.1f} = {claim:.1f} GiB and fills it with KV cache)")
    if margin >= 0 > mem_total - peak_hi:
        startup["note"] += "; the pessimistic end of the range does not fit"
    if not_counted:
        startup["note"] += f"; not counted (unknown): {', '.join(not_counted)}"

    kv_space = est["kv_space"]
    one_ctx: dict[str, Any] = {"kv_space_gib": _r(kv_space), "needed_gib": _r(kv_one), "source": kv_src}
    full: dict[str, Any] = {"kv_space_gib": _r(kv_space), "needed_gib": _r(kv_full), "source": kv_src,
                            "requested_concurrency": concurrency}
    if kv_one is None:
        one_ctx.update(ok=None, level="UNKNOWN", margin_gib=None, note="KV size per token unknown")
        full.update(ok=None, level="UNKNOWN", margin_gib=None, est_full_length_seqs=None, est_kv_tokens=None,
                    note="KV size per token unknown")
    else:
        tokens_fit = max(0.0, kv_space) * 1024 ** 3 / per_tok
        seqs = tokens_fit / context_length
        one_ctx.update(ok=kv_space >= kv_one, margin_gib=_r(kv_space - kv_one), level=_level(kv_space - kv_one),
                       note=(f"vLLM's claim minus weights, first prefill and graphs leaves {kv_space:.1f} GiB of KV; "
                             f"one {context_length:,}-token sequence needs {kv_one:.1f} GiB") if kv_space >= kv_one else
                       (f"only {max(0.0, kv_space):.1f} GiB of KV is left, one {context_length:,}-token sequence "
                        f"needs {kv_one:.1f} GiB: vLLM refuses to start"))
        ok = kv_space >= kv_full
        full.update(ok=ok, margin_gib=_r(kv_space - kv_full),
                    level=_level(kv_space - kv_full) if ok else ("LOW" if kv_space >= kv_one else "CRITICAL"),
                    est_full_length_seqs=round(seqs, 1), est_kv_tokens=int(tokens_fit),
                    note=f"room for about {seqs:.1f} full-length sequences, {concurrency} requested"
                    + ("" if ok else ": vLLM serves fewer in parallel"))
        if observed_kv_tokens:
            cal = observed_kv_tokens / context_length
            full.update(source="calibrated", est_kv_tokens=int(observed_kv_tokens),
                        est_full_length_seqs=round(cal, 1), ok=cal >= concurrency, estimate_kv_tokens=int(tokens_fit),
                        note=f"vLLM reported {observed_kv_tokens:,} KV tokens on its last start with these settings: "
                             f"about {cal:.1f} full-length sequences, {concurrency} requested")
            full["level"] = "SAFE" if full["ok"] else ("LOW" if cal >= 1 else "CRITICAL")
            one_ctx.update(source="calibrated", ok=cal >= 1, level="SAFE" if cal >= 1 else "CRITICAL")
    steady = {"gib": _r(est["steady"]), "low_gib": _r(rng["low"]["steady"]), "high_gib": _r(rng["high"]["steady"]),
              "note": "weights, KV cache and everything else resident while serving; the first-prefill working set "
                      "comes on top only while a long prefill runs"}

    # ---- confidence: how many inputs were read rather than assumed --------------------------------
    inputs = [mem_src] + [b.source for b in buckets if b.key in (
        "os_kernel_docker", "twinspark_services", "desktop", "other_apps", "main_weights", "drafter_weights",
        "kv_one_sequence")] + [startup["source"]]
    measured = sum(1 for s in inputs if s in ("measured", "calibrated"))
    confidence = {"measured_inputs": measured, "inputs": len(inputs),
                  "text": f"measured inputs: {measured}/{len(inputs)}",
                  "unknown": [b.label for b in buckets if b.source == "unknown"]}
    if drafter_info and not drafter_info["confirmed"] and sharded and "unconfirmed" in drafter_info["placement"]:
        notes.append("the drafter runs with draft_tensor_parallel_size 1; which node holds it is not confirmed, so it "
                     "is counted fully on both")
    return NodePlan(node_id=node_id, mem_total_gib=mem_total, mem_total_source=mem_src,
                    gpu_memory_utilization=util, vllm_claim_gib=claim, buckets=buckets, startup=startup,
                    one_context=one_ctx, full_concurrency=full, steady_state=steady,
                    context_length=context_length, concurrency=concurrency,
                    kv_bytes_per_token=per_tok if per_tok > 0 else None, drafter=drafter_info,
                    confidence=confidence, notes=notes)


_MOE_KEYS = ("num_experts", "num_local_experts", "n_routed_experts", "moe_num_experts")


def _cfg_int(cfg: dict, key: str, default: int = 0, hi: int = 10 ** 9) -> int:
    """One integer field of a (user supplied or downloaded) config.json, validated."""
    v = cfg.get(key)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"hf_config.{key} must be a number")
    if v < 0 or v > hi:
        raise ValueError(f"hf_config.{key} is out of range")
    return int(v)


def make_spec_from_hf(config: dict, weight_bytes: Optional[int] = None) -> ModelSpec:
    """Build a ModelSpec from a HF ``config.json`` (+ safetensors index total_size).

    ``num_params`` is not part of config.json; pass ``weight_bytes`` from
    ``model.safetensors.index.json -> metadata.total_size`` whenever possible.
    Malformed input raises ``ValueError`` (never ``TypeError``/``OverflowError``).
    """
    if not isinstance(config, dict):
        raise ValueError("hf_config must be an object")
    cfg = config.get("text_config", config)   # multimodal wrappers nest the LM config
    if not isinstance(cfg, dict):
        raise ValueError("hf_config.text_config must be an object")
    if weight_bytes is not None:
        if (isinstance(weight_bytes, bool) or not isinstance(weight_bytes, (int, float))
                or weight_bytes != weight_bytes or not 0 <= weight_bytes <= 10 ** 15):
            raise ValueError("weight_bytes must be a number between 0 and 1e15")
        weight_bytes = int(weight_bytes)
    hidden = _cfg_int(cfg, "hidden_size")
    heads = _cfg_int(cfg, "num_attention_heads", 1) or 1
    layers = _cfg_int(cfg, "num_hidden_layers")
    head_dim = _cfg_int(cfg, "head_dim") or (hidden // heads if hidden else 0)
    kv_heads = _cfg_int(cfg, "num_key_value_heads") or heads
    experts = next((_cfg_int(cfg, k) for k in _MOE_KEYS if cfg.get(k)), 0)
    num_params = _cfg_int(config, "num_params", hi=10 ** 13) or _cfg_int(config, "_num_params", hi=10 ** 13)
    if not num_params and not weight_bytes:
        raise ValueError("need num_params or weight_bytes (safetensors index) to size weights")
    return ModelSpec(
        num_params=num_params, layers=layers, num_kv_heads=kv_heads, head_dim=head_dim,
        weight_bytes=weight_bytes, is_moe=experts > 0, num_experts=experts,
        num_attention_heads=_cfg_int(cfg, "num_attention_heads"),
        kv_lora_rank=_cfg_int(cfg, "kv_lora_rank"),
        qk_rope_head_dim=_cfg_int(cfg, "qk_rope_head_dim"),
        hidden_size=hidden, num_hidden_layers=layers,
    )
