# TwinSpark — Optimal Dual DGX Spark Profiles

> **Research note (2026-09).** This is the desk research that seeded the built-in recipes. It was
> written before any run on real hardware. Model versions and figures may be out of date; the Cookbook
> and `tsm fit` are the current source.
>
> **Target hardware:** 2 × NVIDIA DGX Spark (GB10 Blackwell, 128 GiB unified memory each,
> ~273 GB/s, ~1000 TOPS FP4, ARM64, connected via QSFP).
> **Rule:** two Sparks are never a pooled 256 GiB pool — every config is
> validated **per node** against a 128 GiB budget.
> **Status:** research-grounded (figures from Hugging Face configs / safetensors indices and
> verified deployment blogs). Precision figures are `VERIFIED` (source-lifted) or `ESTIMATED`
> (math on top of a VERIFIED basis). Nothing here had been run on real Sparks when it was
> written — measure first (section 8).

There are two independent levers on a dual-Spark rig:

1. **Topology** — how a *single* model spans the two nodes (TP2 / TP2+EP / PP2).
2. **Replication** — a model that fits on *one* node runs twice (one copy per node),
   the gateway round-robins → **2× throughput**.

The optimal choice per model is decided by: **(weights-at-quant ÷ node) + KV + overhead ≤ ~112 GiB**.

### The four concrete models in play (verified live on Hugging Face)

|#| Model | Repo |
|---|---|---|
|1| DeepSeek V4 Flash 0731 | `deepseek-ai/DeepSeek-V4-Flash-0731` |
|2| DeepSeek V4 Flash Vision-Exp | `deepseek-ai/DeepSeek-V4-Flash-Vision-Exp` |
|3| Qwen 3.8 Flash-Next | `Qwen/Qwen3.8-Flash-Next` |
|4| GLM 5.3 Flash | `zai-org/GLM-5.3-Flash` |

> **DeepSeek V4.1 Flash is dropped** (out of the running — doesn't fit 2×128 GB; see §5 for
> the reasoning). The Qwen 3.8 lineup here is the **Flash-Next** hybrid-MoE n-gram model, not
> the old dense `Qwen3-8B`. All weights below come from `model.safetensors.index.json` on the
> exact repos above.

---

## 0. Memory budget (per node, headless)

| Bucket | GiB |
|---|---:|
| MemTotal | 128.0 |
| System + Docker + TwinSpark + reserve | ~10.6 |
| Non-vLLM total (planner `reserve_budget`) | ~10.6 |
| **Practical vLLM pool** (`gpu-memory-utilization` ≈ 0.88) | **~112** |

`gpu-memory-utilization` is derived from the reserve, never vLLM's 0.9 default.

---

## 1. DeepSeek V4 Flash 0731 — the flagship → **TP2, dual-Spark, dedicated `dspark` path**

> **Repo:** `deepseek-ai/DeepSeek-V4-Flash-0731` — the concrete model in play (verified on HF).

| Property | Value | Basis |
|---|---|---:|
| Parameters | **284B total / 13B active**, MoE | model card `VERIFIED` |
| Routed experts / top-k | 256 / 6 (+1 shared) | config `VERIFIED` |
| Layers / hidden | 43 / 4096 | config `VERIFIED` |
| Attention | MLA (`DeepseekV4ForCausalLM`) | config `VERIFIED` |
| `q_lora_rank` / `qk_rope_head_dim` / head_dim | 1024 / 64 / 512 | config `VERIFIED` |
| Native expert dtype | **fp4** (`expert_dtype: fp4`) | config `VERIFIED` |
| **Published quant** | **FP8 + FP4 — the repo IS already the quant** (`quant_method: fp8`, `expert_dtype: fp4`) | config `VERIFIED` |
| Raw checkpoint | **155.4 GiB** (48 shards, fp8/fp4 mixed) | HF file listing `VERIFIED` |
| Sliding window | 128 (CSA/HCA hybrid) | config `VERIFIED` |
| MTP heads | 1 (`num_nextn_predict_layers: 1`) | config `VERIFIED` |
| Max context | 1,048,576 | config `VERIFIED` |

**Why TP2, not single or replicated:** 155 GiB does **not** fit a single 128 GiB node,
and replicating two 155 GiB copies is impossible. TP2 halves weights → **~77.7 GiB/node**,
leaving ~27 GiB for KV cache + concurrency. TP2 delivers **~3× decode throughput
vs single-node (41 tok/s vs 12–15 tok/s)** — VERIFIED. The community-verified *dedicated*
DS4-GB10 vLLM image exists (`aidendle94/sparkrun-vllm-ds4-gb10:production-ready`).

**Attention is hybrid (this is the KV trick):** V4 uses Compressed Sparse Attention (CSA) +
Heavily Compressed Attention (HCA) with **sliding_window = 128** on top of MLA (1 KV head,
`head_dim=512`). KV grows sub-linearly — empirically **~7.5–7.8 KB/token** (not the naive
~1 KB MLA figure) but cheap enough that **1M context costs only ~12% decode-speed loss vs
262K**. Verified: 1M ctx = 41 tok/s, 262K = 46 tok/s, 128K+, fp8 KV pool 14.77 GiB/node
supports **2.1M tokens** of KV.

### Per-node estimate (TP2, official mixed-checkpoint, FP8 KV)

| | GiB |
|---|---:|
| Weights (155.4 GiB ÷ 2) | 77.7 |
| KV cache (fp8, block 256, @512K ctx × 3–6 seqs) | ~14.8 |
| Activations / graphs / backend | ~4 |
| **Model total** | **~96.5** |
| + OS reserve @ util 0.82–0.85 | ~31.5 left for OS/container/NCCL |
| **Peak GPU memory observed** | **~79–84 GiB/node** — SAFE |

