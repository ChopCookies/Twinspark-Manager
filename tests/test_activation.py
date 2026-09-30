import asyncio

import httpx
import pytest

from twinspark.controller.controller import BusyError
from twinspark.controller.planner import ModelSpec
from twinspark.schemas.enums import Topology

from .conftest import draft


async def test_tp2_activation_end_to_end(cluster):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.TP2))
    job = await c.activate("qwen")
    job = await cluster.wait_job(job.job_id)
    assert job.state.value == "completed", (job.error, [s.message for s in job.steps])
    assert [s.status for s in job.steps] == ["ok"] * len(job.steps)
    assert job.steps[-1].stage == "healthy"
    # one container per node, both owned
    assert len(cluster.runtimes["A"].containers) == 1 and len(cluster.runtimes["B"].containers) == 1
    started = cluster.runtimes["B"].history[-1]
    assert "--headless" in started and "vllm" in started
    # gateway now routes the alias to the head node
    st = cluster.gateway.routes["default"]
    assert st.backends == ["http://127.0.0.1:18100"] and st.served_model == "qwen"
    assert c.active()["profile"] == "qwen"
    assert c.get_profile("qwen").latest().known_good
    assert job.payload["dry_run"] is True and "_revision" not in job.payload


async def test_switch_stops_previous_model_first(cluster):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.TP2))
    c.create_profile(draft("mistral", Topology.SINGLE_A, repo="org/model-b"))
    await cluster.wait_job((await c.activate("qwen")).job_id)
    job = await cluster.wait_job((await c.activate("mistral")).job_id)
    assert job.state.value == "completed"
    assert list(cluster.runtimes["B"].containers) == []          # tp2 worker gone
    (name,) = cluster.runtimes["A"].containers
    assert name.startswith("tsm-mistral-a-")
    assert cluster.gateway.routes["default"].served_model == "mistral"


async def test_crash_during_loading_rolls_back_to_previous(cluster):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.SINGLE_A))
    c.create_profile(draft("glm", Topology.TP2, repo="org/model-c"))
    await cluster.wait_job((await c.activate("qwen")).job_id)

    glm_plan = c.launch_plan(c.get_profile("glm").latest())
    cluster.runtimes["B"].fail_start.add(glm_plan.containers[1].name)   # worker "OOMs"

    job = await cluster.wait_job((await c.activate("glm")).job_id)
    assert job.state.value == "failed"
    assert job.steps[-1].stage == "loading"
    assert "out of memory" in (job.steps[-1].log_excerpt or "").lower()
    assert any("context" in g for g in job.guidance)
    assert "restoring previous qwen" in job.rollback

    await cluster.wait_idle()
    assert c.active()["profile"] == "qwen"                             # rollback succeeded
    assert cluster.gateway.routes["default"].served_model == "qwen"
    assert list(cluster.runtimes["B"].containers) == []                # partial worker removed


async def test_failure_before_stop_keeps_old_model_serving(cluster):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.SINGLE_A))
    c.create_profile(draft("huge", Topology.SINGLE_A, repo="org/huge"))
    c.set_model_spec("org/huge", ModelSpec(num_params=400 * 10**9, layers=80, num_kv_heads=8,
                                           head_dim=128))
    await cluster.wait_job((await c.activate("qwen")).job_id)
    before = dict(cluster.runtimes["A"].containers)

    job = await cluster.wait_job((await c.activate("huge")).job_id)
    assert job.state.value == "failed" and job.steps[-1].stage == "validating"
    assert "not to fit" in job.error
    assert cluster.runtimes["A"].containers == before                  # untouched
    st = cluster.gateway.routes["default"]
    assert st.served_model == "qwen" and not st.draining


async def test_second_activation_is_rejected_while_busy(cluster):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.SINGLE_A))
    job = await c.activate("qwen")
    with pytest.raises(BusyError):
        await c.activate("qwen")
    await cluster.wait_job(job.job_id)


async def test_preflight_detects_foreign_process_on_vllm_port(cluster):
    """The real situation next to an existing hand-started vLLM."""
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1",
                                        cluster.controller.config.runtime.vllm_port)
    try:
        c = cluster.controller
        c.create_profile(draft("qwen", Topology.SINGLE_A))
        job = await cluster.wait_job((await c.activate("qwen")).job_id)
        assert job.state.value == "failed" and job.steps[-1].stage == "resolving"
        assert "already in use" in job.error
        assert cluster.runtimes["A"].history == []                     # nothing was started
    finally:
        server.close()


async def test_stop_and_restart_adoption(cluster):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.TP2))
    await cluster.wait_job((await c.activate("qwen")).job_id)

    # controller restarts while containers keep running -> routes re-attached, no restart
    cluster.gateway.routes.clear()
    history_before = len(cluster.runtimes["A"].history)
    assert await c.on_startup() is None
    assert cluster.gateway.routes["default"].served_model == "qwen"
    assert len(cluster.runtimes["A"].history) == history_before

    job = await c.stop()
    assert job.state.value == "completed"
    assert cluster.runtimes["A"].containers == {} and cluster.runtimes["B"].containers == {}
    assert c.active() is None
    assert not cluster.gateway.routes["default"].backends


async def test_agent_rejects_bad_token_and_foreign_mounts(cluster, tmp_path):
    agent = cluster.controller.agents["A"]
    bad = httpx.AsyncClient(transport=agent._client._transport, base_url="http://agent-a")
    r = await bad.post("/v1/action", json={"action": "hardware_facts"},
                       headers={"authorization": "Bearer wrong"})
    assert r.status_code == 401

    c = cluster.controller
    c.create_profile(draft("qwen", Topology.SINGLE_A))
    spec = c.launch_plan(c.get_profile("qwen").latest()).containers[0]
    from twinspark.controller.launch import Mount
    evil = spec.model_copy(update={"mounts": [Mount(host="/etc", container="/x")]})
    from twinspark.controller.agent_client import AgentActionError
    with pytest.raises(AgentActionError, match="outside allowed roots"):
        await agent.call("container_start", spec=evil.model_dump(mode="json"))
    wrong_node = spec.model_copy(update={"node": "B"})
    with pytest.raises(AgentActionError, match="spec is for node"):
        await agent.call("container_start", spec=wrong_node.model_dump(mode="json"))
