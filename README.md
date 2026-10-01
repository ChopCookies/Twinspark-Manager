# TwinSpark Manager

One web page, one CLI and one stable OpenAI-compatible endpoint for **two NVIDIA DGX Spark
(GB10) machines**. Switch between community vLLM recipes, manage model files on both nodes,
and keep the machines headless.

> **Status: alpha (0.4.1).** The control path is covered by automated tests, and real Docker
> activation, chat, streaming and shutdown passed on DGX Spark with a 135M model (single node
> and two-node PP2) on 2026-09-28 — see [small-model development](SMALL-MODEL-DEVELOPMENT.md).
> Large-model activation and other topologies still need on-site validation, which is why a
> new install starts in **dry-run** (nothing is run on your machines) until you say otherwise.

---

## Try it first — no Spark needed (1 minute)

```bash
git clone https://github.com/ChopCookies/Twinspark-Manager && cd Twinspark-Manager
python3.12 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/tsm demo
```

`tsm demo` starts two simulated Sparks, the controller, the gateway and the web GUI on this
machine (real HTTP between them, containers simulated) and prints the URL and key. Click
through the Cookbook, pin a recipe, press *Plan*. Ctrl-C removes everything.

## Install on your two Sparks (about 10 minutes)

You need: both Sparks cabled QSFP-to-QSFP, Docker (ships with DGX OS), Python 3.12+ (Ubuntu 24.04
has it), and a user with `sudo`. Setup never changes your network configuration.

**1. Node A** (the machine that will host the controller and the OpenAI endpoint):

```bash
git clone https://github.com/ChopCookies/Twinspark-Manager && cd Twinspark-Manager
sudo ./install.sh
```

The installer puts the program in `/opt/twinspark`, then starts the guided `tsm setup`. It
detects almost everything and only asks what it cannot know:

| Step | What it does for you |
|---|---|
| QSFP link | finds the cabled interface, its address and RoCE devices; prints a netplan snippet if the link has no address yet |
| Who / where | reuses your existing Hugging Face cache (`~/.cache/huggingface`) and runs as your user, so your docker group and SSH setup just work |
| Reach the GUI | `localhost` + SSH tunnel by default; offers your Tailscale address if it sees one |
| Ports | moves to a free port if `8000` / `8100` are taken by a hand-started vLLM |
| Safety | starts in **dry-run**: containers are simulated until `tsm go-live` |

It then writes the configs, creates the secrets and a dedicated SSH key for weight sync,
installs the systemd units and prints the exact line to run on node B.

**2. Node B** — paste the line node A printed (it carries the shared secrets, treat it like a password):

```bash
git clone https://github.com/ChopCookies/Twinspark-Manager && cd Twinspark-Manager
sudo ./install.sh --join tsm1.…
```

Lost the line? `sudo tsm join-code` on node A prints it again.

**3. Open the GUI.** From your laptop: `ssh -L 8443:localhost:8443 you@node-a`, browse to
<http://localhost:8443/> and paste the management key (`tsm init --show` prints it again).
The **Get started** page checks both nodes and shows the next step with the exact command.

**4. Run a model.**

```bash
tsm cookbook list                      # built-in dual-Spark recipes
tsm cookbook import deepseek-v4-flash-0731-b12x
tsm pin deepseek-v4-flash-0731-b12x    # freezes the model commit + image digest
tsm plan  deepseek-v4-flash-0731-b12x  # the exact docker/vllm commands for A and B, nothing started
tsm activate deepseek-v4-flash-0731-b12x
```

Clients use `http://node-a:8000/v1`, model `default`, with the `inference_api_key`
(`tsm init --show`).

**5. Go live.** While in dry-run, `activate` walks the whole pipeline with simulated
containers, so you can compare `tsm plan` with the command you run by hand today. When it
matches, stop any hand-started vLLM (`tsm foreign ls`) and on **each** node run:

```bash
sudo tsm go-live        # runtime_mode: docker + restarts the agent;  `--revert` goes back
```

Start with a small `single-a` profile, then your large model. `tsm doctor` checks everything
a fast, stable dual-Spark setup needs (driver parity, RDMA, headless, privd, SSH, disk).

### Unattended / scripted

```bash
sudo ./install.sh --yes --qsfp-iface enp1s0f1np1 --qsfp-ip 192.168.100.1 --runtime-mode dry-run
sudo tsm setup --dry          # show what would be written, change nothing
sudo tsm setup --root /tmp/stage --yes --no-start      # build a complete install in a scratch directory
```

`tsm setup --help` lists every flag. Re-running setup is safe: existing config files are kept
(`--force` regenerates them and keeps a `.bak-…` copy), secrets and keys are never rotated.

---

## What you get

- **Web GUI** (served by the controller): Get started, Dashboard, System status, Updates,
  Profiles, Cookbook, Model files, Mods, Planner, Diagnostics, Jobs, Logs.
- **Recipes → profiles.** Built-in researched configs and community recipes (eugr/spark-vllm-docker
  format) by URL, paste or file; immutable revisions (model commit + image digest); diff, duplicate,
  known-good tracking; *Pin & prepare* stages weights without touching the model that is serving.
- **Topologies:** `single-a`, `single-b`, `replicated`, `tp2`, `pp2`, `tp-ep`, plus **split**
  profiles (a different model on each node). Native multi-node vLLM or Ray.
- **Safe switching:** preflight → download / rsync A→B over QSFP → drain → stop → reclaim memory →
  start → health → smoke test → route. Before the stop stage the old model keeps serving; after it,
  a failure restores the previous known-good deployment.
