"""Regression coverage for interactions between the two completed feature branches."""
from __future__ import annotations

import pytest

from twinspark.controller.compatibility import check_alias
from twinspark.controller.controller import BusyError
from twinspark.controller.integration import IntegrationRequest, integrate
from twinspark.schemas.enums import Topology

from .conftest import draft


async def test_automatic_recipe_preparation_protects_profile_from_deletion(cluster):
    ctrl = cluster.controller
    job = await integrate(ctrl, IntegrationRequest(draft=draft(topology=Topology.SINGLE_A),
                                                   request_id="merge-protection-1"))
    assert ctrl.busy_profile == "qwen"
    with pytest.raises(BusyError):
        ctrl.delete_profile("qwen")
    result = await cluster.wait_job(job.job_id)
    assert result.state.value == "completed", result.error
    assert ctrl.busy_profile is None
    assert ctrl.delete_profile("qwen")


async def test_compatibility_checks_protect_the_exact_recipe_while_running(cluster):
    ctrl = cluster.controller
    profile = ctrl.create_profile(draft(topology=Topology.SINGLE_A))
    ctrl.gateway.set_route("default", ["http://simulation.invalid"], "qwen", profile.latest().revision_id)
    job = await check_alias(ctrl, "default", ["chat"])
    with pytest.raises(BusyError):
        ctrl.delete_profile("qwen")
    result = await cluster.wait_job(job.job_id)
    assert result.payload["checks"][0]["status"] == "simulated"
    assert ctrl.busy_profile is None


@pytest.mark.parametrize("operation", ["stop", "switch", "prepare"])
async def test_background_startup_respects_operator_changes_during_agent_wait(cluster, monkeypatch, operation):
    ctrl = cluster.controller
    profile = ctrl.create_profile(draft(topology=Topology.SINGLE_A))
    active = {"profile": profile.name, "revision_id": profile.latest().revision_id, "since": "original"}
    ctrl.store.kv_set("active", active)

    async def refresh():
        return {}

    async def wait(timeout):
        if operation == "prepare":
            await ctrl._lock.acquire()
        else:
            ctrl.store.kv_set("active", None if operation == "stop" else {**active, "since": "operator-switch"})
        return []

    async def forbidden(*args, **kwargs):
        pytest.fail("startup overrode an operator decision")

    monkeypatch.setattr(ctrl, "refresh_hardware", refresh)
    monkeypatch.setattr(ctrl, "_wait_for_agents", wait)
    monkeypatch.setattr(ctrl, "_adopt_running", forbidden)
    monkeypatch.setattr(ctrl, "activate", forbidden)
    try:
        assert await ctrl.on_startup() is None
    finally:
        if operation == "prepare":
            ctrl._lock.release()


async def test_startup_does_not_restore_old_routes_after_operator_stop_during_health_probe(cluster, monkeypatch):
    ctrl = cluster.controller
    profile = ctrl.create_profile(draft(topology=Topology.SINGLE_A))
    ctrl.store.kv_set("active", {"profile": profile.name, "revision_id": profile.latest().revision_id})

    async def refresh():
        return {}

    async def wait(timeout):
        return []

    async def health(action, **kwargs):
        assert action == "health_probe"
        ctrl.store.kv_set("active", None)
        return {"healthy": True}

    async def forbidden(*args, **kwargs):
        pytest.fail("startup restarted the deployment that was deliberately stopped")

    monkeypatch.setattr(ctrl, "refresh_hardware", refresh)
    monkeypatch.setattr(ctrl, "_wait_for_agents", wait)
    monkeypatch.setattr(ctrl.agents["A"], "call", health)
    monkeypatch.setattr(ctrl, "activate", forbidden)
    assert await ctrl.on_startup() is None
    assert ctrl.active() is None
    assert not ctrl.gateway.routes
