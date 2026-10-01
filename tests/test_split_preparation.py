import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from twinspark.controller import pinning
from twinspark.controller.app import create_app
from twinspark.controller.controller import BusyError
from twinspark.controller.files import FilesError, _profile_refs
from twinspark.controller.jobs import ActivationCoordinator
from twinspark.controller.planner import ModelSpec
from twinspark.controller.preparation import prepare
from twinspark.gateway.app import build_gateway_app
from twinspark.schemas.enums import Topology
from twinspark.schemas.job import Job
from twinspark.schemas.profile import ProfileDraft

from .conftest import DIGEST, SHA, draft


def split_draft():
    a = draft("duo", Topology.SINGLE_A, alias="chat").model_dump(mode="json")
    b = draft("coder", Topology.SINGLE_B, repo="org/model-b", alias="code")
    b.identity.image = "ghcr.io/org/coder"
    b.identity.image_digest = "sha256:" + "c" * 64
    b.advanced.gpu_memory_utilization = 0.6
    a["simple"]["topology"] = "split"
    a["advanced"]["gpu_memory_utilization"] = 0.8
    a["secondary"] = b.model_dump(mode="json")
    return ProfileDraft.model_validate(a)


def test_split_requires_separate_aliases_and_no_nested_deployments():
    d = split_draft().model_dump(mode="json")
    d["secondary"]["simple"]["extra_aliases"] = ["chat"]
    with pytest.raises(ValidationError, match="different API aliases"):
        ProfileDraft.model_validate(d)
    d = split_draft().model_dump(mode="json")
    d["secondary"]["simple"]["topology"] = "tp2"
    with pytest.raises(ValidationError, match="single-b"):
        ProfileDraft.model_validate(d)


