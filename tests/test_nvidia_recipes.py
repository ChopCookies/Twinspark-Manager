"""NVIDIA's DGX Spark recipes (NVIDIA/local-ai-inference-recipes): listing them, importing them, and the bundled
Qwen3.8-27B port. The recipes below are written for these tests in NVIDIA's format; they are not NVIDIA's files."""

from __future__ import annotations

import json
import shlex

import httpx
import pytest

from twinspark.cookbook import RecipeImportError, build_draft, import_text
from twinspark.cookbook.remote import RemoteError, list_source, parse_source
from twinspark.schemas.config import DEFAULT_RECIPE_SOURCES, ControllerConfig
from twinspark.schemas.enums import RuntimeAdapter, Topology, VerificationStatus

from .conftest import draft

REF = "https://raw.githubusercontent.com/NVIDIA/local-ai-inference-recipes/HEAD/models/org/m/dgx-spark-2gpu/recipe.yaml"

SERVE = """\
    export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
    export VLLM_USE_V2_MODEL_RUNNER="1"   # needed since the October builds

    vllm serve org/Model-30B-NVFP4 \\
      --host 0.0.0.0 \\
      --port 8000 \\
      --trust-remote-code \\
      --kv-cache-dtype fp8 \\
      --gpu-memory-utilization 0.8 \\
      --max-model-len 131072 \\
      --max-num-seqs 4 \\
      --enable-prefix-caching \\
      --speculative-config '{"method":"dflash","model": "lab/Model-30B-Draft", "num_speculative_tokens":6}' \\
      --reasoning-parser qwen3 \\
      --tool-call-parser qwen3_xml \\
      --enable-auto-tool-choice \\
      --async-scheduling \\
      --tensor-parallel-size 2 \\
      --nnodes 2 \\
      --node-rank $NODE_RANK \\
      --master-addr "$HEAD_IP" \\
      --master-port "$MASTER_PORT"
"""


def indent(text: str, n: int) -> str:
    return "\n".join((" " * n + line) if line.strip() else "" for line in text.splitlines())


def recipe(hardware="dgx-spark-2gpu", serve=SERVE, worker=None, backend="vllm", image="vllm/vllm-openai:v0.31.0",
           status="optimized", nodes=2) -> str:
    worker = worker if worker is not None else serve.rstrip() + " \\\n      --headless\n"
    workers = f"""
  additional_commands:
    -
      id: worker
      target: workers
      command: |
{indent(worker, 6)}""" if nodes > 1 else ""
    return f"""\
schema_version: 4
title: org/Model-30B · 2× DGX Spark · vLLM
model: org/model-30b
hardware: {hardware}
platform:
  os: linux
  execution: container
backend: {backend}
backend_version: "0.31.0"
status: {status}
validation:
  validated_at: "2026-10-30"
limitations:
  - Measured with one request at a time.
precision:
  label: NVFP4
checkpoint:
  target: https://huggingface.co/org/Model-30B-NVFP4
  target_label: org/Model-30B-NVFP4
  draft: https://huggingface.co/lab/Model-30B-Draft
  draft_label: lab/Model-30B-Draft
topology:
  node_count: {nodes}
  tensor_parallel_size: {nodes}
runtime:
  kind: container
  label: Container (community, mutable, ARM64)
  image: {image}
  setup_command: |
    # Example values:
    #   ETH_IF="enp1s0f0np0"
    export ETH_IF="<connectx-interface>"
    docker run --rm -it \\
      --gpus all \\
      -e HF_TOKEN=$HF_TOKEN \\
      -e NCCL_SOCKET_IFNAME="$ETH_IF" \\
      -e NCCL_IB_HCA="<roce>" \\
      -e NCCL_IB_DISABLE=0 \\
      -e NCCL_BUFFSIZE=1048576 \\
      -e VLLM_FLOAT32_MATMUL_PRECISION=high \\
      -e NODE_RANK="$NODE_RANK" \\
      --entrypoint /bin/bash \\
      "$IMAGE"
serve:
  label: Launch head
  target: head
  command: |
{indent(serve, 4)}
  port: 8000{workers}
notes: Start the worker before the head.
"""


