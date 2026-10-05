# Changelog

## 0.5.0 — 2026-10-05 — first public release

Recipe automation, agent integrations, guided setup, remote management and QSFP automation, plus a
pre-release review. The repository is MIT-licensed from this release on ([LICENSE](LICENSE),
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).

### Pre-release review

- **Vault ownership:** `sudo tsm init --hf-token …` and `sudo tsm remote plug-token` no longer leave
  root-owned secret files. Before this fix, the controller could not read them after its next restart.
  An unreadable secret now says how to fix it.
- **`tsm init --show`** only reads. Pointed at a missing vault, it no longer creates new keys.
- **`tsm qsfp` port choice:**
  - A port whose twin carries the default route is not guessed: a QSFP uplink to a switch is never picked
    when another port has a link, and planning on such a port needs `--iface` (or the interface setup
    recorded). The default-route check then covers the whole port; sessions are checked on the twins that
    change, so adding the second twin next to an SSH session on the first one still works.
  - Two cabled ports need `--iface` before anything is planned (setup uses the interface from the join code).
  - `status` no longer counts a network uplink as a second cable.
  - `revert --temporary` leaves the permanent layout alone.
  - Remote-terminal sessions count like SSH sessions (on the port set in agent.yaml).
- **Request limits:** the gateway caps request bodies at 32 MiB while reading them. With
  `management_auth: none`, the management API only answers loopback host names, which stops DNS rebinding.
- **Installer:** `install.sh` normalises `TSM_HOME` and refuses home and system directories. It marks its
  install directory, and `--uninstall` only deletes a marked one. `--purge` says that models downloaded
  under `/var/lib/twinspark` are deleted too.
- **Join code:** `tsm setup --join -` reads the code from a prompt, so it stays out of the process list and
  the shell history.
- **Dry-run pins** of local images are marked SIMULATED, and a missing local image says to pin again.
  `tsm plan` on an unpinned profile suggests `--draft`.
- **Messages:** doctor hints name today's commands (`tsm go-live`, privd, `tsm rdma --apply`). A sandbox
  setup says that it ran in a sandbox.
- **Demo:**
  - The banner prints a working inference key and a `TSM_API`/`TSM_KEY` line.
  - A missing `127.0.0.2` (macOS) gives a clear error instead of a traceback.
  - The Get started page says that its commands are for real Sparks.
- **Content:**
  - The built-in recipes no longer describe the maintainer's machines as yours.
  - The *verified* badge explains itself.
  - Personal runbooks and research moved to [docs/notes/](docs/notes/), with neutral names and paths.
  - The dev scripts take their hosts and image from the environment.


### QSFP link automation

- `tsm qsfp status|plan|apply|revert|verify|scan` ([docs/qsfp-link.md](docs/qsfp-link.md)): finds the cabled
  ConnectX port and its two twin interfaces (`enp1s0f1np1` / `enP2p1s0f1np1`), plans static addresses on
  **separate /24s** with MTU 9000 (`192.168.100.x` and `192.168.101.x`, node A = .1, node B = .2) — the
  layout of NVIDIA's two-Spark playbook and eugr/spark-vllm-docker — and writes
  `/etc/netplan/60-twinspark-qsfp.yaml`. A twin that already has an address from elsewhere is kept; only the
  missing one is added.
- Safety on headless machines: refuses the interface with the default route or an SSH session, never edits a
  netplan file it did not write (marker line), runs `netplan generate` first, verifies addresses and MTU
  afterwards and restores the previous file if they are not there. `--temporary` sets addresses until the
  next reboot; `revert` undoes the last apply. Read-only `status`, `plan`, `verify` (jumbo-frame ping of the
  other Spark through each twin, SSH check) and `scan` (finds the other Spark on the link, like eugr's
  `autodiscover.sh`).
- `tsm setup` shows the plan when the cabled port lacks addresses and asks (default no) before writing;
  `--configure-qsfp` does it unattended, `--yes` alone never touches the network. Node B takes its host number
  and subnet from the join code. The old single-interface netplan snippet is gone.
