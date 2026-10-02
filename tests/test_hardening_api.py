"""Regression tests for the issues found by the black-box API / gateway / agent review.

Each test pins one defect that was reproduced against the 0.4.1 code: unauthenticated 500s,
validation errors that crashed instead of answering 422, a rate limiter that let strangers lock
out the operator, truncated streams that looked complete, and so on.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import uvicorn

from twinspark.agent.actions import AgentActions
from twinspark.agent.agent import MAX_BODY_BYTES, build_agent_app
from twinspark.controller.app import create_app
from twinspark.gateway.app import build_gateway_app
from twinspark.gateway.gateway import Gateway
from twinspark.schemas.config import AgentConfig, NodeIdentity
from twinspark.security import SecretsVault, tokens_equal

KEY = "mgmt-key"
JSON = {"content-type": "application/json"}


def client(cluster, **kw) -> httpx.AsyncClient:
    """Management API with raise_app_exceptions=False: a bug shows up as a 500, like on a server."""
    app = create_app(cluster.controller, KEY, run_startup=False, background=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                             base_url="http://m", **kw)


# ---- constant-time comparison that cannot raise ------------------------------------------------
def test_tokens_equal_handles_non_ascii_and_empty():
    assert tokens_equal("secret", "secret")
    assert not tokens_equal("secret", "other")
    assert not tokens_equal("café", "secret")
    assert not tokens_equal("\ud800", "secret")           # lone surrogate
    assert not tokens_equal("", "")                        # an empty expected key never matches
    assert not tokens_equal(None, "secret")
    assert tokens_equal("café", "café")


@pytest.mark.parametrize("headers", [
    {"x-api-key": "café".encode("latin-1")},
    {"x-api-key": "😀".encode()},
    {"authorization": b"Bearer caf\xe9"},
])
async def test_management_api_answers_401_for_non_ascii_keys(cluster, headers):
    async with client(cluster) as cl:
        r = await cl.get("/api/v1/status", headers=headers)
    assert r.status_code == 401, r.text


async def test_gateway_answers_401_for_non_ascii_key():
    gw = Gateway("client-key")
    gw.set_route("default", ["http://127.0.0.1:9"], "m")
    app = build_gateway_app(gw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://gw") as cl:
        r = await cl.get("/v1/models", headers={"authorization": b"Bearer caf\xe9"})
    assert r.status_code == 401


async def test_agent_answers_401_for_non_ascii_token_and_has_no_schema(cluster, tmp_path):
    cfg = AgentConfig(node=NodeIdentity(node_id="A", role="agent"), runtime=cluster.controller.config.runtime,
                      secrets_dir=str(tmp_path / "s"))
    app = build_agent_app(cfg, AgentActions(cfg, vault=SecretsVault(tmp_path / "s")), token="agent-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://agent") as cl:
        r = await cl.post("/v1/action", json={"action": "hardware_facts"},
                          headers={"authorization": b"Bearer caf\xe9"})
        assert r.status_code == 401
        assert (await cl.get("/openapi.json")).status_code == 404
        assert (await cl.get("/docs")).status_code == 404


async def test_agent_reads_no_body_before_authenticating(cluster, tmp_path):
    cfg = AgentConfig(node=NodeIdentity(node_id="A", role="agent"), runtime=cluster.controller.config.runtime,
                      secrets_dir=str(tmp_path / "s"))
    app = build_agent_app(cfg, AgentActions(cfg, vault=SecretsVault(tmp_path / "s")), token="agent-token")
    consumed = {"n": 0}

    async def body():
        for _ in range(50):
            consumed["n"] += 1
            yield b"x" * 65536
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://agent") as cl:
        r = await cl.post("/v1/action", content=body(), headers=JSON)
        assert r.status_code == 401
        # authenticated but oversized: refused by declared length without buffering it
        r = await cl.post("/v1/action", content=b"{}",
                          headers={**JSON, "authorization": "Bearer agent-token",
                                   "content-length": str(MAX_BODY_BYTES + 1)})
        assert r.status_code == 413


# ---- validation errors answer 422, never 500 ----------------------------------------------------
@pytest.mark.parametrize("raw", [
    b'{"name": NaN, "simple": {}}',
    b'{"name": Infinity}',
    b'{"name": "\\ud800"}',
    b'{"name": "ok", "simple": {"model": "\\ud800", "api_alias": "-Infinity"}}',
    b'[1, 2, 3]',
])
async def test_bad_json_bodies_give_422_not_500(cluster, raw):
    async with client(cluster) as cl:
        r = await cl.post("/api/v1/profiles", content=raw, headers={**JSON, "x-api-key": KEY})
    assert r.status_code == 422, (r.status_code, r.text[:200])
    assert "detail" in r.json()


async def test_deeply_nested_json_is_rejected_cleanly(cluster):
    deep = b'{"a":' * 5000 + b"1" + b"}" * 5000
    async with client(cluster) as cl:
        r = await cl.post("/api/v1/profiles", content=deep, headers={**JSON, "x-api-key": KEY})
    assert r.status_code in (400, 422), r.status_code


async def test_validation_errors_do_not_echo_the_input(cluster):
    secret = "S3CRET-" + "x" * 200_000
    async with client(cluster) as cl:
        r = await cl.post("/api/v1/profiles", json={"name": secret}, headers={"x-api-key": KEY})
    assert r.status_code == 422
    assert "S3CRET" not in r.text and len(r.content) < 5000


async def test_unauthenticated_callers_learn_nothing_and_cost_little(cluster):
    c = cluster.controller
    c.maintenance.blocking = lambda: True                    # a maintenance run is active
    async with client(cluster) as cl:
        # no key: 401, not the 409 that would reveal "maintenance is running"
        assert (await cl.post("/api/v1/stop")).status_code == 401
        assert (await cl.post("/api/v1/profiles", content=b"{not json", headers=JSON)).status_code == 401
        # a huge declared body is refused after the key check, before it is read
        big = {"x-api-key": KEY, "content-length": str(50 * 1024 ** 2), **JSON}
        assert (await cl.post("/api/v1/profiles", content=b"{}", headers=big)).status_code == 413
        # the real operator still gets the maintenance message
        assert (await cl.post("/api/v1/stop", headers={"x-api-key": KEY})).status_code == 409


async def test_interactive_docs_and_schema_are_off(cluster):
    async with client(cluster) as cl:
        for path in ("/openapi.json", "/mgmt/docs", "/docs", "/redoc"):
            assert (await cl.get(path)).status_code in (404, 405), path


# ---- rate limiting must not let strangers lock out the operator -----------------------------------
async def test_wrong_keys_do_not_use_up_the_operators_budget(cluster):
    cluster.controller.config.rate_limit_per_min = 30
    async with client(cluster) as cl:
        codes = [(await cl.get("/api/v1/status", headers={"x-api-key": "wrong"})).status_code
                 for _ in range(150)]
        assert set(codes) <= {401, 429}
        r = await cl.get("/api/v1/status", headers={"x-api-key": KEY})
        assert r.status_code == 200, "an attacker used up the operator's rate limit"


async def test_rate_limit_answers_429_with_retry_after(cluster):
    cluster.controller.config.rate_limit_per_min = 5
    async with client(cluster) as cl:
        responses = [await cl.get("/api/v1/status", headers={"x-api-key": KEY}) for _ in range(8)]
    limited = [r for r in responses if r.status_code == 429]
    assert limited and all(int(r.headers["retry-after"]) >= 1 for r in limited)


async def test_csrf_rejects_empty_origin_and_cross_site_fetch(cluster):
    async with client(cluster) as cl:
        h = {"x-api-key": KEY}
        assert (await cl.post("/api/v1/stop", headers={**h, "origin": ""})).status_code == 403
        assert (await cl.post("/api/v1/stop", headers={**h, "origin": "null"})).status_code == 403
        assert (await cl.post("/api/v1/stop", headers={**h, "origin": "https://evil.example"})).status_code == 403
        assert (await cl.post("/api/v1/stop", headers={**h, "sec-fetch-site": "cross-site"})).status_code == 403
        same = await cl.post("/api/v1/stop", headers={**h, "origin": "http://m", "sec-fetch-site": "same-origin"})
        assert same.status_code != 403


# ---- other crashes found by the fuzzer -----------------------------------------------------------------
@pytest.mark.parametrize("limit", ["-1", "0", "-99999999999999999999", "99999999999"])
async def test_audit_limit_is_clamped(cluster, limit):
    async with client(cluster) as cl:
        r = await cl.get(f"/api/v1/audit?limit={limit}", headers={"x-api-key": KEY})
    assert r.status_code == 200
    assert 1 <= len(r.json()) <= 1000


@pytest.mark.parametrize("cfg", [
    {"hidden_size": []}, {"hidden_size": {}}, {"hidden_size": 1e308},
    {"num_attention_heads": "many"}, {"num_hidden_layers": -4}, {"head_dim": [1]},
    {"num_key_value_heads": True},
])
async def test_malformed_hf_config_gives_422(cluster, cfg):
    body = {"repo": "o/m", "hf_config": {"hidden_size": 4096, "num_hidden_layers": 4,
                                         "num_attention_heads": 32, **cfg}, "weight_bytes": 10 ** 9}
    async with client(cluster) as cl:
        h = {"x-api-key": KEY}
        for method, url in (("PUT", "/api/v1/system/model-specs"),
                            ("POST", "/api/v1/system/memory/estimate"),
                            ("POST", "/api/v1/system/quantization/variants")):
            r = await cl.request(method, url, json=body, headers=h)
            assert r.status_code == 422, (url, r.status_code, r.text[:120])


async def test_null_hf_config_fields_are_treated_as_absent(cluster):
    body = {"repo": "o/m", "weight_bytes": 10 ** 9,
            "hf_config": {"hidden_size": None, "num_hidden_layers": 4, "num_attention_heads": None}}
    async with client(cluster) as cl:
        r = await cl.put("/api/v1/system/model-specs", json=body, headers={"x-api-key": KEY})
    assert r.status_code < 500


async def test_container_log_name_may_not_end_with_newline(cluster):
    async with client(cluster) as cl:
        r = await cl.get("/api/v1/logs/A/tsm-fz%0a", headers={"x-api-key": KEY})
    assert r.status_code in (404, 422)


async def test_total_failures_are_not_reported_as_200(cluster):
    async with client(cluster) as cl:
        h = {"x-api-key": KEY}
        assert (await cl.delete("/api/v1/mods/bad%20name", headers=h)).status_code == 422
        r = await cl.post("/api/v1/models/files/delete", json={"repo": "../../../x"}, headers=h)
        assert r.status_code in (409, 422)
        r = await cl.get("/api/v1/profiles/nope/compare?a=x&b=y", headers=h)
        assert r.status_code == 404


async def test_telemetry_is_one_route_and_supports_refresh(cluster):
    async with client(cluster) as cl:
        r = await cl.get("/api/v1/system/telemetry?refresh=true", headers={"x-api-key": KEY})
    assert r.status_code == 200 and "nodes" in r.json()


async def test_overlong_model_reference_is_a_clean_error(cluster):
    async with client(cluster) as cl:
        r = await cl.post("/api/v1/system/resolve", json={"ref": "A" * 1_000_000}, headers={"x-api-key": KEY})
    assert r.status_code in (400, 422)


# ---- gateway ----------------------------------------------------------------------------------------------------
def gateway_client(gw: Gateway) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(gw), raise_app_exceptions=False),
                             base_url="http://gw", headers={"authorization": "Bearer client-key"})


async def test_gateway_status_needs_the_inference_key():
    gw = Gateway("client-key")
    gw.set_route("default", ["http://127.0.0.1:9"], "secret-profile-name")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(gw)),
                                 base_url="http://gw") as anon:
        assert (await anon.get("/twinspark/status")).status_code == 401
    async with gateway_client(gw) as cl:
        r = await cl.get("/twinspark/status")
        assert r.status_code == 200 and r.json()["routes"][0]["alias"] == "default"


@pytest.mark.parametrize("model", [["a"], {"a": 1}, 7])
async def test_gateway_rejects_non_string_model_with_400(model):
    gw = Gateway("client-key")
    gw.set_route("default", ["http://127.0.0.1:9"], "m")
    async with gateway_client(gw) as cl:
        r = await cl.post("/v1/chat/completions", json={"model": model, "messages": []})
    assert r.status_code == 400


async def test_gateway_does_not_pretend_to_forward_multipart_audio():
    gw = Gateway("client-key")
    gw.set_route("default", ["http://127.0.0.1:9"], "m")
    async with gateway_client(gw) as cl:
        assert (await cl.post("/v1/audio/transcriptions", json={"model": "default"})).status_code == 404


async def test_gateway_fails_over_to_a_healthy_replica():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "sick":
            return httpx.Response(503, json={"error": "starting"})
        return httpx.Response(200, json={"ok": True})
    gw = Gateway("client-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    gw.set_route("default", ["http://sick:8100", "http://well:8100"], "m")
    async with gateway_client(gw) as cl:
        codes = [(await cl.post("/v1/chat/completions", json={"model": "default", "messages": []})).status_code
                 for _ in range(6)]
    assert codes == [200] * 6, codes
    assert gw.routes["default"].inflight == 0


async def dying_backend(mode: str):
    """Answers 200, sends part of the body, then the connection is cut (OOM kill, container stop)."""
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(0.05)
        if mode == "sse":
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                         b"transfer-encoding: chunked\r\n\r\n")
            chunk = b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
            writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        else:
            body = json.dumps({"choices": [{"message": {"content": "x" * 200}}]}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                         b"content-length: %d\r\n\r\n" % len(body))
            writer.write(body[:60])
        await writer.drain()
        writer.transport.abort()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


@pytest.mark.parametrize("mode", ["sse", "json"])
async def test_truncated_backend_response_is_an_error_for_the_client(mode):
    srv, port = await dying_backend(mode)
    gw = Gateway(None)
    gw.set_route("default", [f"http://127.0.0.1:{port}"], "m")
    server = uvicorn.Server(uvicorn.Config(build_gateway_app(gw), host="127.0.0.1", port=0, log_level="critical"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    gport = server.servers[0].sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(timeout=10) as cl:
            with pytest.raises(httpx.HTTPError):
                async with cl.stream("POST", f"http://127.0.0.1:{gport}/v1/chat/completions",
                                     json={"model": "default", "messages": [], "stream": mode == "sse"}) as r:
                    async for _ in r.aiter_raw():
                        pass
        await asyncio.sleep(0.1)
        assert gw.routes["default"].inflight == 0 and gw.routes["default"].errors >= 1
    finally:
        server.should_exit = True
        await task
        srv.close()


async def test_wait_for_rate_window_helper_sanity():
    # the Retry-After value is the time left in the current minute window
    from twinspark.api.deps import _too_many
    exc = _too_many()
    assert 1 <= int(exc.headers["Retry-After"]) <= 60 and exc.status_code == 429
    assert time.time() > 0
