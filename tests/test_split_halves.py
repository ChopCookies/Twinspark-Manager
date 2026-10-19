"""Split deployments run two independent models: each half switches, fails and recovers on its own
(master plan §2.2). Dry-run agents; an in-flight request is a gateway token held open by the test."""

from __future__ import annotations

import pytest

from twinspark.controller import halves
from twinspark.schemas.profile import ProfileDraft

from .test_split_preparation import split_draft


def b_changed(c, name="duo", **advanced) -> ProfileDraft:
    """The working draft of ``name`` with only node B's half changed."""
    d = c.get_profile(name).latest().draft.model_dump(mode="json")
    d["secondary"]["advanced"].update(advanced or {"max_num_seqs": 3})
    return ProfileDraft.model_validate(d)


async def activated(cluster, name="duo", ref="latest", **kw):
    job = await cluster.wait_job((await cluster.controller.activate(name, ref, **kw)).job_id)
    assert job.state.value == "completed", (job.error, [(s.stage, s.message) for s in job.steps])
    return job


def running(cluster, node):
    return {n: st.status for n, st in cluster.runtimes[node].containers.items()}


async def test_a_new_revision_with_only_b_changed_leaves_a_and_its_requests_alone(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    await activated(cluster)
    r1 = c.active()["revision_id"]
    a_before = running(cluster, "A")
    c.save_revision(b_changed(c))
    r2 = c.get_profile("duo").latest().revision_id
    assert r2 != r1
    tok = c.gateway.routes["chat"].acquire()          # a long answer on node A's model is streaming
    job = await activated(cluster, drain={"policy": "abort", "timeout_s": 0})
    assert job.payload["scope"]["nodes"] == ["B"] and job.payload["scope"]["kept"] == ["A"]
    assert running(cluster, "A") == a_before                         # same container, never stopped
    assert c.gateway.routes["chat"].inflight == 1 and not c.gateway.routes["chat"].draining
    assert c.gateway.routes["chat"].revision_id == r1                # still the container r1 started
    assert c.gateway.routes["code"].revision_id == r2 and c.gateway.routes["code"].backends
    stops = [cmd for cmd in cluster.runtimes["A"].history if cmd[1:2] == ["stop"]]
    assert stops == []
    act = c.active()
    assert act["revision_id"] == r2 and act["half_revisions"] == {"A": r1, "B": r2}
    assert "kept serving throughout" in job.steps[-1].message
    c.gateway.routes["chat"].release(tok)
    # the watchdog probes the containers that really run: node A's still carries r1's name
    assert await c.watchdog_tick() is None
    st = {h["node"]: h for h in c.status()["halves"]}
    assert st["A"]["revision_id"] == r1 and st["B"]["revision_id"] == r2
    assert st["A"]["state"] == st["B"]["state"] == "serving"
    # and a controller restart adopts both halves under their own names
    c.gateway.routes.clear()
    assert await c._adopt_running(c.active())
    assert c.gateway.routes["chat"].revision_id == r1 and c.gateway.routes["code"].revision_id == r2


async def test_the_same_revision_again_restarts_both_halves(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    await activated(cluster)
    job = await activated(cluster)
    assert job.payload["scope"] is None


async def test_a_half_is_restarted_on_its_own_on_request(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    await activated(cluster)
    a_before = running(cluster, "A")
    job = await activated(cluster, nodes=["B"])
    assert job.payload["scope"]["nodes"] == ["B"] and running(cluster, "A") == a_before
    with pytest.raises(ValueError, match="halves A and B"):
        await c.activate("duo", nodes=["C"])
    c.save_revision(b_changed(c))
    with pytest.raises(ValueError, match="only for the active revision"):
        await c.activate("duo", nodes=["B"])


async def test_the_watchdog_restarts_only_the_half_that_crashed(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    await activated(cluster)
    a_before = running(cluster, "A")
    b_name = next(iter(cluster.runtimes["B"].containers))
    cluster.runtimes["B"].containers[b_name].status = "exited"
    problem = await c.watchdog_tick()
    assert "exited" in problem
    assert c.gateway.routes["code"].down_reason and not c.gateway.routes["chat"].down_reason
    incident = c.store.kv_get("last_incident")
    assert incident["nodes"] == ["B"] and "node B's half" in incident["action"]
    await cluster.wait_idle()
    job = c.store.load_job(c.store.list_jobs(limit=1)[0].job_id)
    assert job.kind == "recovery" and job.state.value == "completed" and job.payload["scope"]["nodes"] == ["B"]
    assert running(cluster, "A") == a_before
    assert c.gateway.routes["code"].backends and not c.gateway.routes["code"].down_reason


async def test_a_failed_switch_of_one_half_leaves_the_other_serving(cluster):
    c = cluster.controller
    c.config.auto_rollback = False
    c.create_profile(split_draft())
    await activated(cluster)
    r1 = c.active()["revision_id"]
    a_before = running(cluster, "A")
    c.save_revision(b_changed(c))
    r2 = c.get_profile("duo").latest().revision_id
    cluster.runtimes["B"].fail_start.add(halves.container_name("duo", "B", r2))
    job = await cluster.wait_job((await c.activate("duo")).job_id)
    assert job.state.value == "failed"
    assert running(cluster, "A") == a_before and c.gateway.routes["chat"].backends
    assert "code" not in c.gateway.routes or not c.gateway.routes["code"].backends
    act = c.active()
    assert act["revision_id"] == r1 and act["halves_down"] == ["B"] and act["down_reasons"]["B"]
    assert await c.watchdog_tick() is None                          # a half that is down is not "recovered"
    st = {h["node"]: h for h in c.status()["halves"]}
    assert st["B"]["state"] == "down" and st["A"]["state"] == "serving"
    # restarting the old half brings it back, still without touching A
    job = await activated(cluster, "duo", r1, nodes=["B"])
    assert running(cluster, "A") == a_before and c.gateway.routes["code"].backends
    assert "halves_down" not in c.active() or not c.active()["halves_down"]


async def test_a_failed_half_switch_rolls_back_only_that_half(cluster):
    c = cluster.controller
    c.config.auto_rollback = True
    c.create_profile(split_draft())
    await activated(cluster)
    r1 = c.active()["revision_id"]
    a_before = running(cluster, "A")
    c.save_revision(b_changed(c))
    r2 = c.get_profile("duo").latest().revision_id
    cluster.runtimes["B"].fail_start.add(halves.container_name("duo", "B", r2))
    await cluster.wait_job((await c.activate("duo")).job_id)
    await cluster.wait_idle()
    rollback = c.store.list_jobs(limit=1)[0]
    assert rollback.kind == "rollback" and rollback.state.value == "completed"
    assert rollback.payload["scope"]["nodes"] == ["B"]
    assert c.active()["revision_id"] == r1 and not c.active().get("halves_down")
    assert running(cluster, "A") == a_before


async def test_a_switch_to_a_different_profile_touches_everything(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    other = split_draft().model_copy(update={"name": "trio"})
    c.create_profile(other)
    await activated(cluster)
    job = await activated(cluster, "trio")
    assert job.payload["scope"] is None


async def test_a_half_that_did_not_come_back_after_a_restart_is_restarted_alone(cluster):
    c = cluster.controller
    c.config.autostart = True
    c.create_profile(split_draft())
    await activated(cluster)
    a_before = running(cluster, "A")
    cluster.runtimes["B"].containers.clear()             # node B rebooted; node A's model kept running
    c.gateway.routes.clear()
    job = await c._resume_active()
    assert job is not None and job.payload["scope"]["nodes"] == ["B"]
    assert c.gateway.routes["chat"].backends                          # adopted before B was restarted
    await cluster.wait_job(job.job_id)
    assert running(cluster, "A") == a_before and c.gateway.routes["code"].backends


def test_scoped_plan_keeps_only_that_nodes_part(cluster):
    c = cluster.controller
    c.create_profile(split_draft())
    plan = c.launch_plan(c.get_profile("duo").latest())
    b = halves.scoped_plan(plan, ["B"])
    assert [x.node for x in b.containers] == ["B"] and set(b.routes) == {"code"}
    assert [m["nodes"] for m in b.model_requirements] == [["B"]] and b.node_utilization == {"B": 0.6}
    assert b.start_order == [[b.containers[0].name]]


async def test_the_api_restarts_one_half_and_says_what_a_switch_touches(cluster):
    import httpx

    from twinspark.client.bench_local import switch_touches
    from twinspark.controller.app import create_app
    c = cluster.controller
    c.create_profile(split_draft())
    await activated(cluster)
    app = create_app(c, management_key="k", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "k"}) as api:
        r = await api.post("/api/v1/profiles/duo/activate", json={"nodes": ["B"]})
        assert r.status_code == 202 and r.json()["payload"]["scope"]["nodes"] == ["B"]
        st = (await api.get("/api/v1/status")).json()
        if st["busy"]:
            assert st["switch"]["nodes"] == ["B"] and st["switch"]["aliases"] == ["code"]
        await cluster.wait_job(r.json()["job_id"])
        assert (await api.post("/api/v1/profiles/duo/activate", json={"nodes": ["C"]})).status_code == 422
    busy_b = {"busy": True, "current_job": "act-1", "switch": {"nodes": ["B"], "aliases": ["code"]}}
    assert switch_touches(busy_b, "code") and not switch_touches(busy_b, "chat")
    assert switch_touches({"busy": True, "current_job": "act-2", "switch": {"nodes": None, "aliases": None}}, "chat")
    assert not switch_touches({"busy": False}, "chat")
