"""The controller's Prometheus export, windowed percentiles and the 24 h history (master plan §2.3)."""

from __future__ import annotations

import re

import httpx
import pytest

from twinspark import metrics
from twinspark.controller import controller as controller_mod
from twinspark.metrics import MetricsHistory, parse_samples, quantile, windowed
from twinspark.security import SecretsVault

from .test_split_preparation import split_draft


def vllm_text(ttft_counts: tuple[int, int, int], gen: int, model: str = "m") -> str:
    a, b, c = ttft_counts
    return f"""# HELP vllm:time_to_first_token_seconds Histogram of time to first token in seconds.
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_bucket{{engine="0",le="0.1",model_name="{model}"}} {a}
vllm:time_to_first_token_seconds_bucket{{engine="0",le="0.5",model_name="{model}"}} {b}
vllm:time_to_first_token_seconds_bucket{{engine="0",le="+Inf",model_name="{model}"}} {c}
vllm:time_to_first_token_seconds_sum{{engine="0",model_name="{model}"}} 1.0
vllm:time_to_first_token_seconds_count{{engine="0",model_name="{model}"}} {c}
vllm:generation_tokens_total{{engine="0",model_name="{model}"}} {gen}
vllm:kv_cache_usage_perc{{engine="0",model_name="{model}"}} 0.25
vllm:num_requests_running{{engine="0",model_name="{model}"}} 2
vllm:request_success_total{{engine="0",finished_reason="stop",model_name="{model}"}} 7
"""


def test_window_percentiles_come_from_bucket_deltas():
    before = parse_samples(vllm_text((10, 10, 10), 0))
    after = parse_samples(vllm_text((10, 18, 20), 0))       # the last 10 requests: none under 0.1 s
    w = windowed([(before, after)])
    assert w["ttft_p50_ms"] == pytest.approx(350.0) and w["ttft_p95_ms"] == 500.0
    assert windowed([(None, after)])["ttft_p95_ms"] is None             # the first scrape opens the window
    restarted = parse_samples(vllm_text((1, 2, 2), 0))                 # the engine restarted: count from zero
    assert windowed([(after, restarted)])["ttft_p50_ms"] == 100.0
    assert quantile(0.5, {}) is None


def test_history_keeps_minute_averages_across_a_restart_and_stays_bounded(tmp_path):
    from twinspark.controller.store import Store
    store = Store(str(tmp_path / "s.db"))
    h = MetricsHistory(store, minutes=3, persist_every=1)
    for minute in range(5):
        h.add({"decode_tok_s": 10.0 * minute, "A.mem_available_gib": 50.0}, ts=minute * 60 + 1)
        h.add({"decode_tok_s": 10.0 * minute + 2, "bad": float("nan"), "flag": True}, ts=minute * 60 + 30)
    h.flush()
    again = MetricsHistory(store, minutes=3)
    s = again.series(hours=24, now=5 * 60)
    assert s["ts"] == [120, 180, 240] and s["decode_tok_s"] == [21.0, 31.0, 41.0]
    assert s["A.mem_available_gib"] == [50.0, 50.0, 50.0] and "bad" not in s and "flag" not in s


@pytest.fixture
def fake_scrapes(monkeypatch):
    """Every backend answers with the next text in ``texts[url]`` (the last one repeats)."""
    texts: dict[str, list[str]] = {}

    async def scrape(url, *, headers=None, raw=False, **kw):
        seq = texts.get(url) or [vllm_text((0, 0, 0), 0)]
        text = seq.pop(0) if len(seq) > 1 else seq[0]
        values = metrics._parse_text(text)
        snap = {"base_url": url, "ok": True, "error": None, "ts": metrics.time.time(), **values,
                **metrics.derive(values)}
        if raw:
            snap["samples"] = parse_samples(text)
        return snap
    monkeypatch.setattr(controller_mod, "scrape_metrics", scrape)
    return texts