- `tsm node doctor` lists the QSFP findings as warnings with the fixing command.
- `suggest_rdma` (and so `tsm rdma`, setup and the GUI) now accepts the second twin on its own subnet; before it
  only recognised the layout where both twins share one subnet, which eugr's guide advises against, and
  its advice said to put them on the same one.
- Hardened after an independent review: a hand-written `60-twinspark-qsfp.yaml` (what the old setup snippet told
  people to create) is never overwritten — TwinSpark uses `61-…` instead; backups are kept root-owned in
  `/var/backups/twinspark-qsfp` and verified before `revert` copies them back; the apply window ignores
  Ctrl-C/hang-up, checks that the management interface kept its route and addresses, and a rollback also removes
  addresses and MTU that networkd would keep; `revert` has `apply`'s guards; IPv6 link-local SSH sessions and
  unreadable routing tables are handled; the netplan file is `optional: true` so boot never waits for the link;
  temporary addresses are recorded in `/run` and `revert --temporary` needs no `--node`.
- Tested against a fake Spark (`tests/qsfp_fakes.py`): fake `/sys`, `ip`, `ss`, `ping` and a `netplan` that
  turns YAML into addresses, including rollback paths. **Not yet run on real hardware.**

### Recipe automation and agent integrations

- Reviewed recipe imports can pin and prepare both models in durable jobs with cancellation
  and exact-revision retries. Source tracking compares upstream updates while retaining local
  settings and displaying conflicts before creating a new experiment.
- The Integrations page generates setup files for OpenCode, Aider, LangGraph/LangChain,
  OpenClaw, Hermes and LM Evaluation Harness, including separate coding/planning aliases.
- Compatibility jobs check chat, streaming, tool-result turns and structured output. Dry-run
  results remain simulated; imported harness metrics remain reported evidence tied to an
  immutable recipe revision.
- Gateway streams clean up on cancellation. Read/write failures and HTTP 502/504 responses
  are not replayed; explicit 503 responses may fail over to another replica. Probe responses
  have byte and time limits.
- Combined startup recovery respects operator stops and switches made while nodes reconnect.
  Automatic recipe jobs retain profile deletion protection. Installer elevation preserves
  command options, and uninstall removes the terminal service along with other manager units.
- Maintenance restoration verifies the exact saved revision while retaining startup race protection.
  Remote power scheduling and plug cycles reserve the cluster until the request finishes; explicit
  force and recovery power-on retain their existing behavior.
- Request body limits count streamed bytes as well as declared lengths. Windows demos support
  native cache paths and short directory names, close their database before cleanup, and report
  unsupported host readings as unavailable. Linux-only terminal and root operations fail clearly.
- Mod publication on systems without Linux atomic exchange restores the previous installation
  after a failed replacement, retaining a recovery backup if restoration also fails.

### Setup and first run (milestone 1)

- `install.sh` + `tsm setup`: one command per node. Detects the QSFP interface, RoCE devices and GID
  index, an existing Hugging Face cache, docker access, the Tailscale address and taken ports; writes
  validated `controller.yaml` / `agent.yaml`, the secret vault, a dedicated SSH sync key and systemd units.
  Starts in dry-run. `--yes` for unattended installs, `--dry` to preview, `--root DIR` for a sandbox.
- Node B joins with a single pasted **join code** (`tsm setup --join …`, `tsm join-code` prints it again):
  shared secrets, addresses and node A's sync key (authorised only from node A's QSFP address).
- `tsm go-live [--revert]`, `tsm rdma --apply`, `tsm --version`.
- **Get started** page + `GET /api/v1/system/onboarding`: live checklist with the exact next command.
- `tsm demo` / `twinspark.demo.DemoCluster`: the whole stack (two agents, controller, gateway, GUI) on
  loopback for trying the product and for process-level tests.
- New `runtime.ssh_key` / `runtime.ssh_known_hosts` settings; generated systemd units keep the model
  cache and sync key writable under `ProtectSystem=strict` (the previous example unit could not write
  `known_hosts`, which breaks weight sync when the agent runs under systemd).
- README rewritten around the quick start; development notes moved to `docs/development.md`.

### Error testing and hardening (milestone 2)

