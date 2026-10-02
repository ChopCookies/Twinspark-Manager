import asyncio
import json
import time

import httpx
import pytest

from twinspark.agent import maintenance as root
from twinspark.agent import monitoring
from twinspark.controller.app import create_app
from twinspark.controller.controller import BusyError

from .conftest import draft


def test_sample_rates_unknowns_and_counter_reset(tmp_path, monkeypatch):
    (tmp_path / "net").mkdir()
    (tmp_path / "stat").write_text("cpu 10 0 10 80 0 0 0 0 100 0\n")

    def network(rx, tx):
        (tmp_path / "net/dev").write_text(f"header\nheader\neth0: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n")

    network(100, 200)
    clock = [10]
    monkeypatch.setattr(monitoring.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(monitoring, "gpu_sample", lambda: {"power_w": None})
    monkeypatch.setattr(monitoring, "memory_snapshot", lambda: {"mem_total_gib": 0, "mem_available_gib": 0})
    sampler = monitoring.HostSampler(str(tmp_path), str(tmp_path))
    first = sampler.sample()
    assert first["cpu_pct"] is None and first["mem_total_gib"] is None
    assert first["network"]["eth0"]["rx_bytes_s"] is None
    assert first["gpu"]["power_w"] is None
    clock[0] = 12
    (tmp_path / "stat").write_text("cpu 20 0 20 100 0 0 0 0 100 0\n")
    network(300, 600)
    second = sampler.sample()
    assert second["cpu_pct"] == 50
    assert second["network"]["eth0"]["rx_bytes_s"] == 100
    assert second["network"]["eth0"]["tx_bytes_s"] == 200
    clock[0] = 15
    network(0, 0)
    assert sampler.sample()["network"]["eth0"]["rx_bytes_s"] is None


@pytest.mark.parametrize("value", ["N/A", "[Not Supported]", "nan", "inf", "-1"])
def test_unsupported_sensors_are_not_zero(value):
    assert monitoring.number(value) is None


@pytest.fixture
def root_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "ROOT", tmp_path)
    monkeypatch.setattr(root, "policy", lambda: {"enabled": True, "allow_firmware": True})
    monkeypatch.setattr(root, "boot_id", lambda: "boot-before")
    return tmp_path


def queued(firmware=False):
    s = {"run_id": "a" * 32, "state": "queued", "boot_id": "boot-before", "firmware": firmware}
    root.save(s)
    return s


def test_worker_does_not_reboot_or_bypass_package_safety(root_worker, monkeypatch):
    state = queued()
    calls = []
    monkeypatch.setattr(root, "command", lambda argv, **kw: calls.append(argv) or "")
    root.worker(state["run_id"])
    assert root.load(state["run_id"])["state"] == "awaiting_reboot"
    assert "--no-remove" in calls[1]
    assert not any("reboot" in c for c in calls)
    assert "APT::Update::Error-Mode=any" in calls[0]


def test_worker_failure_does_not_advance_or_install_firmware(root_worker, monkeypatch):
    state = queued(True)

    def fail(argv, **kw):
        raise RuntimeError("package lock held")

    monkeypatch.setattr(root, "command", fail)
    root.worker(state["run_id"])
    result = root.load(state["run_id"])
    assert result["state"] == "failed" and "package lock" in result["error"]
    with pytest.raises(RuntimeError, match="not completed"):
        root.reboot({"run_id": state["run_id"]})


def test_reboot_idempotent_and_firmware_verified_after_boot(root_worker, monkeypatch):
    state = queued(True)
    state.update(state="awaiting_reboot", firmware_targets={"device": "2.0"})
    root.save(state)
    calls = []
    monkeypatch.setattr(root, "command", lambda argv, **kw: calls.append(argv) or "")
    root.reboot({"run_id": state["run_id"]})
    root.reboot({"run_id": state["run_id"]})
    assert len(calls) == 1
    monkeypatch.setattr(root, "boot_id", lambda: "boot-after")
    monkeypatch.setattr(
        root,
        "command",
        lambda argv, **kw: (
            json.dumps({"Devices": [{"DeviceId": "device", "Version": "1.0"}]}) if "fwupdmgr" in argv[0] else ""
        ),
    )
    result = root.status({"run_id": state["run_id"]})
    assert result["state"] == "failed" and "firmware versions" in result["error"]


def test_unexpected_reboot_during_install_is_failure(root_worker, monkeypatch):
    state = queued()
    state["state"] = "installing"
    root.save(state)
    monkeypatch.setattr(root, "boot_id", lambda: "boot-after")
    assert root.status({"run_id": state["run_id"]})["state"] == "failed"


def test_root_policy_default_disabled_and_run_id_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "POLICY", tmp_path / "missing")
    assert root.policy() == {"enabled": False, "allow_firmware": False}
    with pytest.raises(ValueError):
        root.run_id({"run_id": "../other"})


