# Recipe preparation and two-node experiments

TwinSpark keeps one active deployment. A deployment may contain a single model,
a distributed model, replicated copies, or two independent models in a split
profile. Activating any deployment drains and replaces the current one.
Preparation leaves the current deployment serving.

## Switching while requests are running

A switch first stops new requests to the model (they get `503` with `Retry-After`) and waits for the
running ones. What happens to requests still running after `runtime.drain_timeout_s` (30 s) is the drain
policy:

| Policy | After the timeout | Use it when |
|---|---|---|
| `abort` (default) | the switch fails **before anything is stopped**; the old model is reopened and keeps serving | always, unless you know better: an agent in the middle of a long task is not cut off |
| `wait` | keeps waiting, up to `runtime.drain_wait_max_s` (an hour); cancelling the job keeps the old model | the requests matter and will finish |
| `force` | stops the old model; those requests end | they do not matter, or the old model is stuck |

```bash
tsm activate my-experiment                      # abort if requests outlast 30 s
tsm activate my-experiment --drain wait
tsm activate my-experiment --drain force --drain-timeout 0
```

The GUI asks when requests are running, and the dashboard shows how long the oldest one has been
running. A client that disconnects releases its request at once. Switches TwinSpark makes on its own
(a rollback after a failed start, a recovery, maintenance) always force: the model they replace has
already failed or has to go. Set `runtime.drain_policy` in `controller.yaml` to change the default.

## Import and prepare

