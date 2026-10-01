# TwinSpark Manager — Alpha 0.4

> **0.4.1** — see [CHANGELOG.md](CHANGELOG.md). Quick start on the GX10 pair:
> `tsm doctor` → `tsm rdma` (paste the suggested `rdma_hcas`/`ib_gid_index` into controller.yaml)
> → `tsm foreign ls` (stop hand-started vLLM) → `tsm mods import-eugr ~/spark-vllm-docker`
> → `tsm cookbook import deepseek-v4-flash-0731-b12x` → `tsm pin …` → `tsm plan …` → `tsm activate …`.
> Install `deploy/systemd/twinspark-privd.service` on both nodes for page-cache drop and headless mode.


Headless-first vLLM manager for one or two NVIDIA DGX Spark systems.

**Status: alpha.** The control path is covered by automated tests. Real Docker
activation, chat, streaming, and shutdown passed on DGX Spark with a 135M model
in single-node and two-node PP2 configurations on 2026-09-28. See
[small-model development](SMALL-MODEL-DEVELOPMENT.md) for results and reproduction.
Large-model activation and other topologies still need hardware validation.

The original working DeepSeek V4 Flash 0731 setup is backed up separately;
its [two-node startup commands](DEEPSEEK-TWO-NODE-STARTUP.md) restore the original launcher.

## What works

- **Glassmorphic web GUI** (`twinspark/web`, served at the management API `/`):
  frosted-glass SPA with Dashboard, Models, Planner, Cookbook, Resolve,
  Diagnostics and Jobs views. Build it from `src/` with `scripts/build_web.py`.
