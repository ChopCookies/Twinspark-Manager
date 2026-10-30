"""`tsm update` on the command line, and the workstation's own update (`tsm update here`)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from twinspark import cli, cli_update, local_update, releases
from twinspark.controller.app import create_app

from .test_selfupdate import CONS, WHEEL, plan_for

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"


# ---- how TwinSpark is installed here ------------------------------------------------------------------
def test_a_spark_install_is_refused_here(tmp_path):
    (tmp_path / "venv").mkdir()
    (tmp_path / local_update.MARKER).write_text("x")
    k = local_update.install_kind(str(tmp_path / "venv"), "/usr")
    assert k["kind"] == "spark" and not k["ok"] and "tsm update sparks" in k["why"]


def test_install_kinds(monkeypatch, tmp_path):
    class Dist:
        def __init__(self, direct):
            self.direct = direct

        def read_text(self, name):
            return json.dumps(self.direct) if self.direct is not None else None

    def use(direct):
        monkeypatch.setattr(local_update.metadata, "distribution", lambda name: Dist(direct))

    use({"url": "file:///src/tsm", "dir_info": {"editable": True}})
    assert local_update.install_kind("/x/venv", "/usr")["kind"] == "editable"
    use({"url": "https://github.com/x/y", "vcs_info": {"vcs": "git"}})
    assert local_update.install_kind("/home/u/.local/share/pipx/venvs/twinspark-manager", "/usr")["kind"] == "pipx"
    assert local_update.install_kind(r"C:\Users\u\pipx\venvs\twinspark-manager", r"C:\Python312")["kind"] == "pipx"
    use(None)
    assert local_update.install_kind(str(tmp_path / "env"), "/usr")["kind"] == "venv"
    sysk = local_update.install_kind("/usr", "/usr")
    assert sysk["kind"] == "system" and not sysk["ok"] and "pipx" in sysk["why"]


def test_installed_extras_come_back(monkeypatch):
    class Dist:
        requires = ['keyring>=24; extra == "client"', 'huggingface_hub>=0.24; extra == "hf"',
                    'pytest>=9; extra == "dev"', "httpx>=0.27"]

    present = {"twinspark-manager", "keyring", "pytest", "httpx"}

    def dist(name):
        if name == releases.PROJECT:
            return Dist()
        if name not in present:
            raise local_update.metadata.PackageNotFoundError(name)
        return object()

    monkeypatch.setattr(local_update.metadata, "distribution", dist)
    assert local_update.installed_extras() == ["client"]          # dev is never reinstalled; hf is not there


def test_update_here_checks_the_download_and_installs_with_the_constraints(monkeypatch, tmp_path):
    plan = plan_for()
    monkeypatch.setattr(local_update, "install_kind", lambda: {"kind": "pipx", "ok": True, "prefix": "/p"})
    monkeypatch.setattr(local_update, "installed_extras", lambda: ["client"])
    monkeypatch.setattr(local_update, "pip_command", lambda kind: ["/p/bin/python", "-m", "pip"])
    monkeypatch.setattr(local_update, "installed_version", lambda: "9.9.9")
    files = {plan["wheel"]["url"]: WHEEL, plan["constraints"]["url"]: CONS}
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=files[str(r.url)])))
    seen = []

    def run(argv, text=True):
        seen.append(argv)
        assert Path(argv[-1].split("[")[0]).read_bytes() == WHEEL
        return SimpleNamespace(returncode=0)

    assert local_update.update_here(plan, client=client, run=run, log=lambda *a: None) == "9.9.9"
    argv = seen[0]
    assert argv[:4] == ["/p/bin/python", "-m", "pip", "install"] and argv[-1].endswith(".whl[client]")
    assert argv[argv.index("-c") + 1].endswith(releases.CONSTRAINTS)
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"evil")))
    with pytest.raises(releases.ReleaseError, match="does not match"):
        local_update.update_here(plan, client=bad, run=run, log=lambda *a: None)
    assert len(seen) == 1                       # nothing was installed from the tampered download


def test_update_here_refuses_a_development_checkout(monkeypatch):
    monkeypatch.setattr(local_update, "install_kind",
                        lambda: {"kind": "editable", "ok": False, "why": "update it with git pull"})
    with pytest.raises(RuntimeError, match="git pull"):
        local_update.update_here(plan_for())


# ---- the CLI against a live controller ------------------------------------------------------------------
class Server:
    def __init__(self, app):
        self.config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.02)
        sock = self.server.servers[0].sockets[0]
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def live(upd_sync):
    with Server(create_app(upd_sync.controller, "mgmt", run_startup=False, background=False)) as s:
        yield s


@pytest.fixture
def upd_sync(controller_config, tmp_path):
    from .conftest import Cluster
    cl = Cluster(controller_config, tmp_path)

    async def fetch(version, allow_prerelease):
        return plan_for()

    cl.controller.selfupdate.fetch = fetch
    return cl


def run_cli(*argv):
    return cli.main(["--key", "mgmt", *argv])


def test_tsm_update_sparks_runs_the_simulated_update_and_follows_it(live, capsys, monkeypatch):
    monkeypatch.setattr(cli_update, "_release", lambda args: (plan_for(), None))
    assert run_cli("--api", live.url, "update", "sparks", "--yes") == 0
    out = capsys.readouterr().out
    assert "Sparks: A 0.8.0, B 0.8.0 → 9.9.9" in out.replace(cli.__version__, "0.8.0")
    assert "done: both Sparks run TwinSpark 9.9.9 (simulated: dry-run nodes)" in out
    assert run_cli("--api", live.url, "update", "status") == 0
    assert "completed" in capsys.readouterr().out


def test_tsm_update_check_shows_every_node_and_the_release(live, capsys, monkeypatch):
    monkeypatch.setattr(cli_update, "_release", lambda args: (plan_for(), None))
    monkeypatch.setattr(local_update, "install_kind", lambda: {"kind": "pipx", "ok": True, "prefix": "/p"})
    assert run_cli("--api", live.url, "update") == 0
    out = capsys.readouterr().out
    assert re.search(r"this computer\s+\S+\s+\(pipx\)\s+→ 9\.9\.9: tsm update here", out), out
    assert re.search(r"node A\s+\S+\s+→ 9\.9\.9", out) and re.search(r"node B\s+\S+\s+→ 9\.9\.9", out)
    assert "update both: tsm update sparks" in out and "newest release   9.9.9" in out


def test_tsm_update_check_without_node_a(capsys, monkeypatch):
    monkeypatch.setattr(cli_update, "_release", lambda args: (None, "GitHub is not reachable from this computer"))
    monkeypatch.setattr(local_update, "install_kind", lambda: {"kind": "venv", "ok": True, "prefix": "/p"})
    assert run_cli("--api", "http://127.0.0.1:9", "update", "check") == 0
    out = capsys.readouterr().out
    assert "node A is not reachable" in out and "GitHub is not reachable" in out


def test_follow_waits_through_the_controller_restart(monkeypatch, capsys):
    states = iter([None, None, {"run_id": "r", "state": "running", "phase": "node A: restart"},
                   {"run_id": "r", "state": "completed", "target": {"version": "9.9.9"}}])
    monkeypatch.setattr(cli_update, "_get_state", lambda args: next(states))
    st = cli_update.follow(SimpleNamespace(), "r", sleep=lambda s: None)
    out = capsys.readouterr().out
    assert st["state"] == "completed" and out.count("restarting with the new version") == 1


def test_follow_exits_on_failure(monkeypatch):
    monkeypatch.setattr(cli_update, "_get_state",
                        lambda args: {"run_id": "r", "state": "failed", "error": "node B: rolled back"})
    with pytest.raises(SystemExit, match="rolled back"):
        cli_update.follow(SimpleNamespace(), "r", sleep=lambda s: None)


def test_tsm_update_here_on_windows_continues_outside_tsm_exe(monkeypatch, capsys):
    monkeypatch.setattr(cli_update.os, "name", "nt")
    monkeypatch.delenv("TSM_UPDATE_CHILD", raising=False)
    started = []
    monkeypatch.setattr(cli_update.subprocess, "Popen", lambda argv, env: started.append((argv, env)))
    assert cli_update._relaunch_on_windows({"version": "9.9.9"}, SimpleNamespace(prerelease=False))
    argv, env = started[0]
    assert argv[1:5] == ["-m", "twinspark.cli", "update", "here"] and "--yes" in argv and env["TSM_UPDATE_CHILD"] == "1"
    monkeypatch.setenv("TSM_UPDATE_CHILD", "1")
    assert not cli_update._relaunch_on_windows({"version": "9.9.9"}, SimpleNamespace(prerelease=False))


def test_tsm_update_policy_writes_the_switch(tmp_path, monkeypatch, capsys):
    p = tmp_path / "update-policy.json"
    monkeypatch.setattr(cli_update, "POLICY", p)
    assert run_cli("update", "policy") == 0
    assert "missing" in capsys.readouterr().out
    assert run_cli("update", "policy", "off") == 0
    assert json.loads(p.read_text()) == {"enabled": False, "allow_prerelease": False}
    assert run_cli("update", "policy", "on", "--prerelease") == 0
    assert json.loads(p.read_text()) == {"enabled": True, "allow_prerelease": True}
    if os.name != "nt":
        assert oct(p.stat().st_mode & 0o777) == "0o644"


def test_the_policy_command_needs_root_for_the_real_file(monkeypatch):
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("needs a non-root user")
    monkeypatch.setattr(cli_update, "POLICY", cli_update.DEFAULT_POLICY)
    with pytest.raises(SystemExit, match="sudo"):
        run_cli("update", "policy", "off")


# ---- install.sh writes the switch once -------------------------------------------------------------------
def test_install_sh_writes_the_update_policy_once(tmp_path):
    bash = "/bin/bash"
    if not Path(bash).exists():
        pytest.skip("needs bash")
    source = INSTALL.read_text(encoding="utf-8")
    fn = re.search(r"(?ms)^write_update_policy\(\) \{\n.*?^\}\n", source)
    assert fn, "write_update_policy not found in install.sh"
    script = fn.group(0) + "write_update_policy\n"
    env = {**os.environ, "TSM_CONFIG_DIR": str(tmp_path / "etc")}
    subprocess.run([bash, "-c", script], env=env, check=True)
    f = tmp_path / "etc" / "update-policy.json"
    assert json.loads(f.read_text()) == {"enabled": True, "allow_prerelease": False}
    f.write_text('{"enabled": false}\n')
    subprocess.run([bash, "-c", script], env=env, check=True)
    assert json.loads(f.read_text()) == {"enabled": False}           # the operator's choice stays
    assert "write_update_policy\n" in source.split('chmod -R u+rwX,go+rX,go-w "$TSM_HOME"', 1)[1][:40]


# ---- the client's own notice -----------------------------------------------------------------------------
async def test_the_client_app_reports_its_own_update(tmp_path, monkeypatch):
    from twinspark.client.app import create_client_app
    from twinspark.client.session import ClientSession

    async def latest(client, **kw):
        return plan_for()

    monkeypatch.setattr(releases, "latest_async", latest)
    monkeypatch.setattr(local_update, "install_kind", lambda: {"kind": "pipx", "ok": True, "prefix": "/p"})
    monkeypatch.setenv("TSM_CLIENT_KEYRING", "off")
    session = ClientSession(home=tmp_path / "client")
    app = create_client_app(session, "k" * 32)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18765",
                                 headers={"x-api-key": "k" * 32}) as c:
        r = await c.get("/api/v1/client/update")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["release"]["latest"] == "9.9.9" and body["release"]["available"]
    assert body["command"] == "tsm update here"


def test_sha_helper():
    assert releases.sha256(b"x") == hashlib.sha256(b"x").hexdigest() and sys.version_info >= (3, 12)
