"""The engine interface (master plan §3.1): SGLang as a second engine, and the image probe that runs
before the old model is stopped. No Docker: plans are inspected, probes come from the dry-run runtime."""

from __future__ import annotations

import shlex
from types import SimpleNamespace

import pytest

from twinspark.agent import probe
from twinspark.controller import imagecheck
from twinspark.controller.launch import LaunchError, LaunchPlanner
from twinspark.controller.planner import ModelSpec, make_spec_from_hf
from twinspark.controller.store import Store
from twinspark.metrics import _parse_text, derive
from twinspark.schemas.enums import Topology

from .conftest import DIGEST, SHA, draft
from .test_schemas_and_plan import rev_for


def sglang(topology=Topology.SINGLE_A, **advanced):
    d = draft(topology=topology, tool_calling=True)
    d.engine = "sglang"
    d.behaviour.tool_call_parser = "qwen25"
    d.behaviour.reasoning_parser = "qwen3"
    d.advanced.kv_dtype = "fp8"
    d.advanced.max_num_batched_tokens = 8192
    for k, v in advanced.items():
        setattr(d.advanced, k, v)
    return rev_for(d)


def inner(cmd):
    assert cmd[:2] == ["bash", "-c"]
    return imagecheck.engine_argv(cmd)


def test_an_sglang_profile_is_served_by_sglang_with_the_same_settings(controller_config):
    plan = LaunchPlanner(controller_config).plan(sglang(), 0.8)
    (c,) = plan.containers
    argv = inner(c.command)
    assert argv[:3] == ["python3", "-m", "sglang.launch_server"]
    val = lambda f: argv[argv.index(f) + 1]   # noqa: E731
    assert val("--model-path") == "org/model-a" and val("--revision") == SHA
    assert val("--mem-fraction-static") == "0.8" and val("--context-length") == "32768"
    assert val("--kv-cache-dtype") == "fp8_e4m3" and val("--chunked-prefill-size") == "8192"
    assert val("--tool-call-parser") == "qwen25" and val("--reasoning-parser") == "qwen3"
    assert val("--tp-size") == "1" and val("--host") == "127.0.0.1" and "--enable-metrics" in argv
    # the backend key reaches SGLang as a flag, from the env the agent fills; never in the plan
    assert argv[-2:] == ["--api-key", "$VLLM_API_KEY"] and c.env["VLLM_API_KEY"] == "${secret:backend_api_key}"
    assert "vllm" not in shlex.join(argv).replace("VLLM_API_KEY", "")
    assert any("SGLang" in n for n in plan.notes)


def test_sglang_on_two_nodes_uses_its_own_launcher(controller_config):
    plan = LaunchPlanner(controller_config).plan(sglang(Topology.TP2), 0.8)
    head = next(c for c in plan.containers if c.role == "head")
    worker = next(c for c in plan.containers if c.role == "worker")
    h = inner(head.command)
    assert h[h.index("--tp-size") + 1] == "2" and h[h.index("--node-rank") + 1] == "0"
    assert h[h.index("--dist-init-addr") + 1] == "10.0.0.1:29501" and h[h.index("--nnodes") + 1] == "2"
    w = worker.command                                         # no API on rank 1: no key, no host
    assert w[w.index("--node-rank") + 1] == "1" and "--host" not in w and "--api-key" not in w
    assert plan.start_order == [[worker.name], [head.name]]
    ep = LaunchPlanner(controller_config).plan(sglang(Topology.TP_EP), 0.8)
    assert "--ep-size" in inner(next(c for c in ep.containers if c.role == "head").command)


def test_sglang_maps_speculative_methods_and_refuses_what_it_cannot_do(controller_config):
    plan = LaunchPlanner(controller_config).plan(
        sglang(speculative_config={"method": "eagle3", "model": "org/draft", "num_speculative_tokens": 3}), 0.8)
    argv = inner(plan.containers[0].command)
    assert argv[argv.index("--speculative-algorithm") + 1] == "EAGLE3"
    assert argv[argv.index("--speculative-draft-model-path") + 1] == "org/draft"
    assert argv[argv.index("--speculative-num-draft-tokens") + 1] == "4"
    with pytest.raises(LaunchError, match="no SGLang equivalent"):
        LaunchPlanner(controller_config).plan(sglang(speculative_config={"method": "medusa"}), 0.8)
    with pytest.raises(LaunchError, match="mods patch vLLM"):
        LaunchPlanner(controller_config).plan(sglang(mods=["fix-a"]), 0.8)