### Concurrency ceilings (VERIFIED)

| Context | Concurrency |
|---|---:|
| 64K | ~16 seqs |
| 128K | ~8–10 seqs |
| 256K | ~4 seqs |
| 512K | 3 seqs |
| 1M | 2–3 seqs |

### Recommended profile (DeepSeek V4 Flash 0731)

```json
{
  "name": "deepseek-v4-flash-tp2",
  "description": "DeepSeek V4 Flash 0731, TP2 dual-Spark, 131K ctx, MTP on. Flagship reasoning/coder.",
  "simple": {
    "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
    "quantization": "fp8",
    "topology": "tp2",
    "context_length": 131072,
    "concurrency": 4,
    "thinking": true,
    "tool_calling": true,
    "api_alias": "default"
  },
  "behaviour": {
    "reasoning_parser": "deepseek_v4",
    "tool_call_parser": "hermes",
    "temperature": 0.6,
    "top_p": 0.95
  },
  "advanced": {
    "kv_dtype": "fp8",
    "prefix_cache": true,
    "chunked_prefill": true,
    "max_num_batched_tokens": 8192,
    "speculative_config": { "method": "mtp", "num_speculative_tokens": 2 }
  },
  "runtime_adapter": "eugr",
  "distributed_backend": "mp",
  "verification": "community"
}
```

> **MTP on DeepSeek:** native `deepseek_mtp` / auto-detected `mtp`. **K=2 is the verified
> sweet spot — K=3 is a NEGATIVE result** (al-engr.com measured 1.44 tok/s @MTP=3 vs
> 1.46 @MTP=2: worse base, marginal third-token contribution). The dedicated Spark image
> also exposes the `dspark` n-gram path (K=5, `draft_sample_method: probabilistic`). See §6.

### Verified launch flags (FP8 KV — Flowtivity battle-tested)

```bash
# env
VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 VLLM_USE_B12X_MOE=1
VLLM_SPARSE_INDEXER_MAX_LOGITS_MB=256
TORCH_CUDA_ARCH_LIST=12.1a FLASHINFER_CUDA_ARCH_LIST=12.1a HF_HUB_OFFLINE=1
NCCL_IB_DISABLE=0 NCCL_IB_HCA=rocep1s0f0 NCCL_IB_GID_INDEX=3 NCCL_SOCKET_IFNAME=<cabled-nic>
# leader (worker identical with --node-rank 1 --headless)
vllm serve deepseek-ai/DeepSeek-V4-Flash-0731 --served-model-name deepseek-v4-flash-spark \
  --host 0.0.0.0 --port 8000 --trust-remote-code \
  --tensor-parallel-size 2 --pipeline-parallel-size 1 --distributed-executor-backend mp \
  --nnodes 2 --node-rank 0 --master-addr <HEAD_IP> --master-port 29501 \
  --kv-cache-dtype fp8 --block-size 256 \
  --max-model-len 500000 --max-num-seqs 8 --max-num-batched-tokens 8192 \
  --gpu-memory-utilization 0.82 --enable-prefix-caching \
  --speculative-config '{"method":"mtp","num_speculative_tokens":2}' \
  --moe-backend flashinfer_b12x --enable-flashinfer-autotune \
  --tokenizer-mode deepseek_v4 --tool-call-parser deepseek_v4 --enable-auto-tool-choice \
  --reasoning-parser deepseek_v4
```

**Critical gotchas (VERIFIED):** driver mismatch = **2.4× speed gap** between nodes (verify
`nvidia-smi` on both); QSFP carries RDMA+TCP — pin NCCL to the *cabled* HCA (only 1 of 2 RoCE
ports is wired); vLLM upstream lacks SM12x → needs the patched build/jasl fork or
`ghcr.io/anemll/dspark-vllm-gx10:0.1.1`; `VLLM_TRITON_MLA_SPARSE=1` for the sparse MLA kernel;
`--shm-size 10g --ulimit memlock=-1` mandatory for RDMA; CUDA-graph capture ~4 min, cold
start ~9 min (avoid eager — graphs give ~15–20% decode).

---

## 2. DeepSeek V4 Flash Vision-Exp → **TP2, dual-Spark** (community-verified: 1M ctx)

> **Repo:** `deepseek-ai/DeepSeek-V4-Flash-Vision-Exp` — the concrete model in play.

| Property | Value | Basis |
|---|---|---:|
| Backbone | same 43-layer `DeepseekV4` + **32-block ViT** vision tower + aligner | config/community `VERIFIED` |
| Raw checkpoint | **156.3 GiB** | safetensors index `VERIFIED` |
| MTP heads | **3** (`num_nextn_predict_layers: 3`) | config `VERIFIED` |
| Community deploy | **TP2, 2× Spark, 1M ctx, NVFP4 KV** | tonyd2wild recipe `VERIFIED` |
| KV pool (measured) | **2,904,519 tokens (18.18 GiB)** @ 0.85 gmu | tonyd2wild `VERIFIED` |
| Vision (336×336 → 26-token answer) | **~1.03 s** end-to-end | tonyd2wild `VERIFIED` |

Same LM as V4 Flash with a native vision tower — **not** a VLM sidecar. Same-day community
deploy at **TP2 on 2× Spark with a real 1M-token KV pool** (2.9M tokens, 18.18 GiB, fp8) and
**MTP K=5** (`MTP_NUM_TOKENS=5`). Note: the earlier k=3 A/B was *without* the `spec-dspark.py`
patch that silently halves draft acceptance — use **k=5** with Patch 4 mounted. GPU-memory
0.85, `MAX_MODEL_LEN` 1M, `MAX_NUM_SEQS` 12. Vision is cheap (~1 s/image), so content vision
input is not a KV concern at these pools.

