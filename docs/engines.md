# Engines and image checks

A profile names two different things:

- the **image family** (`runtime_adapter`: eugr, NGC, Hydra, …): where the container image comes from and
  which recipes it was written for;
- the **engine** (`engine`: `vllm` or `sglang`): the program inside the container that serves the model.

Until 0.7 every container ran `vllm serve`. The engine is now a field of the profile (Quick settings →
*Engine*, or `"engine": "sglang"` in the full draft), and the launch planner turns the same settings into
that engine's flags.

## What an engine provides

| | vLLM | SGLang (experimental) |
|---|---|---|
| plan | `vllm serve <repo> --revision <sha> …` | `python3 -m sglang.launch_server --model-path <repo> --revision <sha> …` |
| two nodes | `--nnodes 2 --node-rank N --master-addr`, worker `--headless` | `--nnodes 2 --node-rank N --dist-init-addr A:port` (rank 1 serves no API) |
| capabilities | probed from the image (below) | probed from the image (below) |
| start / stop | the agent's container runtime (same for both) | same |
| health | `GET /health` | `GET /health` |
| metrics | `vllm:*` on `/metrics` | `sglang:*` on `/metrics` (`--enable-metrics` is always set) |
| KV pool | `GPU KV cache size: N tokens` in the log | `KV Cache is allocated. #tokens: N` in the log |

Settings and their SGLang flags: context length → `--context-length`, memory share →
`--mem-fraction-static`, parallel sequences → `--max-running-requests`, max batched tokens →
`--chunked-prefill-size`, KV dtype `fp8` → `--kv-cache-dtype fp8_e4m3`, block size → `--page-size`,
prefix cache off → `--disable-radix-cache`, eager → `--disable-cuda-graph`, parsers → `--tool-call-parser`
/ `--reasoning-parser`, speculative `eagle`/`eagle3`/`mtp`/`ngram` → `--speculative-algorithm
EAGLE/EAGLE3/NEXTN/NGRAM` with the draft model and step counts. Settings without an SGLang equivalent
(compilation config, chat-template kwargs, generation overrides) are listed in the plan's notes instead of
being dropped silently. Raw engine arguments (`extra_vllm_args`) are passed as written. Mods patch vLLM and
are refused for SGLang profiles; the Ray backend too (SGLang starts both nodes itself).

The backend key reaches SGLang as `--api-key "$VLLM_API_KEY"`: the agent injects the key only into that
environment variable, so the plan never contains it.

Measurements (KV size, startup footprint) are kept per engine: a vLLM measurement never calibrates an SGLang
start.

## The image check before a switch

A pull that worked says nothing about whether the image knows a flag, a parser or the model. During
*resolving* — before anything is drained or stopped — the agent runs a fixed script from TwinSpark inside
the pinned image (`docker run --rm --network none`, no GPU, no mounts, 6 GiB memory cap) that prints what
the engine's own registries contain: its version, every serve flag, the tool-call and reasoning parsers,
the speculative methods and the model architectures it can load. The controller compares that with the
plan:

- every flag in the command;
- the tool-call parser (with tool calling on) and the reasoning parser;
- the speculative method;
- the model's architecture from its `config.json`.

`runtime.image_check` in controller.yaml decides what happens when something is missing: `enforce`
(default) fails the switch at *resolving* with the list ("nothing was stopped"); `warn` notes it and goes
on; `off` skips the probe. Profiles with mods only get warnings: a mod may add what the plain image lacks.
A probe that cannot run (an older agent, an image without Python) is a warning, never a failure. With less
than 10 GiB free next to the running model the probe is skipped, also with a warning. Answers are kept per
image digest and engine, so each image is asked once.