def test_a_two_spark_recipe_becomes_a_tp2_profile_with_twinsparks_wiring():
    d, report = import_text(recipe(), source_ref=REF)
    s, a, b = d.simple, d.advanced, d.behaviour
    assert report["format"] == "nvidia" and d.source["receipt"]["format"] == "nvidia"
    assert s.model == "org/Model-30B-NVFP4" and s.topology == Topology.TP2 and s.quantization.value == "nvfp4"
    assert s.context_length == 131072 and a.max_num_seqs == 4 and a.kv_dtype == "fp8" and a.prefix_cache
    assert a.gpu_memory_utilization == 0.8 and a.trust_remote_code
    assert (b.reasoning_parser, b.tool_call_parser, s.tool_calling) == ("qwen3", "qwen3_xml", True)
    assert d.image_hint == "vllm/vllm-openai:v0.31.0" and d.runtime_adapter == RuntimeAdapter.CUSTOM
    assert d.verification == VerificationStatus.COMMUNITY and d.tags == ["nvidia", "imported"]
    assert d.name == "model-30b-nvfp4-tp2"
    # exports win over the docker run; tuning comes along, the pair's wiring and the token do not
    assert a.env == {"NCCL_BUFFSIZE": "1048576", "VLLM_FLOAT32_MATMUL_PRECISION": "high",
                     "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True", "VLLM_USE_V2_MODEL_RUNNER": "1"}
    assert not {"NCCL_SOCKET_IFNAME", "NCCL_IB_HCA", "NCCL_IB_DISABLE", "HF_TOKEN", "NODE_RANK"} & set(a.env)
    # the per-node flags are TwinSpark's; the rest stays, in order
    assert a.extra_vllm_args == ["--async-scheduling"]
    assert not any("$" in x for x in a.extra_vllm_args)
    for gone in ("--nnodes 2", "--node-rank $NODE_RANK", '--master-addr "$HEAD_IP"', "--port 8000"):
        assert any(x.startswith(gone.split()[0]) for x in report["dropped"]), (gone, report["dropped"])
    # the drafter is a second checkpoint: staged and pinned with the model
    assert a.speculative_config == {"method": "dflash", "model": "lab/Model-30B-Draft", "num_speculative_tokens": 6}
    assert a.extra_models == ["lab/Model-30B-Draft"]
    src = d.source
    assert src["url"] == REF.replace("raw.githubusercontent.com", "github.com").replace(
        "/local-ai-inference-recipes/", "/local-ai-inference-recipes/blob/")
    assert src["validated_at"] == "2026-10-30" and src["hardware"] == "dgx-spark-2gpu"
    assert "Measured with one request at a time." in src["notes"]
    assert any("mutable" in n for n in src["notes"])
    assert any("NCCL_BUFFSIZE=1048576" in n for n in report["notes"])
    assert any("NCCL_SOCKET_IFNAME" in n and "left to TwinSpark" in n for n in report["notes"])


async def test_the_imported_profile_plans_both_ranks(cluster):
    d, _ = import_text(recipe(), source_ref=REF)
    d.identity = draft("m", Topology.TP2, repo=d.simple.model).identity
    c = cluster.controller
    c.create_profile(d)
    rev = c.get_profile(d.name).add_revision(d, d.identity)
    cmds = c.launch_plan(rev).rendered_commands()
    assert len(cmds) == 2
    for cmd in cmds.values():
        joined = " ".join(shlex.split(cmd))
        assert "--tensor-parallel-size 2" in joined and "--async-scheduling" in joined
        assert "NCCL_BUFFSIZE=1048576" in joined and "$" not in joined.replace("$$", "")


