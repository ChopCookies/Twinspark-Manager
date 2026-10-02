"""Connection exports are stable, revision-aware, credential-free local artifacts."""

from __future__ import annotations

import ast
import json

import pytest
import yaml
from fastapi.testclient import TestClient

from twinspark.controller.app import create_app
from twinspark.controller.integrations import catalog, export_client
from twinspark.schemas.enums import Topology
from twinspark.schemas.profile import ProfileDraft

from .conftest import draft

CLIENTS = {"opencode", "aider", "langgraph", "openclaw", "hermes", "lm-eval"}
BASE = "http://spark-manager:8000/v1"


@pytest.fixture
def offline_controller(cluster, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("connection exports must not contact agents or inference backends")

    monkeypatch.setattr(cluster.gateway._client, "send", forbidden)
    monkeypatch.setattr(cluster.controller, "refresh_hardware", forbidden)
    for agent in cluster.controller.agents.values():
        monkeypatch.setattr(agent, "call", forbidden)
    return cluster.controller


def add_profile(ctrl, name="coder-profile", alias="coder", context=131072, *, tools=True, parser="hermes"):
    recipe = draft(name, Topology.SINGLE_A, alias=alias, context_length=context, tool_calling=tools)
    recipe.behaviour.tool_call_parser = parser
    return ctrl.create_profile(recipe)


def aliases(ctrl):
    return {item["alias"]: item for item in catalog(ctrl)["aliases"]}


def files(exported):
    assert isinstance(exported["files"], list) and exported["files"]
    assert isinstance(exported["notes"], list)
    for item in exported["files"]:
        assert item["name"] and isinstance(item["content"], str) and item["language"]
    return exported["files"]


def content(exported):
    return "\n".join(item["content"] for item in files(exported))


def json_document(exported, key):
    for item in files(exported):
        try:
            document = json.loads(item["content"])
        except ValueError:
            continue
        if isinstance(document, dict) and key in document:
            return document
    raise AssertionError(f"no exported JSON document has {key!r}")


def yaml_document(exported, key):
    for item in files(exported):
        if item["language"] not in {"yaml", "yml"} and not item["name"].endswith((".yml", ".yaml")):
            continue
        document = yaml.safe_load(item["content"])
        if isinstance(document, dict) and key in document:
            return document
    raise AssertionError(f"no exported YAML document has {key!r}")


def test_catalog_lists_supported_clients_and_pinned_plans_without_contacting_nodes(offline_controller):
    ctrl = offline_controller
    profile = add_profile(ctrl)
    unpinned = draft("draft-only", Topology.SINGLE_B, alias="draft-only")
    unpinned.identity = None
    ctrl.create_profile(unpinned)
    result = catalog(ctrl)
    assert {item["id"] for item in result["clients"]} == CLIENTS
    assert all(item["title"] and item["description"] and item["docs_url"] for item in result["clients"])
    assert "codex" not in {item["id"] for item in result["clients"]}
    entry = aliases(ctrl)["coder"]
    assert entry["status"] == "planned"
    assert entry["revision_id"] == profile.latest().revision_id
    assert entry["context_length"] == 131072
    assert entry["configured_tools"] is True and entry["tool_parser"] == "hermes"
    planned_draft = aliases(ctrl)["draft-only"]
    assert planned_draft["status"] == "planned" and planned_draft["revision_id"] is None
    assert result["gateway"]["auth_required"] is True


def test_active_exact_revision_and_measured_context_override_newer_draft(offline_controller):
    ctrl = offline_controller
    profile = add_profile(ctrl, context=131072, parser="old-parser")
    old = profile.latest()
    changed = profile.working_draft().model_copy(deep=True)
    changed.simple.context_length = 262144
    changed.behaviour.tool_call_parser = "new-parser"
    newer = ctrl.save_revision(changed)
    changed.simple.context_length = 524288
    changed.behaviour.tool_call_parser = "draft-parser"
    ctrl.save_draft(profile.name, changed)
    ctrl.store.kv_set("active", {"profile": profile.name, "revision_id": old.revision_id})
    ctrl.gateway.set_route("coder", ["http://node-a"], profile.name, old.revision_id, 98304)
    entry = aliases(ctrl)["coder"]
    assert entry["revision_id"] == old.revision_id != newer.revision_id
    assert entry["context_length"] == 98304
    assert entry["tool_parser"] == "old-parser"
    assert entry["status"] == "serving"


def test_split_plans_keep_each_part_context_parser_and_alias(offline_controller):
    ctrl = offline_controller
    a = draft("duo", Topology.SINGLE_A, alias="coder", context_length=131072, tool_calling=True)
    a.behaviour.tool_call_parser = "node-a-parser"
    b = draft("helper-profile", Topology.SINGLE_B, repo="org/model-b", alias="helper", context_length=65536)
    b.behaviour.tool_call_parser = "node-b-parser"
    document = a.model_dump(mode="json")
    document["simple"]["topology"] = "split"
    document["secondary"] = b.model_dump(mode="json")
    profile = ctrl.create_profile(ProfileDraft.model_validate(document))
    mapped = aliases(ctrl)
    assert mapped["coder"]["node"] == "A" and mapped["helper"]["node"] == "B"
    assert mapped["coder"]["context_length"] == 131072
    assert mapped["helper"]["context_length"] == 65536
    assert mapped["coder"]["tool_parser"] == "node-a-parser"
    assert mapped["helper"]["tool_parser"] == "node-b-parser"
    assert mapped["coder"]["revision_id"] == mapped["helper"]["revision_id"] == profile.latest().revision_id


def test_compatibility_results_are_not_attached_to_a_different_revision(offline_controller):
    ctrl = offline_controller
    profile = add_profile(ctrl)
    revision = profile.latest().revision_id
    summary = {"state": "completed", "checks": [{"name": "tools", "status": "pass"}],
               "dry_run": False, "revision_id": "older-revision"}
    ctrl.store.kv_set("compatibility:coder", summary)
    assert not aliases(ctrl)["coder"].get("latest_check")
    ctrl.store.kv_set("compatibility:coder", {**summary, "revision_id": revision})
    check = aliases(ctrl)["coder"]["latest_check"]
    assert check["revision_id"] == revision and check["checks"] == summary["checks"]


@pytest.mark.parametrize("client_id", sorted(CLIENTS))
def test_exports_contain_stable_alias_url_and_key_placeholder(offline_controller, client_id):
    ctrl = offline_controller
    add_profile(ctrl)
    result = export_client(ctrl, client_id, "coder", "http://spark-manager:8000/")
    exported = content(result)
    assert BASE in exported
    full_export = json.dumps(result)
    assert "coder" in exported and "TWINSPARK_API_KEY" in full_export
    assert "client-key" not in full_export and "backend-key" not in full_export
    assert "org/model-a" not in exported


@pytest.mark.parametrize("address", [
    "http://user:password@spark:8000/v1", "http://spark:8000/v1?key=secret", "http://spark:8000/v1#token",
    "http://0.0.0.0:8000/v1", "http://[::]:8000/v1", "file:///v1", "javascript:alert(1)",
])
def test_export_rejects_credentialed_ambiguous_or_non_http_address(offline_controller, address):
    add_profile(offline_controller)
    with pytest.raises(ValueError):
        export_client(offline_controller, "langgraph", "coder", address)


def test_opencode_singular_provider_uses_compatible_adapter_and_context_budgets(offline_controller):
    ctrl = offline_controller
    add_profile(ctrl, context=131072)
    add_profile(ctrl, "helper-profile", "helper", 65536, tools=False, parser=None)
    result = export_client(ctrl, "opencode", "coder", BASE, secondary_alias="helper")
    document = json_document(result, "provider")
    assert "providers" not in document
    provider = next(iter(document["provider"].values()))
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == BASE
    assert provider["models"]["coder"]["limit"]["context"] == 131072
    assert provider["models"]["helper"]["limit"]["context"] == 65536
    for alias in ("coder", "helper"):
        limits = provider["models"][alias]["limit"]
        assert 0 < limits["output"] < limits["context"]
    assert document["model"].endswith("/coder")
    assert "helper" in json.dumps(document.get("agent", document.get("agents", {})))


def test_aider_optional_weak_model_does_not_require_native_tools(offline_controller):
    ctrl = offline_controller
    add_profile(ctrl, tools=False, parser=None)
    add_profile(ctrl, "helper-profile", "helper", 65536, tools=False, parser=None)
    primary = yaml_document(export_client(ctrl, "aider", "coder", BASE), "model")
    assert primary["model"] == "openai/coder" and not primary.get("weak-model")
    paired = yaml_document(export_client(ctrl, "aider", "coder", BASE, secondary_alias="helper"), "model")
    assert paired["model"] == "openai/coder" and paired["weak-model"] == "openai/helper"


def test_langgraph_export_uses_chat_completions_without_stream_usage_extension(offline_controller):
    add_profile(offline_controller)
    result = export_client(offline_controller, "langgraph", "coder", BASE)
    python_files = [item for item in files(result) if item["language"] == "python" or item["name"].endswith(".py")]
    assert python_files
    source = python_files[0]["content"]
    ast.parse(source)
    assert "ChatOpenAI" in source and "use_responses_api=False" in source and "stream_usage=False" in source
    assert "TWINSPARK_API_KEY" in source


def test_openclaw_export_adds_model_provider_with_merge_mode(offline_controller):
    add_profile(offline_controller)
    document = json_document(export_client(offline_controller, "openclaw", "coder", BASE), "models")
    assert document["models"]["mode"] == "merge"
    provider = next(iter(document["models"]["providers"].values()))
    assert provider["baseUrl"] == BASE
    model = next(item for item in provider["models"] if item["id"] == "coder")
    assert model["contextWindow"] == 131072
    assert 0 < model["maxTokens"] < model["contextWindow"]


@pytest.mark.parametrize("context", [32768, "auto"])
def test_hermes_rejects_low_or_unmeasured_context(offline_controller, context):
    add_profile(offline_controller, context=context)
    with pytest.raises(ValueError):
        export_client(offline_controller, "hermes", "coder", BASE)


@pytest.mark.parametrize("client_id", ["opencode", "openclaw", "hermes"])
def test_context_dependent_export_requires_known_context(offline_controller, client_id):
    add_profile(offline_controller, context="auto")
    with pytest.raises(ValueError):
        export_client(offline_controller, client_id, "coder", BASE)


@pytest.mark.parametrize("client_id", ["aider", "langgraph", "lm-eval"])
def test_context_independent_export_allows_unmeasured_auto_context(offline_controller, client_id):
    add_profile(offline_controller, context="auto", tools=False, parser=None)
    assert files(export_client(offline_controller, client_id, "coder", BASE))


def test_lm_eval_uses_bounded_generation_task_and_full_chat_endpoint(offline_controller):
    profile = add_profile(offline_controller)
    result = export_client(offline_controller, "lm-eval", "coder", BASE)
    document = yaml_document(result, "model_args")
    assert document["model"] == "local-chat-completions"
    assert document["model_args"]["base_url"] == f"{BASE}/chat/completions"
    assert document["model_args"]["model"] == "coder"
    assert document["model_args"]["tokenizer_backend"] is None
    assert document["model_args"]["tokenized_requests"] is False
    assert document["model_args"]["max_retries"] == 0
    assert document["tasks"] == ["gsm8k"] and document["limit"] == 20
    assert document["gen_kwargs"]["max_gen_toks"] == 1024
    assert profile.latest().revision_id in document["output_path"]


def test_ambiguous_planned_alias_is_rejected_until_one_exact_revision_is_serving(offline_controller):
    ctrl = offline_controller
    first = add_profile(ctrl, "first", "coder", 131072)
    add_profile(ctrl, "second", "coder", 65536)
    assert aliases(ctrl)["coder"]["ambiguous"] is True
    with pytest.raises(ValueError, match="multiple|distinct|ambiguous"):
        export_client(ctrl, "langgraph", "coder", BASE)
    ctrl.gateway.set_route("coder", ["http://node-a"], first.name, first.latest().revision_id, 98304)
    resolved = aliases(ctrl)["coder"]
    assert resolved["profile"] == first.name and not resolved.get("ambiguous")
    assert files(export_client(ctrl, "langgraph", "coder", BASE))


def test_pinned_revision_selection_is_used_for_an_inactive_recipe(offline_controller):
    ctrl = offline_controller
    profile = add_profile(ctrl, context=65536)
    old = profile.latest()
    newer_draft = profile.working_draft().model_copy(deep=True)
    newer_draft.simple.context_length = 131072
    ctrl.save_revision(newer_draft)
    saved = ctrl.get_profile(profile.name)
    saved.pinned_revision = old.revision_id
    ctrl.store.save_profile(saved)
    item = aliases(ctrl)["coder"]
    assert item["revision_id"] == old.revision_id and item["context_length"] == 65536


def test_opencode_secondary_auto_context_must_be_known_too(offline_controller):
    ctrl = offline_controller
    add_profile(ctrl)
    add_profile(ctrl, "helper-profile", "helper", "auto")
    with pytest.raises(ValueError):
        export_client(ctrl, "opencode", "coder", BASE, secondary_alias="helper")


@pytest.mark.parametrize("client_id,alias,secondary", [("unknown", "coder", None),
                                                      ("aider", "missing", None),
                                                      ("aider", "coder", "missing"),
                                                      ("aider", "coder", "coder")])
def test_export_rejects_unknown_clients_aliases_and_duplicate_pair(offline_controller, client_id, alias, secondary):
    add_profile(offline_controller)
    with pytest.raises(ValueError):
        export_client(offline_controller, client_id, alias, BASE, secondary_alias=secondary)


def test_management_integration_routes_require_auth_and_validate_exports(offline_controller):
    add_profile(offline_controller)
    app = create_app(offline_controller, "management-secret", run_startup=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/integrations").status_code == 401
        params = {"client": "langgraph", "alias": "coder", "base_url": BASE}
        assert client.get("/api/v1/integrations/export", params=params).status_code == 401
        headers = {"x-api-key": "management-secret"}
        assert client.get("/api/v1/integrations", headers=headers).status_code == 200
        reply = client.get("/api/v1/integrations/export", params=params, headers=headers)
        assert reply.status_code == 200 and files(reply.json())
        assert "management-secret" not in content(reply.json())
        invalid = client.get("/api/v1/integrations/export", params={**params, "base_url": "http://user:pass@host/v1"},
                             headers=headers)
        assert invalid.status_code == 422


def test_unpinned_planned_alias_can_export_but_cannot_run_compatibility(offline_controller):
    ctrl = offline_controller
    recipe = draft("draft-only", Topology.SINGLE_A, alias="planned")
    recipe.identity = None
    ctrl.create_profile(recipe)
    result = export_client(ctrl, "langgraph", "planned", BASE)
    assert result["revision_id"] is None and files(result)
    app = create_app(ctrl, "management-secret", run_startup=False)
    with TestClient(app) as client:
        reply = client.post("/api/v1/integrations/check", json={"alias": "planned"},
                            headers={"x-api-key": "management-secret"})
        assert reply.status_code == 422
        assert not ctrl.busy() and not ctrl.current_job
