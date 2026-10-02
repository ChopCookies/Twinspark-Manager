from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.conftest import draft
from twinspark.controller import compatibility
from twinspark.controller.compatibility import check_alias
from twinspark.controller.controller import BusyError
from twinspark.controller.integrations import catalog
from twinspark.schemas.enums import Topology


def serving(cluster, tools=False):
    ctrl = cluster.controller
    recipe = draft(topology=Topology.SINGLE_A, tool_calling=tools)
    if tools:
        recipe.behaviour.tool_call_parser = "hermes"
    profile = ctrl.create_profile(recipe)
    rev = profile.latest()
    cluster.gateway.set_route("default", ["http://mock-backend"], "qwen",
                              revision_id=rev.revision_id, max_model_len=32768)
    return rev


def runtime(monkeypatch, cluster, mode="docker"):
    async def facts(action, **kwargs):
        assert action == "hardware_facts"
        return {"runtime_mode": mode}
    monkeypatch.setattr(cluster.controller.agents["A"], "call", facts)


async def install_backend(cluster, handler):
    await cluster.gateway._client.aclose()

    def upstream(request):
        response = handler(request)
        if response.is_stream_consumed:
            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=httpx.ByteStream(response.content))
        return response

    cluster.gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))


def answer(content):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


async def test_dry_run_checks_never_generate_or_certify(cluster):
    rev = serving(cluster)

    def forbid(request):
        pytest.fail("dry-run checks sent inference")

    await install_backend(cluster, forbid)
    job = await check_alias(cluster.controller, "default")
    result = await cluster.wait_job(job.job_id)
    assert result.state.value == "completed"
    assert result.payload["dry_run"] is True
    assert result.payload["compatible"] is None
    assert {c["status"] for c in result.payload["checks"]} == {"simulated"}
    assert not cluster.controller.get_profile("qwen").get_revision(rev.revision_id).known_good
    assert catalog(cluster.controller)["aliases"][0]["latest_check"]["dry_run"] is True
    assert cluster.gateway.routes["default"].status == "serving"


async def test_live_in_process_protocol_roundtrips(cluster, monkeypatch):
    rev = serving(cluster, tools=True)
    runtime(monkeypatch, cluster)
    received = []

    def handler(request):
        body = json.loads(request.content)
        received.append(body)
        assert body["model"] == "qwen"
        assert request.headers["authorization"] == "Bearer backend-key"
        assert "client-key" not in request.headers.values()
        if request.url.path == "/v1/responses":
            return httpx.Response(200, json={"output": [{"type": "message", "content": [
                {"type": "output_text", "text": "ready"}]}]})
        if body.get("stream"):
            data = {"choices": [{"delta": {"content": "ready"}}]}
            return httpx.Response(200, text=f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        if body.get("tool_choice") == "auto":
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "check-1", "type": "function", "function": {
                    "name": "twinspark_echo", "arguments": '{"value":"twinspark"}'}}]}}]})
        if body.get("tool_choice") == "none":
            assert body["messages"][-1] == {"role": "tool", "tool_call_id": "check-1", "content": "twinspark"}
            return answer("twinspark")
        return answer('{"ok":true}' if "response_format" in body else "ready")

    await install_backend(cluster, handler)
    result = await cluster.wait_job((await check_alias(cluster.controller, "default", compatibility.CHECKS)).job_id)
    assert result.payload["compatible"] is True
    assert result.payload["revision_id"] == rev.revision_id
    assert [c["status"] for c in result.payload["checks"]] == ["pass"] * 5
    assert len(received) == 6
    assert "client-key" not in result.model_dump_json() and "backend-key" not in result.model_dump_json()
    assert not cluster.controller.get_profile("qwen").latest().known_good


@pytest.mark.parametrize("response,status", [
    (httpx.Response(400, json={"error": "secret diagnostic"}), "unsupported"),
    (httpx.Response(503, text="secret diagnostic"), "fail"),
    (httpx.Response(200, json={"choices": []}), "fail"),
    (httpx.Response(200, json=["bad"]), "fail"),
])
async def test_protocol_failure_recorded_without_sensitive_body(cluster, monkeypatch, response, status):
    serving(cluster)
    runtime(monkeypatch, cluster)
    await install_backend(cluster, lambda request: response)
    result = await cluster.wait_job((await check_alias(cluster.controller, "default", ["chat"])).job_id)
    assert result.state.value == "completed"
    assert result.payload["checks"][0]["status"] == status
    assert result.payload["compatible"] is False
    assert "secret diagnostic" not in result.model_dump_json()


