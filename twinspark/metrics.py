"""vLLM ``/metrics`` scraping and rate computation.

vLLM exposes Prometheus text where every sample carries labels
(``vllm:num_requests_running{engine="0",model_name="x"} 2``). Samples are summed
across label sets (engines / ranks), histogram ``_bucket`` series are skipped,
and both the v0 and v1 metric names are understood, as are SGLang's (``sglang:*``, with
``--enable-metrics``).

``MetricsSampler`` keeps a short history so the dashboard can show *rates*:
decode tok/s, prefill tok/s, requests/s, speculative-decoding acceptance.

For the controller's ``/metrics`` export (controller.exporter) a scrape also keeps the raw samples of the
families TwinSpark passes through, histogram buckets included, under ``twinspark:*`` names
(``EXPORT``). Between two scrapes the bucket deltas give windowed latency percentiles (``windowed``):
the p95 of the last interval, not an average since the engine started. ``MetricsHistory`` keeps minute
averages for 24 hours in the store, so the GUI's history survives a controller restart.
"""

from __future__ import annotations

import collections
import math
import re
import time
from typing import Any, Iterable, Optional

import httpx

# metric suffix (after the "vllm:" / "vllm_" prefix) -> friendly key
_METRICS = {
    "kv_cache_usage_perc": "kv-cache-usage",
    "gpu_cache_usage_perc": "kv-cache-usage",
    "num_requests_running": "requests-running",
    "num_requests_waiting": "requests-waiting",
    "num_preemptions_total": "preemptions-total",
    "time_to_first_token_seconds_sum": "ttft-sum-s",
    "time_to_first_token_seconds_count": "ttft-count",
    "time_to_first_tokens_seconds_sum": "ttft-sum-s",
    "time_to_first_tokens_seconds_count": "ttft-count",
    "e2e_request_latency_seconds_sum": "latency-sum-s",
    "e2e_request_latency_seconds_count": "latency-count",
    "inter_token_latency_seconds_sum": "itl-sum-s",
    "inter_token_latency_seconds_count": "itl-count",
    "time_per_output_token_seconds_sum": "itl-sum-s",
    "time_per_output_token_seconds_count": "itl-count",
    "generation_tokens_total": "gen-tokens-total",
    "prompt_tokens_total": "prompt-tokens-total",
    "request_success_total": "requests-total",
    "prefix_cache_hits_total": "prefix-hits-total",
    "prefix_cache_queries_total": "prefix-queries-total",
    "gpu_prefix_cache_hits_total": "prefix-hits-total",
    "gpu_prefix_cache_queries_total": "prefix-queries-total",
    "spec_decode_num_drafts_total": "spec-drafts-total",
    "spec_decode_num_draft_tokens_total": "spec-draft-tokens-total",
    "spec_decode_num_accepted_tokens_total": "spec-accepted-tokens-total",
    # SGLang
    "num_running_reqs": "requests-running",
    "num_queue_reqs": "requests-waiting",
    "token_usage": "kv-cache-usage",
    "cache_hit_rate": "prefix-hit-rate",
    "num_requests_total": "requests-total",
}
_SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eEinfINFNa]+)(\s+\d+)?\s*$")
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')

