"""Where the client keeps the API keys it fetched from node A.

The OS keyring (macOS Keychain, Windows Credential Manager, Secret Service on Linux) is used when the
optional ``keyring`` package is installed and has a working backend. Otherwise the keys go to
``credentials.json`` in the client home, written atomically with mode 0600.

Only API keys are stored, never an SSH password: :data:`CREDENTIAL_NAMES` is the complete list and
anything else is refused.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

from .paths import ensure_private_dir, read_json, write_json

log = logging.getLogger("twinspark.client")

SERVICE = "TwinSpark Manager"
# The complete list of what may be stored. A keyring cannot be enumerated, so delete_all() relies on it.
CREDENTIAL_NAMES: tuple[str, ...] = ("management_key", "inference_key")
_CONN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
_OFF = {"0", "off", "no", "false", "file"}


def _usable_keyring() -> Any:
    """The ``keyring`` module when it is installed and its backend can actually store secrets, else None.

    ``keyring`` falls back to its ``fail`` backend (raises on every call) or the ``null`` backend
    (silently stores nothing) when no real keyring exists, e.g. on a headless Linux box. Both count as
    "not usable", so the file store is chosen up front instead of failing at the first save.
    """
    if os.environ.get("TSM_CLIENT_KEYRING", "").strip().lower() in _OFF:
        return None
    try:
        import keyring
    except Exception:  # noqa: BLE001 - ImportError, or a broken install raising anything at import
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001
        return None
    cls = type(backend)
    where = f"{cls.__module__}.{cls.__name__}".lower()
    if ".fail" in where or ".null" in where or "failkeyring" in where:
        return None
    try:
        priority = float(getattr(backend, "priority", 1))
    except Exception:  # noqa: BLE001 - priority is a class property some backends compute (and can raise)
        return None
    if priority <= 0:
        return None
    return keyring


class CredentialStore:
    """Per-connection secrets, by ``(conn_id, name)``.

    ``backend`` is ``"keyring"`` or ``"file"``. If a keyring call fails at runtime (a locked or vanished
    keyring) the store switches to the file for the rest of the session and logs why — without the value.
    ``use_keyring=False`` forces the file (tests, or ``TSM_CLIENT_KEYRING=off`` in the environment).
    """

    def __init__(self, home: Path, *, use_keyring: Optional[bool] = None):
        self.home = Path(home)
        self.path = self.home / "credentials.json"
        self._lock = threading.Lock()
        self._keyring = _usable_keyring() if use_keyring in (None, True) else None

    @property
    def backend(self) -> str:
        return "keyring" if self._keyring is not None else "file"

    # ---- public API ---------------------------------------------------------------------------
    def get(self, conn_id: str, name: str) -> Optional[str]:
        self._check(conn_id, name)
        if self._keyring is not None:
            try:
                val = self._keyring.get_password(SERVICE, _user(conn_id, name))
                if val:
                    return val
            except Exception as exc:  # noqa: BLE001
                self._keyring_failed("read", exc)
        # also consulted with a keyring: a value may have landed here after a keyring failure
        return self._file_get(conn_id, name)

    def set(self, conn_id: str, name: str, value: str) -> None:
        self._check(conn_id, name)
        if not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in "\r\n\0"):
            raise ValueError(f"{name} must be one non-empty line of at most 4096 characters")
        if self._keyring is not None:
            try:
                self._keyring.set_password(SERVICE, _user(conn_id, name), value)
                self._file_delete(conn_id, name)          # never leave an older copy in the file
                return
            except Exception as exc:  # noqa: BLE001
                self._keyring_failed("write", exc)
        self._file_set(conn_id, name, value)

    def delete(self, conn_id: str, name: str) -> None:
        self._check(conn_id, name)
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE, _user(conn_id, name))
            except Exception:  # noqa: BLE001 - PasswordDeleteError when absent; nothing to delete is fine
                pass
        self._file_delete(conn_id, name)

    def delete_all(self, conn_id: str) -> None:
        for name in CREDENTIAL_NAMES:
            self.delete(conn_id, name)
        with self._lock:
            doc = self._load()
            if doc["connections"].pop(conn_id, None) is not None:
                self._save(doc)

    # ---- internals ----------------------------------------------------------------------------
    @staticmethod
    def _check(conn_id: str, name: str) -> None:
        if not isinstance(conn_id, str) or not _CONN_ID_RE.match(conn_id):
            raise ValueError(f"{conn_id!r} is not a connection id")
        if name not in CREDENTIAL_NAMES:
            # also what keeps passwords out: there is no slot for one
            raise ValueError(f"{name!r} is not a credential the client stores (known: {', '.join(CREDENTIAL_NAMES)})")

    def _keyring_failed(self, what: str, exc: Exception) -> None:
        log.warning("the OS keyring failed to %s a credential (%s); using %s from now on",
                    what, type(exc).__name__, self.path)
        self._keyring = None

    def _load(self) -> dict[str, Any]:
        doc = read_json(self.path, {})
        if not isinstance(doc, dict) or not isinstance(doc.get("connections"), dict):
            doc = {"version": 1, "connections": {}}
        return doc

    def _save(self, doc: dict[str, Any]) -> None:
        ensure_private_dir(self.home)
        write_json(self.path, doc, mode=0o600)

    def _file_get(self, conn_id: str, name: str) -> Optional[str]:
        with self._lock:
            entry = self._load()["connections"].get(conn_id)
        val = entry.get(name) if isinstance(entry, dict) else None
        return val if isinstance(val, str) and val else None

    def _file_set(self, conn_id: str, name: str, value: str) -> None:
        with self._lock:
            doc = self._load()
            entry = doc["connections"].get(conn_id)
            if not isinstance(entry, dict):
                entry = doc["connections"][conn_id] = {}
            entry[name] = value
            self._save(doc)

    def _file_delete(self, conn_id: str, name: str) -> None:
        with self._lock:
            if not self.path.exists():
                return
            doc = self._load()
            entry = doc["connections"].get(conn_id)
            if isinstance(entry, dict) and name in entry:
                del entry[name]
                if not entry:
                    del doc["connections"][conn_id]
                self._save(doc)


def _user(conn_id: str, name: str) -> str:
    return f"{conn_id}:{name}"
