"""An accepted switch belongs to the node: idempotent requests, stale views refused, and a controller
restart in the middle of a switch cleaned up the same way every time (brief §5.2)."""

from __future__ import annotations

import asyncio

import pytest

from twinspark.controller.controller import JOURNAL_KEY, Controller, StaleViewError
from twinspark.controller.jobs import ActivationCoordinator
from twinspark.gateway.gateway import Gateway
from twinspark.schemas.enums import Topology

from .conftest import draft


async def _serving(cluster, name="old", repo="org/model-a"):
    c = cluster.controller
    c.create_profile(draft(name, Topology.TP2, repo=repo))
    job = await cluster.wait_job((await c.activate(name)).job_id)
    assert job.state.value == "completed", job.error
    return c.active()


async def test_the_same_request_id_returns_the_same_job(cluster):
    c = cluster.controller
    c.create_profile(draft("glm", Topology.TP2))
    first = await c.activate("glm", request_id="gui-0123456789abcdef")
    again = await c.activate("glm", request_id="gui-0123456789abcdef")          # e.g. resent after a drop
    assert again.job_id == first.job_id
    await cluster.wait_job(first.job_id)
    assert (await c.activate("glm", request_id="gui-0123456789abcdef")).job_id == first.job_id
    assert len([j for j in c.store.list_jobs(limit=20) if j.kind == "activation"]) == 1
    c.create_profile(draft("other", Topology.TP2))
    with pytest.raises(ValueError, match="already used for another profile"):
        await c.activate("other", request_id="gui-0123456789abcdef")
    with pytest.raises(ValueError, match="request_id"):
        await c.activate("glm", request_id="bad id; rm -rf")


async def test_a_switch_decided_on_a_stale_view_is_refused(cluster):
    c = cluster.controller
    active = await _serving(cluster)
    c.create_profile(draft("new", Topology.TP2, repo="org/model-b"))
    with pytest.raises(StaleViewError, match="nothing.*you saw|is active now"):
        await c.activate("new", expected_active="someone-else-r9")
    with pytest.raises(StaleViewError):
        await c.activate("new", expected_active=None)                          # the browser saw nothing active
    job = await c.activate("new", expected_active=active["revision_id"])     # the view is current: accepted
    assert (await cluster.wait_job(job.job_id)).state.value == "completed"


async def test_the_api_passes_request_id_and_expected_active(cluster):
    import httpx

    from twinspark.controller.app import create_app
    c = cluster.controller
    active = await _serving(cluster)
    c.create_profile(draft("new", Topology.TP2, repo="org/model-b"))
    app = create_app(c, management_key="k", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "k"}) as api:
        r = await api.post("/api/v1/profiles/new/activate", json={"expected_active": "stale-r1"})
        assert r.status_code == 409 and "changed since you looked" in r.json()["detail"]
        body = {"request_id": "cli-00112233aabbccdd", "expected_active": active["revision_id"]}
        r1 = await api.post("/api/v1/profiles/new/activate", json=body)
        r2 = await api.post("/api/v1/profiles/new/activate", json=body)
        assert r1.status_code == r2.status_code == 202 and r1.json()["job_id"] == r2.json()["job_id"]
        assert (await api.post("/api/v1/profiles/new/activate")).status_code in (202, 409)   # no body still works
    await cluster.wait_idle()


def _restarted(cluster) -> Controller:
    """A second controller over the same database and agents: what runs after a crash or reboot."""
    old = cluster.controller
    return Controller(old.config, Gateway(inference_api_key="client-key", backend_api_key="backend-key"),
                      old.agents, poll_interval=0.01)


async def _hang(monkeypatch, method: str) -> asyncio.Event:
    entered = asyncio.Event()
    real = getattr(ActivationCoordinator, method)

    async def stuck(self, job, rev, step):
        entered.set()
        await asyncio.sleep(3600)                                         # the process "dies" here
    monkeypatch.setattr(ActivationCoordinator, method, stuck)
    return entered, real


