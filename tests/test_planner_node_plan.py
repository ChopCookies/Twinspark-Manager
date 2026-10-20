"""0.6 per-node memory plan: buckets with sources and ranges, three separate verdicts, drafter residency."""

from __future__ import annotations

import pytest

from twinspark.controller.planner import (
    ACT_BUFFERS,
    DEFAULT_BATCHED_TOKENS,
    GB10_MEM_TOTAL_GIB,
    DrafterInput,
    MemoryPlanner,
    ModelSpec,
    drafter_inputs,
    make_spec_from_hf,
)
from twinspark.schemas.enums import Quantization, Topology

GIB = 1024 ** 3


def main_spec(weights_gib=60.0, **kw) -> ModelSpec:
    return ModelSpec(num_params=0, layers=48, num_kv_heads=8, head_dim=128, weight_bytes=int(weights_gib * GIB),
                     hidden_size=kw.pop("hidden_size", 6144), kv_bytes_per_token=kw.pop("kv_bytes_per_token", 9178),
                     **kw)


DRAFTER = ModelSpec(num_params=0, layers=2, num_kv_heads=8, head_dim=128, weight_bytes=int(4 * GIB), hidden_size=4096)
SPARK = {"mem_total_gib": 121.6, "mem_available_gib": 112.6, "desktop_running": False, "desktop_rss_gib": 0.0,
         "desktop_processes": [], "other_processes": [{"name": "hermes-gateway", "pid": 812, "rss_mib": 394}],
         "services_memory": {"twinspark-agent": {"current_mib": 96, "peak_mib": 140, "oom_kills": 0},
                             "twinspark-controller": {"current_mib": 210, "peak_mib": 265, "oom_kills": 0}},
         "owned_containers": {"running": 0, "current_mib": 0.0}, "runtime_mode": "docker"}


def plan(node="A", **kw):
    args = dict(spec=main_spec(), quant=Quantization.NVFP4, context_length=262144, concurrency=6,
                topology=Topology.TP2, node_id=node, gpu_memory_utilization=0.85, using_cuda_graphs=False,
                max_num_batched_tokens=8192, facts=SPARK)
    args.update(kw)
    return MemoryPlanner().plan_node(**args)


def bucket(p, key):
    return p.bucket(key)


def test_a_one_rank_drafter_is_counted_fully_on_both_nodes_until_a_timeline_confirms_where_it_is():
    dr = [DrafterInput("incoai/GLM-5.3-Flash-DFlash2", DRAFTER, 1, "dflash")]
    a, b = plan("A", drafters=dr), plan("B", drafters=dr)
    for p in (a, b):
        w = bucket(p, "drafter_weights")
        assert w.gib == pytest.approx(4.0) and w.source == "measured"
        assert "unconfirmed (counted on both nodes)" in w.note and p.drafter["confirmed"] is False
        assert any("draft_tensor_parallel_size 1" in n for n in p.notes)
        assert bucket(p, "drafter_scratch").phase == "outside"
    # the same drafter at the target's tensor parallel size is split, on a single node it is whole
    split = plan(drafters=[DrafterInput("x/d", DRAFTER, 2, "eagle")])
    assert bucket(split, "drafter_weights").gib == pytest.approx(2.0) and "split over both nodes" in \
        bucket(split, "drafter_weights").note
    single = plan(topology=Topology.SINGLE_A, drafters=dr)
    assert bucket(single, "drafter_weights").gib == pytest.approx(4.0) and "on this node" in single.drafter["placement"]
    # an MTP "drafter" lives in the main checkpoint: nothing extra
    assert bucket(plan(drafters=[DrafterInput("x/m", None, 1, "mtp")]), "drafter_weights") is None
    # the drafter takes KV room on both nodes
    assert a.one_context["kv_space_gib"] < plan("A").one_context["kv_space_gib"] - 3.9


