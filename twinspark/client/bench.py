"""Benchmarks from the workstation, against the endpoint apps use (master plan §2.1).

The load comes from this computer through the gateway forward, so the Sparks only run the model. Each
request streams (``stream_options.include_usage``) and asks for an exact output length (``min_tokens`` =
``max_tokens``, ``ignore_eos``), so time to first token (TTFT), inter-token latency (ITL), time per output
token (TPOT) and throughput are measured the same way every time. Prompts are random sentences from a
fixed word list with a **fresh seed per run**: a second run never replays the first one's prefix cache.
``cache="warm"`` deliberately sends one prompt repeatedly to measure the cache instead.

Presets:

* ``max-throughput`` — 1024 in / 128 out; concurrency doubles until throughput stops growing (less than
  5 % more), errors appear or the ceiling is reached; the answer is the measured peak and its concurrency.
* ``latency-slo`` — the highest concurrency whose p95 TTFT and p95 TPOT stay within the targets
  (doubling, then a binary search between the last level that met them and the first that did not).
* ``context`` — the context ladder 8K/32K/128K/262K/512K/1M up to the deployed limit at concurrency
  1/2/4: prefill time and decode speed per length, plus a retrieval check with several needles spread over
  the context (a large nominal context is not proof that it is used).
* ``quality`` — needles at the deployed context and a tool call answered and continued.
* ``fixed`` — the same levels every time (default 1, 4, 16): what "compare revisions" runs on each side.

A run whose model changed while it ran (another client switched) is marked invalid and stops.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import secrets
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx

FORMAT = "twinspark-bench/1"
PRESETS = ("max-throughput", "latency-slo", "context", "quality", "fixed")
CONTEXT_LADDER = (8192, 32768, 131072, 262144, 524288, 1048576)

# common one-token English words: random sentences of them size a prompt well without a tokenizer
WORDS = ("the of and to in is was for on that with as by at from his her it be this are or an which had "
         "have one not but all were they she he we can there their been if more when will would who so no "
         "time only into other new some could these two may first then do any like my now over such our man "
         "me even most made after also did many before must through back years where much your way well down "
         "should because each just those people how too little state good very make world still own see men "
         "work long get here between both life being under never day same another know while last might us "
         "great old year off come since against go came right used take three states himself few house use "
         "during without again place around however home small found thought went say part once general high "
         "upon school every does got united left number course war until always away something fact though "
         "water less public put think almost hand enough far took head yet government system better set told "
         "nothing night end why called did eyes find going look asked later knew point next program city "
         "business give group toward young days let room president side social given present several order "
         "national possible rather second face per among form important often things looked early white case "
         "john become large big need four within felt along children saw best church ever least power "
         "development light thing seemed family interest want members mind country area others done turned "
         "although open god service certain kind problem began different door thus help means sense whole "
         "matter perhaps itself york times human law line above name example action company hands local show "
         "five history whether gave either today act feet across taken past quite anything seen having death "
         "experience body word half really week").split()


# ---- statistics -----------------------------------------------------------------------------------
def percentile(values: list[float], p: float) -> Optional[float]:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def dist(values: list[float], scale: float = 1.0, digits: int = 1) -> Optional[dict[str, float]]:
    vals = [v * scale for v in values if v is not None]
    if not vals:
        return None
    return {"p50": round(percentile(vals, 50), digits), "p95": round(percentile(vals, 95), digits),
            "p99": round(percentile(vals, 99), digits), "mean": round(statistics.fmean(vals), digits),
            "n": len(vals)}


# ---- prompts --------------------------------------------------------------------------------------------
class PromptMaker:
    """Random sentences of about ``tokens`` tokens; learns words-per-token from the server's usage counts."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.tokens_per_word = 1.0

    def words_for(self, tokens: int) -> int:
        return max(1, int(tokens / self.tokens_per_word))

    def text(self, tokens: int) -> str:
        n = self.words_for(tokens)
        words = [self.rng.choice(WORDS) for _ in range(n)]
        for i in range(12, n, 13):                       # sentences, so the text looks like text
            words[i] = words[i] + "."
        return " ".join(words)

    def learn(self, words: int, prompt_tokens: Optional[int]) -> None:
        if prompt_tokens and words > 50:
            seen = prompt_tokens / words
            self.tokens_per_word = 0.5 * self.tokens_per_word + 0.5 * seen if self.tokens_per_word != 1.0 else seen


