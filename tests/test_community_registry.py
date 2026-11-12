"""Manager's side of the public Community v1 catalogue and download contract."""

import hashlib

import httpx
import pytest

from twinspark.api import routes_cookbook
from twinspark.client import cookbook_local
from twinspark.client.app import create_client_app
from twinspark.client.session import ClientSession
from twinspark.controller.app import create_app
from twinspark.cookbook.remote import RemoteError, list_source

REGISTRY = "https://community.example/api/v1/recipes"
RECIPE = '{"name":"qwen","simple":{"model":"Qwen/Qwen3-8B","quantization":"bf16","topology":"single-a"}}'
DIGEST = hashlib.sha256(RECIPE.encode()).hexdigest()
DOWNLOAD = f"{REGISTRY}/qwen/versions/2/recipe.json"


def listing(page, last_page, slug="qwen"):
    return {"schema_version": 1, "page": page, "last_page": last_page, "total": last_page,
            "data": [{"slug": slug, "title": "Qwen starter", "version": 2, "sha256": DIGEST,
                      "download_url": f"{REGISTRY}/{slug}/versions/2/recipe.json",
                      "update_url": f"{REGISTRY}/{slug}/latest.json", "verification": "experimental"}]}


def test_registry_pages_are_bounded_and_cache_returns_a_copy():
    seen = []

    def serve(request):
        page = int(request.url.params["page"])
        seen.append(page)
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json=listing(page, 8, slug=f"qwen-{page}"))

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        data = list_source(REGISTRY, client, refresh=True)
        assert seen == [1, 2, 3, 4, 5]
        assert data["truncated"] and len(data["files"]) == 5
        assert data["files"][0]["download_url"] == f"{REGISTRY}/qwen-1/versions/2/recipe.json"
        assert data["files"][0]["sha256"] == DIGEST
        data["files"].clear()
        assert len(list_source(REGISTRY, client)["files"]) == 5
        assert seen == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("url", [f"{REGISTRY}/qwen/latest.json", "https://elsewhere.example/recipe.json"])
def test_registry_refuses_non_versioned_or_other_origin_downloads(url):
    data = listing(1, 1)
    data["data"][0]["download_url"] = url
    with httpx.Client(transport=httpx.MockTransport(lambda _req: httpx.Response(200, json=data))) as client:
        with pytest.raises(RemoteError, match="outside its versioned endpoint"):
            list_source(REGISTRY, client, refresh=True)


def test_registry_does_not_follow_redirects():
    seen = []

    def serve(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.example/api/v1/recipes"})

    with httpx.Client(transport=httpx.MockTransport(serve), follow_redirects=True) as client:
        with pytest.raises(RemoteError, match="must not redirect"):
            list_source(REGISTRY, client, refresh=True)
    assert seen == [f"{REGISTRY}?page=1"]


@pytest.mark.parametrize("workstation", [False, True])
async def test_both_import_paths_check_the_registry_receipt_before_preview(cluster, tmp_path, monkeypatch,
                                                                          workstation):
    session = None
    if workstation:
        monkeypatch.setenv("TSM_CLIENT_KEYRING", "off")
        session = ClientSession(home=tmp_path / "client")
        app = create_client_app(session, "key")
        monkeypatch.setattr(cookbook_local, "fetch_text", lambda _url: RECIPE)
        base = "http://127.0.0.1:18765"
    else:
        app = create_app(cluster.controller, "key", run_startup=False)
        monkeypatch.setattr(routes_cookbook, "fetch_text", lambda _url: RECIPE)
        base = "http://manager"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base,
                                     headers={"x-api-key": "key"}) as client:
            path = "/api/v1/cookbook/import-recipe"
            response = await client.post(path, json={"url": DOWNLOAD, "expected_sha256": "0" * 64, "preview": True})
            assert response.status_code == 422 and "SHA-256" in response.json()["detail"]
            response = await client.post(path, json={"url": DOWNLOAD, "expected_sha256": DIGEST, "preview": True})
            assert response.status_code == 200, response.text
            assert response.json()["draft"]["source"]["ref"] == DOWNLOAD
            assert cluster.controller.get_profile("qwen") is None
    finally:
        if session is not None:
            await session.aclose()
