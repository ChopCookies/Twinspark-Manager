"""Measurements bound to the experiment they were made in.

A KV size per token or a startup footprint is only true for the settings it was measured with. Before
0.7 the KV size lived on the repo-wide model spec, so two profiles of the same model with different
KV formats shared one number and a new measurement silently changed the other profile's forecast.

Now every measurement is stored under a **fingerprint** of what it depends on:

* ``kv_fingerprint`` — what changes the KV bytes per token as served on one node: the model commit, the
  image digest, the engine, the KV cache dtype, the topology, block size, attention backend, KV-related
  flags and the speculative decoding setup. Context length and concurrency do not change bytes per
  token, so a profile with a longer context reuses the measurement.
* ``startup_fingerprint`` — the KV fingerprint plus what changes the startup footprint: context
  length, concurrency, batched tokens, chunked prefill, gpu_memory_utilization, eager mode, quantization,
  the distributed backend, extra models and mods.

A fingerprint that does not match means "no measurement": the planner falls back to the recipe's
number (only if the draft still has the settings the recipe measured it with) or to the formula, and
says so. Pinning or rolling back one profile never changes another profile's forecast.

Storage (the controller's kv table):

* ``kv_measure:<fp>``  — up to ``MAX_SAMPLES`` samples ``{bytes_per_token, pool_gib, tokens, revision_id, at}``
* ``calibration:<fp>`` — up to ``MAX_SAMPLES`` samples ``{footprint_gib, min_mem_available_gib, min_stage,
  kv_tokens, steady_gib, baseline_gib, revision_id, at}``
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from typing import Any, Optional

MAX_SAMPLES = 10
# a startup calibrated from fewer samples than this gets an extra allowance on top of the worst one seen
FEW_SAMPLES = 3
FEW_SAMPLES_MARGIN_GIB = 0.5

# vLLM flags (normalised: no dashes, "-" for "_") that change the KV bytes per token
_KV_FLAGS = {"kv-cache-dtype", "block-size", "calculate-kv-scales", "attention-backend", "mamba-ssm-cache-dtype",
             "mamba-cache-dtype", "kv-sharing-fast-prefill", "disable-hybrid-kv-cache-manager",
             "kv-cache-memory", "kv-cache-memory-bytes", "max-num-seqs-per-kv-group", "cache-dtype"}
_KV_ENV = ("VLLM_ATTENTION_BACKEND", "VLLM_USE_FLASHINFER_MLA", "VLLM_MLA_DISABLE", "VLLM_KV_CACHE_LAYOUT")
_FOOTPRINT_FLAGS = _KV_FLAGS | {"max-model-len", "max-num-seqs", "max-num-batched-tokens", "enable-chunked-prefill",
                                "gpu-memory-utilization", "enforce-eager", "cuda-graph-sizes",
                                "max-seq-len-to-capture", "compilation-config", "enable-prefix-caching"}


def _norm_flag(name: str) -> str:
    return str(name).lstrip("-").replace("_", "-").lower()


def _flags(advanced: Any, keep: set[str]) -> dict[str, Any]:
    flags = {_norm_flag(k): v for k, v in (getattr(advanced, "extra_vllm_flags", None) or {}).items()
             if _norm_flag(k) in keep}
    # raw args ("--block-size 32", "--kv-cache-dtype=fp8") count too
    args = [str(x) for x in getattr(advanced, "extra_vllm_args", None) or []]
    for i, arg in enumerate(args):
        if not arg.startswith("--"):
            continue
        name, eq, value = arg.partition("=")
        key = _norm_flag(name)
        if key in keep:
            flags[key] = value if eq else (args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--")
                                           else True)
    return dict(sorted(flags.items()))


def _speculative(advanced: Any) -> Optional[dict[str, Any]]:
    sc = getattr(advanced, "speculative_config", None)
    if not isinstance(sc, dict) or not sc:
        return None
    return {k: sc.get(k) for k in ("method", "model", "num_speculative_tokens", "draft_tensor_parallel_size")
            if sc.get(k) is not None}


def kv_settings(draft: Any) -> dict[str, Any]:
    """The draft settings (not the pinned identity) that change the KV bytes per token."""
    s, a = draft.simple, draft.advanced
    env = getattr(a, "env", None) or {}
    return {
        "model": s.model,
        "topology": getattr(s.topology, "value", s.topology),
        "kv_dtype": (a.kv_dtype or "auto").lower(),
        "block_size": a.block_size,
        "attention_backend": a.attention_backend,
        "env": {k: env[k] for k in _KV_ENV if k in env},
        "flags": _flags(a, _KV_FLAGS),
        "speculative": _speculative(a),
    }


def startup_settings(draft: Any) -> dict[str, Any]:
    """Everything in :func:`kv_settings` plus what changes how much a start takes."""
    s, a = draft.simple, draft.advanced
    return {
        **kv_settings(draft),
        "quantization": getattr(s.quantization, "value", s.quantization),
        "context_length": s.planning_context(),
        "concurrency": a.max_num_seqs or s.concurrency,
        "max_num_batched_tokens": a.max_num_batched_tokens,
        "chunked_prefill": a.chunked_prefill,
        "gpu_memory_utilization": a.gpu_memory_utilization,
        "eager": bool(a.eager_mode),
        "prefix_cache": bool(a.prefix_cache),
        "compilation_config": a.compilation_config or None,
        "distributed_backend": getattr(draft.distributed_backend, "value", draft.distributed_backend),
        "extra_models": sorted(a.extra_models or []),
        "mods": sorted(a.mods or []),
        "flags": _flags(a, _FOOTPRINT_FLAGS),
    }


def _identity_part(identity: Any) -> dict[str, Any]:
    if identity is None:
        return {"model_revision": None, "image_digest": None, "engine": "vllm"}
    return {"model_revision": identity.model_revision, "image_digest": identity.image_digest,
            "engine": "vllm", "vllm_version": getattr(identity, "vllm_version", None)}


def _digest(prefix: str, doc: dict[str, Any]) -> str:
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def kv_fingerprint(draft: Any, identity: Any, node: str) -> Optional[str]:
    """None for an unpinned draft: without a model commit and an image digest nothing can match."""
    if identity is None:
        return None
    return _digest("kv", {**kv_settings(draft), **_identity_part(identity), "node": node})


def startup_fingerprint(draft: Any, identity: Any, node: str) -> Optional[str]:
    if identity is None:
        return None
    return _digest("st", {**startup_settings(draft), **_identity_part(identity), "node": node})


# ---- reading vLLM's boot log -------------------------------------------------------------------
_KV_MEMORY = re.compile(r"Available KV cache memory:\s*([\d.]+)\s*GiB", re.I)


def kv_memory_from_log(text: str) -> Optional[float]:
    """vLLM v1's ``Available KV cache memory: 39.36 GiB`` (the last one: a restart logs it again)."""
    found = _KV_MEMORY.findall(text or "")
    try:
        return float(found[-1]) if found else None
    except ValueError:
        return None


