"""The recipe catalog (master plan §4.1): recipes from the repository's main branch, checked against the
SHA-256 in catalog/index.json, overlaid on the package's recipes without touching imported profiles."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from twinspark import cookbook
from twinspark.cookbook import catalog, updates
from twinspark.cookbook.catalog import Catalog, CatalogError, check_index

ROOT = Path(__file__).resolve().parent.parent
URL = "https://raw.githubusercontent.com/ChopCookies/Twinspark-Manager/main/catalog/index.json"
SMOKE = "smollm2-135m-smoke"


def test_the_committed_index_matches_the_recipes():
    """CI: a recipe changed without `python scripts/build_catalog.py` fails here."""
    r = subprocess.run([sys.executable, str(ROOT / "scripts/build_catalog.py"), "--check"], capture_output=True,
                       text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    doc = check_index(json.loads((ROOT / "catalog/index.json").read_text()))
    names = {e["name"] for e in doc["recipes"]}
    assert names == set(cookbook._package_files())
    glm = next(e for e in doc["recipes"] if e["name"] == "glm-5.3-flash-nvfp4-tp2")
    assert glm["modes"] == ["latency", "throughput"] and glm["default_mode"] == "latency"
    assert all(f["path"].startswith("twinspark/cookbook/recipes/") for e in doc["recipes"] for f in e["files"])


class Repo:
    """A fake raw.githubusercontent.com serving a copy of the recipe folder and its index."""

    def __init__(self, tmp: Path):
        self.root = tmp / "repo"
        shutil.copytree(ROOT / "twinspark/cookbook/recipes", self.root / "twinspark/cookbook/recipes")
        self.requests: list[str] = []
        self.override: dict[str, bytes] = {}
        self.build()

    @property
    def folder(self) -> Path:
        return self.root / "twinspark/cookbook/recipes"

    def build(self, **patch) -> dict:
        def validate(path, mode):
            return cookbook._draft_from_file(path, mode=mode)[0]
        self.index = catalog.build_index(self.folder, self.root, validate=validate, modes_of=cookbook.recipe_modes)
        self.index.update(patch)
        return self.index

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.override:
            return httpx.Response(200, content=self.override[url])
        prefix = "https://raw.githubusercontent.com/ChopCookies/Twinspark-Manager/main/"
        rel = url.removeprefix(prefix)
        if rel == "catalog/index.json":
            return httpx.Response(200, content=catalog.dumps(self.index).encode())
        path = self.root / rel
        return httpx.Response(200, content=path.read_bytes()) if path.is_file() else httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def edit(self, name: str, fn) -> None:
        p = self.folder / f"{name}.json"
        doc = json.loads(p.read_text())
        fn(doc)
        p.write_text(json.dumps(doc, indent=2) + "\n")
        self.build()


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


@pytest.fixture
def cat(tmp_path):
    c = cookbook.make_catalog(tmp_path / "cache", URL)
    catalog.configure(c)
    return c


def test_nothing_changes_while_the_catalog_equals_the_package(repo, cat):
    with repo.client() as client:
        changes = cat.refresh(client)
    assert changes["added"] == changes["updated"] == changes["removed"] == [] and changes["recipes"] == 6
    # every file was the package's own: only the index was downloaded
    assert repo.requests == [URL]
    rows = {r["name"]: r for r in cookbook.list_recipes()}
    assert all(r["origin"] == "package" and r["catalog"] == "same" for r in rows.values())
    assert cat.status()["new"] == [] and cat.status()["updated"] == []


def test_a_new_and_a_corrected_recipe_arrive_and_imported_profiles_stay(repo, cat, cluster):
    c = cluster.controller
    c.create_profile(cookbook.build_draft(SMOKE, "smoke"))
    before = c.get_profile("smoke").model_dump(mode="json")
    new = json.loads((repo.folder / f"{SMOKE}.json").read_text())
    new["name"] = "tiny-new"
    new["source"]["title"] = "A recipe that came later"
    (repo.folder / "tiny-new.json").write_text(json.dumps(new, indent=2) + "\n")
    repo.edit(SMOKE, lambda d: d["simple"].update(context_length=4096))
    with repo.client() as client:
        changes = cat.refresh(client)
    assert changes["added"] == ["tiny-new"] and changes["updated"] == [SMOKE]   # against the package's recipes
    rows = {r["name"]: r for r in cookbook.list_recipes()}
    assert rows["tiny-new"]["origin"] == "catalog" and rows["tiny-new"]["catalog"] == "new"
    assert rows[SMOKE]["origin"] == "catalog" and rows[SMOKE]["catalog"] == "updated"
    assert cookbook.build_draft(SMOKE).simple.context_length == 4096
    assert c.get_profile("smoke").model_dump(mode="json") == before          # never changed by a refresh
    import asyncio
    (row,) = asyncio.run(updates.check_updates(c, [c.get_profile("smoke")]))
    assert row["status"] == "changed"                                    # offered as a separate update
    assert any(ch["field"] == "simple.context_length" for ch in row["changes"])
    # a second refresh: only what changed since is downloaded, and reported
    repo.requests.clear()
    with repo.client() as client:
        again = cat.refresh(client)
    assert again["added"] == again["updated"] == again["removed"] == [] and repo.requests == [URL]
    assert cat.status()["changes"]["added"] == ["tiny-new"]            # the Cookbook still shows the last change


def test_a_file_that_does_not_match_its_sha256_is_not_used(repo, cat):
    repo.edit(SMOKE, lambda d: d["simple"].update(context_length=4096))
    target = f"https://raw.githubusercontent.com/ChopCookies/Twinspark-Manager/main/twinspark/cookbook/recipes/{SMOKE}.json"
    repo.override[target] = b'{"tampered": true}'
    with repo.client() as client, pytest.raises(CatalogError, match="does not match"):
        cat.refresh(client)
    assert cat.snapshot() == {} and cat.last_error                      # nothing half-written is in use
    assert cookbook.build_draft(SMOKE).simple.context_length != 4096
    assert [p.name for p in (cat.cache / "sets").iterdir()] == []       # the partial set was removed
    # a correct refresh, then the cached file is changed on disk: the package's recipe is used again
    del repo.override[target]
    with repo.client() as client:
        cat.refresh(client)
    assert cookbook.build_draft(SMOKE).simple.context_length == 4096
    cached = cat.snapshot()["files"][SMOKE][0]
    cached.write_text(cached.read_text().replace("4096", "8192"))
    cat._loaded = None
    assert SMOKE in cat.snapshot()["bad"]
    assert cookbook.build_draft(SMOKE).simple.context_length != 8192


def test_a_recipe_for_a_newer_twinspark_is_listed_not_offered(repo, cat):
    new = json.loads((repo.folder / f"{SMOKE}.json").read_text())
    new["name"] = "future"
    new["source"]["min_twinspark"] = "0.7.0"
    (repo.folder / "future.json").write_text(json.dumps(new) + "\n")
    repo.build()
    for e in repo.index["recipes"]:
        if e["name"] == "future":
            e["min_twinspark"] = "9.0.0"        # what main would say once 9.0 exists
    with repo.client() as client:
        cat.refresh(client)
    assert not any("future" in u for u in repo.requests if not u.endswith("index.json"))   # not downloaded
    row = next(r for r in cookbook.list_recipes() if r["name"] == "future")
    assert row["needs_twinspark"] == "9.0.0" and "update TwinSpark" in row["error"]
    assert cookbook.build_draft("future") is None
    assert cat.status()["needs_newer"] == {"future": "9.0.0"}


def test_an_unreleased_build_keeps_its_own_recipes(repo, tmp_path):
    repo.edit(SMOKE, lambda d: d["simple"].update(context_length=4096))
    repo.index["twinspark_version"] = "0.6.9"            # main is behind this build
    cat = cookbook.make_catalog(tmp_path / "c2", URL)
    catalog.configure(cat)
    with repo.client() as client:
        cat.refresh(client)
    assert cookbook.build_draft(SMOKE).simple.context_length != 4096
    assert next(r for r in cookbook.list_recipes() if r["name"] == SMOKE)["catalog"] == "older"


@pytest.mark.parametrize("bad, message", [
    ({"format": "nope"}, "not a TwinSpark recipe catalog"),
    ({"recipes": [{"name": "../x"}]}, "no valid name"),
    ({"recipes": [{"name": "x", "min_twinspark": "0.7.0", "files": [
        {"path": "../../etc/passwd", "sha256": "a" * 64, "size": 1}]}]}, "invalid path"),
    ({"recipes": [{"name": "x", "min_twinspark": "0.7.0", "files": [
        {"path": "recipes/other.json", "sha256": "a" * 64, "size": 1}]}]}, "unexpected file"),
    ({"recipes": [{"name": "x", "min_twinspark": "0.7.0", "files": [
        {"path": "recipes/x.json", "sha256": "a" * 64, "size": 10 ** 7}]}]}, "larger than"),
    ({"recipes": [{"name": "x", "min_twinspark": "0.7.0", "files": [
        {"path": "recipes/x.json", "sha256": "zz", "size": 1}]}]}, "invalid sha256"),
])
def test_a_malformed_index_is_refused(bad, message):
    doc = {"format": "twinspark-catalog/1", "twinspark_version": "0.7.0", "recipes": [], **bad}
    with pytest.raises(CatalogError, match=message):
        check_index(doc)


def test_size_limits_https_and_the_switch(tmp_path):
    big = Catalog(tmp_path / "c", URL)

    def huge(request):
        return httpx.Response(200, content=b"{" + b" " * (catalog.MAX_INDEX_BYTES + 10))
    with httpx.Client(transport=httpx.MockTransport(huge)) as client, pytest.raises(CatalogError, match="larger"):
        big.refresh(client)

    def redirect(request):
        return httpx.Response(302, headers={"location": "http://example.com/index.json"})
    with httpx.Client(transport=httpx.MockTransport(redirect)) as client, pytest.raises(CatalogError, match="https"):
        big.refresh(client)
    with pytest.raises(CatalogError, match="https"):
        Catalog(tmp_path / "c", "http://example.com/catalog/index.json")
    off = Catalog(tmp_path / "c", None)
    assert not off.stale() and off.status()["enabled"] is False
    with pytest.raises(CatalogError, match="switched off"):
        off.refresh()
    with pytest.raises(CatalogError, match="no network in tests"):      # conftest: never GitHub from a test
        big.refresh()


async def test_the_api_lists_the_catalog_and_refreshes_on_request(repo, cluster, monkeypatch):
    from twinspark.controller.app import create_app
    c = cluster.controller
    monkeypatch.setattr(catalog, "_client", repo.client)
    repo.edit(SMOKE, lambda d: d["simple"].update(context_length=4096))
    app = create_app(c, "key", run_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "key"}) as client:
        r = await client.post("/api/v1/cookbook/catalog/refresh")
        assert r.status_code == 200, r.text
        assert r.json()["status"]["updated"] == [SMOKE]
        r = await client.get("/api/v1/cookbook")
        row = next(x for x in r.json()["recipes"] if x["name"] == SMOKE)
        assert row["catalog"] == "updated" and r.json()["catalog"]["enabled"]
    assert c.catalog.cache == Path(c.config.db_path).parent / "catalog"