def test_the_engine_is_part_of_the_measurement_fingerprint():
    from twinspark.controller.measurements import kv_fingerprint
    a, b = draft(), draft()
    b.engine = "sglang"
    assert kv_fingerprint(a, a.identity, "A") != kv_fingerprint(b, b.identity, "A")


def test_sglang_metrics_and_kv_pool_are_understood():
    from twinspark.controller.jobs import kv_facts_from_log
    v = _parse_text('sglang:num_running_reqs{model_name="m"} 3\nsglang:num_queue_reqs{model_name="m"} 1\n'
                    'sglang:token_usage{model_name="m"} 0.4\nsglang:cache_hit_rate{model_name="m"} 0.25\n'
                    'sglang:generation_tokens_total{model_name="m"} 100\n')
    d = derive(v)
    assert v["requests-running"] == 3 and v["requests-waiting"] == 1 and v["gen-tokens-total"] == 100
    assert d["kv_cache_usage_pct"] == 40.0 and d["prefix_hit_rate_pct"] == 25.0
    kv = kv_facts_from_log("KV Cache is allocated. #tokens: 1,000,000, K size: 10.00 GB, V size: 10.00 GB")
    assert kv == {"kv_cache_tokens": 1_000_000, "kv_memory_gib": 18.63}


def test_the_architecture_comes_from_config_json():
    spec = make_spec_from_hf({"architectures": ["Qwen3MoeForCausalLM"], "hidden_size": 2048,
                              "num_attention_heads": 16, "num_hidden_layers": 4}, weight_bytes=10 ** 9)
    assert spec.architectures == ("Qwen3MoeForCausalLM",)


# ---- the probe --------------------------------------------------------------------------------------
def test_probe_output_is_parsed_and_bounded():
    good = probe.parse("noise\nTWINSPARK-PROBE " + '{"engine": "vllm", "version": "0.11.0", "flags": ["a"], '
                       '"tool_parsers": ["hermes"], "errors": {"architectures": "ImportError: x"}, "junk": 1}')
    assert good == {"engine": "vllm", "version": "0.11.0", "flags": ["a"], "tool_parsers": ["hermes"],
                    "errors": {"architectures": "ImportError: x"}}
    assert "no probe result" in probe.parse("Traceback ...")["error"]
    assert "not JSON" in probe.parse("TWINSPARK-PROBE {nope")["error"]
    for script in probe.SCRIPTS.values():
        compile(script, "<probe>", "exec")                       # the scripts are valid Python


CAPS = {"engine": "vllm", "version": "0.11.0",
        "flags": ["revision", "served-model-name", "max-model-len", "gpu-memory-utilization", "max-num-seqs",
                  "kv-cache-dtype", "host", "port", "tensor-parallel-size", "enable-auto-tool-choice",
                  "tool-call-parser", "reasoning-parser", "default-chat-template-kwargs", "enable-prefix-caching"],
        "tool_parsers": ["hermes", "qwen3_coder"], "reasoning_parsers": ["qwen3", "deepseek_r1"],
        "speculative_methods": ["eagle3", "mtp", "ngram"], "architectures": ["Qwen3ForCausalLM"]}


def test_compare_names_what_the_image_lacks():
    req = {"flags": ["revision", "max-model-len", "speculative-config"], "tool_parser": "glm47",
           "reasoning_parser": "qwen3", "speculative_method": "dflash", "architectures": ["GlmForCausalLM"]}
    problems = imagecheck.compare(CAPS, req)
    assert any("--speculative-config" in p for p in problems)
    assert any("tool-call parser 'glm47'" in p and "hermes" in p for p in problems)
    assert any("speculative method 'dflash'" in p for p in problems)
    assert any("GlmForCausalLM" in p for p in problems)
    assert not any("reasoning" in p for p in problems)
    assert imagecheck.compare({"error": "x"}, req) == []               # nothing known, nothing judged


