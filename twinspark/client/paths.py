"""Where the workstation client keeps its files, and how it writes them.

Everything lives in one per-user directory (``client_home()``): ``connections.json``, the client's own
``known_hosts``, ``credentials.json`` (only when no OS keyring is usable) and ``ssh/`` with the dedicated
client key. Nothing here is ever written outside that directory.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional

log = logging.getLogger("twinspark.client")

_POSIX = os.name != "nt"


def client_home(*, create: bool = True, platform: Optional[str] = None,
                environ: Optional[Mapping[str, str]] = None, user_home: Optional[Path] = None) -> Path:
    """The client's configuration directory.

    ``TSM_CLIENT_HOME`` overrides it (tests, portable installs). Otherwise: Linux and other Unix
    ``$XDG_CONFIG_HOME/twinspark`` or ``~/.config/twinspark``; macOS ``~/Library/Application Support/TwinSpark``;
    Windows ``%APPDATA%\\TwinSpark``. Created with mode 0700 where the OS has modes, because it holds
    the client's private key and, without a keyring, its API keys.

    ``platform``, ``environ`` and ``user_home`` exist so the per-OS rules can be tested anywhere.
    """
    env = os.environ if environ is None else environ
    plat = platform or sys.platform
    home = Path(user_home) if user_home is not None else Path.home()
    override = env.get("TSM_CLIENT_HOME")
    if override:
        path = Path(override).expanduser()
    elif plat.startswith("win"):
        appdata = env.get("APPDATA")
        path = (Path(appdata) if appdata else home / "AppData" / "Roaming") / "TwinSpark"
    elif plat == "darwin":
        path = home / "Library" / "Application Support" / "TwinSpark"
    else:
        xdg = env.get("XDG_CONFIG_HOME")
        # the XDG spec says a relative value is invalid and must be ignored
        path = (Path(xdg) if xdg and os.path.isabs(xdg) else home / ".config") / "twinspark"
    if create:
        ensure_private_dir(path)
    return path


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and make it 0700 on systems with POSIX modes."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _POSIX:
        try:
            if path.stat().st_mode & 0o077:
                os.chmod(path, 0o700)
        except OSError as exc:            # not ours (e.g. a shared override): keep going, but say so
            log.warning("could not make %s private: %s", path, exc)
    return path


_write_lock = threading.Lock()


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Replace ``path`` with ``data`` so that readers only ever see the old or the new file.

    The temporary sibling is created by ``mkstemp`` (0600 from the first byte, unique name, so two
    writers in one process or two client processes cannot clobber each other's temp file), fsynced,
    then renamed over the target. On Windows a virus scanner can briefly hold the target open, so the
    rename is retried a few times.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if _POSIX:
            os.chmod(tmp, mode)
        with _write_lock:
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if _POSIX or attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    if _POSIX:
        os.chmod(path, mode)


def read_json(path: Path, default: Any) -> Any:
    """Load a JSON file the client wrote; a missing file gives ``default``.

    A file that does not parse is moved aside (``<name>.bad-<time>``) rather than silently replaced,
    so a hand edit gone wrong never costs the user their saved connections.
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        aside = Path(path).with_name(f"{Path(path).name}.bad-{int(time.time())}")
        try:
            os.replace(path, aside)
            log.warning("%s was not valid JSON; moved it to %s and started empty", path, aside)
        except OSError as exc:
            log.warning("%s is not valid JSON and could not be moved aside: %s", path, exc)
        return default


def write_json(path: Path, doc: Any, *, mode: int = 0o600) -> None:
    atomic_write(Path(path), (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"), mode=mode)
