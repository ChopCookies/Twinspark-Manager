# TwinSpark Manager

One web page, one CLI and one stable OpenAI-compatible endpoint for **two NVIDIA DGX Spark
(GB10) machines**. Switch between community vLLM recipes, manage model files on both nodes,
and keep the machines headless.

> **Status: alpha (0.5.0).** The control path is covered by automated tests (about 900). Real Docker
> activation, chat, streaming and shutdown passed on two DGX Sparks with a 135M model (single node
> and two-node PP2) on 2026-09-28, at the code of that day — see the
> [validation note](docs/notes/small-model-validation-2026-09-28.md). Large models, other topologies
> and the newer features (remote management, `tsm qsfp`) still need on-site validation, which is why a
> new install starts in **dry-run** (nothing is run on your machines) until you say otherwise.

---

## Try it first — no Spark needed (1 minute)

```bash
git clone https://github.com/ChopCookies/Twinspark-Manager && cd Twinspark-Manager
python3.12 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/tsm demo
```

`tsm demo` starts two simulated Sparks, the controller, the gateway and the web GUI on this
machine (real HTTP between them, containers simulated) and prints the URL, the keys and a line that
points the CLI at it. Open a profile and press *Plan*, browse the Cookbook; pinning a recipe looks up
the model and image online, so it needs internet access. Ctrl-C removes everything.

Linux and Windows (use `.venv\Scripts\tsm`) work as they are. On macOS the simulated node B needs a
second loopback address first: `sudo ifconfig lo0 alias 127.0.0.2 up`.

## Install on your two Sparks (about 10 minutes)

You need: both Sparks cabled QSFP-to-QSFP, Docker (ships with DGX OS), Python 3.12+ (Ubuntu 24.04
has it), and a user with `sudo`. Setup does not change your network configuration unless you say so
(see the QSFP step below).

**1. Node A** (the machine that will host the controller and the OpenAI endpoint):

```bash
git clone https://github.com/ChopCookies/Twinspark-Manager && cd Twinspark-Manager
sudo ./install.sh
```

The installer puts the program in `/opt/twinspark`, then starts the guided `tsm setup`. It
detects almost everything and only asks what it cannot know:

| Step | What it does for you |
|---|---|
| QSFP link | finds the cabled interface, its addresses and both RoCE devices; if the link has no (or only one) address it shows the two-subnet plan and **asks** before writing any netplan file — [`tsm qsfp`](docs/qsfp-link.md) |
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

Lost the line? `sudo tsm join-code` on node A prints it again. To keep the code out of the process
list and your shell history, use `--join -` and paste it at the prompt.

**3. Open the GUI.** From your laptop: `ssh -L 8443:localhost:8443 you@node-a`, browse to
<http://localhost:8443/> and paste the management key (`tsm init --show` prints it again).
The **Get started** page checks both nodes and shows the next step with the exact command.

**4. Run a model.**

```bash
tsm cookbook list                          # built-in dual-Spark recipes
tsm cookbook show glm-5.3-flash-nvfp4-tp2  # what it needs: image, weights, mods
tsm cookbook import glm-5.3-flash-nvfp4-tp2
tsm pin glm-5.3-flash-nvfp4-tp2            # freezes the model commit + image digest (needs Hub + registry access)
tsm plan glm-5.3-flash-nvfp4-tp2           # the exact docker/vllm commands for A and B, nothing started
tsm activate glm-5.3-flash-nvfp4-tp2       # downloads the weights once, copies them to B over QSFP, starts
```

