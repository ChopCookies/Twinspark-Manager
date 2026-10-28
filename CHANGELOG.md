# Changelog

## 0.8.0 — 2026-10-28 — `tsm update`

Updating TwinSpark used to mean `git pull` and `install.sh` on each node, then restarting the right services
in the right order. From this version on, `tsm update` installs releases from GitHub on both Sparks and on
the workstation, and goes back on its own if a new version does not start. The rest of this release is what
the review of 0.7 suggested: SSH host-key pinning between the nodes, alert rules, a playground export, and CI
for Windows, types and coverage.

### `tsm update`

- **Releases.** Pushing a tag `vX.Y.Z` builds and publishes a GitHub release (`.github/workflows/release.yml`)
  with the wheel, the sdist, `constraints-release.txt` and `SHA256SUMS`. The release notes come from
  `CHANGELOG.md`. Publishing to PyPI is optional, through a trusted publisher and the repository variable
  `PUBLISH_PYPI`.
- **`tsm update sparks`** (and **Updates → TwinSpark** in the GUI) updates node B, then node A, without
  stopping the model. Each node downloads the release, checks every file against `SHA256SUMS` and GitHub's
  digest, installs it next to a backup of `/opt/twinspark/venv`, verifies it and restarts the TwinSpark
  services. If the new version does not install, import or stay up, the node rolls back on its own. The work
  runs as root in its own systemd unit, with the system Python and a stdlib-only worker, so it survives the
  restarts. Node A's controller restarts in the middle of the run and finishes it from its database. Before
  node A's gateway restarts, the run waits for requests in flight. `--version X` installs a published
  version, also an older one.
- **`tsm update here`** updates the workstation client (pipx or venv; a checkout or a Spark install is
  refused with the reason). On Windows it continues outside `tsm.exe` so the launcher can be replaced. The
  client's Connection page says when a newer client is published.
- **`/etc/twinspark/update-policy.json`** (written once by install.sh) switches updates on or off per node:
  `sudo tsm update policy off|on|show`. Nothing reachable over the API can change it.
- `tsm update` / `tsm update status`: versions on every node and on this computer, the newest release, the
  progress of a run. A metric `twinspark_update_in_progress`, and a dashboard notice while a run is going.
- Coming from 0.7: update once by hand (`git pull && sudo ./install.sh --no-setup`, then restart privd, the
  agent and the controller). See [docs/updating.md](docs/updating.md).

### Security

- **Node B's SSH host key is pinned for weight copies.** Node A's agent copied over SSH with
  `StrictHostKeyChecking=accept-new`, so it took the first key it saw on trust. Now the controller asks node B's
  agent for its host keys, over the agent connection it authenticates with B's token. Node A pins exactly
  those keys in the sync's own `known_hosts` and connects with `StrictHostKeyChecking=yes`. `tsm doctor`'s SSH
  check does the same and says so. A node B older than 0.8 falls back to the previous behaviour.
- `tsm qsfp scan --identify` no longer adds the keys of whatever answers on the link subnet to your
  `known_hosts`. It only identifies hosts whose key you have already confirmed.

### Monitoring

- **Alert rules** in `deploy/observability/alerts.yml`, loaded by the compose setup. They cover:
  - an unreachable controller or a node that is down
  - an alias or a split half that is down (not during a switch or an update)
  - gateway errors and stuck requests
  - stale engine metrics
  - low memory, memory pressure and swap use
  - a hot GPU
  - nodes on different versions

  CI checks them with `promtool check rules` and unit-tests them with `promtool test rules`.
- New metrics: `twinspark_node_info{version}` and `twinspark_update_in_progress`.

### Playground

- **Export** the conversation as Markdown (questions, answers, reasoning, the numbers of each answer) or as
  JSON (`twinspark-playground/1`, messages in the shape the chat API takes).

### CI and code health

