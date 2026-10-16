"""Workstation client: the OpenSSH wrapper, host-key TOFU, the password key bootstrap and tunnels.

Every ssh / ssh-keyscan here is the fake from :mod:`tests.test_client_ssh_fake`; nothing reaches a host.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import socket
import stat
from pathlib import Path

import pytest

from tests.test_client_ssh_fake import HOSTKEY, HOSTKEY_FP, OTHER_HOSTKEY, POSIX, FakeSSH, Sandbox, blob_line
from twinspark.client import ssh as S
from twinspark.client.connections import Connection
from twinspark.client.ssh import HostKey, SSHError, Tunnel

pytestmark = pytest.mark.skipif(not POSIX, reason="the fake ssh is a #! script (POSIX only)")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    return FakeSSH(tmp_path, monkeypatch)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "client home"            # a space, like macOS "Application Support"
    h.mkdir()
    return h


def conn(**kw) -> Connection:
    base = dict(id="spark-a", name="Spark A", host="spark-a.local", ssh_user="spark")
    base.update(kw)
    return Connection(**base)


def trust(home: Path, host: str = "spark-a.local", port: int = 22, line: str = HOSTKEY) -> None:
    t, b = line.split()[:2]
    S.trust_host_keys(home, host, port, [HostKey.parse(t, b)])


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---- finding OpenSSH --------------------------------------------------------------------------------
def test_find_ssh_override_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TSM_SSH", str(tmp_path / "nope"))
    with pytest.raises(SSHError) as exc:
        S.find_ssh()
    assert exc.value.kind == "missing" and "TSM_SSH" in exc.value.message
    monkeypatch.delenv("TSM_SSH")
    monkeypatch.delenv("TSM_SSH_KEYSCAN", raising=False)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    for finder in (S.find_ssh, S.find_keyscan):
        with pytest.raises(SSHError) as exc:
            finder()
        assert exc.value.kind == "missing" and "OpenSSH" in exc.value.message


def test_install_hints_name_the_way_per_os():
    assert "OpenSSH Client" in S.install_hint("win32") and "Add-WindowsCapability" in S.install_hint("win32")
    assert "xcode-select" in S.install_hint("darwin")
    assert "apt install openssh-client" in S.install_hint("linux")


# ---- argv -----------------------------------------------------------------------------------------
def test_base_args(home):
    args = S.base_args(conn(ssh_port=2222), home)
    joined = " ".join(args)
    assert args[:4] == ["-p", "2222", "-l", "spark"]
    for opt in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ServerAliveInterval=15", "ServerAliveCountMax=3",
                "ConnectTimeout=10", "ExitOnForwardFailure=yes"):
        assert opt in args
    assert f'UserKnownHostsFile="{home / "known_hosts"}"' in args          # quoted: the path has a space
    assert "accept-new" not in joined and "StrictHostKeyChecking=no" not in joined
    assert "-i" not in args                                                # no key configured or created
    assert "BatchMode=yes" not in S.base_args(conn(), home, batch=False)
    with_key = S.base_args(conn(identity_file="/keys/id"), home)
    assert with_key[with_key.index("-i") + 1] == "/keys/id"


def test_dedicated_key_is_added_without_identities_only(home):
    key, _ = S.ensure_client_key(home)
    args = S.base_args(conn(), home)
    assert args[args.index("-i") + 1] == str(key)
    assert not any("IdentitiesOnly" in a for a in args)
    # an explicit key file wins over the dedicated key
    args = S.base_args(conn(identity_file="~/.ssh/id_work"), home)
    assert args[args.index("-i") + 1] == str(Path("~/.ssh/id_work").expanduser())


def test_percent_in_home_is_escaped(tmp_path):
    h = tmp_path / "50%off"
    h.mkdir()
    assert "UserKnownHostsFile=" + str(h / "known_hosts").replace("%", "%%") in S.base_args(conn(), h)


def test_direct_mode_has_no_ssh(home):
    with pytest.raises(ValueError, match="direct"):
        S.base_args(conn(mode="direct", host="", ssh_user="", direct_url="https://a:8443"), home)


@pytest.mark.parametrize("bad", [dict(host="-oProxyCommand=touch /tmp/pwned"), dict(host="spark;id"),
                                 dict(ssh_user="spark; rm -rf ~"), dict(ssh_user="-oProxyCommand=x"),
                                 dict(ssh_user="a b")])
async def test_injection_is_refused_before_ssh_runs(fake, home, bad):
    with pytest.raises(ValueError):
        await S.run_remote(conn(**bad), ["true"], home=home, timeout=5)
    assert fake.calls() == []
    with pytest.raises(ValueError):
        S.scan_host_keys(bad.get("host", "-x"), 22)
    assert fake.calls("ssh-keyscan") == []


async def test_remote_argv_is_one_quoted_string_after_double_dash(fake, home):
    trust(home)
    fake.configure(canned=[{"match": "echo", "stdout": "ok\n"}])
    argv = ["echo", "a; rm -rf /", "$(id)", "`id`", "it's"]
    res = await S.run_remote(conn(), argv, home=home, timeout=10)
    assert res.ok and res.stdout == "ok\n"
    sent = fake.calls()[-1]["argv"]
    assert sent[-3:] == ["--", "spark-a.local", shlex.join(argv)]
    assert shlex.split(sent[-1]) == argv


# ---- host keys --------------------------------------------------------------------------------------
def test_fingerprint_matches_openssh():
    t, b = HOSTKEY.split()
    assert S.fingerprint(b) == HOSTKEY_FP                 # `ssh-keygen -lf` prints this for that key
    assert HostKey.parse(t, b).fingerprint == HOSTKEY_FP


def test_hostkey_parse_is_strict():
    t, b = HOSTKEY.split()
    with pytest.raises(ValueError):
        HostKey.parse("ssh-rsa", b)                       # blob says ed25519
    with pytest.raises(ValueError):
        HostKey.parse(t, b[:-4] + "!!!!")
    with pytest.raises(ValueError):
        HostKey.parse("ssh-dsa-unknown", b)
    k = HostKey.parse(*blob_line("ecdsa-sha2-nistp256").split())
    assert k.type == "ecdsa-sha2-nistp256"


async def test_tofu_unknown_trusted_changed(fake, home):
    keys = S.scan_host_keys("spark-a.local", 22)
    assert [(k.type, k.fingerprint) for k in keys] == [("ssh-ed25519", HOSTKEY_FP)]
    assert fake.calls("ssh-keyscan")[-1]["argv"][-2:] == ["--", "spark-a.local"]
    assert S.host_key_status(home, "spark-a.local", 22, keys) == "unknown"
    with pytest.raises(SSHError) as exc:
        await S.run_remote(conn(), ["true"], home=home, timeout=10)
    assert exc.value.kind == "host-key-unknown" and "not trusted yet" in exc.value.message

    assert S.trust_host_keys(home, "spark-a.local", 22, keys) == 1
    assert S.trust_host_keys(home, "spark-a.local", 22, keys) == 0          # idempotent
    assert S.host_key_status(home, "spark-a.local", 22, keys) == "trusted"
    assert (await S.run_remote(conn(), ["true"], home=home, timeout=10)).rc == 0
    if POSIX:
        assert stat.S_IMODE((home / "known_hosts").stat().st_mode) == 0o600

    fake.configure(hostkey=OTHER_HOSTKEY)                  # the Spark was reinstalled (or worse)
    new = S.scan_host_keys("spark-a.local", 22)
    assert S.host_key_status(home, "spark-a.local", 22, new) == "changed"
    with pytest.raises(SSHError) as exc:
        await S.run_remote(conn(), ["true"], home=home, timeout=10)
    assert exc.value.kind == "host-key-changed" and "CHANGED" in exc.value.message
    assert "@@@" not in exc.value.detail
    with pytest.raises(ValueError, match="changed"):
        S.trust_host_keys(home, "spark-a.local", 22, new)                  # never silently
    S.trust_host_keys(home, "spark-a.local", 22, new, replace=True)
    assert S.host_key_status(home, "spark-a.local", 22, new) == "trusted"
    assert S.host_key_status(home, "spark-a.local", 22, keys) == "changed"  # the old key is no longer trusted
    assert HOSTKEY.split()[1] not in (home / "known_hosts").read_text()
    assert (await S.run_remote(conn(), ["true"], home=home, timeout=10)).rc == 0


async def test_non_default_port_uses_bracket_pattern(fake, home):
    keys = S.scan_host_keys("Spark-A.local", 2222)
    S.trust_host_keys(home, "Spark-A.local", 2222, keys)
    assert (home / "known_hosts").read_text().startswith("[spark-a.local]:2222 ssh-ed25519 ")
    assert S.host_key_status(home, "spark-a.local", 22, keys) == "unknown"     # port 22 is a different entry
    assert (await S.run_remote(conn(host="spark-a.local", ssh_port=2222), ["true"], home=home, timeout=10)).ok
    assert S.forget_host(home, "spark-a.local", 2222) == 1
    assert S.known_keys(home, "spark-a.local", 2222) == []


def test_hashed_and_extra_type_entries(home):
    import base64
    import hashlib
    import hmac

    salt = b"0123456789abcdefghij"
    digest = hmac.new(salt, b"spark-a.local", hashlib.sha1).digest()
    hashed = f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()}"
    (home / "known_hosts").write_text(f"# comment\n@cert-authority * {HOSTKEY}\n{hashed} {HOSTKEY}\n")
    ed = HostKey.parse(*HOSTKEY.split())
    ecdsa = HostKey.parse(*blob_line("ecdsa-sha2-nistp256").split())
    assert S.host_key_status(home, "spark-a.local", 22, [ed]) == "trusted"
    assert S.host_key_status(home, "spark-a.local", 22, [ed, ecdsa]) == "trusted"      # a new extra type is fine
    assert S.host_key_status(home, "spark-a.local", 22, [ecdsa]) == "changed"          # the known type vanished
    assert S.host_key_status(home, "other", 22, [ed]) == "unknown"


def test_keyscan_failure_is_classified(fake):
    fake.configure(mode="unreachable")
    with pytest.raises(SSHError) as exc:
        S.scan_host_keys("spark-a.local", 22)
    assert exc.value.kind == "unreachable" and "No host key" in exc.value.message


# ---- failure classification ----------------------------------------------------------------------------
@pytest.mark.parametrize("stderr,kind", [
    ("spark@spark-a: Permission denied (publickey,password).", "auth"),
    ("Received disconnect from 1.2.3.4 port 22:2: Too many authentication failures", "auth"),
    ("@@@\n@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @\nHost key verification failed.", "host-key-changed"),
    ("No ED25519 host key is known for x and you have requested strict checking.\nHost key verification failed.",
     "host-key-unknown"),
    ("ssh: connect to host x port 22: Connection refused", "unreachable"),
    ("ssh: connect to host x port 22: Connection timed out", "unreachable"),
    ("ssh: connect to host x port 22: No route to host", "unreachable"),
    ("ssh: Could not resolve hostname x: Name or service not known", "unreachable"),
    ("ssh: Could not resolve hostname x: nodename nor servname provided, or not known", "unreachable"),
    ("Timeout, server host not responding.", "unreachable"),
    ("bind [127.0.0.1]:8443: Address already in use\nCould not request local forwarding.", "port-in-use"),
    ("something odd happened", "other"),
])
def test_ssh_failure_kinds(stderr, kind):
    err = S.ssh_failure(stderr, host="x", port=22, user="spark")
    assert err.kind == kind and err.message and "\n" not in err.message


def test_failure_detail_redacts_token_like_strings():
    err = S.ssh_failure("weird failure abcdefghijklmnopqrstuvwxyz0123456789ABCD", host="x")
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in err.detail and "[REDACTED]" in err.detail


# ---- run_remote ---------------------------------------------------------------------------------------
async def test_run_remote_results_and_errors(fake, home):
    trust(home)
    fake.configure(canned=[{"match": "false-ish", "rc": 3, "stdout": "partial", "stderr": "no such thing\n"}])
    res = await S.run_remote(conn(), ["false-ish"], home=home, timeout=10)
    assert (res.rc, res.stdout, res.stderr, res.ok) == (3, "partial", "no such thing\n", False)  # not an SSHError
    fake.configure(auth="deny")
    with pytest.raises(SSHError) as exc:
        await S.run_remote(conn(), ["true"], home=home, timeout=10)
    assert exc.value.kind == "auth" and "spark@spark-a.local" in exc.value.message
    fake.configure(auth="any", mode="unreachable")
    with pytest.raises(SSHError) as exc:
        await S.run_remote(conn(), ["true"], home=home, timeout=10)
    assert exc.value.kind == "unreachable"


async def test_run_remote_timeout_kills_ssh(fake, home):
    trust(home)
    fake.configure(canned=[{"match": "slow", "sleep": 30}])
    with pytest.raises(SSHError) as exc:
        await S.run_remote(conn(), ["slow"], home=home, timeout=0.5)
    assert exc.value.kind == "timeout"
    await asyncio.sleep(0.1)
    assert not alive(fake.calls()[-1]["pid"])


async def test_run_remote_cancel_kills_ssh(fake, home):
    trust(home)
    fake.configure(canned=[{"match": "slow", "sleep": 30}])
    task = asyncio.create_task(S.run_remote(conn(), ["slow"], home=home, timeout=60))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if fake.calls():
            break
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(fake.calls()[-1]["pid"])


async def test_run_remote_passes_input_through_a_shell(fake, home, tmp_path):
    trust(home)
    box = Sandbox(tmp_path / "node")
    fake.configure(shell=box.shell_config())
    res = await S.run_remote(conn(), ["cat"], home=home, timeout=10, input=b"hello spark")
    assert res.stdout == "hello spark"


# ---- client key + password bootstrap ---------------------------------------------------------------------
def test_ensure_client_key(home):
    key, line = S.ensure_client_key(home)
    assert key == home / "ssh" / "twinspark_ed25519"
    assert S.check_public_line(line) == line and line.startswith("ssh-ed25519 ")
    assert S.ensure_client_key(home) == (key, line)                       # created once
    if POSIX:
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    Path(str(key) + ".pub").unlink()                                       # lost .pub: derived again
    _, again = S.ensure_client_key(home)
    assert again.split()[:2] == line.split()[:2]


@pytest.mark.parametrize("bad", ["", "ssh-ed25519", HOSTKEY + " a b", HOSTKEY + "\n" + HOSTKEY,
                                 'command="sh" ' + HOSTKEY, HOSTKEY + " $(id)", "ssh-rsa " + HOSTKEY.split()[1]])
def test_public_line_is_strict(bad):
    with pytest.raises(ValueError):
        S.check_public_line(bad)


PASSWORD = "c0rrect horse;$(battery) 'staple'"


def _everything_on_disk(root: Path, fake: FakeSSH) -> bytes:
    """All files under ``root`` except the fake server's own config (it has to know the password)."""
    out = b""
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink() and p != fake.dir / "config.json":
            out += p.read_bytes()
    return out


