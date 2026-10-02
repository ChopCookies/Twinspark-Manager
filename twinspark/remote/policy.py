"""The root-owned switch board for remote-management features.

``/etc/twinspark/remote-policy.json`` decides what the controller may ask this node to do:

    {"terminal": true, "reboot": true, "poweroff": false, "boot_next": false, "wol": false}

* Missing file, unreadable file, wrong owner/permissions, bad JSON → every feature **off**
  (fail closed). The reason is kept in ``RemotePolicy.error`` so the GUI can say why.
* Only the JSON value ``true`` enables a feature — ``"yes"``, ``1`` and friends do not.
* The file must be owned by root and not writable by group/others, as must every directory above
  it, so neither the service user nor a compromised agent/controller can switch features on.

Read-only diagnostics (logs, support bundle, reachability) do not need a switch.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

POLICY_PATH = Path("/etc/twinspark/remote-policy.json")
FEATURES = ("terminal", "reboot", "poweroff", "boot_next", "wol")
FEATURE_HELP = {
    "terminal": "browser/CLI shell on this node (as the service user, recorded)",
    "reboot": "reboot this node from the GUI/CLI",
    "poweroff": "power this node off from the GUI/CLI",
    "boot_next": "boot once from the network / USB (UEFI BootNext)",
    "wol": "turn Wake-on-LAN on for a network port",
}
# limits the policy file may tune, with the range each is clamped to
_LIMITS = {"terminal_idle_s": (60, 86400, 900), "terminal_max_s": (300, 86400, 14400),
           "terminal_max_sessions": (1, 8, 2)}


@dataclass(frozen=True)
class RemotePolicy:
    terminal: bool = False
    reboot: bool = False
    poweroff: bool = False
    boot_next: bool = False
    wol: bool = False
    record_terminal: bool = True
    terminal_idle_s: int = 900
    terminal_max_s: int = 14400
    terminal_max_sessions: int = 2
    error: Optional[str] = None           # why the file was ignored (policy is all-off in that case)
    present: bool = False

    def allows(self, feature: str) -> bool:
        return feature in FEATURES and bool(getattr(self, feature))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def enabled(self) -> list[str]:
        return [f for f in FEATURES if getattr(self, f)]


def _unsafe(path: Path, require_root: bool) -> Optional[str]:
    """Why ``path`` may not be trusted as a root-controlled file, or None."""
    try:
        if path.is_symlink():
            return f"{path} is a symlink"
        info = path.stat()
    except OSError as exc:
        return f"{path} cannot be read ({exc.strerror or exc})"
    if not path.is_file():
        return f"{path} is not a regular file"
    if not require_root:
        return None
    if info.st_uid != 0 or info.st_mode & 0o022:
        return f"{path} must be owned by root and not writable by group/others"
    for parent in path.parents:
        try:
            pinfo = parent.stat()
        except OSError:
            return f"{parent} cannot be inspected"
        if parent.is_symlink() or pinfo.st_uid != 0 or pinfo.st_mode & 0o022:
            return f"directory {parent} must be controlled by root"
    return None


def _clamp(data: dict[str, Any], key: str) -> int:
    lo, hi, default = _LIMITS[key]
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(lo, min(hi, value))


def load_policy(path: str | Path = POLICY_PATH, require_root: bool = True) -> RemotePolicy:
    path = Path(path)
    if not os.path.lexists(path):
        return RemotePolicy()
    problem = _unsafe(path, require_root)
    if problem:
        return RemotePolicy(error=problem, present=True)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return RemotePolicy(error=f"{path} is not valid JSON ({exc})", present=True)
    if not isinstance(data, dict):
        return RemotePolicy(error=f"{path} must contain a JSON object", present=True)
    return RemotePolicy(
        **{f: data.get(f) is True for f in FEATURES},
        record_terminal=data.get("record_terminal") is not False,
        terminal_idle_s=_clamp(data, "terminal_idle_s"),
        terminal_max_s=_clamp(data, "terminal_max_s"),
        terminal_max_sessions=_clamp(data, "terminal_max_sessions"),
        present=True)


def write_policy(path: str | Path, changes: dict[str, bool], *, chown_root: bool = True) -> RemotePolicy:
    """Merge ``changes`` (feature -> bool) into the policy file atomically. Run as root.

    Keeps limits and unknown keys already in the file. The result is 0644; with ``chown_root`` the
    file is handed to root (a no-op for the superuser, skipped in sandbox installs).
    """
    path = Path(path)
    bad = sorted(set(changes) - set(FEATURES))
    if bad:
        raise ValueError(f"unknown remote feature(s): {', '.join(bad)} (known: {', '.join(FEATURES)})")
    current: dict[str, Any] = {}
    if path.is_file() and not path.is_symlink():
        try:
            loaded = json.loads(path.read_text())
            current = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            current = {}
    for f in FEATURES:
        current.setdefault(f, False)
    current.update({k: bool(v) for k, v in changes.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".remote-policy.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(current, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        if chown_root and os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return load_policy(path, require_root=False)


@dataclass
class PolicySource:
    """Where a running component looks for the policy (agent/termd/privd share this)."""

    path: Path = POLICY_PATH
    require_root: bool = True
    _last: RemotePolicy = field(default_factory=RemotePolicy, repr=False)

    def current(self) -> RemotePolicy:
        """Always re-read: toggling a feature must take effect without a restart."""
        self._last = load_policy(self.path, self.require_root)
        return self._last

    def with_path(self, path: str | Path, require_root: Optional[bool] = None) -> "PolicySource":
        return replace(self, path=Path(path), require_root=self.require_root if require_root is None else require_root)


# The privileged helper configures this once at start-up (``tsm serve privd``).
PRIVD_POLICY = PolicySource()
