"""Gateway routing (master plan §3.3): least in-flight with per-node weights, session affinity, and
fallbacks while an alias switches. Backends are httpx mock transports; nothing leaves the process."""

from __future__ import annotations

import asyncio
import collections
import json

import httpx
import pytest

from twinspark.gateway.app import build_gateway_app
from twinspark.gateway.gateway import Gateway

from .test_gateway_agents import response

A, B = "http://10.0.0.1:18100", "http://10.0.0.2:18100"


def make(handler, **settings):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gw = Gateway("client-secret", "backend-secret", client=upstream)
    gw.configure({"affinity": True, "balance": "least-inflight", **settings}, fallback_api_key="ext-secret")
    gw.set_route("default", [A, B], "rep", "rev-1", nodes={A: "A", B: "B"})
    gw.set_route("coder", ["http://10.0.0.2:18200"], "coder-model", "rev-1")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(gw)), base_url="http://gw",
                               headers={"authorization": "Bearer client-secret"})
    return gw, client


def chat(model="default", **extra):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


async def test_a_new_request_goes_to_the_replica_with_fewer_requests_in_flight():
    hold = asyncio.Event()
    seen = []

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await hold.wait()
            yield b"data: [DONE]\n\n"

    async def backend(request):
        seen.append(str(request.url).rsplit("/v1", 1)[0])
        if len(seen) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Slow())
        return response(body={"ok": True})

    gw, client = make(backend)
    async with client:
        slow = asyncio.create_task(client.post("/v1/chat/completions", json=chat(stream=True)))
        while gw.routes["default"].inflight < 1:
            await asyncio.sleep(0.01)
        first = seen[0]
        for _ in range(3):
            assert (await client.post("/v1/chat/completions", json=chat())).status_code == 200
        assert seen[1:] == [B if first == A else A] * 3          # the busy replica gets none of them
        assert gw.routes["default"].to_payload()["backend_inflight"] == {"A" if first == A else "B": 1}
        hold.set()
        await slow
        assert gw.routes["default"].to_payload()["backend_inflight"] == {}
    await gw.aclose()


@pytest.mark.parametrize("weights,expected", [({}, {A: 4, B: 4}), ({"A": 3, "B": 1}, {A: 6, B: 2}),
                                              ({"A": 1, "B": 0}, {A: 8})])
async def test_idle_requests_are_shared_by_weight(weights, expected):
    seen = collections.Counter()

    async def backend(request):
        seen[str(request.url).rsplit("/v1", 1)[0]] += 1
        return response(body={})

    gw, client = make(backend, weights=weights)
    async with client:
        for _ in range(8):
            await client.post("/v1/chat/completions", json=chat())
    assert dict(seen) == expected
    await gw.aclose()


async def test_a_session_stays_on_one_replica():
    seen: dict[str, set] = collections.defaultdict(set)

    async def backend(request):
        body = json.loads(request.content)                       # the gateway forwards the body as sent
        seen[body["probe"]].add(str(request.url).rsplit("/v1", 1)[0])
        assert body["model"] == "rep"
        return response(body={})

    gw, client = make(backend)
    async with client:
        for i in range(40):
            await client.post("/v1/chat/completions", json=chat(probe=f"s{i % 8}"),
                              headers={"session-id": f"s{i % 8}"})
        for _ in range(6):
            await client.post("/v1/chat/completions", json=chat(probe="cache", prompt_cache_key="agent-7"))
        for _ in range(6):
            await client.post("/v1/chat/completions", json=chat(probe="none"))
    assert all(len(seen[f"s{i}"]) == 1 for i in range(8)) and len(seen["cache"]) == 1
    assert {next(iter(seen[f"s{i}"])) for i in range(8)} == {A, B}     # sessions are spread over both replicas
    assert seen["none"] == {A, B}                                         # no session: least in flight
    st = gw.routes["default"]
    home = gw.order(st, "s0")[0]
    st.backend_failed(home)                                     # that replica just failed: the session moves
    assert gw.order(st, "s0")[0] != home
    st._down_until.clear()
    gw.configure({"affinity": False})                           # affinity off: the session key is ignored
    assert {gw.order(st, "s1")[0] for _ in range(4)} == {A, B}
    await gw.aclose()


