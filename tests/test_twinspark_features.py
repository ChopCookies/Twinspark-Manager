"""Tests for the new 0.3.0 features: cookbook, resolver, link test, metrics.

All use the existing dry-run cluster (no real vLLM is ever touched) plus unit
tests that mock outbound HTTP for the resolver — so nothing here can affect the
running controller's vLLM instance.
"""

from __future__ import annotations

import httpx
import pytest

from twinspark.cookbook import build_draft, list_recipes
from twinspark.metrics import _parse_text, scrape_metrics
from twinspark.resolver import resolve_hf_revision, resolve_image_digest, spec_from_resolved
from twinspark.schemas.enums import DistributedBackend, Topology

# ---- cookbook --------------------------------------------------------------

def test_cookbook_has_dual_spark_recipes():
    names = {r["name"] for r in list_recipes()}
    for expected in ("deepseek-v4-flash-0731-b12x", "glm-5.3-flash-nvfp4-tp2",
                     "glm-5.3-flash-nvfp4-dflash2-tp2", "mimo-v2.6-flash-tp2",
                     "qwen3.8-flash-next-nvfp4-tp2", "smollm2-135m-smoke"):
        assert expected in names, f"missing recipe {expected}"
    assert not any(n.endswith(".meta") for n in names)
    assert not any("error" in r for r in list_recipes())


def test_cookbook_recipes_validate_as_profiles():
    for r in list_recipes():
        draft = build_draft(r["name"])
        assert draft is not None
        assert draft.distributed_backend in (DistributedBackend.MP, DistributedBackend.RAY)
        assert draft.simple.topology in (Topology.TP2, Topology.TP_EP, Topology.SINGLE_A)
        assert draft.identity is None             # recipes are drafts until pinned
        assert draft.source.get("recipe") == r["name"]


@pytest.mark.asyncio
async def test_cookbook_import_via_cluster(cluster):
    assert cluster.controller.get_profile("glm-5.3-flash-nvfp4-tp2") is None
    draft = build_draft("glm-5.3-flash-nvfp4-tp2")
    profile = cluster.controller.create_profile(draft)
    stored = cluster.controller.get_profile(profile.name)
    assert stored is not None and stored.draft is not None
    assert stored.revisions == []            # nothing pinned yet
    with pytest.raises(ValueError, match="not pinned"):
        await cluster.controller.activate(profile.name)


# ---- resolver (mocked outbound HTTP) ---------------------------------------

_SHA = "a" * 40
_CONFIG = {"architectures": ["DeepseekV3"], "num_hidden_layers": 61,
           "num_attention_heads": 128, "num_key_value_heads": 1,
           "head_dim": 128, "hidden_size": 7168, "num_experts": 256,
           "num_experts_per_topic": 1}


