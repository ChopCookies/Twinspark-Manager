# Benchmarks

TwinSpark measures the served model the way your apps use it: streamed chat completions through the gateway.
The load comes from the workstation client (or from `tsm bench` on node A), so nothing extra runs on the
Sparks. Every result is stored on node A against the exact revision that served it and shows up in that
revision's [validation record](validation.md).

## Running one

**Workstation client.** Open **Benchmarks** in the client's GUI and choose **Run a benchmark**. The run goes
through the client's gateway forward, so it includes the network between your computer and node A, like an
app on that computer. **Compare two revisions** switches node A to one revision, measures, switches to the
other, measures, and switches back.

**On node A:**

```bash
tsm bench run max-throughput                       # the default alias, 1024 prompt / 128 generated tokens
tsm bench run latency-slo --slo-ttft-ms 2000 --slo-tpot-ms 80
tsm bench run context --contexts 8192,32768,131072
tsm bench run quality --alias coder
tsm bench run fixed --levels 1,4,16 -o before.json # the same levels every time
tsm bench list --profile glm-5.3-flash-nvfp4-tp2
tsm bench show benchmark-1a2b3c4d5e
```

`tsm bench run` finds the gateway on this node's port and reads the inference key from this node's vault
(`--gateway URL` and `--inference-key`, or `TSM_GATEWAY` / `TSM_INFERENCE_KEY`, for anything else). The
management key is only used for node A's API, never sent to the gateway.

## Presets

| Preset | What it does | Result |
|---|---|---|
| **max-throughput** | doubles the concurrency from 1 while another doubling still adds at least 5 % output throughput and errors stay under 5 % | the peak output tokens/s and the *knee*: the lowest concurrency within 5 % of the peak |
| **latency-slo** | doubles the concurrency until a level misses the TTFT p95 or TPOT p95 target, then searches between the last good and the first bad level | the highest concurrency that meets both targets |
| **context** | the ladder 8K / 32K / 128K / 262K / 512K / 1M up to the deployed `max_model_len`, at concurrency 1, 2 and 4; at each length a retrieval check | throughput per length, and how many hidden facts the model found |
| **quality** | several facts hidden throughout the longest prompt that fits, then a tool call and the continuation after the tool result | retrieval score and the tool call verdict |
| **fixed** | the levels you choose, nothing adaptive | the same numbers every time: for comparing revisions |

A large nominal context is not proof that the model uses it: the retrieval check hides several facts at
different depths of a long prompt and asks for all of them. *All found* is the claim the context length
supports; finding only the last ones is a common failure of long contexts.

## What is measured

Per level: requests, output tokens/s over the level, TTFT (time to the first generated token), TPOT (time per
output token after the first), inter-token latency, p50 / p95 / p99 of each, the real prompt and output token
counts from the server's `usage`, and the error rate. During the run the client polls node A's status every
2 s for the lowest free memory per node and the GPU power (and energy over the run). That power is the GPU
sensor, not the wall plug.

How the numbers stay comparable:

* **Exact lengths.** Every request asks for exactly the output length (`min_tokens` = `max_tokens`,
  `ignore_eos`), so a model that stops early does not look fast. The real counts are stored next to the
  requested ones.
* **No flattering cache.** Each run has a fresh random seed and each request a fresh prompt, so vLLM's prefix
  cache never replays an earlier prompt. `--cache warm` sends the same prompt every time on purpose, to
  measure the cache itself. `--seed` repeats a run's prompts exactly.
* **One model per run.** The client checks the revision behind the alias after every level and node A's
  switch state. A switch during the run marks it **invalid** with the reason; invalid results are stored as
  such and never count as a measurement.
* **Enough requests.** Each level runs at least as many requests as its concurrency (default 4 times the
  concurrency, at least 8), so the level really has that many in flight.

Requests from your apps during a run share the model and make the numbers worse; run benchmarks when the
model is otherwise idle.

## Evidence class

A stored benchmark is **measured by the client**: real requests through the gateway, reported by the client
that sent them. Node A checks the document (format, sizes, finite numbers, a revision it knows) but cannot
re-check the timings. It is listed in the revision's validation record under *results → benchmarks*.

The result document (`twinspark-bench/1`) contains the options, the seed, every level, the summary, the
telemetry, the revision id and the client version. `GET /api/v1/benchmarks[?profile=&revision=]` lists the
stored results and `GET /api/v1/benchmarks/{id}` returns one with its document.
