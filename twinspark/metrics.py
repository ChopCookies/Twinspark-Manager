"""vLLM ``/metrics`` scraping and rate computation.

vLLM exposes Prometheus text where every sample carries labels
(``vllm:num_requests_running{engine="0",model_name="x"} 2``). Samples are summed
across label sets (engines / ranks), histogram ``_bucket`` series are skipped,
and both the v0 and v1 metric names are understood.

``MetricsSampler`` keeps a short history so the dashboard can show *rates*:
decode tok/s, prefill tok/s, requests/s, speculative-decoding acceptance.
"""

from __future__ import annotations

import collections
import re
import time
from typing import Optional

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
}
_SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eEinfINFNa]+)(\s+\d+)?\s*$")


def _suffix(name: str) -> str:
    for pre in ("vllm:", "vllm_"):
        if name.startswith(pre):
            return name[len(pre):]
    return name


_AVERAGED = {"kv-cache-usage"}      # fractions reported once per engine/rank


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
                         headers: Optional[dict] = None) -> dict:
    """Fetch and parse ``<base_url>/metrics``. Never raises for network errors."""
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
            return {"base_url": base_url, "ok": True, "error": None, "ts": time.time(),
                    **values, **derive(values)}
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
                                               "requests-running", "requests-waiting")) -> dict:
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