# ---- one request ---------------------------------------------------------------------------------------
@dataclass
class RequestResult:
    ok: bool
    error: Optional[str] = None
    aborted: bool = False
    ttft_s: Optional[float] = None
    e2e_s: Optional[float] = None
    itl_s: list[float] = field(default_factory=list)
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    content: str = ""

    @property
    def tpot_s(self) -> Optional[float]:
        if self.ttft_s is None or self.e2e_s is None or not self.completion_tokens or self.completion_tokens < 2:
            return None
        return (self.e2e_s - self.ttft_s) / (self.completion_tokens - 1)


@dataclass
class Target:
    base_url: str                      # …/v1
    api_key: Optional[str]
    alias: str
    http: Optional[httpx.AsyncClient] = None
    timeout_s: float = 900.0

    def headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self.api_key:
            h["authorization"] = f"Bearer {self.api_key}"
        return h


async def stream_request(target: Target, messages: list[dict], max_tokens: int, *, exact: bool = True,
                         temperature: float = 0.0, keep_text: bool = False) -> RequestResult:
    """One streamed chat completion; times every chunk that carries generated text."""
    body: dict[str, Any] = {"model": target.alias, "messages": messages, "max_tokens": max_tokens,
                            "temperature": temperature, "stream": True, "stream_options": {"include_usage": True}}
    if exact:
        body.update(min_tokens=max_tokens, ignore_eos=True)       # vLLM: exactly max_tokens tokens
    http = target.http
    own = http is None
    if own:
        http = httpx.AsyncClient(timeout=httpx.Timeout(target.timeout_s, connect=15), trust_env=False)
    res = RequestResult(ok=False)
    t0 = time.perf_counter()
    last: Optional[float] = None
    chunks = 0
    parts: list[str] = []
    try:
        async with http.stream("POST", target.base_url.rstrip("/") + "/chat/completions", headers=target.headers(),
                               json=body, timeout=httpx.Timeout(target.timeout_s, connect=15)) as r:
            if r.status_code != 200:
                text = (await r.aread())[:300].decode("utf-8", "replace")
                res.error = f"HTTP {r.status_code}: {text}"
                return res
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    doc = json.loads(data)
                except ValueError:
                    continue
                usage = doc.get("usage")
                if isinstance(usage, dict):
                    res.prompt_tokens = usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) \
                        else res.prompt_tokens
                    res.completion_tokens = usage.get("completion_tokens") \
                        if isinstance(usage.get("completion_tokens"), int) else res.completion_tokens
                for choice in doc.get("choices") or []:
                    delta = (choice or {}).get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    piece = (delta.get("content") or "") + reasoning
                    if delta.get("tool_calls"):
                        piece += "·"
                    if not piece:
                        continue
                    now = time.perf_counter()
                    if res.ttft_s is None:
                        res.ttft_s = now - t0
                    elif last is not None:
                        res.itl_s.append(now - last)
                    last = now
                    chunks += 1
                    if keep_text:
                        parts.append(delta.get("content") or "")
        res.e2e_s = time.perf_counter() - t0
        if res.completion_tokens is None:
            res.completion_tokens = chunks              # no usage chunk: one token per text chunk
        res.ok = res.ttft_s is not None
        if not res.ok:
            res.error = "the stream ended without any generated text"
    except httpx.TimeoutException:
        res.error, res.aborted = f"no answer within {target.timeout_s:.0f}s", True
    except httpx.HTTPError as exc:
        res.error = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        if own:
            await http.aclose()
    res.content = "".join(parts)
    return res


# ---- one load level ----------------------------------------------------------------------------------
@dataclass
class Workload:
    input_tokens: int = 1024
    output_tokens: int = 128
    cache: str = "cold"                 # cold: a different prompt per request; warm: the same prompt
    seed: int = 0


