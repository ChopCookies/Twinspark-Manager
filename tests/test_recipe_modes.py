"""Recipe modes (master plan §4.2): one recipe, several variants (latency / throughput), each with its own
measured results, imported as separate profiles."""

from __future__ import annotations

import json

import httpx
import pytest

from twinspark import cookbook
from twinspark.controller.app import create_app
from twinspark.cookbook import RecipeImportError, build_draft, list_recipes, recipe_detail, updates

MTP = "glm-5.3-flash-nvfp4-tp2"
DFLASH = "glm-5.3-flash-nvfp4-dflash2-tp2"


def test_the_default_mode_is_the_recipe_as_written_and_the_other_drops_the_drafter():
    latency = build_draft(MTP)
    assert latency.source["mode"] == "latency" and latency.advanced.speculative_config
    assert latency.source["measured"]                                  # the recipe's numbers belong to it
    throughput = build_draft(MTP, mode="throughput")
    assert throughput.source["mode"] == "throughput" and throughput.source["mode_title"]
    assert not throughput.advanced.speculative_config
    assert throughput.advanced.max_num_seqs == 8
    assert throughput.source["measured"] == {}                         # not another mode's numbers
    # everything the patch does not name stays as in the recipe
    assert throughput.simple.model == latency.simple.model and throughput.image_hint == latency.image_hint
    dflash = build_draft(DFLASH, mode="throughput")
    assert not dflash.advanced.extra_models and not dflash.advanced.speculative_config


def test_an_unknown_mode_or_a_mode_for_a_recipe_without_modes_is_refused():
    with pytest.raises(RecipeImportError, match="has no mode 'turbo'.*latency, throughput"):
        build_draft(MTP, mode="turbo")
    with pytest.raises(RecipeImportError, match="has no modes"):
        build_draft("smollm2-135m-smoke", mode="latency")
    assert build_draft("smollm2-135m-smoke").source.get("mode") is None


def test_the_index_and_the_detail_show_the_modes():
    entry = next(r for r in list_recipes() if r["name"] == MTP)
    assert entry["default_mode"] == "latency"
    assert [m["name"] for m in entry["modes"]] == ["latency", "throughput"]
    assert entry["modes"][0]["measured"] and entry["modes"][1]["measured"] == {}
    plain = next(r for r in list_recipes() if r["name"] == "smollm2-135m-smoke")
    assert plain["modes"] == [] and plain["default_mode"] is None
    d = recipe_detail(MTP, "throughput")
    assert d["mode"] == "throughput" and d["measured"] == {} and d["draft"]["source"]["mode"] == "throughput"
    assert "modes" not in json.dumps(d["draft"]["source"])            # the menu is not copied into a profile


def test_a_malformed_mode_is_refused_when_the_recipe_is_read(tmp_path, monkeypatch):
    doc = json.loads((cookbook.RECIPES_DIR / f"{MTP}.json").read_text())
    doc["modes"]["throughput"]["patch"] = {"identity": {"image": "evil"}}      # only settings may change
    (tmp_path / "bad.json").write_text(json.dumps(doc))
    monkeypatch.setattr(cookbook, "RECIPES_DIR", tmp_path)
    with pytest.raises(RecipeImportError, match="malformed"):
        build_draft("bad", mode="throughput")
    assert "malformed" in list_recipes()[0]["error"]


async def test_import_in_a_mode_through_the_api(cluster):
    c = cluster.controller
    app = create_app(c, "key", run_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "key"}) as client:
        r = await client.post(f"/api/v1/cookbook/import/{MTP}", params={"profile_name": "glm-tp", "mode": "throughput"})
        assert r.status_code == 200, r.text
        assert r.json()["profile"]["name"] == "glm-tp"
        r = await client.post(f"/api/v1/cookbook/import/{MTP}", params={"mode": "turbo"})
        assert r.status_code == 422 and "turbo" in r.json()["detail"]
        r = await client.get(f"/api/v1/cookbook/recipes/{MTP}", params={"mode": "nope"})
        assert r.status_code == 422
        r = await client.get(f"/api/v1/cookbook/recipes/{MTP}")
        assert r.json()["mode"] == "latency"
    assert c.get_profile("glm-tp").working_draft().source["mode"] == "throughput"


async def test_the_update_check_compares_against_the_mode_the_profile_was_imported_in(cluster):
    c = cluster.controller
    c.create_profile(build_draft(MTP, "glm-tp", mode="throughput"))
    c.create_profile(build_draft(MTP, "glm-lat"))
    rows = {r["profile"]: r for r in await updates.check_updates(c, c.list_profiles())}
    assert rows["glm-tp"]["status"] == "current", rows["glm-tp"]
    assert rows["glm-lat"]["status"] == "current", rows["glm-lat"]


async def test_a_change_to_one_mode_is_an_update_only_for_profiles_of_that_mode(cluster, tmp_path, monkeypatch):
    import shutil
    c = cluster.controller
    shutil.copy(cookbook.RECIPES_DIR / f"{MTP}.json", tmp_path / f"{MTP}.json")
    monkeypatch.setattr(cookbook, "RECIPES_DIR", tmp_path)
    c.create_profile(build_draft(MTP, "glm-tp", mode="throughput"))
    c.create_profile(build_draft(MTP, "glm-lat"))
    doc = json.loads((tmp_path / f"{MTP}.json").read_text())
    doc["modes"]["throughput"]["patch"]["advanced"]["max_num_seqs"] = 12
    (tmp_path / f"{MTP}.json").write_text(json.dumps(doc))
    rows = {r["profile"]: r for r in await updates.check_updates(c, c.list_profiles())}
    assert rows["glm-lat"]["status"] == "current" and "not the part" in rows["glm-lat"]["message"]
    assert rows["glm-tp"]["status"] == "changed"
    assert {ch["field"] for ch in rows["glm-tp"]["changes"]} == {"advanced.max_num_seqs"}
