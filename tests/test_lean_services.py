"""The services on each Spark stay small: per-role imports, lazy rich routes, an on-demand terminal."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from twinspark import provision, serve
from twinspark.controller.app import CAPABILITIES, LAZY_ROUTERS, create_app

ROOT = Path(__file__).resolve().parents[1]


def _modules_after(code: str) -> set[str]:
    script = code + "\nimport sys, json\nprint(json.dumps(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=120,
                         cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stderr[-2000:]
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("role", ["agent", "privd", "termd"])
def test_node_services_do_not_load_the_controller_or_fastapi(role):
    """`tsm serve agent|privd|termd` imports only what that role needs (it used to load everything)."""
    builder = {"agent": "build_agent_server", "privd": "run_privd", "termd": "build_termd_server"}[role]
    mods = _modules_after(
        "import sys\n"
        "from twinspark import cli, serve\n"
        "sys.argv = ['tsm', 'serve', '--help']\n"
        # the fast path parses `serve` without building the whole CLI
        "import argparse\n"
        "p = argparse.ArgumentParser(); cli._add_serve_args(p)\n"
        "p.parse_args(['" + role + "', '--config', 'x.yaml'])\n"
        f"import twinspark.serve as s; getattr(s, '{builder}')\n"
        + ("import twinspark.agent.agent\n" if role == "agent" else "")
        + ("import twinspark.remote.termd\n" if role == "termd" else "")
        + ("import twinspark.agent.privd\n" if role == "privd" else ""))
    for heavy in ("fastapi", "twinspark.controller.controller", "twinspark.cookbook", "twinspark.controller.app",
                  "twinspark.schemas.profile", "twinspark.schemas.artifact"):
        assert heavy not in mods, f"{role} imports {heavy}"


def test_the_serve_fast_path_skips_the_full_parser(monkeypatch):
    from twinspark import cli
    seen = {}
    monkeypatch.setattr(cli, "build_parser", lambda: (_ for _ in ()).throw(AssertionError("full parser built")))
    monkeypatch.setattr(cli, "cmd_serve", lambda args: seen.update(vars(args)))
    assert cli.main(["serve", "termd", "--config", "/etc/twinspark/agent.yaml", "--idle-exit", "900"]) == 0
    assert seen["what"] == "termd" and seen["idle_exit"] == 900.0
    with pytest.raises(SystemExit):
        cli.main(["serve", "agent"])                       # --config is still required


def test_socket_activation_is_detected_only_for_this_process():
    assert serve.socket_activated_fd({"LISTEN_FDS": "1", "LISTEN_PID": "42"}, pid=42) == 3
    assert serve.socket_activated_fd({"LISTEN_FDS": "1", "LISTEN_PID": "41"}, pid=42) is None   # a parent's fds
    assert serve.socket_activated_fd({}, pid=42) is None
    assert serve.socket_activated_fd({"LISTEN_FDS": "x", "LISTEN_PID": "42"}, pid=42) is None


async def test_an_idle_terminal_service_asks_its_server_to_stop(tmp_path):
    from twinspark.remote.termd import build_termd_app
    from twinspark.schemas.config import AgentConfig
    cfg = AgentConfig(node={"node_id": "A", "role": "agent"}, secrets_dir=str(tmp_path / "s"),
                      remote_mgmt={"policy_path": str(tmp_path / "policy.json"), "require_root_owned": False,
                                   "record_dir": str(tmp_path / "rec")})
    app = build_termd_app(cfg, token="t", idle_exit_s=0.2)
    app.state.server = SimpleNamespace(should_exit=False)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.server.should_exit:
                break
            await asyncio.sleep(0.05)
    assert app.state.server.should_exit is True


def test_terminal_units_are_socket_activated(tmp_path):
    a = provision.Answers(role="agent", qsfp_ip="192.168.100.2", service_user="spark", group="twinspark",
                          remote={"terminal": True})
    lay = provision.Layout(tmp_path)
    units = provision.render_units(a, lay)
    sock, svc = units["twinspark-terminal.socket"], units["twinspark-terminal.service"]
    assert "ListenStream=192.168.100.2:9444" in sock and "FreeBind=true" in sock and "WantedBy=sockets.target" in sock
    assert "[Install]" not in svc and "--idle-exit" in svc and "Requires=twinspark-terminal.socket" in svc


def test_setup_enables_the_socket_and_retires_an_always_on_terminal(tmp_path):
    a = provision.Answers(role="controller", service_user="spark", group="twinspark", remote={"terminal": True})
    lay = provision.Layout(tmp_path)
    calls = []
    prov = provision.Provisioner(a, lay, run=lambda argv, **kw: (calls.append(argv) or (0, "")))
    prov.systemd = True
    wants = lay.systemd / "multi-user.target.wants"
    wants.mkdir(parents=True)
    (wants / "twinspark-terminal.service").symlink_to(lay.systemd / "twinspark-terminal.service")
    prov.start(["twinspark-agent.service", "twinspark-terminal.service", "twinspark-terminal.socket"])
    assert ["systemctl", "enable", "--now", "twinspark-terminal.socket"] in calls
    assert ["systemctl", "enable", "--now", "twinspark-terminal.service"] not in calls
    assert ["systemctl", "stop", "twinspark-terminal.service"] in calls
    assert not (wants / "twinspark-terminal.service").is_symlink()


async def test_rich_routes_load_on_first_use_and_health_names_the_capabilities(cluster):
    for module in LAZY_ROUTERS.values():
        sys.modules.pop(module, None)
    app = create_app(cluster.controller, management_key="k", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m") as c:
        h = (await c.get("/api/v1/health")).json()
        assert h["api"] == 2 and set(h["capabilities"]) == set(CAPABILITIES)
        assert "twinspark.api.routes_cookbook" not in sys.modules
        r = await c.get("/api/v1/cookbook", headers={"x-api-key": "k"})
        assert r.status_code == 200 and "twinspark.api.routes_cookbook" in sys.modules
        assert (await c.get("/api/v1/cookbook")).status_code == 401          # still behind the key
        r = await c.post("/api/v1/integrations/check", headers={"x-api-key": "k"}, json={})
        assert r.status_code in (400, 404, 422)                               # routed, not the static 405
        assert (await c.get("/")).status_code in (200, 404)                    # the GUI mount still answers