Each recipe lists what it needs. Some use a public image that pinning resolves and activation pulls (the
GLM example above, about 184 GiB of weights). Others, such as `deepseek-v4-flash-0731-b12x`, run on a
vLLM image you build yourself with [eugr/spark-vllm-docker](https://github.com/eugr/spark-vllm-docker);
`tsm cookbook show` says how.

Clients use `http://node-a:8000/v1`, model `default`, with the `inference_api_key`
(`tsm init --show`).

**5. Go live.** While in dry-run, `activate` walks the whole pipeline with simulated
containers, so you can compare `tsm plan` with the command you run by hand today. When it
matches, stop any hand-started vLLM (`tsm foreign ls`) and on **each** node run:

```bash
sudo tsm go-live        # runtime_mode: docker + restarts the agent;  `--revert` goes back
```

Start small: the `smollm2-135m-smoke` recipe (260 MiB) checks the whole path in a minute. Import it and
pin it with the image of the recipe you plan to run (`tsm cookbook import smollm2-135m-smoke`, then
`tsm pin smollm2-135m-smoke --image <image>`). `tsm doctor` checks everything
a fast, stable dual-Spark setup needs (driver parity, RDMA, headless, privd, SSH, disk).

### Unattended / scripted

```bash
sudo ./install.sh --yes --qsfp-iface enp1s0f1np1 --qsfp-ip 192.168.100.1 --runtime-mode dry-run
sudo tsm setup --dry          # show what would be written, change nothing
tsm setup --root /tmp/stage --yes --no-start           # build a complete install in a scratch directory (no root needed)
```

`tsm setup --help` lists every flag. Re-running setup is safe: existing config files are kept
(`--force` regenerates them and keeps a `.bak-…` copy), secrets and keys are never rotated.

---

## What you get

- **Web GUI** (served by the controller): Get started, Dashboard, System status, Updates,
  Profiles, Cookbook, **Integrations**, Model files, Mods, Planner, Diagnostics, **Remote**, Jobs, Logs.
- **Recipes → profiles.** Built-in researched configs and community recipes (eugr/spark-vllm-docker
  format) by URL, paste or file; immutable revisions (model commit + image digest); diff, duplicate,
  known-good tracking; *Pin & prepare* stages weights without touching the model that is serving.
  Reviewed imports can pin and prepare automatically; source updates retain local experiment settings
  and show conflicts before creating an updated profile.
- **Agent clients and evaluation harnesses.** Generate setup files for OpenCode, Aider,
  LangGraph/LangChain, OpenClaw, Hermes and LM Evaluation Harness from stable model aliases.
  Use separate coding/planning models, check protocol compatibility, and record evaluation metrics
  against exact recipe revisions. Simulated checks and imported reports keep their evidence labels.
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
- **Remote management for headless nodes** (opt-in, per node): a recorded browser/CLI terminal, node
  logs and a redacted support bundle, reboot / power-off with typed confirmation, boot-once from
  network or USB, Wake-on-LAN, smart-plug power-cycling and a "why can't I reach it?" triage. See
  [docs/remote-management.md](docs/remote-management.md).

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
- Secrets live in the vault (encrypted files, mode 0600, owned by the service user); configs contain none.
- The service user is in the `docker` group, which makes it root-equivalent on that machine — keep the
  management key as safe as a root password. See [docs/security.md](docs/security.md).

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
tsm qsfp status                # QSFP link on THIS node: both twins, addresses, MTU, RoCE (docs/qsfp-link.md)
tsm stop                       # drain and stop the active model
```

Everything in the GUI is the same API the CLI uses; `tsm --help` lists all commands.

### The QSFP link (both interfaces, ~200 Gb/s)

Each QSFP port is fed by two PCIe halves — two interfaces, two RoCE devices — and NCCL only reaches
~200 Gb/s when both have an address, on **separate subnets** (the layout NVIDIA's playbook and
[eugr/spark-vllm-docker](https://github.com/eugr/spark-vllm-docker) use). `tsm qsfp` sets that up
without the controller and without risking the management network:

```bash
tsm qsfp plan --node A                    # what would change; writes nothing
sudo tsm qsfp apply --node A --temporary  # try it: addresses only until the next reboot
tsm qsfp verify                           # ping the other Spark through each twin, jumbo frames
sudo tsm qsfp apply --node A              # make it permanent (netplan; `sudo tsm qsfp revert` undoes it)
```

It refuses the interface that carries the default route or your remote session, never edits a netplan
file it did not write, validates with `netplan generate`, and puts the old settings back if the
addresses do not come up. `tsm setup` offers the same step (answer *no* by default; `--configure-qsfp`
does it unattended) and `tsm node doctor` reports the link. Details and the honest status — tested
against a fake machine, not yet on real Sparks — are in [docs/qsfp-link.md](docs/qsfp-link.md).

### When a node misbehaves (headless)

```bash
tsm remote reach B             # agent / SSH / terminal probes → what to do next
tsm remote logs B --source previous-boot     # what happened before the last reboot
tsm remote bundle B            # one redacted tar.gz for a bug report
sudo tsm remote enable terminal      # on a node: switch on a recorded shell (off by default)
tsm remote terminal B          # open it (or use the Remote page)
sudo tsm node doctor           # on a node, with or without the controller
```

Power control, Wake-on-LAN, smart plugs and network boot are covered in
[docs/remote-management.md](docs/remote-management.md). Everything that changes a node is off until you
switch it on, on that node.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot reach the controller` | `systemctl status twinspark-controller`; from a laptop you need the SSH tunnel (`ssh -L 8443:localhost:8443 …`) |
| GUI says *invalid management key* | `tsm init --show` on node A prints it |
| Node B "unreachable" on Get started | `journalctl -u twinspark-agent` on B. `401` = token mismatch: re-run `sudo tsm join-code` on A and `sudo tsm setup --join …` on B. Otherwise check B's QSFP address (`ip -br addr`) |
| NCCL tops out near 100 Gb/s | only one QSFP interface has an address: `tsm qsfp status`, then `sudo tsm qsfp apply` (see [docs/qsfp-link.md](docs/qsfp-link.md)) |
| `rdma_hcas is empty` / NCCL falls back to TCP | `sudo tsm rdma --apply && sudo systemctl restart twinspark-controller` |
| `permission denied … docker.sock` | the service user needs the docker group: `sudo usermod -aG docker <user>` and restart the agent |
| `port 8000 / 8100 already in use` | a hand-started vLLM is running: `tsm foreign ls` / `tsm foreign stop`, or let setup pick other ports |
| Weight sync fails | `tsm doctor` shows the SSH check; node B must accept node A's sync key (setup installs it from the join code) |
| `sudo: a terminal is required` over Tailscale SSH | start the session with `ssh -t`, or use `tmux`; setup itself only needs one `sudo` |
| Activation fails and the old model comes back | by design — read the job (`tsm job <id>`), then `tsm logs` |
| A node stopped answering | `tsm remote reach B` says which case it is; the checklist is in [docs/remote-management.md](docs/remote-management.md) |

## Updating and removing

```bash
cd Twinspark-Manager && git pull && sudo ./install.sh --no-setup && sudo systemctl restart twinspark-agent twinspark-controller
sudo ./install.sh --uninstall            # stop services, remove the program; config, models and state stay
sudo ./install.sh --uninstall --purge    # …and delete /etc/twinspark and /var/lib/twinspark
```

Update both nodes (the Get started page warns when versions differ).

## More documentation

- [Recipe and split-model workflow](docs/recipe-workflow.md)
- [Automatic recipe imports and source updates](docs/recipe-automation.md)
- [Agent clients and evaluation harnesses](docs/agent-integrations.md)
- [Monitoring and coordinated maintenance](docs/maintenance.md)
- [Remote management: terminal, logs, power, Wake-on-LAN, network boot](docs/remote-management.md)
- [The QSFP link: both interfaces, addresses, MTU, verification](docs/qsfp-link.md)
- [Development, testing and the demo harness](docs/development.md)
- [Security model and known limits](docs/security.md) · [Reporting a vulnerability](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Field notes and research](docs/notes/): the first hardware validation, a DeepSeek launch with eugr's
  launcher, the research behind the built-in profiles

**Not in this alpha:** mTLS between nodes (bearer token over the direct QSFP link for now),
web OAuth / multi-user (single management key).

## Licence and credits

MIT — see [LICENSE](LICENSE). The bundled DeepSeek recipe is eugr's (MIT) and the GUI ships xterm.js
(MIT); notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The recipe format, the
two-subnet QSFP layout and much of what TwinSpark knows about running vLLM on two Sparks come from
[eugr/spark-vllm-docker](https://github.com/eugr/spark-vllm-docker), NVIDIA's DGX Spark playbooks and
the community recipe authors credited in each built-in recipe.
