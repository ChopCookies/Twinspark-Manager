"""UMA-aware unified memory planner (spec §8, §29).

DGX Spark is a single unified-memory system, NOT a GPU with separate VRAM. The
planner works per node — two Sparks are never treated as one 256 GiB pool.

Two outputs matter:

* ``NodeBudget`` — estimated memory per node, bucket by bucket.
* ``gpu_memory_utilization`` — the value handed to vLLM. vLLM *pre-allocates*
  ``util * total`` for weights + activations + KV cache. On a UMA system that
  pool competes with the OS, Docker and this manager, so it must be derived from
  the non-vLLM reserve, never left at vLLM's default of 0.9.

Values: Estimated (math), Observed (post-launch telemetry), Calibrated (blend).
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from typing import Optional

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


_MOE_KEYS = ("num_experts", "num_local_experts", "n_routed_experts", "moe_num_experts")


def make_spec_from_hf(config: dict, weight_bytes: Optional[int] = None) -> ModelSpec:
    """Build a ModelSpec from a HF ``config.json`` (+ safetensors index total_size).

    ``num_params`` is not part of config.json; pass ``weight_bytes`` from
    ``model.safetensors.index.json -> metadata.total_size`` whenever possible.
    """
    cfg = config.get("text_config", config)   # multimodal wrappers nest the LM config
    hidden = int(cfg.get("hidden_size", 0))
    heads = int(cfg.get("num_attention_heads", 1)) or 1
    layers = int(cfg.get("num_hidden_layers", 0))
    head_dim = int(cfg.get("head_dim") or (hidden // heads if hidden else 0))
    kv_heads = int(cfg.get("num_key_value_heads") or heads)
    experts = next((int(cfg[k]) for k in _MOE_KEYS if cfg.get(k)), 0)
    num_params = int(config.get("num_params") or config.get("_num_params") or 0)
    if not num_params and not weight_bytes:
        raise ValueError("need num_params or weight_bytes (safetensors index) to size weights")
    return ModelSpec(
        num_params=num_params, layers=layers, num_kv_heads=kv_heads, head_dim=head_dim,
        weight_bytes=weight_bytes, is_moe=experts > 0, num_experts=experts,
        num_attention_heads=int(cfg.get("num_attention_heads") or 0),
        kv_lora_rank=int(cfg.get("kv_lora_rank") or 0),
        qk_rope_head_dim=int(cfg.get("qk_rope_head_dim") or 0),
    )
