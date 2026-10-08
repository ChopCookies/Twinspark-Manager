import pytest

from twinspark.controller.staging import StageError, WeightsStager
from twinspark.schemas.job import Job


class Agent:
    def __init__(self, present, verification=None):
        self.present = present
        self.verification = verification or {}
        self.calls = []

    async def call(self, action, **kw):
        self.calls.append((action, kw))
        if action == "weights_present":
            return {"present": self.present}
        if action == "hardware_facts":
            return {"hf_cache_dir": "/cache"}
        return {"task_id": action}

    async def wait_task(self, tid, **kw):
        return {"result": self.verification}


@pytest.mark.parametrize("verified", [True, False])
async def test_copy_requires_successful_verification(controller_config, verified):
    a, b = Agent(True), Agent(False, {"verified": verified, "note": "no recorded hashes"})
    original = a.wait_task

    async def copied(tid, **kw):
        b.present = True
        return await original(tid, **kw)

    a.wait_task = copied
    stager = WeightsStager(controller_config, {"A": a, "B": b}, lambda j: None)
    job = Job(job_id="test", kind="stage")
    step = job.begin_step("staging")
    if verified:
        result = await stager.ensure(job, step, "org/model", "a" * 40, ["A", "B"])
        assert "verified on B" in result
    else:
        with pytest.raises(StageError, match="verification failed on node B"):
            await stager.ensure(job, step, "org/model", "a" * 40, ["A", "B"])
    assert any(action == "verify" for action, _ in b.calls)


async def test_staging_does_not_copy_to_unrequested_node(controller_config):
    a, b = Agent(True), Agent(True)
    stager = WeightsStager(controller_config, {"A": a, "B": b}, lambda j: None)
    job = Job(job_id="test", kind="stage")
    await stager.ensure(job, job.begin_step("staging"), "org/model", "a" * 40, ["B"])
    assert a.calls == []
    assert b.calls[0][0] == "weights_present"


async def test_each_work_item_is_named_with_its_exact_revision(controller_config):
    """Reused weights, the QSFP copy and the checksum check show up as separate items."""
    a, b = Agent(True), Agent(False, {"verified": True})
    original = a.wait_task

    async def copied(tid, **kw):
        b.present = True
        return await original(tid, **kw)

    a.wait_task = copied
    stager = WeightsStager(controller_config, {"A": a, "B": b}, lambda j: None)
    job = Job(job_id="test", kind="prepare")
    await stager.ensure(job, job.begin_step("downloading"), "incoai/drafter", "b" * 40, ["A", "B"], kind="drafter")
    items = {p["kind"] + ":" + (p.get("node") or ""): p for p in job.payload["phases"]}
    assert items["drafter:A"]["state"] == "reused" and "not downloaded" in items["drafter:A"]["detail"]
    assert items["copy:B"]["state"] == "done" and "node A → node B over QSFP" in items["copy:B"]["label"]
    assert items["verify:B"]["state"] == "done" and items["verify:B"]["ref"] == "incoai/drafter@" + "b" * 40
    assert "drafter incoai/drafter@bbbbbbbbbbbb" in items["copy:B"]["label"]


async def test_a_download_is_its_own_item_and_a_failure_is_marked(controller_config):
    from twinspark.controller.agent_client import AgentActionError
    a = Agent(False)

    async def fail(tid, **kw):
        raise AgentActionError("download", "A", "Hub unreachable")
    a.wait_task = fail
    stager = WeightsStager(controller_config, {"A": a}, lambda j: None)
    job = Job(job_id="test", kind="prepare")
    with pytest.raises(AgentActionError):
        await stager.ensure(job, job.begin_step("downloading"), "org/model", "a" * 40, ["A"])
    (item,) = job.payload["phases"]
    assert item["kind"] == "checkpoint" and item["state"] == "failed" and "Hub unreachable" in item["detail"]


async def test_a_failed_job_leaves_no_item_in_progress(controller_config):
    """A failure outside the per-item handling (here: the verify call) still closes the open items."""
    from twinspark.controller.phases import settle
    a, b = Agent(True), Agent(False)

    async def copied(tid, **kw):
        b.present = True
        return {"result": {}}
    a.wait_task = copied

    async def broken(action, **kw):
        if action == "verify":
            raise RuntimeError("agent B went away")
        return await Agent.call(b, action, **kw)
    b.call = broken
    stager = WeightsStager(controller_config, {"A": a, "B": b}, lambda j: None)
    job = Job(job_id="test", kind="prepare")
    with pytest.raises(RuntimeError):
        await stager.ensure(job, job.begin_step("downloading"), "org/model", "a" * 40, ["A", "B"])
    assert any(p["state"] == "running" for p in job.payload["phases"])
    settle(job, "agent B went away")
    assert not any(p["state"] == "running" for p in job.payload["phases"])
    assert any(p["state"] == "failed" and p["detail"] == "agent B went away" for p in job.payload["phases"])