async def test_password_bootstrap_installs_the_key_once(fake, home, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    trust(home)
    box = Sandbox(tmp_path / "node")
    ak = box.home / ".ssh" / "authorized_keys"
    fake.configure(auth="keys", password=PASSWORD, authorized_keys=str(ak), shell=box.shell_config())
    key, line = S.ensure_client_key(home)
    with pytest.raises(SSHError):                         # before: the key is not accepted
        await S.run_remote(conn(), ["true"], home=home, timeout=10)

    assert await S.install_key_with_password(conn(), PASSWORD, line, home=home) is True
    assert ak.read_text().splitlines() == [line]
    assert stat.S_IMODE(ak.stat().st_mode) == 0o600 and stat.S_IMODE(ak.parent.stat().st_mode) == 0o700
    assert (await S.run_remote(conn(), ["true"], home=home, timeout=10)).ok      # BatchMode with the key now

    pw_call = next(c for c in fake.calls() if c["askpass"])
    assert pw_call["askpass_require"] == "force"
    assert not Path(pw_call["askpass"]).exists() and not Path(pw_call["askpass"]).parent.exists()
    verify = fake.calls()[fake.calls().index(pw_call) + 1]["argv"]
    assert "IdentitiesOnly=yes" in verify and str(key) in verify and "BatchMode=yes" in verify

    # a second run finds the key (also with a different comment) and adds nothing
    assert await S.install_key_with_password(conn(), PASSWORD, line, home=home) is False
    assert ak.read_text().splitlines() == [line]

    secret = PASSWORD.encode()
    assert secret not in (fake.dir / "calls.jsonl").read_bytes()
    assert secret not in _everything_on_disk(tmp_path, fake)
    assert PASSWORD not in caplog.text
    assert "TSM_ASKPASS_SECRET" not in os.environ


async def test_password_bootstrap_appends_after_a_line_without_newline(fake, home, tmp_path):
    trust(home)
    box = Sandbox(tmp_path / "node")
    ak = box.home / ".ssh" / "authorized_keys"
    ak.parent.mkdir(mode=0o700)
    ak.write_text("ssh-ed25519 AAAAexisting user@old")                      # no trailing newline
    fake.configure(auth="keys", password=PASSWORD, authorized_keys=str(ak), shell=box.shell_config())
    _, line = S.ensure_client_key(home)
    assert await S.install_key_with_password(conn(), PASSWORD, line, home=home) is True
    assert ak.read_text().splitlines() == ["ssh-ed25519 AAAAexisting user@old", line]


async def test_wrong_password_is_an_auth_error_and_cleans_up(fake, home, tmp_path, caplog):
    trust(home)
    box = Sandbox(tmp_path / "node")
    fake.configure(auth="keys", password="the-real-one", authorized_keys=str(box.home / "ak"),
                   shell=box.shell_config())
    _, line = S.ensure_client_key(home)
    with pytest.raises(SSHError) as exc:
        await S.install_key_with_password(conn(), "wrong-guess-123", line, home=home)
    assert exc.value.kind == "auth" and "password was not accepted" in exc.value.message
    assert "wrong-guess-123" not in exc.value.message + exc.value.detail + caplog.text
    pw_call = next(c for c in fake.calls() if c["askpass"])
    assert not Path(pw_call["askpass"]).parent.exists()
    assert b"wrong-guess-123" not in _everything_on_disk(tmp_path, fake)


async def test_password_is_never_sent_to_an_untrusted_host(fake, home, tmp_path):
    fake.configure(auth="keys", password=PASSWORD, authorized_keys=str(tmp_path / "ak"))
    _, line = S.ensure_client_key(home)
    with pytest.raises(SSHError) as exc:
        await S.install_key_with_password(conn(), PASSWORD, line, home=home)
    assert exc.value.kind == "host-key-unknown"


async def test_password_bootstrap_input_checks(fake, home):
    _, line = S.ensure_client_key(home)
    for pw in ("", "two\nlines"):
        with pytest.raises(ValueError):
            await S.install_key_with_password(conn(), pw, line, home=home)
    with pytest.raises(ValueError):
        await S.install_key_with_password(conn(), "pw", line + "\nssh-ed25519 AAAA evil", home=home)
    assert fake.calls() == []


# ---- tunnels --------------------------------------------------------------------------------------------
@pytest.fixture
async def echo_server():
    async def handle(reader, writer):
        try:
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


class Sleeps:
    """Injectable back-off: records the requested delay, then waits only a moment."""

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0.01)


