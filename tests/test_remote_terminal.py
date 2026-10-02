"""The terminal service against real pseudo-terminals (Linux only, no hardware, no root)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import stat
import sys
import time

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from twinspark.remote import policy as pol
from twinspark.remote import terminal as term
from twinspark.remote.termd import build_termd_app
from twinspark.schemas.config import AgentConfig

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="needs Linux PTYs")
TOKEN = "termd-test-token"


class PolicyFile:
    def __init__(self, path):
        self.path = path

    def write(self, **features):
        self.path.write_text(json.dumps(features))

    def unlink(self):
        self.path.unlink()

    def __fspath__(self):
        return str(self.path)


@pytest.fixture
def policy_file(tmp_path):
    f = PolicyFile(tmp_path / "remote-policy.json")
    f.write(terminal=True)
    return f


@pytest.fixture
async def mgr(tmp_path, policy_file):
    m = term.TerminalManager(pol.PolicySource(path=policy_file, require_root=False), tmp_path / "rec",
                             node_id="B")
    yield m
    await m.stop()                            # never leave a shell behind, even when a test fails


async def read_until(m, sess, needle: bytes, timeout=8.0) -> bytes:
    got = b""
    end = time.monotonic() + timeout
    while needle not in got:
        left = end - time.monotonic()
        if left <= 0:
            raise AssertionError(f"timed out waiting for {needle!r}; got {got[-300:]!r}")
        kind, payload = await asyncio.wait_for(m.next(sess), left)
        if kind == "out":
            got += payload
        elif kind == "exit":
            break
    return got


async def test_terminal_is_refused_until_enabled_and_says_how(tmp_path, policy_file, mgr):
    policy_file.write(terminal=False)
    with pytest.raises(term.TerminalError, match="sudo tsm remote enable terminal"):
        await mgr.open(80, 24)
    policy_file.unlink()
    with pytest.raises(term.TerminalError, match="not enabled on node B"):
        await mgr.open(80, 24)
    assert mgr.sessions == {}


async def test_a_shell_runs_commands_and_exits_cleanly(mgr):
    s = await mgr.open(100, 30, "tester")
    await mgr.write(s, b"echo out-$((6*7))\n")
    assert b"out-42" in await read_until(mgr, s, b"out-42")
    await mgr.write(s, b"exit 3\n")
    for _ in range(100):
        kind, payload = await asyncio.wait_for(mgr.next(s), 8)
        if kind == "exit":
            break
    assert payload["code"] == 3 and s.id not in mgr.sessions


async def test_terminal_size_follows_resize(mgr):
    s = await mgr.open(80, 24)
    mgr.resize(s, 132, 43)
    await mgr.write(s, b"stty size\n")
    assert b"43 132" in await read_until(mgr, s, b"43 132")
    await mgr.close(s, "test over")


async def test_shell_starts_in_a_clean_environment(mgr, monkeypatch):
    monkeypatch.setenv("VLLM_API_KEY", "must-not-leak-1234")
    monkeypatch.setenv("TSM_SECRET_THING", "must-not-leak-5678")
    s = await mgr.open(80, 24)
    await mgr.write(s, b"env | sort; echo done-$((1+1))\n")        # the typed echo never contains the result
    env = await read_until(mgr, s, b"done-2")
    assert b"must-not-leak" not in env and b"TERM=xterm-256color" in env and b"TSM_TERMINAL=1" in env
    await mgr.close(s, "test over")


async def test_session_limit_applies_per_node(mgr, policy_file):
    policy_file.write(terminal=True, terminal_max_sessions=1)
    a = await mgr.open(80, 24)
    with pytest.raises(term.TerminalError, match="limit 1"):
        await mgr.open(80, 24)
    await mgr.close(a, "x")
    b = await mgr.open(80, 24)                 # a slot is free again
    await mgr.close(b, "x")


def alive(pid: int) -> bool:
    """A killed child that nobody has reaped yet (zombie) is dead for our purposes."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


async def test_closing_a_session_kills_its_whole_process_group(mgr):
    s = await mgr.open(80, 24)
    await mgr.write(s, b"sleep 300 & echo child-$((1+1))-$!\n")      # the typed echo never matches
    out = await read_until(mgr, s, b"child-2-")
    out += await read_until(mgr, s, b"\r\n") if b"\r\n" not in out.split(b"child-2-")[1] else b""
    pid = int(out.split(b"child-2-")[1].split()[0])
    assert alive(pid)
    await mgr.close(s, "client left")
    for _ in range(60):
        if not alive(pid):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("background child survived the session")


