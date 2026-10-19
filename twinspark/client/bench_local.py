"""The client's benchmark routes: runs go from this computer through the gateway forward.

* ``POST /api/v1/client/bench`` starts a run (preset and options); ``GET …/bench[/{id}]`` shows progress and
  results; ``POST …/bench/{id}/cancel`` stops one. A finished run is stored on node A
  (``POST /api/v1/benchmarks``) against the revision it measured.
* ``POST /api/v1/client/bench/compare`` runs the same fixed workload on two revisions: it switches node A to
  the first (drain policy *abort*: a busy model is not cut off), measures, switches to the second, measures,
  and switches back to what served before if asked to.

Telemetry for the run (node memory, GPU power) comes from node A's status, polled every 2 s.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from typing import Any, Callable, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .bench import PRESETS, Options, Run, Target
from .session import ClientSession

MAX_KEEP = 20


class BenchIn(BaseModel):
    alias: str = Field("default", min_length=1, max_length=63)
    preset: Literal["max-throughput", "latency-slo", "context", "quality", "fixed"] = "max-throughput"
    input_tokens: int = Field(1024, ge=16, le=1_048_576)
    output_tokens: int = Field(128, ge=1, le=32768)
    cache: Literal["cold", "warm"] = "cold"
    min_concurrency: int = Field(1, ge=1, le=256)
    max_concurrency: int = Field(64, ge=1, le=256)
    requests_per_level: int = Field(0, ge=0, le=2000)
    slo_ttft_ms: float = Field(2000.0, gt=0)
    slo_tpot_ms: float = Field(100.0, gt=0)
    contexts: Optional[list[int]] = Field(None, max_length=12)
    context_concurrency: Optional[list[int]] = Field(None, max_length=6)
    needles: int = Field(5, ge=1, le=12)
    levels: Optional[list[int]] = Field(None, max_length=10)
    upload: bool = True

    def options(self) -> Options:
        kw: dict[str, Any] = self.model_dump(exclude={"alias", "upload", "contexts", "context_concurrency", "levels"})
        for name in ("contexts", "context_concurrency", "levels"):
            value = getattr(self, name)
            if value:
                kw[name] = tuple(int(v) for v in value)
        return Options(**kw)


class Side(BaseModel):
    profile: str = Field(min_length=1, max_length=64)
    revision: str = Field("latest", min_length=1, max_length=80)


class CompareIn(BaseModel):
    alias: str = Field("default", min_length=1, max_length=63)
    a: Side
    b: Side
    levels: list[int] = Field(default_factory=lambda: [1, 4, 16], min_length=1, max_length=8)
    input_tokens: int = Field(1024, ge=16, le=262144)
    output_tokens: int = Field(128, ge=1, le=8192)
    restore: bool = True


class Benchmarks:
    """Runs of this client session, newest last (in memory; finished results live on node A)."""

    def __init__(self, session: ClientSession):
        self.session = session
        self.items: dict[str, dict[str, Any]] = {}

    def _keep(self) -> None:
        while len(self.items) > MAX_KEEP:
            oldest = next(iter(self.items))
            if self.items[oldest].get("task") and not self.items[oldest]["task"].done():
                break
            self.items.pop(oldest)

    async def _sample(self) -> dict[str, dict[str, Any]]:
        r = await self.session.node_request("GET", "/api/v1/status", timeout=10)
        tel = (r.json() or {}).get("telemetry") or {}
        return {n: {"mem_available_gib": t.get("mem_available_gib"), "power_w": (t.get("gpu") or {}).get("power_w")}
                for n, t in tel.items() if isinstance(t, dict)}

    async def _busy(self, alias: str) -> bool:
        try:
            r = await self.session.node_request("GET", "/api/v1/status", timeout=10)
            st = r.json() or {}
        except Exception:  # noqa: BLE001 - unknown is not "busy": the revision guard still applies
            return False
        return switch_touches(st, alias)

    def _target(self, alias: str, concurrency: int) -> Target:
        c = self.session.current
        direct = bool(c and not c.uses_ssh)
        http = httpx.AsyncClient(timeout=httpx.Timeout(900, connect=15), trust_env=False, verify=direct,
                                 limits=httpx.Limits(max_connections=concurrency + 8))
        return Target(self.session.gateway_url() or "", self.session.inference_key(), alias, http=http)

    async def _upload(self, result: dict[str, Any]) -> Optional[str]:
        r = await self.session.node_request("POST", "/api/v1/benchmarks", json=result, timeout=30)
        if r.status_code != 201:
            raise RuntimeError(f"node A did not store the result ({r.status_code}): {r.text[:200]}")
        return r.json().get("id")

    def start(self, req: BenchIn) -> dict[str, Any]:
        opts = req.options()
        top = max([opts.max_concurrency, *opts.levels, *opts.context_concurrency])
        run = Run(self._target(req.alias, top), opts, sampler=self._sample, busy=lambda: self._busy(req.alias))
        item: dict[str, Any] = {"run": run, "uploaded": None, "upload_error": None, "kind": "run"}

        async def go() -> None:
            try:
                result = await run.execute()
                if req.upload and result.get("points") and result.get("revision_id"):
                    try:
                        item["uploaded"] = await self._upload(result)
                    except Exception as exc:  # noqa: BLE001 - the result stays here and can be retried
                        item["upload_error"] = str(exc)[:300]
            except Exception as exc:  # noqa: BLE001
                run.state, run.message = "failed", f"{type(exc).__name__}: {exc}"[:300]
            finally:
                await run.target.http.aclose()
        item["task"] = asyncio.create_task(go())
        self.items[run.id] = item
        self._keep()
        return self.view(run.id)

    def view(self, bench_id: str) -> dict[str, Any]:
        item = self.items.get(bench_id)
        if item is None:
            raise KeyError(bench_id)
        if item["kind"] == "compare":
            out = {k: v for k, v in item.items() if k not in ("task", "current")}
            run = item.get("current")
            out["current"] = run.progress() if run is not None else None
            return out
        out = {**item["run"].progress(), "uploaded": item["uploaded"], "upload_error": item["upload_error"]}
        if not item["task"].done() and out["state"] not in ("pending", "running"):
            # measured, not yet stored: the run is finished only once node A has the result
            out.update(state="running", message="storing the result on node A")
        return out

    def cancel(self, bench_id: str) -> dict[str, Any]:
        item = self.items.get(bench_id)
        if item is None:
            raise KeyError(bench_id)
        if item["kind"] == "compare":
            item["cancelled"] = True
            run = item.get("current")
            if run is not None:
                run.cancel.set()
        else:
            item["run"].cancel.set()
        return self.view(bench_id)

    # ---- compare ------------------------------------------------------------------------------
    async def _switch(self, side: Side, item: dict) -> dict[str, Any]:
        """Activate one side on node A and wait for the job; a busy model aborts the switch (not cut off)."""
        st = (await self.session.node_request("GET", "/api/v1/status", timeout=10)).json() or {}
        body = {"request_id": f"bench-{secrets.token_hex(8)}", "drain_policy": "abort",
                "expected_active": (st.get("active") or {}).get("revision_id")}
        r = await self.session.node_request("POST", f"/api/v1/profiles/{side.profile}/activate",
                                            params={"revision": side.revision}, json=body, timeout=60)
        if r.status_code != 202:
            raise RuntimeError(f"switching to {side.profile} {side.revision} was refused ({r.status_code}): "
                               f"{r.text[:200]}")
        job_id = r.json()["job_id"]
        item["message"] = f"switching node A to {side.profile} {side.revision} (job {job_id})"
        deadline = time.monotonic() + 4 * 3600
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            job = (await self.session.node_request("GET", f"/api/v1/jobs/{job_id}", timeout=15)).json()
            if job.get("state") in ("completed", "failed", "cancelled"):
                if job["state"] != "completed":
                    raise RuntimeError(f"the switch to {side.profile} {side.revision} {job['state']}: "
                                       f"{(job.get('error') or '')[:300]}")
                return job
        raise RuntimeError("the switch did not finish within 4 hours")

    def compare(self, req: CompareIn) -> dict[str, Any]:
        cid = "compare-" + secrets.token_hex(5)
        item: dict[str, Any] = {"id": cid, "kind": "compare", "state": "running", "message": "starting",
                                "sides": [], "table": [], "cancelled": False, "current": None}

        async def go() -> None:
            original = None
            try:
                st = (await self.session.node_request("GET", "/api/v1/status", timeout=10)).json() or {}
                original = st.get("active")
                for side in (req.a, req.b):
                    if item["cancelled"]:
                        break
                    job = await self._switch(side, item)
                    opts = Options(preset="fixed", levels=tuple(req.levels), input_tokens=req.input_tokens,
                                   output_tokens=req.output_tokens)
                    run = Run(self._target(req.alias, max(req.levels)), opts, sampler=self._sample,
                              busy=lambda: self._busy(req.alias))
                    item["current"] = run
                    item["message"] = f"measuring {side.profile} {side.revision}"
                    try:
                        result = await run.execute()
                    finally:
                        await run.target.http.aclose()
                    uploaded = None
                    if result.get("points") and result.get("revision_id"):
                        with contextlib.suppress(Exception):
                            uploaded = await self._upload(result)
                    item["sides"].append({"profile": side.profile, "revision": side.revision,
                                          "revision_id": result.get("revision_id"), "job_id": job.get("job_id"),
                                          "result_id": uploaded, "valid": result.get("valid"),
                                          "summary": result.get("summary"), "points": result.get("points")})
                item["table"] = _compare_table(item["sides"])
                if req.restore and original and len(item["sides"]) == 2 \
                        and item["sides"][-1].get("revision_id") != original.get("revision_id"):
                    item["message"] = f"switching back to {original['profile']}"
                    await self._switch(Side(profile=original["profile"], revision=original["revision_id"]), item)
                item["state"] = "cancelled" if item["cancelled"] else "completed"
                item["message"] = "done" if not item["cancelled"] else "cancelled"
            except Exception as exc:  # noqa: BLE001
                item["state"], item["message"] = "failed", str(exc)[:400]
            finally:
                item["current"] = None
        item["task"] = asyncio.create_task(go())
        self.items[cid] = item
        self._keep()
        return self.view(cid)


def switch_touches(status: dict, alias: str) -> bool:
    """Whether the switch node A is running affects ``alias``: a switch of one split half leaves the other
    half's alias alone, so a benchmark of that alias stays valid."""
    if not (status.get("busy") or status.get("current_job")):
        return False
    aliases = (status.get("switch") or {}).get("aliases")
    return aliases is None or alias in aliases


