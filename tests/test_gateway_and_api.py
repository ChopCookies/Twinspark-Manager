import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from twinspark.controller.app import create_app
from twinspark.gateway.app import build_gateway_app
from twinspark.gateway.gateway import Gateway
from twinspark.schemas.enums import Topology

from .conftest import draft

seen: list[dict] = []


def fake_vllm() -> Starlette:
    async def chat(request: Request):
        body = await request.json()
        seen.append({"path": request.url.path, "model": body["model"],
                     "auth": request.headers.get("authorization")})
        if body.get("stream"):
            async def gen():
                for i in range(3):
                    yield f"data: {json.dumps({'i': i})}\n\n".encode()
                yield b"data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse({"model": body["model"], "path": request.url.path})
    return Starlette(routes=[Route("/v1/{p:path}", chat, methods=["POST"])])


@pytest.fixture
def gw():
    seen.clear()
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_vllm()))
    g = Gateway("client-key", "backend-key", client=upstream)
    g.set_route("default", ["http://vllm"], "qwen", "qwen-r1-x")
    app = build_gateway_app(g)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw",
                               headers={"authorization": "Bearer client-key"})
    return g, client


async def test_alias_rewrite_and_endpoint_passthrough(gw):
    g, client = gw
    r = await client.post("/v1/completions", json={"model": "default", "prompt": "x"})
    assert r.status_code == 200 and r.json() == {"model": "qwen", "path": "/v1/completions"}
    assert seen[-1]["auth"] == "Bearer backend-key"            # client key never forwarded
    r = await client.post("/v1/chat/completions", json={"model": "qwen", "messages": []})
    assert r.json()["path"] == "/v1/chat/completions"         # served name also accepted


async def test_streaming_passthrough(gw):
    g, client = gw
    async with client.stream("POST", "/v1/chat/completions",
                             json={"model": "default", "stream": True, "messages": []}) as r:
        body = b"".join([c async for c in r.aiter_bytes()])
    assert r.headers["content-type"].startswith("text/event-stream")
    assert body.count(b"data:") == 4 and body.endswith(b"[DONE]\n\n")
    assert g.routes["default"].inflight == 0                   # released after the stream


async def test_draining_returns_503_with_retry_after(gw):
    g, client = gw
    g.begin_drain("default", retry_after=17)
    r = await client.post("/v1/chat/completions", json={"model": "default"})
    assert r.status_code == 503 and r.headers["retry-after"] == "17"
    assert r.json()["error"]["state"]["status"] == "switching"


async def test_auth_and_unknown_model(gw):
    g, client = gw
    r = await client.post("/v1/chat/completions", json={"model": "default"},
                          headers={"authorization": "Bearer nope"})
    assert r.status_code == 401
    r = await client.get("/v1/models", headers={"authorization": ""})
    assert r.status_code == 401
    r = await client.post("/v1/chat/completions", json={"model": "gpt-9"})
    assert r.status_code == 404
    models = (await client.get("/v1/models")).json()["data"]
    assert [m["id"] for m in models] == ["default"]


async def test_wait_idle_waits_for_inflight(gw):
    g, _ = gw
    st = g.routes["default"]
    st.acquire()
    assert await g.wait_idle("default", 0.05) is False
    asyncio.get_running_loop().call_later(0.02, st.release)
    assert await g.wait_idle("default", 1) is True


async def test_backend_down_gives_502():
    class Boom(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("refused")
    g = Gateway(None, client=httpx.AsyncClient(transport=Boom()))
    g.set_route("default", ["http://vllm"], "qwen")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(g)), base_url="http://gw")
    r = await client.post("/v1/chat/completions", json={"model": "default"})
    assert r.status_code == 502 and g.routes["default"].inflight == 0


async def test_oversized_request_bodies_are_refused_before_parsing():
    seen.clear()
    g = Gateway("k", client=httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_vllm())))
    g.set_route("default", ["http://vllm"], "qwen")
    app = build_gateway_app(g, max_request_bytes=1000)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw",
                               headers={"authorization": "Bearer k"})
    big = {"model": "default", "messages": [{"role": "user", "content": "x" * 2000}]}
    r = await client.post("/v1/chat/completions", json=big)
    assert r.status_code == 413 and r.json()["error"]["type"] == "invalid_request_error"

    async def chunks():                                     # no Content-Length: counted while reading
        for _ in range(5):
            yield b" " * 400

    r = await client.post("/v1/chat/completions", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413 and not seen
    ok = await client.post("/v1/chat/completions", json={"model": "default", "messages": []})
    assert ok.status_code == 200 and len(seen) == 1


# ---- management API ----------------------------------------------------------------
def test_management_api_auth_and_profile_flow(cluster):
    app = create_app(cluster.controller, "mgmt-key", run_startup=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/profiles").status_code == 401
        h = {"x-api-key": "mgmt-key"}
        d = draft("qwen", Topology.TP2).model_dump(mode="json")
        assert client.post("/api/v1/profiles", json=d, headers=h).status_code == 201
        assert client.post("/api/v1/profiles", json=d, headers=h).status_code == 409
        plan = client.get("/api/v1/profiles/qwen/revisions/latest/launch-plan", headers=h).json()
        assert len(plan["commands"]) == 2
        r = client.post("/api/v1/profiles/qwen/duplicate", params={"new_name": "qwen-long"}, headers=h)
        assert r.status_code == 201
        d2 = draft("qwen-long", Topology.TP2, context_length=131072).model_dump(mode="json")
        assert client.post("/api/v1/profiles/qwen-long/revisions", json=d2, headers=h).status_code == 201
        diff = client.get("/api/v1/profiles/qwen-long/compare", params={"a": "r1", "b": "r2"},
                          headers=h).json()
        assert diff["changed"]["simple.context_length"] == {"from": 32768, "to": 131072}
        # cross-origin write is rejected
        r = client.post("/api/v1/stop", headers={**h, "origin": "http://evil.example"})
        assert r.status_code == 403


def test_estimate_refuses_unknown_models(cluster):
    app = create_app(cluster.controller, "k", run_startup=False)
    with TestClient(app) as client:
        r = client.post("/api/v1/system/memory/estimate", json={"repo": "who/knows"},
                        headers={"x-api-key": "k"})
        assert r.status_code == 422
        cfg = {"hidden_size": 4096, "num_attention_heads": 32, "num_key_value_heads": 8,
               "num_hidden_layers": 36, "num_params": 30_000_000_000}
        r = client.post("/api/v1/system/memory/estimate",
                        json={"hf_config": cfg, "topology": "tp2", "context_length": 65536},
                        headers={"x-api-key": "k"})
        assert r.status_code == 200 and r.json()["fits"] is True