async def test_idle_and_absolute_limits_close_sessions_with_a_warning_first(mgr, policy_file):
    policy_file.write(terminal=True, terminal_idle_s=120, terminal_max_s=3600)
    s = await mgr.open(80, 24)
    t0 = s.last_input
    await mgr.reap(now=t0 + 70)                # inside the last minute: warn, do not close
    kinds = []
    while not s.queue.empty():
        kinds.append((await s.queue.get())[0])
    assert "notice" in kinds and s.id in mgr.sessions
    await mgr.reap(now=t0 + 130)
    assert s.id not in mgr.sessions and "idle for 2 min" in s.reason

    s2 = await mgr.open(80, 24)
    s2.last_input = s2.created + 10 ** 6       # typing constantly, but the session is too old
    await mgr.reap(now=s2.created + 3601)
    assert s2.id not in mgr.sessions and "time limit" in s2.reason


async def test_turning_the_terminal_off_closes_running_sessions(mgr, policy_file):
    s = await mgr.open(80, 24)
    policy_file.write(terminal=False)
    await mgr.reap()
    assert s.id not in mgr.sessions and "disabled" in s.reason


async def test_output_is_recorded_privately_but_typed_secrets_are_not(mgr, tmp_path):
    s = await mgr.open(80, 24, "alice")
    await mgr.write(s, b"echo visible-output\n")
    await read_until(mgr, s, b"visible-output")
    await mgr.write(s, b"stty -echo; read -r pw; echo got-secret; stty echo\n")
    await asyncio.sleep(0.4)
    await mgr.write(s, b"hunter2-super-secret\n")
    await read_until(mgr, s, b"got-secret")
    await mgr.close(s, "done")
    files = list((tmp_path / "rec").glob("*.cast"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    text = files[0].read_text()
    header = json.loads(text.splitlines()[0])
    assert header["version"] == 2 and "alice" in header["title"]
    assert "visible-output" in text and "hunter2-super-secret" not in text
    assert "session closed: done" in text
    assert mgr.recordings()[0]["name"] == files[0].name


async def test_recording_can_be_switched_off_by_policy(mgr, policy_file, tmp_path):
    policy_file.write(terminal=True, record_terminal=False)
    s = await mgr.open(80, 24)
    await mgr.close(s, "x")
    assert not (tmp_path / "rec").exists() or not list((tmp_path / "rec").glob("*.cast"))


def test_recorder_stops_at_its_size_cap_and_prunes_old_files(tmp_path, monkeypatch):
    monkeypatch.setattr(term, "RECORD_CAP", 2000)
    monkeypatch.setattr(term, "RECORD_KEEP", 3)
    d = tmp_path / "rec"
    d.mkdir()
    for i in range(6):
        (d / f"2020010{i}T000000Z-{i:012x}.cast").write_text("{}\n")
    (d / "notes.txt").write_text("keep me")
    r = term.Recorder(d, "abcdef123456", 80, 24, "t")
    for _ in range(50):
        r.output(b"x" * 100)
    r.close("end")
    assert r.full and r.path.stat().st_size < 4000
    assert (d / "notes.txt").exists()
    casts = sorted(p.name for p in d.glob("*.cast"))
    assert len(casts) == 3 and casts[-1] == r.path.name


@pytest.mark.parametrize("name", ["../etc/passwd", "x.cast", "20240101T000000Z-zzzzzzzzzzzz.cast", "", "a/b.cast"])
def test_recordings_cannot_be_used_to_read_other_files(mgr, name):
    with pytest.raises(term.TerminalError):
        mgr.read_recording(name)


def test_shell_choice_never_lands_on_nologin(monkeypatch):
    monkeypatch.setattr(term.pwd, "getpwuid", lambda uid: type("P", (), {"pw_shell": "/usr/sbin/nologin",
                                                                          "pw_dir": "/", "pw_name": "svc"})())
    assert term.pick_shell(None) in ("/bin/bash", "/usr/bin/bash", "/bin/sh")
    assert term.pick_shell("/does/not/exist") in ("/bin/bash", "/usr/bin/bash", "/bin/sh")
    monkeypatch.setattr(term.os, "access", lambda *a, **k: False)
    with pytest.raises(term.TerminalError, match="no usable login shell"):
        term.pick_shell(None)


@pytest.mark.parametrize("cols,rows,expect", [(80, 24, (80, 24)), (0, 0, (20, 5)), (10 ** 6, 10 ** 6, (500, 200)),
                                              ("80", None, (80, 24)), (True, False, (80, 24)), (-5, -5, (20, 5))])
def test_window_sizes_are_clamped(cols, rows, expect):
    assert term.clamp_size(cols, rows) == expect


# ---- the daemon over a real socket -------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def termd(tmp_path, policy_file):
    cfg = AgentConfig(node={"node_id": "B", "role": "agent"}, secrets_dir=str(tmp_path / "sec"),
                      remote_mgmt={"policy_path": os.fspath(policy_file), "require_root_owned": False,
                                   "record_dir": str(tmp_path / "rec")})
    app = build_termd_app(cfg, token=TOKEN)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           ws_max_size=2 * 1024 ** 2))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield type("T", (), {"port": port, "app": app, "policy": policy_file})
    server.should_exit = True
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, 10)


