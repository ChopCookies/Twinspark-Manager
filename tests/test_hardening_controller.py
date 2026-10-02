"""Regression tests for controller defects found by the activation / switching review.

Everything runs against the dry-run ``cluster`` fixture (two simulated agents, no Docker).
"""

from __future__ import annotations

import asyncio
import sqlite3
import textwrap

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from twinspark.controller.agent_client import AgentActionError
from twinspark.controller.autopilot import QuantizationAdvisor
from twinspark.controller.controller import BusyError
from twinspark.controller.jobs import ActivationCoordinator
from twinspark.controller.planner import MemoryPlanner, ModelSpec, make_spec_from_hf
from twinspark.controller.preparation import prepare
from twinspark.cookbook import import_eugr
from twinspark.gateway.app import build_gateway_app
from twinspark.schemas.enums import Quantization, Topology
from twinspark.schemas.job import Job
from twinspark.schemas.profile import ImmutableIdentity

from .conftest import DIGEST, SHA, draft
from .test_split_preparation import split_draft


def unreachable(agent, *only_actions):
    """Make ``agent`` behave like a powered-off node. Returns a switch to bring it back."""
    original = agent.call
    state = {"down": True}

    async def call(action, **kw):
        if state["down"] and (not only_actions or action in only_actions):
            raise AgentActionError(action, agent.node, "agent unreachable (ConnectError)")
        return await original(action, **kw)

    agent.call = call
    return state


# ---- an unrelated node being down must not take a healthy model down ----------------------------
async def test_switching_single_a_models_works_while_node_b_is_down(cluster):
    c = cluster.controller
    c.create_profile(draft("one", Topology.SINGLE_A, alias="one"))
    c.create_profile(draft("two", Topology.SINGLE_A, repo="org/model-b", alias="two"))
    await cluster.wait_job((await c.activate("one")).job_id)
    unreachable(c.agents["B"])
    job = await cluster.wait_job((await c.activate("two")).job_id)
    assert job.state.value == "completed", job.error
    assert c.active()["profile"] == "two"
    assert any("tsm-two" in name for name in cluster.runtimes["A"].containers)


async def test_a_two_node_model_still_fails_cleanly_when_b_is_down(cluster):
    c = cluster.controller
    c.create_profile(draft("big", Topology.TP2))
    unreachable(c.agents["B"])
    job = await cluster.wait_job((await c.activate("big")).job_id)
    assert job.state.value == "failed"
    assert not cluster.runtimes["A"].containers


async def test_stop_reports_a_node_it_could_not_reach(cluster):
    c = cluster.controller
    c.create_profile(draft("big", Topology.TP2))
    await cluster.wait_job((await c.activate("big")).job_id)
    unreachable(c.agents["B"], "containers_stop_owned")
    job = await c.stop()
    assert job.state.value == "failed" and "B" in (job.error or "")
    assert "may still be running" in job.error
    assert not cluster.runtimes["A"].containers          # the reachable node was stopped
    assert c.active() is None


# ---- aliases the new deployment does not use -------------------------------------------------------
async def test_alias_from_the_previous_model_is_released_after_a_switch(cluster):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.SINGLE_A, alias="chat"))
    c.create_profile(draft("coder", Topology.SINGLE_A, repo="org/model-b", alias="code"))
    await cluster.wait_job((await c.activate("qwen")).job_id)
    job = await cluster.wait_job((await c.activate("coder")).job_id)
    assert job.state.value == "completed"
    assert "chat" not in cluster.gateway.routes or not cluster.gateway.routes["chat"].draining
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(cluster.gateway)),
                                 base_url="http://gw", headers={"authorization": "Bearer client-key"}) as cl:
        listed = [m["id"] for m in (await cl.get("/v1/models")).json()["data"]]
        assert listed == ["code"]
        r = await cl.post("/v1/chat/completions", json={"model": "chat", "messages": []})
        assert r.status_code == 404 and "retry-after" not in r.headers


# ---- profile / operation lock ------------------------------------------------------------------------------
async def test_profile_being_activated_cannot_be_deleted(cluster):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.SINGLE_A))
    job = await c.activate("qwen")
    with pytest.raises(BusyError):
        c.delete_profile("qwen")
    await cluster.wait_job(job.job_id)
    assert c.get_profile("qwen") is not None
    assert c.active()["profile"] == "qwen"
    with pytest.raises(BusyError):
        c.delete_profile("qwen")                             # active: stop first
    await c.stop()
    assert c.delete_profile("qwen") is True


async def test_failing_to_save_the_prepare_job_does_not_wedge_the_controller(cluster):
    c = cluster.controller
    c.create_profile(draft("one", Topology.SINGLE_A))
    real = c.store.save_job
    calls = {"n": 0}

    def flaky(job):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database or disk is full")
        return real(job)

    c.store.save_job = flaky
    with pytest.raises(sqlite3.OperationalError):
        await prepare(c, "one")
    c.store.save_job = real
    assert not c.busy()
    await cluster.wait_job((await c.activate("one")).job_id)
    assert c.active()["profile"] == "one"