async def test_metrics_carry_alias_node_and_revision_per_split_half(cluster, fake_scrapes, tmp_path):
    from twinspark.controller.app import create_app
    c = cluster.controller
    c.create_profile(split_draft())
    job = await cluster.wait_job((await c.activate("duo")).job_id)
    assert job.state.value == "completed"
    chat, code = c.gateway.routes["chat"].backends[0], c.gateway.routes["code"].backends[0]
    fake_scrapes[chat] = [vllm_text((10, 10, 10), 100, "duo-a"), vllm_text((10, 18, 20), 400, "duo-a")]
    fake_scrapes[code] = [vllm_text((0, 0, 0), 0, "duo-b")]
    c.telemetry = {"A": {"mem_total_gib": 121.0, "mem_available_gib": 60.5, "swap_used_gib": 0.0,
                         "psi_some_avg10": 1.5, "gpu": {"power_w": 88.0}},
                   "B": {"error": "agent unreachable", "stale": True}}
    await c.metrics_tick(refresh_nodes=False)
    await c.metrics_tick(refresh_nodes=False)
    assert c.half_samplers["A"].latest()["ttft_p95_ms"] == 500.0
    assert c.status()["halves"][0]["metrics"]["ttft_p95_ms"] == 500.0
    vault = SecretsVault(c.config.secrets_dir)
    key = vault.ensure("metrics_api_key")[0]
    app = create_app(c, management_key="mgmt", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m") as api:
        assert (await api.get("/metrics")).status_code == 401
        assert (await api.get("/metrics", headers={"authorization": "Bearer wrong"})).status_code == 401
        assert (await api.get("/api/v1/status", headers={"authorization": f"Bearer {key}"})).status_code == 401
        r = await api.get("/metrics", headers={"authorization": f"Bearer {key}"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert (await api.get("/metrics", headers={"x-api-key": "mgmt"})).status_code == 200
        hist = (await api.get("/api/v1/system/metrics/history", headers={"x-api-key": "mgmt"})).json()
        assert "ts" in hist
    text = r.text
    r1 = c.active()["revision_id"]
    assert re.search(r'twinspark:generation_tokens_total\{alias="chat",node="A",model="duo-a",revision="'
                     + r1 + r'",engine="0"\} 400', text)
    assert 'twinspark:time_to_first_token_seconds_bucket{alias="chat",node="A"' in text and 'le="+Inf"' in text
    assert 'twinspark:requests_finished_total{alias="chat",node="A",model="duo-a",revision="' + r1 \
        + '",engine="0",finished_reason="stop"} 7' in text
    assert 'twinspark:kv_cache_usage_ratio{alias="code",node="B"' in text
    assert "model_name" not in text
    assert 'twinspark_node_memory_available_bytes{node="A"} ' in text and 'twinspark_node_up{node="B"} 0' in text
    assert 'twinspark_node_memory_pressure_avg10_percent{node="A",kind="some"} 1.5' in text
    assert 'twinspark_half_serving{node="B",name="duo-b"' in text
    assert 'twinspark_window_ttft_seconds{scope="half",node="A",quantile="0.95"} 0.5' in text
    assert 'twinspark_active_info{profile="duo"' in text and "twinspark_switch_in_progress 0" in text
    # every family is declared once, before its samples
    types = re.findall(r"^# TYPE (\S+) (\S+)$", text, re.M)
    assert len(types) == len({name for name, _ in types})
    assert ("twinspark:time_to_first_token_seconds", "histogram") in types
    for line in text.splitlines():
        assert line.startswith("#") or re.match(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? -?[0-9.e+-]+$', line), line


def test_tsm_prometheus_key_creates_once_and_rotates(tmp_path, capsys):
    from twinspark import cli
    secrets = str(tmp_path / "secrets")
    cli.main(["--secrets-dir", secrets, "prometheus", "key"])
    first = capsys.readouterr().out.strip()
    cli.main(["--secrets-dir", secrets, "prometheus", "key"])
    assert capsys.readouterr().out.strip() == first and len(first) > 30
    cli.main(["--secrets-dir", secrets, "prometheus", "key", "--rotate"])
    assert capsys.readouterr().out.strip() != first
    cli.main(["prometheus", "config", "--scheme", "https", "--insecure", "--target", "127.0.0.1:9443"])
    job = capsys.readouterr().out
    assert "scheme: https" in job and "insecure_skip_verify: true" in job and '"127.0.0.1:9443"' in job
    assert "credentials_file: /etc/prometheus/twinspark.key" in job


def test_the_shipped_dashboard_matches_its_generator_and_uses_exported_names():
    import importlib.util
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("gd", root / "scripts/build_grafana_dashboard.py")
    gd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gd)
    shipped = json.loads((root / "deploy/observability/grafana/dashboards/twinspark.json").read_text())
    assert shipped == gd.build()
    exported = {n for n, _, _ in metrics.EXPORT.values()}
    for p in shipped["panels"]:
        for t in p.get("targets", []):
            for name in re.findall(r"twinspark:[a-z_]+", t["expr"]):
                assert re.sub(r"_bucket$", "", name) in exported, name