# ---- the store ------------------------------------------------------------------------------------
def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


class Measurements:
    """Fingerprinted measurements in the controller's store (``kv_get``/``kv_set``)."""

    def __init__(self, store: Any):
        self.store = store

    # -- writing --
    def _append(self, key: str, sample: dict[str, Any]) -> None:
        doc = self.store.kv_get(key) or {}
        samples = [s for s in doc.get("samples") or [] if isinstance(s, dict)]
        # one sample per revision start: a re-adopted deployment must not count twice
        samples = [s for s in samples if not (s.get("revision_id") == sample.get("revision_id")
                                              and s.get("job_id") and s.get("job_id") == sample.get("job_id"))]
        samples.append(sample)
        self.store.kv_set(key, {"samples": samples[-MAX_SAMPLES:], "updated_at": time.time()})

    def record_kv(self, draft: Any, identity: Any, nodes: list[str], kv: dict[str, Any], *,
                  revision_id: str, job_id: Optional[str] = None) -> list[str]:
        """Store the KV bytes per token vLLM's boot log implies (pool GiB / pool tokens) for each node."""
        tokens, pool = kv.get("kv_cache_tokens"), _num(kv.get("kv_memory_gib"))
        if not isinstance(tokens, int) or tokens <= 0 or not pool:
            return []
        per_tok = pool * 1024 ** 3 / tokens
        keys = []
        for node in nodes:
            fp = kv_fingerprint(draft, identity, node)
            if fp:
                self._append(f"kv_measure:{fp}", {"bytes_per_token": round(per_tok, 1), "pool_gib": round(pool, 3),
                                                   "tokens": tokens, "revision_id": revision_id, "job_id": job_id,
                                                   "at": time.time()})
                keys.append(fp)
        return keys

    def record_startup(self, draft: Any, identity: Any, node: str, summary: dict[str, Any], *,
                       revision_id: str, job_id: Optional[str] = None,
                       kv_tokens: Optional[int] = None) -> Optional[str]:
        """Store one node's startup timeline summary (see controller.timeline) as a calibration sample."""
        fp = startup_fingerprint(draft, identity, node)
        footprint = _num((summary or {}).get("footprint_gib"))
        if not fp or footprint is None:
            return None
        sample = {"footprint_gib": round(footprint, 3), "revision_id": revision_id, "job_id": job_id,
                  "min_mem_available_gib": _num(summary.get("startup_min_available_gib"))
                  or _num(summary.get("min_mem_available_gib")),
                  "min_stage": summary.get("startup_min_stage") or summary.get("min_stage"),
                  "baseline_gib": _num(summary.get("baseline_available_gib")),
                  "steady_gib": _num(summary.get("steady_footprint_gib")),
                  "kv_tokens": kv_tokens if isinstance(kv_tokens, int) else None, "at": time.time()}
        self._append(f"calibration:{fp}", sample)
        return fp

    # -- reading --
    def _samples(self, key: str) -> list[dict[str, Any]]:
        doc = self.store.kv_get(key) or {}
        return [s for s in doc.get("samples") or [] if isinstance(s, dict)]

    def kv(self, draft: Any, identity: Any, node: str) -> Optional[dict[str, Any]]:
        """``{value, samples, spread, fingerprint}`` for this exact setup on this node, or None."""
        fp = kv_fingerprint(draft, identity, node)
        values = [_num(s.get("bytes_per_token")) for s in self._samples(f"kv_measure:{fp}")] if fp else []
        values = [v for v in values if v]
        if not values:
            return None
        return {"value": statistics.median(values), "samples": len(values),
                "spread": max(values) - min(values), "fingerprint": fp}

    def startup(self, draft: Any, identity: Any, node: str) -> Optional[dict[str, Any]]:
        """The calibration of this exact startup on this node: the worst footprint seen (plus an allowance
        while there are few samples), the median, the spread and how many starts back it."""
        fp = startup_fingerprint(draft, identity, node)
        samples = [s for s in self._samples(f"calibration:{fp}") if _num(s.get("footprint_gib")) is not None] \
            if fp else []
        if not samples:
            return None
        foot = [_num(s["footprint_gib"]) for s in samples]
        worst = max(samples, key=lambda s: s["footprint_gib"])
        margin = FEW_SAMPLES_MARGIN_GIB if len(foot) < FEW_SAMPLES else 0.0
        tokens = [s["kv_tokens"] for s in samples if isinstance(s.get("kv_tokens"), int)]
        return {"footprint_gib": max(foot) + margin, "worst_gib": max(foot), "median_gib": statistics.median(foot),
                "spread_gib": max(foot) - min(foot), "margin_gib": margin, "samples": len(foot),
                "min_mem_available_gib": worst.get("min_mem_available_gib"), "min_stage": worst.get("min_stage"),
                "kv_tokens": min(tokens) if tokens else None, "at": max(s.get("at") or 0 for s in samples),
                "fingerprint": fp}