async def test_model_files_cannot_be_deleted_while_a_job_holds_the_lock(cluster):
    c = cluster.controller
    c.create_profile(draft("one", Topology.SINGLE_A))
    await c._lock.acquire()
    try:
        from twinspark.controller.files import FilesError
        with pytest.raises(FilesError, match="busy"):
            await c.delete_model_files("org/other", None, ["A"], force=True, preview=False)
    finally:
        c._lock.release()


# ---- watchdog ---------------------------------------------------------------------------------------------------
async def test_watchdog_does_not_burn_its_budget_while_a_node_is_away(cluster):
    c = cluster.controller
    c.create_profile(draft("big", Topology.TP2))
    await cluster.wait_job((await c.activate("big")).job_id)
    state = unreachable(c.agents["B"])
    cluster.runtimes["B"].containers.clear()                 # B rebooted: its container is gone
    for _ in range(5):
        assert "unreachable" in (await c.watchdog_tick() or "")
        await cluster.wait_idle()
    assert c.watch_state["recoveries"] == [], "recovery attempts that cannot succeed used up the budget"
    assert "waiting" in c.store.kv_get("last_incident")["action"]
    assert cluster.gateway.routes["default"].status == "down"
    audits = [e for e in c.store.audit_log(limit=100) if e.action == "deployment.incident"]
    assert len(audits) == 1, "the same outage must not be written to the audit log every tick"

    state["down"] = False                                    # B is back, container still gone
    assert await c.watchdog_tick() is not None
    await cluster.wait_idle()
    assert "restarting" in c.store.kv_get("last_incident")["action"]
    assert c.watch_state["recoveries"] and cluster.runtimes["B"].containers


async def test_watchdog_does_not_restart_what_the_operator_just_stopped(cluster):
    c = cluster.controller
    c.create_profile(draft("one", Topology.SINGLE_A))
    await cluster.wait_job((await c.activate("one")).job_id)
    gate, entered = asyncio.Event(), asyncio.Event()
    original = c.agents["A"].call

    async def slow(action, **kw):
        if action == "health_probe" and not gate.is_set():
            entered.set()
            await gate.wait()
            return {"status": "missing", "healthy": False, "exited": True, "exit_code": None, "log_tail": ""}
        return await original(action, **kw)

    c.agents["A"].call = slow
    tick = asyncio.create_task(c.watchdog_tick())
    await entered.wait()
    await c.stop()
    gate.set()
    assert await tick is None
    await cluster.wait_idle()
    assert c.active() is None and not cluster.runtimes["A"].containers


async def test_split_deployment_only_marks_the_failed_nodes_route_down(cluster):
    c = cluster.controller
    c.config.watchdog.auto_recover = False
    c.create_profile(split_draft())
    await cluster.wait_job((await c.activate("duo")).job_id)
    cluster.runtimes["B"].containers.clear()
    assert await c.watchdog_tick() is not None
    assert cluster.gateway.routes["chat"].status == "serving"
    assert cluster.gateway.routes["code"].status == "down"


# ---- boot ----------------------------------------------------------------------------------------------------------
async def test_boot_waits_for_the_other_node_to_come_up(cluster):
    c = cluster.controller
    state = unreachable(c.agents["B"], "hardware_facts")

    async def come_back():
        await asyncio.sleep(0.15)
        state["down"] = False

    asyncio.create_task(come_back())
    assert await c._wait_for_agents(5) == []


async def test_boot_wait_gives_up_after_the_timeout(cluster):
    c = cluster.controller
    unreachable(c.agents["B"], "hardware_facts")
    assert await c._wait_for_agents(0) == ["B"]


# ---- staging, disk, smoke test ---------------------------------------------------------------------------------
def weights_only_on(cluster, node):
    for n, agent in cluster.controller.agents.items():
        original = agent.call

        async def call(action, _o=original, _n=n, **kw):
            if action == "weights_present":
                return {"present": _n == node, "path": "x"}
            return await _o(action, **kw)

        agent.call = call


async def test_weights_that_exist_only_on_b_are_downloaded_on_a_when_there_is_no_sync_path(cluster):
    c = cluster.controller
    assert c.config.nodes["A"].ssh_user is None              # what `tsm setup` writes: A -> B only
    weights_only_on(cluster, "B")
    downloads = []
    original = c.agents["A"].call

    async def spy(action, **kw):
        if action == "download":
            downloads.append(kw["repo"])
        if action == "weights_present":
            return {"present": bool(downloads), "path": "x"}
        return await original(action, **kw)

    c.agents["A"].call = spy
    c.create_profile(draft("ona", Topology.SINGLE_A))
    job = await cluster.wait_job((await c.activate("ona")).job_id)
    assert job.state.value == "completed", job.error
    assert downloads == ["org/model-a"]