# vLLM families passed through on the controller's /metrics: suffix -> (TwinSpark name, type, help)
EXPORT: dict[str, tuple[str, str, str]] = {
    "num_requests_running": ("twinspark:requests_running", "gauge", "Requests the engine is processing"),
    "num_requests_waiting": ("twinspark:requests_waiting", "gauge", "Requests waiting for the engine"),
    "kv_cache_usage_perc": ("twinspark:kv_cache_usage_ratio", "gauge", "Share of the KV cache in use (0-1)"),
    "gpu_cache_usage_perc": ("twinspark:kv_cache_usage_ratio", "gauge", "Share of the KV cache in use (0-1)"),
    "num_preemptions_total": ("twinspark:preemptions_total", "counter", "Requests preempted for lack of KV cache"),
    "prompt_tokens_total": ("twinspark:prompt_tokens_total", "counter", "Prompt tokens processed"),
    "generation_tokens_total": ("twinspark:generation_tokens_total", "counter", "Tokens generated"),
    "request_success_total": ("twinspark:requests_finished_total", "counter", "Requests finished, by reason"),
    "prefix_cache_hits_total": ("twinspark:prefix_cache_hits_total", "counter", "Prefix cache hits (tokens)"),
    "gpu_prefix_cache_hits_total": ("twinspark:prefix_cache_hits_total", "counter", "Prefix cache hits (tokens)"),
    "prefix_cache_queries_total": ("twinspark:prefix_cache_queries_total", "counter",
                                   "Prefix cache queries (tokens)"),
    "gpu_prefix_cache_queries_total": ("twinspark:prefix_cache_queries_total", "counter",
                                       "Prefix cache queries (tokens)"),
    "spec_decode_num_drafts_total": ("twinspark:spec_decode_drafts_total", "counter", "Speculative drafts"),
    "spec_decode_num_draft_tokens_total": ("twinspark:spec_decode_draft_tokens_total", "counter",
                                           "Speculative draft tokens"),
    "spec_decode_num_accepted_tokens_total": ("twinspark:spec_decode_accepted_tokens_total", "counter",
                                              "Accepted speculative tokens"),
    "time_to_first_token_seconds": ("twinspark:time_to_first_token_seconds", "histogram",
                                    "Time to the first generated token"),
    "inter_token_latency_seconds": ("twinspark:inter_token_latency_seconds", "histogram",
                                    "Time between generated tokens"),
    "time_per_output_token_seconds": ("twinspark:inter_token_latency_seconds", "histogram",
                                      "Time between generated tokens"),
    "e2e_request_latency_seconds": ("twinspark:e2e_request_latency_seconds", "histogram",
                                    "Request latency from arrival to the last token"),
    "request_queue_time_seconds": ("twinspark:request_queue_time_seconds", "histogram",
                                   "Time a request waited before the engine took it"),
    "request_prefill_time_seconds": ("twinspark:request_prefill_time_seconds", "histogram", "Prefill time"),
    "request_decode_time_seconds": ("twinspark:request_decode_time_seconds", "histogram", "Decode time"),
    # SGLang's names for the same things
    "num_running_reqs": ("twinspark:requests_running", "gauge", "Requests the engine is processing"),
    "num_queue_reqs": ("twinspark:requests_waiting", "gauge", "Requests waiting for the engine"),
    "token_usage": ("twinspark:kv_cache_usage_ratio", "gauge", "Share of the KV cache in use (0-1)"),
    "cache_hit_rate": ("twinspark:prefix_cache_hit_ratio", "gauge", "Prefix cache hit rate (SGLang, 0-1)"),
    "num_requests_total": ("twinspark:requests_finished_total", "counter", "Requests finished, by reason"),
}
_HIST_PARTS = ("_bucket", "_sum", "_count")
# windowed percentiles the dashboard and the history show: key prefix -> TwinSpark histogram
WINDOWED = {"ttft": "twinspark:time_to_first_token_seconds", "itl": "twinspark:inter_token_latency_seconds",
            "e2e": "twinspark:e2e_request_latency_seconds"}


def _suffix(name: str) -> str:
    for pre in ("vllm:", "vllm_", "sglang:", "sglang_"):
        if name.startswith(pre):
            return name[len(pre):]
    return name


_AVERAGED = {"kv-cache-usage", "prefix-hit-rate"}      # fractions reported once per engine/rank


def _parse_text(text: str) -> dict[str, float]:
    found: dict[str, float] = {}
    counts: dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        if name.endswith("_bucket"):
            continue
        tag = _METRICS.get(_suffix(name))
        if tag is None:
            continue
        try:
            num = float(m.group(3))
        except ValueError:
            continue
        if num != num:            # NaN
            continue
        found[tag] = found.get(tag, 0.0) + num
        counts[tag] = counts.get(tag, 0) + 1
    for tag in _AVERAGED & set(found):
        found[tag] = found[tag] / counts[tag]
    return found


