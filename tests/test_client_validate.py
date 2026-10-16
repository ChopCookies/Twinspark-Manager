"""validate_endpoint against a fake OpenAI-compatible gateway (in-process ASGI; no network, no real model)."""
from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from twinspark.client.adapters import Check, Endpoint, checks_passed, validate_endpoint

KEY = "inference-key-Zq8vN2rT5wX7"
BASE = "http://gateway.test/v1"


class FakeGateway:
    """A tiny TwinSpark-like gateway: /v1/models with TwinSpark metadata and /v1/chat/completions with
    switchable behaviour for chat, streaming and tool calls."""

    def __init__(self, key=KEY, aliases=None, tools="ok", stream="ok", chat="ok", models_status=200,
                 delay=0.0, echo_auth=False):
        self.key = key
        self.aliases = aliases if aliases is not None else {"glm": {"max_model_len": 131072, "revision": "rev-1"}}
        self.tools, self.stream, self.chat = tools, stream, chat
        self.models_status = models_status
        self.delay = delay
        self.echo_auth = echo_auth
        self.requests: list[dict] = []

    def _auth(self, request):
        if self.key is None:
            return None
        if request.headers.get("authorization") != f"Bearer {self.key}":
            said = request.headers.get("authorization", "") if self.echo_auth else "invalid api key"
            return JSONResponse({"error": {"message": said, "type": "invalid_api_key"}}, status_code=401)
        return None

    async def models(self, request: Request):
        self.requests.append({"path": "models"})
        denied = self._auth(request)
        if denied:
            return denied
        if self.models_status != 200:
            return JSONResponse({"error": {"message": "backend exploded"}}, status_code=self.models_status)
        return JSONResponse({"object": "list", "data": [
            {"id": alias, "object": "model", "owned_by": "twinspark", "root": f"org/{alias}-model",
             "max_model_len": meta.get("max_model_len"),
             "twinspark": {"alias": alias, "status": meta.get("status", "serving"), "revision": meta.get("revision"),
                           "max_model_len": meta.get("max_model_len")}}
            for alias, meta in self.aliases.items()]})

    @staticmethod
    def _reply(content=None, tool_calls=None, reasoning=None):
        message = {"role": "assistant", "content": content}
        if tool_calls is not None:
            message["tool_calls"] = tool_calls
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        return JSONResponse({"id": "c1", "object": "chat.completion",
                             "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]})

    async def completions(self, request: Request):
        denied = self._auth(request)
        if denied:
            return denied
        body = await request.json()
        self.requests.append({"path": "chat", "body": body})
        if self.delay:
            await asyncio.sleep(self.delay)
        if body.get("model") not in self.aliases:
            return JSONResponse({"error": {"message": f"unknown model/alias: {body.get('model')!r}",
                                           "type": "model_not_found"}}, status_code=404)
        messages = body.get("messages") or []
        if body.get("tools"):
            if messages and messages[-1].get("role") == "tool":
                if self.tools == "loop":
                    return self._reply(None, [{"id": "c2", "type": "function", "function": {
                        "name": "twinspark_echo", "arguments": "{\"value\": \"again\"}"}}])
                return self._reply(f"The tool returned {messages[-1]['content']}.")
            if self.tools == "400":
                return JSONResponse({"error": {"message": '"auto" tool choice requires --enable-auto-tool-choice'}},
                                    status_code=400)
            if self.tools == "none":
                return self._reply("I would call twinspark_echo now.")
            if self.tools == "raw-text":
                return self._reply('<tool_call>{"name": "twinspark_echo", "arguments": {"value": "twinspark"}}'
                                   "</tool_call>")
            arguments = {"ok": '{"value": "twinspark"}', "loop": '{"value": "twinspark"}',
                         "bad-json": '{"value": "twin', "wrong-schema": '{"text": 3}'}[self.tools]
            return self._reply(None, [{"id": "call_1", "type": "function",
                                       "function": {"name": "twinspark_echo", "arguments": arguments}}])
        if body.get("stream"):
            return self._stream()
        if self.chat == "500":
            return JSONResponse({"error": {"message": "engine died"}}, status_code=500)
        if self.chat == "503":
            return JSONResponse({"error": {"message": "model is switching"}}, status_code=503)
        if self.chat == "huge":
            return Response(b'{"x": "' + b"a" * (200 * 1024) + b'"}', media_type="application/json")
        if self.chat == "reasoning":
            return self._reply("", reasoning="Thinking about how to say ready...")
        return self._reply("ready")

    def _stream(self):
        def events():
            if self.stream == "json":
                yield b'data: {"choices": [{"delta": {"content": "ready"}}]}\n\n'
                return
            yield b'data: {"choices": [{"delta": {"role": "assistant"}}]}\n\n'
            yield b'data: {"choices": [{"delta": {"content": "rea"}}]}\n\n'
            yield b'data: {"choices": [{"delta": {"content": "dy"}}]}\n\n'
            if self.stream != "no-done":
                yield b"data: [DONE]\n\n"
        media = "application/json" if self.stream == "json" else "text/event-stream"
        return StreamingResponse(events(), media_type=media)

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/v1/models", self.models),
                                 Route("/v1/chat/completions", self.completions, methods=["POST"])])

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()))

    def endpoint(self, alias="glm", key=KEY, context=None, revision="rev-1") -> Endpoint:
        meta = self.aliases.get(alias, {})
        return Endpoint(BASE, alias, key, context if context is not None else meta.get("max_model_len"),
                        revision_id=revision)