- Windows job for the workstation side (client, `tsm update`, portability), run in the ANSI code page.
- mypy job: every module is checked except a recorded list of older ones, which is shortened module by module.
  This release fixes the findings in the others, including a client integration route that answered 500
  instead of 404 when a served model had no id.
- Coverage on the newest-dependency job, with a report in the job summary and a floor of 80 %.
- `.github/dependabot.yml` for the GitHub actions and the release pins.

## 0.7.1 — 2026-10-27 — fixes from a full review of 0.7.0

### Fixes

- **Uninstall stopped only four units.** `install.sh --uninstall` left `twinspark-gateway.service` (new in 0.7)
  and `twinspark-terminal.socket` enabled: the gateway kept restarting a deleted program. It now stops and
  removes every unit `tsm setup` can write, and a test keeps the two lists equal.
- **Going back to `gateway_mode: in-process`** left `twinspark-gateway.service` running on the gateway port,
  so the controller's in-process gateway could not start. `sudo tsm setup` now stops, disables and removes
  the unit (keeping a `.bak` copy) before the controller restarts.
- **The image check before a switch** could pull an image inside the check (`docker run` without
  `--pull never`), left its container running when it timed out (killing the Docker CLI does not stop the
  container: up to 6 GiB next to the serving model), and that container was then listed as a hand-started
  inference container. The probe now never pulls, runs without capabilities, is named and labelled, is
  removed after a timeout, and is never counted as foreign.
- **Token usage could be lost or counted twice** when `GET /api/v1/usage` (a worker thread) flushed the
  counters while requests ended on the event loop. The hand-over is one locked swap now. Publishing the
  gateway state from a worker thread copies the routing first instead of iterating it while it changes.
- **`tsm` on Windows:** `tsm mods file-patch` wrote its `run.sh` with CRLF line endings, which bash on the
  Spark cannot run, and `tsm cookbook import FILE`, `tsm edit` and config loading read files in the
  Windows code page instead of UTF-8 (a recipe with an umlaut failed). These read and write UTF-8 with LF
  now, `tsm mods install` converts a CRLF `run.sh` to LF, and a test keeps workstation-side modules from
  relying on the platform encoding.
- **`tsm demo` could fail to start** ("demo server failed to start … address already in use"): the agent
  and terminal ports were only checked on 127.0.0.2, but agent A listens on 127.0.0.1, where any local
  service may already hold that port. The demo now picks ports free on both addresses.
- Background tasks of the terminal (the shell-exit watcher) and of the client (closing a replaced HTTP
  client) are kept referenced until they finish.

### Dependencies

- Lower bounds raised past published advisories: FastAPI ≥ 0.133 with **Starlette ≥ 1.3.1**, **anyio ≥ 4.14.2**,
  **cryptography ≥ 50.0** (pytest ≥ 9.0.3, pytest-asyncio ≥ 1.3 for development). `constraints/release.txt`
  regenerated (it pinned a certifi with an advisory).
- CI: a dependency-advisory job (`pip-audit` on the release and the minimum set), a weekly scheduled run,
  and the GitHub actions moved to their Node 24 versions.

### Cleanup

- Removed `twinspark/artifacts.py` and `twinspark/schemas/artifact.py`, which nothing used.

## 0.7.0 — 2026-10-19 — measured, not guessed

0.6 moved the work off the Sparks. 0.7 makes what TwinSpark says about a deployment checkable: memory
forecasts tied to the exact settings they were measured with, a benchmark runner whose results are stored
against a revision, metrics for Prometheus, and a recipe catalog with community results. Around it the
gateway grew up (one key per app, affinity, fallbacks, its own process) and SGLang joined vLLM. The plan
behind it compared TwinSpark with GPUStack and took ideas, not code.

### Correctness first

- **Measurements tied to their settings.** KV bytes per token and start-up footprints are stored under a
  fingerprint of what they depend on (model commit, image digest, engine, KV dtype, topology, attention and
  cache flags, speculation, node); a different setup is not a measurement. A recipe's KV size applies only
  while the profile keeps the settings it was measured with. Model specs are per repository and commit.
