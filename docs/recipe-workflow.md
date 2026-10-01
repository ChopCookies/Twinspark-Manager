# Recipe preparation and two-node experiments

TwinSpark keeps one active deployment. A deployment may contain a single model,
a distributed model, replicated copies, or two independent models in a split
profile. Activating any deployment drains and replaces the current one.
Preparation leaves the current deployment serving.

## Import and prepare

In Cookbook → Paste / URL, open a YAML/JSON file, paste its contents, or give a
recipe URL. Review the mapped fields, raw arguments, image, mods and requirements.
Import saves the exact reviewed snapshot. Edit the profile, then choose
**Pin & prepare**. Pin resolves model commits, image digests and drafter commits;
prepare checks the pinned revision and stages its files. Pinning and preparation
do not install missing patches or start an inference container.

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

The new implementation is locally tested with simulated agents and mocked HTTP
backends. Existing older on-site SmolLM single-node and PP2 records are not proof
that 0.4.1 or any large community model runs on the current hardware.

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