- Profiles with immutable revisions (model commit sha + image digest pinned), diff, duplicate, pin, known-good tracking
- Launch planner: exact per-node `docker run … vllm serve …` for `single-a`, `single-b`, `replicated`, `tp2`, `pp2`, `tp-ep`; native multi-node (`--nnodes/--node-rank/--headless`) or Ray
- `gpu_memory_utilization` derived from the unified-memory reserve (vLLM's 0.9 default is never used blindly)
- Activation pipeline with live progress, preflight (foreign process on the vLLM port, disk, writable caches), weight download + rsync A→B over QSFP, drain → stop → reclaim → start → health → smoke test → route
- Failure handling: before the stop stage the old model keeps serving; after it, partial containers are removed and the previous known-good deployment is restored automatically
- Stable OpenAI gateway: aliases, real SSE streaming, 503 + `Retry-After` while switching, in-flight draining, round-robin for `replicated`
- Controller restart adopts running containers instead of restarting them; autostart after reboot
- Agents only ever touch containers labelled `org.twinspark.owned=true` — a hand-started vLLM is never stopped by the manager
- **Cookbook import** — the four `OPTIMAL-PROFILES.md` researched configs shipped as
  recipes (`tsm cookbook list` / import via CLI or GUI); creates a real profile.
- **Identity resolution** (`tsm resolve` / GUI) — `org/model@branch` → pinned commit
  sha + `config.json` + safetensors total_size, and `image:tag` → `@sha256:` digest
  via the Docker Registry API. Pure outbound HTTP; never touches the local vLLM.
- **NCCL / QSFP link test** (`tsm link` / Diagnostics) — echo responder + burst
  initiator across the 200G link. Dry-run returns simulated figures; real numbers
  need docker mode.
- **vLLM `/metrics` scraping** (`tsm metrics` / Diagnostics) — read-only Prometheus
  scrape of the routed backend for KV-cache %, tokens, TTFT and latency.
- **Headless-mode apply** (`tsm headless desktop|headless-safe|headless-max`) —
  GDM/thermal/power profile transition per node (dry-run now, privd-mediated in docker mode).
- `tsm` CLI: `init`, `serve`, `plan`, `activate` (live progress), `status`, `stop`,
  `jobs`, `logs`, `cookbook`, `resolve`, `link`, `metrics`, `headless`

## Not in this alpha

mTLS between nodes (bearer token
over the direct QSFP link for now), and web GUI OAuth / multi-user (single
management API key). One-click pinning is available in the Profiles view.

## Install (both nodes)

```bash
sudo useradd -r -m -G docker twinspark
sudo python3 -m venv /opt/twinspark/venv
sudo /opt/twinspark/venv/bin/pip install ".[hf]"
sudo mkdir -p /etc/twinspark /var/lib/twinspark /var/cache/twinspark
sudo chown -R twinspark: /var/lib/twinspark /var/cache/twinspark
```

Node A:
```bash
sudo -u twinspark tsm init --secrets-dir /etc/twinspark/secrets --show   # prints keys once
sudo cp deploy/controller.example.yaml /etc/twinspark/controller.yaml    # edit IPs/ifaces
sudo cp deploy/agent-a.example.yaml   /etc/twinspark/agent.yaml
```
Node B:
```bash
sudo -u twinspark tsm init --role agent --secrets-dir /etc/twinspark/secrets \
     --agent-token <from A> --backend-api-key <from A>
sudo cp deploy/agent-b.example.yaml /etc/twinspark/agent.yaml
```
Both: `sudo cp deploy/systemd/*.service /etc/systemd/system/` and enable
`twinspark-agent` (both) and `twinspark-controller` (A only).

Weight sync needs SSH keys from `twinspark@A` to `nodes.B.ssh_user@B` over the QSFP IP.

## Using it

```bash
ssh -L 8443:localhost:8443 spark-a          # or use the Tailscale IP
export TSM_API=http://127.0.0.1:8443 TSM_KEY=<management_api_key>
curl -X POST -H "x-api-key: $TSM_KEY" -H 'content-type: application/json' \
     $TSM_API/api/v1/profiles -d @examples/qwen-tp2.json
tsm plan qwen-flash-tp2            # exact commands, nothing is started
tsm activate qwen-flash-tp2        # live stage-by-stage progress
tsm status
```
Clients: `http://spark-a:8000/v1`, model `default`, key = `inference_api_key`.

**Web GUI:** open `http://127.0.0.1:8443/` (same-origin with the management API)
and enter the management API key. Import a researched model from the **Cookbook**
tab, pin its model commit and image digest in **Profiles**, sanity-check memory
in **Planner**, then activate the pinned profile.

Community recipes can be previewed before import. Invalid parallel sizes,
unsupported distributed backends, and memory utilization outside `(0, 0.95]`
are rejected with a validation message. Correct the recipe or its template
overrides and preview again; these settings are never silently replaced.

## Phase 0 — first contact with real hardware

1. Install with `runtime_mode: dry-run` on both agents, `vllm_port: 8100`, and put the
   gateway on a free port if your current vLLM already uses 8000.
2. `tsm plan …` and compare with the command line you run by hand today. Check every
   flag against the image you pin (`--nnodes/--node-rank/--headless`,
   `--default-chat-template-kwargs`, `--attention-backend`, `VLLM_API_KEY`).
3. `tsm activate …` in dry-run — the preflight must pass on both nodes.
4. Maintenance window: stop the hand-started vLLM, set `runtime_mode: docker`, restart
   the agents, activate a small `single-a` profile first, then `tp2`.
5. Register real model specs (`PUT /api/v1/system/model-specs` with `config.json` and the
   safetensors `total_size`) so the memory fit check runs before every switch.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

For local development on Windows (PowerShell):

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest -q -rs
```

The dry-run agents support local Windows testing. Four HF-cache tests require
symbolic-link permissions and skip when Windows denies them; one executable-bit
test requires a POSIX filesystem. Run the full suite on Linux before deployment.
Real Docker/RDMA/headless operation still targets Linux on the Sparks.

To build an installable wheel including the cookbook and web UI:

```bash
python -m pip wheel --no-deps . --wheel-dir dist
```

## Web client development

**System status** compares both nodes' CPU, GPU, shared memory, GPU sensor power,
and network traffic. **Updates** offers an opt-in coordinated install/reboot run:
B before A, with health checks, restart recovery, and restoration of the active
pinned revision. Root-owned node policies default to disabled, and firmware has
its own opt-in. See [monitoring and maintenance setup](docs/maintenance.md) for
configuration, measurement limits, and recovery instructions.

Profiles and built-in recipes support search and filters that persist while
navigating. Recipe imports review the settings before saving; name conflicts
remain in the dialog so you can correct them without starting over.

Import recipes by URL, pasted text, or a local YAML/JSON file. **Pin & prepare**
resolves an immutable revision, checks images and patches, and stages weights
without switching the current deployment. The completed job can switch to that
exact prepared revision.

**Combine two recipes** creates a split profile: A and B each run an independent
model, image and settings, with separate API model names. TP2, PP2 and TP + expert
parallel still distribute one model across both nodes. See the
[recipe and split-model guide](docs/recipe-workflow.md) for CLI commands, limits
and the on-site test checklist. The 0.4.1 changes have been tested locally with
simulated agents; validation on the Sparks remains pending.

```bash
python scripts/build_web.py
python scripts/preview_web.py
```

Open `http://127.0.0.1:18744/` for a local preview seeded with the six built-in
recipes. Its database is in memory and no Spark agents are connected; changes
disappear when the process stops. The preview serves the built `web/dist` assets,
so rebuild and refresh after editing `web/src`. Run client regression checks with:

```bash
node --test tests/web_client.test.cjs
```

Add `--demo-nodes` to the preview command for labeled sample telemetry and a fully
simulated two-node maintenance workflow.
