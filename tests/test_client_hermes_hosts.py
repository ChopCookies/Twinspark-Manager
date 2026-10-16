"""SSHHost over a fake runner, and HermesAdapter driving a "remote" Hermes through it.

The fake runner does what ``ssh host -- <shlex.join(argv)>`` does on the far side: a POSIX shell parses the
joined command line. It runs locally with HOME pointing at a tmp dir, so the real shell scripts are exercised
without a network or a real remote machine.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import stat
import subprocess
import sys
import time

import pytest

from tests.test_client_hermes import DEEPSEEK_CONFIG, DEEPSEEK_ENV, everything_text, kill_fake_gateways, make_install
from tests.test_client_validate import KEY, FakeGateway
from twinspark.client.adapters import HermesAdapter, LocalHost, SSHHost


def fake_ssh(home, calls=None):
    async def run(argv, timeout, input):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        if calls is not None:
            calls.append(list(argv))
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", shlex.join(argv), stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(home),
            env={"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"})
        out, err = await asyncio.wait_for(proc.communicate(input), timeout)
        return proc.returncode, out, err
    return run


@pytest.fixture
def remote(tmp_path):
    home = tmp_path / "remote-home"
    home.mkdir()
    return SSHHost(fake_ssh(home), label="ws-2"), home


async def test_identity_env_and_platform(remote):
    host, home = remote
    assert await host.home() == str(home)
    assert await host.env("HOME") == str(home) and await host.env("NOT_SET_ANYWHERE") is None
    assert await host.platform() == "linux"
    assert host.is_local is False and host.label == "ws-2"


async def test_files_round_trip_with_hostile_paths(remote, tmp_path):
    host, home = remote
    tricky = home / "a b'c $(touch PWNED)" / ";rm -rf x" / ".env"
    data = "KEY=x\r\nunicode=äöü ✓\n".encode() + bytes(range(256))
    await host.write_bytes_atomic(str(tricky), data)
    assert tricky.read_bytes() == data and await host.read_bytes(str(tricky)) == data
    assert stat.S_IMODE(tricky.stat().st_mode) == 0o600              # new files are private by default
    assert not list(tmp_path.rglob("PWNED")) and not (home / "x").exists()
    assert not [p for p in tricky.parent.iterdir() if p.name.startswith(".tsm-")]   # no temp file left
    text = "model:\r\n  default: glm\r\n"
    await host.write_text_atomic(str(tricky), text)
    assert await host.read_text(str(tricky)) == text
    assert await host.list_dir(str(tricky.parent)) == [".env"]
    assert await host.list_dir(str(home / "missing")) == []


async def test_write_keeps_mode_and_symlinks(remote):
    host, home = remote
    target = home / "dotfiles" / "config.yaml"
    target.parent.mkdir()
    target.write_text("old\n")
    target.chmod(0o640)
    link = home / "config.yaml"
    link.symlink_to(target)
    await host.write_text_atomic(str(link), "new\n")
    assert link.is_symlink() and target.read_text() == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    await host.write_text_atomic(str(target), "explicit\n", 0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


async def test_stat_kinds(remote):
    host, home = remote
    f = home / "tool"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    (home / "dir").mkdir()
    (home / "good-link").symlink_to(f)
    (home / "bad-link").symlink_to(home / "gone")
    info = await host.stat(str(f))
    assert (info.kind, info.executable, info.mode, info.is_symlink) == ("file", True, 0o755, False)
    assert info.mtime and abs(info.mtime - f.stat().st_mtime) < 2
    assert (await host.stat(str(home / "dir"))).kind == "dir"
    good = await host.stat(str(home / "good-link"))
    assert good.kind == "file" and good.is_symlink and good.target == str(f)
    bad = await host.stat(str(home / "bad-link"))
    assert bad.kind == "broken-link" and bad.target == str(home / "gone") and not bad.exists
    assert (await host.stat(str(home / "nothing"))).kind == "missing"
    assert await host.exists(str(f)) and not await host.exists(str(home / "bad-link"))


async def test_reads_are_bounded(remote):
    host, home = remote
    big = home / "big.log"
    big.write_bytes(b"0123456789" * 100)
    with pytest.raises(ValueError):
        await host.read_bytes(str(big), 100)
    assert await host.read_bytes(str(big), 10, head=True) == b"0123456789"
    assert await host.read_tail(str(big), 15) == b"567890123456789"
    assert await host.read_tail(str(home / "none.log"), 15) == b""
    with pytest.raises(FileNotFoundError):
        await host.read_bytes(str(home / "none"))


async def test_backup_remove_and_dirs(remote):
    host, home = remote
    src = home / ".env"
    src.write_bytes(b"A=1\r\n")
    src.chmod(0o644)
    dest = home / "backups" / "b1" / ".env"
    assert await host.backup(str(src), str(dest)) == str(dest)
    assert dest.read_bytes() == b"A=1\r\n" and stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert await host.backup(str(home / "missing"), str(home / "b2")) is None
    assert await host.remove(str(dest)) is True and await host.remove(str(dest)) is False
    await host.make_dirs(str(home / "x" / "y"), 0o700)
    assert stat.S_IMODE((home / "x" / "y").stat().st_mode) == 0o700


async def test_which_run_and_processes(remote, tmp_path):
    host, home = remote
    assert (await host.which("sh")).endswith("/sh")
    assert await host.which("definitely-not-a-command-tsm") is None
    result = await host.run(["sh", "-c", 'printf %s "$FOO"'], 10, env={"FOO": "bar baz"})
    assert result == (0, "bar baz", "")
    assert await host.pid_alive(os.getpid())
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert not await host.pid_alive(done.pid)
    log = home / "logs" / "out.log"
    pid = await host.start_detached(["sh", "-c", "echo started; exec sleep 30"], {"X": "1"}, str(log),
                                    str(home / "logs" / "err.log"))
    try:
        assert await host.pid_alive(pid)
        assert "sleep 30" in (await host.process_args(pid) or "")
        for _ in range(100):
            if log.exists() and b"started" in log.read_bytes():
                break
            await asyncio.sleep(0.05)
        assert b"started" in log.read_bytes()
        assert await host.terminate(pid, 1.0)
        assert not await host.pid_alive(pid)
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


async def test_local_host_basics(tmp_path):
    host = LocalHost(environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, home=str(tmp_path))
    path = tmp_path / "sub" / "f.txt"
    await host.write_text_atomic(str(path), "x\r\n")
    assert path.read_bytes() == b"x\r\n" and stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    await host.write_text_atomic(str(path), "y\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert (await host.run(["definitely-not-a-command-tsm"], 5)).rc == 127
    slow = await host.run(["sleep", "5"], 0.2)
    assert slow.rc == 124 and "did not finish" in slow.stderr
    started = time.monotonic()
    pid = await host.start_detached(["sleep", "30"], None, str(tmp_path / "l.log"))
    assert await host.pid_alive(pid) and await host.terminate(pid, 2.0) and not await host.pid_alive(pid)
    assert time.monotonic() - started < 5


# ---- the adapter on a remote host -----------------------------------------------------------------------------
async def test_hermes_adapter_over_ssh(tmp_path):
    inst = make_install(tmp_path)
    calls: list[list[str]] = []
    host = SSHHost(fake_ssh(inst.home, calls), label="ws-2")
    gw = FakeGateway()
    adapter = HermesAdapter(host, http=gw.client(), start_wait=5.0, stop_grace=1.0)
    try:
        det = await adapter.detect()
        assert det.host == "ws-2" and det.state == "launcher-broken"
        assert det.broken_launchers[0]["path"] == str(inst.wrapper) and det.launcher == str(inst.launcher)
        preview = await adapter.preview(gw.endpoint())
        result = await adapter.apply(gw.endpoint(), preview.token)
        assert (inst.hermes / ".env").read_text() == DEEPSEEK_ENV + f"TWINSPARK_API_KEY={KEY}\n"
        assert stat.S_IMODE((inst.hermes / ".env").stat().st_mode) == 0o600
        checks = await adapter.validate(gw.endpoint())
        assert checks[0].id == "vantage" and checks[0].status == "warn" and "ws-2" in checks[0].detail
        assert "curl" not in checks[0].detail
        assert {c.id: c.status for c in checks}["tools"] == "pass"
        launched = await adapter.launch("gateway")
        assert launched.action == "started" and launched.host == "ws-2"
        assert (await adapter.status(gw.endpoint())).state.running
        assert (await adapter.stop()).stopped
        await adapter.restore(result.backup.id)
        assert (inst.hermes / "config.yaml").read_text() == DEEPSEEK_CONFIG
        assert (inst.hermes / ".env").read_text() == DEEPSEEK_ENV
        # The key travelled only as base64 on stdin of the write script, never on a command line.
        assert all(KEY not in " ".join(argv) for argv in calls)
        assert KEY not in everything_text(det, preview, result, checks, launched)
    finally:
        kill_fake_gateways(tmp_path)
