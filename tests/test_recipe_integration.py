import asyncio

import httpx
import pytest

from twinspark.controller.app import create_app
from twinspark.controller.controller import BusyError
from twinspark.controller.integration import IntegrationRequest, RetryRequest, integrate, retry
from twinspark.controller.pinning import PinError
from twinspark.schemas.enums import Topology

from .conftest import draft
from .test_split_preparation import split_draft


def request(recipe=None, key="test-request-001", **kwargs):
    return IntegrationRequest(draft=recipe or split_draft(), request_id=key, **kwargs)


async def test_integration_prepares_split_snapshot_without_touching_active_deployment(cluster):
    c = cluster.controller
    c.create_profile(draft("baseline", Topology.SINGLE_A))
    await cluster.wait_job((await c.activate("baseline")).job_id)
    before = {n: list(rt.history) for n, rt in cluster.runtimes.items()}
    req = request()
    job = await integrate(c, req)
    req.draft.simple.context_length = 1234  # input ownership must not leak into the saved job
    job = await cluster.wait_job(job.job_id)
    assert job.state.value == "completed", job.error
    assert [s.stage for s in job.steps] == ["pinning", "validating", "resolving", "downloading", "syncing"]
    assert job.payload["ready"] and job.payload["dry_run"]
    assert job.profile_revision == c.get_profile("duo").latest().revision_id
    assert job.payload["snapshot"]["simple"]["context_length"] == 32768
    assert job.payload["resolved"]["secondary"]["model"].startswith("org/model-b@")
    assert c.active()["profile"] == "baseline"
    assert {n: rt.history for n, rt in cluster.runtimes.items()} == before
    assert not c.gateway.routes["default"].draining
    assert "_revision" not in job.payload


@pytest.mark.parametrize("topology", [Topology.SINGLE_A, Topology.SINGLE_B, Topology.TP2, Topology.PP2])
async def test_integration_handles_single_and_distributed_recipes(cluster, topology):
    job = await cluster.wait_job((await integrate(cluster.controller, request(draft(topology=topology)))).job_id)
    assert job.state.value == "completed", job.error
    assert job.payload["ready"]
    assert cluster.controller.active() is None


async def test_idempotency_survives_completion_and_rejects_changed_inputs(cluster):
    c = cluster.controller
    req = request()
    job = await integrate(c, req)
    same = await integrate(c, req)
    assert same.job_id == job.job_id
    await cluster.wait_job(job.job_id)
    same = await integrate(c, req)
    assert same.state.value == "completed"
    assert len(c.store.list_jobs()) == 1
    assert len(c.get_profile("duo").revisions) == 1
    req.draft.description = "another recipe"
    with pytest.raises(ValueError, match="different recipe settings"):
        await integrate(c, req)


async def test_busy_and_missing_nodes_do_not_create_a_profile(cluster):
    c = cluster.controller
    await c._lock.acquire()
    with pytest.raises(BusyError):
        await integrate(c, request())
    assert c.get_profile("duo") is None
    c._lock.release()
    c.agents.pop("B")
    with pytest.raises(ValueError, match="nodes not configured: B"):
        await integrate(c, request())
    assert c.get_profile("duo") is None