def test_a_calibrated_timeline_confirms_the_drafter_on_one_node():
    dr = [DrafterInput("incoai/d", DRAFTER, 1, "dflash")]
    observed = {"nodes": {"A": {"footprint_gib": 104.2}, "B": {"footprint_gib": 100.1}}}
    a, b = plan("A", drafters=dr, observed=observed), plan("B", drafters=dr, observed=observed)
    assert bucket(a, "drafter_weights").gib == pytest.approx(4.0) and bucket(b, "drafter_weights").gib == 0
    assert bucket(a, "drafter_weights").source == "calibrated" and "node A only (calibrated" in a.drafter["placement"]
    # an ambiguous difference keeps it on both
    unsure = {"nodes": {"A": {"footprint_gib": 102.0}, "B": {"footprint_gib": 100.0}}}
    assert "unconfirmed" in plan("B", drafters=dr, observed=unsure).drafter["placement"]


def _old_single_verdict(spec, util, mem_total, ctx, conc):
    """What 0.5's _draft_fit called "fits": weights + one sequence inside vLLM's pool, nothing else."""
    p = MemoryPlanner()
    budget = p.estimate(spec=spec, quant=Quantization.NVFP4, context_length=ctx, concurrency=conc,
                        topology=Topology.TP2, using_cuda_graphs=False, mem_total_gib=mem_total)
    fixed = budget.vllm_needed - budget.kv_cache
    pool = util * budget.mem_total - fixed
    per_tok = budget.kv_cache * GIB / max(1, ctx * conc)
    return pool > 0 and pool * GIB >= per_tok * ctx


def test_startup_one_context_and_full_concurrency_are_separate_verdicts():
    """A desktop, a resident app and a drafter on node A: 0.5 said "fits", the startup peak does not."""
    spec = main_spec(80.0, kv_bytes_per_token=50000)
    facts = {**SPARK, "mem_available_gib": 96.0, "desktop_running": True, "desktop_rss_gib": 4.2,
             "desktop_processes": ["gnome-shell", "Xorg"],
             "other_processes": [{"name": "hermes-gateway", "pid": 812, "rss_mib": 394},
                                 {"name": "chrome", "pid": 900, "rss_mib": 6100}]}
    assert _old_single_verdict(spec, 0.88, 121.6, 262144, 6) is True
    p = plan(spec=spec, gpu_memory_utilization=0.88, facts=facts,
             drafters=[DrafterInput("incoai/d", DRAFTER, 1, "dflash")])
    assert p.startup["ok"] is False and p.startup["level"] == "CRITICAL" and p.startup["margin_gib"] < 0
    assert p.one_context["ok"] is True
    assert p.full_concurrency["ok"] is False and p.full_concurrency["level"] == "LOW"
    assert 1 <= p.full_concurrency["est_full_length_seqs"] < 6
    assert "does not fit" not in p.one_context["note"] and "vLLM serves fewer" in p.full_concurrency["note"]
    # startup peak and steady state are separate figures; the first prefill is the difference
    prefill = bucket(p, "first_prefill").gib
    assert p.steady_state["gib"] == pytest.approx(p.startup["peak_gib"] - prefill, abs=0.02)
    # every number is per node: nothing adds up the two Sparks
    assert p.mem_total_gib == 121.6 and p.vllm_claim_gib == pytest.approx(0.88 * 121.6)
    assert "256" not in str(p.as_dict())


def test_the_first_prefill_peak_scales_with_max_num_batched_tokens():
    small, large = plan(max_num_batched_tokens=4096), plan(max_num_batched_tokens=16384)
    assert bucket(large, "first_prefill").gib == pytest.approx(4 * bucket(small, "first_prefill").gib)
    assert bucket(small, "first_prefill").gib == pytest.approx(4096 * 6144 * 2 * ACT_BUFFERS / GIB)
    assert large.one_context["kv_space_gib"] < small.one_context["kv_space_gib"]
    default = plan(max_num_batched_tokens=None)
    assert f"{DEFAULT_BATCHED_TOKENS:,}" in bucket(default, "first_prefill").note
    unchunked = plan(max_num_batched_tokens=None, chunked_prefill=False, context_length=32768)
    assert "chunked prefill off" in bucket(unchunked, "first_prefill").note
    # without a hidden size the 0.5 rule is the fallback, still an estimate
    old = plan(spec=main_spec(hidden_size=0))
    assert "0.5's rule" in bucket(old, "first_prefill").note and bucket(old, "first_prefill").source == "estimated"


