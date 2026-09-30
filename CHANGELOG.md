# Changelog

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
