"""Named inference keys and per-key usage (master plan §4.1). Backends are httpx mock transports."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from twinspark.controller.keys import KeyStore, Usage, digest
from twinspark.gateway.app import build_gateway_app
from twinspark.gateway.gateway import Gateway

from .test_gateway_agents import Chunks, response


def gateway_with(handler, keys):
    gw = Gateway("legacy-key", "backend-secret", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    gw.set_route("default", ["http://a"], "m-default", "rev-1")
    gw.set_route("coder", ["http://b"], "m-coder", "rev-1")
    gw.set_keys(keys)
    app = build_gateway_app(gw)
    return gw, lambda token: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw",
                                               headers={"authorization": f"Bearer {token}"} if token else {})


def rec(token, **kw):
    return {"id": kw.pop("id", "key-" + token[-3:]), "sha256": digest(token), "aliases": [], **kw}


async def test_each_key_works_alone_and_only_for_its_aliases():
    async def backend(request):
        return response(body={"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    gw, client = gateway_with(backend, [rec("tsk_all"), rec("tsk_cod", aliases=["coder"]),
                                        rec("tsk_old", revoked_at=time.time()),
                                        rec("tsk_exp", expires_at=time.time() - 1)])
    body = {"model": "default", "messages": []}
    async with client("tsk_all") as c:
        assert (await c.post("/v1/chat/completions", json=body)).status_code == 200
    async with client("tsk_cod") as c:
        r = await c.post("/v1/chat/completions", json=body)
        assert r.status_code == 403 and "may not use 'default'" in r.json()["error"]["message"]
        assert (await c.post("/v1/chat/completions", json={**body, "model": "coder"})).status_code == 200
        assert [m["id"] for m in (await c.get("/v1/models")).json()["data"]] == ["coder"]
        assert (await c.get("/v1/models/default")).status_code == 404
    for token in ("tsk_old", "tsk_exp", "legacy-key", "nope", None):
        async with client(token) as c:
            assert (await c.post("/v1/chat/completions", json=body)).status_code == 401, token
    async with client(gw.internal_token) as c:              # TwinSpark's own checks
        assert (await c.post("/v1/chat/completions", json=body)).status_code == 200
    u = gw.take_usage()
    totals = {(r["key_id"], r["alias"]): (r["requests"], r["prompt_tokens"], r["completion_tokens"]) for r in u["rows"]}
    assert totals == {("key-all", "default"): (1, 7, 3), ("key-cod", "coder"): (1, 7, 3)}   # internal is not counted
    assert set(u["last_used"]) == {"key-all", "key-cod"} and gw.take_usage()["rows"] == []
    await gw.aclose()


async def test_without_named_keys_the_old_single_key_still_works():
    gw, client = gateway_with(lambda r: response(body={}), [])
    async with client("legacy-key") as c:
        assert (await c.post("/v1/chat/completions", json={"model": "default"})).status_code == 200
    assert gw.take_usage()["rows"][0]["key_id"] == "default"
    await gw.aclose()


@pytest.mark.parametrize("include_usage", [True, False])
async def test_streamed_tokens_are_counted_from_usage_or_estimated_from_events(include_usage):
    events = [b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'] * 5
    if include_usage:
        events.append(b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":5}}\n\n')
    events.append(b"data: [DONE]\n\n")

    async def backend(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(events))
    gw, client = gateway_with(backend, [rec("tsk_all")])
    async with client("tsk_all") as c:
        r = await c.post("/v1/chat/completions", json={"model": "default", "stream": True})
        assert r.status_code == 200 and r.content == b"".join(events)
    (row,) = gw.take_usage()["rows"]
    expected = (11, 5, 0) if include_usage else (0, 5, 1)
    assert (row["prompt_tokens"], row["completion_tokens"], row["estimated_requests"]) == expected
    await gw.aclose()


def test_the_store_keeps_hashes_and_daily_totals(tmp_path):
    from twinspark.controller.store import Store
    store = Store(str(tmp_path / "s.db"))
    ks = KeyStore(store)
    assert ks.migrate("legacy-key-123") and not ks.migrate("legacy-key-123")
    token, r = ks.create("laptop (OpenClaw)", aliases=["coder"], expires_in_days=30, app="openclaw")
    assert token.startswith("tsk_") and r["prefix"] == token[:10] and r["expires_at"] > time.time() + 29 * 86400
    raw = json.dumps(store.kv_get("inference_keys"))
    assert token not in raw and "legacy-key-123" not in raw and digest(token) in raw
    for bad in ({"name": "x\ny"}, {"name": "ok", "aliases": ["bad alias"]}, {"name": "ok", "expires_in_days": 0}):
        with pytest.raises(ValueError):
            ks.create(**bad)
    with pytest.raises(ValueError, match="revoke"):
        ks.delete(r["id"])
    ks.revoke(r["id"])
    ks.delete(r["id"])
    assert [x["id"] for x in ks.records()] == ["default"]
    u = Usage(store)
    u.add([{"day": "2026-10-18", "key_id": "default", "alias": "default", "requests": 2, "prompt_tokens": 10,
            "completion_tokens": 4, "estimated_requests": 0}] * 2)
    import datetime as dt
    rows = u.rows(days=3, today=dt.date(2026, 10, 19))
    assert rows == [{"day": "2026-10-18", "key_id": "default", "alias": "default", "requests": 4, "prompt_tokens": 20,
                     "completion_tokens": 8, "estimated_requests": 0}]
    store.kv_set("usage:2026-01-01", {"x|y": {}})
    u.prune(today=dt.date(2026, 10, 19))
    assert store.kv_keys("usage:") == ["usage:2026-10-18"]


async def test_keys_api_end_to_end(cluster):
    from twinspark.controller.app import create_app
    c = cluster.controller
    assert [k["id"] for k in c.keys.records()] == ["default"]           # the cluster's key, migrated
    app = create_app(c, management_key="k", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "k"}) as api:
        listed = (await api.get("/api/v1/keys")).json()
        assert listed[0]["migrated"] and listed[0]["state"] == "active" and "sha256" not in listed[0]
        r = await api.post("/api/v1/keys", json={"name": "hermes", "aliases": ["default"], "app": "hermes"})
        assert r.status_code == 201
        token, key_id = r.json()["key"], r.json()["id"]
        assert token not in json.dumps((await api.get("/api/v1/keys")).json())
        assert c.gateway.authenticate(f"Bearer {token}")["id"] == key_id
        assert (await api.post("/api/v1/keys", json={"name": "x", "expires_in_days": 0})).status_code == 422
        assert (await api.delete(f"/api/v1/keys/{key_id}")).status_code == 409
        assert (await api.post(f"/api/v1/keys/{key_id}/revoke")).json()["state"] == "revoked"
        assert c.gateway.authenticate(f"Bearer {token}") is None
        assert c.gateway.authenticate("Bearer client-key")["id"] == "default"    # the other key is untouched
        assert (await api.delete(f"/api/v1/keys/{key_id}")).status_code == 200
        assert (await api.post("/api/v1/keys/nope/revoke")).status_code == 404
        c.gateway.record_usage("default", "default", 100, 20, False)
        u = (await api.get("/api/v1/usage", params={"days": 7})).json()
        assert u["totals"]["default"] == {"requests": 1, "prompt_tokens": 100, "completion_tokens": 20}
        csv_text = (await api.get("/api/v1/usage", params={"days": 7, "format": "csv"})).text
        assert csv_text.splitlines()[0].startswith("day,key_id,key_name,alias")
        assert ",default,default,default,1,100,20,0" in csv_text
    assert c.keys.records()[0]["last_used_at"]                             # flushed with the usage


def test_tsm_keys_and_usage(monkeypatch, capsys):
    from twinspark import cli
    calls = []

    class FakeApi:
        as_json = False

        def __call__(self, method, path, **kw):
            calls.append((method, path, kw.get("json") or kw.get("params")))
            if path == "/api/v1/keys" and method == "POST":
                return {"key": "tsk_SECRET", "id": "key-1", "name": kw["json"]["name"],
                        "aliases": kw["json"]["aliases"], "expires_at": None}
            if path == "/api/v1/keys":
                return [{"id": "key-1", "name": "laptop", "app": None, "prefix": "tsk_SECRET"[:10], "aliases": [],
                         "state": "active", "last_used_at": None}]
            if path == "/api/v1/usage":
                return {"rows": [{"day": "2026-10-19", "key_id": "key-1", "alias": "default", "requests": 3,
                                  "prompt_tokens": 30, "completion_tokens": 9, "estimated_requests": 1}],
                        "names": {"key-1": "laptop"}}
            return {"id": "key-1", "state": "revoked"}
    monkeypatch.setattr(cli, "Api", lambda *a, **k: FakeApi())
    cli.main(["keys", "create", "laptop", "--alias", "coder", "--expires", "30"])
    out = capsys.readouterr()
    assert out.out.strip() == "tsk_SECRET" and "shown this once" in out.err
    assert calls[-1][2] == {"name": "laptop", "aliases": ["coder"], "expires_in_days": 30, "app": None}
    cli.main(["keys"])
    assert "laptop" in capsys.readouterr().out
    cli.main(["keys", "revoke", "key-1"])
    assert "revoked" in capsys.readouterr().out
    cli.main(["usage", "--days", "7"])
    assert "laptop" in capsys.readouterr().out


async def test_a_revoked_and_deleted_default_key_never_comes_back(cluster, controller_config):
    """The review's case: revoke the migrated key (it leaked), delete it, restart — 0.6's key stays dead and
    the gateway does not fall back to its keyless loopback mode either."""
    from twinspark.controller.controller import Controller
    c = cluster.controller
    assert c.gateway.authenticate("Bearer client-key")["id"] == "default"
    c.revoke_key("default")
    assert c.gateway.authenticate("Bearer client-key") is None
    c.delete_key("default")
    assert c.keys.records() == []
    assert c.gateway.authenticate("Bearer client-key") is None              # straight after the delete
    assert c.gateway.authenticate(None) is None and c.gateway.requires_key()   # and not open
    again = Controller(controller_config, Gateway("client-key", "backend-key"), store=c.store)
    assert again.keys.records() == []                                     # a restart does not re-migrate
    assert again.gateway.authenticate("Bearer client-key") is None and again.gateway.authenticate(None) is None


def test_a_keyless_loopback_gateway_stays_keyed_once_a_key_existed(tmp_path):
    from twinspark.controller.store import Store
    store = Store(str(tmp_path / "s.db"))
    ks, gw = KeyStore(store), Gateway(None)
    assert not ks.migrate(None) and gw.authenticate(None)["id"] == "open"    # never had a key: open (loopback)
    token, rec = ks.create("app")
    ks.revoke(rec["id"])
    ks.delete(rec["id"])
    gw.set_keys(ks.records(), managed=ks.managed())
    assert ks.records() == [] and gw.authenticate(None) is None            # deleting the last key does not open it


def test_a_revocation_is_not_lost_to_a_usage_flush(tmp_path):
    """touch() reads the records, an API thread revokes, touch() writes: the revocation must survive."""
    import threading

    from twinspark.controller.store import Store
    ks = KeyStore(Store(str(tmp_path / "s.db")))
    token, rec = ks.create("app")
    read, go = threading.Event(), threading.Event()
    original = ks.records

    def slow_records():
        out = original()
        if threading.current_thread().name == "flush":
            read.set()
            go.wait(5)
        return out
    ks.records = slow_records
    flush = threading.Thread(name="flush", target=ks.touch, args=({rec["id"]: time.time()},))
    flush.start()
    assert read.wait(5)
    revoker = threading.Thread(target=ks.revoke, args=(rec["id"],))
    revoker.start()
    time.sleep(0.2)                       # the revocation waits for the flush instead of interleaving with it
    go.set()
    flush.join(5)
    revoker.join(5)
    ks.records = original
    (r,) = ks.records()
    assert r["revoked_at"] and r["last_used_at"]


def test_usage_handed_over_from_another_thread_is_never_lost():
    """GET /usage flushes from a worker thread while requests end on the event loop: every request counts
    exactly once."""
    import threading
    gw = Gateway(None)
    n, taken, done = 20000, [], threading.Event()

    def requests():
        for _ in range(n):
            gw.record_usage("key-1", "default", 3, 5, False)
        done.set()

    def flusher():
        while not done.is_set():
            taken.extend(gw.take_usage()["rows"])
        taken.extend(gw.take_usage()["rows"])
    threads = [threading.Thread(target=requests), threading.Thread(target=flusher)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sum(r["requests"] for r in taken) == n
    assert sum(r["completion_tokens"] for r in taken) == 5 * n
