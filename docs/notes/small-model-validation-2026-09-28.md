# Small-model validation on two Sparks — 2026-09-28

> **Field note.** This records the first real-hardware run of TwinSpark on the maintainer's two DGX
> Sparks, at the code of that date (before 0.4.1). It shows that the real Docker path works on GB10. It does
> not show that every later change or every large model works.

## What was run

The model was [SmolLM2-135M-Instruct](https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct). It ran on a
single node (A), then with two-node pipeline parallelism (PP2). Both runs completed:

- activation
- completions, chat and SSE streaming
- gateway authentication
- adoption of running containers
- managed shutdown

The model's nine attention heads cannot be divided for TP2, so PP2 exercised the real two-node launch path.

The selected download was **259.8 MiB per node**, including tokenizer files. The first download took about
two minutes, and node B received its copy over QSFP. No Docker image download was needed: the profiles pin a
vLLM image that was already built on both nodes (eugr/spark-vllm-docker, `vllm-node-b12x`) by its local
image ID. The exported profiles are in [examples/](examples/). Their image ID belongs to that local build,
so on other machines pin them again with your own image.

The run also exercised these code paths:

- correct Hugging Face resolution endpoints and single-file checkpoint sizing
- bounded inference-only downloads and complete-shard checks
- revision-only sync, including Hugging Face 2.x shared blobs
- local Docker image pins
- precise small memory fractions
- preservation of Docker ownership labels

## Reproduce it

`scripts/dev_cluster.py` drives the same run through the real manager, outside the installed services.
Run it from a checkout on node A, with key-based SSH to node B. Then run each step once the previous one
has finished:

```bash
export TSM_DEV_PEER=<user>@192.168.100.2            # node B over the QSFP link
export TSM_DEV_IMAGE_ID=$(docker image inspect --format '{{.Id}}' vllm-node-b12x)
.venv/bin/python scripts/dev_cluster.py prepare
.venv/bin/python scripts/dev_cluster.py deploy
.venv/bin/python scripts/dev_cluster.py stage
.venv/bin/python scripts/dev_cluster.py test --topology single-a --topology pp2
```

**Development limits.** The development configuration:

- rejects snapshots over 0.35 GiB
- keeps 5 GiB free on the download filesystem
- disables image pulls

**Profile settings.** The profiles use:

- a 1,024-token context
- one concurrent request
- eager execution
- an explicit 32 MiB KV cache. This setting controls the cache only; it is not a hard cap on total process
  memory.

**Where things live.** Runtime configuration, credentials, logs and SQLite state are kept in
`/tmp/twinspark-manager-dev`. After `/tmp` is cleared, run `prepare` and `deploy` again. Node B's agent uses a
virtual environment at `/tmp/twinspark-manager-dev/venv` with
`fastapi uvicorn pydantic pyyaml httpx cryptography`.

**Management UI.** To run it after `prepare` and `deploy`:

```bash
.venv/bin/python scripts/dev_cluster.py serve
```

**Ports.**

| Service | Port |
| --- | --- |
| Management (`http://127.0.0.1:18443`) | 18443 |
| Gateway (`http://127.0.0.1:18000`) | 18000 |
| Agents | 19443 |
| Test vLLM | 18100 |
| Two-node rendezvous | 29521 |

The management credentials are in the development vault.