def test_measured_facts_replace_the_defaults_and_say_so():
    none = plan(facts={})
    by = {b.key: b for b in none.buckets}
    assert by["os_kernel_docker"].source == "estimated" and by["os_kernel_docker"].gib == pytest.approx(6.4)
    assert by["twinspark_services"].source == "estimated"
    assert by["desktop"].source == "unknown" and by["desktop"].gib is None
    assert by["other_apps"].source == "unknown" and by["other_apps"].gib is None
    assert none.mem_total_source == "estimated" and none.mem_total_gib == GB10_MEM_TOTAL_GIB
    assert none.startup["not_counted"] == ["Desktop session", "Other processes"]
    assert "not counted (unknown): Desktop session, Other processes" in none.startup["note"]

    real = plan()
    by = {b.key: b for b in real.buckets}
    assert {by[k].source for k in ("os_kernel_docker", "twinspark_services", "desktop", "other_apps")} == {"measured"}
    assert by["twinspark_services"].gib == pytest.approx(306 / 1024, abs=0.01)
    assert by["other_apps"].gib == pytest.approx(394 / 1024, abs=0.01)
    assert "hermes-gateway 394 MiB" in by["other_apps"].note
    used = 121.6 - 112.6
    assert by["os_kernel_docker"].gib == pytest.approx(used - (306 + 394) / 1024, abs=0.01)
    assert real.confidence["measured_inputs"] > none.confidence["measured_inputs"]
    assert real.confidence["text"].startswith("measured inputs: ")

    # a node read while a model served: its containers' memory is separated, or the default is used
    known = plan(facts={**SPARK, "mem_available_gib": 20.0, "owned_containers": {"running": 2, "current_mib": 92160}})
    assert known.bucket("os_kernel_docker").source == "measured"
    assert known.bucket("os_kernel_docker").gib == pytest.approx(101.6 - 90 - 700 / 1024, abs=0.02)
    unknown = plan(facts={**SPARK, "mem_available_gib": 20.0, "owned_containers": {"running": 2, "current_mib": None}})
    assert unknown.bucket("os_kernel_docker").source == "estimated" and "was running" in \
        unknown.bucket("os_kernel_docker").note

    # dry-run readings belong to the simulation host: estimates, with the simulated units
    dry = plan(facts={**SPARK, "runtime_mode": "dry-run", "other_processes": None,
                      "services_memory": {"twinspark-agent": {"current_mib": 96, "simulated": True}}})
    by = {b.key: b for b in dry.buckets}
    assert by["os_kernel_docker"].source == "estimated" and "dry-run" in by["os_kernel_docker"].note
    assert by["twinspark_services"].source == "estimated" and "simulated" in by["twinspark_services"].note
    assert by["other_apps"].source == "unknown"


def test_unknown_stays_unknown():
    p = plan(facts={}, drafters=[DrafterInput("incoai/not-pinned", None, 1, "dflash")])
    d = p.as_dict()
    w = next(b for b in d["buckets"] if b["key"] == "drafter_weights")
    assert w["gib"] is None and w["low"] is None and w["source"] == "unknown" and "pin the profile" in w["note"]
    assert "Drafter weights" in p.startup["not_counted"]
    assert "Drafter weights" in p.confidence["unknown"]
    # a model whose KV layout is unknown has no KV verdicts rather than "fits"
    nokv = plan(spec=ModelSpec(num_params=0, layers=0, num_kv_heads=0, head_dim=0, weight_bytes=10 * GIB))
    assert nokv.one_context["ok"] is None and nokv.one_context["level"] == "UNKNOWN"
    assert nokv.full_concurrency["est_kv_tokens"] is None
    assert nokv.bucket("kv_one_sequence").source == "unknown"