def test_a_single_spark_recipe_with_assignments_before_the_command():
    serve = """\
    VLLM_FLOAT32_MATMUL_PRECISION="high" \\
    VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \\
    vllm serve org/Model-30B-NVFP4 \\
        --host 0.0.0.0 \\
        --max-model-len 262144 \\
        --speculative-config '{"method":"mtp","num_speculative_tokens":3}'
"""
    d, report = import_text(recipe(hardware="dgx-spark", serve=serve, nodes=1, image="nvcr.io/nvidia/vllm:26.10-py3"))
    assert d.simple.topology == Topology.SINGLE_A and d.name == "model-30b-nvfp4"
    assert d.advanced.env["VLLM_ALLOW_LONG_MAX_MODEL_LEN"] == "1"
    assert d.advanced.env["VLLM_FLOAT32_MATMUL_PRECISION"] == "high"
    assert d.runtime_adapter == RuntimeAdapter.NGC and d.advanced.extra_models == []
    # the recipe names a drafter its command does not load: said, not guessed
    assert any("lab/Model-30B-Draft" in n and "does not load it" in n for n in report["notes"])


def test_a_worker_that_differs_from_the_head_is_reported():
    worker = SERVE.replace("--max-num-seqs 4", "--max-num-seqs 2").rstrip() + " \\\n      --headless\n"
    _, report = import_text(recipe(worker=worker))
    assert any("worker's command differs" in n for n in report["notes"])
    _, report = import_text(recipe())
    assert not any("worker's command differs" in n for n in report["notes"])


@pytest.mark.parametrize("change, message", [
    ({"hardware": "dgx-spark-4gpu"}, "dgx-spark-4gpu"),
    ({"backend": "llama-cpp"}, "vLLM recipes"),
    ({"serve": SERVE.replace("    export PYTORCH", "    pip install foo\n    export PYTORCH")}, "only `export"),
    ({"serve": SERVE.replace("--async-scheduling", '--served-model-name x --kv-cache-memory-bytes "$KV"')},
     "without a shell"),
    ({"serve": SERVE.replace('VLLM_USE_V2_MODEL_RUNNER="1"', 'VLLM_USE_V2_MODEL_RUNNER="$ON"')}, "shell variable"),
    ({"serve": SERVE.replace("--tensor-parallel-size 2", "--tensor-parallel-size 1")}, "splits the model"),
    ({"image": "Not An Image"}, "container image"),
])
def test_recipes_a_pair_cannot_run_are_refused_with_the_reason(change, message):
    with pytest.raises(RecipeImportError, match=message):
        import_text(recipe(**change))


def test_eugr_recipes_stage_their_drafter_too():
    text = """\
name: Drafted
container: vllm-node
command: |
  vllm serve org/Target-8B \\
    --speculative-config '{{"method":"mtp","model":"lab/Target-8B-assistant","num_speculative_tokens":4}}' \\
    --tensor-parallel-size 2
"""
    d, report = import_text(text)
    assert d.advanced.extra_models == ["lab/Target-8B-assistant"]
    assert any("lab/Target-8B-assistant" in n for n in report["notes"])
    dotted = """\
name: Dotted
container: vllm-node
command: |
  vllm serve org/Target-8B \\
    --speculative_config.method dflash \\
    --speculative_config.model lab/Target-8B-DFlash \\
    --speculative_config.num_speculative_tokens 7
"""
    d, _ = import_text(dotted)
    assert d.advanced.speculative_config == {"method": "dflash", "model": "lab/Target-8B-DFlash",
                                             "num_speculative_tokens": 7}
    assert d.advanced.extra_models == ["lab/Target-8B-DFlash"]
    assert not any("speculative" in x for x in d.advanced.extra_vllm_args)


