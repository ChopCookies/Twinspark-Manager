# Working DeepSeek V4 Flash 0731 deployment

Captured on 2026-09-28 from the **running deployment**, with `/v1/models`
responding successfully. This is the original B12X recipe, not the manager's
earlier researched cookbook preset.

## Backup

`backups/deepseek-v4-flash-0731-20260928T174609Z/`

This backup is local operational data and is excluded from the GitHub source
repository. The restoration commands below require the original local backup.
`scripts/backup_deepseek.py` captures a new backup from a working deployment.
contains the recipe, cluster environment, complete launcher source archive
(including mods), exact generated scripts from both containers, container/image
inspection records, process arguments, and SHA-256 checksums. It is about 550 KiB;
the existing model weights and Docker layers stay in their current caches.

```bash
cd /home/chopc/Documents/twinspark-manager/backups/deepseek-v4-flash-0731-20260928T174609Z
sha256sum --check SHA256SUMS
```

| Setting | Working value |
| --- | --- |
| Node A / rank 0 | `gx10-d95c-node1`, `192.168.100.1` |
| Node B / rank 1 | `gx10-cba3-node2`, `192.168.100.2` |
| SSH user | `chopc` |
| Network interface | `enp1s0f1np1` |
| RDMA device / GID | `rocep1s0f1` / `3` |
| Containers | `vllm_node` on each node |
| Image tag | `vllm-node-b12x` |
| Model | `deepseek-ai/DeepSeek-V4-Flash-0731` |
| API | `http://192.168.100.1:8000/v1` |
| Rendezvous | `192.168.100.1:29501` |
| Parallelism | TP2, native processes, worker uses `--headless` |
| Context | `auto` (live API reports 1,048,576 tokens) |
| GPU utilization | `0.85` |
| KV cache / block size | `fp8` / `256` |
| Speculation | DSpark, 5 tokens, probabilistic draft sampling |

Image ID observed on node A:
`sha256:d43f15877df4176dfc70b7ebca336d5de698e1696da02fc3738b0338a65f5db2`.
Registry reference:
`eugr/spark-vllm-b12x@sha256:eb3ed2bbb0c91dc6d41282d22532267b5a449088c78a032400cd887fe9ddd2c5`.
The live containers also have the recipe's `instanttensor-hybrid-draft-loader` mod.

## Start the two nodes from node A

The launcher manages both nodes through SSH. Run this when intentionally
starting/restarting DeepSeek: it recreates its `vllm_node` containers.

```bash
cd /home/chopc/spark-vllm-docker
./run-recipe.sh recipes/deepseek-v4-flash-0731.yaml \
  --nodes 192.168.100.1,192.168.100.2 \
  --container vllm-node-b12x \
  --eth-if enp1s0f1np1 --ib-if rocep1s0f1 \
  --no-ray --master-port 29501 --daemon
```

No `--setup`, build, or download flags are needed: both nodes already have the
image and weights. Add `--dry-run` to inspect the launch without executing it.
The recipe enables B12X MLA/MoE/linear kernels and applies the required loader mod.

```bash
docker logs --tail 80 -f vllm_node
ssh chopc@192.168.100.2 'docker logs --tail 80 vllm_node'
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/v1/models
```

The exact expanded vLLM commands, including their environment variables, are in
`node-A-exec-script.sh` and `node-B-exec-script.sh` in that local backup.
These scripts need the container's mounts, RDMA settings, and applied mod;
use the launcher above for a complete restart.

## Restore the saved launcher source if the original changes

```bash
mkdir -p /home/chopc/Documents/twinspark-manager/restored-spark-launcher
tar -xzf /home/chopc/Documents/twinspark-manager/backups/deepseek-v4-flash-0731-20260928T174609Z/spark-launcher.tar.gz \
  -C /home/chopc/Documents/twinspark-manager/restored-spark-launcher
cd /home/chopc/Documents/twinspark-manager/restored-spark-launcher
./run-recipe.sh recipes/deepseek-v4-flash-0731.yaml \
  --nodes 192.168.100.1,192.168.100.2 \
  --container vllm-node-b12x \
  --eth-if enp1s0f1np1 --ib-if rocep1s0f1 \
  --no-ray --master-port 29501 --daemon
```

This restores the launch configuration; it depends on the existing cached image
and weights. The backup deliberately does not duplicate hundreds of GB of data.
