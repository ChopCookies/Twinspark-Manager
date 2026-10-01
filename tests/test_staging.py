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