def by_id(checks: list[Check]) -> dict[str, Check]:
    return {c.id: c for c in checks}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_all_checks_pass_and_report_alias_metadata():
    gw = FakeGateway()
    async with gw.client() as http:
        checks = await validate_endpoint(BASE, KEY, "glm", http=http, min_context=64000)
    ids = [c.id for c in checks]
    assert ids == ["transport", "auth", "alias", "context", "chat", "streaming", "tools"]
    assert all(c.status == "pass" for c in checks), [(c.id, c.status, c.detail) for c in checks]
    alias = by_id(checks)["alias"]
    assert alias.data["max_model_len"] == 131072 and alias.data["revision_id"] == "rev-1"
    assert "131,072" in alias.detail and "rev-1" in alias.detail
    assert checks_passed(checks)
    # The tool round trip sent the echo result back as a tool message with the call's ID.
    second = [r["body"] for r in gw.requests if r["path"] == "chat" and r["body"].get("tools")][-1]
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "twinspark"}
    assert second["tool_choice"] == "none"


async def test_connection_refused_is_transport_and_skips_the_rest():
    url = f"http://127.0.0.1:{free_port()}/v1"
    checks = await validate_endpoint(url, KEY, "glm", timeout=5)
    first = checks[0]
    assert (first.id, first.layer, first.status) == ("transport", "transport", "fail")
    assert "refused" in first.detail and "forward" in first.detail
    assert [c.status for c in checks[1:]] == ["skip"] * 5
    assert not checks_passed(checks)


async def test_connect_error_from_transport_is_transport():
    def boom(request):
        raise httpx.ConnectError("Name or service not known")
    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as http:
        checks = await validate_endpoint(BASE, KEY, "glm", http=http)
    assert checks[0].layer == "transport" and checks[0].status == "fail"


async def test_wrong_key_is_auth_and_never_echoed():
    wrong = "management-key-Kp4sW9xL2mQ7"
    gw = FakeGateway(echo_auth=True)              # a careless server repeats the Authorization header
    async with gw.client() as http:
        checks = await validate_endpoint(BASE, wrong, "glm", http=http)
    c = by_id(checks)
    assert (c["transport"].status, c["auth"].status, c["auth"].layer) == ("pass", "fail", "auth")
    assert "401" in c["auth"].detail and "management key" in c["auth"].detail
    assert all(x.status == "skip" for x in checks[2:])
    assert wrong not in json.dumps([x.to_dict() for x in checks])


async def test_unknown_alias_is_model_layer_with_served_aliases():
    gw = FakeGateway(aliases={"default": {"max_model_len": 65536}, "secondary": {"max_model_len": 32768}})
    async with gw.client() as http:
        checks = await validate_endpoint(BASE, KEY, "deepseek-chat", http=http)
    alias = by_id(checks)["alias"]
    assert (alias.layer, alias.status) == ("model", "fail")
    assert "default, secondary" in alias.detail
    assert alias.data["served_aliases"] == ["default", "secondary"]
    assert all(c.status == "skip" for c in checks[3:])


@pytest.mark.parametrize("mode", ["bad-json", "none", "raw-text", "wrong-schema", "400", "loop"])
async def test_broken_tool_calls_are_parser_layer(mode):
    gw = FakeGateway(tools=mode)
    async with gw.client() as http:
        checks = await validate_endpoint(BASE, KEY, "glm", http=http)
    c = by_id(checks)
    assert c["chat"].status == "pass" and c["streaming"].status == "pass"
    assert (c["tools"].layer, c["tools"].status) == ("parser", "fail"), c["tools"].detail
    assert "parser" in c["tools"].detail or "template" in c["tools"].detail or "schema" in c["tools"].detail


