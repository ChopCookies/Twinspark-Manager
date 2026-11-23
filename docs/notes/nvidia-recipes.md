# What changed for two Sparks, late October to November 2026

A survey on 2026-11-23 of the sources TwinSpark's recipes come from: NVIDIA, eugr/spark-vllm-docker, Mia's AI
Lab and the DGX Spark forum. It records what became part of TwinSpark and why the rest did not.

## NVIDIA publishes its own DGX Spark recipes

The first public commit of [NVIDIA/local-ai-inference-recipes](https://github.com/NVIDIA/local-ai-inference-recipes)
is from 2026-11-18 (release v0.69). It holds 27 models and 131 recipes for DGX Spark, DGX Station, RTX PRO and
GeForce. Each recipe is one `recipe.yaml` per model, machine, engine and precision, with a `performance.yaml`
of NVIDIA's own measurements. eugr added a runner for this format on 2026-11-17 (`run-nvidia-recipe.py`).

TwinSpark now imports these files and lists the two-Spark ones in Cookbook → Community
([docs/cookbook.md](../cookbook.md#nvidias-recipes)). The recipe files are marked "NVIDIA Proprietary", so
TwinSpark does not copy them. It reads them from GitHub when you browse, like eugr's.

### The two-Spark recipes

NVIDIA's measurement: SPEED-Bench coding prompts, one request at a time, 8,192 output tokens.

| Model | Image | Speculation | tok/s | In TwinSpark |
|---|---|---|---|---|
| Qwen3.8-27B NVFP4 | `vllm/vllm-openai:v0.31.0` | DFlash2 drafter (z-lab), 8 tokens | 54.7 | **built in**: `qwen3.8-27b-nvfp4-dflash2-tp2` |
| Qwen3.8-Flash-Next FP4-Local | `eugr/spark-vllm-b12x:nightly-20261015` | MTP, 4 tokens | 59.4 | Community |
| DeepSeek-V4-Flash-0731 | `eugr/spark-vllm-b12x:latest` | DSpark, 5 tokens | 55.7 | Community; built in as `deepseek-v4-flash-0731-b12x` (eugr's recipe on a local image) |
| DeepSeek-V4-Flash-Vision-Exp | `eugr/spark-vllm-b12x:latest` | DSpark, 6 tokens | 45.1 | Community |
| Qwen3.6-27B NVFP4 | `vllm/vllm-openai:v0.31.0` | MTP, 5 tokens | 54.9 | Community |
| GLM-5.3-Flash FP4-Local | `eugr/spark-vllm-b12x:nightly-20261015` | MTP, 5 tokens | 33.9 | Community; built in: two RedHatAI NVFP4 recipes |
| DeepSeek-V4-Flash | `eugr/spark-vllm:latest` | MTP, 2 tokens | 32.9 | Community |
| MiniMax-M2.7 NVFP4 | `eugr/spark-vllm:nightly-20261013` | none | 26.4 | Community |

**Why only Qwen3.8-27B is built in.** It is the only new model among them that runs on a public, versioned
image with standard weights. Qwen3.6-27B on the same image is the previous generation of the same size. The FP4-Local and DeepSeek lanes run on eugr's B12X images, which NVIDIA marks
as community builds. Two of them are tagged `latest`, and nightly tags are not kept forever. Built in, such a
recipe would fail one day with an image that no longer exists. From Community, Pin shows whether the tag is
still there when you import. MiniMax-M2.7 is slower than the built-in recipes and not new.

**Memory.** NVIDIA's DeepSeek and GLM lanes use `gpu-memory-utilization` 0.85 and 0.87. eugr lowered both
recipes to 0.8 on 2026-10-26 "to leave room for host and distributed-worker allocations in Spark's shared
RAM". TwinSpark's built-in DeepSeek recipe follows that now. Mia's AI Lab measured MemAvailable below 1 GiB
at 0.835 on the Qwen3.8-Flash-Next lane, where GB10 boxes hard-reset. Watch MemAvailable on both Sparks when
you first start an imported recipe above 0.8.

## eugr/spark-vllm-docker

No new recipe files since 2026-10-15. Changes since late October:

- 2026-10-26: DeepSeek-V4-Flash-0731, -Vision-Exp and GLM-5.3-Flash lowered to `gpu_memory_utilization`
  0.8 (GLM also with `PYTORCH_ALLOC_CONF=per_process_memory_fraction:0.8`). TwinSpark's built-in DeepSeek
  copy takes this over. Its other changes since 2026-09-28 (B12X loader, renamed attention backend, no mod)
  need a rebuilt image. Import the current recipe from Community for those.
- 2026-10-30: the Qwen3.8-Flash-Next cluster recipe keeps its PLE tables on the GPU.
- 2026-11-16 to 11-23: build fixes (PyTorch 2.14, FlashInfer, cuBLASLt FP8 autotune agreement across ranks)
  and the NVIDIA recipe runner.

TwinSpark's eugr importer now stages a drafter that a recipe names in `--speculative-config` (`"model": …`,
or the dotted `--speculative_config.model …`). Before, an imported recipe with a drafter pinned only the
target model. Its start then failed offline, unless the drafter happened to be in the Hugging Face cache.

## Mia's AI Lab

No new commits to the two-Spark repositories since TwinSpark's kits were made. DeepSeek-V4.1-Flash-1017 was
last changed on 2026-11-09, the TensorFold Qwen on 2026-10-26. The Qwen3.8-Flash-Next vLLM lane is unchanged
since 2026-09-29. Their newest published recipe is for one Spark (Qwen3.8-Flash-Next, 1M context, 37 tok/s
for one request). It runs with their own scripts on a single Spark and is not added.

## Other models

- **Inkling 2 Small** (Thinking Machines; vLLM day-0 support on 2026-11-12): 594B parameters, 17B active.
  NVFP4 and MXFP8 together are more than the 256 GB of two Sparks.
- **Inkling Small** (276B, NVFP4) fits two Sparks, but its two-Spark lanes need patches. vLLM needs an
  attention fallback for sm_121 and a load-time memory reclaim. eugr's recipe needs two mods, and its KV cache
  is BF16 only (about 300K tokens). It is not added.
- **vLLM 0.32.0** (released in the last weeks; the exact date was not confirmed): NVFP4 KV cache on SM12x,
  Qwen3.8-Flash-Next QSA GEMM fusion. No two-Spark measurements yet. `qwen3.8-flash-next-nvfp4-tp2-vllm030` stays on 0.30.0 until someone measures
  0.32.
