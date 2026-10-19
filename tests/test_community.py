"""Community reports (opt-in, previewed) and the PHP endpoint for them (community/strato), run with PHP's
built-in server and SQLite: what leaves a node is an allowlist, and both ends apply the same rules."""

from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from twinspark.cookbook import build_draft, community
from twinspark.cookbook.community import ReportError, build_report, check_report, tuning_changes
from twinspark.cookbook.tracking import settings

ROOT = Path(__file__).resolve().parent.parent
STRATO = ROOT / "community/strato"
SHA = "c" * 64


def record(**over) -> dict:
    """A validation record (controller.validation.build_record) as report_for_profile sees it."""
    rec = {
        "evidence": {"class": "manager-validated"},
        "revision": {"recipe": "glm-5.3-flash-nvfp4-tp2", "recipe_ref": "builtin:glm-5.3-flash-nvfp4-tp2.json",
                     "recipe_sha256": SHA},
        "procedure": {"state": "completed", "dry_run": False, "stages": []},
        "deployment": {"parts": [{"topology": "tp2", "vllm_version": "0.11.2", "image": "vllm-node-tf5",
                                  "model_repo": "/home/someone/models/glm"}]},
        "results": {"benchmarks": [
            {"preset": "max-throughput", "valid": True, "at": "2026-10-01T10:00:00",
             "summary": {"peak_output_tok_s": 812.5, "at_concurrency": 8, "text": "peak 812 tok/s on spark-a"}},
            {"preset": "max-throughput", "valid": True, "at": "2026-09-01T10:00:00",
             "summary": {"peak_output_tok_s": 100}},
            {"preset": "context", "valid": False, "at": "2026-10-02", "summary": {"longest": 1}}]},
        "nodes": {"A": {"hostname": "spark-a.local"}},
        "mode": "throughput", "engine": "vllm", "changes": [{"field": "advanced.max_num_seqs", "value": 12}],
    }
    rec.update(over)
    return rec


def test_a_report_holds_only_the_allowlisted_facts():
    doc = build_report(record(), comment="Works fine with 12 sequences.", reporter="ab" * 16)
    assert doc == {
        "format": "twinspark-report/1", "recipe": "glm-5.3-flash-nvfp4-tp2", "recipe_sha256": SHA,
        "mode": "throughput", "outcome": "works", "failure_stage": None, "twinspark": doc["twinspark"],
        "engine": "vllm", "engine_version": "0.11.2", "topology": "tp2", "hardware": "2x DGX Spark",
        "benchmarks": {"max-throughput": {"peak_output_tok_s": 812.5, "at_concurrency": 8}},   # newest, numbers
        "changes": [{"field": "advanced.max_num_seqs", "value": 12}],
        "comment": "Works fine with 12 sequences.", "reporter": "ab" * 16}
    text = json.dumps(doc)
    for private in ("spark-a", "/home/someone", "vllm-node-tf5", "hostname"):
        assert private not in text
    failed = build_report(record(procedure={"state": "failed", "stages": [
        {"stage": "stage-weights", "status": "ok"}, {"stage": "health-check", "status": "failed"}]}))
    assert failed["outcome"] == "fails" and failed["failure_stage"] == "health" and "reporter" not in failed


def test_dry_runs_and_profiles_without_a_cookbook_recipe_are_not_reported():
    with pytest.raises(ReportError, match="dry run"):
        build_report(record(procedure={"state": "completed", "dry_run": True}))
    with pytest.raises(ReportError, match="Cookbook recipe"):
        build_report(record(revision={"recipe": "x", "recipe_ref": "https://example.com/x.yaml", "recipe_sha256": SHA}))
    with pytest.raises(ReportError, match="--outcome"):
        build_report(record(procedure=None))
    assert build_report(record(procedure=None), outcome="works-with-changes")["outcome"] == "works-with-changes"


