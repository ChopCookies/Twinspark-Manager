# Memory planning per node (0.6)

A DGX Spark (GB10) has one pool of unified memory, about 121.6 GiB as DGX OS reports it, shared by the
CPU and the GPU. Two Sparks are two such pools, never one 256 GiB pool. TwinSpark therefore plans every
node on its own. For each node the plan lists what the node holds, bucket by bucket, and gives three
separate answers:

| Verdict | Question |
|---|---|
| **Startup peak** | Does everything that is resident at startup, plus the first prefill, fit in this node's MemTotal? |
| **One max-length request** | Does vLLM's claim leave KV room for one sequence of the full context length? (If not, vLLM refuses to start.) |
| **Requested concurrency** | How many full-length sequences fit in that KV room, compared with the number requested? |

Before 0.6 there was only a single "fits", and it meant "weights plus one sequence fit inside vLLM's
pool". It did not count the OS, the desktop, other processes, or a separate drafter model. A start
could pass that check and still fail at its peak, as GLM-5.3 DFlash2 did on node A with
`NV_ERR_NO_MEMORY`.

`GET /api/v1/profiles/{name}/fit` still returns the 0.5 keys (`fits`, `full_concurrency_fits`,
`headroom_gib`, `level`, `est_kv_tokens`, ...), but they are now worked out from the per-node plans, with
the worst node deciding. `fits` means that the startup peak fits on every node and that one max-length
request fits. `headroom_gib` and `level` describe the smallest startup margin. The new keys are:

* `plan`, one entry per node, with `buckets`, `startup`, `one_context`, `full_concurrency`,
  `steady_state`, `confidence` and `drafter`
* `verdicts`, the worst node's result for each of the three questions
* `confidence`, for example `measured inputs: 12/18`
* `drafters` and `drafter_gib_per_node`

## Sources

Every bucket has a source, a range (`low`–`high`) and a note:

* **measured**: read from the node (`hardware_facts`) or from the checkpoint files (the safetensors
  index size, or the KV bytes per token that the recipe author measured).
* **calibrated**: taken from this revision's last start (its startup timeline, or the KV pool that
  vLLM reported).
* **estimated**: a formula or a default. The defaults are the 0.5 reserves, now labelled as estimates.
* **unknown**: nothing was read. The bucket is shown as unknown and is never counted as 0. A verdict
  lists any unknown buckets under `not_counted`.

A dry-run node's readings come from the simulation host, not from a Spark, so they are labelled as
estimates. The TwinSpark units it reports are simulated values marked `"simulated": true`. Other
processes stay unknown.

## Buckets and formulas