> ⚠️ vLLM upstream can't load this checkpoint as-is: the vision checkpoint reports the
> *same* architecture string as the text-only `DeepseekV4ForCausalLM` but carries 316 tensors
> vLLM has nowhere to put → fails at load. Needs the vision-ported build (tonyd2wild repo).

### Recommended profile (Vision-Exp)

```json
{
  "name": "deepseek-v4-flash-vision-tp2",
  "description": "DeepSeek V4 Flash Vision-Exp, TP2 dual-Spark, 1M ctx, native vision.",
  "simple": {
    "model": "deepseek-ai/DeepSeek-V4-Flash-Vision-Exp",
    "quantization": "fp8",
    "topology": "tp2",
    "context_length": 1048576,
    "concurrency": 12,
    "thinking": true,
    "tool_calling": true,
    "api_alias": "vision"
  },
  "behaviour": { "reasoning_parser": "deepseek_v4", "temperature": 0.6 },
  "advanced": {
    "kv_dtype": "fp8",
    "prefix_cache": true,
    "implicit_tokenizer": true,
    "speculative_config": { "method": "dspark", "num_speculative_tokens": 5 }
  },
  "runtime_adapter": "tonyd2wild",
  "distributed_backend": "mp",
  "verification": "community"
}
```

---

## 3. Qwen 3.8 Flash-Next → **TP2 SPEED (verified)**, *or* **REPLICATED** (it fits one node!)

> **Repo:** `nvidia/Qwen3.8-Flash-Next-NVFP4` (the covered official quant) over
> `Qwen/Qwen3.8-Flash-Next` — the concrete model in play (verified on HF).
> This is the **d-spark / n-gram model itself**, not an addon: its config carries
> `ngram_size=3`, `heads_per_ngram=8`, `split_ngram_parts=128`, an indexer, and `ple` —
> the per-token n-gram routing Qwen3.8 introduces. The dense `Qwen3-8B` (the "old" 8B) is
> unrelated and not part of this family.

| Property | Value | Basis |
|---|---|---:|
| Parameters | **125B total / ~6B active** hybrid MoE, `Qwen4ExpForConditionalGeneration` | community `VERIFIED` |
| Attention | **Hybrid: Gated DeltaNet (linear) + full/small-attn**, 3:1 interleave | config `VERIFIED` |
| N-gram table | **51B-param FP8 PLE table (47.68 GiB)** — lives on NVMe, 16 rows/token read on demand | config+community `VERIFIED` |
| MTP head | 4B (`qwen4_exp_mtp`) | config+community `VERIFIED` |
| Base checkpoint | **335.3 GiB (BF16)** | HF file listing `VERIFIED` |
| **Published NVFP4** | **`nvidia/Qwen3.8-Flash-Next-NVFP4` = 123.6 GiB** (modelopt, 10 shards) | HF file listing `VERIFIED` |
| **Published FP8** | **`Qwen/Qwen3.8-Flash-Next-FP8` = 172.8 GiB** (official) | HF file listing `VERIFIED` |
| Max context | 262,144 (YaRN-scalable → ~1M) | config `VERIFIED` |
| **Fits ONE Spark?** | **YES** — ~76 GiB resident weights + negligible KV (n-gram on disk) | tonyd2wild `VERIFIED` |

**Key correction vs. earlier draft:** with the n-gram table left on NVMe, the NVFP4
checkpoint needs only **~76 GiB resident** — **it fits a single DGX Spark**. That (a) makes
**REPLICATION a real option** (one copy per node, round-robin → 2× concurrent throughput,
zero inter-node comm, fault-tolerant) and (b) reframes TP2 as a *speed/KV* lever, not a
*fit* lever. **Two community-verified TP2 lanes** from the same launcher family:

| TP2 lane | Single-stream | Aggregate | KV pool | Purpose |
|---|---:|---:|---:|---|
| **SPEED** (default) | **53.7 tok/s** | 97.9 @ 6 streams | 1.97M tokens | low-latency agentic/structured |
| **CONTEXT** | 35.8 tok/s | — | **5.87M tokens** (22× 262K) | max concurrent long contexts |

MTP4 + CUDA graphs is the unlock (~64% draft acceptance, 3.56 tok/step): with it on,
2 Sparks outperform 4× older 3090s on agentic workloads. Native n-gram makes it the best
`dspark`-style decode candidate in the lineup. FP8 (172.8 GiB) is the higher-fidelity
fallback; BF16 (335 GiB) fits only 4× Spark TP4.

### Recommended profile (Qwen 3.8 Flash-Next — TP2 SPEED)

```json
{
  "name": "qwen3.8-flash-tp2",
  "description": "Qwen 3.8 Flash-Next, TP2 dual-Spark, 262K hybrid-attention context, MTP4.",
  "simple": {
    "model": "nvidia/Qwen3.8-Flash-Next-NVFP4",
    "quantization": "nvfp4",
    "topology": "tp2",
    "context_length": 262144,
    "concurrency": 8,
    "thinking": true,
    "tool_calling": true,
    "api_alias": "default"
  },
  "behaviour": {
    "reasoning_parser": "qwen3",
    "tool_call_parser": "hermes",
    "temperature": 0.6
  },
  "advanced": {
    "kv_dtype": "fp8",
    "prefix_cache": true,
    "max_num_batched_tokens": 32768,
    "extra_vllm_flags": { "enable_expert_parallel": true },
    "speculative_config": { "method": "qwen4_exp_mtp", "num_speculative_tokens": 4 },
    "compilation": { "cudagraph_mode": "FULL_DECODE_ONLY", "torch_compile": false }
  },
  "runtime_adapter": "tonyd2wild",
  "distributed_backend": "mp",
  "verification": "community"
}
```

