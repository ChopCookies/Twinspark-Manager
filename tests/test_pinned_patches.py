"""A recipe's required patch is an installable, pinned prerequisite, not instructions in prose.

Found on two real Sparks: a GLM preparation pinned everything, then stopped at "mod(s) not installed", and the
recipe told the operator to find a Python file and build the mod by hand.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from twinspark import cli
from twinspark import mod_artifacts as ma
from twinspark.agent import mods as modlib
from twinspark.controller import prerequisites
from twinspark.controller.app import create_app
from twinspark.cookbook import build_draft
from twinspark.schemas.profile import ProfileDraft

from .conftest import draft

PATCH = b"# SPDX-License-Identifier: Apache-2.0\nTOPK = 2048\n"
OTHER = b"# something else\n"
DESC = {"kind": "file-patch", "repo": "someone/spark-recipes", "commit": "a" * 40,
        "path": "docker/indexer_sm121.py", "sha256": hashlib.sha256(PATCH).hexdigest(),
        "target": "/usr/local/lib/python3.12/dist-packages/vllm/indexer.py", "license": "Apache-2.0",
        "why": "long decodes fail without it"}


def art(**changes):
    return ma.parse("fix", {**DESC, **changes})


# ---- the descriptor ------------------------------------------------------------------------------------
def test_a_descriptor_must_be_complete_and_pinned():
    a = art()
    assert a["filename"] == "indexer_sm121.py"
    assert ma.raw_url(a) == ("https://raw.githubusercontent.com/someone/spark-recipes/" + "a" * 40
                             + "/docker/indexer_sm121.py")
    assert ma.review_url(a).startswith("https://github.com/someone/spark-recipes/blob/" + "a" * 40)
    for bad, match in [({"commit": "main"}, "40-character commit"), ({"sha256": "abc"}, "SHA-256"),
                       ({"license": None}, "license"), ({"path": "docker/../../etc/passwd"}, "'..'"),
                       ({"target": "relative/x.py"}, "absolute path"), ({"path": "docker/run.sh"}, "run.sh"),
                       ({"repo": "https://github.com/a/b"}, "owner/name"), ({"kind": "archive"}, "not supported")]:
        with pytest.raises(ma.ArtifactError, match=match):
            art(**bad)


def test_a_recipe_with_an_incomplete_descriptor_is_refused():
    with pytest.raises(ValueError, match="mod_artifacts"):
        ProfileDraft.model_validate({**draft("x").model_dump(mode="json"),
                                     "source": {"mod_artifacts": {"fix": {**DESC, "commit": "main"}}}})


def test_the_built_mod_is_exactly_what_tsm_mods_file_patch_makes(tmp_path):
    a = art()
    built = ma.files(a, PATCH)
    assert set(built) == {"run.sh", "indexer_sm121.py"} and built["indexer_sm121.py"] == PATCH
    assert {p: hashlib.sha256(b).hexdigest() for p, b in built.items()} == ma.expected(a)
    src = tmp_path / "indexer_sm121.py"
    src.write_bytes(PATCH)
    by_hand = cli.file_patch_mod("fix", src, a["target"], tmp_path / "out")
    h1 = modlib.install_mod(tmp_path / "m1", "fix", cli._pack_dir(by_hand))["hash"]
    h2 = modlib.install_mod(tmp_path / "m2", "fix", ma.pack(built))["hash"]
    assert h1 == h2                       # a mod made by hand from the same file is the same mod
    with pytest.raises(ma.ArtifactError, match="pins"):
        ma.files(a, OTHER)


def test_matching_ignores_extra_files_but_not_other_content():
    a = art()
    exp = ma.expected(a)
    assert ma.matches(a, {**exp, "README.md": "0" * 64}) is True       # a README beside it changes nothing
    assert ma.matches(a, {**exp, "indexer_sm121.py": "1" * 64}) is False
    assert ma.matches(a, {"indexer_sm121.py": exp["indexer_sm121.py"]}) is False   # no run.sh
    assert ma.matches(a, None) is None                                 # a large mod: not listed


async def test_fetch_takes_exactly_the_pinned_file_and_checks_it():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=PATCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await ma.fetch(art(), c) == PATCH
    assert seen == [ma.raw_url(art())]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=OTHER))) as c:
        with pytest.raises(ma.ArtifactError, match="not installing"):
            await ma.fetch(art(), c)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as c:
        with pytest.raises(ma.ArtifactError, match="404"):
            await ma.fetch(art(), c)


# ---- the GLM recipe ------------------------------------------------------------------------------------
def test_the_glm_dflash2_recipe_pins_its_patch():
    d = build_draft("glm-5.3-flash-nvfp4-dflash2-tp2")
    a = ma.of_source(d.source)["glm53-sm121-sparse-attn"]
    assert "glm53-sm121-sparse-attn" in d.advanced.mods
    assert a["repo"] == "seanlinmt/GLM-5.3-Flash-NVFP4-DFlash2-2x-DGX-Spark"
    assert a["commit"] == "e6dcb04852aa469ad7b17c7156cabeae0f46f497"
    assert a["sha256"] == "8a3ecfb0bab2441dd7417ed00a10d142191496149f88e5fe79fcfaea4b160980"
    assert a["target"].endswith("vllm/model_executor/layers/sparse_attn_indexer_kpool.py")
    assert a["license"] == "Apache-2.0"
    assert a["image"].endswith("4def0ef644cb2e9814136dcffd5e385e21bc594f48f3b292234051904abe85a6")
    text = " ".join(d.source["requirements"])
    assert "tsm mods required" in text and "Download that file" not in text


# ---- on the cluster ------------------------------------------------------------------------------------
def pinned_profile(c, name="glmx"):
    d = draft(name)
    d.advanced.mods = ["fix"]
    d.source = {"mod_artifacts": {"fix": DESC}}
    return c.create_profile(d)


async def fake_fetch(a):
    return PATCH


async def test_missing_then_installed_then_a_switch_works(cluster):
    c = cluster.controller
    p = pinned_profile(c)
    st = await prerequisites.status(c, p.name)
    m = st["mods"][0]
    assert m["state"] == "missing" and m["installable"] and not st["ready"]
    assert m["artifact"]["raw_url"] == ma.raw_url(art())
    # without the patch: the switch stops before anything is downloaded, and says how to fix it
    job = await cluster.wait_job((await c.activate(p.name)).job_id)
    assert job.state.value == "failed" and "not installed" in job.error
    assert f"tsm mods required {p.name} --install" in job.error
    res = await prerequisites.install(c, p.name, "fix", fetch=fake_fetch)
    assert res["install"]["ok"] and res["mod"]["state"] == "installed" and res["mod"]["matches_recipe"] is True
    assert res["ready"]
    job = await cluster.wait_job((await c.activate(p.name)).job_id)
    assert job.state.value == "completed", job.error
    audit = [e for e in c.store.audit_log(limit=50) if e.action == "mod.install_pinned"]
    assert audit and audit[0].detail["commit"] == "a" * 40


async def test_other_content_under_the_pinned_name_is_not_silently_used(cluster):
    c = cluster.controller
    p = pinned_profile(c, "glmy")
    other = {"run.sh": b"echo patched differently\n", "indexer_sm121.py": OTHER}
    await c.install_mod("fix", ma.pack(other))
    st = await prerequisites.status(c, p.name)
    assert st["mods"][0]["state"] == "installed" and st["mods"][0]["matches_recipe"] is False and not st["ready"]
    job = await cluster.wait_job((await c.activate(p.name)).job_id)
    assert job.state.value == "failed" and "not the patch this revision pins" in job.error
    await prerequisites.install(c, p.name, "fix", fetch=fake_fetch)          # the fix the message names
    assert (await prerequisites.status(c, p.name))["ready"]


async def test_a_mod_without_a_pin_cannot_be_installed_from_the_recipe(cluster):
    c = cluster.controller
    d = draft("plain")
    d.advanced.mods = ["handmade"]
    c.create_profile(d)
    st = await prerequisites.status(c, "plain")
    assert st["mods"][0]["installable"] is False and "artifact" not in st["mods"][0]
    with pytest.raises(prerequisites.PrerequisiteError, match="does not pin"):
        await prerequisites.install(c, "plain", "handmade", fetch=fake_fetch)
    with pytest.raises(prerequisites.PrerequisiteError, match="does not use"):
        await prerequisites.install(c, "plain", "other", fetch=fake_fetch)


async def test_the_mods_page_offers_the_pinned_patch(cluster):
    c = cluster.controller
    pinned_profile(c, "glmz")
    from twinspark.controller.files import mods_overview
    ov = await mods_overview(c)
    assert ov["missing"] == [{"name": "fix", "profiles": ["glmz"], "pinned_by": ["glmz"]}]


async def test_the_api_needs_the_confirmation(cluster, monkeypatch):
    c = cluster.controller
    pinned_profile(c, "glmw")
    monkeypatch.setattr(ma, "fetch", fake_fetch)
    app = create_app(c, "mgmt", run_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"x-api-key": "mgmt"}) as h:
        st = (await h.get("/api/v1/profiles/glmw/prerequisites")).json()
        assert st["mods"][0]["state"] == "missing"
        r = await h.post("/api/v1/profiles/glmw/prerequisites/install", json={"mod": "fix", "confirm": "yes"})
        assert r.status_code == 422
        r = await h.post("/api/v1/profiles/glmw/prerequisites/install",
                         json={"mod": "fix", "confirm": "INSTALL PINNED PATCH"})
        assert r.status_code == 200 and r.json()["ready"]
        assert (await h.get("/api/v1/profiles/nope/prerequisites")).status_code == 404


class FakeApi:
    as_json = False

    def __init__(self, status, install=None):
        self.status, self.install, self.calls = status, install, []

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        return self.status if method == "GET" else self.install


def missing_status():
    a = art()
    return {"profile": "glmx", "ready": False, "errors": {}, "artifact_error": None, "mods": [{
        "name": "fix", "state": "missing", "matches_recipe": None, "installable": True,
        "nodes": {"A": {"present": False}, "B": {"present": False}},
        "artifact": {**{k: a.get(k) for k in ("repo", "commit", "path", "sha256", "target", "license")},
                     "review_url": ma.review_url(a), "raw_url": ma.raw_url(a)}}]}


def test_tsm_mods_required_lists_and_exits_nonzero_until_ready(capsys):
    api = FakeApi(missing_status())
    with pytest.raises(SystemExit) as exc:
        cli.cmd_mods_required(SimpleNamespace(path="glmx", install=None, yes=False), api)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "MISSING" in out and "someone/spark-recipes@aaaaaaaaaaaa" in out and "review: https://github.com" in out


def test_tsm_mods_required_install(capsys):
    h = "sha256:" + "d" * 64
    api = FakeApi(missing_status(), {"install": {"ok": True, "results": {"A": {"hash": h}, "B": {"hash": h}}},
                                     "mod": {"matches_recipe": True}, "ready": True})
    cli.cmd_mods_required(SimpleNamespace(path="glmx", install="*", yes=True), api)
    post = [c for c in api.calls if c[0] == "POST"]
    assert post == [("POST", "/api/v1/profiles/glmx/prerequisites/install",
                     {"mod": "fix", "confirm": "INSTALL PINNED PATCH"})]
    assert "installed on A, B" in capsys.readouterr().out


def test_tsm_mods_required_install_fails_loudly_on_a_partial_install():
    api = FakeApi(missing_status(), {"install": {"ok": False, "results": {"A": {"hash": "sha256:x"},
                                                                         "B": {"error": "disk full"}}},
                                     "mod": {"matches_recipe": None}, "ready": False})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_mods_required(SimpleNamespace(path="glmx", install="*", yes=True), api)
    assert exc.value.code == 1


def test_agents_list_the_files_of_small_mods(tmp_path):
    modlib.install_mod(tmp_path, "fix", ma.pack(ma.files(art(), PATCH)))
    st = modlib.mods_status(tmp_path, ["fix", "absent"])
    assert st["fix"]["files"] == ma.expected(art()) and st["absent"]["files"] is None
    with tempfile.TemporaryDirectory() as t:
        big = Path(t) / "big"
        (big / "p").mkdir(parents=True)
        (big / "run.sh").write_text("echo\n")
        for i in range(70):
            (big / "p" / f"{i}.txt").write_text(str(i))
        assert modlib.file_hashes(big) is None                          # too many files: tree hash only
    assert json.dumps(st)                                                # JSON-safe for the API
