# DeepSeek V4 Flash 0731 on two Sparks, started with eugr's launcher

> **Field note.** This was recorded on 2026-09-28 from a working deployment on the maintainer's two
> Sparks. It used eugr's launcher directly, without TwinSpark. It is kept as a reference for the built-in
> recipe `deepseek-v4-flash-0731-b12x`. Paths, user names and addresses are placeholders: replace them
> with yours.

## The working settings

The recipe is eugr's original B12X recipe. It is not the manager's earlier researched cookbook preset.

| Setting | Working value |
| --- | --- |
| Node A / rank 0 | `spark-a`, `192.168.100.1` |
| Node B / rank 1 | `spark-b`, `192.168.100.2` |
| SSH user | `<user>` (key-based SSH from A to B) |
| Network interface | `enp1s0f1np1` |
| RDMA device / GID | `rocep1s0f1` / `3` |
| Containers | `vllm_node` on each node |
| Image tag | `vllm-node-b12x` (built locally with `./build-and-copy.sh --exp-b12x`) |
| Model | `deepseek-ai/DeepSeek-V4-Flash-0731` |
| API | `http://192.168.100.1:8000/v1` |
| Rendezvous | `192.168.100.1:29501` |
| Parallelism | TP2, native processes, worker uses `--headless` |
| Context | `auto` (live API reports 1,048,576 tokens) |
| GPU utilization | `0.85` |
| KV cache / block size | `fp8` / `256` |
| Speculation | DSpark, 5 tokens, probabilistic draft sampling |

The registry reference of the base image at the time was:
`eugr/spark-vllm-b12x@sha256:eb3ed2bbb0c91dc6d41282d22532267b5a449088c78a032400cd887fe9ddd2c5`.
A local build has its own image ID. The live containers also had the recipe's
`instanttensor-hybrid-draft-loader` mod.

## Start the two nodes from node A

The launcher manages both nodes through SSH. Run this only when you mean to start or restart DeepSeek,
because it recreates the `vllm_node` containers.

```bash
cd ~/spark-vllm-docker
./run-recipe.sh recipes/deepseek-v4-flash-0731.yaml \
  --nodes 192.168.100.1,192.168.100.2 \
  --container vllm-node-b12x \
  --eth-if enp1s0f1np1 --ib-if rocep1s0f1 \
  --no-ray --master-port 29501 --daemon
```

No `--setup`, build or download flags are needed when both nodes already have the image and the weights.
Add `--dry-run` to inspect the launch without executing it. The recipe enables the B12X MLA/MoE/linear
kernels and applies the required loader mod.

```bash
docker logs --tail 80 -f vllm_node
ssh <user>@192.168.100.2 'docker logs --tail 80 vllm_node'
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/v1/models
```

## Keep a copy of a working launch

`scripts/backup_deepseek.py` captures a running deployment without copying weights or stopping it.
It saves:

- the recipe and the cluster environment, with secrets redacted;
- a source archive of the launcher, including mods;
- the exact scripts generated inside both containers;
- container and image inspection records;
- SHA-256 checksums.

The copy is about 550 KiB and goes to `backups/`, which git ignores. Set `TSM_EUGR_DIR` (your
spark-vllm-docker checkout) and `TSM_PEER` (`<user>@<node B address>`) first.

To restart from such a copy, unpack its `spark-launcher.tar.gz` and run the same `run-recipe.sh`
command inside it. It depends on the cached image and weights. The copy deliberately does not
duplicate hundreds of GB of data.

## The same model with TwinSpark

Import the built-in recipe, then check that the commands match the launch above before you activate it:

```bash
tsm cookbook import deepseek-v4-flash-0731-b12x
tsm pin deepseek-v4-flash-0731-b12x
tsm plan deepseek-v4-flash-0731-b12x
```

Stop the launcher's `vllm_node` containers before the first activation (`tsm foreign ls`).
