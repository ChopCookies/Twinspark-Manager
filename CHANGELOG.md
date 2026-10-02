# Changelog

## Unreleased — quick start, error testing, remote management

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
Sparks. Each fix has a regression test (`tests/test_hardening_*.py`, 296 tests in total).

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

## Unreleased — v0.4 fixes

### Monitoring and maintenance

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

### GUI and web client

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

### Backend and packaging

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

Launches now match the proven dual-Spark setup, community recipes import losslessly,
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
- Built-in: DeepSeek V4 Flash 0731 B12X (your running setup), GLM-5.3-Flash NVFP4 (+ DFlash2),
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
  `tsm doctor`, `tsm rdma`, then `tsm plan <profile>` and compare with your eugr command
  before the first `tsm activate`.
