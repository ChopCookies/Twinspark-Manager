import pytest
from pydantic import ValidationError

from twinspark.controller.launch import LaunchError, LaunchPlanner
from twinspark.controller.planner import MemoryPlanner, ModelSpec, make_spec_from_hf
from twinspark.schemas.enums import DistributedBackend, Quantization, Topology
from twinspark.schemas.profile import AdvancedSettings, ImmutableIdentity, Profile

from .conftest import DIGEST, SHA, draft


def rev_for(d):
    p = Profile(name=d.name)
    return p.add_revision(d, d.identity)


def test_mutable_refs_rejected():
    with pytest.raises(ValidationError):
        ImmutableIdentity(model_repo="o/m", model_revision="main", quantization="nvfp4",
                          image="x/y", image_digest=DIGEST, vllm_version="1")
    with pytest.raises(ValidationError):
        ImmutableIdentity(model_repo="o/m", model_revision=SHA, quantization="nvfp4",
                          image="x/y:latest", image_digest=DIGEST, vllm_version="1")


@pytest.mark.parametrize("flag", ["--tensor-parallel-size", "tensor_parallel_size", "--Port",
                                  "gpu_memory_utilization", "--api_key"])
def test_manager_owned_flags_rejected_in_any_spelling(flag):
    with pytest.raises(ValidationError):
        AdvancedSettings(extra_vllm_flags={flag: 1})


def test_split_topology_is_refused_explicitly():
    with pytest.raises(ValidationError):
        draft(topology=Topology.SPLIT)


def test_revision_ids_are_short_and_refs_resolve():
    p = Profile(name="qwen")
    d = draft()
    r1 = p.add_revision(d, d.identity)
    assert ":" not in r1.revision_id and len(r1.revision_id) < 30
    assert p.get_revision("r1") is r1 and p.get_revision("latest") is r1


def test_duplicate_renames_revisions():
    p = Profile(name="qwen")
    d = draft()
    p.add_revision(d, d.identity).known_good = True
    c = p.duplicate("qwen-copy")
    assert all(r.profile_name == "qwen-copy" and not r.known_good for r in c.revisions)
    assert c.revisions[0].revision_id != p.revisions[0].revision_id


def test_single_a_plan(controller_config):
    rev = rev_for(draft(topology=Topology.SINGLE_A))
    plan = LaunchPlanner(controller_config).plan(rev, 0.85)
    (c,) = plan.containers
    cmd = c.command
    assert cmd[:3] == ["vllm", "serve", "org/model-a"]
    assert cmd[cmd.index("--revision") + 1] == SHA
    assert cmd[cmd.index("--gpu-memory-utilization") + 1] == "0.85"
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--port") + 1] == "18100"
    assert c.image_ref.endswith(DIGEST)
    assert c.labels["org.twinspark.owned"] == "true"
    assert c.env["HF_HUB_OFFLINE"] == "1"
    assert c.env["VLLM_API_KEY"].startswith("${secret:")
    assert plan.routes == {"default": ["http://127.0.0.1:18100"]}
    # secrets redacted in the rendered command
    assert "***" in plan.rendered_commands()[c.name]


def test_tp2_native_plan(controller_config):
    rev = rev_for(draft(topology=Topology.TP2))
    plan = LaunchPlanner(controller_config).plan(rev, 0.8)
    head, worker = plan.containers
    assert (head.node, head.role, worker.node, worker.role) == ("A", "head", "B", "worker")
    for c, rank in ((head, "0"), (worker, "1")):
        cmd = c.command
        assert cmd[cmd.index("--tensor-parallel-size") + 1] == "2"
        assert cmd[cmd.index("--nnodes") + 1] == "2"
        assert cmd[cmd.index("--node-rank") + 1] == rank
        assert cmd[cmd.index("--master-addr") + 1] == "10.0.0.1"
        assert c.env["NCCL_SOCKET_IFNAME"] == "enp1s0f1np1"
    assert "--headless" in worker.command and "--port" not in worker.command
    assert worker.health_url is None and "VLLM_API_KEY" not in worker.env
    assert plan.backend == DistributedBackend.NATIVE