def test_a_pattern_source_lists_a_whole_repository_with_one_request():
    seen = []
    tree = [
        {"path": "models/qwen/q27b/dgx-spark-2gpu/linux/vllm/128gb/nvfp4/recipe.yaml", "type": "blob",
         "size": 4000, "sha": "a" * 40},
        {"path": "models/qwen/q27b/dgx-spark-2gpu/linux/vllm/128gb/nvfp4/performance.yaml", "type": "blob"},
        {"path": "models/glm/g/dgx-spark-2gpu/linux/vllm/fp4-local/recipe.yaml", "type": "blob", "size": 3000},
        {"path": "models/ds/d/dgx-spark-4gpu/linux/vllm/fp4/recipe.yaml", "type": "blob"},
        {"path": "models/qwen/q27b/dgx-spark/linux/vllm/128gb/nvfp4/recipe.yaml", "type": "blob"},
        {"path": "models/qwen/q27b/dgx-spark-2gpu/linux/llama-cpp/q4/recipe.yaml", "type": "blob"},
        {"path": "models/qwen/q27b/dgx-spark-2gpu/linux/vllm", "type": "tree"},
        {"path": "models/x/../../etc/dgx-spark-2gpu/linux/vllm/recipe.yaml", "type": "blob"},
    ]

    def serve(request: httpx.Request):
        seen.append(str(request.url))
        assert request.url.path == "/repos/NVIDIA/local-ai-inference-recipes/git/trees/HEAD"
        assert request.url.params["recursive"] == "1"
        return httpx.Response(200, json={"sha": "f" * 40, "tree": tree, "truncated": False})

    src = DEFAULT_RECIPE_SOURCES[1]
    assert parse_source(src)[2] == "models/*/dgx-spark-2gpu/linux/vllm/*recipe.yaml"
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        out = list_source(src, client, refresh=True)
        assert out["kind"] == "tree" and not out["truncated"]
        assert [f["title"] for f in out["files"]] == ["glm/g/dgx-spark-2gpu/linux/vllm/fp4-local",
                                                      "qwen/q27b/dgx-spark-2gpu/linux/vllm/128gb/nvfp4"]
        q = out["files"][1]
        assert q["download_url"] == ("https://raw.githubusercontent.com/NVIDIA/local-ai-inference-recipes/HEAD/"
                                     "models/qwen/q27b/dgx-spark-2gpu/linux/vllm/128gb/nvfp4/recipe.yaml")
        assert q["url"].startswith("https://github.com/NVIDIA/local-ai-inference-recipes/blob/HEAD/models/qwen/")
        assert q["size"] == 4000 and q["name"] == "recipe.yaml"
        list_source(src, client)                                  # cached
        assert len(seen) == 1
        pinned = list_source("NVIDIA/local-ai-inference-recipes:models/*/recipe.yaml@main", httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(
                200 if r.url.path.endswith("/trees/main") else 404, json={"tree": tree}))), refresh=True)
        assert all("/main/" in f["download_url"] for f in pinned["files"]) and len(pinned["files"]) == 5


@pytest.mark.parametrize("src", ["NVIDIA/local-ai-inference-recipes:../*/recipe.yaml",
                                 "NVIDIA/local-ai-inference-recipes:models/../*.yaml",
                                 "NVIDIA/local-ai-inference-recipes:models/*.yaml@../../../users"])
def test_a_pattern_cannot_leave_the_repository(src):
    with pytest.raises(RemoteError, match="inside the repository"):
        list_source(src, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))), refresh=True)


def test_nvidias_two_spark_recipes_are_a_default_source():
    assert ControllerConfig.model_fields["recipe_sources"].default_factory() == list(DEFAULT_RECIPE_SOURCES)
    assert DEFAULT_RECIPE_SOURCES[0] == "eugr/spark-vllm-docker:recipes"


# ---- the bundled port of NVIDIA's Qwen3.8-27B two-Spark recipe ---------------------------------------------
NAME = "qwen3.8-27b-nvfp4-dflash2-tp2"