def test_root_start_is_idempotent_and_policy_is_enforced(root_worker, monkeypatch):
    calls = []
    monkeypatch.setattr(root, "command", lambda argv, **kw: calls.append(argv) or "")
    params = {"run_id": "c" * 32, "firmware": False}
    root.start(params)
    root.start(params)
    assert len(calls) == 1
    assert calls[0][0] == "/usr/bin/systemd-run"
    monkeypatch.setattr(root, "policy", lambda: {"enabled": False, "allow_firmware": False})
    with pytest.raises(RuntimeError, match="not enabled"):
        root.start({"run_id": "d" * 32})


@pytest.mark.asyncio
async def test_failed_node_never_starts_peer_and_cannot_release_live_worker(cluster, monkeypatch):
    c = cluster.controller
    started = []
    for n, agent in c.agents.items():
        original = agent.call

        async def call(action, _n=n, _original=original, **params):
            if action == "maintenance_status" and _n == "B":
                return {"state": "failed", "error": "package failure", "worker_active": True}
            if action == "maintenance_start":
                started.append(_n)
            return await _original(action, **params)

        monkeypatch.setattr(agent, "call", call)
    await c.maintenance.start()
    await c.maintenance.task
    assert c.maintenance.state()["state"] == "failed"
    assert not started
    with pytest.raises(ValueError, match="still updating"):
        await c.maintenance.release()
    await c.aclose()


@pytest.mark.asyncio
async def test_lost_worker_record_does_not_repeat_installation(cluster):
    c = cluster.controller
    state = {
        "run_id": "e" * 32,
        "state": "running",
        "phase": "update",
        "order": ["B", "A"],
        "index": 0,
        "nodes": {},
        "dispatched": ["B"],
        "firmware": False,
        "previous": None,
        "deadline": time.time() + 60,
    }
    c.store.kv_set("maintenance", state)
    with pytest.raises(RuntimeError, match="lost or never saved"):
        await c.maintenance.tick(state)
    await c.aclose()


