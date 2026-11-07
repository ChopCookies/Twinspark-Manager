"""Kits: a model served by its author's own scripts, with TwinSpark stepping aside and its gateway forwarding."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from twinspark import cli_kits, kits
from twinspark.agent.runtime import _looks_like_vllm
from twinspark.controller import kits as kit_ctl
from twinspark.controller.app import create_app
from twinspark.gateway.app import build_gateway_app
from twinspark.gateway.gateway import Gateway, RouteState
from twinspark.gateway.state import apply_state, export_state
from twinspark.schemas.enums import Topology

from .conftest import draft

QWEN = "mia-qwen3.8-flash-next-tensorfold"
DSV = "mia-deepseek-v4.1-flash-1017"


# ---- the catalog ---------------------------------------------------------------------------------------------
def test_the_catalog_has_mias_two_kits():
    all_ = kits.load_all()
    assert set(all_) == {QWEN, DSV}
    q, d = all_[QWEN], all_[DSV]
    assert q.repo == "https://github.com/MiaAI-Lab/Qwen3.8-Flash-Dual-DGX-Sparks-TensorFold"
    assert d.repo == "https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks"
    assert (q.container, d.container) == ("qwen38-fn-tf", "dsv41-1017-exl3-tf")
    assert d.min_available_gib == 112 and d.prepare_needs_idle
    assert q.measured["single_stream_tok_s"] == 67.4 and d.measured["single_stream_tok_s"] == 80.7


@pytest.mark.parametrize("patch, match", [
    ({"repo": "https://example.com/x/y"}, "github.com"),
    ({"repo": "https://github.com/x/y/../z"}, "github.com"),
    ({"name": "Bad Name"}, "invalid kit name"),
    ({"container": "a b"}, "container"),
    ({"start": "../start.sh"}, "inside the checkout"),
    ({"stop": "/usr/bin/stop"}, "inside the checkout"),
    ({"aliases": []}, "aliases"),
    ({"port": 80}, "port"),
])
def test_a_kit_that_could_reach_outside_its_checkout_is_refused(patch, match):
    doc = json.loads((kits.KITS_DIR / f"{QWEN}.json").read_text())
    doc.update(patch)
    with pytest.raises(kits.KitError, match=match):
        kits.parse(doc)


def test_the_agent_counts_a_tensorfold_container_as_a_foreign_engine():
    assert _looks_like_vllm("ghcr.io/miaai-lab/qwen3.8-flash-dual-dgx-sparks-tensorfold@sha256:ab", "")
    assert _looks_like_vllm("tensorfold-dsv41:exl3-tp2-0123abcd", "tf-dsv41-lanes /hf 1 2")
    assert not _looks_like_vllm("postgres:16", "postgres")


# ---- the gateway ---------------------------------------------------------------------------------------------
def test_a_kit_route_never_receives_the_backend_key():
    g = Gateway("client", "backend-secret")
    ours, kit = RouteState("a"), RouteState("b", keyless=True)
    assert g.backend_headers(ours)["authorization"] == "Bearer backend-secret"
    assert "authorization" not in g.backend_headers(kit)
    g.set_route("default", ["http://127.0.0.1:8888"], "Qwen", "kit:x", keyless=True)
    copy = Gateway("client", "backend-secret")
    apply_state(copy, export_state(g, 1))                      # the standalone gateway gets it too
    assert copy.routes["default"].keyless
    g.clear_route("default")
    assert not g.routes["default"].keyless


async def test_requests_reach_the_kit_without_twinsparks_key():
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    seen = []

    async def chat(request: Request):
        seen.append((str(request.url), request.headers.get("authorization"), (await request.json())["model"]))
        return JSONResponse({"ok": True})

    kit_server = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"])])
    g = Gateway("client-key", "backend-key", client=httpx.AsyncClient(transport=httpx.ASGITransport(app=kit_server)))
    g.set_route("default", ["http://127.0.0.1:8888"], "Qwen3.8-Flash-Next", "kit:x", keyless=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(g)), base_url="http://gw",
                                 headers={"authorization": "Bearer client-key"}) as c:
        r = await c.post("/v1/chat/completions", json={"model": "default", "messages": []})
    assert r.status_code == 200
    assert seen == [("http://127.0.0.1:8888/v1/chat/completions", None, "Qwen3.8-Flash-Next")]


# ---- the controller -----------------------------------------------------------------------------------------
@pytest.fixture
def healthy(monkeypatch):
    state = {"ok": True, "served": "Qwen3.8-Flash-Next"}

    async def probe(url):
        return state["ok"], state["served"] if state["ok"] else None

    monkeypatch.setattr(kit_ctl, "probe", probe)
    return state


def kit_running(cluster, name="qwen38-fn-tf"):
    for rt in cluster.runtimes.values():
        rt.foreign = [{"name": name, "image": "ghcr.io/miaai-lab/x-tensorfold@sha256:00", "status": "Up",
                       "command": "tensorfold-native serve"}]


async def test_handover_stops_twinsparks_model_and_the_gateway_forwards_to_the_kit(cluster, healthy):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.TP2))
    assert (await cluster.wait_job((await c.activate("qwen")).job_id)).state.value == "completed"
    res = await kit_ctl.handover(c, QWEN)
    assert c.active() is None and not any(cluster.runtimes[n].containers for n in "AB")
    assert res["external"]["state"] == "starting"
    st = cluster.gateway.routes["default"]
    assert not st.backends and "starting" in st.down_reason          # clients get 503 + Retry-After meanwhile
    kit_running(cluster)
    await kit_ctl.attach(c, QWEN)
    st = cluster.gateway.routes["default"]
    assert st.backends == ["http://127.0.0.1:8888"] and st.keyless and st.served_model == "Qwen3.8-Flash-Next"
    assert cluster.gateway.routes["qwen"].backends == ["http://127.0.0.1:8888"]
    ext = c.status()["external"]
    assert ext["kit"] == QWEN and ext["state"] == "serving" and ext["containers"] == {"A": "qwen38-fn-tf",
                                                                                     "B": "qwen38-fn-tf"}


async def test_a_kit_that_does_not_answer_is_not_routed(cluster, healthy):
    c = cluster.controller
    await kit_ctl.handover(c, QWEN)
    healthy["ok"] = False
    with pytest.raises(kit_ctl.KitStateError, match="does not answer"):
        await kit_ctl.attach(c, QWEN)
    assert not cluster.gateway.routes["default"].backends


async def test_switching_back_stops_the_kits_containers_on_both_sparks(cluster, healthy):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.TP2))
    await kit_ctl.handover(c, QWEN)
    kit_running(cluster)
    await kit_ctl.attach(c, QWEN)
    job = await cluster.wait_job((await c.activate("qwen")).job_id)
    assert job.state.value == "completed", job.error
    for n in "AB":
        assert ["docker", "stop", "-t", "60", "qwen38-fn-tf"] in cluster.runtimes[n].history
        assert not cluster.runtimes[n].foreign
    assert c.status()["external"] is None and c.active()["profile"] == "qwen"
    assert not cluster.gateway.routes["default"].keyless                    # TwinSpark's own model again
    assert any("stopped the kit" in (s.message or "") for s in job.steps)


async def test_a_switch_does_not_start_while_a_kit_container_cannot_be_stopped(cluster, healthy, monkeypatch):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.TP2))
    await kit_ctl.handover(c, QWEN)
    kit_running(cluster)
    await kit_ctl.attach(c, QWEN)

    def stuck(name, grace_s=60):
        raise RuntimeError("docker stop timed out")
    monkeypatch.setattr(cluster.runtimes["B"], "stop_foreign", stuck)
    job = await cluster.wait_job((await c.activate("qwen")).job_id)
    assert job.state.value == "failed" and "could not be stopped" in job.error
    assert not cluster.runtimes["A"].containers and not cluster.runtimes["B"].containers


async def test_failed_stop_keeps_the_kit_tracked_and_retries_cleanup(cluster, healthy, monkeypatch):
    c = cluster.controller
    await kit_ctl.handover(c, QWEN)
    kit_running(cluster)
    await kit_ctl.attach(c, QWEN)
    worker = cluster.runtimes["B"]
    with monkeypatch.context() as patch:
        def stuck(name, grace_s=60):
            raise RuntimeError("docker stop timed out")

        patch.setattr(worker, "stop_foreign", stuck)
        result = await kit_ctl.detach(c, stop_containers=True)
        assert result["problems"] and result["external"]["state"] == "stop-failed"
        assert kit_ctl.state(c)["containers"]["B"] == "qwen38-fn-tf"
        await kit_ctl.reattach(c)
        assert not cluster.gateway.routes["default"].backends
        with pytest.raises(kit_ctl.KitStateError, match="stop it first"):
            await kit_ctl.handover(c, QWEN)
    result = await kit_ctl.detach(c, stop_containers=True)
    assert result["stopped"] == ["B:qwen38-fn-tf"] and not result["problems"]
    assert not worker.foreign and kit_ctl.state(c) is None


async def test_stop_ends_a_serving_kit(cluster, healthy):
    c = cluster.controller
    await kit_ctl.handover(c, DSV)
    kit_running(cluster, "dsv41-1017-exl3-tf")
    await kit_ctl.attach(c, DSV)
    job = await c.stop()
    assert job.state.value == "completed", job.error
    assert c.status()["external"] is None
    assert not cluster.runtimes["A"].foreign and not cluster.runtimes["B"].foreign
    assert not cluster.gateway.routes["deepseek"].backends


async def test_the_watchdog_marks_a_silent_kit_down_and_up_again(cluster, healthy):
    c = cluster.controller
    await kit_ctl.handover(c, QWEN)
    await kit_ctl.attach(c, QWEN)
    healthy["ok"] = False
    assert await c.watchdog_tick() == f"kit {QWEN} does not answer"
    assert "does not answer" in cluster.gateway.routes["default"].down_reason
    healthy["ok"] = True
    assert await c.watchdog_tick() is None
    assert cluster.gateway.routes["default"].down_reason is None


async def test_a_restarted_controller_routes_the_kit_again(cluster, healthy):
    c = cluster.controller
    await kit_ctl.handover(c, QWEN)
    await kit_ctl.attach(c, QWEN)
    c.gateway.routes.clear()                         # an in-process gateway forgets its routes on restart
    c.gateway.set_route("old", ["http://x"], "old")
    await c._resume_active()
    assert "old" not in c.gateway.routes
    assert c.gateway.routes["default"].backends == ["http://127.0.0.1:8888"] and c.gateway.routes["default"].keyless


async def test_one_kit_at_a_time(cluster, healthy):
    c = cluster.controller
    await kit_ctl.handover(c, QWEN)
    with pytest.raises(kit_ctl.KitStateError, match="stop it first"):
        await kit_ctl.handover(c, DSV)
    with pytest.raises(kit_ctl.KitStateError, match="not handed the Sparks"):
        await kit_ctl.attach(c, DSV)


def test_the_api(cluster, healthy):
    app = create_app(cluster.controller, "mgmt-key", run_startup=False)
    h = {"x-api-key": "mgmt-key"}
    with TestClient(app) as client:
        assert client.get("/api/v1/kits").status_code == 401
        listed = client.get("/api/v1/kits", headers=h).json()
        assert {k["name"] for k in listed["kits"]} == {QWEN, DSV} and listed["external"] is None
        r = client.post(f"/api/v1/kits/{QWEN}/handover", json={"confirm": "yes"}, headers=h)
        assert r.status_code == 422
        assert client.post("/api/v1/kits/nope/handover", json={"confirm": "nope"}, headers=h).status_code == 404
        r = client.post(f"/api/v1/kits/{QWEN}/handover", json={"confirm": QWEN, "aliases": ["chat"]}, headers=h)
        assert r.status_code == 200 and r.json()["external"]["aliases"] == ["chat"]
        assert client.post(f"/api/v1/kits/{QWEN}/attach", headers=h).json()["external"]["state"] == "serving"
        assert client.get("/api/v1/status", headers=h).json()["external"]["kit"] == QWEN
        r = client.post("/api/v1/kits/detach", json={"stop": False, "kit": QWEN}, headers=h)
        assert r.status_code == 200 and r.json()["was"] == QWEN


# ---- tsm kit -----------------------------------------------------------------------------------------------------
class FakeApi:
    def __init__(self, active=None):
        self.calls = []
        self.active = active

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        if path == "/api/v1/status":
            return {"active": {"profile": self.active} if self.active else None}
        if path.endswith("/attach"):
            return {"external": {"aliases": ["default", "qwen"], "served": "Qwen3.8-Flash-Next"}}
        if path.endswith("/detach"):
            return {"stopped": [], "problems": []}
        return {}


def checkout(tmp_path: Path, start_code: int = 0) -> Path:
    d = tmp_path / "home" / "twinspark-kits" / QWEN
    (d / "scripts").mkdir(parents=True)
    log = tmp_path / "ran.log"
    (d / "start.sh").write_text(f'#!/bin/sh\necho "start HOST=$HOST PORT=$PORT args=$* cwd=$PWD" >> {log}\n'
                                f"exit {start_code}\n")
    (d / "stop.sh").write_text(f'#!/bin/sh\necho "stop" >> {log}\n')
    for f in ("start.sh", "stop.sh"):
        (d / f).chmod(0o755)
    d.chmod(0o755)
    return d


def args_for(action, **kw):
    import argparse
    base = dict(action=action, name=QWEN, dir=None, alias=None, port=None, yes=True, extra=[],
                api="http://127.0.0.1:8443", key="k", secrets_dir="/nonexistent", json=False)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def as_user(monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("tsm kit runs on node A (Linux)")
    monkeypatch.setattr(cli_kits, "_user", lambda: ("me", tmp_path / "home", os.getuid()))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    api = FakeApi(active="glm")
    monkeypatch.setattr(cli_kits, "_api", lambda args: api)
    return api


def test_kit_start_hands_over_runs_the_kits_script_bound_to_loopback_and_attaches(as_user, tmp_path):
    d = checkout(tmp_path)
    cli_kits.cmd_kit(args_for("start", extra=["--max-tokens", "16384"]), None)
    paths = [p for _, p, _ in as_user.calls]
    assert paths == ["/api/v1/status", f"/api/v1/kits/{QWEN}/handover", f"/api/v1/kits/{QWEN}/attach"]
    assert as_user.calls[1][2] == {"confirm": QWEN, "aliases": None, "port": 8888}
    ran = (tmp_path / "ran.log").read_text()
    assert f"start HOST=127.0.0.1 PORT=8888 args=--max-tokens 16384 cwd={d}" in ran


def test_a_kit_that_fails_to_start_is_cleaned_up(as_user, tmp_path):
    checkout(tmp_path, start_code=3)
    with pytest.raises(SystemExit, match="did not start. Nothing serves now"):
        cli_kits.cmd_kit(args_for("start"), None)
    assert (tmp_path / "ran.log").read_text().splitlines()[-1] == "stop"
    assert as_user.calls[-1] == ("POST", "/api/v1/kits/detach", {"stop": True, "kit": QWEN})


def test_kit_stop_runs_the_kits_stop_and_releases_the_route(as_user, tmp_path):
    checkout(tmp_path)
    cli_kits.cmd_kit(args_for("stop"), None)
    assert (tmp_path / "ran.log").read_text() == "stop\n"
    assert as_user.calls == [("POST", "/api/v1/kits/detach", {"stop": False, "kit": QWEN})]


def test_kit_start_refuses_a_folder_everyone_can_write_and_a_remote_controller(as_user, tmp_path):
    d = checkout(tmp_path)
    with pytest.raises(SystemExit, match="node A"):
        cli_kits.cmd_kit(args_for("start", api="https://spark-a.example:8443"), None)
    d.chmod(d.stat().st_mode | stat.S_IWOTH)
    with pytest.raises(SystemExit, match="writable by every user"):
        cli_kits.cmd_kit(args_for("start"), None)
    assert not as_user.calls                        # nothing was handed over


def test_the_scripts_never_run_as_root(monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("tsm kit runs on node A (Linux)")
    import pwd
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.delenv("SUDO_USER", raising=False)
    with pytest.raises(SystemExit, match="do not run as root"):
        cli_kits._user()
    me = pwd.getpwuid(os.getuid())
    if me.pw_name == "root":
        me = next((p for p in pwd.getpwall() if p.pw_uid >= 1000 and p.pw_name != "nobody"), None) or \
            pwd.getpwnam("nobody")
    monkeypatch.setenv("SUDO_USER", me.pw_name)
    seen = []
    monkeypatch.setattr(cli_kits.subprocess, "call", lambda argv, **kw: seen.append(argv) or 0)
    assert cli_kits._run(["./start.sh"], cwd=tmp_path, env={"HOST": "127.0.0.1"}) == 0
    assert seen[0][:4] == ["runuser", "-u", me.pw_name, "--"]
    assert f"HOME={me.pw_dir}" in seen[0] and "HOST=127.0.0.1" in seen[0] and seen[0][-1] == "./start.sh"