Everything below was found by exercising the stack on loopback (`DemoCluster`, dry-run runtime),
fuzzing the API and driving the GUI at desktop and phone widths — nothing was tried on the live
Sparks. Each fix has a regression test (`tests/test_hardening_*.py`).

**Security**
- Management and agent tokens are compared byte-wise in constant time, so a non-ASCII header can no
  longer cause a 500; authentication runs before the request body is read; failed logins have their own
  rate bucket and cannot lock the operator out of the GUI.
- `/docs`, `/redoc` and `/openapi.json` are disabled on the controller and agent; validation errors are
  sanitised (no echoed input, no secrets in 422 bodies).
- Secret references (`${secret:slot}`) are only honoured for the documented slot → variable pairs
  (`backend_api_key` → `VLLM_API_KEY`, `hf_token` → `HF_TOKEN`). A profile, recipe or mod cannot read the vault.
- Container mounts must match a strict path pattern and are mapped on the agent, not trusted from the
  controller. Containers start with `--pull never`; secrets reach them through a 0600 `--env-file`,
  not on the command line.
- Outbound fetches (recipes, cookbook sources) are limited to public `https://` hosts without credentials
  (`twinspark/netguard.py`); redirects are re-checked.
- Mod archives are bounded (64 MiB archive, 512 MiB extracted, 20 000 files, 64 mods), reject links and path
  escapes, and are swapped in atomically (`RENAME_EXCHANGE`) so a failed install never leaves half a mod.
- Vault writes are atomic (temp file + fsync + replace); join codes and the sync public key are validated
  before anything is written to `authorized_keys`.

**Reliability**
- The watchdog no longer burns its hourly recovery budget while a node is unreachable; it waits, logs
  once and recovers with the full budget when the node returns. It also stands down when a switch or
  prepare starts mid-probe.
- After a reboot the controller waits (up to `startup_wait_s`, default 180 s) for the other node's agent
  instead of failing recovery because it started a few seconds earlier. The wait runs in the background,
  so the GUI is available immediately.
- Maintenance retries a lost reboot request instead of waiting forever.
- Stopping a deployment whose node was removed from the plan is logged and skipped rather than raised.
- A profile that is being activated cannot be deleted; failed model-file deletes are reported, not
  swallowed; per-node failures (mods, files) surface as errors in the GUI.

**GUI**
- No horizontal page scroll at phone width (profile Overview, tag chips, grids), a Copy button that no
  longer covers the command, scrollable tab strips with a visible edge, higher-contrast secondary text.
- Unknown profiles/jobs/routes show a "Not found" page with ways back instead of a dead end; a hand-typed
  `%` in the URL no longer breaks routing.

### Remote management for headless nodes (milestone 3)

Full guide: [docs/remote-management.md](docs/remote-management.md). Everything that changes a node is
**off until switched on, on that node**, in a root-owned `/etc/twinspark/remote-policy.json` that fails
closed (`sudo tsm remote enable|disable|policy`, or `tsm setup --remote …`).

- **Terminal**: opt-in `twinspark-terminal` service (PTY over WebSocket, separate from the sandboxed
  agent), in the **Remote** page (xterm.js, vendored; touch keys for phones) and as `tsm remote terminal B`
  (raw TTY, Ctrl-] . to leave). One-time 30 s tickets, Origin check, idle/total limits, at most 2 sessions
  per node, every session recorded as asciicast and audited.
- **Diagnostics without a shell**: `tsm remote reach` (agent / SSH / terminal probes and a verdict:
  ok, agent_error, agent_down, link_down, host_down, each with next steps), `tsm remote logs` (agent,
  kernel, previous boot, docker, …), `tsm remote bundle` (redacted support archive, size- and time-bounded).
- **Power**: reboot / power-off scheduled a few seconds ahead through `twinspark-privd`, typed
  confirmation (`REBOOT B`, `POWEROFF B`), cancel, refused while the cluster is busy unless forced.
- **Out-of-band**: boot-once from network / USB via UEFI BootNext (`tsm remote boot`), Wake-on-LAN
  (node setting and `nodes.<id>.wake` magic packet), HTTP smart plug (`nodes.<id>.plug`, token in the vault as
  `${secret:plug_token}`, typed `CUT POWER B`).