async def test_existing_recipe_requires_exact_reviewed_draft(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    stale = request(existing=True)
    edited = split_draft()
    edited.description = "edited after review"
    c.save_draft("duo", edited)
    with pytest.raises(ValueError, match="changed since review"):
        await integrate(c, stale)
    assert c.get_profile("duo").draft.description == "edited after review"
    assert not c.busy()


async def test_cancel_during_pinning_does_not_commit_or_prepare(cluster, monkeypatch):
    from twinspark.controller import pinning
    c = cluster.controller
    d = split_draft()
    d.identity = d.secondary.identity = None
    entered, release = asyncio.Event(), asyncio.Event()

    async def fake_pin(ctrl, part, *args, **kwargs):
        entered.set()
        await release.wait()
        pinned = part.model_copy(update={"identity": draft(repo=part.simple.model).identity}, deep=True)
        return pinned, {}, []

    monkeypatch.setattr(pinning, "_pin_draft", fake_pin)
    image = "ghcr.io/example/vllm:sm121"
    job = await integrate(c, request(d, pins={"image": image, "secondary": {"image": image}}))
    await entered.wait()
    c.cancel_job(job.job_id)
    release.set()
    job = await cluster.wait_job(job.job_id)
    assert job.state.value == "failed" and "cancelled" in job.error
    assert not c.get_profile("duo").revisions
    assert [s.stage for s in job.steps] == ["pinning"]


async def test_failed_preflight_retry_reuses_exact_revision_and_retains_history(cluster, monkeypatch):
    c = cluster.controller
    agent = c.agents["B"]
    original = agent.call
    fail = True

    async def call(action, **kw):
        if action == "preflight" and fail:
            raise RuntimeError("node B temporarily unavailable")
        return await original(action, **kw)

    agent.call = call
    job = await cluster.wait_job((await integrate(c, request())).job_id)
    assert job.state.value == "failed"
    assert job.profile_revision
    fail = False

    async def no_repin(*a, **kw):
        raise AssertionError("retry must not resolve floating refs again")

    monkeypatch.setattr(c, "pin_profile", no_repin)
    req = RetryRequest(request_id="retry-request-001")
    retried = await retry(c, job.job_id, req)
    assert (await retry(c, job.job_id, req)).job_id == retried.job_id
    retried = await cluster.wait_job(retried.job_id)
    assert retried.state.value == "completed", retried.error
    assert retried.profile_revision == job.profile_revision
    assert retried.payload["retry_of"] == job.job_id
    assert c.store.load_job(job.job_id).state.value == "failed"
    assert len(c.get_profile("duo").revisions) == 1


async def test_retry_after_profile_edit_is_rejected(cluster, monkeypatch):
    c = cluster.controller

    async def fail(*a, **kw):
        raise PinError("Hub unavailable")

    monkeypatch.setattr(c, "pin_profile", fail)
    job = await cluster.wait_job((await integrate(c, request())).job_id)
    d = c.get_profile("duo").working_draft()
    d.simple.context_length = 4096
    c.save_draft("duo", d)
    with pytest.raises(ValueError, match="changed since review"):
        await retry(c, job.job_id, RetryRequest(request_id="retry-edited-001"))
    assert not c.busy()


async def test_second_model_pin_failure_is_atomic_and_retryable(cluster, monkeypatch):
    from twinspark.controller import pinning
    c = cluster.controller
    d = split_draft()
    d.identity = d.secondary.identity = None
    fail = True
    seen = []

    async def fake_pin(ctrl, part, *args, **kw):
        seen.append((part.simple.model, kw))
        if part.simple.model == "org/model-b" and fail:
            raise PinError("node B image missing")
        part.identity = draft(repo=part.simple.model).identity
        return part.model_copy(deep=True), {}, []

    monkeypatch.setattr(pinning, "_pin_draft", fake_pin)
    req = request(d, pins={"image": "ghcr.io/example/vllm:sm121",
                           "secondary": {"model_ref": "release", "local_image": "coder-local"}})
    job = await cluster.wait_job((await integrate(c, req)).job_id)
    assert job.state.value == "failed"
    assert not c.get_profile("duo").revisions
    assert job.payload["snapshot"]["identity"] is None
    assert seen[-1][1]["model_ref"] == "release"
    fail = False
    job = await cluster.wait_job((await retry(c, job.job_id, RetryRequest(request_id="pin-retry-001"))).job_id)
    assert job.state.value == "completed", job.error
    assert len(c.get_profile("duo").revisions) == 1


async def test_integration_api_auth_validation_and_retry(cluster):
    c = cluster.controller
    app = create_app(c, management_key="manager-key", run_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://manager") as client:
        req = request().model_dump(mode="json")
        assert (await client.post("/api/v1/cookbook/integrate", json=req)).status_code == 401
        client.headers["x-api-key"] = "manager-key"
        response = await client.post("/api/v1/cookbook/integrate", json=req)
        assert response.status_code == 202, response.text
        job = await cluster.wait_job(response.json()["job_id"])
        assert (await client.post("/api/v1/cookbook/integrate", json=req)).json()["job_id"] == job.job_id
        assert (await client.post(f"/api/v1/cookbook/integration/{job.job_id}/retry",
                                 json={"request_id": "retry-done-001"})).status_code == 422
        req["request_id"] = "bad / id"
        assert (await client.post("/api/v1/cookbook/integrate", json=req)).status_code == 422


async def test_a_recipe_without_an_image_is_refused_before_anything_starts(cluster, monkeypatch):
    """Like qwen3.8-flash-next-nvfp4-tp2 (image_hint null): ask for the image, do not fail minutes later."""
    from twinspark.controller import pinning
    c = cluster.controller
    d = split_draft()
    d.identity = d.secondary.identity = None
    d.image_hint = d.secondary.image_hint = None
    d.source = {"url": "https://github.com/example/recipe"}
    hub = []
    monkeypatch.setattr(pinning, "resolve_hf_revision", lambda *a, **k: hub.append(a) or {})
    with pytest.raises(ValueError, match="node A: no vLLM image chosen.*github.com/example/recipe.*node B"):
        await integrate(c, request(d))
    assert c.get_profile("duo") is None and not c.store.list_jobs() and not c.busy()
    with pytest.raises(ValueError, match="node B: no vLLM image chosen"):        # A chosen, B still missing
        await integrate(c, request(d, pins={"image": "ghcr.io/example/vllm:sm121"}))
    c.create_profile(d)
    with pytest.raises(pinning.PinError, match=r"tsm pin duo --image <ref>.*node B: .*tsm pin duo --image-b <ref>"):
        await c.pin_profile("duo")
    assert hub == []                                                         # failed before any Hub lookup


def test_tsm_pin_passes_node_b_choices_as_secondary():
    from twinspark import cli
    sent = {}

    def api(method, path, json=None, timeout=None):
        sent.update(path=path, body=json)
        raise SystemExit(0)
    api.as_json = False
    args = cli.build_parser().parse_args(["pin", "duo", "--image", "a/img:1", "--image-b", "b/img:2"])
    with pytest.raises(SystemExit):
        cli.cmd_pin(args, api)
    assert sent["body"] == {"image": "a/img:1", "secondary": {"image": "b/img:2"}}