@pytest.mark.asyncio
async def test_drain_timeout_preserves_serving_model(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(draft())
    c.save_revision(draft())
    job = await c.activate("qwen")
    await cluster.wait_job(job.job_id)
    previous = c.active()

    async def never_idle(*args):
        return False

    monkeypatch.setattr(c.gateway, "wait_idle", never_idle)
    await c.maintenance.start()
    await c.maintenance.task
    assert c.maintenance.state()["state"] == "failed"
    assert "did not drain" in c.maintenance.state()["error"]
    assert c.active() == previous
    assert any(container.status == "running" for container in cluster.runtimes["A"].list_owned())
    await c.aclose()


@pytest.mark.asyncio
async def test_release_and_resume_are_serialized(cluster, monkeypatch):
    c = cluster.controller
    c.store.kv_set("maintenance", {"run_id": "f" * 32, "state": "failed", "order": ["B", "A"]})
    entered, allow = asyncio.Event(), asyncio.Event()
    original = c.agents["A"].call

    async def slow(action, **params):
        if action == "maintenance_status":
            entered.set()
            await allow.wait()
        return await original(action, **params)

    monkeypatch.setattr(c.agents["A"], "call", slow)
    release = asyncio.create_task(c.maintenance.release())
    await entered.wait()
    resume = asyncio.create_task(c.maintenance.resume())
    allow.set()
    await release
    with pytest.raises(ValueError, match="held maintenance"):
        await resume
    assert c.maintenance.state()["state"] == "released"
    await c.aclose()


@pytest.mark.asyncio
async def test_cluster_update_order_and_exact_revision_restore(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(draft())
    rev = c.save_revision(draft())
    job = await c.activate("qwen")
    await cluster.wait_job(job.job_id)
    newer = draft()
    newer.simple.context_length = 8192
    c.save_revision(newer)
    order = []
    for name, a in c.agents.items():
        original = a.call

        async def call(action, _name=name, _original=original, **params):
            if action == "maintenance_start":
                order.append(_name)
            return await _original(action, **params)

        monkeypatch.setattr(a, "call", call)
    await c.maintenance.start(firmware=True)
    with pytest.raises(BusyError):
        await c.activate("qwen")
    await c.maintenance.task
    assert c.maintenance.state()["state"] == "completed", c.maintenance.state()
    assert order == ["B", "A"]
    assert c.active()["revision_id"] == rev.revision_id
    assert not c.busy()
    await c.aclose()


def restoring_checkpoint(ctrl, previous):
    state = {"run_id": "9" * 32, "state": "running", "phase": "restoring", "firmware": False,
             "previous": previous, "order": ["B", "A"], "index": 2, "nodes": {},
             "restore_job": None, "deadline": time.time() + 60}
    ctrl.store.kv_set("maintenance", state)
    for alias in list(ctrl.gateway.routes):
        ctrl.gateway.clear_route(alias)
    return state


@pytest.mark.asyncio
async def test_restart_during_restore_rebuilds_exact_revision_routes_while_maintenance_is_reserved(cluster):
    c = cluster.controller
    c.create_profile(draft())
    activation = await c.activate("qwen")
    assert (await cluster.wait_job(activation.job_id)).state.value == "completed"
    previous = c.active()
    changed = draft()
    changed.simple.context_length = 8192
    newer = c.save_revision(changed)
    state = restoring_checkpoint(c, previous)
    assert c.busy()
    await c.maintenance.tick(state)
    assert c.maintenance.state()["state"] == "completed"
    assert c.gateway.routes["default"].revision_id == previous["revision_id"] != newer.revision_id
    assert c.active()["revision_id"] == previous["revision_id"] and not c.busy()
    await c.aclose()


@pytest.mark.asyncio
async def test_restore_checkpoint_holds_cluster_if_any_model_container_is_unhealthy(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(draft())
    activation = await c.activate("qwen")
    await cluster.wait_job(activation.job_id)
    restoring_checkpoint(c, c.active())
    original = c.agents["B"].call

    async def unhealthy(action, **params):
        if action == "health_probe":
            return {"healthy": False}
        return await original(action, **params)

    monkeypatch.setattr(c.agents["B"], "call", unhealthy)
    c.maintenance.ensure_running()
    await c.maintenance.task
    assert c.maintenance.state()["state"] == "failed" and c.busy()
    assert not c.gateway.routes["default"].backends
    await c.aclose()


@pytest.mark.asyncio
async def test_restore_does_not_resurrect_routes_after_active_state_changes_during_health(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(draft())
    activation = await c.activate("qwen")
    await cluster.wait_job(activation.job_id)
    state = restoring_checkpoint(c, c.active())
    entered, allow = asyncio.Event(), asyncio.Event()
    original = c.agents["A"].call

    async def delayed_health(action, **params):
        if action == "health_probe":
            entered.set()
            await allow.wait()
        return await original(action, **params)

    monkeypatch.setattr(c.agents["A"], "call", delayed_health)
    tick = asyncio.create_task(c.maintenance.tick(state))
    await entered.wait()
    c.store.kv_set("active", None)
    allow.set()
    with pytest.raises(RuntimeError, match="restoring the model failed"):
        await tick
    assert c.active() is None and not c.gateway.routes["default"].backends
    await c.aclose()


@pytest.mark.asyncio
async def test_foreign_workload_holds_cluster_without_updating(cluster):
    c = cluster.controller
    cluster.runtimes["B"].foreign = [{"name": "foreign-vllm"}]
    await c.maintenance.start()
    await c.maintenance.task
    state = c.maintenance.state()
    assert state["state"] == "failed" and "unmanaged inference" in state["error"]
    assert state["nodes"] == {}
    assert c.busy()
    cluster.runtimes["B"].foreign = []
    await c.maintenance.resume()
    await c.maintenance.task
    assert c.maintenance.state()["state"] == "completed"
    await c.aclose()


@pytest.mark.asyncio
async def test_restart_resumes_checkpoint_instead_of_autostart(cluster):
    c = cluster.controller
    rid = "b" * 32
    c.store.kv_set(
        "maintenance",
        {
            "run_id": rid,
            "state": "running",
            "phase": "reboot",
            "order": ["B", "A"],
            "index": 0,
            "nodes": {},
            "firmware": False,
            "previous": None,
            "deadline": time.time() + 60,
        },
    )
    await c.agents["B"].call("maintenance_start", run_id=rid, firmware=False)
    await c.agents["B"].call("maintenance_reboot", run_id=rid)
    await c.on_startup()
    await c.maintenance.task
    assert c.maintenance.state()["state"] == "completed"
    await c.aclose()


@pytest.mark.asyncio
async def test_telemetry_collects_and_retains_stale_sample(cluster, monkeypatch):
    c = cluster.controller
    await c.metrics_tick()
    assert set(c.telemetry) == {"A", "B"}
    assert "cpu_pct" in c.telemetry["A"]
    at = c.telemetry["A"]["at"]

    async def fail(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(c.agents["A"], "call", fail)
    await c.metrics_tick()
    assert c.telemetry["A"]["at"] == at
    assert c.telemetry["A"]["stale"]
    assert len(c.telemetry_history["A"]) == 1
    await c.aclose()


@pytest.mark.asyncio
async def test_api_requires_explicit_optin_and_blocks_mutations_during_hold(cluster):
    c = cluster.controller
    app = create_app(c, "test-key", run_startup=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local", headers={"x-api-key": "test-key"}
    ) as client:
        r = await client.post("/api/v1/system/maintenance/start", json={"confirm": "yes"})
        assert r.status_code == 422
        c.store.kv_set("maintenance", {"state": "failed"})
        assert (await client.post("/api/v1/stop")).status_code == 409
        assert (await client.get("/api/v1/system/maintenance")).status_code == 200
    await c.aclose()