async def _wait(c: Controller, job_id: str, timeout=10):
    for _ in range(int(timeout / 0.02)):
        job = c.store.load_job(job_id)
        if job and job.state.value in ("completed", "failed") and not c.busy():
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish")


async def test_a_restart_during_the_switch_stops_the_partial_containers_and_restores_the_previous(
        cluster, monkeypatch):
    previous = await _serving(cluster)
    c = cluster.controller
    c.config.startup_wait_s = 0
    c.create_profile(draft("new", Topology.TP2, repo="org/model-b"))
    entered, real = await _hang(monkeypatch, "_wait_loaded")              # after the old model was stopped
    job = await c.activate("new")
    await asyncio.wait_for(entered.wait(), 10)
    new_containers = {n for rt in cluster.runtimes.values() for n, st in rt.containers.items()
                      if st.status == "running"}
    assert new_containers and c.store.kv_get(JOURNAL_KEY)["job_id"] == job.job_id
    monkeypatch.setattr(ActivationCoordinator, "_wait_loaded", real)
    c2 = _restarted(cluster)
    rollback = await c2.on_startup()
    crashed = c2.store.load_job(job.job_id)
    assert crashed.state.value == "failed" and "interrupted by controller restart" in crashed.error
    assert crashed.rollback.startswith("the controller restarted during the switch: stopped")
    assert f"restoring previous {previous['profile']}" in crashed.rollback
    assert rollback is not None and rollback.kind == "rollback" and rollback.payload["rollback_of"] == job.job_id
    assert (await _wait(c2, rollback.job_id)).state.value == "completed"
    assert c2.active()["revision_id"] == previous["revision_id"]
    assert not any(st.status == "running" for rt in cluster.runtimes.values() for n, st in rt.containers.items()
                   if n in new_containers and "new" in n)
    assert c2.store.kv_get(JOURNAL_KEY) is None
    for t in list(c._tasks):                                              # the "dead" process
        t.cancel()


async def test_without_auto_rollback_the_partial_switch_is_only_cleaned_up(cluster, monkeypatch):
    await _serving(cluster)
    c = cluster.controller
    c.config.startup_wait_s = 0
    c.config.auto_rollback = False
    c.create_profile(draft("new", Topology.TP2, repo="org/model-b"))
    entered, real = await _hang(monkeypatch, "_wait_loaded")
    job = await c.activate("new")
    await asyncio.wait_for(entered.wait(), 10)
    monkeypatch.setattr(ActivationCoordinator, "_wait_loaded", real)
    c2 = _restarted(cluster)
    assert await c2.on_startup() is None
    assert c2.active() is None and "nothing to restore" in c2.store.load_job(job.job_id).rollback
    assert not any(st.status == "running" for rt in cluster.runtimes.values() for st in rt.containers.values())
    for t in list(c._tasks):
        t.cancel()


async def test_a_restart_before_the_old_model_was_touched_keeps_it_serving(cluster, monkeypatch):
    previous = await _serving(cluster)
    c = cluster.controller
    c.config.startup_wait_s = 0
    c.create_profile(draft("new", Topology.TP2, repo="org/model-b"))
    entered, real = await _hang(monkeypatch, "_download")                 # before draining/stopping
    job = await c.activate("new")
    await asyncio.wait_for(entered.wait(), 10)
    monkeypatch.setattr(ActivationCoordinator, "_download", real)
    c2 = _restarted(cluster)
    await c2.on_startup()                                                 # re-attaches the running model
    assert "before the old model was touched" in c2.store.load_job(job.job_id).rollback
    assert c2.active()["revision_id"] == previous["revision_id"] and c2.gateway.routes
    assert c2.store.kv_get(JOURNAL_KEY) is None
    for t in list(c._tasks):
        t.cancel()


async def test_a_finished_switch_leaves_no_journal(cluster):
    await _serving(cluster)
    assert cluster.controller.store.kv_get(JOURNAL_KEY) is None
