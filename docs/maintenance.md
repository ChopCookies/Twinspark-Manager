# System monitoring and coordinated maintenance

The **System status** page compares CPU load, GPU use/temperature/sensor power,
shared memory, swap, and per-interface network rates on nodes A and B. Samples
arrive every `metrics_interval_s` (10 seconds by default), with 120 samples kept
in controller memory. Disconnects retain the last sample with a stale warning.
History resets when the controller restarts. A dash means unavailable; it is
never an invented zero. CPU/network rates require two samples.

Spark uses shared CPU/GPU memory. An unsupported GPU memory sensor is expected
on some drivers; adding RAM and VRAM would double-count memory. GPU sensor watts
are not wall electricity consumption. Total consumption needs an external meter;
this version does not connect to meters or estimate electricity cost. Linux
network-interface counters may omit RDMA traffic that bypasses the kernel.
See [NVIDIA's Spark known issues](https://docs.nvidia.com/dgx/dgx-spark/known-issues.html).

## What automatic maintenance does

**Updates → Check nodes → Review automatic update** starts one explicitly
authorized run. There is no unattended recurring schedule. The workflow:

1. Saves the active profile and exact immutable revision in SQLite.
2. Drains requests and stops the TwinSpark deployment. If requests do not finish
   within `runtime.drain_timeout_s`, the run holds without stopping the model.
3. Verifies both nodes have no remaining managed/unmanaged inference, running
   Docker containers, GPU compute processes, transfers, or link-test tasks.
4. Installs OS/driver packages on **B first, then A**, through the configured APT
   repositories. APT refresh errors and package removals stop the run. Existing
   configuration files are kept; unauthenticated packages are not allowed.
5. Optionally updates firmware through configured fwupd remotes. Firmware is a
   separate opt-in in both the root policy and the review dialog. No custom
   firmware downloads, force flags, downgrades, or vendor cross-flashes are used.
6. Reboots each node only after its worker reports successful installation.
   Checks a changed boot ID, clean `dpkg --audit`, firmware target versions,
   Docker/GPU availability, and the configured active RDMA HCAs before advancing.
7. Restores the saved model revision after all configured nodes pass checks.

The cluster has downtime for the entire run. TwinSpark reserves model switches,
downloads, and other management writes during maintenance. The UI remains
available except while node A (the controller) reboots. The browser reconnects;
the controller resumes its saved checkpoint before normal model autostart.

This updates packages supplied by the machine's repositories, including drivers
when packaged there. It does **not** self-upgrade TwinSpark's Python environment,
replace pinned recipe images, update model weights, or provide OS/firmware rollback.

## Enable on real nodes

The feature defaults to **disabled**. Use Linux/systemd nodes with APT, NVIDIA
drivers, and (when needed) a recent fwupd supporting JSON output. Install the
updated TwinSpark agent and privileged helper on **both** nodes, and the
controller on A. Enable their supplied systemd units so they start after reboot.
The code and interpreter under `/opt/twinspark` must be root-controlled.

Copy `deploy/maintenance-policy.example.json` to
`/etc/twinspark/maintenance-policy.json` on each node, owned by root, mode 0644 or
0600. The containing `/etc/twinspark` directory and all its parents must also be
root-owned and not writable by group/others. Set `enabled` to `true` when ready.
Leave `allow_firmware` false unless you have verified the vendor's update path
for that exact hardware. The GUI cannot override this root-owned policy.

NVIDIA's [OS/component update instructions](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html)
apply to Founders Edition hardware. Partner systems such as ASUS GX10 may have
different firmware support. Use only remotes supported by that node's vendor.
Physical power-cycle or interactive firmware requirements leave the run held
for an operator; TwinSpark does not claim they succeeded.

Before first deployment, back up the controller database and node configuration,
ensure SSH/local-console recovery is available, and validate on a test pair in a
maintenance window. The implementation has been tested with isolated dry-run
agents and mocked updater commands on Windows; real APT, firmware, and reboots
have not been exercised on Spark hardware here.

## Failure and recovery

Worker state is root-owned under `/var/lib/twinspark-maintenance/`. Installation
runs in `twinspark-maintenance-<run-id>.service`, independent of the agent process.
The run ID makes dispatch/reboot requests idempotent. Controller state is in the
normal SQLite database; writes use full synchronization. Unexpected restarts
during installation fail the run instead of repeating installation blindly.

On failure, the cluster remains reserved. Read the error in **Updates**, inspect
the node and its systemd journal, and repair package/firmware issues locally.
**Recheck progress** polls the existing run, and can recover from a temporary
connection or post-boot health-check failure. It never repeats a failed worker.
**Release after repair** requires stopped workers and healthy node checks, then
leaves model activation to you. A new run needs a fresh explicit opt-in.

If a worker is awaiting reboot or rebooting, release is refused. If a machine
never returns, reconnect/repair it rather than clearing the controller state.
Do not delete state records or terminate dpkg while installation is running.
Worker time limits are two hours for APT and one hour for firmware; a timeout
requires operator review. A reboot may take up to 30 minutes before the
controller holds for review.

## Safe local preview

```
.venv/bin/python scripts/build_web.py
.venv/bin/python scripts/preview_web.py --demo-nodes
```

Open `http://127.0.0.1:18744/#/system`. Sample metrics are visibly labeled.
The Updates page runs a simulation against two in-process dry-run agents. It
never invokes Docker, the privilege helper, OS updates, firmware, or host reboots.
The temporary preview state disappears when the preview exits.
