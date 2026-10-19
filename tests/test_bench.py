"""The benchmark runner and its storage (master plan §2.1), against a fake OpenAI-compatible model.

The fake streams exactly ``max_tokens`` chunks and slows down once more than ``knee`` requests are in
flight, so throughput rises and then plateaus like a real server. It runs as a real uvicorn server on
loopback: streaming and concurrency behave as over the network.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from twinspark.client import bench
from twinspark.client.bench import Options, PromptMaker, Run, Target, percentile, stream_request
from twinspark.client.ssh import free_local_port

KEY = "inference-key-for-the-bench"


class FakeModel:
    def __init__(self, knee: int = 8, per_token_s: float = 0.002, ttft_s: float = 0.01, max_model_len: int = 40000,
                 forgetful: bool = False):
        self.knee, self.per_token_s, self.ttft_s = knee, per_token_s, ttft_s
        self.max_model_len, self.forgetful = max_model_len, forgetful
        self.revision = "rev-1"
        self.inflight = 0
        self.bodies: list[dict] = []

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/v1/models", self.models), Route("/v1/chat/completions", self.chat,
                                                                          methods=["POST"])])

    async def models(self, request: Request):
        return JSONResponse({"data": [{"id": "default", "max_model_len": self.max_model_len,
                                       "twinspark": {"revision_id": self.revision}}]})

    def _slow(self) -> float:
        return max(1.0, self.inflight / self.knee)

    async def chat(self, request: Request):
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return JSONResponse({"error": {"message": "bad key"}}, status_code=401)
        body = await request.json()
        prompt = body["messages"][-1]["content"]
        self.bodies.append({k: v for k, v in body.items() if k != "messages"} | {"prompt": prompt})
        words = len(prompt.split())
        n = int(body.get("max_tokens") or 16)
        if "vault" in prompt and "List every vault" in prompt:
            found = re.findall(r"the code for the (\w+) vault is (\d{4})", prompt)
            if self.forgetful:
                found = found[: len(found) // 2]
            text = "\n".join(f"{name}: {code}" for name, code in found)
            pieces = [text]
            n = len(pieces)
        else:
            pieces = ["tok"] * n

        async def gen():
            self.inflight += 1
            try:
                await asyncio.sleep(self.ttft_s * self._slow())
                for piece in pieces:
                    yield "data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}) + "\n\n"
                    await asyncio.sleep(self.per_token_s * self._slow())
                usage = {"prompt_tokens": int(words * 1.25) + 12, "completion_tokens": n}
                yield "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
                yield "data: [DONE]\n\n"
            finally:
                self.inflight -= 1
        return StreamingResponse(gen(), media_type="text/event-stream")


@pytest.fixture
def model():
    """The fake runs on its own event loop in a thread, so the client's load does not slow the server down."""
    fake = FakeModel()
    port = free_local_port(19500)
    server = uvicorn.Server(uvicorn.Config(fake.app(), host="127.0.0.1", port=port, log_level="warning",
                                           access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    fake.url = f"http://127.0.0.1:{port}/v1"
    yield fake
    server.should_exit = True
    thread.join(10)


def target(fake, **kw) -> Target:
    return Target(fake.url, KEY, "default", **kw)


def test_percentiles_interpolate_like_numpy():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([5], 95) == 5
    assert percentile([], 50) is None
    assert round(percentile(list(range(1, 101)), 95), 2) == 95.05


async def test_one_streamed_request_is_timed_and_asks_for_an_exact_length(model):
    r = await stream_request(target(model), [{"role": "user", "content": "hello world " * 30}], 32)
    assert r.ok and r.completion_tokens == 32 and r.prompt_tokens == int(60 * 1.25) + 12
    assert r.ttft_s > 0 and len(r.itl_s) == 31 and r.tpot_s > 0
    sent = model.bodies[-1]
    assert sent["min_tokens"] == sent["max_tokens"] == 32 and sent["ignore_eos"] is True
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    bad = await stream_request(Target(model.url, "wrong", "default"), [{"role": "user", "content": "x"}], 4)
    assert not bad.ok and bad.error.startswith("HTTP 401")


async def test_max_throughput_finds_the_knee(model):
    # the fake stops scaling at 8 in flight; 15 % instead of 5 % keeps loopback noise from looking like a gain
    run = Run(target(model), Options(preset="max-throughput", input_tokens=64, output_tokens=24,
                                     max_concurrency=64, requests_per_level=0, plateau=0.15))
    res = await run.execute()
    assert res["valid"] and res["format"] == bench.FORMAT
    tps = [p["output_tok_s"] for p in res["points"]]
    assert tps[1] > tps[0] * 1.5                                  # more load pays below the knee
    assert 4 <= res["summary"]["at_concurrency"] <= 32            # and stops paying around it
    assert len(res["points"]) < 7                                 # the ramp stopped at the plateau, not at 64
    assert res["summary"]["peak_output_tok_s"] == max(tps)
    p = res["points"][0]
    assert p["ttft_ms"]["p50"] > 0 and p["tpot_ms"]["p95"] > 0 and p["prompt_tokens"]["mean"] > 0
    assert p["completion_tokens"] == {"mean": 24.0, "min": 24, "max": 24}
    assert "peak" in res["summary"]["text"]


async def test_latency_slo_searches_the_highest_concurrency_that_meets_it(model):
    model.per_token_s = 0.005
    # 5 ms per token up to the knee (8 in flight); above it every token gets slower, so 12 ms is met up to about 19
    run = Run(target(model), Options(preset="latency-slo", input_tokens=32, output_tokens=12, max_concurrency=64,
                                     slo_ttft_ms=5000, slo_tpot_ms=12.0, requests_per_level=16))
    res = await run.execute()
    best = res["summary"]["max_concurrency"]
    assert 8 <= best <= 24, res["summary"]
    levels = [p["concurrency"] for p in res["points"]]
    assert levels[:4] == [1, 2, 4, 8] and len(levels) > 5                # doubling, then the binary search


async def test_every_run_uses_fresh_prompts_unless_the_cache_is_the_point(model):
    for _ in range(2):
        await Run(target(model), Options(preset="fixed", levels=(1,), input_tokens=40, output_tokens=2,
                                         requests_per_level=2)).execute()
    prompts = [b["prompt"] for b in model.bodies]
    assert len(set(prompts)) == len(prompts)                               # no run replays another's prefix
    model.bodies.clear()
    await Run(target(model), Options(preset="fixed", levels=(2,), input_tokens=40, output_tokens=2, cache="warm",
                                     requests_per_level=4)).execute()
    assert len({b["prompt"] for b in model.bodies}) == 1                   # warm: one prompt, on purpose


async def test_a_switch_during_the_run_invalidates_it(model):
    run = Run(target(model), Options(preset="fixed", levels=(1, 2, 4), input_tokens=16, output_tokens=4,
                                     requests_per_level=2))

    async def switch_soon():
        while not run.points:
            await asyncio.sleep(0.005)
        model.revision = "rev-2"
    asyncio.create_task(switch_soon())
    res = await run.execute()
    assert res["valid"] is False and "changed during the run" in res["invalid_reason"]
    assert run.state == "invalid" and len(res["points"]) < 3


async def test_needles_measure_whether_the_context_is_used(model):
    good = await bench.needles(target(model), 4000, PromptMaker(7), count=6)
    assert good["found"] == 6 and good["score"] == 1.0
    model.forgetful = True
    half = await bench.needles(target(model), 4000, PromptMaker(7), count=6)
    assert half["found"] == 3 and half["score"] == 0.5


async def test_the_context_ladder_stops_at_the_deployed_limit(model):
    model.max_model_len = 40000
    run = Run(target(model), Options(preset="context", output_tokens=8, context_concurrency=(1,),
                                     contexts=(8192, 32768, 131072), requests_per_level=1, needles=3))
    res = await run.execute()
    assert res["summary"]["contexts"] == [8192, 32768]
    assert [r["context"] for r in res["ladder"]] == [8192, 32768]
    assert all(n["score"] == 1.0 for n in res["needles"]) and res["summary"]["needle_score_min"] == 1.0


async def test_an_alias_the_gateway_does_not_serve_fails_at_once(model):
    res = await Run(Target(model.url, KEY, "nope"), Options(preset="fixed", levels=(1,))).execute()
    assert res["valid"] is False and "does not serve nope" in res["invalid_reason"] and res["points"] == []


async def test_telemetry_reports_memory_floor_and_energy():
    readings = iter([{"A": {"mem_available_gib": 80.0, "power_w": 100.0}},
                     {"A": {"mem_available_gib": 60.0, "power_w": 140.0}},
                     {"A": {"mem_available_gib": 70.0, "power_w": None}}])

    async def sample():
        try:
            return next(readings)
        except StopIteration:
            return {}
    tele = bench.Telemetry(sample, interval=0.05)
    tele.start()
    await asyncio.sleep(0.2)
    out = (await tele.stop())["A"]
    assert out["min_mem_available_gib"] == 60.0 and out["mean_power_w"] == 120.0 and out["energy_wh"] >= 0


# ---- stored on node A -------------------------------------------------------------------------------
async def test_results_are_stored_against_the_revision_and_show_in_the_validation_record(cluster, model):
    from twinspark.controller.app import create_app
    from twinspark.controller.validation import build_record
    from twinspark.schemas.enums import Topology

    from .conftest import draft
    c = cluster.controller
    c.create_profile(draft("glm", Topology.TP2))
    rev = c.get_profile("glm").latest()
    model.revision = rev.revision_id
    res = await Run(target(model), Options(preset="fixed", levels=(1, 2), input_tokens=16, output_tokens=4,
                                           requests_per_level=2)).execute()
    app = create_app(c, management_key="k", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "k"}) as api:
        r = await api.post("/api/v1/benchmarks", json=res)
        assert r.status_code == 201 and r.json()["profile"] == "glm" and r.json()["evidence"] == "measured-by-client"
        listed = (await api.get("/api/v1/benchmarks", params={"profile": "glm"})).json()
        assert listed[0]["id"] == r.json()["id"] and "c1:" in listed[0]["summary"]["text"]
        full = (await api.get(f"/api/v1/benchmarks/{r.json()['id']}")).json()
        assert full["result"]["points"][0]["concurrency"] == 1
        assert (await api.post("/api/v1/benchmarks", json={"format": "nope"})).status_code == 422
        assert (await api.post("/api/v1/benchmarks", json={**res, "preset": "made-up"})).status_code == 422
        assert (await api.post("/api/v1/benchmarks", content=json.dumps({**res, "revision_id": "rev-unknown"}))
                ).status_code == 404
        assert (await api.post("/api/v1/benchmarks", content=json.dumps(res).replace('"seed": ', '"seed": NaN, "x": ')
                               )).status_code == 422
        big = {**res, "summary": {"text": "x" * (1024 * 1024 + 10)}}
        assert (await api.post("/api/v1/benchmarks", content=json.dumps(big))).status_code == 413
        assert (await api.get("/api/v1/benchmarks/nope")).status_code == 404
    rec = build_record(c, "glm")
    assert [b["job_id"] for b in rec["results"]["benchmarks"]] == [r.json()["id"]]
    from twinspark.controller.validation import to_markdown
    assert "measured by the client" in to_markdown(rec)


# ---- the client routes --------------------------------------------------------------------------------
class FakeSession:
    """The bits of ClientSession the benchmark routes use: node A's API (an ASGI app) and the gateway."""

    def __init__(self, node_app, fake: FakeModel):
        self.node = httpx.AsyncClient(transport=httpx.ASGITransport(app=node_app), base_url="http://node-a",
                                      headers={"x-api-key": "k"})
        self.fake = fake
        self.current = type("Conn", (), {"uses_ssh": False})()

    def gateway_url(self):
        return self.fake.url

    def inference_key(self):
        return KEY

    async def node_request(self, method, path, **kw):
        kw.pop("timeout", None)
        return await self.node.request(method, path, **kw)


async def wait_done(c, bench_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = (await c.get(f"/api/v1/client/bench/{bench_id}")).json()
        if st["state"] not in ("pending", "running"):
            return st
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {bench_id} did not finish")


async def test_the_client_runs_a_benchmark_and_stores_it_on_node_a(cluster, model):
    from fastapi import FastAPI

    from twinspark.client.bench_local import add_bench_routes
    from twinspark.controller.app import create_app
    from twinspark.schemas.enums import Topology

    from .conftest import draft
    c = cluster.controller
    c.create_profile(draft("glm", Topology.TP2))
    model.revision = c.get_profile("glm").latest().revision_id
    node_app = create_app(c, management_key="k", run_startup=False, background=False)
    app = FastAPI()
    add_bench_routes(app, FakeSession(node_app, model), lambda: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as cl:
        r = await cl.post("/api/v1/client/bench", json={"preset": "fixed", "levels": [1, 2], "input_tokens": 16,
                                                        "output_tokens": 4, "requests_per_level": 2})
        assert r.status_code == 202 and r.json()["state"] in ("pending", "running")
        st = await wait_done(cl, r.json()["id"])
        assert st["state"] == "completed" and st["uploaded"] and st["upload_error"] is None
        listed = (await cl.get("/api/v1/client/bench")).json()
        assert "max-throughput" in listed["presets"] and listed["runs"][0]["id"] == r.json()["id"]
        stored = (await cl.get("/api/v1/client/bench/nope")).status_code
        assert stored == 404
        r = await cl.post("/api/v1/client/bench", json={"preset": "made-up"})
        assert r.status_code == 422
        # an alias nobody serves: the run fails at once and nothing is uploaded
        r = await cl.post("/api/v1/client/bench", json={"alias": "nope", "preset": "fixed", "levels": [1]})
        st = await wait_done(cl, r.json()["id"])
        assert st["state"] == "failed" and st["uploaded"] is None and st["upload_error"] is None
    stored = c.store.load_job(listed["runs"][0]["uploaded"])
    assert stored.kind == "benchmark" and stored.payload["profile"] == "glm"


async def test_compare_switches_measures_both_and_switches_back(model, monkeypatch):
    """Node A here is a stand-in that activates instantly: the point is the order and the drain policy."""
    from fastapi import FastAPI
    from starlette.responses import JSONResponse as J

    from twinspark.client import bench_local
    from twinspark.client.bench_local import add_bench_routes
    calls, stored = [], []
    node = FastAPI()
    state = {"active": {"profile": "old", "revision_id": "rev-old"}}

    @node.get("/api/v1/status")
    def status():
        return {"active": state["active"], "busy": False, "telemetry": {}}

    @node.post("/api/v1/profiles/{name}/activate")
    async def activate(name: str, request: Request):
        body = await request.json()
        rev = request.query_params["revision"]
        calls.append((name, rev, body["drain_policy"], body["expected_active"]))
        rid = rev if rev.startswith("rev-") else f"rev-{name}"
        state["active"] = {"profile": name, "revision_id": rid}
        model.revision = rid
        return J({"job_id": f"job-{len(calls)}"}, status_code=202)

    @node.get("/api/v1/jobs/{job_id}")
    def job(job_id: str):
        return {"job_id": job_id, "state": "completed"}

    @node.post("/api/v1/benchmarks")
    async def store(request: Request):
        stored.append((await request.json())["revision_id"])
        return J({"id": f"benchmark-{len(stored)}"}, status_code=201)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(bench_local.asyncio, "sleep", lambda s: real_sleep(min(s, 0.01)))
    app = FastAPI()
    add_bench_routes(app, FakeSession(node, model), lambda: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as cl:
        r = await cl.post("/api/v1/client/bench/compare", json={
            "a": {"profile": "glm", "revision": "latest"}, "b": {"profile": "qwen", "revision": "latest"},
            "levels": [1, 2], "input_tokens": 16, "output_tokens": 4})
        st = await wait_done(cl, r.json()["id"])
    assert st["state"] == "completed", st["message"]
    assert [(n, d) for n, _, d, _ in calls] == [("glm", "abort"), ("qwen", "abort"), ("old", "abort")]
    assert calls[0][3] == "rev-old" and calls[2][1] == "rev-old"              # expected_active guards each switch
    assert stored == ["rev-glm", "rev-qwen"]
    assert [s["revision_id"] for s in st["sides"]] == ["rev-glm", "rev-qwen"] and all(s["valid"] for s in st["sides"])
    row = st["table"][0]
    assert row["concurrency"] == 1 and row["a_output_tok_s"] > 0 and row["b_output_tok_s"] > 0


# ---- tsm bench ----------------------------------------------------------------------------------------
def test_tsm_bench_runs_stores_lists_and_shows(model, monkeypatch, capsys, tmp_path):
    from twinspark import cli
    model.revision = "rev-glm"
    rows: list[dict] = []

    class FakeApi:
        as_json = False

        def __call__(self, method, path, **kw):
            if path == "/api/v1/status":
                return {"busy": False, "telemetry": {"A": {"mem_available_gib": 50.0, "gpu": {"power_w": 90.0}}}}
            if method == "POST" and path == "/api/v1/benchmarks":
                doc = kw["json"]
                rows.append({"id": "benchmark-1", "profile": "glm", "label": "r1", "preset": doc["preset"],
                             "valid": doc["valid"], "summary": doc["summary"], "created_at": time.time(),
                             "revision_id": doc["revision_id"], "alias": doc["alias"], "evidence": "measured-by-client",
                             "result": doc})
                return {k: v for k, v in rows[-1].items() if k != "result"}
            if path == "/api/v1/benchmarks":
                return [{k: v for k, v in r.items() if k != "result"} for r in rows]
            if path == "/api/v1/benchmarks/benchmark-1":
                return rows[0]
            raise AssertionError(path)
    monkeypatch.setattr(cli, "Api", lambda *a, **k: FakeApi())
    out = tmp_path / "result.json"
    assert cli.main(["--secrets-dir", str(tmp_path / "none"), "bench", "run", "fixed", "--gateway",
                     model.url.removesuffix("/v1"), "--inference-key", KEY, "--levels", "1,2", "--input-tokens", "16",
                     "--output-tokens", "4", "--requests-per-level", "2", "-o", str(out)]) == 0
    text = capsys.readouterr().out
    assert "c1:" in text and "out tok/s" in text and "stored on node A as benchmark-1" in text
    doc = json.loads(out.read_text())
    assert doc["revision_id"] == "rev-glm" and doc["options"]["levels"] == [1, 2]
    assert doc["telemetry"]["A"]["min_mem_available_gib"] == 50.0
    cli.main(["bench", "list"])
    assert "benchmark-1" in capsys.readouterr().out
    cli.main(["bench", "show", "benchmark-1"])
    shown = capsys.readouterr().out
    assert "measured-by-client" in shown and "TTFT p95" in shown and "lowest free memory 50.0 GiB" in shown
    with pytest.raises(SystemExit) as ex:                  # an alias nobody serves is not a valid run
        cli.main(["--secrets-dir", str(tmp_path / "none"), "bench", "run", "fixed", "--alias", "nope", "--gateway",
                  model.url, "--inference-key", KEY, "--levels", "1"])
    assert ex.value.code == 1 and "NOT VALID" in capsys.readouterr().out and len(rows) == 1