def test_the_qwen_27b_recipe_is_nvidias_on_the_stock_image():
    d = build_draft(NAME)
    a = d.advanced
    assert d.image_hint == "vllm/vllm-openai:v0.31.0" and a.mods == []
    assert d.simple.topology == Topology.TP2 and d.simple.context_length == 262144
    assert a.gpu_memory_utilization == 0.8 and a.kv_dtype == "fp8" and a.max_num_batched_tokens == 16384
    assert a.speculative_config == {"method": "dflash", "model": "z-lab/Qwen3.8-27B-DFlash2",
                                    "num_speculative_tokens": 8, "draft_tensor_parallel_size": 2}
    assert a.extra_models == ["z-lab/Qwen3.8-27B-DFlash2"]       # staged and pinned with the model
    assert d.source["url"].startswith("https://github.com/NVIDIA/local-ai-inference-recipes/")
    text_only = build_draft(NAME, mode="text-only").advanced.extra_vllm_flags
    assert text_only == {"async-scheduling": True, "language-model-only": True}


async def test_the_qwen_27b_plan_is_nvidias_launch(cluster):
    c = cluster.controller
    d = build_draft(NAME)
    d.identity = draft("q", Topology.TP2, repo=d.simple.model).identity
    d.name = "q27"
    c.create_profile(d)
    rev = c.get_profile("q27").add_revision(d, d.identity)
    cmds = c.launch_plan(rev).rendered_commands()
    assert len(cmds) == 2
    for cmd in cmds.values():
        argv = shlex.split(cmd)
        joined = " ".join(argv)
        for flag in ("--tensor-parallel-size 2", "--kv-cache-dtype fp8", "--gpu-memory-utilization 0.8",
                     "--max-model-len 262144", "--max-num-seqs 8", "--max-num-batched-tokens 16384",
                     "--enable-chunked-prefill", "--enable-prefix-caching", "--load-format safetensors",
                     "--async-scheduling", "--trust-remote-code"):
            assert flag in joined, (flag, joined)
        spec = json.loads(argv[argv.index("--speculative-config") + 1])
        assert spec["method"] == "dflash" and spec["model"] == "z-lab/Qwen3.8-27B-DFlash2"
        assert "VLLM_USE_V2_MODEL_RUNNER=1" in joined and "VLLM_FLOAT32_MATMUL_PRECISION=high" in joined
    head = next(cmd for cmd in cmds.values() if "--headless" not in cmd)
    assert "--tool-call-parser qwen3_xml" in head and "--reasoning-parser qwen3" in head


@pytest.mark.parametrize("bad", [
    "additional_commands: 5", "additional_commands: {worker: x}", "additional_commands: [1, 'x', null]",
])
def test_odd_shapes_in_a_recipe_are_ignored_not_a_crash(bad):
    text = recipe(nodes=1, hardware="dgx-spark", serve=SERVE.replace("--tensor-parallel-size 2", "")
                  .replace("--nnodes 2", "")).replace("  port: 8000", f"  port: 8000\n  {bad}")
    text = text.replace("limitations:\n  - Measured with one request at a time.", "limitations: 7")
    d, _ = import_text(text)
    assert d.simple.topology == Topology.SINGLE_A and d.source.get("notes") is not None


def test_the_import_route_previews_an_nvidia_recipe(cluster):
    from fastapi.testclient import TestClient

    from twinspark.controller.app import create_app

    h = {"x-api-key": "k"}
    with TestClient(create_app(cluster.controller, "k", run_startup=False)) as client:
        r = client.post("/api/v1/cookbook/import-recipe", headers=h,
                        json={"text": recipe(), "preview": True, "profile_name": "m30"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["preview"] and body["report"]["format"] == "nvidia"
        assert body["draft"]["advanced"]["extra_models"] == ["lab/Model-30B-Draft"]
        assert cluster.controller.get_profile("m30") is None
        r = client.post("/api/v1/cookbook/import-recipe", headers=h, json={"text": recipe(hardware="dgx-spark-8gpu")})
        assert r.status_code == 422 and "dgx-spark-8gpu" in r.json()["detail"]
