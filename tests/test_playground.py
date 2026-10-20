"""The chat playground's endpoint (master plan §4.1): the management session, not an app key; the stream
passes through as it comes; nothing but chat, completions and the model list."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from twinspark.client.ssh import free_local_port
from twinspark.controller.app import create_app

KEY = "mgmt-key"


@pytest.fixture
def backend():
    seen: list[dict] = []

    async def chat(request):
        seen.append({"headers": dict(request.headers), "body": await request.json()})

        async def gen():
            for i in range(5):
                yield f'data: {{"choices":[{{"delta":{{"content":"t{i} "}}}}]}}\n\n'.encode()
                await asyncio.sleep(0.15)
            yield b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":5}}\n\n'
            yield b"data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    async def models(request):
        return JSONResponse({"data": []})
    port = free_local_port(19800)
    app = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]),
                            Route("/v1/models", models, methods=["GET"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}", seen
    server.should_exit = True
    t.join(10)


@pytest.fixture
def served(cluster):
    """The management app on a real loopback port, so the test sees the stream's timing as a browser would."""
    c = cluster.controller
    app = create_app(c, KEY, run_startup=False, background=False)
    port = free_local_port(19850)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    yield c, f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(10)


def test_a_chat_streams_through_with_the_management_key_only(backend, served):
    url, seen = backend
    c, base = served
    c.gateway.set_route("coder", [url], "served-model", "rev-1")
    body = {"model": "coder", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            "stream_options": {"include_usage": True}}
    arrivals = []
    with httpx.Client(base_url=base, timeout=20, headers={"accept-encoding": "gzip"}) as client:
        with client.stream("POST", "/api/v1/playground/v1/chat/completions", json=body,
                           headers={"x-api-key": KEY}) as r:
            assert r.status_code == 200, r.read()
            assert "gzip" not in r.headers.get("content-encoding", "")      # tokens are not held back
            text = ""
            for chunk in r.iter_text():
                arrivals.append(time.monotonic())
                text += chunk
        assert text.count("data: ") == 7 and text.endswith("[DONE]\n\n")
        assert arrivals[-1] - arrivals[0] > 0.4                              # it arrived piece by piece
        sent = seen[0]
        assert sent["body"]["model"] == "served-model"
        assert KEY not in json.dumps(sent["headers"])                     # the management key stays here
        assert c.gateway.usage == {}                                       # not an app's usage
        assert client.get("/api/v1/playground/v1/models", headers={"x-api-key": KEY}).status_code == 200
        # without the management key, or with an app key only: refused
        assert client.post("/api/v1/playground/v1/chat/completions", json=body).status_code == 401
        token, _ = c.create_key("an app")
        assert client.post("/api/v1/playground/v1/chat/completions", json=body,
                           headers={"authorization": f"Bearer {token}"}).status_code == 401
        # nothing else of the OpenAI API
        r = client.post("/api/v1/playground/v1/embeddings", json={"model": "coder", "input": "x"},
                        headers={"x-api-key": KEY})
        assert r.status_code == 404 and "chat/completions" in r.json()["detail"]
        r = client.post("/api/v1/playground/v1/chat/completions", json=body,
                        headers={"x-api-key": KEY, "origin": "https://evil.example"})
        assert r.status_code == 403                                        # CSRF rules as for every /api route