async def run_point(target: Target, workload: Workload, concurrency: int, requests: int,
                    prompts: PromptMaker, cancel: Optional[asyncio.Event] = None) -> dict[str, Any]:
    """``requests`` requests with ``concurrency`` in flight at a time; their distributions and throughput."""
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(requests):
        queue.put_nowait(i)
    results: list[RequestResult] = []
    sizes: list[int] = []
    warm = prompts.text(workload.input_tokens) if workload.cache == "warm" else None
    if warm is not None:                                     # fill the cache once, not measured
        await stream_request(target, [{"role": "user", "content": warm}], 1, exact=True)

    async def worker() -> None:
        while not queue.empty() and not (cancel and cancel.is_set()):
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            text = warm or prompts.text(workload.input_tokens)
            words = text.count(" ") + 1
            r = await stream_request(target, [{"role": "user", "content": text}], workload.output_tokens)
            if r.ok and warm is None:
                prompts.learn(words, r.prompt_tokens)
            sizes.append(words)
            results.append(r)

    t0 = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(max(1, min(concurrency, requests)))))
    wall = time.perf_counter() - t0
    ok = [r for r in results if r.ok]
    out_tokens = sum(r.completion_tokens or 0 for r in ok)
    return {
        "concurrency": concurrency, "requests": len(results), "ok": len(ok),
        "errors": sum(1 for r in results if not r.ok and not r.aborted),
        "aborted": sum(1 for r in results if r.aborted),
        "error_rate": round(1 - len(ok) / len(results), 3) if results else None,
        "first_error": next((r.error for r in results if r.error), None),
        "duration_s": round(wall, 2),
        "output_tok_s": round(out_tokens / wall, 1) if wall > 0 else None,
        "request_s": round(len(ok) / wall, 3) if wall > 0 else None,
        "ttft_ms": dist([r.ttft_s for r in ok], 1000),
        "itl_ms": dist([x for r in ok for x in r.itl_s], 1000, 2),
        "tpot_ms": dist([r.tpot_s for r in ok if r.tpot_s is not None], 1000, 2),
        "e2e_s": dist([r.e2e_s for r in ok], 1, 2),
        "prompt_tokens": _counts([r.prompt_tokens for r in ok]),
        "completion_tokens": _counts([r.completion_tokens for r in ok]),
        "cache": workload.cache,
    }


def _counts(values: list[Optional[int]]) -> Optional[dict[str, float]]:
    vals = [v for v in values if isinstance(v, int)]
    if not vals:
        return None
    return {"mean": round(statistics.fmean(vals), 1), "min": min(vals), "max": max(vals)}


# ---- quality --------------------------------------------------------------------------------------------
_VAULTS = ("amber", "basalt", "cobalt", "dune", "ember", "fjord", "garnet", "harbor", "indigo", "juniper",
           "krypton", "lagoon")


async def needles(target: Target, context_tokens: int, prompts: PromptMaker, count: int = 5) -> dict[str, Any]:
    """``count`` facts spread evenly through about ``context_tokens`` tokens of text; the model has to list them."""
    rng = random.Random(prompts.rng.random())
    names = rng.sample(_VAULTS, min(count, len(_VAULTS)))
    codes = {n: str(rng.randint(1000, 9999)) for n in names}
    filler = max(256, context_tokens - 200 - 30 * len(names))
    per = filler // (len(names) + 1)
    pieces = []
    for n in names:
        pieces.append(prompts.text(per))
        pieces.append(f"Remember this: the code for the {n} vault is {codes[n]}.")
    pieces.append(prompts.text(per))
    question = ("Somewhere in the text above, codes for several vaults are given. List every vault and its code, "
                "one per line, as `name: code`. Nothing else.")
    r = await stream_request(target, [{"role": "user", "content": "\n\n".join(pieces) + "\n\n" + question}],
                             max_tokens=40 + 15 * len(names), exact=False, keep_text=True)
    found = [n for n in names if codes[n] in r.content]
    return {"context_tokens": r.prompt_tokens or context_tokens, "needles": len(names), "found": len(found),
            "score": round(len(found) / len(names), 2) if r.ok else 0.0, "ok": r.ok, "error": r.error,
            "ttft_ms": round(r.ttft_s * 1000) if r.ttft_s else None}


async def tool_check(target: Target) -> list[dict[str, Any]]:
    """A chat, a streamed reply and a tool call answered and continued (the Integrations check)."""
    from .adapters.validate import validate_endpoint
    checks = await validate_endpoint(target.base_url, target.api_key, target.alias, http=target.http, tools=True)
    return [c.to_dict() for c in checks]


# ---- guards and telemetry ------------------------------------------------------------------------------
async def served_revision(target: Target) -> tuple[Optional[str], Optional[int]]:
    """(revision id, max_model_len) the gateway reports for the alias."""
    http = target.http or httpx.AsyncClient(timeout=15, trust_env=False)
    try:
        r = await http.get(target.base_url.rstrip("/") + "/models", headers=target.headers(), timeout=15)
        data = r.json().get("data") if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        data = []
    finally:
        if target.http is None:
            await http.aclose()
    m = next((x for x in data or [] if isinstance(x, dict) and x.get("id") == target.alias), None)
    if m is None:
        return None, None
    meta = m.get("twinspark") or {}
    ctx = m.get("max_model_len") or meta.get("max_model_len")
    return meta.get("revision_id") or meta.get("revision"), ctx if isinstance(ctx, int) else None


