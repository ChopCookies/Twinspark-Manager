"""Actual body-size bounds and authentication order after merging the API routes."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request

from twinspark.api.body_limit import BodyLimitMiddleware
from twinspark.controller.app import create_app


def echo_app(json_limit=16, upload_limit=32, events=None):
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, json_limit=json_limit, upload_limit=upload_limit)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def echo(request: Request):
        if events is not None:
            events.append("handler")
        return {"size": len(await request.body()), "content_length": request.headers.get("content-length")}

    return app


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                             base_url="http://controller")


def controller_app(cluster):
    app = create_app(cluster.controller, "management-key", run_startup=False, background=False)
    # Keep the real assembly order: authentication wraps the body limiter. Outside
    # BaseHTTPMiddleware, receive errors would instead become ExceptionGroups.
    limiter = next(m for m in app.user_middleware if m.cls is BodyLimitMiddleware)
    limiter.kwargs.update(json_limit=16, upload_limit=32)
    return app


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_multiple_chunks_are_bounded_without_content_length(method):
    consumed = 0

    async def body():
        nonlocal consumed
        for _ in range(8):
            consumed += 1
            yield b"x" * 8

    async with client(echo_app()) as session:
        response = await session.request(method, "/api/v1/profiles", content=body())
    assert response.status_code == 413, response.text
    assert response.json() == {"detail": "request body exceeds the limit of 16 bytes"}
    assert consumed == 3


async def test_exact_limit_is_accepted_and_body_is_not_prebuffered():
    events = []

    async def body():
        assert events == ["handler"]
        yield b"x" * 8
        yield b"y" * 8

    async with client(echo_app(events=events)) as session:
        response = await session.post("/api/v1/profiles", content=body())
    assert response.status_code == 200, response.text
    assert response.json() == {"size": 16, "content_length": None}


async def test_small_declared_content_length_does_not_bypass_actual_limit():
    async def body():
        yield b"x" * 16
        yield b"private-submitted-value"

    async with client(echo_app()) as session:
        response = await session.post("/api/v1/profiles", content=body(), headers={"content-length": "1"})
    assert response.status_code == 413, response.text
    assert "private-submitted-value" not in response.text


@pytest.mark.parametrize("size, expected", [(32, 200), (33, 413)])
async def test_mod_upload_uses_larger_limit(size, expected):
    async def body():
        yield b"x" * 16
        yield b"y" * (size - 16)

    async with client(echo_app()) as session:
        response = await session.post("/api/v1/mods/custom", content=body())
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json()["size"] == size


@pytest.mark.parametrize("method, path", [("GET", "/api/v1/profiles"), ("POST", "/outside-api")])
async def test_other_requests_pass_through(method, path):
    async def body():
        yield b"x" * 40

    async with client(echo_app()) as session:
        response = await session.request(method, path, content=body())
    assert response.status_code == 200, response.text
    assert response.json()["size"] == 40


async def test_websocket_scope_and_receive_are_unchanged():
    original_scope = {"type": "websocket", "path": "/api/v1/remote/terminal/ws"}

    async def receive():
        raise AssertionError("middleware read the WebSocket")

    async def send(message):
        raise AssertionError("middleware sent a WebSocket response")

    async def app(scope, downstream_receive, downstream_send):
        assert scope is original_scope
        assert downstream_receive is receive
        assert downstream_send is send

    await BodyLimitMiddleware(app)(original_scope, receive, send)


@pytest.mark.parametrize("path", [
    "/api/v1/profiles",
    "/api/v1/cookbook/integrate",
    "/api/v1/integrations/check",
    "/api/v1/integrations/evaluations",
])
async def test_controller_routes_reject_oversized_chunked_bodies(cluster, path):
    app = controller_app(cluster)

    async def body():
        yield b"x" * 8
        yield b"y" * 9

    async with client(app) as session:
        response = await session.post(path, content=body(), headers={"x-api-key": "management-key"})
    assert response.status_code == 413, response.text
    assert not cluster.controller.store.list_jobs()


async def test_controller_authenticates_before_consuming_chunked_data(cluster):
    app = controller_app(cluster)
    consumed = 0

    async def body():
        nonlocal consumed
        consumed += 1
        yield b"x" * 100

    async with client(app) as session:
        response = await session.post("/api/v1/integrations/check", content=body(),
                                      headers={"x-api-key": "wrong-key"})
    assert response.status_code == 401, response.text
    assert consumed == 0