def test_only_tuning_settings_with_plain_values_count_as_changes():
    base = build_draft("glm-5.3-flash-nvfp4-tp2")
    mine = base.model_copy(deep=True)
    mine.advanced.max_num_seqs = 12
    mine.advanced.gpu_memory_utilization = 0.8
    mine.advanced.env = {"HF_TOKEN": "hf_secret", "NCCL_SOCKET_IFNAME": "enp1s0f1np1"}
    mine.advanced.extra_vllm_args = ["--served-model-name", "/home/me/x"]
    mine.advanced.extra_vllm_flags = {**base.advanced.extra_vllm_flags, "swap-space": 4, "download-dir": "/mnt/models",
                                      "tokenizer": "my-private-tokenizer"}
    mine.simple.api_alias = "my-secret-alias"
    mine.simple.model = "me/private-finetune"
    changes = tuning_changes(base.source["receipt"]["baseline"], settings(mine))
    fields = {c["field"]: c["value"] for c in changes}
    assert fields == {"advanced.max_num_seqs": 12, "advanced.gpu_memory_utilization": 0.8,
                      "advanced.extra_vllm_flags.swap-space": 4}
    assert tuning_changes(None, settings(mine)) == []
    mine.advanced.extra_vllm_flags = {}                    # a recipe flag taken out is a change too
    removed = {c["field"]: c["value"] for c in tuning_changes(base.source["receipt"]["baseline"], settings(mine))}
    assert removed["advanced.extra_vllm_flags.moe-backend"] is None


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(hostname="spark-a"), "unexpected fields"),
    (lambda d: d.update(comment="see 192.168.1.20 for logs"), "address or a path"),
    (lambda d: d.update(comment="cache is in /home/me/.cache"), "address or a path"),
    (lambda d: d.update(comment="x" * 501), "500 characters"),
    (lambda d: d["changes"].append({"field": "advanced.env.HF_TOKEN", "value": "abc"}), "not reportable"),
    (lambda d: d["changes"].append({"field": "advanced.max_num_seqs", "value": "/home/me"}), "not reportable"),
    (lambda d: d["changes"].append({"field": "advanced.extra_vllm_flags.tokenizer", "value": "my-private-tok"}),
     "not reportable"),
    (lambda d: d["benchmarks"].update(context={"text": "free text"}), "benchmark"),
    (lambda d: d.update(outcome="great"), "outcome"),
    (lambda d: d.update(reporter="spark-a"), "reporter"),
    (lambda d: d.update(recipe="../etc"), "recipe"),
])
def test_both_ends_refuse_the_same_documents(change, message, strato):
    doc = build_report(record(), reporter="ab" * 16)
    change(doc)
    with pytest.raises(ReportError, match=message):
        check_report(doc)
    r = strato.post(doc)
    assert r.status_code == 422, r.text
    assert message.split()[0].lower() in r.json()["error"].lower()


