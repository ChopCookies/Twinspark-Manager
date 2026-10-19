# Validation records and the hardware matrix

TwinSpark's control path is covered by automated tests, but tests do not prove that a model runs on two
DGX Sparks. This page keeps the two apart and lists what has really been run.

## Evidence classes

| Class | Means | Says nothing about |
|---|---|---|
| **manager-validated** | TwinSpark activated the revision on real hardware: the containers started, the health check and the smoke test (chat, streaming) passed, and the gateway routed to it | anything the record does not list (other contexts, other images, long runs) |
| **dry-run** | TwinSpark walked the whole pipeline with simulated containers | whether the model starts at all |
| **external** | the recipe's notes say it ran outside TwinSpark, for example with its author's launcher | whether TwinSpark at this version can run it |
| **none** | nothing has run | — |

A recipe that "works externally" and a manager version that "ran it" are separate facts; a record never
upgrades one into the other. Benchmark results carry their own class, **measured by the client** (real
requests through the gateway, reported by the client that sent them; see [benchmarks](benchmarks.md)).

## Exporting a record

```bash
tsm validation export glm-5.3-flash-nvfp4-tp2                 # JSON: the pinned revision's latest activation
tsm validation export glm-5.3-flash-nvfp4-tp2 --job act-1a2b3c --markdown -o glm.md
```

Also `GET /api/v1/profiles/{name}/validation[?revision=&job=&format=markdown]`. A record
(`twinspark-validation/1`) contains:

- **manager**: TwinSpark version and the git commit it was installed from (`install.sh` writes
  `/opt/twinspark/source-commit`, marked `dirty` for local edits);
- **nodes**: each agent's version, runtime mode, kernel, NVIDIA driver and CUDA versions;
- **deployment**: per model part the model commit, quantization, image and digest, vLLM version, topology,
  context, concurrency, KV dtype, speculative settings, extra models, the mods with their hashes on each
  node, and the exact container commands (no environment, so no secrets);
- **procedure**: the job, every stage with its duration and message, errors, rollback and warnings;
- **results**: vLLM's KV pool, where the KV size came from, the startup footprint per node, the failure
  classification of a failed start, benchmark runs and evaluation records of the revision.

Nothing in a record identifies your network: host names are left out, IP addresses and home directories in
the commands are replaced by placeholders, and no keys are included.

## The matrix

| Date | Case | Revision | Topology | Context | Evidence | Record |
|---|---|---|---|---|---|---|
| 2026-09-28 | Small single-node and two-node smoke test (SmolLM2-135M) | — | single node, pp2 | — | manager-validated (the code of that day; no exported record yet) | [note](notes/small-model-validation-2026-09-28.md) |
| — | DeepSeek V4 Flash with eugr's launcher | — | two nodes | — | external | [note](notes/deepseek-v4-flash-two-node-startup.md) |

Still to run, in this order (each also checks switching away and back, a controller restart during
serving, and the return to the last known-good revision):

1. a small single-node smoke test, exported with `tsm validation export`;
2. a real large TP2 model;
3. GLM at 262K context, then higher;
4. DeepSeek up to 1M context, with a retrieval test (several needles) and a multi-step tool test.

Failure tests (pulling a node, killing a rank) only in a planned maintenance window.

## Adding a record

Export it with `--markdown`, add the block under a new heading below, and put the JSON next to it as
`docs/validation/<date>-<profile>.json` in the same pull request. Check it first: it should contain no
host names or addresses.