- **Real calibration.** Successful starts record vLLM's KV pool and each node's start-up footprint; the
  planner uses them with sample count, median, spread and an allowance while there are few. Dry runs are
  never recorded.
- **Verdicts that name their numbers:** "with --gpu-memory-utilization=0.60 vLLM may use 76.8 of 128.0 GiB
  per node and needs 286.5 GiB (weights 261.9, KV cache 10.0, …) — 209.7 GiB short per node".
- **A safe drain policy.** A manual switch no longer cuts off running requests: *abort* (default) fails the
  switch before anything stops and reopens the old model when requests outlast `drain_timeout_s`; *wait*
  lets them finish (cancellable); *force* is 0.6's behaviour. Status shows the oldest request's age.
- **CI on every push and pull request:** the minimum and the newest dependency sets, GUI tests, ruff, a
  wheel install smoke test, the recipe catalog check; failures arrive as annotations.
  `constraints/minimum.txt` and `constraints/release.txt` (what `install.sh` installs).
- **Validation records** (`tsm validation export`): what was tested with what — manager commit, agents,
  model commit, image digest, mod hashes, settings, stages, benchmarks — with evidence classes
  (manager-validated, dry-run, external). [docs/validation.md](docs/validation.md)
- **Virtual interfaces** (docker, br-, veth, cni, flannel, tailscale, VLANs, macvlans, …) never count as a
  QSFP port, and a RoCE GID that belongs to one is never chosen for NCCL.

### Measuring

- **Benchmark runner** (`tsm bench`, the *Benchmarks* page; runs from the client through the gateway):
  max-throughput (the knee), latency-slo (the highest concurrency meeting TTFT/TPOT p95), context (a ladder
  up to the deployed limit with needle retrieval), quality (retrieval and a tool call), fixed. Exact output
  lengths, a fresh seed and prompt per request, a run invalidated by a switch. Results are stored on node A
  against the revision and appear in its validation record; *Compare* measures two revisions back to back.
  [docs/benchmarks.md](docs/benchmarks.md)
- **Prometheus export** (`GET /metrics`, a read-only scrape key from `tsm prometheus key`): the engine's
  metrics with alias, node, model and revision labels, node memory, PSI and GPU, gateway routes, switch
  state. Windowed TTFT/ITL percentiles from histogram deltas; 24 h of minute averages kept across restarts;
  a Prometheus + Grafana compose file and dashboard for the workstation. [docs/metrics.md](docs/metrics.md)
- **Split profiles, one half at a time.** A new revision touches only the half that changed; *Restart this
  half* (`tsm activate --half B`); the watchdog restarts only the half that failed; status shows each half.
- **Performance lab**: what each recipe measured on two Sparks, generated from the recipes.
  [docs/perf-lab.md](docs/perf-lab.md)

### The gateway

- **One key per app**, limited to aliases, expirable, revocable, shown once; only hashes are kept. 0.6's
  key becomes the key `default`, so configured apps keep working. Requests and tokens per day, key and
  alias for 90 days (*Keys & usage*, `tsm keys`, `tsm usage --csv`). Hermes gets a key of its own.
- **Routing:** session affinity (`session-id` header or `prompt_cache_key`), least requests in flight,
  per-node weights. A **fallback** per alias while it switches or is down: another alias or an external
  OpenAI-compatible endpoint with its own key. `tsm gateway set`, the dashboard's *Routing…*.
- **Its own process** (`twinspark-gateway.service`, new installs): fed by an atomically written state file,
  answering the controller on a Unix socket; a controller restart or update keeps running streams.
  [docs/gateway.md](docs/gateway.md)

### Engines

- **SGLang** next to vLLM: profiles name an engine apart from the image family; the same settings become
  SGLang flags (two-node launch, EP, speculation, the backend key). Mods and Ray are refused for SGLang.