def test_observations_calibrate_startup_and_kv():
    observed = {"at": 1.0, "nodes": {"A": {"footprint_gib": 99.0, "min_mem_available_gib": 13.0,
                                           "min_stage": "loading"}}}
    p = plan(observed=observed, observed_kv_tokens=581040)
    resident = sum(b.gib for b in p.buckets if b.phase == "resident")
    assert p.startup["source"] == "calibrated" and p.startup["peak_gib"] == pytest.approx(resident + 99.0, abs=0.02)
    assert p.startup["estimate_gib"] != p.startup["peak_gib"] and "last startup" in p.startup["note"]
    assert p.full_concurrency["source"] == "calibrated" and p.full_concurrency["est_kv_tokens"] == 581040
    assert p.full_concurrency["est_full_length_seqs"] == pytest.approx(581040 / 262144, abs=0.05)
    assert p.full_concurrency["ok"] is False                     # 2.2 of 6 sequences: vLLM serves fewer
    assert plan(node="B", observed=observed).startup["source"] == "estimated"   # nothing observed on B


def test_eager_single_node_and_loading_buckets():
    p = plan(topology=Topology.SINGLE_A)
    assert p.bucket("comm_buffers").gib == 0 and p.bucket("cuda_graphs").gib == 0
    assert p.bucket("main_weights").gib == pytest.approx(60.0)
    graphs = plan(using_cuda_graphs=True)
    assert graphs.bucket("cuda_graphs").gib > 0
    loader = p.bucket("loader_transient")
    assert loader.phase == "load" and loader.gib == pytest.approx(60.0 / 48)
    # ranges: the pessimistic end is never below the estimate
    for b in p.buckets:
        if b.gib is not None:
            assert b.low <= b.gib <= b.high


def test_hf_config_carries_hidden_size_and_layers():
    spec = make_spec_from_hf({"hidden_size": 6144, "num_hidden_layers": 46, "num_attention_heads": 48,
                              "num_key_value_heads": 8}, weight_bytes=10 * GIB)
    assert spec.hidden_size == 6144 and spec.num_hidden_layers == 46 and spec.layers == 46
    # specs registered before 0.6 lack the new fields and still load
    old = ModelSpec(**{"num_params": 0, "layers": 30, "num_kv_heads": 4, "head_dim": 64, "weight_bytes": GIB})
    assert old.hidden_size == 0 and old.num_hidden_layers == 30


def test_drafter_inputs_come_from_extra_models_and_speculative_config():
    from twinspark.schemas.profile import AdvancedSettings
    specs = {"incoai/GLM-5.3-Flash-DFlash2": DRAFTER}
    adv = AdvancedSettings(extra_models=["incoai/GLM-5.3-Flash-DFlash2@" + "e" * 40, "org/tokenizer-only"],
                           speculative_config={"method": "dflash", "model": "incoai/GLM-5.3-Flash-DFlash2",
                                               "draft_tensor_parallel_size": 1})
    ins = drafter_inputs(adv, specs.get)
    assert [(d.repo, d.draft_tensor_parallel_size, d.method) for d in ins] == [
        ("incoai/GLM-5.3-Flash-DFlash2", 1, "dflash"), ("org/tokenizer-only", 1, None)]
    assert ins[0].spec is DRAFTER and ins[1].spec is None
    # a speculative model named only in speculative_config is a drafter too; a local path is not a repo
    only = drafter_inputs(AdvancedSettings(speculative_config={"model": "org/eagle", "method": "eagle3"}), specs.get)
    assert [d.repo for d in only] == ["org/eagle"] and only[0].draft_tensor_parallel_size is None
    assert drafter_inputs(AdvancedSettings(speculative_config={"model": "/models/x"}), specs.get) == []
