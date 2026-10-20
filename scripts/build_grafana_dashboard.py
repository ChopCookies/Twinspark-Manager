#!/usr/bin/env python3
"""Write deploy/observability/grafana/dashboards/twinspark.json (the Grafana dashboard for /metrics).

The panels are listed here once; run this after changing them. The JSON is committed so the compose
setup works without Python."""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "deploy/observability/grafana/dashboards/twinspark.json"
DS = {"type": "prometheus", "uid": "twinspark-prometheus"}

# (title, unit, [(expr, legend)], width)
ROWS = [
    ("Serving", [
        ("Decode tokens/s", "short",
         [("sum by (alias) (rate(twinspark:generation_tokens_total[1m]))", "{{alias}}")], 8),
        ("Prefill tokens/s", "short", [("sum by (alias) (rate(twinspark:prompt_tokens_total[1m]))", "{{alias}}")], 8),
        ("Requests running / waiting", "short",
         [("sum by (alias) (twinspark:requests_running)", "{{alias}} running"),
          ("sum by (alias) (twinspark:requests_waiting)", "{{alias}} waiting")], 8),
        ("TTFT p50 / p95", "s", [
            ("histogram_quantile(0.5, sum by (le, alias) (rate(twinspark:time_to_first_token_seconds_bucket[5m])))",
             "{{alias}} p50"),
            ("histogram_quantile(0.95, sum by (le, alias) (rate(twinspark:time_to_first_token_seconds_bucket[5m])))",
             "{{alias}} p95")], 8),
        ("Inter-token latency p50 / p95", "s", [
            ("histogram_quantile(0.5, sum by (le, alias) (rate(twinspark:inter_token_latency_seconds_bucket[5m])))",
             "{{alias}} p50"),
            ("histogram_quantile(0.95, sum by (le, alias) (rate(twinspark:inter_token_latency_seconds_bucket[5m])))",
             "{{alias}} p95")], 8),
        ("KV cache in use", "percentunit", [("max by (alias) (twinspark:kv_cache_usage_ratio)", "{{alias}}")], 8),
        ("Prefix cache hit rate", "percentunit", [
            ("sum by (alias) (rate(twinspark:prefix_cache_hits_total[5m])) / "
             "sum by (alias) (rate(twinspark:prefix_cache_queries_total[5m]))", "{{alias}}")], 8),
        ("Speculative acceptance (tokens per draft)", "short", [
            ("1 + sum by (alias) (rate(twinspark:spec_decode_accepted_tokens_total[5m])) / "
             "sum by (alias) (rate(twinspark:spec_decode_drafts_total[5m]))", "{{alias}}")], 8),
        ("Preemptions/s", "short", [("sum by (alias) (rate(twinspark:preemptions_total[5m]))", "{{alias}}")], 8),
    ]),
    ("Nodes", [
        ("MemAvailable", "bytes", [("twinspark_node_memory_available_bytes", "node {{node}}")], 8),
        ("Swap in use", "bytes", [("twinspark_node_swap_used_bytes", "node {{node}}")], 8),
        ("Memory pressure (PSI avg10)", "percent", [
            ("twinspark_node_memory_pressure_avg10_percent", "node {{node}} {{kind}}")], 8),
        ("GPU power (sensor)", "watt", [("twinspark_node_gpu_power_watts", "node {{node}}")], 8),
        ("GPU utilization", "percent", [("twinspark_node_gpu_utilization_percent", "node {{node}}")], 8),
        ("Page cache", "bytes", [("twinspark_node_page_cache_bytes", "node {{node}}")], 8),
    ]),
    ("Gateway and switches", [
        ("Requests in flight", "short", [("twinspark_gateway_inflight_requests", "{{alias}}")], 8),
        ("Gateway errors/s", "short", [("rate(twinspark_gateway_errors_total[5m])", "{{alias}}")], 8),
        ("Switching / route up", "short", [("twinspark_switch_in_progress", "switch in progress"),
                                           ("twinspark_gateway_route_up", "{{alias}} up")], 8),
    ]),
]


def panel(pid: int, title: str, unit: str, targets: list[tuple[str, str]], x: int, y: int, w: int) -> dict:
    return {"id": pid, "type": "timeseries", "title": title, "datasource": DS,
            "gridPos": {"h": 8, "w": w, "x": x, "y": y},
            "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 8}}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
            "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": legend}
                        for i, (e, legend) in enumerate(targets)]}


def build() -> dict:
    panels, pid, y = [], 1, 0
    for row_title, items in ROWS:
        panels.append({"id": pid, "type": "row", "title": row_title, "collapsed": False,
                       "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []})
        pid, y, x = pid + 1, y + 1, 0
        for title, unit, targets, w in items:
            if x + w > 24:
                x, y = 0, y + 8
            panels.append(panel(pid, title, unit, targets, x, y, w))
            pid, x = pid + 1, x + w
        y += 8
    return {"uid": "twinspark", "title": "TwinSpark", "tags": ["twinspark", "dgx-spark", "vllm"],
            "timezone": "browser", "schemaVersion": 39, "version": 1, "refresh": "30s",
            "time": {"from": "now-6h", "to": "now"}, "editable": True, "panels": panels,
            "description": "Two DGX Sparks through TwinSpark Manager's /metrics (docs/metrics.md)"}


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
