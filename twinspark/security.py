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
}


class SecretsVault:
    def __init__(self, secrets_dir: str | Path = "/etc/twinspark/secrets"):
        self.dir = Path(secrets_dir)
        self.key_file = self.dir / ".master.key"
        self._fernet: Optional[Fernet] = None

    def _f(self) -> Fernet:
        if self._fernet is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
            if self.key_file.exists():
                key = self.key_file.read_bytes()
            else:
                key = Fernet.generate_key()
                _write_private(self.key_file, key)
            self._fernet = Fernet(key)
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


def _write_private(path: Path, data: bytes) -> None:
    """Create with 0600 from the start (no window where the file is world-readable)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