> **Replicated variant (both verified to fit):** clone the profile with `topology: "replicated"`
> and `runtime_adapter: "tonyd2wild"` — one copy per node, gateway round-robins. Good when
> you want maximum *concurrent* throughput and can tolerate per-request latency over raw
> aggregate; **bad** if you need a single huge KV pool or faster first token (TP2 wins there).
> CONTEXT lane: `MTP=4, gmu=0.80, PLE_MODE=mmap` → 5.87M-token pool. The n-gram table
> (47.68 GiB) needs the disk-backed patch in tonyd2wild's repo on **both** lanes.<br>
> **Global Qwen gotchas:** don't combine the 4096 prefill chunk with torch.compile on (with
> piecewise-compile the 4096 chunk that's +50% under compile-off makes it *slower*); with the
> table resident at TP2, torch.compile's autotune makes a second ~24 GiB table copy per node
> and can reboot the Sparks — keep compile off when the table is in unified memory.

> Smaller siblings (VERIFIED): **Qwen3.8-27B** (27.8B dense, 51.7 GiB BF16 / ~24.6 GiB NVFP4,
> `qwen3_5` hybrid, 262K native) is a clean **single-node / replicated** append — both copies
> fit a node with ~86 GiB headroom. And **Qwen3-8B** (8.2B dense, 15.3 GiB BF16 / 7.6 GiB NVFP4,
> GQA 32/8) is the classic **REPLICATED** winner at the small end: each copy is ~7.6 GiB NVFP4,
> so one per node + round-robin gives **2× throughput, zero inter-node comm, fault tolerance** —
> strictly better than TP2 for a model this small. For the ~235B-A22B MoE (235B/22B, 128 experts,
> 437.9 GiB BF16 / ~219 GiB NVFP4 → 109.5 GiB/node), **TP2+EP is required** (NVFP4 is the only
> precision that fits; EP shards the 128 experts 64/GPU).

---

## 4. GLM 5.3 Flash → **TP2 + EP** (NVFP4), dual-Spark — community-verified

> **Repo:** `zai-org/GLM-5.3-Flash` base, but deploy the **`RedHatAI/GLM-5.3-Flash-NVFP4`**
> (compressed-tensors) quant — see the corruption warning below.

| Property | Value | Basis |
|---|---|---:|
| Parameters | **320B total / 18B active**, MoE | config `VERIFIED` |
| Architecture | KDA + DeepSeek Sparse Attention (DSA) + mHC, MLA (`Glm5NextForConditionalGeneration`) | config `VERIFIED` |
| Routed experts / top-k | 288 / 8 (+1 shared) | config `VERIFIED` |
| Layers / hidden | 45 (3 dense + 42 MoE) / 4096 | config `VERIFIED` |
| **Published quant** | **base = FP8 native** (`quant_method: fp8`, 305.8 GiB, 62 shards) | config + HF `VERIFIED` |
| NVFP4 (ModelOpt) | `nvidia/GLM-5.3-Flash-NVFP4` = **190.4 GiB** (33 shards) | HF `VERIFIED` |
| **⭐ Deploy NVFP4** | **`RedHatAI/GLM-5.3-Flash-NVFP4`** (compressed-tensors, 11 large shards) | tonyd2wild `VERIFIED` |
| Max context | 1,048,576 | config `VERIFIED` |
| MTP / drafter | 1 layer MTP (`glm5_next_mtp`); community uses **DFlash2** drafter `k=7` | config + community `VERIFIED` |
| Community deploy | **TP2, 2× Spark, 262K ctx, fp8 KV, DFlash2** — 52.2 tok/s code / 18.8 prose | tonyd2wild `VERIFIED` |

**Why TP2 + EP:** the base is **FP8 native at 305.8 GiB** — does not fit even split (÷2 =
152.9/node > budget). NVFP4 is the only precision that fits: 190.4 GiB → **~95.2 GiB/node**
with `--enable-expert-parallel` sharding the 288 experts ~144/GPU. GLM-5.3-Flash is the
strongest "fits dual-Spark at 4-bit" capability pick (rivals Claude Opus-class on
coding/agentic per model docs).