In Cookbook → Paste / URL, open a YAML/JSON file, paste its contents, or give a
recipe URL. Review the mapped fields, raw arguments, image, mods and requirements.
Import saves the exact reviewed snapshot. Edit the profile, then choose
**Pin & prepare**. Pin resolves model commits, image digests and drafter commits;
prepare checks the pinned revision and stages its files. Pinning and preparation
do not install missing patches or start an inference container. A patch the recipe pins
(see [Required patches](#required-patches)) is one step: **Mods → Install pinned patch**,
or `tsm mods required <profile> --install`, then **Retry** on the failed preparation.

The CLI equivalent is:

```bash
tsm cookbook import-recipe ./recipe.yaml --as my-experiment --preview
tsm cookbook import-recipe ./recipe.yaml --as my-experiment
tsm edit my-experiment
tsm pin my-experiment
tsm plan my-experiment
tsm prepare my-experiment
tsm activate my-experiment r1
```

Use the revision printed by prepare (or the job's Switch to prepared revision
button). Later edits or re-pinning do not alter that revision. Preparation checks
agent reachability, memory estimates when model metadata is available, required
patches, pinned images, and complete weights. It cannot establish that an image's
vLLM supports every recipe flag or that its model kernels will work; activation
loads the model and tests a completion before publishing its routes.

Weight staging includes separate drafters and subdirectory patterns. A changed
`download_include` invalidates the old recipe completeness check; only the
missing files need downloading. Unknown patterns fail visibly. Transfer checksum
failures stop the job before the old deployment is touched. A cache copied from
an unverified download must first be downloaded/verified on its source node, or
verification must be explicitly disabled for the stage operation.

## Required patches

A recipe lists its mods in `advanced.mods`. The common case is a patch that replaces one file in the image
with a file from the recipe author's repository. For that, the recipe can carry the patch itself, pinned,
in `source.mod_artifacts`:

```json
"mod_artifacts": {
  "glm53-sm121-sparse-attn": {
    "kind": "file-patch",
    "repo": "seanlinmt/GLM-5.3-Flash-NVFP4-DFlash2-2x-DGX-Spark",
    "commit": "e6dcb04852aa469ad7b17c7156cabeae0f46f497",
    "path": "docker/sparse_attn_indexer_kpool_sm121.py",
    "sha256": "8a3ecfb0bab2441dd7417ed00a10d142191496149f88e5fe79fcfaea4b160980",
    "target": "/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/sparse_attn_indexer_kpool.py",
    "license": "Apache-2.0",
    "image": "ghcr.io/tonyd2wild/vllm-glm53-flash@sha256:4def0ef6…",
    "why": "what breaks without it"
  }
}
```

- **Everything is pinned.** The commit must be a full 40-character commit, never a branch. The file's SHA-256
  and a license are required. A recipe with an incomplete descriptor is refused when it is imported.
- **Installing** (Mods → Install pinned patch, or `tsm mods required PROFILE --install`) does this:
  1. Node A fetches exactly that file from GitHub and refuses it if the SHA-256 differs.
  2. It builds the mod as `tsm mods file-patch` does. The generated `run.sh` refuses an image without the target
     file and checks the copy's SHA-256.
  3. It installs identical content on every node. A node that failed is named. The same command again (it is
     idempotent) completes the install.
- **The revision pins it too.** While a switch or a preparation resolves, before anything is downloaded, each
  node's copy of the mod must be that patch: the generated `run.sh` and the pinned file. Other content
  installed by hand under the same name stops the job, and the message names the fix. Extra files beside it,
  such as a README, are ignored.
- `tsm mods required PROFILE` lists the states. A mod can be missing on a node, differ between the nodes, be
  installed and identical, and be the recipe's pinned patch or not. Whether the patch works on the GPU only
  shows in a real activation (its validation record).

A patch the recipe does not pin is installed by hand as before: `tsm mods file-patch NAME --file FILE --target
PATH`, or `tsm mods install DIR`. `tsm mods install` exits non-zero when any node did not get the mod.

## Two independent models

In Profiles choose **Combine two recipes**. Select the recipe for each node and
two different API model names. This copies the working settings, provenance and
pins into one new profile. Changes to the original profiles do not affect it.
Each model needs to fit its own node; combining recipes originally written for
TP2 does not make either full checkpoint smaller. Combined recipes start with
experimental verification status and discard measurements specific to the
original distributed topology.

```bash
tsm split dual-model chat-recipe coder-recipe --alias-a chat --alias-b code
tsm edit dual-model
tsm pin dual-model
tsm fit dual-model
tsm plan dual-model
tsm prepare dual-model
tsm activate dual-model r1
```

The outer draft describes node A; `secondary` contains node B's full draft and
must use `single-b`. Quick settings edit A; edit `secondary` in Full draft for B.
The two parts can use different images, mods, parsers, context lengths, memory
fractions and extra models. Pin overrides apply to A; B uses its own image hint
or pinned identity. Both pins are saved together after both resolutions succeed.

The gateway exposes both aliases at the same OpenAI-compatible endpoint. For
`dual-model`, it rewrites `chat` to the backend name `dual-model-a`, and `code`
to `dual-model-b`. Each route retains its own observed maximum context length.
Node B's API is reached from A using `nodes.B.qsfp_ip`. Its model files are staged
only on B; A's files only on A. A source cache on the other node may still be
reused through QSFP. Both APIs must become healthy. A failure after the previous
deployment is stopped removes the partial deployment and invokes normal rollback.

### Each half on its own

The two halves of a split are independent models, and TwinSpark treats them that way:

- **A new revision touches only what changed.** Edit node B's half, pin, and activate: only B's model is
  drained, stopped and started. Node A's container keeps running, its alias stays open, and requests that
  are streaming from it are not waited for or cut off. The job says *only node B's half changes*; the
  dashboard shows which revision started each half (*kept across later revisions*). This needs the same
  profile and an identical half (same settings, model commit and image) that is serving; anything else
  switches both halves.
- **Restart one half:** the dashboard's *Restart this half*, or `tsm activate dual-model --half B`. Activating
  the same revision without `--half` restarts both.
- **The watchdog recovers only the half that failed.** If node B's model crashes, only `code` goes down and
  only B is restarted; `chat` keeps serving.
- **A failed switch of one half leaves the other serving.** With `auto_rollback` the previous version of that
  half is started again (again without touching the other). Without it the half stays **down** (shown with
  the reason on the dashboard and in `tsm status`) until you start it with *Start this half* or switch.
- **After a controller restart** a half that still runs is adopted; one that did not survive (node B rebooted)
  is restarted alone when `autostart` is on.
- **Metrics per half:** the dashboard, `tsm status` and `/metrics` ([metrics](metrics.md)) show each half's
  throughput, KV cache and latency with its own alias, node and revision.

TP2, PP2 and TP-EP are one model on two nodes: they always switch, fail and recover as one unit.

## One model distributed across both nodes

Use `tp2` (tensor parallel), `pp2` (pipeline parallel), or `tp-ep` (TP with expert
parallel). Native launches explicitly use the `mp` executor, worker rank 1 first,
then head rank 0 after the configured delay. Both ranks use the same model commit,
image and patches. Both nodes need the entire checkpoint cache even though GPU
weight residency is sharded. The gateway sends inference to the head on A.

TP2 requires attention heads divisible by two; TwinSpark rejects a known odd
count before starting. PP2 is the appropriate two-node smoke path for the bundled
SmolLM2-135M recipe, which has nine attention heads. Model architecture, kernels,
vLLM version and RDMA availability still determine actual compatibility and speed.
The [vLLM parallelism guide](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/)
describes native multi-node and Ray deployments. Use the exact image expected by
the recipe; older images may not accept the multi-node flags.

## On-site validation

The implementation is tested with simulated agents and mocked HTTP backends. The
[on-site SmolLM run of 2026-09-28](notes/small-model-validation-2026-09-28.md)
(single node and PP2) showed that the real Docker path works on GB10 at that day's code;
it is not proof that the current version or a large community model runs on your hardware.

1. Install the same manager build on controller and agents. Run the full Python
   suite on Linux, including filesystem tests skipped on Windows.
2. Record the current profile and exact revision with `tsm status`. Check both
   nodes with `tsm doctor`, `tsm hardware`, `tsm rdma`, and `tsm link --mode rdma`.
3. Prepare a small single-node recipe. Confirm the old model keeps serving while
   preparation runs, and that missing patches/weights fail before switching.
4. During a test window, activate the small recipe. Check `/v1/models`, ordinary
   and streaming chat completions, and client authentication.
5. Repeat with a PP2 smoke recipe, then a TP2-compatible model. Inspect both rank
   logs for startup failures and the actual NCCL transport. Do not infer RDMA
   performance from a green HTTP health probe.
6. Combine two small recipes with distinct aliases. Test normal and streaming
   requests to both, confirm each node has its assigned image and weights, and
   compare each route's context limit. Restart the controller and verify it adopts
   both routes without restarting healthy containers.
7. Test a failed node-B launch and verify rollback. Restore the exact original
   revision, confirm it is healthy, and retain the job/log records locally.
8. Increase checkpoint size, context and concurrency gradually while watching
   per-node shared memory, GPU use, temperature, power and QSFP traffic.

Do not treat a successful dry run as hardware validation or benchmark evidence.
