"""Agent/harness wire contracts, without calling real models or executing tools."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from twinspark.gateway.app import build_gateway_app
from twinspark.gateway.gateway import Gateway


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, close_error=False):
        self.chunks = chunks
        self.closed = False
        self.close_error = close_error

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True
        if self.close_error:
            raise httpx.CloseError("close failed")


def response(status=200, body=None, *, headers=None, stream=None):
    return httpx.Response(status, headers=headers or {"content-type": "application/json"},
                          stream=stream or Chunks([json.dumps(body or {}).encode()]))


@asynccontextmanager
async def gateway_client(handler, *, replicas=False, max_response_bytes=None):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = Gateway("client-secret", "backend-secret", client=upstream)
    gateway.set_route("default", ["http://node-a", "http://replica"] if replicas else ["http://node-a"],
                      "profile-a", "split-r1", 32768)
    gateway.set_route("coder", ["http://node-b"], "profile-b", "split-r1", 65536)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(
        gateway, max_response_bytes=max_response_bytes)),
                                 base_url="http://gateway",
                                 headers={"authorization": "Bearer client-secret"}) as client:
        try:
            yield gateway, client
        finally:
            await gateway.aclose()


@pytest.mark.parametrize("alias,node,served", [("default", "node-a", "profile-a"),
                                               ("coder", "node-b", "profile-b")])
async def test_agent_tool_result_turn_and_structured_output_are_preserved(alias, node, served):
    call = {"id": "call_1", "type": "function",
            "function": {"name": "inspect_recipe", "arguments": '{"recipe":"qwen"}'}}
    body = {
        "model": alias,
        "messages": [{"role": "developer", "content": "Inspect the chosen recipe."},
                     {"role": "assistant", "content": None, "tool_calls": [call]},
                     {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'}],
        "tools": [{"type": "function", "function": {"name": "inspect_recipe", "strict": True,
                  "parameters": {"type": "object", "properties": {"recipe": {"type": "string"}},
                                 "required": ["recipe"], "additionalProperties": False}}}],
        "tool_choice": "auto", "parallel_tool_calls": False,
        "response_format": {"type": "json_schema", "json_schema": {"name": "result", "strict": True,
                            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                       "required": ["ok"], "additionalProperties": False}}},
        "structured_outputs": {"choice": ["yes", "no"]},
        "stream_options": {"include_usage": True},
    }
    result = {"id": "chat_1", "choices": [{"message": {"role": "assistant", "tool_calls": [call]},
                                              "finish_reason": "tool_calls"}], "model": served}
    seen = []

    async def backend(request):
        seen.append(request)
        return response(body=result)

    async with gateway_client(backend) as (gateway, client):
        reply = await client.post("/v1/chat/completions", json=body)
        assert reply.status_code == 200 and reply.json() == result
        assert seen[0].url.host == node
        assert json.loads(seen[0].content) == {**body, "model": served}
        assert seen[0].headers["authorization"] == "Bearer backend-secret"
        assert "client-secret" not in str(seen[0].headers)
        assert gateway.routes[alias].inflight == 0


async def test_responses_native_payload_and_unsupported_backend_error_pass_through():
    body = {"model": "coder", "instructions": "Inspect recipes", "previous_response_id": "resp_0",
            "input": [{"type": "function_call_output", "call_id": "call_1", "output": '{"ok":true}'}],
            "tools": [{"type": "function", "name": "inspect_recipe", "parameters": {"type": "object"}}],
            "text": {"format": {"type": "json_schema", "name": "result", "schema": {"type": "object"}}}}
    seen = []
    error = {"error": {"message": "Responses is unavailable in this backend", "code": "not_found"}}

    async def backend(request):
        seen.append(request)
        return response(404, error)

    async with gateway_client(backend) as (_, client):
        reply = await client.post("/v1/responses", json=body)
        assert reply.status_code == 404 and reply.json() == error
        assert len(seen) == 1 and seen[0].url.path == "/v1/responses"
        assert json.loads(seen[0].content) == {**body, "model": "profile-b"}


@pytest.mark.parametrize("endpoint", ["chat/completions", "responses"])
async def test_fragmented_tool_stream_and_usage_are_forwarded_byte_for_byte(endpoint):
    chunks = [b'event: response.function_call_arguments.delta\n',
              b'data: {"delta":"{\\"name\\":\\""}\n\n',
              b'data: {"delta":"qwen\\"}"}\n\n',
              b'data: {"usage":{"completion_tokens":8}}\n\n', b'data: [DONE]\n\n']
    stream = Chunks(chunks)

    async def backend(request):
        return response(headers={"content-type": "text/event-stream", "x-request-id": "turn_1",
                                 "connection": "keep-alive, x-internal", "x-internal": "node-only",
                                 "proxy-authenticate": "internal"}, stream=stream)

    async with gateway_client(backend) as (gateway, client):
        reply = await client.post(f"/v1/{endpoint}", json={"model": "coder", "stream": True})
        assert reply.content == b"".join(chunks)
        assert reply.headers["content-type"] == "text/event-stream"
        assert reply.headers["x-request-id"] == "turn_1"
        assert not {"connection", "x-internal", "proxy-authenticate"} & reply.headers.keys()
        assert stream.closed and gateway.routes["coder"].inflight == 0


@pytest.mark.parametrize("model", [[], {}, ["coder"], 12, True, "", "   "])
async def test_invalid_model_is_a_client_error_without_backend_request(model):
    async def backend(request):
        raise AssertionError("invalid model reached the backend")

    async with gateway_client(backend) as (gateway, client):
        reply = await client.post("/v1/chat/completions", json={"model": model})
        assert reply.status_code == 400
        assert reply.json()["error"]["param"] == "model"
        assert gateway.resolve(model) is None or isinstance(model, str)
        assert all(route.requests == 0 for route in gateway.routes.values())


@pytest.mark.parametrize("failure,status", [(httpx.ReadTimeout, 504), (httpx.WriteTimeout, 504),
                                            (httpx.ReadError, 502), (httpx.RemoteProtocolError, 502)])
async def test_potentially_accepted_agent_turn_is_not_replayed_to_another_replica(failure, status):
    seen = []

    async def backend(request):
        seen.append(request.url.host)
        if request.url.host == "node-a":
            raise failure("first backend may have accepted the turn", request=request)
        return response(body={"would_duplicate": True})

    async with gateway_client(backend, replicas=True) as (gateway, client):
        reply = await client.post("/v1/responses", json={"model": "default", "input": "Inspect"})
        assert reply.status_code == status and seen == ["node-a"]
        assert gateway.routes["default"].inflight == 0
        assert gateway.routes["default"].errors == 1


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_connection_failure_can_use_the_other_replica(failure):
    seen = []

    async def backend(request):
        seen.append(request.url.host)
        if request.url.host == "node-a":
            raise failure("not sent", request=request)
        return response(body={"ok": True})

    async with gateway_client(backend, replicas=True) as (gateway, client):
        reply = await client.post("/v1/chat/completions", json={"model": "default"})
        assert reply.status_code == 200 and seen == ["node-a", "replica"]
        assert gateway.routes["default"].inflight == 0


@pytest.mark.parametrize("status", [502, 504])
async def test_gateway_error_status_does_not_replay_an_accepted_agent_turn(status):
    seen = []

    async def backend(request):
        seen.append(request.url.host)
        return response(status, {"error": {"message": "previous turn may have been accepted"}})

    async with gateway_client(backend, replicas=True) as (gateway, client):
        reply = await client.post("/v1/responses", json={"model": "default", "input": "Inspect"})
        assert reply.status_code == status and seen == ["node-a"]
        assert gateway.routes["default"].inflight == 0
        assert gateway.routes["default"].errors == 1


async def test_explicit_unavailable_replica_fails_over_and_close_error_does_not_strand_drain():
    seen = []
    rejected = Chunks([b'{"error":"starting"}'], close_error=True)

    async def backend(request):
        seen.append(request.url.host)
        if request.url.host == "node-a":
            return response(503, stream=rejected)
        return response(body={"ok": True})

    async with gateway_client(backend, replicas=True) as (gateway, client):
        for _ in range(3):
            reply = await client.post("/v1/chat/completions", json={"model": "default"})
            assert reply.status_code == 200 and reply.json() == {"ok": True}
        assert seen == ["node-a", "replica", "replica", "replica"]
        assert rejected.closed and gateway.routes["default"].inflight == 0
        gateway.begin_drain("default")
        assert await gateway.wait_idle("default", 0.01)


async def test_cancelled_agent_turn_before_headers_does_not_strand_a_deployment_drain():
    entered = asyncio.Event()

    async def backend(request):
        entered.set()
        await asyncio.Event().wait()

    async with gateway_client(backend) as (gateway, client):
        turn = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "coder"}))
        await asyncio.wait_for(entered.wait(), 1)
        assert gateway.routes["coder"].inflight == 1
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        gateway.begin_drain("coder")
        assert await gateway.wait_idle("coder", 0.01)
        assert gateway.routes["coder"].inflight == 0


async def test_backend_close_error_still_releases_agent_stream():
    stream = Chunks([b"data: [DONE]\n\n"], close_error=True)

    async def backend(request):
        return response(headers={"content-type": "text/event-stream"}, stream=stream)

    async with gateway_client(backend) as (gateway, client):
        reply = await client.post("/v1/chat/completions", json={"model": "default", "stream": True})
        assert reply.content == b"data: [DONE]\n\n"
        assert stream.closed and gateway.routes["default"].inflight == 0
        assert gateway.routes["default"].errors == 1


async def test_broken_tool_stream_is_not_reported_as_successful_end_of_stream():
    class InterruptedStream(Chunks):
        async def __aiter__(self):
            yield b'data: {"delta":"{\\"recipe\\":"}\n\n'
            raise httpx.ReadError("upstream lost during arguments")

    stream = InterruptedStream([])

    async def backend(request):
        return response(headers={"content-type": "text/event-stream"}, stream=stream)

    async with gateway_client(backend) as (gateway, client):
        with pytest.raises(httpx.ReadError):
            await client.post("/v1/chat/completions", json={"model": "coder", "stream": True})
        assert stream.closed and gateway.routes["coder"].inflight == 0
        assert gateway.routes["coder"].errors == 1
        assert "stream interrupted" in gateway.routes["coder"].last_error


async def test_caller_disconnect_closes_backend_stream_and_releases_drain():
    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'data: {"delta":"hello"}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            # The close is itself asynchronous and must survive Starlette's cancellation.
            await asyncio.sleep(0.01)
            self.closed = True

    stream = SlowStream()

    async def backend(request):
        return response(headers={"content-type": "text/event-stream"}, stream=stream)

    async with gateway_client(backend) as (gateway, _):
        request_sent = False
        disconnect = asyncio.Event()

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b'{"model":"coder","stream":true}',
                        "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                disconnect.set()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
                 "query_string": b"", "root_path": "",
                 "headers": [(b"authorization", b"Bearer client-secret"),
                             (b"content-type", b"application/json")],
                 "server": ("gateway", 80), "client": ("127.0.0.1", 1234)}
        await asyncio.wait_for(build_gateway_app(gateway)(scope, receive, send), 1)
        assert stream.closed and gateway.routes["coder"].inflight == 0
        gateway.begin_drain("coder")
        assert await gateway.wait_idle("coder", 0.01)


@pytest.mark.parametrize("upstream_status", [200, 400, 500])
async def test_probe_json_cap_stops_before_buffering_or_exposing_oversized_upstream(upstream_status):
    class CountedChunks(Chunks):
        consumed = 0

        async def __aiter__(self):
            for chunk in self.chunks:
                self.consumed += 1
                yield chunk

    stream = CountedChunks([b"x" * 16, b"PRIVATE_UPSTREAM_BODY" * 100, b"must not be consumed"])

    async def backend(request):
        return response(upstream_status, stream=stream)

    async with gateway_client(backend, max_response_bytes=32) as (gateway, client):
        reply = await client.post("/v1/chat/completions", json={"model": "coder"})
        assert reply.status_code == 502
        assert reply.json()["error"]["code"] == "backend_response_too_large"
        assert b"PRIVATE_UPSTREAM_BODY" not in reply.content
        assert stream.consumed == 2 and stream.closed
        assert gateway.routes["coder"].inflight == 0


async def test_probe_sse_cap_aborts_transfer_without_buffering_the_complete_stream():
    class CountedChunks(Chunks):
        consumed = 0

        async def __aiter__(self):
            for chunk in self.chunks:
                self.consumed += 1
                yield chunk

    stream = CountedChunks([b"data: hello\n\n", b"data: " + b"x" * 100, b"data: [DONE]\n\n"])

    async def backend(request):
        return response(headers={"content-type": "text/event-stream"}, stream=stream)

    async with gateway_client(backend, max_response_bytes=32) as (gateway, client):
        with pytest.raises(httpx.ReadError, match="size limit"):
            await client.post("/v1/chat/completions", json={"model": "coder", "stream": True})
        assert stream.consumed == 2 and stream.closed
        assert gateway.routes["coder"].inflight == 0
        assert gateway.routes["coder"].errors == 1


@pytest.mark.parametrize("max_response_bytes", [None, 128])
async def test_normal_gateway_is_uncapped_and_probe_allows_exact_byte_budget(max_response_bytes):
    data = b"x" * 128
    stream = Chunks([data[:32], data[32:]])

    async def backend(request):
        return response(headers={"content-type": "application/octet-stream"}, stream=stream)

    async with gateway_client(backend, max_response_bytes=max_response_bytes) as (gateway, client):
        reply = await client.post("/v1/chat/completions", json={"model": "coder"})
        assert reply.status_code == 200 and reply.content == data
        assert stream.closed and gateway.routes["coder"].inflight == 0
