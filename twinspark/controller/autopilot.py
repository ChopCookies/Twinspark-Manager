"""Memory Autopilot, Context Advisor, and Quantization Advisor (spec §9, §10, §11)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..schemas.enums import MemoryStrategy, Quantization, Topology, VerificationStatus
from .planner import MemoryPlanner, ModelSpec


@dataclass
class Advice:
    safe: bool
    message: str
    headroom_gib: float = 0.0
    extra_gib_needed: float = 0.0
    gpu_memory_utilization: float = 0.0
    settings: dict = field(default_factory=dict)
    suggested: list[str] = field(default_factory=list)


class MemoryAutopilot:
    """Translates an optimisation goal into concrete settings + a fit verdict (spec §9)."""

    def __init__(self, planner: MemoryPlanner, spec: ModelSpec, quant: Quantization,
                 topology: Topology):
        self.planner, self.spec, self.quant, self.topology = planner, spec, quant, topology

    def plan(self, strategy: MemoryStrategy, context_length: int, concurrency: int,
             **overrides) -> Advice:
        s: dict = {"context_length": context_length, "concurrency": concurrency,
                   "quant": self.quant, "eager": False, "kv_dtype": None}
        if strategy == MemoryStrategy.MAX_CONTEXT:
            s["kv_dtype"] = "fp8"                     # halves KV per token
            s["concurrency"] = 1
        elif strategy == MemoryStrategy.MAX_QUALITY:
            s["kv_dtype"] = "auto"
            s["concurrency"] = max(1, concurrency // 2)
        elif strategy == MemoryStrategy.MAX_THROUGHPUT:
            s["concurrency"] = max(8, concurrency * 2)
            s["max_num_batched_tokens"] = 8192
        elif strategy == MemoryStrategy.MAX_MODEL_SIZE:
            s["context_length"] = max(4096, context_length // 2)
            s["concurrency"] = 1
            s["kv_dtype"] = "fp8"
        s.update(overrides)

        budget = self.planner.estimate(
            spec=self.spec, quant=s["quant"], context_length=s["context_length"],
            concurrency=s["concurrency"], topology=self.topology,
            kv_dtype=s["kv_dtype"], using_cuda_graphs=not s["eager"],
        )
        util = self.planner.gpu_memory_utilization(budget)
        fits, headroom = self.planner.fits(budget, util)
        public = {k: (v.value if hasattr(v, "value") else v) for k, v in s.items()}
        if fits:
            return Advice(True, "Safe configuration", headroom_gib=headroom,
                          gpu_memory_utilization=util, settings=public)
        extra = -headroom
        return Advice(False, f"{extra:.1f} GiB additional memory required",
                      extra_gib_needed=extra, gpu_memory_utilization=util, settings=public,
                      suggested=self._suggestions(s))

    def _suggestions(self, s: dict) -> list[str]:
        out = []
        if s["concurrency"] > 1:
            out.append(f"reduce concurrency to {max(1, s['concurrency'] // 2)}")
        if s["context_length"] > 8192:
            out.append(f"reduce context to {max(4096, s['context_length'] // 2)}")
        if s.get("kv_dtype") not in ("fp8", "fp8_e4m3", "fp8_e5m2"):
            out.append("use an fp8 KV cache")
        if not s["eager"]:
            out.append("enable eager mode (no CUDA graphs, ~1.2 GiB, slower decode)")
        if s["quant"] in (Quantization.BF16, Quantization.FP8):
            out.append("use an NVFP4 checkpoint")
        if self.topology in (Topology.SINGLE_A, Topology.SINGLE_B):
            out.append("spread across both Sparks (tp2)")
        return out


@dataclass
class ContextCandidate:
    context: int
    concurrency: int
    headroom_gib: float
    status: str

    def as_row(self) -> dict:
        return {"context": self.context, "context_label": f"{self.context // 1024}K",
                "concurrency": self.concurrency, "headroom_gib": round(self.headroom_gib, 1),
                "status": self.status}


class ContextAdvisor:
    def __init__(self, planner: MemoryPlanner, spec: ModelSpec, quant: Quantization,
                 topology: Topology, kv_dtype: Optional[str] = None):
        self.planner, self.spec, self.quant, self.topology = planner, spec, quant, topology
        self.kv_dtype = kv_dtype

    def find_max_safe(self, concurrency: int, contexts: list[int]) -> list[ContextCandidate]:
        rows = []
        for ctx in sorted(contexts):
            budget = self.planner.estimate(
                spec=self.spec, quant=self.quant, context_length=ctx, concurrency=concurrency,
                topology=self.topology, kv_dtype=self.kv_dtype,
            )
            _, headroom = self.planner.fits(budget)
            status = "Does not fit" if headroom < 0 else ("Tight" if headroom < 2.0 else "Safe")
            rows.append(ContextCandidate(ctx, concurrency, headroom, status))
        return rows


@dataclass
class QuantVariant:
    quant: Quantization
    weight_gib: float
    total_expected_gib: float
    headroom_gib: float
    fits: bool
    status: VerificationStatus
    limitations: str = ""


class QuantizationAdvisor:
    """Sizes each quantisation. Verification status is NOT guessed here — it comes
    from the catalog/cookbook that provided the checkpoint (``known_status``)."""

    _QUALITY = {Quantization.BF16: 5, Quantization.FP8: 4, Quantization.NVFP4: 3,
                Quantization.MXFP4: 3, Quantization.INT4: 2, Quantization.AWQ: 2,
                Quantization.GPTQ: 2, Quantization.AUTOROUND: 2}

    def __init__(self, planner: MemoryPlanner, spec: ModelSpec, topology: Topology,
                 known_status: Optional[dict[Quantization, VerificationStatus]] = None):
        self.planner, self.spec, self.topology = planner, spec, topology
        self.known_status = known_status or {}

    def variants(self, context_length: int = 32768, concurrency: int = 4) -> list[QuantVariant]:
        out = []
        spec = ModelSpec(**{**self.spec.__dict__, "weight_bytes": None})  # compare like-for-like
        for q in Quantization:
            budget = self.planner.estimate(spec=spec, quant=q, context_length=context_length,
                                           concurrency=concurrency, topology=self.topology)
            fits, headroom = self.planner.fits(budget)
            out.append(QuantVariant(
                quant=q, weight_gib=budget.model_weights, total_expected_gib=budget.total,
                headroom_gib=headroom, fits=fits,
                status=self.known_status.get(q, VerificationStatus.EXPERIMENTAL),
                limitations=("needs a runtime with sm_121a FP4 kernels"
                             if q in (Quantization.NVFP4, Quantization.MXFP4) else ""),
            ))
        return out

    def best_that_fits(self, context_length: int, concurrency: int) -> Optional[QuantVariant]:
        fitting = [v for v in self.variants(context_length, concurrency) if v.fits]
        return max(fitting, key=lambda v: self._QUALITY[v.quant]) if fitting else None
