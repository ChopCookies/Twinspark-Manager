"""Root-side, opt-in maintenance. Fixed commands; durable, idempotent run IDs.

The worker runs in a separate systemd unit so restarting the agent cannot kill
dpkg. Firmware comes exclusively from the machine's configured fwupd remotes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

POLICY = Path("/etc/twinspark/maintenance-policy.json")
ROOT = Path("/var/lib/twinspark-maintenance")
LOCK = threading.Lock()


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def run_id(params):
    value = params.get("run_id", "")
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("invalid maintenance run ID")
    return value


def policy():
    if not POLICY.exists():
        return {"enabled": False, "allow_firmware": False}
    info = POLICY.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or POLICY.is_symlink():
        raise RuntimeError("maintenance policy must be root-owned and not writable by group/others")
    for parent in POLICY.parents:
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022 or parent.is_symlink():
            raise RuntimeError("maintenance policy directories must be controlled by root")
    data = json.loads(POLICY.read_text())
    return {"enabled": data.get("enabled") is True, "allow_firmware": data.get("allow_firmware") is True}


def load(rid):
    path = ROOT / (rid + ".json")
    return json.loads(path.read_text()) if path.exists() else None


def save(state):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = ROOT / (state["run_id"] + ".json")
    tmp = path.with_suffix(".tmp")
    state["updated_at"] = time.time()
    with tmp.open("w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def command(argv, timeout=60, accepted=(0,)):
    p = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l", "LC_ALL": "C"},
    )
    if p.returncode not in accepted:
        raise RuntimeError(f"{argv[0]} failed ({p.returncode}): {(p.stderr or p.stdout)[-1500:]}")
    return p.stdout


def probe(params):
    p = policy()
    # Simulation does not refresh repositories, download, or install anything.
    preview = command(["/usr/bin/apt-get", "--simulate", "--no-remove", "dist-upgrade"])
    return {
        **p,
        "boot_id": boot_id(),
        "package_preview": preview[-10000:],
        "reboot_required": Path("/var/run/reboot-required").exists(),
    }


def start(params):
    rid = run_id(params)
    firmware = params.get("firmware", False)
    if not isinstance(firmware, bool):
        raise ValueError("firmware must be a boolean")
    with LOCK:
        old = load(rid)
        if old:
            if old["firmware"] != firmware:
                raise ValueError("run ID already used with different options")
            return old
        p = policy()
        if not p["enabled"] or (firmware and not p["allow_firmware"]):
            raise RuntimeError("automatic maintenance is not enabled by this node's root policy")
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in ROOT.glob("*.json"):
            old = json.loads(path.read_text())
            if old["state"] not in ("completed", "failed"):
                raise RuntimeError("another maintenance run needs completion or operator repair")
        state = {
            "run_id": rid,
            "state": "queued",
            "firmware": firmware,
            "boot_id": boot_id(),
            "started_at": time.time(),
            "phase": "queued",
        }
        save(state)
        try:
            command(
                [
                    "/usr/bin/systemd-run",
                    "--unit=twinspark-maintenance-" + rid,
                    "--collect",
                    sys.executable,
                    "-m",
                    "twinspark.agent.maintenance",
                    rid,
                ]
            )
        except Exception as exc:
            state.update(state="failed", error=str(exc))
            save(state)
            raise
        return state


def firmware_verified(targets, devices):
    versions = {d.get("DeviceId"): d.get("Version") for d in devices}
    return all(versions.get(k) == v for k, v in targets.items())


def verify(params):
    rid = run_id(params)
    active = command(
        ["/usr/bin/systemctl", "is-active", "twinspark-maintenance-" + rid + ".service"], accepted=(0, 3, 4)
    ).strip()
    if active in ("active", "activating", "deactivating"):
        raise RuntimeError("maintenance worker is still active")
    audit = command(["/usr/bin/dpkg", "--audit"])
    if audit.strip():
        raise RuntimeError("dpkg requires repair: " + audit[-1000:])
    state = load(rid)
    targets = (state or {}).get("firmware_targets", {})
    if targets:
        devices = json.loads(command(["/usr/bin/fwupdmgr", "get-devices", "--json"]))
        if not firmware_verified(targets, devices.get("Devices", [])):
            raise RuntimeError("firmware versions do not match the update targets; operator review required")
    return {"healthy": True}


def status(params):
    rid = run_id(params)
    with LOCK:
        state = load(rid)
        if not state:
            return {"state": "missing", "boot_id": boot_id()}
        current = boot_id()
        if current != state["boot_id"]:
            if state["state"] == "rebooting":
                try:
                    # A successful boot alone does not prove package/firmware health.
                    verify(params)
                    state.update(state="completed", phase="verified", new_boot_id=current)
                except Exception as exc:
                    state.update(state="failed", error=str(exc))
                save(state)
            elif state["state"] not in ("completed", "failed"):
                state.update(
                    state="failed", error="node restarted before the update completed; inspect package/firmware state"
                )
                save(state)
        elif state["state"] in ("queued", "installing", "failed"):
            unit = "twinspark-maintenance-" + rid + ".service"
            active = command(["/usr/bin/systemctl", "is-active", unit], accepted=(0, 3, 4)).strip()
            # Re-read after querying systemd: the worker may have finished meanwhile.
            state = load(rid)
            worker_active = active in ("active", "activating", "deactivating")
            if state["state"] in ("queued", "installing") and not worker_active:
                if time.time() - state["updated_at"] > 30:
                    state.update(
                        state="failed", error="maintenance worker stopped; inspect its journal before retrying"
                    )
                    save(state)
            state["worker_active"] = worker_active
        return state


def reboot(params):
    rid = run_id(params)
    with LOCK:
        state = load(rid)
        if not state:
            raise ValueError("unknown maintenance run")
        if state["state"] in ("rebooting", "completed"):
            return state  # A lost HTTP reply must never cause a second reboot.
        if state["state"] != "awaiting_reboot":
            raise RuntimeError("updates have not completed successfully")
        if not policy()["enabled"]:
            raise RuntimeError("maintenance policy has been disabled")
        state.update(state="rebooting", phase="rebooting")
        save(state)
        try:
            command(["/usr/bin/systemctl", "reboot", "--no-block"])
        except Exception as exc:
            state.update(state="failed", error=str(exc))
            save(state)
            raise
        return state


def worker(rid):
    run_id({"run_id": rid})
    state = load(rid)
    if not state or state["state"] != "queued":
        raise RuntimeError("worker requires a newly queued run")
    try:
        p = policy()
        if not p["enabled"] or (state["firmware"] and not p["allow_firmware"]):
            raise RuntimeError("maintenance policy disabled")
        state.update(state="installing", phase="repository metadata")
        save(state)
        command(["/usr/bin/apt-get", "-o", "APT::Update::Error-Mode=any", "update"], timeout=900)
        state["phase"] = "OS and driver packages"
        save(state)
        command(
            [
                "/usr/bin/apt-get",
                "--assume-yes",
                "--no-remove",
                "-o",
                "Dpkg::Options::=--force-confold",
                "dist-upgrade",
            ],
            timeout=7200,
        )
        if state["firmware"]:
            state["phase"] = "firmware"
            save(state)
            command(["/usr/bin/fwupdmgr", "refresh"], timeout=300, accepted=(0, 2))
            output = command(["/usr/bin/fwupdmgr", "get-updates", "--json"], accepted=(0, 2))
            data = json.loads(output or "{}")
            targets = {}
            for device in data.get("Devices", []):
                releases = device.get("Releases", [])
                if releases:
                    did, version = device.get("DeviceId"), releases[0].get("Version")
                    if not did or not version:
                        raise RuntimeError("firmware metadata lacks a verifiable device/version")
                    targets[did] = version
            state["firmware_targets"] = targets
            save(state)
            if targets:
                command(
                    [
                        "/usr/bin/fwupdmgr",
                        "update",
                        "--assume-yes",
                        "--no-reboot-check",
                        "--no-unreported-check",
                        "--no-remote-check",
                    ],
                    timeout=3600,
                )
        state.update(state="awaiting_reboot", phase="awaiting controller reboot")
    except Exception as exc:
        state.update(state="failed", error=str(exc)[-2000:])
    save(state)


if __name__ == "__main__":
    worker(sys.argv[1])