def ws_url(t, query="cols=100&rows=30&actor=tester"):
    return f"ws://127.0.0.1:{t.port}/v1/terminal?{query}"


async def test_websocket_without_or_with_a_wrong_token_never_connects(termd):
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic " + TOKEN},
                    {"Authorization": "Bearer "}, {"X-Api-Key": TOKEN}):
        with pytest.raises(InvalidStatus) as exc:
            async with connect(ws_url(termd), additional_headers=headers):
                pass
        assert exc.value.response.status_code == 403
    assert termd.app.state.manager.sessions == {}


async def test_websocket_session_end_to_end(termd):
    async with connect(ws_url(termd), additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello" and hello["cols"] == 100 and hello["recorded"] is True
        await ws.send(b"echo ws-$((20+22))\n")
        got = b""
        while b"ws-42" not in got:
            m = await asyncio.wait_for(ws.recv(), 8)
            got += m if isinstance(m, bytes) else b""
        await ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        await ws.send(b"stty size\n")
        while b"40 120" not in got:
            got += await asyncio.wait_for(ws.recv(), 8)
        await ws.send(b"exit\n")
        exit_msg = None
        with contextlib.suppress(ConnectionClosed):
            while True:
                m = await asyncio.wait_for(ws.recv(), 8)
                if isinstance(m, str) and json.loads(m)["type"] == "exit":
                    exit_msg = json.loads(m)
    assert exit_msg and exit_msg["code"] == 0
    assert termd.app.state.manager.sessions == {}


async def test_disconnecting_the_browser_ends_the_shell(termd):
    mgr = termd.app.state.manager
    async with connect(ws_url(termd), additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        await ws.recv()
        assert len(mgr.sessions) == 1
        proc = next(iter(mgr.sessions.values())).proc
    for _ in range(100):
        if not mgr.sessions:
            break
        await asyncio.sleep(0.05)
    assert mgr.sessions == {} and proc.poll() is not None


async def test_websocket_tells_the_user_how_to_enable_a_disabled_terminal(termd):
    termd.policy.write(terminal=False)
    async with connect(ws_url(termd), additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        msg = json.loads(await ws.recv())
        assert msg["type"] == "error" and "sudo tsm remote enable terminal" in msg["error"]
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 5)


async def test_oversized_input_closes_the_session(termd):
    mgr = termd.app.state.manager
    async with connect(ws_url(termd), additional_headers={"Authorization": f"Bearer {TOKEN}"},
                       max_size=None) as ws:
        await ws.recv()
        await ws.send(b"x" * (term.MAX_INPUT + 10))
        with pytest.raises(ConnectionClosed):
            for _ in range(50):
                await asyncio.wait_for(ws.recv(), 5)
    for _ in range(100):
        if not mgr.sessions:
            break
        await asyncio.sleep(0.05)
    assert mgr.sessions == {}


@pytest.mark.parametrize("query", ["cols=abc&rows=-1", "cols=99999999999&rows=0", "cols=&rows=&actor=" + "A" * 500, ""])
async def test_odd_query_strings_still_give_a_sane_terminal(termd, query):
    async with connect(ws_url(termd, query), additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        hello = json.loads(await ws.recv())
        assert 20 <= hello["cols"] <= 500 and 5 <= hello["rows"] <= 200


async def test_http_endpoints_need_the_token(termd):
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{termd.port}") as c:
        for path in ("/v1/terminal/status", "/v1/terminal/recordings", "/v1/terminal/recordings/x.cast"):
            assert (await c.get(path)).status_code == 401
            assert (await c.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
        ok = await c.get("/v1/terminal/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200 and ok.json()["policy"]["terminal"] is True
        assert (await c.get("/openapi.json")).status_code == 404
        bad = await c.get("/v1/terminal/recordings/..%2F..%2Fetc%2Fpasswd",
                          headers={"Authorization": f"Bearer {TOKEN}"})
        assert bad.status_code in (404, 422)