def _fake_client():
    routes = {
        "/api/models/org/model/revision/main": {"sha": _SHA},
        f"/org/model/resolve/{_SHA}/config.json": _CONFIG,
        f"/org/model/resolve/{_SHA}/model.safetensors.index.json":
            {"metadata": {"total_size": 1024**3}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, request=request)
        return httpx.Response(200, json=body, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_resolve_hf_revision_pins_branch():
    r = resolve_hf_revision("org/model@main", client=_fake_client())
    assert r["revision"] == _SHA
    assert r["branch"] == "main"
    spec = spec_from_resolved(r)
    assert spec.weight_bytes == 1024**3 and spec.is_moe and spec.num_experts == 256


def test_resolve_image_digest_keeps_existing():
    assert resolve_image_digest("nvcr.io/nvidia/vllm@sha256:" + "b" * 64) \
        == "nvcr.io/nvidia/vllm@sha256:" + "b" * 64


def test_resolve_hf_rejects_mutable_on_identity():
    # the resolver returns an immutable sha; wiring it into ImmutableIdentity is the
    # profile layer's job — ensure the sha is a full 40-hex (passes the validator).
    from twinspark.schemas.profile import ImmutableIdentity
    ident = ImmutableIdentity(model_repo="org/model", model_revision=_SHA,
                              quantization="nvfp4", image="nvcr.io/nvidia/vllm",
                              image_digest="sha256:" + "c" * 64, vllm_version="0.x")
    assert ident.model_revision == _SHA


# ---- metrics ----------------------------------------------------------------

def test_metrics_parse_prometheus():
    text = """# TYPE vllm:num_requests_running gauge
vllm:num_requests_running 3
vllm:gpu_cache_usage_perc 0.42
vllm:generation_tokens_total{role=\"masked\"} 5
"""
    v = _parse_text(text)
    assert v["requests-running"] == 3.0
    assert v["kv-cache-usage"] == 0.42


def test_metrics_ignores_histogram_buckets():
    text = ('vllm:time_to_first_tokens_seconds_bucket{le="0.1"} 4\n'
            'vllm:time_to_first_tokens_seconds_sum 1.5\n'
            'vllm:time_to_first_tokens_seconds_count 7\n')
    v = _parse_text(text)
    assert "ttft-sum-s" in v and v["ttft-sum-s"] == 1.5
    assert v["ttft-count"] == 7.0


@pytest.mark.asyncio
async def test_scrape_metrics_ok():
    async def handler(request):
        return httpx.Response(200, text='vllm:num_requests_running 2\n', request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    snap = await scrape_metrics("http://x:8000", client=client)
    assert snap["ok"] and snap["requests-running"] == 2.0
    await client.aclose()


@pytest.mark.asyncio
async def test_scrape_metrics_404_is_clean():
    async def handler(request):
        return httpx.Response(404, request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    snap = await scrape_metrics("http://x:8000", client=client)
    assert not snap["ok"] and "no /metrics" in snap["error"]
    await client.aclose()


# ---- link test via dry-run cluster -----------------------------------------

@pytest.mark.asyncio
async def test_link_test_dry_run(cluster):
    result = await cluster.controller.run_link_test(duration_s=3, port=29511)
    init = result["initiator"]
    assert init["dry_run"] is True
    assert "bandwidth_gbps" in init and init["bandwidth_gbps"] > 0
    resp = result["responder"]
    assert resp["dry_run"] is True


@pytest.mark.asyncio
async def test_headless_apply_dry_run(cluster):
    result = await cluster.controller.headless_apply("headless-max")
    assert result["mode"] == "headless-max"
    for res in result["results"].values():
        assert res["dry_run"] is True
        ops = [st["op"] for st in res["steps"]]
        assert ops == ["boot_target", "display_manager"]


# ---- API wiring + web mount (dry-run safe) ---------------------------------

def test_new_routes_and_web_mount(cluster):
    from fastapi.testclient import TestClient

    from twinspark.controller.app import create_app

    app = create_app(cluster.controller, "mgmt-key", run_startup=False)
    h = {"x-api-key": "mgmt-key"}
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200 and "app.js" in r.text
        assert client.get("/api/v1/health").json()["version"]

        r = client.get("/api/v1/cookbook", headers=h)
        assert r.status_code == 200
        names = {x["name"] for x in r.json()["recipes"]}
        assert "glm-5.3-flash-nvfp4-tp2" in names

        r = client.post("/api/v1/cookbook/import/glm-5.3-flash-nvfp4-tp2", headers=h)
        assert r.status_code == 200
        assert r.json()["profile"]["name"] == "glm-5.3-flash-nvfp4-tp2"
        r = client.post("/api/v1/cookbook/import/glm-5.3-flash-nvfp4-tp2", headers=h)
        assert r.status_code == 409
        imported = client.get("/api/v1/cookbook", headers=h).json()["recipes"]
        glm = next(x for x in imported if x["name"] == "glm-5.3-flash-nvfp4-tp2")
        assert glm["imported_as"] == ["glm-5.3-flash-nvfp4-tp2"]

        r = client.get("/api/v1/profiles?summary=true", headers=h)
        row = next(x for x in r.json() if x["name"] == "glm-5.3-flash-nvfp4-tp2")
        assert row["pinned"] is False and row["model"] == "RedHatAI/GLM-5.3-Flash-NVFP4"

        r = client.get("/api/v1/profiles/glm-5.3-flash-nvfp4-tp2/draft/launch-plan", headers=h)
        assert r.status_code == 200 and r.json()["unpinned"] is True
        cmd = " ".join(r.json()["commands"].values())
        assert "--tensor-parallel-size 2" in cmd and "--block-size 2304" in cmd

        r = client.post("/api/v1/profiles/glm-5.3-flash-nvfp4-tp2/activate", headers=h)
        assert r.status_code == 422 and "not pinned" in r.json()["detail"]

        r = client.post("/api/v1/system/link-test",
                        json={"duration_s": 2, "port": 29511}, headers=h)
        assert r.status_code == 200
        assert r.json()["initiator"]["dry_run"] is True and r.json()["initiator"]["bandwidth_gbps"] > 0

        r = client.get("/api/v1/system/metrics", headers=h)
        assert r.status_code == 200 and r.json()["ok"] is False   # nothing running

        r = client.post("/api/v1/system/headless", json={"mode": "headless-safe"}, headers=h)
        assert r.status_code == 200 and r.json()["mode"] == "headless-safe"
        r = client.post("/api/v1/system/headless", json={"mode": "bogus"}, headers=h)
        assert r.status_code == 422

        r = client.get("/api/v1/system/doctor", headers=h)
        assert r.status_code == 200 and r.json()["checks"]
        assert client.get("/api/v1/system/rdma", headers=h).status_code == 200
        assert client.get("/api/v1/system/foreign", headers=h).json() == {"A": [], "B": []}
        assert client.get("/api/v1/models/files", headers=h).status_code == 200
        assert client.get("/api/v1/mods", headers=h).status_code == 200