- **Image check before a switch:** a fixed probe in the pinned image (no network, no GPU, memory-capped)
  confirms flags, parsers, speculative method and architecture before anything is drained
  (`runtime.image_check: enforce | warn | off`). [docs/engines.md](docs/engines.md)

### The Cookbook

- **Recipe modes:** a recipe can offer variants with their own numbers — the GLM-5.3-Flash recipes come as
  *latency* (the drafter, as published) and *throughput* (no drafter, more sequences). Import asks which;
  `tsm cookbook import … --mode`. Update checks compare with the same mode.
- **A recipe catalog in this repository** (`catalog/index.json`, generated and checked by CI): node A and
  the client fetch new and corrected recipes from `main` — https only, size limits, every file checked
  against its SHA-256, refreshed when older than a day or on `tsm cookbook refresh`. A recipe for a newer
  TwinSpark is listed, not offered. **Imported profiles are never changed**: a newer recipe is an update
  to review, which creates a separate profile. [catalog/README.md](catalog/README.md)
- **Community results**, opt-in and previewed (`tsm cookbook report`, *Share result…*): how a recipe went,
  versions, benchmark numbers and the tuning settings changed — built from an allowlist, without host
  names, addresses, paths, model names or keys. The Cookbook shows totals (success rate, median tok/s,
  settings changed most often). The server side is a small PHP + MySQL/MariaDB endpoint in
  [community/strato](community/strato/README.md); reports stay off until `cookbook.community_url` is set.
- **Playground:** chat with one alias or two side by side, streamed as an app sees it, with time to first
  token, decode tok/s and *Copy as curl*. It uses the management session, not an app key.
  [docs/cookbook.md](docs/cookbook.md)

### Documentation

- A documentation site from `docs/` (mkdocs-material, GitHub Pages).
- Guides for [Open WebUI, n8n, LiteLLM, Continue, Cherry Studio, Aider and OpenCode](docs/integrations/README.md).
- The README compares TwinSpark with general cluster managers.

### Upgrading from 0.6