Resident before vLLM starts (from the node's last `hardware_facts`):

| Bucket | Measured as | Default when not read |
|---|---|---|
| OS, kernel + Docker | `MemTotal − MemAvailable`, minus the parts below that were read separately and minus running TwinSpark containers (their cgroup `memory.current`) | 6.4 GiB (0.5's 6.0 OS + 0.4 Docker), range 4.5–8.0 |
| TwinSpark services | sum of `memory.current` of `twinspark-{agent,controller,privd,terminal}.service` (cgroup v2); the high end of the range is the sum of `memory.peak` | 0.2 GiB |
| Desktop session | RSS of the graphical-session processes | unknown |
| Other processes | RSS of the 5 largest processes above 100 MiB that are not TwinSpark, the desktop, Docker/containerd, kernel threads or processes inside a container (for example a resident Hermes gateway, ≈394 MiB on node A) | unknown |

When `MemTotal − MemAvailable` was read, it already contains everything on the node. A part that
could not be read on its own is therefore included in "OS, kernel + Docker" rather than counted a
second time. If the reading was taken while a model was running and its containers' memory could not
be read, the default is used instead.

Inside vLLM's claim (`gpu_memory_utilization × MemTotal`; vLLM fills whatever this leaves with KV
cache):

* **Model weights**: checkpoint bytes ÷ 2 under TP2/TP-EP/PP2. Without a checkpoint size it is
  parameters × bytes per parameter. Range −2 % / +5 %.
* **Drafter weights**: the drafter checkpoint's bytes. See the next section for where it is counted.
* **First-prefill peak**: `max_num_batched_tokens × hidden_size × 2 bytes × 24`. Inference holds the
  activations of one layer at a time, so the layer count does not multiply this figure. The 24
  hidden-sized buffers per token cover q/k/v/o, the MLP or MoE intermediates including top-k token
  copies, and the residual and norm buffers. Under tensor parallelism the hidden states are
  replicated, so the figure is not halved. Without `max_num_batched_tokens`, vLLM's default of 8,192 is
  assumed; with chunked prefill off, a single full-context prefill is assumed. Without a `hidden_size`
  in the spec, the 0.5 rule is used: max(1 GiB, 5 % of the weights) + 0.25 GiB per sequence (up to 16).
  Range ×0.5 to ×1.5.
* **CUDA graphs**: 1.2 GiB, or 0 in eager mode.

Next to vLLM's claim:

* **NCCL / backend buffers**: 0.8 GiB native or 1.7 GiB Ray, and 0 on a single node.
* **Drafter scratch**: the first-prefill formula applied to the drafter's hidden size; without one,
  10 % of its weights and at least 0.25 GiB. It is counted outside the claim because vLLM does not
  profile every speculative method's dummy run.

While loading only:

* **Loader transient**: one layer of this node's weights (weights ÷ `num_hidden_layers`), at least
  0.5 GiB, because repacking briefly holds the old and the new copy of a layer. Without a layer count,
  1 GiB.

KV:

* **KV for one sequence** = KV bytes per token per node × context length. **KV for the requested
  concurrency** = that × `max_num_seqs`. KV bytes per token are the recipe's measured value when there
  is one; otherwise they come from the textbook formula (MLA latent, and KV heads halved under TP2).

The verdicts, per node (all figures in GiB):

```
resident    = OS + services + desktop + other processes        (known buckets only)
claim       = gpu_memory_utilization × MemTotal
in_claim    = weights + drafter + first prefill + CUDA graphs
next_to     = NCCL/backend + drafter scratch
startup     = max( resident + next_to + max(claim, in_claim + KV one),            first prefill
                   resident + NCCL + weights + drafter + loader transient )     weight loading
steady      = startup (first-prefill phase) − first prefill
margin      = MemTotal − startup         SAFE ≥ 4, LOW ≥ 1.5, CRITICAL below
KV room     = claim − in_claim
one request = KV room ≥ KV one
full        = KV room ≥ KV for the requested concurrency;  sequences ≈ KV room ÷ KV one
```

The pessimistic margin (`margin_low_gib`) uses the high end of every range.

## Why a one-rank drafter is counted on both nodes

`speculative_config.draft_tensor_parallel_size: 1` makes the drafter a one-rank model. Under TP2,
TwinSpark cannot tell before a start which rank, and therefore which node, will hold it. Splitting it
over the pair would undercount the node that actually holds it. So the whole drafter is counted on
every node that can host it, labelled **"unconfirmed (counted on both nodes)"**.

A drafter that runs at the target's tensor-parallel size (`draft_tensor_parallel_size: 2`, or no value,
which is vLLM's default) is split over the two nodes. An extra model that `speculative_config` does
not name is counted on both nodes as well, because nothing says where it is loaded.

The label changes to **calibrated** only after a startup timeline:

* If node A's startup footprint is larger than node B's by at least 75 % of the drafter's size, the
  drafter is placed on A only.
* If the two footprints are within 25 % of the drafter's size of each other, it is on both nodes.

Anything in between stays unconfirmed. The head node also runs the API server, so the difference is
only a hint.

When pinning resolves `advanced.extra_models`, it also registers each drafter's ModelSpec (its
config.json and checkpoint size), the same way it does for the main model. This is best effort. When
the Hub cannot be reached, the pin still succeeds, and the drafter's size shows as unknown.

## The startup timeline

From the moment an activation starts its destructive part, just before the old model is stopped,
each node of the deployment samples its own memory once a second. The agent actions are
`timeline_start(job_id, interval_s=1.0, max_samples=3600)`, `timeline_mark(job_id, stage)`,
`timeline_stop(job_id)` and `timeline_read(job_id, since_index=0, max_points=None)`.

Each sample records:

* the time, as a monotonic offset and as wall-clock time
* MemAvailable, MemFree, page cache and swap used
* swap-in and swap-out pages per second (from `/proc/vmstat` deltas)
* memory PSI `some` and `full` avg10
* each TwinSpark unit's cgroup `memory.current`
* for the deployment's containers, their cgroup `memory.current`, `memory.peak` and `memory.max`, plus
  the `oom`/`oom_kill` counts from `memory.events`. The cgroup is found from the container name, via
  its ID, and is unknown when it cannot be found.

What the NVIDIA driver itself has allocated is recorded as unknown: on a GB10, `nvidia-smi` reports no
memory figures, so driver allocations show up only as falling MemAvailable. MemFree is recorded next
to MemAvailable, so that a failure with plenty of reclaimable page cache can be told apart from a
truly full node.

The controller marks every stage: stopping, reclaiming, starting-cluster, loading, testing, routing,
healthy, and "failed" when the start fails. Each mark also takes a sample.

The timeline is bounded and best effort:

* There is one sampler per agent; a new start ends the previous one.
* There are at most 3,600 samples (7,200 at the very most) and at most two hours of sampling. After
  that the agent stops by itself.
* Start, mark and stop calls time out after a few seconds.
* An agent that fails a call is left out for the rest of the activation, and the reason is recorded
  in `startup_timeline.errors`.
* An agent too old to know the actions turns the timeline off for that node, not the activation.