def _compare_table(sides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_c: dict[int, dict[str, Any]] = {}
    for i, side in enumerate(sides):
        tag = "ab"[i]
        for p in side.get("points") or []:
            row = by_c.setdefault(p["concurrency"], {"concurrency": p["concurrency"]})
            row[f"{tag}_output_tok_s"] = p.get("output_tok_s")
            row[f"{tag}_ttft_p95_ms"] = (p.get("ttft_ms") or {}).get("p95")
            row[f"{tag}_tpot_p95_ms"] = (p.get("tpot_ms") or {}).get("p95")
    return [by_c[c] for c in sorted(by_c)]


def add_bench_routes(app: FastAPI, session: ClientSession, connected: Callable[[], None]) -> Benchmarks:
    bench = Benchmarks(session)
    app.state.benchmarks = bench

    @app.get("/api/v1/client/bench")
    def runs():
        return {"presets": list(PRESETS), "runs": [bench.view(i) for i in reversed(list(bench.items))]}

    # async: the run is a task on the app's event loop (a plain def would run in a worker thread)
    @app.post("/api/v1/client/bench", status_code=202)
    async def start(req: BenchIn):
        connected()
        if not session.gateway_url():
            raise HTTPException(503, "no gateway address: connect to node A first",
                                headers={"X-TwinSpark-Client": "transport"})
        return bench.start(req)

    @app.post("/api/v1/client/bench/compare", status_code=202)
    async def compare(req: CompareIn):
        connected()
        return bench.compare(req)

    @app.get("/api/v1/client/bench/{bench_id}")
    def one(bench_id: str):
        try:
            return bench.view(bench_id)
        except KeyError:
            raise HTTPException(404, "no such run in this client session") from None

    @app.post("/api/v1/client/bench/{bench_id}/cancel")
    def cancel(bench_id: str):
        try:
            return bench.cancel(bench_id)
        except KeyError:
            raise HTTPException(404, "no such run in this client session") from None

    return bench
