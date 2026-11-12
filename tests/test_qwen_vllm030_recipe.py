"""The Qwen3.8 stock-vLLM-0.30 recipe renders the published lane's launch, on both nodes."""

from __future__ import annotations

import json
import shlex

from twinspark.cookbook import RECIPES_DIR, build_draft
from twinspark.schemas.enums import Topology

from .conftest import draft

NAME = "qwen3.8-flash-next-nvfp4-tp2-vllm030"


def test_the_recipe_uses_the_stock_image_and_no_patches():
    d = build_draft(NAME)
    assert d.image_hint == "vllm/vllm-openai:v0.30.0"
    assert d.advanced.mods == [] and "mod_artifacts" not in d.source
    assert d.simple.topology == Topology.TP_EP and d.simple.context_length == 262144
    assert d.advanced.kv_dtype == "auto"            # v0.30's QSA attention takes only BF16 KV
    assert d.advanced.gpu_memory_utilization == 0.8   # 0.835 measured < 1 GiB MemAvailable
    assert d.source["url"] == "https://github.com/MiaAI-Lab/Qwen3.8-Flash-Next-Dual-DGX-Sparks"


def test_the_older_qwen_recipe_no_longer_runs_the_nodes_out_of_memory():
    raw = json.loads((RECIPES_DIR / "qwen3.8-flash-next-nvfp4-tp2.json").read_text(encoding="utf-8"))
    assert raw["advanced"]["gpu_memory_utilization"] <= 0.8
    assert any(NAME in n for n in raw["source"]["notes"])


async def test_the_plan_is_the_published_launch(cluster):
    c = cluster.controller
    c.config.runtime.health_timeout_s = 2400     # the shipped default (the test cluster waits 5 s)
    d = build_draft(NAME)
    d.identity = draft("qwen", Topology.TP_EP, repo=d.simple.model).identity
    d.name = "qwen"
    c.create_profile(d)
    rev = c.get_profile("qwen").add_revision(d, d.identity)
    plan = c.launch_plan(rev)
    cmds = plan.rendered_commands()
    assert len(cmds) == 2                        # one container per node
    for cmd in cmds.values():
        argv = shlex.split(cmd)
        joined = " ".join(argv)
        for flag in ("--tensor-parallel-size 2", "--enable-expert-parallel",
                     "--all2all-backend allgather_reducescatter", "--max-num-seqs 8",
                     "--max-num-batched-tokens 8192", "--max-model-len 262144", "--kv-cache-dtype auto",
                     "--mamba-ssm-cache-dtype bfloat16", "--load-format safetensors",
                     "--safetensors-load-strategy lazy", "--mm-encoder-tp-mode data",
                     "--distributed-executor-backend mp", "--gpu-memory-utilization 0.8"):
            assert flag in joined, (flag, joined)
        spec = json.loads(argv[argv.index("--speculative-config") + 1])
        assert spec == {"method": "mtp", "num_speculative_tokens": 3, "disable_eagle_block_drop": True,
                        "index_share_for_mtp_iteration": True}
        assert json.loads(argv[argv.index("--compilation-config") + 1]) == {"mode": 0,
                                                                             "cudagraph_mode": "FULL_DECODE_ONLY"}
        assert "VLLM_USE_BREAKABLE_CUDAGRAPH=0" in joined
        assert "VLLM_FLASHINFER_AUTOTUNE_CACHE_DIR=/tmp/fi_autotune" in joined
    head = next(cmd for cmd in cmds.values() if "--headless" not in cmd)
    assert "--tool-call-parser qwen3_coder" in head and "--reasoning-parser qwen3" in head
    assert "--enable-prompt-tokens-details" in head
    assert not any(n.startswith("WARNING") for n in plan.notes), plan.notes   # the ~11 min boot is covered
    assert plan.startup_limits["engine_ready_timeout_s"] == 1800