def _labels(text: Optional[str]) -> dict[str, str]:
    if not text:
        return {}
    return {k: v.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
            for k, v in _LABEL_RE.findall(text[1:-1])}


def parse_samples(text: str) -> list[tuple[str, str, dict[str, str], float]]:
    """The samples of the ``EXPORT`` families: ``(twinspark name, part, labels, value)``; ``part`` is
    ``_bucket`` / ``_sum`` / ``_count`` for histograms, "" otherwise. vLLM's ``model_name`` label is kept."""
    out: list[tuple[str, str, dict[str, str], float]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        name, part = _suffix(m.group(1)), ""
        target = EXPORT.get(name)
        if target is None:
            for suffix in _HIST_PARTS:
                if name.endswith(suffix) and EXPORT.get(name[:-len(suffix)], ("", ""))[1] == "histogram":
                    target, part = EXPORT[name[:-len(suffix)]], suffix
                    break
        if target is None or (target[1] == "histogram" and not part):
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        if value != value:
            continue
        out.append((target[0], part, _labels(m.group(2)), value))
    return out


def _buckets(samples: Iterable[tuple], family: str) -> dict[float, float]:
    """Cumulative counts per upper bound of one histogram, summed over its label sets (engines, ranks)."""
    out: dict[float, float] = {}
    for name, part, labels, value in samples:
        if name == family and part == "_bucket" and "le" in labels:
            try:
                le = float(labels["le"])
            except ValueError:
                continue
            out[le] = out.get(le, 0.0) + value
    return out


def quantile(q: float, buckets: dict[float, float]) -> Optional[float]:
    """``histogram_quantile`` as Prometheus computes it (linear within the bucket)."""
    items = sorted(buckets.items())
    if not items or items[-1][1] <= 0:
        return None
    rank = q * items[-1][1]
    prev_le, prev_c = 0.0, 0.0
    for le, c in items:
        if c >= rank:
            if math.isinf(le):
                return prev_le
            if c == prev_c:
                return le
            return prev_le + (le - prev_le) * (rank - prev_c) / (c - prev_c)
        prev_le, prev_c = le, c
    return prev_le


def windowed(pairs: Iterable[tuple[Optional[list], list]]) -> dict[str, Optional[float]]:
    """p50/p95/p99 (ms) of the requests that finished between two scrapes, over several backends.

    ``pairs`` is ``(previous samples or None, current samples)`` per backend. A counter that went down (an
    engine restarted) counts from zero."""
    merged: dict[str, dict[float, float]] = {k: {} for k in WINDOWED}
    for prev, cur in pairs:
        if prev is None:
            continue                    # the first scrape of a backend opens the window
        for key, family in WINDOWED.items():
            now = _buckets(cur, family)
            before = _buckets(prev or [], family)
            reset = any(now.get(le, 0.0) < c for le, c in before.items())
            for le, c in now.items():
                delta = c if reset else c - before.get(le, 0.0)
                merged[key][le] = merged[key].get(le, 0.0) + max(0.0, delta)
    out: dict[str, Optional[float]] = {}
    for key, buckets in merged.items():
        for q in (50, 95, 99):
            v = quantile(q / 100, buckets)
            out[f"{key}_p{q}_ms"] = None if v is None else round(v * 1000, 1)
    return out


def derive(values: dict[str, float]) -> dict[str, Optional[float]]:
    out: dict[str, Optional[float]] = {"kv_cache_usage_pct": None, "avg_ttft_ms": None,
                                       "avg_e2e_latency_ms": None, "avg_itl_ms": None,
                                       "prefix_hit_rate_pct": None, "spec_mean_accept_len": None,
                                       "spec_accept_rate_pct": None}
    kv = values.get("kv-cache-usage")
    out["kv_cache_usage_pct"] = round(kv * 100, 1) if kv is not None else None
    for key, s, c in (("avg_ttft_ms", "ttft-sum-s", "ttft-count"),
                      ("avg_e2e_latency_ms", "latency-sum-s", "latency-count"),
                      ("avg_itl_ms", "itl-sum-s", "itl-count")):
        sv, cv = values.get(s), values.get(c)
        out[key] = round(sv / cv * 1000, 1) if sv is not None and cv else None
    hits, q = values.get("prefix-hits-total"), values.get("prefix-queries-total")
    out["prefix_hit_rate_pct"] = round(100 * hits / q, 1) if hits is not None and q else None
    if out["prefix_hit_rate_pct"] is None and values.get("prefix-hit-rate") is not None:
        out["prefix_hit_rate_pct"] = round(100 * values["prefix-hit-rate"], 1)      # SGLang reports the ratio
    drafts = values.get("spec-drafts-total")
    acc = values.get("spec-accepted-tokens-total")
    dtok = values.get("spec-draft-tokens-total")
    if drafts and acc is not None:
        out["spec_mean_accept_len"] = round(1 + acc / drafts, 2)
    if dtok and acc is not None:
        out["spec_accept_rate_pct"] = round(100 * acc / dtok, 1)
    return out


async def scrape_metrics(base_url: str, *, timeout: float = 5.0,
                         client: Optional[httpx.AsyncClient] = None,
                         headers: Optional[dict] = None, raw: bool = False) -> dict:
    """Fetch and parse ``<base_url>/metrics``. Never raises for network errors. ``raw`` also returns the
    ``EXPORT`` samples under ``"samples"`` (for /metrics and the windowed percentiles)."""
    url = base_url.rstrip("/") + "/metrics"
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        try:
            r = await client.get(url, headers=headers)
            if r.status_code == 404:
                return {"base_url": base_url, "ok": False, "error": "no /metrics endpoint"}
            r.raise_for_status()
            values = _parse_text(r.text)
            snap = {"base_url": base_url, "ok": True, "error": None, "ts": time.time(), **values, **derive(values)}
            if raw:
                snap["samples"] = parse_samples(r.text)
            return snap
        except httpx.HTTPError as exc:
            return {"base_url": base_url, "ok": False, "error": str(exc) or type(exc).__name__}
    finally:
        if own:
            await client.aclose()


_RATE_KEYS = {"gen-tokens-total": "decode_tok_s", "prompt-tokens-total": "prefill_tok_s",
              "requests-total": "requests_per_s"}


class MetricsSampler:
    """Ring buffer of scrapes + per-interval rates for the dashboard."""

    def __init__(self, maxlen: int = 360):
        self.samples: collections.deque[dict] = collections.deque(maxlen=maxlen)
        self.revision: Optional[str] = None

    def add(self, snap: dict, revision: Optional[str]) -> dict:
        if revision != self.revision:
            self.samples.clear()
            self.revision = revision
        if not snap.get("ok"):
            return snap
        prev = self.samples[-1] if self.samples else None
        point = dict(snap)
        if prev:
            dt = max(1e-3, snap["ts"] - prev["ts"])
            for key, rate in _RATE_KEYS.items():
                a, b = prev.get(key), snap.get(key)
                if a is not None and b is not None and b >= a:
                    point[rate] = round((b - a) / dt, 1)
            da, dd = (snap.get("spec-accepted-tokens-total", 0) - prev.get("spec-accepted-tokens-total", 0),
                      snap.get("spec-drafts-total", 0) - prev.get("spec-drafts-total", 0))
            if dd > 0:
                point["spec_accept_len_now"] = round(1 + da / dd, 2)
        self.samples.append(point)
        return point

    def latest(self) -> Optional[dict]:
        return self.samples[-1] if self.samples else None

    def series(self, keys: tuple[str, ...] = ("decode_tok_s", "prefill_tok_s", "kv_cache_usage_pct",
                                               "requests-running", "requests-waiting", "ttft_p95_ms")) -> dict:
        return {"ts": [s["ts"] for s in self.samples],
                **{k: [s.get(k) for s in self.samples] for k in keys}}


def combine_snapshots(snaps: list[dict]) -> dict:
    """Merge scrapes of several backends (replicated topology) into one view."""
    ok = [s for s in snaps if s.get("ok")]
    if not ok:
        return snaps[0] if snaps else {"ok": False, "error": "no backends"}
    if len(ok) == 1:
        return ok[0]
    merged: dict = {"ok": True, "error": None, "ts": max(s["ts"] for s in ok),
                    "base_url": ", ".join(s["base_url"] for s in ok)}
    tags = {k for s in ok for k in s if k in set(_METRICS.values())}
    values = {}
    for k in tags:
        nums = [s[k] for s in ok if s.get(k) is not None]
        values[k] = (sum(nums) / len(nums)) if k in _AVERAGED else sum(nums)
    merged.update(values)
    merged.update(derive(values))
    return merged


# ---- 24 hours of minute averages, kept across restarts ------------------------------------------------
HISTORY_KEYS = ("decode_tok_s", "prefill_tok_s", "kv_cache_usage_pct", "prefix_hit_rate_pct", "ttft_p95_ms",
                "itl_p95_ms", "requests-running", "requests-waiting")
NODE_KEYS = ("mem_available_gib", "swap_used_gib", "psi_some_avg10", "gpu_power_w")


class MetricsHistory:
    """Minute averages of the dashboard's numbers (``HISTORY_KEYS``) and each node's memory, swap, memory
    pressure and GPU power (``"<node>.<key>"``), for the last ``minutes``. Kept in the store every
    ``persist_every`` minutes and on shutdown, so a controller restart keeps the history (bounded)."""

    STORE_KEY = "metrics_history"

    def __init__(self, store: Any = None, minutes: int = 1440, persist_every: int = 10):
        self.store, self.persist_every = store, persist_every
        self.points: collections.deque[dict] = collections.deque(maxlen=minutes)
        self._open: Optional[dict] = None
        self._since_persist = 0
        saved = (store.kv_get(self.STORE_KEY) if store is not None else None) or {}
        for pt in saved.get("points") or []:
            if isinstance(pt, dict) and isinstance(pt.get("t"), (int, float)):
                self.points.append(pt)

    def add(self, values: dict[str, Optional[float]], ts: Optional[float] = None) -> None:
        ts = time.time() if ts is None else ts
        minute = int(ts // 60) * 60
        if self._open is not None and self._open["t"] != minute:
            self._close()
        if self._open is None:
            self._open = {"t": minute, "sums": {}, "counts": {}}
        for k, v in values.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                self._open["sums"][k] = self._open["sums"].get(k, 0.0) + v
                self._open["counts"][k] = self._open["counts"].get(k, 0) + 1

    def _close(self) -> None:
        o, self._open = self._open, None
        if not o or not o["counts"]:
            return
        self.points.append({"t": o["t"], **{k: round(o["sums"][k] / n, 2) for k, n in o["counts"].items()}})
        self._since_persist += 1
        if self._since_persist >= self.persist_every:
            self.persist()

    def flush(self) -> None:
        """Close the running minute and keep everything (controller shutdown)."""
        self._close()
        self.persist()

    def persist(self) -> None:
        self._since_persist = 0
        if self.store is not None:
            try:
                self.store.kv_set(self.STORE_KEY, {"points": list(self.points)})
            except Exception:  # noqa: BLE001 - history is a convenience: never break the metrics loop
                pass

    def series(self, hours: float = 24.0, now: Optional[float] = None) -> dict[str, list]:
        now = time.time() if now is None else now
        pts = [p for p in self.points if p["t"] >= now - hours * 3600]
        keys = sorted({k for p in pts for k in p if k != "t"})
        return {"ts": [p["t"] for p in pts], **{k: [p.get(k) for p in pts] for k in keys}}