async def test_tools_require_recipe_support_without_sending_a_request(cluster, monkeypatch):
    serving(cluster)
    runtime(monkeypatch, cluster)
    await install_backend(cluster, lambda req: pytest.fail("no tool parser configured"))
    result = await cluster.wait_job((await check_alias(cluster.controller, "default", ["tools"])).job_id)
    assert result.payload["checks"][0]["status"] == "unsupported"


async def test_checks_require_serving_revision_and_cluster_lock(cluster):
    cluster.controller.create_profile(draft())
    with pytest.raises(ValueError, match="activate"):
        await check_alias(cluster.controller, "default")
    await cluster.controller._lock.acquire()
    try:
        with pytest.raises(BusyError):
            await check_alias(cluster.controller, "default")
    finally:
        cluster.controller._lock.release()


async def test_unknown_runtime_does_not_send_inference(cluster, monkeypatch):
    serving(cluster)
    runtime(monkeypatch, cluster, "unknown")
    await install_backend(cluster, lambda req: pytest.fail("unknown runtime sent inference"))
    result = await cluster.wait_job((await check_alias(cluster.controller, "default", ["chat"])).job_id)
    assert result.state.value == "failed"
    assert result.payload["compatible"] is None


class StalledStream(httpx.AsyncByteStream):
    def __init__(self):
        self.entered = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.entered.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self):
        self.closed = True


async def test_cancellation_releases_job_and_gateway_counters(cluster, monkeypatch):
    serving(cluster)
    runtime(monkeypatch, cluster)
    stream = StalledStream()
    await install_backend(cluster, lambda req: httpx.Response(200, stream=stream,
                                 headers={"content-type": "text/event-stream"}))
    job = await check_alias(cluster.controller, "default", ["streaming"])
    await asyncio.wait_for(stream.entered.wait(), 2)
    cluster.controller.cancel_job(job.job_id)
    result = await cluster.wait_job(job.job_id)
    assert result.payload["cancelled"] is True
    assert stream.closed
    assert cluster.gateway.routes["default"].inflight == 0
    assert cluster.controller.current_job is None


async def test_timeout_releases_job_and_gateway_counters(cluster, monkeypatch):
    serving(cluster)
    runtime(monkeypatch, cluster)
    monkeypatch.setattr(compatibility, "CHECK_TIMEOUT", 0.1)
    stream = StalledStream()
    await install_backend(cluster, lambda req: httpx.Response(200, stream=stream))
    result = await cluster.wait_job((await check_alias(cluster.controller, "default", ["chat"])).job_id)
    assert result.payload["checks"][0]["status"] == "fail"
    assert stream.closed
    assert cluster.gateway.routes["default"].inflight == 0


async def test_actual_probe_byte_limit(cluster, monkeypatch):
    serving(cluster)
    runtime(monkeypatch, cluster)
    await install_backend(cluster, lambda req: answer("x" * (compatibility.MAX_RESPONSE_BYTES + 1)))
    result = await cluster.wait_job((await check_alias(cluster.controller, "default", ["chat"])).job_id)
    assert result.payload["checks"][0]["status"] == "fail"
    assert cluster.gateway.routes["default"].inflight == 0


@pytest.mark.parametrize("content", ['{"ok":1}', '{"ok":"true"}', '{"ok":true,"extra":0}', '[]'])
async def test_structured_requires_actual_boolean_schema(cluster, monkeypatch, content):
    serving(cluster)
    runtime(monkeypatch, cluster)
    await install_backend(cluster, lambda req: answer(content))
    result = await cluster.wait_job((await check_alias(cluster.controller, "default", ["structured"])).job_id)
    assert result.payload["checks"][0]["status"] == "fail"


async def test_broken_sse_cannot_pass(cluster, monkeypatch):
    serving(cluster)
    runtime(monkeypatch, cluster)
    await install_backend(cluster, lambda req: httpx.Response(200,
        text='data: {"choices":[{"delta":{"content":"ready"}}]}\n\n',
        headers={"content-type": "text/event-stream"}))
    result = await cluster.wait_job((await check_alias(cluster.controller, "default", ["streaming"])).job_id)
    assert result.payload["checks"][0]["status"] == "fail"


async def test_compressed_probe_response_is_rejected_before_decoding(cluster, monkeypatch):
    serving(cluster)
    runtime(monkeypatch, cluster)

    def handler(req):
        assert req.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=httpx.ByteStream(b"not-a-gzip-stream"),
                              headers={"content-encoding": "gzip"})

    await install_backend(cluster, handler)
    result = await cluster.wait_job((await check_alias(cluster.controller, "default", ["chat"])).job_id)
    assert result.payload["checks"][0]["status"] == "fail"
    assert cluster.gateway.routes["default"].inflight == 0
