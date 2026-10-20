"""A switch does not silently cut running requests (master plan §1.3).

Policies for requests still running when a switch starts: ``abort`` (the default for a manual switch:
the switch fails before anything is stopped and the old model keeps serving), ``wait`` (until they are
done, cancellable) and ``force`` (end them, the 0.6 behaviour). Switches the controller makes itself force.
"""

from __future__ import annotations

import asyncio

import httpx

from twinspark.controller.app import create_app
from twinspark.schemas.enums import Topology

from .conftest import draft


async def _serving(cluster):
    c = cluster.controller
    c.create_profile(draft("old", Topology.TP2, repo="org/model-a"))
    job = await cluster.wait_job((await c.activate("old")).job_id)
    assert job.state.value == "completed", job.error
    c.create_profile(draft("new", Topology.TP2, repo="org/model-b"))
    return c.gateway.routes["default"]


def _old_running(cluster) -> bool:
    return any(st.status == "running" and "old" in n for rt in cluster.runtimes.values()
               for n, st in rt.containers.items())


async def test_by_default_a_switch_with_a_running_request_is_aborted_and_the_old_model_keeps_serving(cluster):
    route = await _serving(cluster)
    c = cluster.controller
    tok = route.acquire()                                       # a long agent task, mid-answer
    job = await cluster.wait_job((await c.activate("new", drain={"timeout_s": 0.2})).job_id)
    assert job.state.value == "failed" and "switch aborted: 1 request(s) still running" in job.error
    assert "The old model keeps serving" in job.error and "'wait'" in job.error and "'force'" in job.error
    assert job.rollback == "failed before stopping the old model; it keeps serving"
    assert not route.draining and route.backends and route.served_model and c.active()["profile"] == "old"
    assert _old_running(cluster)
    route.release(tok)


async def test_wait_lets_running_requests_finish_then_switches(cluster):
    route = await _serving(cluster)
    c = cluster.controller
    tok = route.acquire()

    async def finish_later():
        await asyncio.sleep(0.4)
        route.release(tok)
    asyncio.create_task(finish_later())
    job = await cluster.wait_job((await c.activate("new", drain={"policy": "wait", "timeout_s": 0.1})).job_id)
    assert job.state.value == "completed", job.error
    drain = next(s for s in job.steps if s.stage == "draining")
    assert drain.message.startswith("drained") and c.active()["profile"] == "new"


async def test_waiting_can_be_cancelled_and_nothing_is_stopped(cluster):
    route = await _serving(cluster)
    c = cluster.controller
    tok = route.acquire()
    job = await c.activate("new", drain={"policy": "wait"})
    for _ in range(200):
        cur = c.store.load_job(job.job_id)
        if any(s.stage == "draining" and "waiting for 1 running request" in (s.message or "") for s in cur.steps):
            break
        await asyncio.sleep(0.02)
    c.cancel_job(job.job_id)
    done = await cluster.wait_job(job.job_id)
    assert done.state.value in ("failed", "cancelled") and c.active()["profile"] == "old"
    assert not route.draining and _old_running(cluster)
    route.release(tok)


async def test_force_ends_running_requests_like_before(cluster):
    route = await _serving(cluster)
    c = cluster.controller
    route.acquire()
    job = await cluster.wait_job((await c.activate("new", drain={"policy": "force", "timeout_s": 0.2})).job_id)
    assert job.state.value == "completed", job.error
    drain = next(s for s in job.steps if s.stage == "draining")
    assert "forced after" in drain.message and "1 request(s) still running" in drain.message


def test_controller_made_switches_force_and_bad_settings_are_refused(cluster):
    c = cluster.controller
    assert c._drain_settings({"policy": "wait"}, controller_made=True)["policy"] == "force"
    assert c._drain_settings(None, controller_made=False) == {"policy": "abort", "timeout_s": 1}
    assert c._drain_settings({"timeout_s": 0}, controller_made=False)["timeout_s"] == 0


async def test_the_api_takes_the_policy_and_shows_running_requests(cluster):
    route = await _serving(cluster)
    c = cluster.controller
    tok = route.acquire()
    app = create_app(c, management_key="k", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "k"}) as api:
        st = (await api.get("/api/v1/status")).json()
        r = next(x for x in st["routes"] if x["alias"] == "default")
        assert r["inflight"] == 1 and r["oldest_inflight_s"] is not None
        bad = await api.post("/api/v1/profiles/new/activate", json={"drain_policy": "kill"})
        assert bad.status_code == 422
        assert (await api.post("/api/v1/profiles/new/activate", json={"drain_timeout_s": -1})).status_code == 422
        ok = await api.post("/api/v1/profiles/new/activate", json={"drain_policy": "force", "drain_timeout_s": 0})
        assert ok.status_code == 202
    route.release(tok)
    await cluster.wait_idle()