> ⚠️ **⚠️ CHECKPOINT CORRUPTION — this is the single most important GLM gotcha.** ModelOpt-
> quantized NVFP4 builds (`LibertAIDAI/GLM-5.3-Flash-NVFP4`, abliterated variants, and the
> `nvidia` pack that leaves 403 attention tensors in BF16) emit **intermittent corrupted token
> IDs** (vLLM #54150) — nearly invisible in English, but a corrupted token inside a tool-call
> block desyncs the parser and can spiral into repetition lock. Community measured
> **0 / 0 / 0** corruptions on `RedHatAI/GLM-5.3-Flash-NVFP4` (compressed-tensors) vs
> **4 / 9 / 8** on ModelOpt, under Korean-Hangul probe @ temp 0. **Use RedHatAI**, drop-in —
> same arch, no flag changes, and it loads ~2× faster (11 large shards vs 120 small). Ensure the
> vision `chat_template_mm.jinja` is present in the weights dir or image requests 500.

### Recommended profile (GLM 5.3 Flash)

```json
{
  "name": "glm-5.3-flash-tp2-ep",
  "description": "GLM 5.3 Flash, TP2 + expert-parallel dual-Spark, NVFP4 (RedHatAI), DFlash2.",
  "simple": {
    "model": "RedHatAI/GLM-5.3-Flash-NVFP4",
    "quantization": "nvfp4",
    "topology": "tp2",
    "context_length": 262144,
    "concurrency": 4,
    "thinking": true,
    "tool_calling": true,
    "api_alias": "default"
  },
  "behaviour": {
    "reasoning_parser": "glm",
    "tool_call_parser": "jupyter",
    "temperature": 0.6
  },
  "advanced": {
    "kv_dtype": "fp8",
    "prefix_cache": true,
    "max_num_batched_tokens": 32768,
    "extra_vllm_flags": { "enable_expert_parallel": true, "moe_backend": "marlin" },
    "speculative_config": { "method": "dflash2", "num_speculative_tokens": 7 }
  },
  "runtime_adapter": "tonyd2wild",
  "distributed_backend": "mp",
  "verification": "community"
}
```

> DFlash2 `k=7` (draft acceptance ≈0.39) is the community-verified drafter; toggling to the
> native `glm5_next_mtp` MTP is fine when the DFlash2 image isn't available. The 4×-Spark lift
> (TP4, 1M ctx, 3.9M-token KV pool) exists but is out of scope for a 2-box rig.

---

## 5. DeepSeek V4.1 Flash — interesting, **but does NOT fit dual-Spark. Verdict: defer.**

| Property | Value | Basis |
|---|---|---:|
| Backbone params | **552B** | config `VERIFIED` |
| Engram memory | **196B additional** | config `VERIFIED` |
| **Total** | **~748B** | VERIFIED |
| Active / prompt-token | 8B | VERIFIED |
| Active / output-token | 16B | VERIFIED |
| Layers | 40 (text) + 32 (ViT) | config `VERIFIED` |
| Routed experts | 384 | config `VERIFIED` |
| MTP layers | 3 | config `VERIFIED` |
| Raw safetensors (BF16) | **763 GB / 711 GiB** | index `VERIFIED` |

**The math, honestly:**

| Route | Weight/node | Verdict |
|---|---:|---|
| BF16 (711 GiB) | 355 GiB | ❌ 3× over budget |
| FP8 (~356 GiB) | 178 GiB | ❌ over budget |
| NVFP4 (~374+ GiB @ 0.5 B/param) | 187 GiB | ❌ **still over** 128 GiB |

**V4.1 Flash does NOT fit 2×128 GB, period.** At 748B params even NVFP4 (~374 GiB) exceeds
the pair's combined budget once sharded. The **Engram/n-gram offload** accelerates *decode*
of what already fits — it does **not** shrink weights, so it cannot rescue the fit. And
because the compression artifacts compound with attention compression, cramming this into
4-bit heads straight into the "quantisations that are shit" risk you flagged. **Verdict:
not a dual-Spark model** — it wants 4× DGX Spark (or a Hopper/Grace node) at a sane
precision, or a genuinely high-quality NVFP4 community convert plus a smaller context ceiling.
Revisit if/when such a convert ships and you accept ~93 GiB/node weights with minimal KV.

---

## 5.5 Community-tested dual-Spark set-ups — the ground truth to copy from

These are *measured, working* projects — people already run exactly these four models on
**2× DGX Spark** and publish the recipe + flags + numbers. Prefer their configs over my
derived estimates anywhere they conflict; every profile above was reconciled against them.

### Primary (the four in-play models, all dual-Spark)

| Author / repo | Covers | Key measured results & flags |
|---|---|---|
| **tonyd2wild/...** (canonical family) | all four | the single most authoritative dual-Spark source; see rows below |
| `tonyd2wild/DeepSeek-V4-Flash-Dual-Spark-Recipe` | **DS V4 Flash 0731** ✅ | **~40 tok/s** single, **~92 agg @ 8 concurrent**, 500K ctx; MTP `k=2` (~78% draft accept on code); TP2, fp8 KV, block 256, gmu 0.80, no-Ray `mp` backend; pre-pull image + weights on both nodes; driver/node match = +140% prefill |
| `tonyd2wild/DeepSeek-V4-Flash-2x-Spark-1M` | DS V4 Flash | **45.5 tok/s @ 1M ctx**, KV pool 2.84M, gmu 0.85 |
| `tonyd2wild/DeepSeek-v4-Flash-DSpark-60-tok-s-900K-ctx-2x-DGX-Spark` | DS V4 Flash dspark | **~62 tok/s @ 900K** via n-gram offload, fp8 KV |
| `tonyd2wild/DeepSeek-v4-Flash-Vision-Exp-DSpark-1M-NVFP4-KV-2x-DGX-Spark` | **Vision-Exp** ✅ | **1M ctx, KV 2.9M tokens (18.18 GiB), MTP K=5 (needs spec-dspark patch), ~1 s/image**; KV cache NVFP4; `MAX_NUM_SEQS` 12 |
| `tonyd2wild/Qwen3.8-Flash-Next-NVFP4-DGX-Spark` | **Qwen 3.8** ✅ | fits **one** Spark (~76 GiB resident; 47.68 GiB n-gram table on NVMe); 43.9 tok/s single; TP2-SPEED **53.7 tok/s**, agg 97.9; CONTEXT lane **5.87M KV**; MTP4 + CUDA graphs = unlock |
| `tonyd2wild/GLM-5.3-Flash-NVFP4-DFlash2-2x-DGX-Spark` | **GLM 5.3** ✅ | **world-first dual-Spark GLM-5.3**: TP2, 262K, fp8 KV, DFlash2 k=7; **52.2 code / 18.8 prose tok/s**; **RedHatAI/GLM-5.3-Flash-NVFP4** (compressed-tensors) not ModelOpt |
| `tonyd2wild/GLM-5.3-Flash-EXL3-on-2x-NVIDIA-DGX-Spark` | GLM 5.3 alt | **EXL3/TR3 4bpw** variant, 1M ctx; NVFP4 vs EXL3 A/B (quality tie, EXL3 stronger on prose+tooling) |
| `tonyd2wild/DeepSeek-V4.1-Flash-vLLM-DGX-Spark` | **V4.1** ❌ | TP4 on **4×** Spark — confirms the 2-box defer |

### Enablers & infra

| Author / repo | What it gives you |
|---|---|
| **eugr/spark-vllm-docker** | the community-standard **DGX-Spark vLLM image + docker/dc** wrapper (dual-node orchestration, RDMA env, shm/memlock) — base all the `runtime_adapter: eugr` profiles assume. *(If 404, it moved/pivoted; the tonyd2wild family now ships its own launchers + image refs.)* |
| jasl/vllm fork + `ghcr.io/anemll/dspark-vllm-gx10:0.1.1` | add SM12x/FP4/MLA support upstream vLLM lacks; the dspark n-gram offload path |
| Flowtivity | 181 s to load a 148.66 GiB / 46-shard checkpoint; load-time sanity anchor |

**What to copy verbatim from these:** tp2 + `--distributed-executor-backend mp` (no Ray),
fp8 KV + `--block-size 256`, image + weights pre-pulled on both nodes, worker-first
(rank 1) then head (rank 0), driver/`nvidia-smi` match on both boxes, RoCE pinning to the
*cabled* HCA, `--shm-size 10g --ulimit memlock=-1`, and the per-model MTP/drafter settings
already folded into the profiles above. These repos are your Phase-0 "does the pinned image
really support X" oracle.

---

### §5.5b Independent cross-confirmation & challenges to tony's numbers

tony is *not* the only one — the same four models run dual-Spark in **many** independent
repos. They mostly **confirm** tony's approach (TP2, V4 KV on `fp8_ds_mla`, DSpark/DFlash2,
Anemll/eugr images) but a few **improve** on his headline numbers. Treat these as the
second opinion that hardens the profiles.