When a start succeeds, the job keeps a downsampled copy of the timeline (at most 120 points per node,
MemAvailable and MemFree only). The summary also goes to the store as `observed_startup:{revision_id}`.
That is a separate key, because the revision's `observed:{revision_id}` record is rewritten when the
job completes. `/fit` shows it under `observed.startup`.

The **startup footprint** is MemAvailable just before the containers started minus the lowest
MemAvailable after that. The next plan of the same settings uses it: the startup peak becomes what
is resident now plus that footprint, and is labelled **calibrated**. vLLM's reported KV pool
calibrates the KV verdicts in the same way.

When a start fails, the evidence capture reads each node's timeline back before the containers are
removed. The job's `startup_evidence.timeline` keeps at most 600 points per node, chosen so that the
lowest point and the stage edges survive downsampling. It also keeps a per-node summary computed over
every sample: the lowest MemAvailable and when it happened, the footprint, the swap and PSI peaks, and
any cgroup OOM events. The job page draws MemAvailable for each node with the stage marks and the
lowest point.

## Failure classification

`startup_evidence.classification` is `{cause, confidence, evidence}`. It is shown on the job page and
by `tsm job`. The rules are checked in this order:

1. **container-limit**: a cgroup `oom_kill` while the container had a memory limit.
2. **host-oom**: Docker's OOMKilled flag, or a cgroup `oom_kill` without a limit.
3. **driver-allocation**: NVIDIA `NV_ERR_NO_MEMORY` kernel messages (high confidence), or CUDA
   out-of-memory in the first error (medium confidence).
4. **rdma-nccl**: NCCL, ibverbs or RDMA errors in the first error.
5. **timeout**: the health wait or the engine ran out of time.
6. **application-exception**: any other Python exception.
7. **unknown**: anything else. An exit code of 1 on its own is reported as unknown, because it does
   not say why the start failed.

Timeline readings are added as supporting evidence whatever the cause: MemAvailable below 2 GiB, PSI
`full` of 10 % or more, and swap-out above 1,000 pages per second.

## TwinSpark's own footprint

0.6 moved the work that does not need the Sparks to the [workstation client](client.md) and trimmed what
stays: each service imports only what its role needs (the agent and the terminal are plain Starlette, the
rarely used cookbook and integrations routes load on first use), and the terminal is socket-activated, so
it holds no memory until someone opens a shell. Measured with `scripts/measure_memory.py` on an x86 dry-run
sandbox (Python 3.12, unique set size per process, MiB):

| Service | 0.5.1 | 0.6.0 |
|---|---|---|
| `tsm-agent` (each node) | 46.0 | 32.1 |
| controller + gateway (node A) | 54.0 | 51.0 |
| `tsm-termd` | 45.0 | 30.6 while a shell is open, **0** idle |
| `tsm-privd` (root, each node) | 42.6 | 17.1 |
| **node A resident** (agent, controller, privd, terminal switched on but no shell open) | ≈188 | **≈100** |
| **node B resident** (agent, privd, terminal switched on but no shell open) | ≈134 | **≈49** |

(In 0.5.1 a switched-on terminal service ran all the time; with the terminal off, subtract its 45 MiB
from the 0.5.1 totals.)

Budgets (provisional until measured on GB10): **≤ 128 MiB on node A, ≤ 80 MiB on node B** for the
resident TwinSpark services. The planner keeps counting `twinspark-*.service` with its measured
`memory.current` (bucket "TwinSpark services"), so whatever a Spark really uses is what the plan
subtracts. To measure on a Spark without touching its configuration or services (a sandbox in a temp
directory, its own ports, dry-run), from a checkout:

```bash
/opt/twinspark/venv/bin/python scripts/measure_memory.py           # prints a table
/opt/twinspark/venv/bin/python scripts/measure_memory.py --termd   # include an open terminal; --json for raw numbers
sudo /opt/twinspark/venv/bin/python scripts/measure_memory.py --privd   # include the root helper
```

The rest of the controller is mostly the FastAPI framework itself (its own heap is ≈7 MiB). Moving it
to plain Starlette like the agent would save about 10 MiB more; that is left for when on-site numbers
show it matters.

## What still needs real hardware

Every formula above, and the dry-run timeline, are checked by tests, but none has been compared with
a real start yet. A full GLM-5.3 DFlash2 run on two Sparks (TP2, `draft_tensor_parallel_size: 1`,
`gpu_memory_utilization` 0.85, `max_num_batched_tokens` 8192) is still needed to calibrate:

* the startup footprint on each node, and so which node holds the drafter
* how large the first-prefill peak really is compared with `24 × hidden_size` per token, and whether
  vLLM's profiler sees it
* the drafter scratch, the CUDA context and NCCL buffers, and the loader transient
* whether MemFree, rather than MemAvailable, is what the driver runs out of while the weights load