Sampler = Callable[[], Awaitable[dict[str, dict[str, Any]]]]


class Telemetry:
    """Node readings polled while a run goes: lowest MemAvailable, mean GPU power and the energy it implies."""

    def __init__(self, sample: Optional[Sampler], interval: float = 2.0):
        self.sample, self.interval = sample, interval
        self.points: dict[str, list[tuple[float, Optional[float], Optional[float]]]] = {}
        self._task: Optional[asyncio.Task] = None

    async def _loop(self) -> None:
        while True:
            try:
                nodes = await self.sample() if self.sample else {}
            except Exception:  # noqa: BLE001 - telemetry is a courtesy; the run goes on without it
                nodes = {}
            now = time.time()
            for n, t in (nodes or {}).items():
                self.points.setdefault(n, []).append((now, _f(t.get("mem_available_gib")), _f(t.get("power_w"))))
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self.sample is not None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> dict[str, Any]:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        out = {}
        for n, pts in self.points.items():
            mem = [m for _, m, _ in pts if m is not None]
            power = [p for _, _, p in pts if p is not None]
            span = pts[-1][0] - pts[0][0] if len(pts) > 1 else 0.0
            out[n] = {"samples": len(pts), "min_mem_available_gib": round(min(mem), 2) if mem else None,
                      "mean_power_w": round(statistics.fmean(power), 1) if power else None,
                      "energy_wh": round(statistics.fmean(power) * span / 3600, 2) if power and span else None}
        return out