- **Stable gateway:** aliases, real SSE streaming, `503 + Retry-After` while switching, in-flight draining,
  round-robin across replicas.
- **Memory aware:** `gpu_memory_utilization` is derived from the unified-memory reserve; page cache is
  dropped before launch (via the root helper); headless modes free the desktop's memory.
- **Operations:** system status (CPU / GPU / shm / network per node), opt-in coordinated OS/driver
  updates (B before A) with checkpoints, watchdog with auto-recovery, audit log.
- **Hands-off boot:** the controller adopts running containers after a restart and resumes the last
  healthy model after a reboot.

## How it fits together

```
 your laptop ──ssh -L 8443──┐                       clients ──► :8000 (OpenAI API, needs inference key)
                            ▼                                       │
   ┌──────────────────── node A ───────────────────────┐   QSFP    ┌──────── node B ────────┐
   │ controller + gateway ─► agent A ─► docker (vLLM)  │◄═════════►│ agent B ─► docker (vLLM)│
   │ web GUI / API (:8443)    │                        │ 200 Gb/s  │   │                     │
   │ SQLite state, vault      └─► tsm-privd (root)     │ NCCL/RoCE │   └─► tsm-privd (root)  │
   └───────────────────────────────────────────────────┘ + rsync   └─────────────────────────┘
```

- The **agent** only ever performs a fixed list of typed actions (start/stop *its own* labelled
  containers, download, verify, sync, telemetry…). There is no generic "run this command" action,
  and a hand-started vLLM is never touched.
- **`tsm-privd`** is a tiny root helper over a Unix socket with an allowlist (drop page cache,
  headless switch, maintenance).
- Secrets live in an encrypted vault; configs contain none.

| Port | Where | Purpose |
|---|---|---|
| 8443 | A, `127.0.0.1` | web GUI + management API (needs the management key) |
| 8000 | A, all interfaces | OpenAI-compatible gateway (needs the inference key) |
| 9443 | A `127.0.0.1`, B QSFP address only | agent (bearer token) |
| 8100 | each node | internal vLLM port (must not collide with a hand-started vLLM) |

| File | Purpose |
|---|---|
| `/etc/twinspark/controller.yaml`, `agent.yaml` | configuration (safe to edit; comments explain) |
| `/etc/twinspark/secrets/` | encrypted vault (keys, tokens) |
| `/var/lib/twinspark/` | state database, mods, sync key |
| `/etc/systemd/system/twinspark-{privd,agent,controller}.service` | services |

## Everyday commands

```bash
tsm status                     # active model, routes, memory, live serving metrics
tsm doctor                     # full health check with a fix for every finding
tsm models ls                  # model files on both nodes;  tsm models rm org/repo
tsm logs                       # container logs of the active deployment on both nodes
tsm headless headless-max --now    # stop the desktop session, free its memory
tsm link --mode rdma           # QSFP / NCCL link test
tsm rdma --apply               # write discovered RoCE devices into controller.yaml
tsm stop                       # drain and stop the active model
```

Everything in the GUI is the same API the CLI uses; `tsm --help` lists all commands.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot reach the controller` | `systemctl status twinspark-controller`; from a laptop you need the SSH tunnel (`ssh -L 8443:localhost:8443 …`) |
| GUI says *invalid management key* | `tsm init --show` on node A prints it |
| Node B "unreachable" on Get started | `journalctl -u twinspark-agent` on B. `401` = token mismatch: re-run `sudo tsm join-code` on A and `sudo tsm setup --join …` on B. Otherwise check B's QSFP address (`ip -br addr`) |
| `rdma_hcas is empty` / NCCL falls back to TCP | `sudo tsm rdma --apply && sudo systemctl restart twinspark-controller` |
| `permission denied … docker.sock` | the service user needs the docker group: `sudo usermod -aG docker <user>` and restart the agent |
| `port 8000 / 8100 already in use` | a hand-started vLLM is running: `tsm foreign ls` / `tsm foreign stop`, or let setup pick other ports |
| Weight sync fails | `tsm doctor` shows the SSH check; node B must accept node A's sync key (setup installs it from the join code) |
| `sudo: a terminal is required` over Tailscale SSH | start the session with `ssh -t`, or use `tmux`; setup itself only needs one `sudo` |
| Activation fails and the old model comes back | by design — read the job (`tsm job <id>`), then `tsm logs` |

## Updating and removing

```bash
cd Twinspark-Manager && git pull && sudo ./install.sh --no-setup && sudo systemctl restart twinspark-agent twinspark-controller
sudo ./install.sh --uninstall            # stop services, remove the program; config, models and state stay
sudo ./install.sh --uninstall --purge    # …and delete /etc/twinspark and /var/lib/twinspark
```

Update both nodes (the Get started page warns when versions differ).

## More documentation

- [Recipe and split-model workflow](docs/recipe-workflow.md)
- [Monitoring and coordinated maintenance](docs/maintenance.md)
- [Development, testing and the demo harness](docs/development.md)
- [Changelog](CHANGELOG.md) · [Review notes](REVIEW.md) · [Researched profiles](OPTIMAL-PROFILES.md)
- [Two-node DeepSeek startup commands](DEEPSEEK-TWO-NODE-STARTUP.md) · [Small-model validation](SMALL-MODEL-DEVELOPMENT.md)

**Not in this alpha:** mTLS between nodes (bearer token over the direct QSFP link for now),
web OAuth / multi-user (single management key).