#### DeepSeek V4 Flash 0731 — independent agrees, two repos beat tony's ~62 tok/s

| Repo | Stars | Confirms / improves |
|---|---:|---|
| **raullenchai/twinspark** | 178 | **~75 tok/s single-stream DSpark** (2.74× over the 27 tok/s bandwidth floor), 1.7k tok/s prefill, 1M ctx, 2.68M-token KV — *beats tony's ~62*; the definitive self-healing 2-node production recipe. Same `anemll` image. |
| **Anemll/dspark-vllm-gx10** | 75 | the canonical **two-node DSpark/NVFP4 port** (vLLM 0.25.1/0.25.2): TP2, `nvfp4_ds_mla` KV, FlashInfer SM121 sparse-MLA + b12x native-MXFP4 MoE backend; 350K ctx. *The image every other DS repo builds on.* |
| alexellis/deepseek-v4-flash-0731-2x-dgx-spark | 6 | **prose 38–45, code 65–89 tok/s**, ~82 structured, 1M ctx, `nvfp4_ds_mla` KV — code lane *exceeds* tony. |
| Weschera/DeepSeek-V4-Flash-0731-DSpark-2x-DGX-Spark | 9 | **DSpark K7 = 83.8 tok/s single-stream** (3.09× vs no-spec) with output-SHA256 reproducibility; a 40K speed profile + 1M serving profile. |
| botAGI/DeepSeek-V4-Flash-DSpark-GB10-2x-DGX-Spark-1m-fp4-fp8 | 9 | DSpark, 1M ctx, fp4/fp8 — confirms the 1M lane. |
| bird/DSv4-Flash-spark | 3 | DSpark γ=5, 45–65+ tok/s; overlays that cut multi-turn TTFT in half (0.92→0.47 s) and lift aggregate 108→131 tok/s. |

> **Reconciliation:** tony's ~40–62 tok/s is the *conservative floor*. raullenchai (~75) and
> Weschera's K7 (83.8) are the current dual-Spark decode ceiling for DS 0731, all on the same
> `anemll`/DSpark path → the §1 flagship profile's `dspark` lane should target **60–85 tok/s**.

#### Qwen 3.8 Flash-Next — MiaAI-Lab is the heavy independent weight

| Repo | Stars | Confirms / improves |
|---|---:|---|
| **MiaAI-Lab/Qwen3.8-Flash-Next-Dual-DGX-Sparks** | 373 | **TP2 + EP + MTP3** on `nvidia/Qwen3.8-Flash-Next-NVFP4`, 262K; ~126 GiB free/node (rsync worker copy from head); ~500k-token KV at fp8, 3.65M claimed (NVFP4 KV). The most-followed Qwen dual-Spark kit — **matches my §3 TP2-SPEED recommendation exactly.** |
| getrefined/Qwen3.8-Flash-Next-NVFP4-vLLM-DGX-Spark | 12 | vLLM (not SGLang) TP2+EP MTP3 + CUDA graphs on `RadixArk` NVFP4: **greedy 55.8 tok/s, agg 126.1 @8** — *beats tony's 53.7/97.9*; the vLLM sibling of tony's SGLang repo. |
| PixelML/qwen3-8-flash-next-sglang-2x-dgx-spark | 8 | SGLang NVFP4 TP2 — confirms tony's SGLang lane. |
| 0xBakeer / dolf3131 / etc. | — | single-Spark n-gram-on-NVMe — reinforce the fits-one-node finding in §3. |

> **Reconciliation:** the community splits Qwen into **TP2+EP (vLLM or SGLang) at ~53–56 tok/s,
> agg ~98–126** — functionally identical to my two-lane §3. The highest measured aggregate
> (getrefined 126.1 @8) comes from **TP2+EP + MTP3 + CUDA graphs**, so the §3 speed profile
> should add `enable_expert_parallel: true` + CUDA graphs, not just plain TP2.

#### GLM 5.3 Flash — kingjones30 day-0 + sfxnz confirm; note the drafter split

