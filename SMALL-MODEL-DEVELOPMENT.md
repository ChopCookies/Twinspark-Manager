# Small-model development — 2026-09-28

Real hardware validation passed on both Sparks using
[SmolLM2-135M-Instruct](https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct):
single-node on A, then two-node pipeline parallelism (PP2). Both runs completed
activation, completions, chat, SSE streaming, gateway authentication, adoption
of running containers, and managed shutdown. The automated suite passed 58 tests.
The raw results are retained locally in `research/hardware-validation-2026-09-28.json`.
Node captures are excluded from the GitHub source upload; the results and reproduction
steps below summarize the hardware validation.

The selected download was **259.8 MiB per node**, including tokenizer files;
initial download took about two minutes and node B received a copy over QSFP.
No Docker image download was needed. The model's nine attention heads cannot
be divided into TP2, so PP2 exercised the real two-node launch path.

The subsequent requested SSD cleanup removed these development weights too.
No development servers or inference containers are left running. DeepSeek 0731
is preserved but stopped; see [its startup guide](DEEPSEEK-TWO-NODE-STARTUP.md).

## Reproduce when resuming development

The existing Python environments remain installed locally and on node B.

```bash
cd /home/chopc/Documents/twinspark-manager
.venv/bin/python scripts/dev_cluster.py prepare
.venv/bin/python scripts/dev_cluster.py deploy
.venv/bin/python scripts/dev_cluster.py stage
.venv/bin/python scripts/dev_cluster.py test --topology single-a --topology pp2
```

Run each command after the previous one finishes. `stage` downloads only inference
files and synchronizes that revision. Development configuration rejects snapshots
over 0.35 GiB, keeps 5 GiB free on the download filesystem, and disables image pulls.
Profiles pin the already installed B12X image by its local immutable image ID.
They use a 1,024-token context, one concurrent request, eager execution, and an
explicit 32 MiB KV cache. The KV setting controls the cache; it is not a hard cap
on total process memory.

Runtime configuration, credentials, logs, and SQLite state live in
`/tmp/twinspark-manager-dev`; the source and pinned example profiles are retained
in this repository. The temporary environment must be recreated after `/tmp`
is cleared. Node B's agent uses a virtual environment at
`/tmp/twinspark-manager-dev/venv` with `fastapi uvicorn pydantic pyyaml httpx cryptography`.

To run the management UI after preparing/deploying:

```bash
.venv/bin/python scripts/dev_cluster.py serve
```

Management listens at `http://127.0.0.1:18443`, the gateway at
`http://127.0.0.1:18000`, agents at port `19443`, test vLLM at `18100`, and
two-node rendezvous at `29521`. The saved profiles can be activated in the UI.
Management credentials are in the development vault, not committed to source.

Changes exercised by this run: correct Hugging Face resolution endpoints and
single-file checkpoint sizing; bounded inference-only downloads; complete-shard
checks; revision-only sync including Hugging Face 2.x shared blobs; local Docker
image pins; precise small memory fractions; preservation of Docker ownership labels.
