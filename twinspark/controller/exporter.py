"""The controller's Prometheus export (master plan §2.3): one scrape config for the whole cluster.

``GET /metrics`` on node A's management API (api.routes_metrics) renders what the controller already
collects; nothing extra runs on the Sparks and nothing is scraped on demand:

* ``twinspark:*`` — the engine's own families from each backend's last scrape (``metrics.EXPORT``),
  histogram buckets included, labelled with ``alias``, ``node``, ``model`` and ``revision``. vLLM's
  ``model_name`` becomes ``model``; ``engine``, ``finished_reason`` and ``le`` are kept.
* ``twinspark_node_*`` — each node's memory, swap, memory pressure (PSI avg10), page cache, CPU and GPU.
* ``twinspark_gateway_*`` — routes: up, in-flight, requests and errors per alias.
* ``twinspark_active_info``, ``twinspark_switch_in_progress``, ``twinspark_half_serving`` — what is deployed
  and whether a switch is running.
* ``twinspark_window_*`` — values over the controller's last scrape interval (decode/prefill tokens/s,
  TTFT/ITL percentiles from the histogram deltas). They are windowed, not averages since the engine
  started: Prometheus can compute its own rates from the counters; these match the GUI.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any, Optional

from .. import __version__
from ..metrics import EXPORT

if TYPE_CHECKING:
    from .controller import Controller

_GIB = 1024 ** 3
_KEEP_ENGINE_LABELS = ("engine", "finished_reason", "le")


def _esc(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels: dict[str, Any]) -> str:
    items = [(k, v) for k, v in labels.items() if v is not None and v != ""]
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in items) + "}" if items else ""


def _num(v: Any) -> Optional[str]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return repr(float(v)) if isinstance(v, float) else str(v)


class _Families:
    def __init__(self) -> None:
        self.meta: dict[str, tuple[str, str]] = {}
        self.lines: dict[str, list[str]] = {}

    def add(self, family: str, kind: str, help_: str, value: Any, labels: Optional[dict] = None,
            sample: Optional[str] = None) -> None:
        text = _num(value)
        if text is None:
            return
        self.meta.setdefault(family, (kind, help_))
        self.lines.setdefault(family, []).append(f"{sample or family}{_labels(labels or {})} {text}")

    def render(self) -> str:
        out = []
        for family, (kind, help_) in self.meta.items():
            out.append(f"# HELP {family} {help_}")
            out.append(f"# TYPE {family} {kind}")
            out.extend(self.lines[family])
        return "\n".join(out) + "\n"


def render(ctrl: "Controller") -> str:
    f = _Families()
    f.add("twinspark_build_info", "gauge", "TwinSpark Manager version on node A", 1, {"version": __version__})
    _nodes(ctrl, f)
    _deployment(ctrl, f)
    _gateway(ctrl, f)
    _engines(ctrl, f)
    _windows(ctrl, f)
    return f.render()


def _nodes(ctrl: "Controller", f: _Families) -> None:
    for node in sorted(ctrl.config.nodes):
        t = ctrl.telemetry.get(node) or {}
        up = bool(t) and not t.get("stale") and not t.get("error")
        f.add("twinspark_node_up", "gauge", "The node's agent answered the last telemetry poll", int(up),
              {"node": node})
        if not up:
            continue
        lab = {"node": node}
        for key, family, help_ in (
                ("mem_total_gib", "twinspark_node_memory_total_bytes", "Total memory (shared CPU/GPU)"),
                ("mem_available_gib", "twinspark_node_memory_available_bytes", "MemAvailable"),
                ("mem_free_gib", "twinspark_node_memory_free_bytes", "MemFree"),
                ("page_cache_gib", "twinspark_node_page_cache_bytes", "Page cache and buffers"),
                ("swap_used_gib", "twinspark_node_swap_used_bytes", "Swap in use")):
            v = t.get(key)
            f.add(family, "gauge", help_, v * _GIB if isinstance(v, (int, float)) else None, lab)
        for kind in ("some", "full"):
            f.add("twinspark_node_memory_pressure_avg10_percent", "gauge",
                  "Memory pressure stall (PSI avg10, percent of time)", t.get(f"psi_{kind}_avg10"),
                  {**lab, "kind": kind})
        f.add("twinspark_node_cpu_utilization_percent", "gauge", "CPU utilization", t.get("cpu_pct"), lab)
        gpu = t.get("gpu") or {}
        f.add("twinspark_node_gpu_power_watts", "gauge", "GPU power (GPU sensor, not the wall)",
              gpu.get("power_w"), lab)
        f.add("twinspark_node_gpu_utilization_percent", "gauge", "GPU utilization", gpu.get("utilization_pct"), lab)
        f.add("twinspark_node_gpu_temperature_celsius", "gauge", "GPU temperature", gpu.get("temperature_c"), lab)


def _deployment(ctrl: "Controller", f: _Families) -> None:
    act = ctrl.active()
    f.add("twinspark_switch_in_progress", "gauge", "A switch, stop or maintenance holds the cluster", int(ctrl.busy()))
    if act:
        f.add("twinspark_active_info", "gauge", "The active deployment", 1,
              {"profile": act.get("profile"), "revision": act.get("revision_id"), "label": act.get("label")})
    for h in ctrl.halves_status() or []:
        f.add("twinspark_half_serving", "gauge", "A split deployment's half on this node is serving",
              int(h["state"] == "serving"), {"node": h["node"], "name": h["name"], "revision": h["revision_id"]})


def _gateway(ctrl: "Controller", f: _Families) -> None:
    for st in ctrl.gateway.routes.values():
        lab = {"alias": st.alias, "model": st.served_model, "revision": st.revision_id}
        f.add("twinspark_gateway_route_up", "gauge", "The alias routes to a backend and is not down or switching",
              int(st.status == "serving"), lab)
        f.add("twinspark_gateway_inflight_requests", "gauge", "Requests in flight through the gateway",
              st.inflight, {"alias": st.alias})
        f.add("twinspark_gateway_oldest_inflight_seconds", "gauge", "Age of the oldest request in flight",
              st.oldest_inflight_s(), {"alias": st.alias})
        f.add("twinspark_gateway_requests_total", "counter", "Requests through the gateway (since the controller "
              "started)", st.requests, {"alias": st.alias})
        f.add("twinspark_gateway_errors_total", "counter", "Failed requests through the gateway", st.errors,
              {"alias": st.alias})


def _engines(ctrl: "Controller", f: _Families) -> None:
    kinds = {name: (kind, help_) for name, kind, help_ in EXPORT.values()}
    now = time.time()
    for backend in sorted(ctrl.backend_samples):
        rec = ctrl.backend_samples[backend]
        ours = rec.get("labels") or {}
        f.add("twinspark_scrape_age_seconds", "gauge", "Seconds since the controller scraped this backend",
              round(now - rec.get("ts", now), 1), {"alias": ours.get("alias"), "node": ours.get("node")})
        for name, part, labels, value in rec.get("samples") or []:
            kind, help_ = kinds.get(name, ("untyped", ""))
            extra = {k: labels[k] for k in _KEEP_ENGINE_LABELS if k in labels}
            f.add(name, kind, help_, value, {**ours, **extra}, sample=name + part)


def _windows(ctrl: "Controller", f: _Families) -> None:
    groups = [({"scope": "deployment"}, ctrl.sampler.latest())]
    groups += [({"scope": "half", "node": n}, s.latest()) for n, s in sorted(ctrl.half_samplers.items())]
    for lab, point in groups:
        if not point or not point.get("ok"):
            continue
        for key, family, what in (("decode_tok_s", "twinspark_window_decode_tokens_per_second", "Generated tokens/s"),
                                  ("prefill_tok_s", "twinspark_window_prefill_tokens_per_second", "Prompt tokens/s"),
                                  ("requests_per_s", "twinspark_window_requests_per_second", "Finished requests/s")):
            f.add(family, "gauge", f"{what} over the controller's last scrape interval", point.get(key), lab)
        for metric, family in (("ttft", "twinspark_window_ttft_seconds"), ("itl", "twinspark_window_itl_seconds")):
            for q in (50, 95, 99):
                v = point.get(f"{metric}_p{q}_ms")
                f.add(family, "gauge", f"{metric.upper()} percentile of the requests of the last interval",
                      None if v is None else v / 1000, {**lab, "quantile": str(q / 100)})