def coordinator(controller_config, tmp_path, rev, caps, *, policy="enforce", free=60.0):
    calls = []

    class Agent:
        async def call(self, action, **kw):
            if action == "memory_telemetry":
                return {"mem_available_gib": free}
            calls.append((action, kw))
            return caps
    controller_config.runtime.image_check = policy
    plan = LaunchPlanner(controller_config).plan(rev, 0.8)
    coord = SimpleNamespace(config=controller_config, dry_run=False, plan=plan, store=Store(str(tmp_path / "s.db")),
                            model_specs={"org/model-a": ModelSpec(num_params=1, layers=1, num_kv_heads=1, head_dim=1,
                                                                  architectures=("Qwen3ForCausalLM",))},
                            model_spec=None, persist=lambda job: None, agent=lambda node: Agent())
    return coord, calls


async def test_a_switch_is_refused_before_anything_stops_when_the_image_lacks_a_parser(controller_config, tmp_path):
    d = draft(topology=Topology.SINGLE_A, tool_calling=True)
    d.behaviour.tool_call_parser = "glm47"
    rev = rev_for(d)
    coord, calls = coordinator(controller_config, tmp_path, rev, CAPS)
    step = SimpleNamespace(message="")
    with pytest.raises(RuntimeError, match="no tool-call parser 'glm47'.*nothing was stopped"):
        await imagecheck.check_plan(coord, None, rev, step)
    assert calls[0] == ("image_probe", {"image_ref": f"nvcr.io/nvidia/vllm@{DIGEST}", "engine": "vllm",
                                        "timeout": 300})
    # the answer is kept per image digest: the next check does not probe again
    with pytest.raises(RuntimeError):
        await imagecheck.check_plan(coord, None, rev, step)
    assert len(calls) == 1
    # warn: the switch goes on with a warning; mods: always only a warning
    coord, _ = coordinator(controller_config, tmp_path / "w", rev, CAPS, policy="warn")
    (tmp_path / "w").mkdir(exist_ok=True)
    assert "glm47" in (await imagecheck.check_plan(coord, None, rev, step))[0]


async def test_an_image_that_can_serve_the_profile_passes_and_a_broken_probe_only_warns(controller_config, tmp_path):
    d = draft(topology=Topology.SINGLE_A, tool_calling=True)
    d.behaviour.tool_call_parser = "hermes"
    d.behaviour.reasoning_parser = "qwen3"
    rev = rev_for(d)
    coord, _ = coordinator(controller_config, tmp_path, rev, CAPS)
    assert await imagecheck.check_plan(coord, None, rev, SimpleNamespace(message="")) == []
    (tmp_path / "b").mkdir()
    coord, _ = coordinator(controller_config, tmp_path / "b", rev, {"error": "docker: no such image"})
    warns = await imagecheck.check_plan(coord, None, rev, SimpleNamespace(message=""))
    assert len(warns) == 1 and "could not check" in warns[0]
    (tmp_path / "m").mkdir()
    coord, calls = coordinator(controller_config, tmp_path / "m", rev, CAPS, free=4.0)   # a big model is serving
    warns = await imagecheck.check_plan(coord, None, rev, SimpleNamespace(message=""))
    assert calls == [] and "only 4.0 GiB free" in warns[0]


async def test_the_agent_runs_only_its_own_probe_script(cluster):
    a = cluster.controller.agents["A"]
    cluster.runtimes["A"].probe_result = {"engine": "sglang", "version": "0.5.2", "flags": ["model-path"]}
    out = await a.call("image_probe", image_ref=f"lmsysorg/sglang@{DIGEST}", engine="sglang")
    assert out == {"engine": "sglang", "version": "0.5.2", "flags": ["model-path"]}
    assert cluster.runtimes["A"].history[-1][:4] == ["docker", "run", "--rm", "--network"]
    from twinspark.controller.agent_client import AgentActionError
    with pytest.raises(AgentActionError, match="pinned by digest"):
        await a.call("image_probe", image_ref="lmsysorg/sglang:latest", engine="sglang")
    with pytest.raises(AgentActionError, match="unknown engine"):
        await a.call("image_probe", image_ref=f"x/y@{DIGEST}", engine="bash")
    cluster.runtimes["A"].probe_result = None
    assert await a.call("image_probe", image_ref=f"x/y@{DIGEST}", engine="vllm") == {"simulated": True}
