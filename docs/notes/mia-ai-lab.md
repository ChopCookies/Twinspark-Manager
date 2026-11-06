# Mia's AI Lab recipes — what TwinSpark takes over

> **Research note (2026-11).** I read the two-Spark recipes listed on
> [mia-ai.net/models](https://mia-ai.net/models?for=spark%3A2) at the commits named below. Nothing here
> was run on real Sparks. The numbers are the source's own measurements.

Mia's AI Lab ([github.com/MiaAI-Lab](https://github.com/MiaAI-Lab)) publishes launch kits for two DGX Sparks.
Each kit is a `start.sh` plus patches, and each publishes measurements. Some run on vLLM or SGLang, the
engines TwinSpark drives. The fastest run on TensorFold, a separate engine written in Zig. Several use
EXL3 weights.

**EXL3** is ExLlamaV3's trellis quantization. It allows any average bitrate. At 2.9 bits per weight,
DeepSeek V4.1 Flash takes about 99.5 GiB per Spark, which fits two Sparks; NVFP4 did not
([researched profiles §5](researched-profiles.md#5-deepseek-v41-flash-interesting-but-does-not-fit-dual-spark-verdict-defer)).
vLLM does not load EXL3 by itself: the lanes either overlay ExLlamaV3 kernels onto a vLLM image or run TensorFold.

## The lanes

| Model, lane | Engine and image | Weights | One stream, tok/s | License | In TwinSpark |
|---|---|---|---|---|---|
| Qwen3.8 Flash Next, vLLM 0.30 | `vllm/vllm-openai:v0.30.0`, stock | NVFP4 | 52.5 prose / 61.5 code | AGPL-3.0 | **recipe `qwen3.8-flash-next-nvfp4-tp2-vllm030`** |
| Qwen3.8 Flash Next, day-0 vLLM | `vllm/vllm-openai:qwen38-flash-next` + 3 patch scripts | NVFP4 | 56.1 / 75.8 | AGPL-3.0 | no: needs its patches (PLE, MXFP8, FP8 KV) |
| MiMo V2.6 Flash, SGLang | `lmsysorg/sglang` nightly 2026-09-21 (+ ffmpeg for audio and video) | MXFP4 | 35.0 / 46.0 (EAGLE MTP) | AGPL-3.0 | no: needs a Python patch in every SGLang process |
| DeepSeek V4.1 Flash, vLLM + EXL3 | `ghcr.io/miaai-lab/deepseek-v4.1-flash-exl3-2x-dgx-sparks:2.9bpw` (public) | EXL3 2.9 bpw | 31.6 | AGPL-3.0 | no: needs Engram tables mounted, plus the kit's memory steps |
| DeepSeek V4 Flash Vision-Exp, DSpark | `ghcr.io/anemll/dspark-vllm-gx10:0.1.1` + ~35 hotfixes | NVFP4 KV | 38.6 | MIT | no: TwinSpark has `deepseek-v4-flash-0731-b12x` on the eugr path |
| DeepSeek V4.1 Flash 1017, TensorFold | TensorFold (Zig), built on each node | EXL3 2.9 bpw | 80.7 / 118.1 | Apache-2.0 | no: another engine |
| Qwen3.8 Flash Next, TensorFold | TensorFold, MTP | NVFP4 / INT4 | 67.4 | not checked | no: another engine |
| GLM 5.3 Flash, TensorFold | TensorFold | EXL3 4-bit | 47.8 | not checked | no: another engine |

Commits read: Qwen3.8 `cd839d0b`, MiMo `201be3e7`, DeepSeek V4.1 EXL3 `6f7d1590`, DeepSeek V4.1 1017
`31457d05`, DeepSeek V4 DSpark `a8b46367`. Licenses are from each repository's `LICENSE` file.

## What TwinSpark takes over, and how

TwinSpark is MIT-licensed. A recipe carries settings: the flags, environment values and measurements of a
published launch, with credit and a link. It does not carry the kit's scripts or patches. A patch from an
AGPL-3.0 repository is not copied into TwinSpark. If a recipe needs one, the recipe pins it like the GLM
patch (`source.mod_artifacts`, see [recipe workflow](../recipe-workflow.md#required-patches)). Node A
then fetches it from upstream at a fixed commit.

### Qwen3.8 Flash Next on stock vLLM 0.30: ported

The kit's `start-v030.sh` runs the nvidia checkpoint on the unmodified vLLM 0.30.0 image. The recipe
takes its flags and environment, except one optional overlay:

- the reduced draft vocabulary, which measured 5–15% faster decode, is left out;
- BF16 KV, because v0.30 accepts nothing else without a backport;
- `gpu-memory-utilization` 0.80;
- `VLLM_USE_BREAKABLE_CUDAGRAPH=0`;
- FlashInfer autotuning inside the container;
- a bfloat16 SSM state.

The existing `qwen3.8-flash-next-nvfp4-tp2` recipe drops from 0.85 to 0.80 for the same reason as the new
one. At 0.835 the kit measured 0.3–0.9 GiB `MemAvailable` on both nodes under load, the range where GB10
boxes hard-reset.

### What the others would need

- **SGLang patches.** MiMo's SGLang lane puts `patches/sitecustomize.py` on `PYTHONPATH`. Without it the
  MTP draft fails to load at TP2, and weights load as zero-copy views of the mmap'd shards. TwinSpark's
  mods patch vLLM only and are refused for SGLang profiles. A pinned patch could fetch the file and
  mount it as a `sitecustomize.py` for SGLang.
- **A second checkpoint as a path.** The vLLM EXL3 lane reads DeepSeek's Engram tables from shards 47 and 48
  of `deepseek-ai/DeepSeek-V4.1-Flash` (~190 GiB). It passes their directory through
  `--hf-overrides '{"engram_table_dir": …}'`. TwinSpark can stage extra repositories (`extra_models`)
  but cannot yet substitute one's path into a flag. The kit's `start.sh` also checks a boot margin, drops
  the page cache after loading, and pins the KV pool in bytes. Its speed (31.6 tok/s) is below the
  TensorFold lane of the same model.
- **A TensorFold engine.** TensorFold serves an OpenAI-compatible API. It starts two ranks
  (`--tp 2 --rank R --master HOST`), and each rank needs its own preprocessed weight file. Its image is
  built from source on each Spark, and Engram's tables must be on local disk. TwinSpark would need a third
  engine next to vLLM and SGLang, with these preparation steps. These are the fastest lanes listed.
- **The DSpark hotfixes.** The DeepSeek V4 Vision-Exp kit applies about 35 hotfix scripts and an
  overlay of vLLM files at start. They are MIT-licensed, so they could ship as a mod. TwinSpark's
  existing DeepSeek V4 Flash recipe covers the model on another path.

## Findings for TwinSpark itself

- **Persisted FlashInfer caches.** TwinSpark mounts `~/.cache/flashinfer` and `~/.cache/vllm` from each node.
  The Qwen kit saw TP2 deadlock at the autotune step once when the two nodes' autotune caches differed.
  The new recipe keeps that cache in the container. Whether other two-node recipes need the same is not
  known yet.
- **Memory share on GB10.** Above about 0.80, a busy two-node deployment can leave less than 1 GiB
  `MemAvailable`. Recipes in the cookbook that use more are worth checking against a soak test.