async def test_split_activation_routes_models_and_stages_only_required_nodes(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    queries = []
    for node, agent in c.agents.items():
        original = agent.call

        async def call(action, _node=node, _call=original, **kw):
            if action == "weights_present":
                queries.append((_node, kw["repo"]))
            return await _call(action, **kw)

        agent.call = call
    job = await cluster.wait_job((await c.activate("duo")).job_id)
    assert job.state.value == "completed", job.error
    assert set(queries) == {("A", "org/model-a"), ("B", "org/model-b")}
    plan = c.launch_plan(c.get_profile("duo").latest())
    assert plan.node_utilization == {"A": 0.8, "B": 0.6}
    assert [s.image_ref for s in plan.containers] == ["nvcr.io/nvidia/vllm@" + DIGEST,
                                                     "ghcr.io/org/coder@sha256:" + "c" * 64]
    assert c.gateway.routes["chat"].served_model == "duo-a"
    assert c.gateway.routes["code"].served_model == "duo-b"
    assert c.active()["aliases"] == ["chat", "code"]
    assert set(job.payload["models"]) == {"duo-a", "duo-b"}
    with pytest.raises(FilesError, match="active deployment"):
        await c.delete_model_files("org/model-b", SHA, ["B"], force=True)
    assert _profile_refs(c)[("org/model-b", SHA)] == ["duo"]
    c.store.kv_set(f"observed:{plan.revision_id}", {"models": {
        "duo-a": {"max_model_len": 8192}, "duo-b": {"max_model_len": 16384}}})
    c.gateway.routes.clear()
    assert await c._adopt_running(c.active())
    assert c.gateway.routes["chat"].max_model_len == 8192
    assert c.gateway.routes["code"].max_model_len == 16384


async def test_split_node_b_failure_restores_previous_deployment(cluster):
    c = cluster.controller
    c.create_profile(draft("baseline", Topology.SINGLE_A))
    await cluster.wait_job((await c.activate("baseline")).job_id)
    c.create_profile(split_draft())
    plan = c.launch_plan(c.get_profile("duo").latest())
    cluster.runtimes["B"].fail_start.add(plan.containers[1].name)
    job = await cluster.wait_job((await c.activate("duo")).job_id)
    assert job.state.value == "failed" and "node B" in job.error
    await cluster.wait_idle()
    assert c.active()["profile"] == "baseline"
    assert c.gateway.routes["default"].served_model == "baseline"
    assert cluster.runtimes["B"].containers == {}


async def test_split_checks_memory_of_second_model_before_stopping(cluster):
    c = cluster.controller
    c.create_profile(draft("baseline", Topology.SINGLE_A))
    await cluster.wait_job((await c.activate("baseline")).job_id)
    c.create_profile(split_draft())
    c.set_model_spec("org/model-b", ModelSpec(num_params=500 * 10**9, layers=80,
                                             num_kv_heads=8, head_dim=128))
    job = await cluster.wait_job((await c.activate("duo")).job_id)
    assert job.state.value == "failed" and "duo-b: estimated not to fit" in job.error
    assert c.active()["profile"] == "baseline"
    assert set(c.profile_fit("duo")["nodes"]) == {"A", "B"}


async def test_prepare_checks_everything_without_switching(cluster):
    c = cluster.controller
    c.create_profile(draft("baseline", Topology.SINGLE_A))
    await cluster.wait_job((await c.activate("baseline")).job_id)
    c.create_profile(split_draft())
    before = {n: list(rt.history) for n, rt in cluster.runtimes.items()}
    job = await prepare(c, "duo")
    with pytest.raises(BusyError):
        await c.activate("duo")
    with pytest.raises(FilesError, match="busy"):
        await c.delete_model_files("org/model-b", SHA, ["B"], force=True)
    job = await cluster.wait_job(job.job_id)
    assert job.state.value == "completed", job.error
    assert job.payload["ready"] and job.payload["dry_run"]
    assert [s.stage for s in job.steps] == ["validating", "resolving", "downloading", "syncing"]
    assert c.active()["profile"] == "baseline"
    assert not c.gateway.routes["default"].draining
    assert {n: rt.history for n, rt in cluster.runtimes.items()} == before
    assert not c.get_profile("duo").latest().known_good


async def test_prepare_missing_mod_keeps_baseline_serving(cluster):
    c = cluster.controller
    c.create_profile(draft("baseline", Topology.SINGLE_A))
    await cluster.wait_job((await c.activate("baseline")).job_id)
    d = split_draft()
    d.secondary.advanced.mods = ["missing-patch"]
    c.create_profile(d)
    job = await cluster.wait_job((await prepare(c, "duo")).job_id)
    assert job.state.value == "failed" and "node B preflight" in job.error
    assert c.active()["profile"] == "baseline"


async def test_prepare_cancellation_releases_operation_lock(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    job = await prepare(c, "duo")
    c.cancel_job(job.job_id)
    result = await cluster.wait_job(job.job_id)
    assert result.state.value == "failed" and "cancelled" in result.error
    assert not c.busy() and c.active() is None
    assert all(not rt.history for rt in cluster.runtimes.values())


async def test_mixed_real_and_simulated_nodes_are_rejected(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    original = c.agents["B"].call

    async def call(action, **kw):
        result = await original(action, **kw)
        if action == "hardware_facts":
            result["runtime_mode"] = "docker"
        return result

    c.agents["B"].call = call
    result = await cluster.wait_job((await prepare(c, "duo")).job_id)
    assert result.state.value == "failed" and "mix simulated and real" in result.error
    assert all(not rt.history for rt in cluster.runtimes.values())


async def test_odd_attention_heads_fail_tp2_but_allow_pp2(cluster):
    c = cluster.controller
    c.create_profile(draft("odd", Topology.TP2))
    c.set_model_spec("org/model-a", ModelSpec(num_params=135000000, layers=30,
                                             num_kv_heads=3, head_dim=64, num_attention_heads=9))
    result = await cluster.wait_job((await c.activate("odd")).job_id)
    assert result.state.value == "failed" and "use pp2" in result.error
    assert all(not rt.history for rt in cluster.runtimes.values())
    c.create_profile(draft("pipeline", Topology.PP2))
    assert (await cluster.wait_job((await c.activate("pipeline")).job_id)).state.value == "completed"


async def test_compose_snapshots_and_unpinned_plan_api(cluster):
    c = cluster.controller
    a, b = draft("first", Topology.TP2), draft("second", Topology.TP2, repo="org/model-b")
    a.identity = b.identity = None
    c.create_profile(a)
    c.create_profile(b)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(c, management_key="test-key")),
                              base_url="http://controller",
                              headers={"authorization": "Bearer test-key"})
    response = await client.post("/api/v1/profiles/compose/split", json={
        "name": "duo", "node_a": "first", "node_b": "second"})
    assert response.status_code == 201, response.text
    b.simple.context_length = 65536
    c.save_draft("second", b)
    assert c.get_profile("duo").draft.secondary.simple.context_length == 32768
    plan = await client.get("/api/v1/profiles/duo/draft/launch-plan")
    assert plan.status_code == 200 and plan.json()["unpinned"]
    assert len(plan.json()["plan"]["containers"]) == 2
    assert (await client.post("/api/v1/profiles/duo/prepare")).status_code == 422
    await client.aclose()


async def test_split_pinning_is_atomic_when_second_image_is_missing(cluster, monkeypatch):
    c = cluster.controller
    d = split_draft()
    d.identity = d.secondary.identity = None
    d.image_hint, d.secondary.image_hint = "image-a", "image-b"
    c.create_profile(d)
    monkeypatch.setattr(pinning, "resolve_hf_revision", lambda ref, token: {
        "repo": ref.partition("@")[0], "revision": SHA, "config": {
            "num_hidden_layers": 4, "num_attention_heads": 8, "num_key_value_heads": 2,
            "head_dim": 64, "hidden_size": 512}, "weight_bytes": 1024**3})
    original = cluster.runtimes["B"].image_inspect
    cluster.runtimes["B"].image_inspect = lambda ref: {"present": False}
    with pytest.raises(pinning.PinError, match="node B"):
        await c.pin_profile("duo")
    assert c.get_profile("duo").revisions == []
    assert c.get_profile("duo").draft.identity is None
    cluster.runtimes["B"].image_inspect = original
    result = await c.pin_profile("duo")
    assert result["resolved"]["secondary"]["model"] == "org/model-b@" + SHA
    assert c.get_profile("duo").latest().draft.secondary.identity.model_revision == SHA
    await c.pin_profile("duo")
    assert len(c.get_profile("duo").revisions) == 1


async def test_split_smoke_test_uses_correct_backend_model(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(split_draft())
    rev = c.get_profile("duo").latest()
    coord = ActivationCoordinator(config=c.config, agents=c.agents, gateway=c.gateway,
                                  planner=c.planner, persist=lambda j: None)
    coord.plan = c.launch_plan(rev)
    seen = []

    def handler(request):
        import json
        model = "duo-a" if request.url.host == "127.0.0.1" else "duo-b"
        if request.method == "POST":
            assert json.loads(request.content)["model"] == model
            seen.append(model)
            return httpx.Response(200, json={"choices": [{"text": "Hello"}]})
        return httpx.Response(200, json={"data": [{"id": model, "max_model_len":
                                                    8192 if model == "duo-a" else 16384}]})

    client_type = httpx.AsyncClient
    monkeypatch.setattr("twinspark.controller.jobs.httpx.AsyncClient",
                        lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
    job = Job(job_id="test")
    await coord._smoke_test(job, rev, job.begin_step("testing"))
    await coord._route(job, rev, job.begin_step("routing"))
    assert sorted(seen) == ["duo-a", "duo-b"]
    assert c.gateway.routes["chat"].max_model_len == 8192
    assert c.gateway.routes["code"].max_model_len == 16384


async def test_split_gateway_proxies_normal_and_streaming_requests_to_both_models(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    await cluster.wait_job((await c.activate("duo")).job_id)
    seen = []

    async def upstream(request):
        body = await request.json()
        model = "duo-a" if request.url.hostname == "127.0.0.1" else "duo-b"
        assert body["model"] == model
        assert request.headers["authorization"] == "Bearer backend-key"
        seen.append(model)
        if body.get("stream"):
            async def chunks():
                yield b'data: {"choices":[]}\n\n'
                yield b"data: [DONE]\n\n"
            return StreamingResponse(chunks(), media_type="text/event-stream")
        return JSONResponse({"model": model, "choices": [{"message": {"content": "Hi"}}]})

    await c.gateway._client.aclose()
    c.gateway._client = httpx.AsyncClient(transport=httpx.ASGITransport(
        app=Starlette(routes=[Route("/v1/chat/completions", upstream, methods=["POST"])])))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(c.gateway)),
                                 base_url="http://gateway",
                                 headers={"authorization": "Bearer client-key"}) as client:
        for alias, model in (("chat", "duo-a"), ("code", "duo-b")):
            for streaming in (False, True):
                response = await client.post("/v1/chat/completions", json={
                    "model": alias, "messages": [{"role": "user", "content": "Hi"}], "stream": streaming})
                assert response.status_code == 200
                if streaming:
                    assert "data: [DONE]" in response.text
                else:
                    assert response.json()["model"] == model
    assert seen == ["duo-a", "duo-a", "duo-b", "duo-b"]


@pytest.mark.parametrize("topology", [Topology.TP2, Topology.PP2, Topology.TP_EP])
async def test_distributed_modes_start_worker_first_with_explicit_mp(cluster, topology):
    c = cluster.controller
    c.create_profile(draft("distributed", topology))
    job = await cluster.wait_job((await c.activate("distributed")).job_id)
    assert job.state.value == "completed", job.error
    plan = c.launch_plan(c.get_profile("distributed").latest())
    assert plan.start_order[0] == [plan.containers[1].name]
    for container in plan.containers:
        argv = container.command
        assert argv[argv.index("--distributed-executor-backend") + 1] == "mp"
        assert argv[argv.index("--pipeline-parallel-size") + 1] == ("2" if topology == Topology.PP2 else "1")
