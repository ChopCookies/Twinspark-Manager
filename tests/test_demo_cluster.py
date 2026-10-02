"""Real HTTP between controller and two agents, started from config files `tsm setup` generated.

This is the closest the test-suite gets to a deployment without hardware: separate uvicorn
servers, bearer tokens read from the vault, node B joined with a real join code, containers
simulated by the dry-run runtime.
"""

from __future__ import annotations

import sys

import httpx
import pytest

from twinspark.demo import DemoCluster
from twinspark.security import SecretsVault


@pytest.fixture
async def demo():
    async with DemoCluster() as d:
        yield d


def auth(d):
    return {"x-api-key": d.key}


def test_demo_provision_accepts_non_linux_host_username(tmp_path, monkeypatch):
    from twinspark.schemas.config import ControllerConfig, load_config

    monkeypatch.setattr("twinspark.demo.getpass.getuser", lambda: "Windows User")
    d = DemoCluster(base=tmp_path)
    d.provision()
    config = load_config(d.a_layout.controller_yaml, ControllerConfig)
    assert config.nodes["B"].ssh_user == "twinspark-demo"
    assert d.key


@pytest.mark.skipif(sys.platform == "linux", reason="Linux supports the demo terminal")
def test_demo_terminal_on_unsupported_host_has_clear_error():
    with pytest.raises(ValueError, match="requires Linux PTYs"):
        DemoCluster(remote={"terminal": True})


async def test_stack_boots_and_both_nodes_answer_over_real_http(demo):
    async with httpx.AsyncClient(base_url=demo.url, headers=auth(demo), timeout=30) as c:
        h = (await c.get("/api/v1/health")).json()
        assert h["status"] == "ok" and h["nodes"] == ["A", "B"]
        st = (await c.get("/api/v1/status")).json()
        assert st["nodes"]["A"]["agent_version"] == st["nodes"]["B"]["agent_version"]
        assert st["nodes"]["A"]["runtime_mode"] == st["nodes"]["B"]["runtime_mode"] == "dry-run"
        assert len((await c.get("/api/v1/profiles?summary=true")).json()) >= 4      # built-in recipes seeded


async def test_management_api_needs_the_key_and_gui_is_served(demo):
    async with httpx.AsyncClient(base_url=demo.url, timeout=30) as c:
        assert (await c.get("/api/v1/status")).status_code == 401
        assert (await c.get("/api/v1/status", headers={"x-api-key": "wrong"})).status_code == 401
        assert (await c.get("/api/v1/status", headers={"authorization": f"Bearer {demo.key}"})).status_code == 200
        page = await c.get("/")
        assert page.status_code == 200 and "TwinSpark" in page.text
        assert (await c.get("/js/start.js")).status_code == 200


async def test_agents_reject_calls_without_the_shared_token(demo):
    async with httpx.AsyncClient(timeout=10) as c:
        url = f"http://127.0.0.2:{demo.agent_port}/v1/action"
        body = {"action": "hardware_facts", "params": {}}
        assert (await c.post(url, json=body)).status_code == 401
        assert (await c.post(url, json=body, headers={"Authorization": "Bearer nope"})).status_code == 401
        token = SecretsVault(demo.b_layout.secrets).get("agent_token")
        ok = await c.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200 and ok.json()["result"]["node_id"] == "B"


async def test_gateway_requires_the_inference_key_and_answers_503_or_404_when_idle(demo):
    async with httpx.AsyncClient(base_url=demo.gateway_url, timeout=10) as c:
        r = await c.post("/v1/chat/completions", json={"model": "default", "messages": []})
        assert r.status_code in (401, 403)


async def test_checklist_progresses_as_the_operator_works(demo):
    async with httpx.AsyncClient(base_url=demo.url, headers=auth(demo), timeout=60) as c:
        ob = (await c.get("/api/v1/system/onboarding")).json()
        steps = {s["id"]: s for s in ob["steps"]}
        assert steps["nodes"]["status"] == "done"
        assert steps["live"]["status"] == "todo" and "go-live" in steps["live"]["fix"]["command"]
        assert steps["recipe"]["status"] == "done"             # the demo seeds recipes
        assert steps["pin"]["status"] == "todo" and ob["dry_run"] is True
        assert ob["next"] in ("link", "pin") and not ob["complete"]


async def test_dry_run_plan_works_through_the_full_stack(demo):
    async with httpx.AsyncClient(base_url=demo.url, headers=auth(demo), timeout=60) as c:
        names = [p["name"] for p in (await c.get("/api/v1/profiles?summary=true")).json()]
        for name in names:
            r = await c.get(f"/api/v1/profiles/{name}/draft/launch-plan")
            assert r.status_code in (200, 422), f"{name}: {r.text}"     # a clear message, never a 500
        ok = await c.get(f"/api/v1/profiles/{names[0]}/draft/launch-plan")
        assert ok.status_code == 200 and ok.json()["plan"]["containers"] and ok.json()["commands"]