# ---- the KV size the planner uses ------------------------------------------------------------------
def recipe_kv_applies(draft: Any) -> tuple[bool, str]:
    """A recipe's measured KV bytes per token is valid for the settings it was measured with. Imports
    since 0.7 record those (``source.kv_measured_with``); older imports did not, so their number is used
    with a note that its settings are unknown."""
    src = draft.source if isinstance(draft.source, dict) else {}
    if not src.get("kv_bytes_per_token"):
        return False, "the recipe has no KV measurement"
    stamp = src.get("kv_measured_with")
    if not isinstance(stamp, dict):
        return True, "recipe measurement (the settings it was made with were not recorded)"
    now = kv_settings(draft)
    changed = sorted(k for k in set(stamp) | set(now) if stamp.get(k) != now.get(k))
    if changed:
        return False, f"the recipe measured its KV size with other settings ({', '.join(changed)} changed)"
    return True, "measured by the recipe author with these settings"


def resolve_kv(measurements: Optional[Measurements], draft: Any, identity: Any,
               node: str) -> tuple[Optional[float], str, str]:
    """``(bytes_per_token or None, source, note)``: a measurement of this exact setup, else the recipe's
    number when its settings still apply, else None (the planner's formula, labelled estimated)."""
    if measurements is not None and identity is not None:
        got = measurements.kv(draft, identity, node)
        if got:
            n = got["samples"]
            return got["value"], "measured", (f"measured on node {node} from vLLM's KV pool on {n} start(s) of these "
                                              f"exact settings")
    applies, why = recipe_kv_applies(draft)
    if applies:
        return float(draft.source["kv_bytes_per_token"]), "measured", why
    return None, "estimated", why
