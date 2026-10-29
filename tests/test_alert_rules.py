"""deploy/observability/alerts.yml refers only to metrics node A really exports (promtool checks the syntax in CI)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from twinspark.controller import exporter

ROOT = Path(__file__).resolve().parents[1]
OBS = ROOT / "deploy" / "observability"


def rules() -> list[dict]:
    doc = yaml.safe_load((OBS / "alerts.yml").read_text(encoding="utf-8"))
    return [r for g in doc["groups"] for r in g["rules"]]


def test_every_alert_names_a_severity_a_summary_and_a_duration():
    names = set()
    for r in rules():
        assert r["alert"].startswith("TwinSpark") and r["alert"] not in names
        names.add(r["alert"])
        assert r["labels"]["severity"] in ("critical", "warning", "info")
        assert r["annotations"]["summary"] and re.fullmatch(r"\d+[mh]", r["for"])


def test_alerts_use_only_exported_metrics():
    source = Path(exporter.__file__).read_text(encoding="utf-8")
    exported = set(re.findall(r'"(twinspark_[a-z0-9_]+)"', source))
    used = {m for r in rules() for m in re.findall(r"\btwinspark_[a-z0-9_]+", r["expr"])}
    assert used and used <= exported, used - exported
    assert all("up{" not in r["expr"] or 'job="twinspark"' in r["expr"] for r in rules())


def test_prometheus_loads_the_rules_and_the_unit_tests_cover_them():
    prom = yaml.safe_load((OBS / "prometheus.yml").read_text(encoding="utf-8"))
    assert "/etc/prometheus/alerts.yml" in prom["rule_files"]
    compose = (OBS / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./alerts.yml:/etc/prometheus/alerts.yml:ro" in compose
    tests = yaml.safe_load((OBS / "alerts-test.yml").read_text(encoding="utf-8"))
    tested = {t["alertname"] for case in tests["tests"] for t in case["alert_rule_test"]}
    assert tested <= {r["alert"] for r in rules()} and len(tested) >= 3


def test_the_update_and_version_gauges_are_exported(cluster):
    text = exporter.render(cluster.controller)
    assert "twinspark_update_in_progress 0" in text
    cluster.controller.store.kv_set("hardware:A", {"agent_version": "0.8.0"})
    assert 'twinspark_node_info{node="A",version="0.8.0"} 1' in exporter.render(cluster.controller)