| Repo | Stars | Confirms / improves |
|---|---:|---|
| **kingjones30/GLM-5.3-Flash-2x-DGX-Spark** | 8 | **day-0 GLM-5.3 on 2× Spark: 24.74 code / 30.30 structured / 19.6 prose tok/s**, MTP-5, NVFP4 + `fp8_ds_mla` KV, 262K. Adds the real GLM gotchas: `--language-model-only` (vision processor OOMs the front-end to 15.7 GB RSS on 121 GB), `--moe-backend marlin`, and the **FlashInfer sparse-MLA `pe_dim=64` hard-assert** + top-k table fix. |
| **sfxnz/GLM-5.3-Flash-NVFP4-vLLM-2x-DGX-Spark** | 17 | LibertAIDAI NVFP4 (~181 GiB) + **DFlash2-7** (block-diffusion) default at 327,680 ctx; structured 50.7→68.1 tok/s. Cross-check on the DFlash2 path. |
| Reederey87/glm53-flash-exl3-2x-dgx-spark / vcruz305 / Enntity / Tutanka01 / hisorishige / Bizuayeu | 5–65 | EXL3 and NVFP4 variants — broad **independent confirmation** GLM-5.3 fits 2× Spark; several are EXL3/TR3 4bpw (≈ tony's EXL3 alt). |

> **Reconciliation:** tony's 52.2 code / 18.8 prose and sfxnz's structured 68 tok/s are the
> *drafter-boosted* figures; kingjones30's 19.6 prose is a *single-stream MTP-5* floor (same
> ballpark). The DFlash2-vs-MTP split is deliberate, not disagreement. The **`--language-model-only`
> OOM fix** is the new must-have GLM flag → added to checklist below. GLM keeps the
> `RedHatAI/GLM-5.3-Flash-NVFP4` (corruption-safe) default; sfxnz/kingjones run the ModelOpt
> `LibertAIDAI`/`nvidia` packs, so keep the corruption note as the tiebreaker.

#### Vision-Exp — sparser but consistent

| Repo | Stars | Confirms / improves |
|---|---:|---|
| shaircast/deepseek-v4-vision-dual-dgx-spark | 0 | TP2, native vision, tools, structured output on 2× Spark — confirms the §2 lane. |
| FlyCockpit/DeepSeek-V4-Vision-2x-DGX-Sparks | 8 | vision for V4-Flash on 2× Spark. |
| tonyd2wild/DeepSeek-V4-Flash-Vision-SGLang-DGX-Spark | 3 | SGLang TP2 (2 Sparks), real-prompt bench. |

> Vision-Exp has the thinnest independent tail (most authors target the text flagship or
> GLM); tonyd2wild's dspark-1M repo remains the anchor for §2, and alexellis/Weschera confirm
> the shared DS checkpoint + `nvfp4_ds_mla` KV path it uses.

---

## 6. Multi-Token Prediction (MTP) / speculative decoding — enable it, it's the point

On a **memory-bandwidth-bound** device (GB10 ~273 GB/s), decode is gated by how fast weights
are fetched — MTP/speculation amortises that fetch across K tokens per forward pass.

### What vLLM supports (speculative.py, VERIFIED)

```
method: ngram | ngram_gpu | medusa | mlp_speculator | draft_model | suffix
      | eagle | eagle3 | custom_class | mtp | deepseek_mtp | glm4_moe_mtp
      | glm5_next_mtp | qwen3_next_mtp | qwen3_5_mtp | qwen4_exp_mtp
      | longcat_flash_mtp | kimi_k3_mtp | dspark | dflash
```

### Measured wins (VERIFIED, low/medium QPS)

- MTP-native: **1.5–2.5×** decode throughput (GLM-5 in production: 186+ tok/s).
- **dspark** (n-gram offload, NVFP4/MoE-optimised): Kimi-K2.6 **2.55×**, K2.7-Code **2.36×**.
- DeepSeek-V4-Flash-DSpark on Spark: **57.1 tok/s** decode.

### When to bother vs. not

| Load | MTP verdict |
|---|---|
| Single-stream / a few concurrent requests | ✅ **Big win** — enable |
| High concurrency (>8 in-scheduler) | ⚠️ GPU already saturated; diminishing returns |
| Code / structured / repetitive output | ✅ dspark shines (n-gram match) |

### Wiring into a profile

```json
"advanced": { "speculative_config": {
  "method": "mtp",
  "num_speculative_tokens": 3,
  "num_speculative_tokens_per_batch_size": [[1, 4, 3], [5, 8, 2], [9, 16, 1]]
}}
```

`num_speculative_tokens_per_batch_size` (dynamic K) is the smart default — keep K high for
1–4 concurrent requests, taper as the GPU saturates. For DeepSeek the verified sweet spot is
**K=2** (K=3 is a measured negative — see §1); GLM/Qwen validate **K=3–5**.

---

## 7. Decision summary