def tunnel(home: Path, forwards, **kw) -> Tunnel:
    kw.setdefault("poll_connecting", 0.05)
    kw.setdefault("poll_up", 0.1)
    return Tunnel(conn(), forwards, home=home, **kw)


async def _until(pred, timeout: float = 15.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_tunnel_up_forwards_traffic_and_stop_leaves_no_child(fake, home, echo_server):
    trust(home)
    lport = S.free_local_port()
    states: list[str] = []
    t = tunnel(home, [(lport, echo_server, "api")], on_change=lambda tun: states.append(tun.state))
    await t.start()
    assert await t.wait_for("up", timeout=15) == "up"
    reader, writer = await asyncio.open_connection("127.0.0.1", lport)
    writer.write(b"ping")
    await writer.drain()
    assert await asyncio.wait_for(reader.readexactly(4), 5) == b"ping"
    writer.close()
    argv = fake.calls()[-1]["argv"]
    assert "-N" in argv and f"127.0.0.1:{lport}:127.0.0.1:{echo_server}" in argv
    assert argv[-2:] == ["--", "spark-a.local"]
    snap = t.snapshot()
    assert snap["state"] == "up" and snap["forwards"][0]["local"] == f"127.0.0.1:{lport}"
    pid = t.pid
    assert alive(pid)
    await t.stop()
    assert t.state == "stopped" and not alive(pid)
    assert states[:2] == ["connecting", "up"] and states[-1] == "stopped"
    await t.stop()                                                          # twice is fine


async def test_tunnel_reconnects_with_exponential_backoff(fake, home, echo_server):
    trust(home)
    fake.configure(exit_after=0.3)
    sleeps = Sleeps()
    ups: list[float] = []
    t = tunnel(home, [(S.free_local_port(), echo_server, "api")], sleep=sleeps, backoff_max=4.0,
               stable_after=1000.0, on_change=lambda tun: ups.append(tun.since) if tun.state == "up" else None)
    await t.start()
    try:
        await _until(lambda: len(sleeps.delays) >= 4)
        assert sleeps.delays[:4] == [1.0, 2.0, 4.0, 4.0]                   # doubling, capped
        assert len(ups) >= 2                                                # it came back up in between
        assert t.state in ("up", "reconnecting", "unreachable") and t.restarts >= 4
        assert len(fake.calls()) >= 4
    finally:
        await t.stop()


async def test_backoff_starts_over_after_a_stable_period(fake, home, echo_server):
    trust(home)
    fake.configure(exit_after=0.3)
    sleeps = Sleeps()
    t = tunnel(home, [(S.free_local_port(), echo_server, "api")], sleep=sleeps, stable_after=0.0)
    await t.start()
    try:
        await _until(lambda: len(sleeps.delays) >= 3)
        assert sleeps.delays[:3] == [1.0, 1.0, 1.0]
    finally:
        await t.stop()


async def test_tunnel_auth_failure_is_terminal(fake, home, echo_server):
    trust(home)
    fake.configure(auth="deny")
    sleeps = Sleeps()
    t = tunnel(home, [(S.free_local_port(), echo_server, "api")], sleep=sleeps)
    await t.start()
    assert await t.wait_for("auth-failed", timeout=15) == "auth-failed"
    assert "refused the login" in t.last_error and t.snapshot()["terminal"] is True
    pid = t.pid
    await asyncio.sleep(0.3)
    assert len(fake.calls()) == 1 and sleeps.delays == [] and not t.running and not alive(pid)
    fake.configure(auth="any")                                              # the user fixed it: start again
    await t.start()
    assert await t.wait_for("up", timeout=15) == "up"
    await t.stop()


async def test_tunnel_host_key_problem_is_terminal(fake, home, echo_server):
    t = tunnel(home, [(S.free_local_port(), echo_server, "api")], sleep=Sleeps())
    await t.start()
    assert await t.wait_for("host-key-problem", timeout=15) == "host-key-problem"
    assert "not trusted" in t.last_error
    trust(home)
    fake.configure(hostkey=OTHER_HOSTKEY)
    await t.start()
    assert await t.wait_for("host-key-problem", timeout=15) == "host-key-problem"
    assert "CHANGED" in t.last_error
    await t.stop()


async def test_tunnel_unreachable_keeps_retrying(fake, home, echo_server):
    trust(home)
    fake.configure(mode="unreachable")
    sleeps = Sleeps()
    t = tunnel(home, [(S.free_local_port(), echo_server, "api")], sleep=sleeps)
    await t.start()
    try:
        await _until(lambda: len(sleeps.delays) >= 2)
        assert t.state == "unreachable" and "Could not reach" in t.last_error and t.running
        fake.configure(mode="ok")
        assert await t.wait_for("up", timeout=15) == "up"
        assert t.last_error is None
    finally:
        await t.stop()


async def test_cancelling_the_supervisor_kills_ssh(fake, home, echo_server):
    trust(home)
    t = tunnel(home, [(S.free_local_port(), echo_server, "api")])
    task = asyncio.create_task(t.run())
    await t.wait_for("up", timeout=15)
    pid = t.pid
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(pid) and t.state == "stopped"


async def test_busy_local_port_is_reported_without_running_ssh(fake, home, echo_server):
    trust(home)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        sleeps = Sleeps()
        t = tunnel(home, [(port, echo_server, "api")], sleep=sleeps)
        await t.start()
        try:
            await _until(lambda: sleeps.delays)
            assert t.state == "reconnecting" and f"Local port {port}" in t.last_error
            assert fake.calls() == []
        finally:
            await t.stop()


async def test_refused_remote_connection_is_noted(fake, home):
    trust(home)
    dead = S.free_local_port()                          # nothing listens there on "node A"
    t = tunnel(home, [(S.free_local_port(), dead, "gateway")])
    await t.start()
    try:
        await t.wait_for("up", timeout=15)
        await _until(lambda: t.last_refused_at is not None)
    finally:
        await t.stop()


async def test_on_change_errors_do_not_break_the_tunnel(fake, home, echo_server):
    trust(home)

    def boom(_t):
        raise RuntimeError("ui bug")

    t = tunnel(home, [(S.free_local_port(), echo_server, "api")], on_change=boom)
    await t.start()
    assert await t.wait_for("up", timeout=15) == "up"
    await t.stop()


def test_tunnel_input_checks(home):
    with pytest.raises(ValueError):
        Tunnel(conn(), [], home=home)
    with pytest.raises(ValueError):
        Tunnel(conn(), [(1000, 1, "a"), (1000, 2, "b")], home=home)
    with pytest.raises(ValueError):
        Tunnel(conn(), [(0, 1, "a")], home=home)
    with pytest.raises(ValueError):
        Tunnel(conn(), [(1000, 1, "a", "-oProxyCommand=x")], home=home)
    with pytest.raises(ValueError):
        Tunnel(conn(mode="direct", host="", ssh_user="", direct_url="https://a:1"), [(1000, 1, "a")], home=home)


async def test_start_fails_plainly_without_ssh(home, monkeypatch, tmp_path):
    monkeypatch.setenv("TSM_SSH", str(tmp_path / "missing-ssh"))
    t = Tunnel(conn(), [(S.free_local_port(), 8443, "api")], home=home)
    with pytest.raises(SSHError) as exc:
        await t.start()
    assert exc.value.kind == "missing" and t.state == "stopped"


def test_plan_forwards_and_ipv6_spec():
    c = conn(local_api_port=None, local_gateway_port=None)
    fw = S.plan_forwards(c)
    assert [f.label for f in fw] == ["api", "gateway"] and fw[0].local_port != fw[1].local_port
    assert (fw[0].remote_port, fw[1].remote_port) == (8443, 8000)
    fixed = S.plan_forwards(conn(local_api_port=18443, local_gateway_port=18000), manager_host="100.64.0.1")
    assert fixed[0] == S.Forward(18443, 8443, "api", "100.64.0.1") and fixed[1].local_port == 18000
    assert S.Forward(1, 2, "x", "::1").spec() == "127.0.0.1:1:[::1]:2"
    t = Tunnel.for_connection(conn(local_api_port=18443, local_gateway_port=18000), home=Path("/nonexistent"))
    assert [f.local_port for f in t.forwards] == [18443, 18000]


def test_askpass_helper_answers_password_prompts_only(tmp_path):
    import subprocess

    helper = S._write_askpass(tmp_path)
    assert stat.S_IMODE(helper.stat().st_mode) == 0o700
    env = {**os.environ, S.ASKPASS_ENV: PASSWORD}
    pw = subprocess.run([str(helper), "spark@spark-a's password: "], capture_output=True, text=True, env=env)
    assert (pw.returncode, pw.stdout) == (0, PASSWORD + "\n")
    question = "Are you sure you want to continue connecting (yes/no/[fingerprint])? "
    hk = subprocess.run([str(helper), question], capture_output=True, text=True, env=env)
    assert hk.returncode == 1 and hk.stdout == ""
    env.pop(S.ASKPASS_ENV)
    assert subprocess.run([str(helper), "password: "], capture_output=True, env=env).returncode == 1
    assert PASSWORD not in helper.read_text()