def _f(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ---- a run ---------------------------------------------------------------------------------------------------
@dataclass
class Options:
    preset: str = "max-throughput"
    input_tokens: int = 1024
    output_tokens: int = 128
    cache: str = "cold"
    min_concurrency: int = 1
    max_concurrency: int = 64
    requests_per_level: int = 0          # 0 = 4 x concurrency, at least 8
    plateau: float = 0.05
    max_error_rate: float = 0.05
    slo_ttft_ms: float = 2000.0
    slo_tpot_ms: float = 100.0
    contexts: tuple[int, ...] = CONTEXT_LADDER
    context_concurrency: tuple[int, ...] = (1, 2, 4)
    needles: int = 5
    levels: tuple[int, ...] = (1, 4, 16)
    seed: Optional[int] = None

    def requests_for(self, concurrency: int) -> int:
        # fewer requests than the concurrency would never put that many in flight: the level would not be measured
        return max(concurrency, self.requests_per_level) if self.requests_per_level else max(8, 4 * concurrency)


class Invalid(Exception):
    """The model behind the alias changed while the run was going."""


class Run:
    """One benchmark run: progress for the GUI, a cancel switch, and the result document."""

    def __init__(self, target: Target, options: Options, *, sampler: Optional[Sampler] = None,
                 busy: Optional[Callable[[], Awaitable[bool]]] = None):
        self.id = "bench-" + secrets.token_hex(5)
        self.target, self.options = target, options
        self.seed = options.seed if options.seed is not None else secrets.randbelow(2 ** 31)
        self.prompts = PromptMaker(self.seed)
        self.cancel = asyncio.Event()
        self.telemetry = Telemetry(sampler)
        self.busy = busy
        self.points: list[dict[str, Any]] = []
        self.state, self.message = "pending", "starting"
        self.result: Optional[dict[str, Any]] = None
        self.revision: Optional[str] = None
        self.max_model_len: Optional[int] = None

    def progress(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "message": self.message, "points": self.points,
                "preset": self.options.preset, "alias": self.target.alias, "result": self.result}

    async def _guard(self) -> None:
        rev, _ = await served_revision(self.target)
        if rev != self.revision:
            raise Invalid(f"the model behind {self.target.alias} changed during the run ({self.revision} → {rev})")
        if self.busy is not None and await self.busy():
            raise Invalid("node A started a switch during the run")

    async def _point(self, workload: Workload, concurrency: int) -> dict[str, Any]:
        self.message = (f"{self.options.preset}: concurrency {concurrency}, {workload.input_tokens:,} in / "
                        f"{workload.output_tokens} out")
        p = await run_point(self.target, workload, concurrency, self.options.requests_for(concurrency),
                            self.prompts, self.cancel)
        await self._guard()
        self.points.append(p)
        return p

    async def execute(self) -> dict[str, Any]:
        o = self.options
        started = time.time()
        self.state = "running"
        self.revision, self.max_model_len = await served_revision(self.target)
        if self.revision is None and self.max_model_len is None:
            self.state, self.message = "failed", f"the gateway does not serve {self.target.alias}"
            self.result = self._doc(started, valid=False, reason=self.message, summary={})
            return self.result
        self.telemetry.start()
        valid, reason, summary, extra = True, None, {}, {}
        own_http = self.target.http is None
        if own_http:                       # one pool for the run: a client per request would measure its setup
            top = max([o.max_concurrency, *o.levels, *o.context_concurrency])
            self.target.http = httpx.AsyncClient(timeout=httpx.Timeout(self.target.timeout_s, connect=15),
                                                 trust_env=False, limits=httpx.Limits(max_connections=top + 8))
        try:
            if o.preset == "max-throughput":
                summary = await self._max_throughput()
            elif o.preset == "latency-slo":
                summary = await self._latency_slo()
            elif o.preset == "context":
                summary, extra = await self._context()
            elif o.preset == "quality":
                summary, extra = await self._quality()
            elif o.preset == "fixed":
                summary = await self._fixed()
            else:
                raise ValueError(f"unknown preset {o.preset!r}; choose one of {', '.join(PRESETS)}")
            if self.cancel.is_set():
                valid, reason = False, "cancelled"
        except Invalid as exc:
            valid, reason = False, str(exc)
        finally:
            tele = await self.telemetry.stop()
            if own_http:
                await self.target.http.aclose()
                self.target.http = None
        self.state = "completed" if valid else ("cancelled" if reason == "cancelled" else "invalid")
        self.message = summary.get("text") or reason or "done"
        self.result = self._doc(started, valid=valid, reason=reason, summary=summary, telemetry=tele, **extra)
        return self.result

    def _doc(self, started: float, *, valid: bool, reason: Optional[str], summary: dict,
             telemetry: Optional[dict] = None, **extra: Any) -> dict[str, Any]:
        from .. import __version__
        o = self.options
        return {"format": FORMAT, "id": self.id, "preset": o.preset, "alias": self.target.alias,
                "revision_id": self.revision, "max_model_len": self.max_model_len, "seed": self.seed,
                "options": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(o).items()},
                "points": self.points, "summary": summary, "valid": valid, "invalid_reason": reason,
                "telemetry": telemetry or {}, "client_version": __version__,
                "started_at": started, "finished_at": time.time(), **extra}

    def _workload(self, input_tokens: Optional[int] = None) -> Workload:
        o = self.options
        return Workload(input_tokens=input_tokens or o.input_tokens, output_tokens=o.output_tokens, cache=o.cache,
                        seed=self.seed)

    def _levels(self) -> list[int]:
        o, c, out = self.options, max(1, self.options.min_concurrency), []
        while c <= o.max_concurrency:
            out.append(c)
            c *= 2
        return out or [o.min_concurrency]

    async def _max_throughput(self) -> dict[str, Any]:
        """Double the load until another doubling no longer adds ``plateau`` (5 %) throughput or errors pile up.

        The answer is the knee: the lowest concurrency within ``plateau`` of the peak. Beyond it more load only
        adds latency."""
        o, last = self.options, None
        for c in self._levels():
            if self.cancel.is_set():
                break
            p = await self._point(self._workload(), c)
            if (p["error_rate"] or 0) > o.max_error_rate:
                break
            if last is not None and (p["output_tok_s"] or 0) < (last["output_tok_s"] or 0) * (1 + o.plateau):
                break                                          # more load no longer pays
            last = p
        ok = [p for p in self.points if (p["error_rate"] or 0) <= o.max_error_rate and p["output_tok_s"]]
        if not ok:
            return {"text": "no level completed"}
        peak = max(p["output_tok_s"] for p in ok)
        knee = next(p for p in ok if p["output_tok_s"] * (1 + o.plateau) >= peak)
        ttft = (knee.get("ttft_ms") or {}).get("p95")
        return {"peak_output_tok_s": peak, "at_concurrency": knee["concurrency"],
                "knee_output_tok_s": knee["output_tok_s"], "ttft_p95_ms": ttft,
                "tpot_p95_ms": (knee.get("tpot_ms") or {}).get("p95"),
                "text": f"peak {peak:,} output tok/s; {knee['output_tok_s']:,} already at concurrency "
                        f"{knee['concurrency']}" + (f" (TTFT p95 {ttft:,.0f} ms)" if ttft is not None else "")}

    async def _fixed(self) -> dict[str, Any]:
        for c in self.options.levels:
            if self.cancel.is_set():
                break
            await self._point(self._workload(), c)
        best = max(self.points, key=lambda p: p["output_tok_s"] or 0, default=None)
        return {"levels": [p["concurrency"] for p in self.points],
                "output_tok_s": {str(p["concurrency"]): p["output_tok_s"] for p in self.points},
                "text": ", ".join(f"c{p['concurrency']}: {p['output_tok_s']:,} tok/s" for p in self.points)
                + (f"; best at concurrency {best['concurrency']}" if best else "")}

    def _meets(self, p: dict) -> bool:
        o = self.options
        ttft, tpot = (p.get("ttft_ms") or {}).get("p95"), (p.get("tpot_ms") or {}).get("p95")
        return ((p["error_rate"] or 0) <= o.max_error_rate and ttft is not None and ttft <= o.slo_ttft_ms
                and (tpot is None or tpot <= o.slo_tpot_ms))

    async def _latency_slo(self) -> dict[str, Any]:
        o = self.options
        ok_level, ok_point, bad = None, None, None
        for c in self._levels():
            if self.cancel.is_set():
                break
            p = await self._point(self._workload(), c)
            if self._meets(p):
                ok_level, ok_point = c, p
            else:
                bad = c
                break
        while ok_level is not None and bad is not None and bad - ok_level > 1 and not self.cancel.is_set():
            mid = (ok_level + bad) // 2
            p = await self._point(self._workload(), mid)
            if self._meets(p):
                ok_level, ok_point = mid, p
            else:
                bad = mid
        target = f"TTFT p95 ≤ {o.slo_ttft_ms:.0f} ms, TPOT p95 ≤ {o.slo_tpot_ms:.0f} ms"
        if ok_point is None:
            return {"max_concurrency": 0, "text": f"no concurrency meets {target}"}
        return {"max_concurrency": ok_level, "output_tok_s": ok_point["output_tok_s"],
                "ttft_p95_ms": (ok_point.get("ttft_ms") or {}).get("p95"),
                "tpot_p95_ms": (ok_point.get("tpot_ms") or {}).get("p95"),
                "text": f"concurrency {ok_level} meets {target} ({ok_point['output_tok_s']:,} output tok/s)"
                        + ("" if bad is not None else f"; the ceiling {o.max_concurrency} was reached")}

    async def _context(self) -> tuple[dict[str, Any], dict[str, Any]]:
        o = self.options
        limit = self.max_model_len or max(o.contexts)
        ladder = [c for c in o.contexts if c + o.output_tokens + 64 <= limit] or [max(1024, limit // 2)]
        rows, recall = [], []
        for ctx in ladder:
            for conc in o.context_concurrency:
                if self.cancel.is_set():
                    break
                p = await self._point(self._workload(ctx - o.output_tokens - 64), conc)
                rows.append({"context": ctx, **p})
            if not self.cancel.is_set():
                recall.append(await needles(self.target, ctx - 64, self.prompts, o.needles))
                await self._guard()
        worst = min((r["score"] for r in recall), default=None)
        longest = max(ladder)
        return ({"contexts": ladder, "longest": longest, "needle_score_min": worst,
                 "text": f"{len(ladder)} context lengths up to {longest:,} tokens; retrieval "
                         f"{'—' if worst is None else f'{worst:.0%}'} at worst"},
                {"ladder": rows, "needles": recall})

    async def _quality(self) -> tuple[dict[str, Any], dict[str, Any]]:
        o = self.options
        ctx = min(self.max_model_len or 32768, 131072) - 256
        recall = await needles(self.target, ctx, self.prompts, o.needles)
        await self._guard()
        tools = await tool_check(self.target)
        passed = all(c["status"] != "fail" for c in tools)
        return ({"needle_score": recall["score"], "tools_ok": passed,
                 "text": f"retrieval {recall['found']}/{recall['needles']} at {recall['context_tokens']:,} tokens; "
                         f"tool call and continuation {'passed' if passed else 'FAILED'}"},
                {"needles": [recall], "checks": tools})


def summarize_for_record(result: dict[str, Any]) -> str:
    return (result.get("summary") or {}).get("text") or ("invalid: " + str(result.get("invalid_reason")))