# --------------------------------------------------------------------------------------- the PHP side


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Strato:
    """php -S serving community/strato. SQLite by default; with TSM_TEST_MYSQL_DSN (+ _USER, _PASSWORD) the same
    test runs against a MySQL/MariaDB test database whose tables come from community/strato/schema.sql."""

    def __init__(self, tmp: Path):
        self.secret = "f" * 64
        dsn = os.environ.get("TSM_TEST_MYSQL_DSN")
        if dsn:
            db = {"dsn": dsn, "db_user": os.environ.get("TSM_TEST_MYSQL_USER"),
                  "db_pass": os.environ.get("TSM_TEST_MYSQL_PASSWORD")}
        else:
            path = tmp / "reports.sqlite"
            con = sqlite3.connect(path)
            con.executescript((ROOT / "tests/data/strato-schema-sqlite.sql").read_text())
            con.close()
            db = {"dsn": f"sqlite:{path}"}
        self.cfg = tmp / "private" / "config.php"
        self.cfg.parent.mkdir()
        self.cfg.write_text("<?php return " + _php({**db, "secret": self.secret, "max_reports_per_day": 25}) + ";\n")
        if dsn:
            self.sql("DELETE FROM reports")
            self.sql("DELETE FROM rate_limits")
        self.port = _free_port()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TWINSPARK_CONFIG": str(self.cfg),
               "TMPDIR": str(tmp)}
        self.proc = subprocess.Popen(["php", "-S", f"127.0.0.1:{self.port}", "-t", str(STRATO)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                httpx.get(self.base + "/report.php", timeout=1)          # 405: up (and no totals cached yet)
                return
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("php -S did not start")

    def sql(self, query: str) -> list[dict]:
        """A query through PDO with the endpoint's own configuration."""
        code = ("$c = require getenv('CFG'); $p = new PDO($c['dsn'], $c['db_user'] ?? null, $c['db_pass'] ?? null);"
                "$p->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION); $q = $p->query(getenv('Q'));"
                "echo json_encode($q->columnCount() ? $q->fetchAll(PDO::FETCH_ASSOC) : []);")
        out = subprocess.run(["php", "-r", code], env={"CFG": str(self.cfg), "Q": query, "PATH": "/usr/bin:/bin"},
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout or "[]")

    def post(self, doc) -> httpx.Response:
        return httpx.post(self.base + "/report.php", json=doc, timeout=10)

    def rows(self) -> list[dict]:
        return self.sql("SELECT * FROM reports ORDER BY id")

    def close(self):
        self.proc.terminate()
        self.proc.wait(10)


def _php(value) -> str:
    """A PHP literal for a config value (strings, numbers, None)."""
    if isinstance(value, dict):
        return "[" + ", ".join(f"{_php(k)} => {_php(v)}" for k, v in value.items()) + "]"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _php_ready() -> bool:
    if not shutil.which("php"):
        return False
    mods = subprocess.run(["php", "-m"], capture_output=True, text=True).stdout.lower()
    driver = "pdo_mysql" if os.environ.get("TSM_TEST_MYSQL_DSN") else "pdo_sqlite"
    return driver in mods and "mbstring" in mods


@pytest.fixture
def strato(tmp_path):
    if not _php_ready():
        pytest.skip("PHP with pdo_sqlite and mbstring is not installed")
    s = Strato(tmp_path)
    yield s
    s.close()


def test_reports_are_stored_replaced_and_totalled(strato):
    me = build_report(record(), reporter="ab" * 16)
    r = strato.post(me)
    assert r.status_code == 201 and r.json()["ok"]
    again = copy.deepcopy(me)
    again["outcome"], again["failure_stage"] = "fails", "start"
    assert strato.post(again).status_code == 200              # the same reporter, recipe file and mode: replaced
    other = build_report(record(), reporter="cd" * 16)
    other["benchmarks"]["max-throughput"]["peak_output_tok_s"] = 900.0
    assert strato.post(other).status_code == 201
    anon = build_report(record(mode="latency", changes=[]))
    assert strato.post(anon).status_code == 201
    assert strato.post(anon).status_code == 201                # anonymous: counted again, only rate-limited
    rows = strato.rows()
    assert len(rows) == 4
    stored = json.dumps(rows)
    assert "ab" * 16 not in stored and "127.0.0.1" not in stored    # keyed hashes only
    st = httpx.get(strato.base + "/stats.php", timeout=10).json()["recipes"]["glm-5.3-flash-nvfp4-tp2"]
    assert (st["reports"], st["works"], st["fails"]) == (4, 3, 1)
    assert st["success_rate"] == 0.75 and st["median_tok_s"] == pytest.approx(812.5)
    assert st["modes"]["throughput"]["reports"] == 2 and st["modes"]["latency"]["works"] == 2
    assert st["top_changes"] == [{"field": "advanced.max_num_seqs", "value": 12, "count": 1}]  # from working setups
    # moderation: a hidden report is not counted (one recipe's totals are read fresh)
    strato.sql("UPDATE reports SET hidden = 1 WHERE outcome = 'fails'")
    one = httpx.get(strato.base + "/stats.php", params={"recipe": "glm-5.3-flash-nvfp4-tp2"}, timeout=10).json()
    assert one["recipes"]["glm-5.3-flash-nvfp4-tp2"]["fails"] == 0
    # the client reads the totals and keeps only what it knows
    with httpx.Client(transport=httpx.HTTPTransport()) as c:
        r = c.get(strato.base + "/stats.php")
    cleaned = community._clean_stats(r.json()["recipes"]["glm-5.3-flash-nvfp4-tp2"])
    assert cleaned["reports"] == 4 and cleaned["top_changes"][0]["field"] == "advanced.max_num_seqs"


def test_the_endpoint_limits_reports_per_address_and_size(strato):
    doc = build_report(record(changes=[]))
    codes = [strato.post(doc).status_code for _ in range(26)]
    assert codes[:25] == [201] * 25 and codes[25] == 429
    r = httpx.post(strato.base + "/report.php", content=b"{" + b" " * 20000 + b"}",
                   headers={"content-type": "application/json"}, timeout=10)
    assert r.status_code == 413
    assert httpx.get(strato.base + "/report.php", timeout=10).status_code == 405
    assert httpx.post(strato.base + "/report.php", content=b"x", headers={"content-type": "text/plain"},
                      timeout=10).status_code == 415
    assert httpx.get(strato.base + "/lib.php", timeout=10).status_code == 404


def test_the_strato_files_carry_no_credentials():
    example = (STRATO / "config.example.php").read_text()
    assert "change-me" in example and "replace-with" in example
    for p in STRATO.iterdir():
        assert p.name != "config.php", "a filled-in config must never be committed"


# -------------------------------------------------------------------------------- node A's routes


async def test_the_api_previews_and_only_sends_when_switched_on(cluster, monkeypatch):
    from twinspark.controller.app import create_app

    from .conftest import identity
    c = cluster.controller
    d = build_draft("smollm2-135m-smoke", "smoke")
    d.identity = identity(d.simple.model, d.simple.quantization)            # pinned: a revision to report on
    d.advanced.max_num_seqs = 3
    c.create_profile(d)
    app = create_app(c, "key", run_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "key"}) as client:
        r = await client.get("/api/v1/cookbook/report/smoke", params={"outcome": "works"})
        assert r.status_code == 200, r.text
        pv = r.json()
        assert pv["enabled"] is False and pv["report"]["recipe"] == "smollm2-135m-smoke"
        assert {"field": "advanced.max_num_seqs", "value": 3} in pv["report"]["changes"]
        rid = pv["report"]["reporter"]
        assert (Path(c.config.db_path).parent / "community-reporter").read_text().strip() == rid
        r = await client.post("/api/v1/cookbook/report", json={"report": pv["report"]})
        assert r.status_code == 409                                           # off: nothing leaves
        c.config.cookbook.community_url = "https://reports.example.com/twinspark"
        sent = []

        def fake_send(url, doc, client=None):
            sent.append((url, doc))
            return {"ok": True, "message": "recorded"}
        monkeypatch.setattr(community, "send_report", fake_send)
        tampered = {**pv["report"], "hostname": "spark-a"}
        r = await client.post("/api/v1/cookbook/report", json={"report": tampered})
        assert r.status_code == 422 and not sent                              # checked again before sending
        r = await client.post("/api/v1/cookbook/report", json={"report": pv["report"]})
        assert r.status_code == 200 and sent[0][0] == "https://reports.example.com/twinspark"
        r = await client.get("/api/v1/cookbook/report/smoke", params={"anonymous": "true", "outcome": "works"})
        assert "reporter" not in r.json()["report"]


def test_send_report_and_stats_over_https_only():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path.endswith("stats.php"):
            return httpx.Response(200, json={"recipes": {"x": {"reports": 2, "works": 1, "success_rate": 0.5,
                                                                "top_changes": [{"field": "advanced.env.X",
                                                                                 "value": 1, "count": 2}]}}})
        return httpx.Response(201, json={"ok": True, "message": "recorded"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert community.send_report("https://r.example/ts/", build_report(record()), c)["message"] == "recorded"
        stats = community.community_stats("https://r.example/ts", c, refresh=True)
    assert seen == ["https://r.example/ts/report.php", "https://r.example/ts/stats.php"]
    assert stats["recipes"]["x"]["reports"] == 2
    with pytest.raises(ReportError, match="https"):
        community.send_report("http://r.example", build_report(record()))
    assert community.community_stats(None) == {}
