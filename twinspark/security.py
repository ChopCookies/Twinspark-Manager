"""Encrypted secrets vault (spec §40).

Secrets (HF token, API keys, agent token) are stored Fernet-encrypted under
``secrets_dir`` and referenced by slot name only. They never appear in
profiles, plans, logs or support bundles.
"""

from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

SECRET_SLOTS = {
    "hf_token", "ngc_api_key",
    "inference_api_key",      # clients -> gateway
    "management_api_key",     # GUI/CLI -> controller
    "backend_api_key",        # gateway -> vLLM (never leaves the cluster)
    "agent_token",            # controller -> agents (same value on both nodes)
    "plug_token",             # optional: referenced as ${secret:plug_token} in smart-plug requests
}


def tokens_equal(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time comparison that never raises.

    ``secrets.compare_digest`` raises ``TypeError`` for non-ASCII ``str`` input, and a client
    controls that input through any header, so compare the encoded bytes instead. An empty or
    missing ``expected`` never matches.
    """
    if not expected:
        return False
    a = (provided or "").encode("utf-8", "surrogatepass")
    b = expected.encode("utf-8", "surrogatepass")
    return _secrets.compare_digest(a, b)


class SecretsVault:
    def __init__(self, secrets_dir: str | Path = "/etc/twinspark/secrets"):
        self.dir = Path(secrets_dir)
        self.key_file = self.dir / ".master.key"
        self._fernet: Optional[Fernet] = None

    def _load_or_create_key(self) -> bytes:
        for _ in range(3):
            try:
                key = self.key_file.read_bytes().strip()
            except FileNotFoundError:
                key = b""
            if key:
                return key
            new = Fernet.generate_key()
            try:                       # exclusive: two first starts cannot overwrite each other
                _write_private(self.key_file, new, exclusive=True)
                return new
            except FileExistsError:
                if self.key_file.exists() and self.key_file.stat().st_size == 0:
                    self.key_file.unlink(missing_ok=True)      # left empty by an earlier crash
        raise RuntimeError(f"cannot initialise the vault master key in {self.dir}")

    def _f(self) -> Fernet:
        if self._fernet is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
            self._fernet = Fernet(self._load_or_create_key())
        return self._fernet

    def set(self, name: str, value: str) -> None:
        if name not in SECRET_SLOTS:
            raise ValueError(f"'{name}' is not a known secret slot")
        _write_private(self.dir / f"{name}.enc", self._f().encrypt(value.encode()))

    def get(self, name: str) -> str:
        p = self.dir / f"{name}.enc"
        if not p.exists():
            return ""
        return self._f().decrypt(p.read_bytes()).decode()

    def ensure(self, name: str) -> tuple[str, bool]:
        """Return (value, created). Generates a random value if the slot is empty."""
        val = self.get(name)
        if val:
            return val, False
        return self.rotate(name), True

    def rotate(self, name: str) -> str:
        new = _secrets.token_urlsafe(32)
        self.set(name, new)
        return new

    def redact(self, text: str) -> str:
        for name in SECRET_SLOTS:
            val = self.get(name)
            if val:
                text = text.replace(val, "[REDACTED]")
        return text


def _write_private(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """Write ``data`` to ``path`` with mode 0600 from the first byte, atomically.

    A temporary sibling is written in full (short writes looped), fsynced, then renamed over
    the target, so a full disk or a crash can never leave a truncated secret behind.
    ``exclusive`` refuses to replace an existing file (used for the master key).
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        if exclusive:
            os.link(tmp, path)         # raises FileExistsError when someone else got there first
        else:
            os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    os.chmod(path, 0o600)
