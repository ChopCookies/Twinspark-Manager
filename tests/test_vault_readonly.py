"""A service that only reads secrets never writes into the vault.

The standalone gateway runs with ``ProtectSystem=strict`` and no writable path for ``/etc/twinspark/secrets``.
Before 0.8.1 every read went through a ``mkdir`` + ``chmod`` of the vault directory, and the gateway failed at
start with ``OSError: [Errno 30] Read-only file system`` (found on two real Sparks).
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from twinspark import provision
from twinspark.security import SecretsVault

ROOT = Path(__file__).resolve().parents[1]


def filled(tmp_path: Path) -> Path:
    d = tmp_path / "secrets"
    v = SecretsVault(d)
    v.set("inference_api_key", "inf-key")
    v.set("backend_api_key", "be-key")
    return d


def make_read_only(monkeypatch) -> None:
    """From now on, every way the vault code could write fails like a read-only mount."""
    def ro(*a, **k):
        raise OSError(errno.EROFS, "Read-only file system", str(a[0]) if a else "")

    real_open = os.open

    def guarded_open(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            ro(path)
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "mkdir", ro)
    monkeypatch.setattr(os, "chmod", ro)
    monkeypatch.setattr(os, "open", guarded_open)


def test_reading_an_existing_vault_writes_nothing(tmp_path, monkeypatch):
    d = filled(tmp_path)
    make_read_only(monkeypatch)
    v = SecretsVault(d)
    assert v.get("inference_api_key") == "inf-key"
    assert v.get("backend_api_key") == "be-key"
    assert v.get("fallback_api_key") == ""                # absent: empty, without creating anything
    assert v.redact("x inf-key y") == "x [REDACTED] y"


def test_the_gateway_starts_from_a_read_only_vault(tmp_path, monkeypatch):
    from twinspark.gateway import standalone
    d = filled(tmp_path)
    cfg = tmp_path / "controller.yaml"
    cfg.write_text(f"secrets_dir: {d}\ngateway_state_path: {tmp_path / 'state.json'}\n"
                   f"gateway_listener: {{bind: 127.0.0.1, port: 18999}}\n", encoding="utf-8")

    make_read_only(monkeypatch)
    _cfg, gw, _follower = standalone.build(str(cfg))
    assert gw.requires_key() and gw.backend_api_key == "be-key"


def test_a_vault_without_its_master_key_says_so_instead_of_making_a_new_one(tmp_path):
    d = filled(tmp_path)
    (d / ".master.key").unlink()
    with pytest.raises(RuntimeError, match="no master key"):
        SecretsVault(d).get("inference_api_key")
    assert not (d / ".master.key").exists()             # a new key would make every secret unreadable


def test_writing_still_creates_the_vault_private(tmp_path):
    d = tmp_path / "new" / "secrets"
    v = SecretsVault(d)
    assert v.get("hf_token") == "" and not d.exists()    # a read creates nothing
    value, created = v.ensure("agent_token")
    assert created and value and (d / ".master.key").is_file()
    if os.name != "nt":
        assert (d.stat().st_mode & 0o777) == 0o700
    assert SecretsVault(d).get("agent_token") == value


def test_the_gateway_unit_keeps_the_vault_read_only(tmp_path):
    """The fix is in the code, not in a writable path: the gateway's unit stays as strict as it was."""
    a = provision.Answers(role="controller", node_id="A", hostname="spark-a", qsfp_iface="enp1s0f1np1",
                          qsfp_ip="192.168.100.1", peer_ip="192.168.100.2", service_user="spark")
    unit = provision.render_gateway_unit(a, provision.Layout(tmp_path))
    assert "ProtectSystem=strict" in unit and "ReadWritePaths" not in unit


def _userns_ro_mount_works(tmp: Path) -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return False
    probe = tmp / "probe"
    probe.mkdir()
    p = subprocess.run(["unshare", "-rm", "sh", "-c",
                        f"mount --bind {probe} {probe} && mount -o remount,bind,ro {probe} && ! touch {probe}/x"],
                       capture_output=True, text=True, timeout=20)
    return p.returncode == 0


def test_a_real_read_only_mount_of_the_vault(tmp_path):
    """The same as the service sees it: the vault on a read-only bind mount (needs user namespaces)."""
    if not _userns_ro_mount_works(tmp_path):
        pytest.skip("unprivileged user namespaces with mount are not available here")
    d = filled(tmp_path)
    script = ("import sys; sys.path.insert(0, sys.argv[2]); from twinspark.security import SecretsVault; "
              "v = SecretsVault(sys.argv[1]); print(v.get('inference_api_key'), v.get('backend_api_key'))")
    p = subprocess.run(["unshare", "-rm", "sh", "-c",
                        'mount --bind "$1" "$1" && mount -o remount,bind,ro "$1" && exec "$2" -c "$3" "$1" "$4"',
                        "sh", str(d), sys.executable, script, str(ROOT)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.split() == ["inf-key", "be-key"]