- **When the controller is down**: `sudo tsm node status|doctor|logs|bundle|reboot|poweroff|cancel|boot|wol`
  work on the node alone; `tsm wake MAC` and `tsm netboot plan|serve` (short-lived proxyDHCP/TFTP rescue
  helper locked to one MAC) are standalone.
- `tsm demo --with-terminal`, `DemoCluster(remote={...})`; the setup wizard asks which features to enable.
- Controller→node traffic uses typed actions only (allowlist; no generic exec); the remote endpoints stay
  reachable during a maintenance run so a node can still be inspected.
- New tests cover the policy, root helper, terminal relay, controller, CLI and docs examples; the Remote
  page was also checked in a real browser at desktop and phone width.
- **Not verified on the hardware**: Wake-on-LAN, BootNext and network boot depend on DGX Spark firmware;
  the guide says how to test each once while the machine is still reachable.

## 0.4.1 — 2026-10-01

- Prepare pinned recipes in a background job without stopping the active model:
  check memory, images, mods, then stage and check main/drafter weights.
- Support split profiles with separate models, images, settings, memory fractions
  and aliases on A/B. Pin both together; snapshot source recipes when composing.
  Route and smoke-test each backend with its own served model name, restore both
  routes on restart, and protect both models and mods from deletion while active.
- Add local recipe-file import, a Combine two recipes dialog, Pin & prepare,
  per-node fit/launch-plan displays, and a switch to the exact prepared revision.
  Add `tsm split` and `tsm prepare` commands.
- Track recipe download includes in snapshot manifests. Download newly required
  files instead of reusing an incomplete cache; reject patterns matching no files.
  Fail staging when copied weights have no successful checksum verification.
- Explicitly select vLLM's `mp` executor for native two-node launches and reject
  TP2 when a known attention-head count cannot divide across the two GPUs.
- Add split/preparation/cancellation/rollback/restart/backend-routing regressions,
  distributed TP2/PP2/TP-EP checks, and recipe-file/cache tests. Hardware tuning
  and real inference on the Sparks are deliberately deferred to on-site work.
- Local validation: **147 Python tests passed, 5 platform-related skips**, and
  **13 web-client tests passed**. Recipe-file import, split composition and
  preparation were checked in the browser; the 0.4.1 wheel builds successfully.

### Fixes after 0.4.0 (released with 0.4.1)

#### Monitoring and maintenance

- Add parallel per-node CPU, shared memory, GPU use/temperature/power, and network
  sampling, with bounded history, unknown sensor values, and stale-data handling.
- Add responsive System status and Updates pages, live dashboard tiles, and a
  clearly labeled local preview with two simulated nodes.
- Add opt-in OS/driver installation, separately opted-in firmware, B-before-A
  reboot sequencing, and restoration of the exact previous model revision.
- Persist maintenance checkpoints; run package installation independently of
  agent restarts. Hold on failures, refuse concurrent model changes, verify
  workloads are stopped, and check package/firmware/GPU/RDMA health after reboot.
- Require a root-owned policy on each real node. No automatic schedule, app
  self-update, image replacement, or automatic OS/firmware rollback is enabled.
- Validate with isolated dry-run agents and mocked privileged commands. Actual
  Spark updates and firmware/reboot behavior remain to be validated on hardware.

#### GUI and web client

- Search and filter profiles by state and built-in recipes by topology, with
  result counts, useful empty states, and filters retained during navigation.
- Add an import → customize/pin → plan/run guide, responsive library layouts,
  keyboard focus indicators, and reduced-motion support.
- Review built-in and community imports in a shared dialog. Save the exact
  previewed profile; keep errors and entered names visible when import fails.
  Validate template overrides instead of silently dropping malformed lines.
- Keep modal keyboard focus contained, restore focus on close, and prevent
  duplicate submissions while a request is running.
- Fix navigation with authentication disabled and keep the login gate open
  when the controller cannot be reached. Highlight saved but unpinned edits.
- Decode bundled recipes as UTF-8 to prevent garbled punctuation on Windows.
- Add a local in-memory GUI preview and nine client regression tests.

#### Backend and packaging

- Include built-in recipes, recipe metadata, and the web UI in installation
  packages. Previously they were only available when running from the source tree.
