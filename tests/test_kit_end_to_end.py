"""`tsm kit` against real servers: the CLI as its own process, the controller, two agents and the gateway over HTTP
(the demo cluster), and a stand-in kit whose start.sh starts a real server (tests/fake_kit).

What the unit tests in test_kits.py cannot show: that the pieces fit. The CLI hands the Sparks over, runs the kit's
script as the right user with the right environment, the gateway forwards to the kit without TwinSpark's backend
key, and stop takes it all down again. As root the scripts run through the user's login shell (the `sudo tsm kit`
path); otherwise directly. The CI job "install and services on systemd" runs the same stand-in kit under sudo.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

from twinspark.demo import DemoCluster, free_port
from twinspark.security import SecretsVault

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="tsm kit runs on node A (Linux)")

KIT = "mia-qwen3.8-flash-next-tensorfold"
FAKE = Path(__file__).parent / "fake_kit"


@pytest.fixture
async def demo():
    async with DemoCluster() as d:
        yield d


@pytest.fixture
def checkout():
    """The stand-in kit in a folder of the user its scripts run as (as root: a normal user of this machine)."""
    env: dict[str, str] = {}
    owner = None
    if os.geteuid() == 0:
        import pwd
        owner = next((p for p in pwd.getpwall() if p.pw_uid >= 1000 and p.pw_name != "nobody"
                      and p.pw_shell.rsplit("/", 1)[-1] in ("bash", "sh", "zsh", "dash") and Path(p.pw_dir).is_dir()),
                     None)
        if owner is None or not shutil.which("runuser"):
            pytest.skip("as root, the kit's scripts need a normal user with a login shell to run as")
        env["SUDO_USER"] = owner.pw_name
    top = Path(tempfile.mkdtemp(prefix="tsm-kit-", dir="/tmp"))       # not pytest's tmp_path: that is 0700
    top.chmod(0o755)
    path = top / "kit"
    shutil.copytree(FAKE, path)
    if owner is not None:
        for p in (path, *path.iterdir()):
            os.chown(p, owner.pw_uid, owner.pw_gid)
    yield path, env
    pid = path / "server.pid"
    if pid.exists():
        with_pid = pid.read_text().strip()
        if with_pid.isdigit():
            try:
                os.kill(int(with_pid), 15)
            except OSError:
                pass
    shutil.rmtree(top, ignore_errors=True)


async def tsm(demo, env: dict[str, str], *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "twinspark.cli", "--api", demo.url, "--key", demo.key, *args,
        env={**os.environ, **env}, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), 120)
    return proc.returncode or 0, out.decode(errors="replace")


async def test_start_serve_and_stop_a_kit_over_real_servers(demo, checkout):
    path, env = checkout
    port = free_port()
    inference = SecretsVault(demo.a_layout.secrets).get("inference_api_key")
    chat = {"model": "default", "messages": [{"role": "user", "content": "hi"}]}

    code, out = await tsm(demo, env, "kit", "start", KIT, "--dir", str(path), "--port", str(port), "--yes")
    assert code == 0, out
    assert "serves through TwinSpark's gateway" in out and "GiB available" in out

    log = (path / "run.log").read_text()
    assert f"HOST=127.0.0.1 PORT={port} CONTAINER_NAME=qwen38-fn-tf" in log     # loopback, and the name we stop
    if "SUDO_USER" in env:
        assert f"user={env['SUDO_USER']} " in log                                # never root

    async with httpx.AsyncClient(timeout=30) as c:
        st = (await c.get(f"{demo.url}/api/v1/status", headers={"x-api-key": demo.key})).json()
        assert st["active"] is None and st["external"]["kit"] == KIT and st["external"]["state"] == "serving"
        auth = {"Authorization": f"Bearer {inference}"}
        r = await c.post(f"{demo.gateway_url}/v1/chat/completions", json=chat, headers=auth)
        assert r.status_code == 200, r.text
        answer = r.json()
        assert answer["model"] == "Fake-Kit-Model"                # the gateway sent the kit's own model name
        assert answer["seen_authorization"] is None               # and no TwinSpark key
        assert (await c.post(f"{demo.gateway_url}/v1/chat/completions", json={**chat, "model": "qwen"},
                             headers=auth)).status_code == 200
        assert (await c.post(f"{demo.gateway_url}/v1/chat/completions", json=chat)).status_code == 401
        listed = (await c.get(f"{demo.gateway_url}/v1/models", headers=auth)).json()
        assert {"default", "qwen"} <= {m["id"] for m in listed["data"]}

    code, out = await tsm(demo, env, "status")
    assert code == 0 and f"the kit {KIT} is serving" in out, out
    code, out = await tsm(demo, env, "kit", "start", KIT, "--dir", str(path), "--port", str(port), "--yes")
    assert code != 0 and "stop it first" in out, out               # one kit at a time, also the same one

    code, out = await tsm(demo, env, "kit", "stop", KIT, "--dir", str(path), "--yes")
    assert code == 0 and "stopped" in out, out
    assert f"PORT={port} CONTAINER_NAME=qwen38-fn-tf" in (path / "run.log").read_text().splitlines()[-1]
    async with httpx.AsyncClient(timeout=30) as c:
        st = (await c.get(f"{demo.url}/api/v1/status", headers={"x-api-key": demo.key})).json()
        assert st["external"] is None
        r = await c.post(f"{demo.gateway_url}/v1/chat/completions", json=chat,
                         headers={"Authorization": f"Bearer {inference}"})
        assert r.status_code in (404, 503), r.text
        with pytest.raises(httpx.HTTPError):
            await c.get(f"http://127.0.0.1:{port}/health")


async def test_a_kit_whose_own_start_fails_gives_the_sparks_back(demo, checkout):
    path, env = checkout
    start = path / "start.sh"
    start.write_text(start.read_text().replace('"${FAKE_KIT_FAIL:-0}"', '"1"'))    # the kit's own check refuses
    code, out = await tsm(demo, env, "kit", "start", KIT, "--dir", str(path), "--port", str(free_port()), "--yes")
    assert code != 0 and "did not start" in out, out
    lines = (path / "run.log").read_text().splitlines()
    assert lines[0].startswith("start ") and lines[-1].startswith("stop "), lines   # its stop.sh cleaned up
    async with httpx.AsyncClient(timeout=30) as c:
        st = (await c.get(f"{demo.url}/api/v1/status", headers={"x-api-key": demo.key})).json()
    assert st["external"] is None and not [r for r in st["routes"] if r.get("status") == "serving"]
