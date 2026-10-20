# Performance lab: models on two DGX Sparks

What runs on two NVIDIA DGX Sparks (GB10, 2 × 128 GB unified memory, 200 Gb/s QSFP between them), and how
fast. Every row is a Cookbook recipe: import it, pin it, switch to it, and measure it yourself with the same
settings.

**Read the numbers with their source.** *Measured by* is who measured: the recipe's author, with their own
launcher, unless the row says otherwise. *Status* is the recipe's verification: `verified` (ran on the
maintainers' two Sparks — the recipe's notes say how), `community` (a published, measured dual-Spark
recipe), `experimental` (derived or not yet tested). None of these numbers was measured by `tsm bench` yet:
that is what the validation runs of 0.7 add, row by row. Your numbers depend on the image, the model revision and the prompt — compare like with
like with [`tsm bench`](benchmarks.md).

## Results

<!-- results:start (scripts/build_perf_lab.py) -->
| Recipe | Name | Mode | Nodes | Status | Measured | Measured by |
|---|---|---|---|---|---|---|
| DeepSeek V4 Flash 0731 — B12X + DSpark (eugr recipe) | `deepseek-v4-flash-0731-b12x` | — | tp2 | verified | context: max-model-len auto → 1,048,576 tokens on two DGX Sparks<br>speculation: DSpark, 5 tokens, probabilistic drafting<br>gpu-memory-utilization: 0.85 | [eugr/spark-vllm-docker (MIT licence) — snapshot of 2026-09-28, run by the TwinSpark maintainers](https://github.com/eugr/spark-vllm-docker/blob/main/recipes/deepseek-v4-flash-0731.yaml) |
| GLM-5.3-Flash NVFP4 (RedHatAI) — TP2 + DFlash2 k=7 | `glm-5.3-flash-nvfp4-dflash2-tp2` | Latency — DFlash2 k=7 | tp2 | experimental | one request, tok/s: 46.9<br>6 at once, tok/s total: 56.2<br>structured output, tok/s: 54-61 (temp 0)<br>KV cache: 581,040 tokens at 262K<br>start-up: ~15 min | [seanlinmt / tonyd2wild (recipe + image), incoai (drafter)](https://github.com/seanlinmt/GLM-5.3-Flash-NVFP4-DFlash2-2x-DGX-Spark) |
| GLM-5.3-Flash NVFP4 (RedHatAI) — TP2 + DFlash2 k=7 | `glm-5.3-flash-nvfp4-dflash2-tp2` | Throughput — no drafter, 8 sequences | tp2 | experimental | not measured yet | [seanlinmt / tonyd2wild (recipe + image), incoai (drafter)](https://github.com/seanlinmt/GLM-5.3-Flash-NVFP4-DFlash2-2x-DGX-Spark) |
| GLM-5.3-Flash NVFP4 (RedHatAI) — TP2 + MTP3 | `glm-5.3-flash-nvfp4-tp2` | Latency — MTP k=3 | tp2 | community | one request, tok/s: 26.5<br>4 at once, tok/s total: 57.7<br>8 at once, tok/s total: 76.2<br>KV cache: 7.48 GiB ≈ 1.15M tokens at 262K<br>start-up: ~13-15 min (checkpoint load dominates) | [himorishige (settings), tonyd2wild (image), RedHatAI (checkpoint)](https://github.com/himorishige/glm53-flash-2x-dgx-spark-recipe) |
| GLM-5.3-Flash NVFP4 (RedHatAI) — TP2 + MTP3 | `glm-5.3-flash-nvfp4-tp2` | Throughput — no speculation, 8 sequences | tp2 | community | not measured yet | [himorishige (settings), tonyd2wild (image), RedHatAI (checkpoint)](https://github.com/himorishige/glm53-flash-2x-dgx-spark-recipe) |
| MiMo-V2.6-Flash-RL — TP2, 300K, fp8 KV | `mimo-v2.6-flash-tp2` | — | tp2 | experimental | one request, tok/s: 53.3<br>6 at once, tok/s total: 155.8<br>time to first token, s: 0.37<br>KV cache: 1.87M tokens (12.6 GiB, fp8, 300K)<br>note: numbers include the bundled DFlash drafter (k=7); without it expect roughly half on code | [tonyd2wild (recipe + image), Plaaasma / MiaAI-Lab (alternatives)](https://github.com/tonyd2wild/MiMo-V2.6-Flash-2x-DGX-Spark) |
| Qwen3.8-Flash-Next NVFP4 — TP2+EP, MTP3 | `qwen3.8-flash-next-nvfp4-tp2` | — | tp-ep | experimental | one request, tok/s: 55.8<br>8 at once, tok/s total: 126.1 | [getrefined (vLLM lane), MiaAI-Lab (SGLang lane)](https://github.com/getrefined/Qwen3.8-Flash-Next-NVFP4-vLLM-DGX-Spark) |
<!-- results:end -->

*tok/s* is output (decode) tokens per second; *N at once* is the total over N parallel requests. A mode without
numbers has not been measured yet — that is where a benchmark of yours helps most.

## Measuring a recipe yourself

```bash
tsm cookbook import glm-5.3-flash-nvfp4-tp2 --mode throughput --as glm-batch
tsm pin glm-batch && tsm activate glm-batch
tsm bench run max-throughput --alias default        # the knee: where more parallel requests stop paying
tsm bench run latency-slo --alias default           # the most requests that still meet a TTFT/TPOT target
tsm bench run context --alias default               # speed and retrieval as the context grows
tsm validation export glm-batch --markdown         # the validation record: what exactly was measured
```

The presets, the knee and the methodology are in [docs/benchmarks.md](benchmarks.md); what a validation record
contains in [docs/validation.md](validation.md). Results are stored against the exact revision, so a
comparison names what changed between two runs.

## Adding results here

- **Share the result** from the profile (*Share result…*, or `tsm cookbook report <profile>`): the Cookbook's
  community totals then include it ([docs/cookbook.md](cookbook.md#community-results)).
- **Open a recipe report** on GitHub (*New issue → Recipe report*) with the validation record and the
  benchmark: a recipe whose numbers TwinSpark reproduced becomes `verified`, and its row here shows them.
- **Correct a recipe** with a pull request: the recipe file, `python scripts/build_catalog.py` and
  `python scripts/build_perf_lab.py` — every installation gets the corrected recipe from the
  [catalog](../catalog/README.md) without an update.