- Update both nodes as before (`git pull && sudo ./install.sh --no-setup`, then restart the services); the
  installer keeps your `controller.yaml`. An installation set up with 0.6 keeps the gateway in-process. To
  run it as its own service: add `gateway_mode: standalone` and run `sudo tsm setup`
  ([docs/gateway.md](docs/gateway.md#its-own-process)).
- Your inference key keeps working as the key `default`; give each app its own and revoke `default` later.
- A KV size that 0.6 stored on a model spec shared by several profiles is ignored; the next real start of
  each profile measures its own. Profiles imported before 0.7 still use their recipe's KV size, with a note
  that the settings it was measured with are unknown; re-importing records them.
- The recipe catalog is on by default; `cookbook.catalog_url: ""` switches it off for an air-gapped setup.

## 0.6.0 — 2026-10-16 — a workstation client, lean services on the Sparks

The test of 0.5.0 on two DGX Sparks asked for three things: less TwinSpark on the Sparks, a connection
that does not depend on a hand-typed SSH tunnel, and memory planning that explains a failed start before
it happens. This release moves the work that does not need the Sparks to your own computer and keeps the
deployment where it is.

### The workstation client (`tsm client`)

- **Connect by address.** Install TwinSpark on your computer with pipx (macOS, Windows, Linux), run
  `tsm client`, add node A's address and SSH user. The client:
  - shows node A's SSH host key fingerprints and stores only the ones you confirm, in its own
    `known_hosts` (`StrictHostKeyChecking=yes`; never `no`, and `~/.ssh` is not touched);
  - logs in with your SSH agent or keys, or installs its own key once with your password (used once,
    never stored, only sent to the confirmed host);
  - reads node A's `controller.yaml` and the keys (`tsm init --show`, `sudo -n` only on request, or paste);
  - keeps `ssh -N -L` forwards for the management API and the gateway up, with keep-alives and a 1–30 s
    back-off, on stable local ports so apps configured once keep working.
  Tailscale addresses are recognised (and your tailnet peers listed); a deliberately exposed HTTPS
  listener can be used directly (plain http only for loopback). [docs/client.md](docs/client.md)
- **The whole GUI on `127.0.0.1`**, guarded like node A's own API: a per-launch session key handed over in
  the URL fragment, loopback `Host` only (DNS rebinding), `Origin` checks on state changes. Node A's pages
  are passed through with the stored management key; a dropped connection shows *Not connected to the
  Sparks* with a link to the Connection page instead of an error, and a rotated key says so.
- **Three states, kept apart:** client connected · agents compatible · model ready — in the sidebar and on
  the Connection page, each with its own fix. A version/capability handshake (`/api/v1/health` now reports
  `api: 2` and a capability list) tells you which side to update when the client and the Sparks differ.
- **Work moved off the Sparks:** the cookbook index, community recipe fetching and recipe → profile
  conversion run on the client; node A receives only the finished profile. Integrations gains **Test from
  this computer** (chat, streaming and a tool call through the address local apps use).
- **Hermes lifecycle** on your computer (default) or on node A: detect (including a broken launcher and
  the working one), five separate states, a previewed diff of `config.yaml` and `.env`, apply with a
  byte-exact backup and only if the files are unchanged, validate, start/stop/restart its gateway, the
  exact chat command, restore any backup, disconnect. The context length comes from the deployed model;
  below Hermes's 64k minimum it is refused. Only the inference key is ever written; the management key
  is refused. [docs/agent-integrations.md](docs/agent-integrations.md#hermes-agent-lifecycle)
- **CLI:** `tsm client add | list | remove | connect | env | ports`; `eval "$(tsm client env NAME)"` points
  the `tsm` CLI at the client's forward.

### Lean services on the Sparks

- Each service imports only what its role needs: the agent and the terminal are plain Starlette,
  `tsm serve` no longer loads the whole CLI, shared container definitions moved to `twinspark.containers`,
  and the cookbook and integrations routes load on first use.
- **The terminal is socket-activated** (`twinspark-terminal.socket`): it uses no memory until someone opens
  a shell and exits after 15 idle minutes. Status checks ask systemd about the socket instead of connecting
  to it (which would start the terminal). `sudo tsm remote enable terminal` moves an existing install to it
  (stopping the always-on 0.5 terminal first).
- Measured on an x86 dry-run sandbox (unique set size): node A ≈188 → ≈100 MiB, node B ≈134 → ≈49 MiB with
  the terminal switched on but idle. Provisional budgets: 128 MiB on A, 80 MiB on B.
  `scripts/measure_memory.py` measures a Spark without touching its services.
  [docs/memory-planning.md](docs/memory-planning.md#twinsparks-own-footprint)

### Memory planning per node

- Every node is planned on its own, bucket by bucket (OS + Docker, TwinSpark services, desktop, other
  processes, weights, drafter weights and scratch, first-prefill peak, CUDA graphs, NCCL buffers, loader
  transient, KV), each with a source — measured,
  calibrated, estimated or unknown (never counted as 0). Three verdicts per node: **startup peak**, **one
  max-length request**, **requested concurrency**. A one-rank drafter (`draft_tensor_parallel_size: 1`) is
  counted on both nodes. `/fit` keeps its 0.5 keys, now derived from the worst node.
- A start whose estimated startup margin on a node is critical gets a warning in the validating stage.
- **Startup timeline:** each node samples MemAvailable, MemFree, page cache, swap, PSI and the cgroups of
  TwinSpark and the deployment once a second from just before the old model stops; every stage is marked.
  A successful start calibrates the next plan of the same settings; a failed one keeps the timeline in its
  evidence, drawn on the job page.
- **Failure classification** of a failed start: container limit, host OOM, driver allocation, RDMA/NCCL,
  timeout, application exception or unknown, with the evidence for it.

### Activations that survive a lost client or a reboot

- **Idempotent requests:** an activation carries a `request_id`; a retried request (a dropped connection,
  a double click) returns the same job instead of starting a second switch.
- **Stale-view check:** the GUI sends the deployment it showed (`expected_active`); if another client changed
  it meanwhile, the switch is refused with 409 instead of overriding it.
- **Durable journal:** an accepted switch is recorded before it starts. If the controller restarts in the
  middle (crash, reboot, power cut), recovery is deterministic: before the old model was touched, nothing
  changed and it keeps serving; after that, the partial containers are stopped and — with `auto_rollback` —
  the previous deployment is started again.
- One writer: while a restarted controller recovers an interrupted switch, a new switch is refused (409)
  instead of being torn down by the recovery; cancel is refused while a failed start is still cleaning up.

### Compatibility

- Node A still serves the GUI, the API and the CLI as before; SSH tunnels and Tailscale Serve keep working.
  The client is an additional way in.
- New optional extra `client` (`keyring`) for keys in the system keychain.

### Needs on-site validation

GB10 memory numbers of the services; the full GLM-5.3 DFlash2 run that calibrates the per-node plan; Hermes
against a real install; the client on Windows and macOS (including the one-time password key install on
Windows); the socket-activated terminal on real systemd; the client over Tailscale.

## 0.5.1 — 2026-10-08 — fixes from the first hardware test

The first test of 0.5.0 on two DGX Sparks found these problems:

- a failed GLM DFlash2 start left no usable error;
- a link test that mixed a live node and a dry-run node;
- a recipe without an image that failed only after preparation had started;
- a GUI that flashed on every refresh.

This release fixes them.

### P1 — startup failures you can act on

- **Startup evidence is saved before cleanup.** When an activation fails, the controller first saves each
  rank's container log, then removes the containers. Each log is redacted and capped (the end is kept: the
  last 50,000 lines, at most 2,000,000 characters). The controller also records:
  - the exit code, OOM-killed state and finish time;
  - a memory reading;
  - the number of NVIDIA driver `NV_ERR_NO_MEMORY` messages in the kernel log since the container started
    ("at least" when the last 2,000 kernel lines were all read).

  Saving this is bounded (150 s) and can never keep the cleanup and the rollback from running. The job
  stays *running* until both are done, so `tsm activate` and the job page show the rollback line too
  (Cancel is not offered during that cleanup). A container whose start call timed out is included.

  `tsm job <id>` and the job page show the **first** real error of each rank (an engine or worker
  exception rather than the API server's final wrapper traceback), the last one, and a download of the
  full log (`tsm job <id> --log FILE`). When the driver reported out-of-memory errors, the job says that
  an exit code of 1 does not rule out a GPU allocation failure. The evidence of the newest 20 failed jobs
  is kept (files 0600).
- **GLM-5.3-Flash DFlash2 recipe** now follows its author's DFlash2 launcher:
  - `max-num-batched-tokens 8192` instead of 32768 (the first prefill sets the startup memory peak);
  - `kv-cache-dtype fp8_e4m3`;
  - `VLLM_ENGINE_READY_TIMEOUT_S=3600` for the ~15 minute boot;
  - the launcher's FlashInfer/NCCL environment (not its NIC names or addresses);
  - a required mod, `glm53-sm121-sparse-attn`, for the SM121 sparse-attention fix. The agent checks it
    before every activation. Make it with the new `tsm mods file-patch` (one file replaced in the image,
    with a check that the target file exists and that its checksum matches after the copy).

  Its Needs list now covers `vm.swappiness=0` and headless nodes. Pin compares the image tag with the
  digest the launcher was written for, and warns if the tag has moved.
  **An existing profile keeps its old settings:** re-import the recipe (or edit the draft) and pin a new
  revision.
- **Both startup clocks in the plan.** `tsm plan` and the plan page show vLLM's engine-ready limit
  (from the recipe, or the image's default of 600 s) next to TwinSpark's `runtime.health_timeout_s`,
  and which of the two ends a slow start first. A recipe that documents a long boot but sets no
  engine limit gets a warning.
- **Link tests:**
  - Both nodes must run in the same mode. A live node paired with a dry-run node is refused before
    anything starts, with the `sudo tsm go-live` the other node needs.
  - An all-dry-run test is labelled *simulated*.
  - Failed or unreadable RDMA runs show as **unavailable**, with an error for each device, never as
    0 Gb/s. A one-PCIe-half measurement is labelled as such.
  - Results show the duration, stream count and devices.
  - TCP responders close when the streams finish, and a responder is cancelled if the initiator
    fails to start.
- **Missing image asked for up front.** A recipe without an image (`image_hint: null`, such as the
  Qwen3.8 recipe) can still be imported as a draft. Import & prepare and Pin & prepare now ask for an
  image (one per node for split profiles; `tsm pin` has `--image-b` / `--local-image-b` /
  `--model-ref-b` for node B). The controller rejects a missing image before it contacts Hugging Face,
  and never picks an installed image by itself.
- **`headless-max` is guarded like `--now`.** It always stops the desktop, so the CLI asks first (`-y`
  skips this), the GUI asks, and the controller refuses it while a model is being activated.

### P2 — clarity

- **The GUI no longer flashes.** The dashboard, jobs and job pages patch the page in place instead of
  rebuilding it. Focus, the selected profile and buttons that are still pending survive a refresh.
  Polls run one at a time, and a slow answer can no longer overwrite a newer one. A real-Chromium
  test covers this.
- **Stop** is on the active profile's row and on its page, not only on the dashboard, and a failed stop
  says so.
- **Get started** separates four states: QSFP *configured*, link *measured* (or only simulated, or
  failed), and whether *both PCIe halves* are in use. Using one half is valid but caps bandwidth near
  100 Gb/s, and the page shows the reviewed `tsm qsfp plan` for the second half.
- **Preparation progress** lists each piece of work as its own item, with the exact `repo@revision`:
  - the image pull;
  - the main checkpoint;
  - the drafter;
  - the checksum check;
  - the QSFP copy to node B.

  Each item is marked running, done, reused, failed or skipped; a job that ends leaves no item running.
- **Cache wording:** complete weights that were downloaded outside TwinSpark show as "external cache —
  reused as is, no new download", which is not the same as "staged by TwinSpark" or "checksum
  verified" (`tsm models ls`: `ext`).
- **Remote access before going headless.** `tsm headless status` and the Headless tab show, per node:
  - SSH running and enabled at boot;
  - Tailscale up, its address, and `tailscaled` enabled at boot;
  - on node A, whether the manager port is forwarded;
  - the default boot target.

  The headless confirmation warns about a node with no way back in. New: `tsm remote tailscale-serve`
  shows, saves (`--apply`) or removes a persistent tailnet-only TCP forward of the management port,
  then checks that the health page answers through it (over https when the listener has TLS). It
  refuses a controller set to `management_auth: none`. If that is set later, `tsm doctor` fails while the
  forward exists, and warns when it cannot read the forward state.
- The privileged-helper error says when a shell only lacks the new `twinspark` group (log out and back
  in), instead of suggesting a restart.
- Redaction (support bundles and startup logs) no longer hides settings such as
  `max_num_batched_tokens=8192`, and now catches `--api-key VALUE` and `'api_key': ['…']`. Its patterns
  are bounded, so a hostile log line cannot stall the controller.
- Recipe links in the GUI are only made for `http(s)` URLs.
- The jobs list no longer carries each failed job's startup evidence; the job page loads it.

### Tests

- The suite no longer depends on the host. `efibootmgr`, `fwupd`, the terminal's process cleanup and the
  ownership of the fake QSFP backup file all behave the same as root and as an ordinary user, on
  machines with or without those tools.
- Closing a remote terminal now ends every process in its session, not only its process group.

### Still to validate on site

- One complete GLM DFlash2 boot with the corrected recipe: health, a real completion, and a controlled
  stop.
- The Tailscale forward, opened from another tailnet device.

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