async def test_disk_space_is_requested_only_for_weights_still_missing(cluster):
    c = cluster.controller
    c.create_profile(draft("big", Topology.TP2))
    rev = c.get_profile("big").latest()
    coord = ActivationCoordinator(
        config=c.config, agents=c.agents, gateway=c.gateway, planner=c.planner, persist=lambda j: None,
        model_specs={"org/model-a": ModelSpec(num_params=0, layers=4, num_kv_heads=8, head_dim=64,
                                              weight_bytes=100 * 1024 ** 3)})
    coord.plan = c.launch_plan(rev)
    weights_only_on(cluster, "B")
    assert await coord._disk_needed_gib("A", rev) == int(100 * 1.05) + 2
    assert await coord._disk_needed_gib("B", rev) == 0       # already there: nothing to reserve


async def serve(routes):
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=routes), host="127.0.0.1", port=0, log_level="critical"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    return server, task, server.servers[0].sockets[0].getsockname()[1]


async def test_smoke_test_accepts_a_pooling_model_without_completions(cluster):
    c = cluster.controller
    c.create_profile(draft("embed", Topology.SINGLE_A, repo="org/embedder"))
    rev = c.get_profile("embed").latest()

    async def completions(request):
        return JSONResponse({"object": "error", "message": "The model does not support Completions API",
                             "code": 404}, status_code=404)

    async def models(request):
        return JSONResponse({"data": [{"id": "embed"}]})

    server, task, port = await serve([Route("/v1/completions", completions, methods=["POST"]),
                                      Route("/v1/models", models, methods=["GET"])])
    try:
        coord = ActivationCoordinator(config=c.config, agents=c.agents, gateway=c.gateway, planner=c.planner,
                                      persist=lambda j: None)
        coord.plan = c.launch_plan(rev)
        coord.plan.routes = {"default": [f"http://127.0.0.1:{port}"]}
        coord.plan.route_models = {"default": "embed"}
        coord.dry_run = False
        job = Job(job_id="t")
        message = await coord._smoke_test(job, rev, job.begin_step("testing"))
        assert "model listed" in message
        coord.plan.route_models = {"default": "something-else"}          # not listed: still a failure
        with pytest.raises(RuntimeError, match="not listed"):
            await coord._smoke_test(job, rev, job.begin_step("testing"))
    finally:
        server.should_exit = True
        await task


# ---- recipes and advisors ---------------------------------------------------------------------------------------
RECIPE = textwrap.dedent("""
    name: demo-recipe
    model: org/some-model
    container: vllm-node
    command: |
      vllm serve org/some-model --max-model-len 32768 --gpu-memory-utilization 0.8 {extra}
""")


def imported(cluster, extra=""):
    result = import_eugr(RECIPE.format(extra=extra))
    d = result[0] if isinstance(result, tuple) else result
    d.identity = ImmutableIdentity(model_repo=d.simple.model, model_revision=SHA, quantization=d.simple.quantization,
                                   image="nvcr.io/nvidia/vllm", image_digest=DIGEST)
    cluster.controller.create_profile(d)
    plan = cluster.controller.launch_plan(cluster.controller.get_profile(d.name).latest())
    return next(x for x in plan.containers if x.health_url).command


def test_recipe_without_max_num_seqs_keeps_vllms_default(cluster):
    assert "--max-num-seqs" not in imported(cluster)


def test_recipe_with_max_num_seqs_keeps_its_value(cluster):
    cmd = imported(cluster, "--max-num-seqs 64")
    assert cmd[cmd.index("--max-num-seqs") + 1] == "64"


def test_quantization_variants_of_a_hub_derived_spec_have_weights():
    spec = make_spec_from_hf({"hidden_size": 8192, "num_attention_heads": 64, "num_hidden_layers": 80,
                              "num_key_value_heads": 8}, weight_bytes=40 * 1024 ** 3)
    assert spec.num_params == 0
    rows = QuantizationAdvisor(MemoryPlanner(), spec, Topology.SINGLE_A,
                               base_quant=Quantization.NVFP4).variants(32768, 4)
    by_quant = {r.quant: r.weight_gib for r in rows}
    assert all(w > 1 for w in by_quant.values())
    assert by_quant[Quantization.BF16] > by_quant[Quantization.FP8] > by_quant[Quantization.NVFP4]
    assert 38 < by_quant[Quantization.NVFP4] < 42            # the size we started from


def test_advisory_estimates_use_the_real_memory_total(cluster):
    c = cluster.controller
    spec = ModelSpec(num_params=7 * 10 ** 9, layers=32, num_kv_heads=8, head_dim=128)
    nominal = c.planner.estimate(spec=spec, quant=Quantization.FP8, context_length=4096, concurrency=1,
                                 topology=Topology.SINGLE_A)
    c.store.kv_set("hardware:A", {"mem_total_gib": 121.69})
    c.store.kv_set("hardware:B", {"mem_total_gib": 121.69})
    real = c.planner.estimate(spec=spec, quant=Quantization.FP8, context_length=4096, concurrency=1,
                              topology=Topology.SINGLE_A)
    assert nominal.mem_total == 128 and real.mem_total == pytest.approx(121.69)
    assert real.headroom_gib() < nominal.headroom_gib()
