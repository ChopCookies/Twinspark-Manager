"""Recipe errors must be actionable and must never create a changed experiment."""

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from twinspark.controller.app import create_app
from twinspark.cookbook import RecipeImportError, import_text, recipe_detail


@pytest.mark.parametrize("field,value", [
    ("command", ["vllm", "serve", "org/model"]),
    ("defaults", "ctx=8192"),
    ("defaults", []),
    ("env", ["FLAG=1"]),
    ("env", "FLAG=1"),
    ("mods", "mods/fix"),
    ("build_args", [123]),
    ("quantization", 4),
    ("command", "vllm serve org/model --max-model-len {}"),
    ("command", "vllm serve org/model --max-model-len {ctx.missing}"),
])
def test_malformed_recipe_returns_validation_error_without_creating_profile(cluster, field, value):
    doc = {"name": "invalid", "command": "vllm serve org/model", "defaults": {"ctx": 8192}}
    doc[field] = value
    with TestClient(create_app(cluster.controller, "key", run_startup=False)) as client:
        response = client.post("/api/v1/cookbook/import-recipe", headers={"x-api-key": "key"},
                               json={"text": yaml.safe_dump(doc)})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert cluster.controller.list_profiles() == []


@pytest.mark.parametrize("source", [None, [], "community"])
def test_invalid_twinspark_source_is_a_recipe_error(source):
    text = json.dumps({"name": "test", "simple": {"model": "org/model"}, "source": source})
    with pytest.raises(RecipeImportError, match="not a valid TwinSpark recipe"):
        import_text(text, source_ref="pasted")


@pytest.mark.parametrize("flag,value", [
    ("tensor-parallel-size", "0"),
    ("pipeline-parallel-size", "-2"),
    ("data-parallel-size", "0"),
    ("distributed-executor-backend", "external_launcher"),
    ("distributed-executor-backend", ""),
    ("gpu-memory-utilization", "oops"),
    ("gpu-memory-utilization", "nan"),
    ("gpu-memory-utilization", "inf"),
    ("gpu-memory-utilization", "0.96"),
    ("gpu-memory-utilization", "0"),
])
def test_import_does_not_silently_replace_launch_settings(flag, value):
    with pytest.raises(RecipeImportError):
        import_text(f"name: test\ncommand: vllm serve org/model --{flag} {value}\n")


@pytest.mark.parametrize("backend", ["mp", "native", "ray", "auto"])
def test_supported_backend_is_preserved(backend):
    draft, report = import_text(
        f"command: vllm serve org/model -tp 2 --distributed-executor-backend {backend}\n")
    assert draft.distributed_backend.value == backend
    assert report["mapped"]["distributed-executor-backend"] == backend


def test_valid_recipe_defaults_and_provenance_survive_import():
    text = """name: experiment
command: vllm serve org/model -tp 2 --max-model-len {ctx} --gpu-memory-utilization 0.85
defaults: {ctx: 8192}
env: {FLAG: 1}
mods: [mods/fix]
"""
    draft, _ = import_text(text, overrides={"ctx": 16384}, source_ref="https://example.com/recipe.yaml")
    assert draft.simple.context_length == 16384
    assert draft.advanced.gpu_memory_utilization == 0.85
    assert draft.advanced.env == {"FLAG": "1"}
    assert draft.advanced.mods == ["fix"]
    assert draft.source["ref"] == "https://example.com/recipe.yaml"


def test_builtin_recipe_text_uses_utf8():
    detail = recipe_detail("deepseek-v4-flash-0731-b12x")
    assert "—" in detail["title"]
    assert "→" in detail["measured"]["context"]