| Model | Params (a/t) | Deploy quant (repo) | Topology | Weight/node | Fits dual? | Community-verified perf |
|---|---:|---|---:|---|---|---|
| **DeepSeek V4 Flash 0731** | 284B/13B | FP8+FP4 native `deepseek-ai/...-0731` = 155.4 GiB | **TP2** | ~77.7 | ✅ | **40–62 t/s (tony) up to 75 (raullenchai) / 83.8 K7 (Weschera)**, 92 agg @8 |
| **DeepSeek V4 Flash Vision-Exp** | 284B/13B + vision | base = 156.3 GiB (no NVFP4 published) | **TP2** | ~78.2 | ✅ | **1M ctx, KV 2.9M tok, MTP K=5 (patch), ~1 s/vision** |
| **Qwen 3.8 Flash-Next** | 125B/~6B act. MoE | NVFP4 `nvidia/Qwen3.8-Flash-Next-NVFP4` = 123.6 GiB | **TP2 SPEED** (or **REPLICATED**) | ~61.8 (or ~76/node) | ✅ | **53.7–55.8 tok/s (TP2+EP+MTP3+graphs), agg 98–126; 5.87M KV CONTEXT lane** |
| **GLM 5.3 Flash** | 320B/18B | NVFP4 **`RedHatAI/GLM-5.3-Flash-NVFP4`** = ~190 GiB (compressed-tensors) | **TP2 + EP** | ~95.2 | ✅ | **52.2 code / 18.8 prose tok/s, DFlash2 K=7, 262K** |
| **DeepSeek V4.1 Flash** | ~748B (incl. 196B Engram) | 711 GiB BF16 | — | 187+ even @NVFP4 | ❌ **defer** | fit-bound; wants 4× Sparks |

> **Note (community upgrade):** Vision-Exp ships `num_nextn_predict_layers: 3` and the
> tonyd2wild deploy runs **K=5** — fine because the `spec-dspark.py` patch is mounted
> (K=3 without it *halves* draft acceptance). Qwen now has **two** verified answers:
> TP2-SPEED for low latency / big pool, or REPLICATED for max concurrent throughput (fits one
> node). GLM: deploy **RedHatAI** (compressed-tensors), **not** ModelOpt NVFP4 (tokens corrupt).

**Boot/fallback:** pin `deepseek-v4-flash-tp2` as the default alias with a
`qwen3.8-flash-tp2` fallback source for when Node B is down — the gateway auto-503s during
switch, and an explicitly configured single-node fallback profile prevents a silent topology
change if a Spark is unavailable.

---

## 8. Phase 0 validation checklist (no-launch)

- [ ] Pin each `model_revision` to the 40-char commit sha (not `main`).
- [ ] Pin container image by digest; verify flags against the pinned build (`--nnodes/--node-rank/--headless`, `--default-chat-template-kwargs`).
- [ ] Verify NVIDIA driver version is **identical on both nodes** (2.4× perf gap if mismatched).
- [ ] Confirm QSFP RoCE wiring: pin `NCCL_IB_HCA` to the *cabled* port (`rocep1s0f0`) and set `NCCL_SOCKET_IFNAME` — only 1 of 2 RoCE HCAs is connected.
- [ ] Register real `ModelSpec` via `PUT /api/v1/system/model-specs` (config.json + safetensors `total_size`) so the planner fits-check is exact, not ESTIMATED.
- [ ] Confirm the chosen image ships the FP4/MLA kernels (`sm_121a`) and `eugr`/`dspark` path for DeepSeek (upstream vLLM lacks SM12x; use jasl fork or Anemll image, e.g. `ghcr.io/anemll/dspark-vllm-gx10:0.1.2`).
- [ ] GLM: add `--language-model-only` + `--moe-backend marlin`; verify the pinned image passes the FlashInfer sparse-MLA `pe_dim=64` assert + GLM top-k table fix (kingjones30); include the vision `chat_template_mm.jinja`.
- [ ] Qwen speed lane: add `enable_expert_parallel: true` + FULL_DECODE_ONLY CUDA graphs (getrefined/MiaAI-Lab reach agg 126 tok/s that way, not plain TP2).
- [ ] Dry-run activate each profile against `runtime_mode: dry-run`, preflight both nodes.
- [ ] NCCL/QSFP bandwidth + latency on the 200G link before any TP2 launch.
- [ ] Double-check `--enable-expert-parallel` is supported by the pinned image before GLM TP2+EP.
- [ ] RDMA/container: `--shm-size 10g --ulimit memlock=-1` + `/dev/infiniband` mount mandatory.

---

*Sources: HF `config.json` + `model.safetensors*.json` file listings for the four concrete
models — `deepseek-ai/DeepSeek-V4-Flash-0731` (FP8+FP4 native),
`deepseek-ai/DeepSeek-V4-Flash-Vision-Exp`, `Qwen/Qwen3.8-Flash-Next` + official `-FP8`,
`nvidia/Qwen3.8-Flash-Next-NVFP4`, `zai-org/GLM-5.3-Flash` (FP8 native),
`RedHatAI/GLM-5.3-Flash-NVFP4` — plus prior DeepSeek-V4.1-Flash / Qwen3.8-27B / GLM-4.5 notes;
vLLM `vllm/config/speculative.py` + speculative-decoding docs; Route179, Flowtivity, MiaAI-Lab,
al-engr DGX-Spark deployment blogs; **community dual-Spark set-ups: the tonyd2wild recipe
family (deepseek-v4-flash-dgx-spark, the two DeepSeek 1M/dspark repos, the Vision-Exp
dspark-1M, Qwen3.8-Flash-Next-NVFP4, GLM-5.3-Flash-NVFP4-DFlash2-2x, GLM-5.3-EXL3, and the
V4.1 vLLM repo); independent confirm/cross-checks: raullenchai/twinspark (DS ~75 tok/s),
Anemll/dspark-vllm-gx10, alexellis, Weschera (DS K7 83.8), MiaAI-Lab Dual-DGX-Sparks (Qwen,
373★), getrefined (Qwen agg 126), PixelML, kingjones30 (GLM day-0), sfxnz (GLM DFlash2),
Reederey87, Enntity, bird/DSv4-Flash-spark; eugr/spark-vllm-docker, jasl/vllm fork, Anemll
dspark image**; Spark Arena + Novita AI + Baseten benchmarks. All weight figures are the
**published repo file sizes** (not estimates); all performance figures are **measured in the
cited community set-ups**.
Companion research file: [glm-mtp-research.md](glm-mtp-research.md).*
