import asyncio
import hashlib
import json

import httpx
import pytest

from twinspark.controller.app import create_app
from twinspark.cookbook import import_text, recipe_detail, remote, updates
from twinspark.cookbook.remote import RemoteError, fetch_text, list_source

from .conftest import draft
from .test_split_preparation import split_draft

URL = "https://example.com/recipe.yaml"
OLD = ("name: original\ncontainer: image-old\ncommand: vllm serve org/model --max-model-len {ctx}\n"
       "defaults: {ctx: 8192}\n")


def imported(text=OLD, name="custom", **kw):
    return import_text(text, profile_name=name, source_ref=URL, **kw)[0]


def test_receipt_records_actual_input_and_replaces_embedded_receipt():
    text = json.dumps({**draft().model_dump(mode="json"), "source": {"receipt": {"ref": "https://wrong"}}})
    d = imported(text)
    receipt = d.source["receipt"]
    assert receipt["ref"] == URL
    assert receipt["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert "identity" not in receipt["baseline"]
    assert "source" not in receipt["baseline"]
    assert receipt["format"] == "twinspark"


def test_import_accepts_utf8_bom_but_enforces_size_in_bytes():
    d = imported("\ufeff" + json.dumps(draft().model_dump(mode="json")))
    assert d.simple.model == "org/model-a"
    with pytest.raises(ValueError, match="larger than 512 KiB"):
        imported("#" + "😀" * (512 * 1024 // 4))


async def test_update_preserves_local_settings_while_applying_new_upstream_values(cluster, monkeypatch):
    c = cluster.controller
    d = imported()
    d.simple.context_length = 4096
    d.simple.api_alias = "chat"
    c.create_profile(d)
    incoming = OLD.replace("image-old", "image-new").replace("org/model", "org/new-model")
    monkeypatch.setattr(updates, "fetch_text", lambda ref: incoming)
    before = c.get_profile("custom").model_dump(mode="json")
    result = (await updates.check_updates(c, c.list_profiles()))[0]
    assert result["status"] == "changed", result
    new = result["preview"]["draft"]
    assert new["simple"]["context_length"] == 4096
    assert new["simple"]["api_alias"] == "chat"
    assert new["simple"]["model"] == "org/new-model"
    assert new["image_hint"] == "image-new"
    assert new["name"] == "custom-update"
    assert new["verification"] == "experimental"
    assert set(result["preserved"]) == {"simple.context_length", "simple.api_alias"}
    assert result["conflicts"] == []
    assert {x["field"] for x in result["changes"]} == {"image_hint", "simple.model"}
    assert c.get_profile("custom").model_dump(mode="json") == before
    assert c.get_profile("custom-update") is None


async def test_conflicts_are_reported_and_template_overrides_are_reused(cluster, monkeypatch):
    c = cluster.controller
    d = imported(overrides={"ctx": 16384})
    d.simple.context_length = 4096
    c.create_profile(d)
    incoming = OLD.replace("{ctx}", "32768").replace("image-old", "image-new")
    monkeypatch.setattr(updates, "fetch_text", lambda ref: incoming)
    result = (await updates.check_updates(c, c.list_profiles()))[0]
    assert result["status"] == "changed"
    assert result["conflicts"] == ["simple.context_length"]
    assert result["preview"]["draft"]["simple"]["context_length"] == 4096
    assert result["preview"]["draft"]["source"]["receipt"]["overrides"] == {"ctx": 16384}


async def test_successive_updates_compare_against_upstream_baseline(cluster, monkeypatch):
    c = cluster.controller
    d = imported()
    d.simple.api_alias = "chat"
    c.create_profile(d)
    monkeypatch.setattr(updates, "fetch_text", lambda ref: OLD.replace("image-old", "image-new"))
    first = (await updates.check_updates(c, [c.get_profile("custom")]))[0]
    from twinspark.schemas.profile import ProfileDraft
    c.create_profile(ProfileDraft.model_validate(first["preview"]["draft"]))
    monkeypatch.setattr(updates, "fetch_text", lambda ref: OLD.replace("image-old", "image-newer"))
    second = (await updates.check_updates(c, [c.get_profile("custom-update")]))[0]
    assert second["preview"]["draft"]["image_hint"] == "image-newer"
    assert second["preview"]["draft"]["simple"]["api_alias"] == "chat"
    assert second["conflicts"] == []


async def test_split_update_handles_each_model_and_preserves_independent_context(cluster, monkeypatch):
    c = cluster.controller
    text = json.dumps(split_draft().model_dump(mode="json"))
    d = imported(text)
    d.secondary.simple.context_length = 4096
    c.create_profile(d)
    incoming = split_draft().model_dump(mode="json")
    incoming["secondary"]["simple"]["model"] = "org/new-model-b"
    incoming["secondary"]["identity"]["model_repo"] = "org/new-model-b"
    incoming["secondary"]["identity"]["model_revision"] = "d" * 40
    monkeypatch.setattr(updates, "fetch_text", lambda ref: json.dumps(incoming))
    result = (await updates.check_updates(c, c.list_profiles()))[0]
    assert result["status"] == "changed", result
    b = result["preview"]["draft"]["secondary"]
    assert b["simple"]["model"] == "org/new-model-b"
    assert b["simple"]["context_length"] == 4096
    assert b["identity"]["model_revision"] == "d" * 40
    assert "secondary.simple.context_length" in result["preserved"]
    assert "secondary.identity.model_revision" in {x["field"] for x in result["changes"]}


async def test_patch_lists_are_merged_as_whole_settings(cluster, monkeypatch):
    c = cluster.controller
    d = imported(OLD + "mods: [mods/base]\n")
    d.advanced.mods = ["my-local-patch"]
    c.create_profile(d)
    monkeypatch.setattr(updates, "fetch_text", lambda ref: OLD + "mods: [mods/new-upstream]\n")
    result = (await updates.check_updates(c, c.list_profiles()))[0]
    assert result["preview"]["draft"]["advanced"]["mods"] == ["my-local-patch"]
    assert result["conflicts"] == ["advanced.mods"]


async def test_builtin_metadata_only_updates_are_detected(cluster, monkeypatch, tmp_path):
    from twinspark import cookbook
    c = cluster.controller
    (tmp_path / "recipe.yaml").write_text(OLD, encoding="utf-8")
    sidecar = tmp_path / "recipe.meta.json"
    sidecar.write_text(json.dumps({"notes": ["initial"]}), encoding="utf-8")
    monkeypatch.setattr(cookbook, "RECIPES_DIR", tmp_path)
    c.create_profile(cookbook.build_draft("recipe"))
    sidecar.write_text(json.dumps({"notes": ["updated guidance"]}), encoding="utf-8")
    result = (await updates.check_updates(c, c.list_profiles()))[0]
    assert result["status"] == "changed"
    assert result["changes"] == []
    assert result["preview"]["draft"]["source"]["notes"] == ["updated guidance"]


async def test_checks_deduplicate_source_fetches_and_isolate_errors(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(imported(name="one"))
    c.create_profile(imported(name="two"))
    failed = imported(name="broken")
    failed.source["receipt"]["ref"] = "https://bad/recipe"
    c.create_profile(failed)
    c.create_profile(draft("untracked"))
    calls = []

    def fetch(ref):
        calls.append(ref)
        if ref.startswith("https://bad"):
            raise RemoteError("upstream unavailable")
        return OLD

    monkeypatch.setattr(updates, "fetch_text", fetch)
    results = await updates.check_updates(c, c.list_profiles())
    assert len(calls) == 2
    states = {r["profile"]: r["status"] for r in results}
    assert states == {"broken": "error", "one": "current", "two": "current", "untracked": "untracked"}


async def test_concurrency_is_bounded_and_names_are_unique(cluster, monkeypatch):
    c = cluster.controller
    for i in range(8):
        d = imported(name=f"profile-{i}")
        d.source["receipt"]["ref"] = f"https://example.com/{i}.yaml"
        c.create_profile(d)
    c.create_profile(draft("profile-0-update"))
    in_flight, peak = 0, 0
    original = asyncio.to_thread

    async def fake_to_thread(fn, ref):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return OLD.replace("image-old", "image-new")

    monkeypatch.setattr(updates.asyncio, "to_thread", fake_to_thread)
    results = await updates.check_updates(c, c.list_profiles())
    monkeypatch.setattr(updates.asyncio, "to_thread", original)
    assert peak == 4
    assert next(r for r in results if r["profile"] == "profile-0")["preview"]["draft"]["name"] == "profile-0-update-2"


async def test_recipe_updates_api_keeps_profiles_unchanged_and_validates_names(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(imported())
    monkeypatch.setattr(updates, "fetch_text", lambda ref: OLD.replace("image-old", "image-new"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(c, "key", run_startup=False)),
                                 base_url="http://manager", headers={"x-api-key": "key"}) as client:
        response = await client.get("/api/v1/cookbook/updates?profile=custom")
        assert response.status_code == 200
        assert response.json()["updates"][0]["status"] == "changed"
        assert (await client.get("/api/v1/cookbook/updates?profile=missing")).status_code == 404
        assert len(c.list_profiles()) == 1


@pytest.mark.parametrize("payload", [b"not json", b"{}", b"[null]", b'[{"type":"file"}]'])
def test_bad_catalog_responses_are_actionable_errors(payload):
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=payload))) as client:
        with pytest.raises(RemoteError):
            list_source("example/recipes:folder", client, refresh=True)


def test_catalog_cache_cannot_be_mutated_by_callers():
    remote._cache.clear()
    payload = [{"name": "b.yaml", "path": "recipes/b.yaml", "type": "file"},
               {"name": "a.json", "path": "recipes/a.json", "type": "file"},
               {"name": "b.meta.json", "path": "recipes/b.meta.json", "type": "file"}]
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
        first = list_source("example/recipes:folder", client, refresh=True)
        assert [f["name"] for f in first["files"]] == ["a.json", "b.yaml"]
        first["files"].clear()
        assert len(list_source("example/recipes:folder", client)["files"]) == 2


def test_fetch_rejects_credentials_invalid_utf8_and_https_downgrades():
    with pytest.raises(RemoteError, match="credentials"):
        fetch_text("https://user:secret@example.com/recipe")
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"\xff"))) as client:
        with pytest.raises(RemoteError, match="UTF-8"):
            fetch_text(URL, client)

    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"location": "http://example.com/recipe"}) if req.url.scheme == "https" \
            else httpx.Response(200, text=OLD)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(RemoteError, match="stay on https"):
            fetch_text(URL, client)
    assert seen == [URL]


def test_builtin_receipt_includes_sidecar_metadata():
    detail = recipe_detail("deepseek-v4-flash-0731-b12x")
    receipt = detail["draft"]["source"]["receipt"]
    assert receipt["ref"] == "builtin:deepseek-v4-flash-0731-b12x.yaml"
    assert len(receipt["metadata_sha256"]) == 64