async def test_while_an_alias_switches_its_fallback_alias_answers():
    seen = []

    async def backend(request):
        seen.append((str(request.url), json.loads(request.content)["model"], request.headers.get("authorization")))
        return response(body={"ok": True})

    gw, client = make(backend, fallbacks={"default": {"alias": "coder"}})
    async with client:
        gw.begin_drain("default")
        r = await client.post("/v1/chat/completions", json=chat())
        assert r.status_code == 200 and r.headers["x-twinspark-fallback"] == "alias:coder"
        assert seen[-1] == ("http://10.0.0.2:18200/v1/chat/completions", "coder-model", "Bearer backend-secret")
        assert gw.routes["default"].inflight == 0               # the drain of default does not wait for these
        gw.begin_drain("coder")                                  # the fallback is switching too: plain 503
        r = await client.post("/v1/chat/completions", json=chat())
        assert r.status_code == 503 and "x-twinspark-fallback" not in r.headers
        gw.cancel_drain("default")
        r = await client.post("/v1/chat/completions", json=chat())
        assert r.status_code == 200 and "x-twinspark-fallback" not in r.headers
    await gw.aclose()


async def test_an_external_fallback_gets_its_own_key_never_the_backend_key():
    seen = []

    async def backend(request):
        seen.append((str(request.url), json.loads(request.content)["model"], request.headers.get("authorization")))
        return response(body={"ok": True})

    gw, client = make(backend, fallbacks={"default": {"url": "https://llm.example.org/api/v1", "model": "small"}})
    async with client:
        gw.mark_down("default", "container exited")
        r = await client.post("/v1/chat/completions", json=chat())
        assert r.status_code == 200 and r.headers["x-twinspark-fallback"] == "external"
        assert seen == [("https://llm.example.org/api/v1/chat/completions", "small", "Bearer ext-secret")]
        gw.configure(gw.settings, fallback_api_key="")
        await client.post("/v1/chat/completions", json=chat())
        assert seen[-1][2] is None                               # no key configured: none is sent
    await gw.aclose()


async def test_settings_api_validates_and_applies(cluster):
    from twinspark.controller.app import create_app
    c = cluster.controller
    app = create_app(c, management_key="k", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "k"}) as api:
        v = (await api.get("/api/v1/gateway")).json()
        assert v["source"] == "controller.yaml" and v["settings"]["balance"] == "least-inflight"
        bad = [{"fallbacks": {"default": {"alias": "default"}}},
               {"fallbacks": {"default": {"url": "https://user:pw@x.example/v1", "model": "m"}}},
               {"fallbacks": {"default": {"url": "https://x.example/v1"}}},
               {"fallbacks": {"default": {"alias": "a", "url": "https://x.example/v1", "model": "m"}}},
               {"weights": {"A": -1}}, {"weights": {"C": 1}}, {"balance": "random"}]
        for body in bad:
            assert (await api.put("/api/v1/gateway", json=body)).status_code == 422, body
        r = await api.put("/api/v1/gateway", json={"weights": {"A": 2}, "fallbacks": {
            "default": {"url": "https://llm.example.org/v1", "model": "small"}}})
        assert r.status_code == 200 and r.json()["source"] == "runtime" and not r.json()["fallback_key_set"]
        assert c.gateway.settings["weights"] == {"A": 2.0}
        assert (await api.put("/api/v1/gateway/fallback-key", json={"key": "ext-1"})).json()["fallback_key_set"]
        assert c.gateway.fallback_api_key == "ext-1"
        assert (await api.put("/api/v1/gateway/fallback-key", json={"key": "has space"})).status_code == 422
        r = await api.delete("/api/v1/gateway")
        assert r.json()["source"] == "controller.yaml" and c.gateway.settings["weights"] == {}
    assert c.gateway.fallback_api_key == "ext-1"                 # the key stays in the vault until removed
    assert "ext-1" not in json.dumps(c.store.kv_get("gateway_settings") or {})


async def test_a_replicated_deployment_tells_the_gateway_which_node_each_backend_is(cluster):
    from twinspark.schemas.enums import Topology

    from .conftest import draft
    c = cluster.controller
    c.create_profile(draft("rep", Topology.REPLICATED))
    job = await cluster.wait_job((await c.activate("rep")).job_id)
    assert job.state.value == "completed"
    st = c.gateway.routes["default"]
    assert sorted(st.backend_nodes.values()) == ["A", "B"] and set(st.backend_nodes) == set(st.backends)


def test_the_cli_reads_fallback_targets():
    from twinspark.cli_gateway import parse_fallback
    assert parse_fallback("default=alias:secondary") == ("default", {"alias": "secondary"})
    assert parse_fallback("default=url:https://x.example/v1,model:small") == (
        "default", {"url": "https://x.example/v1", "model": "small"})
    assert parse_fallback("default=none") == ("default", None)
    with pytest.raises(SystemExit):
        parse_fallback("default")
