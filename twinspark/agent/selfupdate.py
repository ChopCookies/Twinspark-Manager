"""Root side of `tsm update` on a Spark (tsm-privd operations ``update_probe/start/status``).

The controller decides *what* to install (a release from GitHub, with the hashes from its SHA256SUMS).
This module checks that request, fixes everything else and starts
:mod:`twinspark.agent.update_worker` in its own transient systemd unit. What it fixes:

* the URLs — built from the fixed repository, a validated tag and the exact asset names
* the environment — the one this privd runs from, and only one install.sh made
* the services to restart — those of the installed units that are running

The root-owned policy ``/etc/twinspark/update-policy.json`` (written by install.sh, ``{"enabled": true}``)
switches it on or off per node; set ``"enabled": false`` there to allow only manual updates.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..releases import CONSTRAINTS, SHA256_RE, TAG_RE, asset_url, is_prerelease, parse_version, wheel_name

POLICY = Path("/etc/twinspark/update-policy.json")
ROOT = Path("/var/lib/twinspark-update")
MARKER = ".twinspark-install"           # install.sh marks the program directory with it
LOCK = threading.Lock()
# restart order: the controller last (on node A it is the one that waits for this update)
UNITS = ("twinspark-privd.service", "twinspark-agent.service", "twinspark-gateway.service",
         "twinspark-controller.service")
ACTIVE = ("queued", "running")
KEEP_RUNS = 5
_RID = re.compile(r"^[a-f0-9]{32}\Z")


def _root_controlled(path: Path) -> None:
    for p in (path, *path.parents):
        info = p.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or p.is_symlink():
            raise RuntimeError(f"{p} must be owned by root and not writable by group/others")


def policy() -> dict[str, bool]:
    if not POLICY.exists():
        return {"enabled": False, "allow_prerelease": False, "missing": True}
    _root_controlled(POLICY)
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    return {"enabled": data.get("enabled") is True, "allow_prerelease": data.get("allow_prerelease") is True}


def install_venv() -> Path:
    """The environment this privd runs from, if install.sh made it (``<TSM_HOME>/venv`` next to the marker)."""
    venv = Path(sys.prefix)
    if venv.name != "venv" or not (venv.parent / MARKER).is_file():
        raise RuntimeError(f"{venv} was not installed by install.sh; update this node by hand "
                           f"(git pull && sudo ./install.sh --no-setup)")
    _root_controlled(venv)                  # bin/python itself is a symlink to the base interpreter
    return venv


def base_python() -> str:
    """The interpreter the venv was made from: the worker runs with it, outside the venv it replaces."""
    exe = os.path.realpath(getattr(sys, "_base_executable", "") or sys.executable)
    if not os.path.isfile(exe):
        raise RuntimeError(f"base interpreter {exe} not found")
    _root_controlled(Path(exe))
    return exe


def installed_version(venv: Path) -> str | None:
    for meta in sorted(venv.glob("lib/python3*/site-packages/twinspark_manager-*.dist-info/METADATA")):
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Version: "):
                return line[9:].strip()
            if not line.strip():
                break
    return None


def _systemctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["/usr/bin/systemctl", *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout)


def running_units() -> list[str]:
    return [u for u in UNITS if _systemctl("is-active", u).stdout.strip() == "active"]


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def run_id(params: dict) -> str:
    value = params.get("run_id", "")
    if not isinstance(value, str) or not _RID.match(value):
        raise ValueError("invalid update run ID")
    return value


def check_target(target: Any) -> dict:
    """The release the controller asks for — only its tag and two hashes come from the request."""
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    tag = target.get("tag")
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        raise ValueError("target.tag must be a release tag such as v0.8.0")
    version = tag[1:]
    out: dict[str, Any] = {"tag": tag, "version": version}
    for key, name in (("wheel", wheel_name(version)), ("constraints", CONSTRAINTS)):
        f = target.get(key)
        digest = f.get("sha256") if isinstance(f, dict) else None
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            raise ValueError(f"target.{key}.sha256 must be 64 lowercase hex characters")
        out[key] = {"name": name, "sha256": digest, "url": asset_url(tag, name)}
    return out


def _load(rid: str) -> dict | None:
    path = ROOT / rid / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _save(state: dict) -> None:
    d = ROOT / state["run_id"]
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    state["updated_at"] = time.time()
    tmp = d / "state.json.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(d / "state.json")


def _runs() -> list[dict]:
    out = []
    for path in ROOT.glob("*/state.json") if ROOT.exists() else []:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda s: s.get("started_at", 0))


def _prune() -> None:
    done = [s for s in _runs() if s.get("state") not in ACTIVE]
    for s in done[:-KEEP_RUNS]:
        shutil.rmtree(ROOT / s["run_id"], ignore_errors=True)


def probe(params: dict) -> dict:
    p = policy()
    out: dict[str, Any] = {**p, "dry_run": False}
    try:
        venv = install_venv()
        out.update(venv=str(venv), version=installed_version(venv), venv_bytes=_dir_bytes(venv),
                   free_bytes=shutil.disk_usage(venv.parent).free, units=running_units())
    except Exception as exc:  # noqa: BLE001 - reported to the controller, which refuses to start
        out["install_error"] = str(exc)
    active = [s["run_id"] for s in _runs() if s.get("state") in ACTIVE]
    out["active_run"] = active[0] if active else None
    return out


def start(params: dict) -> dict:
    rid = run_id(params)
    target = check_target(params.get("target"))
    with LOCK:
        old = _load(rid)
        if old:
            if old["target"]["tag"] != target["tag"] or old["target"]["wheel"]["sha256"] != target["wheel"]["sha256"]:
                raise ValueError("run ID already used for another release")
            return old
        p = policy()
        if not p["enabled"]:
            raise RuntimeError("TwinSpark updates are switched off on this node "
                               f"({POLICY}: \"enabled\": true switches them on)")
        if is_prerelease(target["version"]) and not p["allow_prerelease"]:
            raise RuntimeError(f"{target['tag']} is a pre-release; this node's {POLICY} does not allow those")
        if parse_version(target["version"]) is None:
            raise ValueError("unsupported version")
        busy = [s["run_id"] for s in _runs() if s.get("state") in ACTIVE]
        if busy:
            raise RuntimeError(f"another update ({busy[0][:12]}) is still running on this node")
        venv = install_venv()
        need = int(_dir_bytes(venv) * 1.3) + 400 * 1024 * 1024
        free = shutil.disk_usage(venv.parent).free
        if free < need:
            raise RuntimeError(f"{venv.parent} needs {need // 2**20} MiB free for the backup and the new "
                               f"version; {free // 2**20} MiB are")
        python = base_python()
        state = {"run_id": rid, "state": "queued", "phase": "queued", "target": target, "venv": str(venv),
                 "from_version": installed_version(venv), "units": running_units(), "started_at": time.time()}
        _save(state)
        worker = ROOT / rid / "worker.py"
        shutil.copyfile(Path(__file__).with_name("update_worker.py"), worker)
        os.chmod(worker, 0o600)
        p_run = subprocess.run(
            ["/usr/bin/systemd-run", f"--unit=twinspark-update-{rid}", "--collect",
             "--description=TwinSpark update", python, "-I", "-S", str(worker), str(ROOT / rid)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        if p_run.returncode != 0:
            state.update(state="failed", error=f"systemd-run failed: {(p_run.stderr or p_run.stdout)[-1000:]}")
            _save(state)
            raise RuntimeError(state["error"])
        _prune()
        return state


def status(params: dict) -> dict:
    rid = run_id(params)
    with LOCK:
        state = _load(rid)
        if not state:
            return {"run_id": rid, "state": "missing"}
        unit = f"twinspark-update-{rid}.service"
        active = _systemctl("is-active", unit).stdout.strip() in ("active", "activating", "deactivating")
        state = _load(rid) or state                      # re-read: it may have finished meanwhile
        if state["state"] in ACTIVE and not active and time.time() - state.get("updated_at", 0) > 60:
            state.update(state="failed", error="the update worker stopped without finishing; "
                                               f"see journalctl -u {unit} and {state['venv']}.rollback")
            _save(state)
        state["worker_active"] = active
        return state
