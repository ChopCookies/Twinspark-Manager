"""Installed mods are usable by the container, and a partial install is reported as a failure.

Found on two real Sparks: the installed mod directory stayed 0700 (tempfile.mkdtemp), so a container running as
root with its capabilities dropped could not enter the read-only mount: ``bash ./run.sh: Permission denied``.
And `tsm mods install` exited 0 when one node had failed.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from twinspark import cli
from twinspark.agent import mods as modlib

POSIX = os.name == "posix"


def _zip(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return base64.b64encode(buf.getvalue()).decode()


@pytest.mark.skipif(not POSIX, reason="permission bits need a POSIX filesystem")
def test_the_mod_and_its_folders_can_be_entered_by_another_user(tmp_path, monkeypatch):
    old = os.umask(0o077)                  # an operator's restrictive umask must not leak into public code
    try:
        modlib.install_mod(tmp_path, "fix", _zip({"run.sh": "echo ok\n", "patch/deep/x.py": "print(1)\n"}))
    finally:
        os.umask(old)
    mod = tmp_path / "fix"
    assert (mod.stat().st_mode & 0o777) == 0o755
    assert (mod / "patch").stat().st_mode & 0o777 == 0o755
    assert (mod / "patch" / "deep").stat().st_mode & 0o777 == 0o755
    assert (mod / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (mod / "patch" / "deep" / "x.py").stat().st_mode & 0o777 == 0o644


@pytest.mark.skipif(not POSIX or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setpriv"),
                    reason="runs the mod as another UID: needs root and setpriv")
def test_a_different_uid_without_capabilities_runs_the_installed_mod():
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="tsm-modtest-", dir="/tmp"))   # pytest's tmp_path is 0700 for root
    try:
        os.chmod(base, 0o755)
        root = base / "mods"
        root.mkdir(mode=0o755)
        modlib.install_mod(root, "fix", _zip({"run.sh": "cat patch/x.py\n", "patch/x.py": "print(1)\n"}))
        # like the container: another user, no capabilities, reading the mod in place
        p = subprocess.run(["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--inh-caps=-all",
                            "--bounding-set=-all", "bash", "-c", f"cd {root / 'fix'} && bash ./run.sh"],
                           capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, p.stderr
        assert p.stdout.strip() == "print(1)"
    finally:
        shutil.rmtree(base, ignore_errors=True)


class FakeApi:
    as_json = False

    def __init__(self, answer, status=200):
        self.answer, self.status, self.calls = answer, status, []

    def __call__(self, method, path, ok=(), **kw):
        self.calls.append((method, path, kw.get("json")))
        if self.status >= 400 and self.status not in ok:
            raise SystemExit(f"error {self.status}")
        return self.answer


def test_a_node_that_failed_makes_the_install_fail(capsys):
    api = FakeApi({"name": "fix", "results": {"A": {"hash": "sha256:" + "a" * 64},
                                              "B": {"error": "mods dir not writable"}}, "consistent": False})
    assert cli._install_mod(api, "fix", "eA==", None) is False
    err = capsys.readouterr().err
    assert "NOT installed on node B" in err and "run the same command again" in err


def test_different_content_on_the_nodes_fails_too(capsys):
    api = FakeApi({"name": "fix", "results": {"A": {"hash": "sha256:" + "a" * 64},
                                              "B": {"hash": "sha256:" + "b" * 64}}, "consistent": False})
    assert cli._install_mod(api, "fix", "eA==", None) is False
    assert "different content" in capsys.readouterr().err


def test_every_node_failing_is_reported_not_a_crash(capsys):
    api = FakeApi({"detail": "failed on every node: no space left"}, status=422)
    assert cli._install_mod(api, "fix", "eA==", None) is False
    assert "no space left" in capsys.readouterr().err


def test_a_complete_install_succeeds(capsys):
    h = "sha256:" + "c" * 64
    api = FakeApi({"name": "fix", "results": {"A": {"hash": h}, "B": {"hash": h}}, "consistent": True})
    assert cli._install_mod(api, "fix", "eA==", None) is True
    assert "node B: ok" in capsys.readouterr().out


def test_tsm_mods_install_exits_nonzero_when_a_node_failed(tmp_path, monkeypatch):
    mod = tmp_path / "fix"
    mod.mkdir()
    (mod / "run.sh").write_text("echo ok\n")
    monkeypatch.setattr(cli, "_install_mod", lambda *a, **k: False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_mods(SimpleNamespace(action="install", path=str(mod), name=None, nodes=None), FakeApi({}))
    assert exc.value.code == 1


async def test_the_api_names_the_failed_nodes(cluster, monkeypatch):
    c = cluster.controller
    real = c.agents["B"].call

    async def broken(action, *a, **k):
        if action == "mods_install":
            from twinspark.controller.agent_client import AgentActionError
            raise AgentActionError(action, "B", "read-only mods dir")
        return await real(action, *a, **k)

    monkeypatch.setattr(c.agents["B"], "call", broken)
    res = await c.install_mod("fix", _zip({"run.sh": "echo ok\n"}))
    assert res["ok"] is False and res["failed"] == ["B"] and "hash" in res["results"]["A"]
    monkeypatch.setattr(c.agents["B"], "call", real)
    again = await c.install_mod("fix", _zip({"run.sh": "echo ok\n"}))          # the same install again
    assert again["ok"] and again["failed"] == [] and again["hash"] == res["results"]["A"]["hash"]