@pytest.mark.parametrize("status,word", [("500", "HTTP 500"), ("503", "switching")])
async def test_server_errors_are_model_layer(status, word):
    gw = FakeGateway(chat=status)
    async with gw.client() as http:
        checks = await validate_endpoint(BASE, KEY, "glm", http=http, tools=False)
    chat = by_id(checks)["chat"]
    assert (chat.layer, chat.status) == ("model", "fail") and word in chat.detail
    assert "tools" not in by_id(checks)


async def test_model_list_5xx_is_model_layer():
    gw = FakeGateway(models_status=502)
    async with gw.client() as http:
        checks = await validate_endpoint(BASE, KEY, "glm", http=http)
    c = by_id(checks)
    assert c["transport"].status == "pass" and c["auth"].status == "skip"
    assert (c["alias"].layer, c["alias"].status) == ("model", "fail")


async def test_context_below_minimum_fails_and_unknown_context_warns():
    gw = FakeGateway(aliases={"small": {"max_model_len": 32768}, "unknown": {}})
    async with gw.client() as http:
        small = by_id(await validate_endpoint(BASE, KEY, "small", http=http, min_context=64000, tools=False))
        unknown = by_id(await validate_endpoint(BASE, KEY, "unknown", http=http, min_context=64000, tools=False))
    assert small["context"].status == "fail" and "32,768" in small["context"].detail
    assert "raising the number" in small["context"].detail
    assert unknown["context"].status == "warn"


@pytest.mark.parametrize("mode,word", [("no-done", "[DONE]"), ("json", "server-sent events")])
async def test_broken_streams_fail(mode, word):
    gw = FakeGateway(stream=mode)
    async with gw.client() as http:
        streaming = by_id(await validate_endpoint(BASE, KEY, "glm", http=http, tools=False))["streaming"]
    assert streaming.status == "fail" and word in streaming.detail


async def test_responses_are_bounded_in_size_and_time():
    huge = FakeGateway(chat="huge")
    async with huge.client() as http:
        chat = by_id(await validate_endpoint(BASE, KEY, "glm", http=http, tools=False))["chat"]
    assert chat.status == "fail" and "KiB" in chat.detail
    slow = FakeGateway(delay=2.0)
    async with slow.client() as http:
        checks = by_id(await validate_endpoint(BASE, KEY, "glm", http=http, timeout=0.3, tools=False))
    assert checks["chat"].status == "fail" and checks["chat"].layer == "model"
    assert "0 s" in checks["chat"].detail or "within" in checks["chat"].detail


async def test_reasoning_only_reply_warns_instead_of_failing():
    gw = FakeGateway(chat="reasoning")
    async with gw.client() as http:
        chat = by_id(await validate_endpoint(BASE, KEY, "glm", http=http, tools=False))["chat"]
    assert chat.status == "warn" and "reasoning" in chat.detail


async def test_invalid_base_url_is_transport():
    checks = await validate_endpoint("ftp://gateway/v1", KEY, "glm")
    assert checks[0].layer == "transport" and checks[0].status == "fail"
    assert all(c.status == "skip" for c in checks[1:])


async def test_gateway_without_key_still_validates():
    gw = FakeGateway(key=None)
    async with gw.client() as http:
        checks = by_id(await validate_endpoint(BASE, None, "glm", http=http))
    assert checks["auth"].status == "pass" and "without an inference key" in checks["auth"].detail
    assert checks["tools"].status == "pass"


def test_endpoint_never_shows_its_key():
    ep = Endpoint("http://127.0.0.1:8000", "glm", KEY, 131072)
    assert ep.base_url == "http://127.0.0.1:8000/v1"
    assert KEY not in repr(ep) and KEY not in json.dumps(ep.to_dict())
    assert ep.to_dict()["has_api_key"] is True
    with pytest.raises(ValueError) as exc:
        Endpoint(BASE, "glm", "two words", 131072)
    assert "two words" not in str(exc.value)
    with pytest.raises(ValueError):
        Endpoint(BASE, "has space", KEY, 131072)
    with pytest.raises(ValueError):
        Endpoint(BASE, "glm", KEY, True)
    with pytest.raises(ValueError):
        Endpoint(BASE, "glm", KEY, 1000, source="cloud")