- Validate recipe YAML field shapes and template expansion before importing;
  malformed defaults, environment fields, build arguments, and JSON provenance
  now return validation errors instead of server errors.
- Reject zero/negative parallel sizes, unsupported distributed backends, and
  invalid memory utilization. Preserve supported backend selections and stop
  silently clamping or ignoring explicit memory settings.
- Use portable host information for local dry-run agents; sort RDMA device names
  consistently across platforms.
- Add 28 recipe regression cases. Separate POSIX executable-bit checks and probe
  Windows symlink permissions so unsupported tests report explicit skips.
- Validation on Windows: **126 passed, 5 skipped**, plus **9 client checks**. Built and installed a wheel
  separately from the source tree; all six recipes and metadata load, and the
  homepage, JavaScript, CSS, and cookbook API respond successfully. Real Spark
  hardware and the five filesystem-dependent cases still need Linux validation.

## 0.4.0 — 2026-09-30

Launches now match the community's proven dual-Spark setup (eugr/spark-vllm-docker), recipes import losslessly,
and model files / mods are managed on both nodes.

### Launch wiring
- RoCE for NCCL: `rdma_hcas` + `ib_gid_index` per node (both PCIe halves, e.g.
  `rocep1s0f1,roceP2p1s0f1`, GID 3) → `NCCL_IB_HCA`, `/dev/infiniband` + `IPC_LOCK`
  (or `container_mode: privileged`), nofile 1048576, NCCL/Gloo/UCX interface vars.
- Worker (rank 1) starts first, head after `head_start_delay_s`; persistent JIT caches.
- Mods (eugr format, folder with `run.sh`) applied in the container before `exec vllm serve`.
- Raw recipe args kept verbatim and in order (dotted flags, backend flags); manager-owned
  flags (host/port/parallel wiring) are rejected.
- Page cache dropped (tsm-privd) and a free-memory gate before every launch.

### Recipes
- eugr/spark-vllm-docker YAML importer with a mapping report (mapped / raw / dropped).
- Built-in: DeepSeek V4 Flash 0731 B12X (the maintainer's running setup), GLM-5.3-Flash NVFP4 (+ DFlash2),
  MiMo-V2.6-Flash, Qwen3.8-Flash-Next, SmolLM2 smoke test.
- Community browser for GitHub recipe folders (`recipe_sources`), import by URL or paste.
- One-click Pin: HF commit sha, multi-arch registry digest or local image ID (checked identical
  on both nodes), drafter repos pinned and injected into `speculative_config`.

### Model files & mods
- Inventory of both HF caches (sizes, completeness, verified, refs, which profiles use what).
- Stage = download once + parallel rsync over QSFP + hash verify; safe delete that keeps
  blobs shared with other snapshots/repos; refuses active/staging models.
- Mods: install from zip/tar/dir, identical hash on both nodes, `tsm mods import-eugr`.

### Operations
- Watchdog with auto-recovery budget, gateway failover/mark-down, cancel before destructive stages.
- Live metrics (decode/prefill tok/s, KV %, TTFT, ITL, spec-decode acceptance) with history.
- Doctor, RDMA discovery, TCP + RDMA (ib_write_bw) link tests, headless mode via privd,
  foreign (hand-started) vLLM detection/stop.
- Memory fit per profile with estimated KV tokens, next to the observed KV pool of the last run.

### Interfaces
- New web GUI (dashboard, profiles with settings/JSON editor/revisions/plan, cookbook,
  model files, mods, planner, diagnostics, jobs, logs).
- New CLI: `pin`, `edit`, `export`, `fit`, `cancel`, `job`, `models ls|stage|rm`,
  `mods ls|install|import-eugr|rm`, `cookbook show|community|import-recipe`, `doctor`,
  `rdma`, `link --mode rdma`, `metrics -w`, `headless --now`, `foreign`, `hardware`, `--json`.

### Not verified on hardware yet
- Everything was tested against dry-run agents (80 tests). First run on the Sparks:
  `tsm doctor`, `tsm rdma`, then `tsm plan <profile>` and compare with the command you use today
  before the first `tsm activate`.
