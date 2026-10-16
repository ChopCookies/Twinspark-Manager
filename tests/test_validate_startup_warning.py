"""The switch itself says when the estimated startup margin is critical, before the old model is stopped."""

from __future__ import annotations

from types import SimpleNamespace

from twinspark.controller.planner import ModelSpec
from twinspark.schemas.enums import Topology

from .conftest import draft


async def test_a_critical_startup_margin_is_warned_about_in_the_job(cluster, monkeypatch):
    c = cluster.controller
    c.set_model_spec("org/model-a", ModelSpec(num_params=10**9, layers=4, num_kv_heads=8, head_dim=64,
                                              weight_bytes=2 * 1024**3, num_attention_heads=8))
    c.create_profile(draft("glm", Topology.TP2))
    seen = []

    def plan_node(**kw):
        seen.append(kw["node_id"])
        level = "CRITICAL" if kw["node_id"] == "A" else "SAFE"
        return SimpleNamespace(startup={"ok": level != "CRITICAL", "level": level,
                                        "margin_gib": 0.4 if level == "CRITICAL" else 9.0, "peak_gib": 120.0})
    monkeypatch.setattr(c.planner, "plan_node", plan_node)
    job = await cluster.wait_job((await c.activate("glm")).job_id)
    assert job.state.value == "completed"                       # advisory: it does not refuse the switch
    assert sorted(seen) == ["A", "B"]
    assert job.payload["startup_verdicts"]["glm"]["A"]["level"] == "CRITICAL"
    warn = [w for w in job.payload["warnings"] if "startup margin on node A" in w]
    assert warn and "0.4 GiB (CRITICAL)" in warn[0]
    assert "startup margin on node A" in job.steps[0].message


async def test_a_broken_advisory_model_never_blocks_a_switch(cluster, monkeypatch):
    c = cluster.controller
    c.set_model_spec("org/model-a", ModelSpec(num_params=10**9, layers=4, num_kv_heads=8, head_dim=64,
                                              weight_bytes=2 * 1024**3, num_attention_heads=8))
    c.create_profile(draft("glm", Topology.TP2))
    monkeypatch.setattr(c.planner, "plan_node", lambda **kw: 1 / 0)
    job = await cluster.wait_job((await c.activate("glm")).job_id)
    assert job.state.value == "completed" and "startup_verdicts" not in job.payload