def test_pp2_and_ray(controller_config):
    d = draft(topology=Topology.PP2)
    d.distributed_backend = DistributedBackend.RAY
    plan = LaunchPlanner(controller_config).plan(rev_for(d), 0.8)
    head_script = plan.containers[0].command[2]
    assert "ray start --head" in head_script and "--pipeline-parallel-size 1" not in head_script
    assert "--pipeline-parallel-size 2" in head_script
    assert "--distributed-executor-backend ray" in head_script
    assert plan.start_order == [[plan.containers[0].name], [plan.containers[1].name]]


def test_tool_calling_needs_parser(controller_config):
    with pytest.raises(LaunchError):
        LaunchPlanner(controller_config).plan(rev_for(draft(tool_calling=True)), 0.8)


def test_behaviour_flags(controller_config):
    d = draft(topology=Topology.SINGLE_A, thinking=True, tool_calling=True)
    d.behaviour.reasoning_parser = "qwen3"
    d.behaviour.tool_call_parser = "hermes"
    d.behaviour.temperature = 0.6
    cmd = LaunchPlanner(controller_config).plan(rev_for(d), 0.8).containers[0].command
    assert cmd[cmd.index("--reasoning-parser") + 1] == "qwen3"
    assert "--enable-auto-tool-choice" in cmd
    assert cmd[cmd.index("--default-chat-template-kwargs") + 1] == '{"enable_thinking":true}'
    assert cmd[cmd.index("--override-generation-config") + 1] == '{"temperature":0.6}'


def test_missing_qsfp_ip_fails_loudly(controller_config):
    controller_config.nodes["B"].qsfp_ip = None
    with pytest.raises(LaunchError):
        LaunchPlanner(controller_config).plan(rev_for(draft(topology=Topology.TP2)), 0.8)


# ---- planner ---------------------------------------------------------------------
SPEC = ModelSpec(num_params=70 * 10**9, layers=80, num_kv_heads=8, head_dim=128)


def test_tp2_halves_weights_and_kv_replicated_does_not():
    p = MemoryPlanner()
    kw = dict(spec=SPEC, quant=Quantization.FP8, context_length=32768, concurrency=2)
    single = p.estimate(topology=Topology.SINGLE_A, **kw)
    tp2 = p.estimate(topology=Topology.TP2, **kw)
    rep = p.estimate(topology=Topology.REPLICATED, **kw)
    assert tp2.model_weights == pytest.approx(single.model_weights / 2)
    assert tp2.kv_cache == pytest.approx(single.kv_cache / 2)
    assert rep.model_weights == pytest.approx(single.model_weights)


def test_gpu_memory_utilization_leaves_room_for_the_os():
    p = MemoryPlanner()
    util = p.gpu_memory_utilization(p.reserve_budget())
    assert 0.5 < util <= 0.88
    assert p.gpu_memory_utilization(p.reserve_budget(headless=False)) <= util


def test_fp8_kv_halves_kv():
    p = MemoryPlanner()
    kw = dict(spec=SPEC, quant=Quantization.NVFP4, context_length=65536, concurrency=1,
              topology=Topology.SINGLE_A)
    assert p.estimate(kv_dtype="fp8", **kw).kv_cache == pytest.approx(p.estimate(**kw).kv_cache / 2)


def test_spec_from_hf_uses_head_dim_and_mla():
    spec = make_spec_from_hf({"hidden_size": 7168, "num_attention_heads": 128, "num_hidden_layers": 61,
                              "kv_lora_rank": 512, "qk_rope_head_dim": 64, "n_routed_experts": 256,
                              "head_dim": 192}, weight_bytes=400 * 1024**3)
    assert spec.is_mla and spec.is_moe and spec.head_dim == 192
    with pytest.raises(ValueError):
        make_spec_from_hf({"hidden_size": 10, "num_attention_heads": 2, "num_hidden_layers": 1})
