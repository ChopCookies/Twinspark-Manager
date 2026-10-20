"""Measurements belong to the experiment they were made in (master plan §1.1, §1.2).

Before 0.7 the KV size per token lived on the repo-wide model spec: two profiles of one model with different
KV formats shared a number, and a new measurement changed another profile's forecast. These tests pin the
fixed behaviour down: fingerprints, the recipe's number only for the settings it was measured with, per-commit
specs, and calibrations that survive a controller restart and are not reused for a different context.
"""

from __future__ import annotations

from twinspark.controller.controller import Controller
from twinspark.controller.jobs import kv_facts_from_log
from twinspark.controller.measurements import (
    Measurements,
    kv_fingerprint,
    kv_memory_from_log,
    kv_settings,
    recipe_kv_applies,
    resolve_kv,
    startup_fingerprint,
)
from twinspark.controller.planner import ModelSpec
from twinspark.gateway.gateway import Gateway
from twinspark.schemas.enums import Topology
from twinspark.schemas.profile import ImmutableIdentity

from .conftest import SHA, draft

SPEC = ModelSpec(num_params=0, layers=40, num_kv_heads=8, head_dim=128, weight_bytes=40 * 1024 ** 3,
                 hidden_size=4096)
FACTS = {"mem_total_gib": 121.6, "mem_available_gib": 112.0, "desktop_running": False, "desktop_rss_gib": 0.0,
         "other_processes": [], "services_memory": {}}


def with_kv(d, kv_dtype=None, kvb=None, stamp=True, **adv):
    """A draft as a recipe import leaves it: the recipe's KV size and the settings it was measured with."""
    d = d.model_copy(update={"advanced": d.advanced.model_copy(update={"kv_dtype": kv_dtype, **adv})}, deep=True)
    if kvb:
        d.source["kv_bytes_per_token"] = kvb
        if stamp:
            d.source["kv_measured_with"] = kv_settings(d)
    return d


def other_image(digest_char: str = "c") -> ImmutableIdentity:
    return draft().identity.model_copy(update={"image_digest": "sha256:" + digest_char * 64})


def kv_one_gib(fit, node="A"):
    return next(b for b in fit["plan"][node]["buckets"] if b["key"] == "kv_one_sequence")["gib"]


def _setup(c):
    c.set_model_spec("org/model-a", SPEC, SHA)
    for n in ("A", "B"):
        c.store.kv_set(f"hardware:{n}", FACTS)


# ---- fingerprints -------------------------------------------------------------------------------------
def test_fingerprints_follow_what_changes_the_measurement():
    d = with_kv(draft(), "fp8")
    ident = d.identity
    assert kv_fingerprint(d, ident, "A") == kv_fingerprint(d.model_copy(deep=True), ident, "A")
    assert kv_fingerprint(d, ident, "A") != kv_fingerprint(d, ident, "B")                    # per node
    assert kv_fingerprint(d, ident, "A") != kv_fingerprint(with_kv(draft(), "bf16"), ident, "A")
    assert kv_fingerprint(d, ident, "A") != kv_fingerprint(d, other_image(), "A")            # new image
    longer = d.model_copy(update={"simple": d.simple.model_copy(update={"context_length": 131072})}, deep=True)
    # the bytes per token do not depend on the context; the startup footprint does
    assert kv_fingerprint(longer, ident, "A") == kv_fingerprint(d, ident, "A")
    assert startup_fingerprint(longer, ident, "A") != startup_fingerprint(d, ident, "A")
    flagged = with_kv(draft(), "fp8", extra_vllm_args=["--block-size", "32"])
    assert kv_fingerprint(flagged, ident, "A") != kv_fingerprint(d, ident, "A")
    assert kv_fingerprint(d, None, "A") is None                                              # unpinned


# ---- the recipe's number only for its own settings -------------------------------------------------------
def test_a_recipe_kv_size_does_not_survive_a_change_of_kv_format():
    fp8 = with_kv(draft(), "fp8", kvb=16384)
    assert recipe_kv_applies(fp8) == (True, "measured by the recipe author with these settings")
    bf16 = fp8.model_copy(update={"advanced": fp8.advanced.model_copy(update={"kv_dtype": "bf16"})}, deep=True)
    ok, why = recipe_kv_applies(bf16)
    assert not ok and "kv_dtype" in why
    value, source, note = resolve_kv(None, bf16, bf16.identity, "A")
    assert value is None and source == "estimated" and "kv_dtype" in note
    # imports before 0.7 did not record the settings: the number is used, and says so
    legacy = with_kv(draft(), "fp8", kvb=16384, stamp=False)
    assert resolve_kv(None, legacy, legacy.identity, "A")[2].startswith("recipe measurement (the settings")


def test_two_profiles_of_one_model_with_different_kv_formats_do_not_share_a_forecast(cluster):
    c = cluster.controller
    _setup(c)
    c.create_profile(with_kv(draft("fp8"), "fp8", kvb=16384))
    c.create_profile(with_kv(draft("fp8"), "fp8", kvb=16384).model_copy(update={"name": "bf16"}))
    p = c.get_profile("bf16")
    d = p.working_draft()
    c.save_draft("bf16", d.model_copy(update={"advanced": d.advanced.model_copy(update={"kv_dtype": "bf16"})}))
    fp8, bf16 = c.profile_fit("fp8"), c.profile_fit("bf16")
    assert fp8["kv_source"]["A"]["source"] == "measured" and fp8["kv_source"]["A"]["bytes_per_token"] == 16384
    assert bf16["kv_source"]["A"]["source"] == "estimated" and "kv_dtype" in bf16["kv_source"]["A"]["note"]
    assert kv_one_gib(fp8) == round(16384 * 32768 / 1024 ** 3, 2)
    assert kv_one_gib(bf16) != kv_one_gib(fp8)


def test_a_kv_size_left_on_the_shared_spec_by_06_changes_no_forecast(cluster):
    c = cluster.controller
    _setup(c)
    c.create_profile(with_kv(draft("measured"), "fp8", kvb=16384))
    c.create_profile(draft("plain"))
    before = kv_one_gib(c.profile_fit("plain"))
    # what a 0.6 pin of a third profile left behind: a repo-wide KV size
    for key in ("model_spec:org/model-a", f"model_spec:org/model-a@{SHA}"):
        c.store.kv_set(key, {**SPEC.__dict__, "kv_bytes_per_token": 32768})
    assert kv_one_gib(c.profile_fit("plain")) == before                          # untouched profile: unchanged
    assert c.profile_fit("measured")["kv_source"]["A"]["bytes_per_token"] == 16384
    assert c.model_spec("org/model-a").kv_bytes_per_token is None


def test_specs_are_kept_per_commit(cluster):
    c = cluster.controller
    small = ModelSpec(num_params=0, layers=40, num_kv_heads=8, head_dim=128, weight_bytes=10 * 1024 ** 3)
    c.set_model_spec("org/model-a", SPEC, SHA)
    c.set_model_spec("org/model-a", small, "d" * 40)                 # a later commit with smaller weights
    assert c.model_spec("org/model-a", SHA).weight_bytes == SPEC.weight_bytes
    assert c.model_spec("org/model-a", "d" * 40).weight_bytes == small.weight_bytes
    assert c.model_spec("org/model-a").weight_bytes == small.weight_bytes      # latest, for unpinned drafts
    assert c.model_spec("org/model-a", "e" * 40).weight_bytes == small.weight_bytes   # unknown commit: latest


# ---- measurements ----------------------------------------------------------------------------------------------
def test_vllm_boot_log_gives_the_kv_bytes_per_token():
    log = ("INFO gpu_worker.py: Available KV cache memory: 39.36 GiB\n"
           "INFO kv_cache_utils.py: GPU KV cache size: 1,234,567 tokens\n"
           "INFO kv_cache_utils.py: Maximum concurrency for 262,144 tokens per request: 4.71x\n")
    facts = kv_facts_from_log(log)
    assert facts == {"kv_cache_tokens": 1234567, "max_concurrency": 4.71, "max_concurrency_at_tokens": 262144,
                     "kv_memory_gib": 39.36}
    assert kv_memory_from_log("nothing here") is None


def test_a_new_revision_measures_only_itself_and_the_old_one_keeps_its_value(cluster):
    c = cluster.controller
    _setup(c)
    m = Measurements(c.store)
    d1 = with_kv(draft("glm"), "fp8", kvb=16384)
    d2 = d1.model_copy(update={"identity": other_image()}, deep=True)              # pinned to a new image
    m.record_kv(d1, d1.identity, ["A", "B"], {"kv_cache_tokens": 1_000_000, "kv_memory_gib": 10.0},
                revision_id="r1", job_id="j1")
    got = resolve_kv(m, d1, d1.identity, "A")
    assert got[1] == "measured" and round(got[0]) == round(10 * 1024 ** 3 / 1_000_000) and "1 start" in got[2]
    assert resolve_kv(m, d2, d2.identity, "A")[0] == 16384                        # new image: recipe number
    m.record_kv(d2, d2.identity, ["A"], {"kv_cache_tokens": 1_000_000, "kv_memory_gib": 20.0},
                revision_id="r2", job_id="j2")
    assert round(resolve_kv(m, d1, d1.identity, "A")[0]) == round(10 * 1024 ** 3 / 1_000_000)   # r1 unchanged
    assert round(resolve_kv(m, d2, d2.identity, "A")[0]) == round(20 * 1024 ** 3 / 1_000_000)
    m.record_kv(d1, d1.identity, ["A"], {"kv_cache_tokens": 0}, revision_id="r1")       # nothing to measure
    assert m.kv(d1, d1.identity, "A")["samples"] == 1


def test_calibration_survives_a_restart_and_is_not_reused_for_another_context(cluster, controller_config):
    c = cluster.controller
    _setup(c)
    c.create_profile(draft("glm"))
    d = c.get_profile("glm").latest().draft
    ident = c.get_profile("glm").latest().identity
    summary = {"footprint_gib": 60.0, "startup_min_available_gib": 50.0, "startup_min_stage": "loading",
               "baseline_available_gib": 110.0, "steady_footprint_gib": 55.0}
    m = Measurements(c.store)
    assert m.record_startup(d, ident, "A", summary, revision_id="r1", job_id="j1", kv_tokens=800_000)
    # a restarted controller over the same database
    c2 = Controller(controller_config, Gateway(inference_api_key="k", backend_api_key="b"), c.agents)
    fit = c2.profile_fit("glm")
    a, b = fit["plan"]["A"]["startup"], fit["plan"]["B"]["startup"]
    assert a["source"] == "calibrated" and a["calibration_samples"] == 1 and a["calibration_margin_gib"] == 0.5
    assert "1 earlier start(s) with exactly these settings" in a["note"]
    assert b["source"] == "estimated"                                         # node B was not measured
    assert fit["calibration"]["A"]["samples"] == 1 and fit["calibration"]["B"] is None
    assert fit["plan"]["A"]["full_concurrency"]["source"] == "calibrated"     # vLLM's KV token count
    # three starts: the worst is used without the allowance, the spread is reported
    for i, foot in enumerate((58.0, 63.0)):
        m.record_startup(d, ident, "A", {**summary, "footprint_gib": foot}, revision_id="r1", job_id=f"j{i + 2}")
    cal = m.startup(d, ident, "A")
    assert cal["samples"] == 3 and cal["footprint_gib"] == 63.0 and cal["margin_gib"] == 0 and cal["spread_gib"] == 5.0
    # the same job is one sample, however often it is recorded
    m.record_startup(d, ident, "A", {**summary, "footprint_gib": 99.0}, revision_id="r1", job_id="j3")
    assert m.startup(d, ident, "A")["samples"] == 3
    # a longer context is a different startup: the old total is not matched to it
    longer = d.model_copy(update={"simple": d.simple.model_copy(update={"context_length": 131072})}, deep=True)
    assert m.startup(longer, ident, "A") is None


async def test_a_completed_dry_run_activation_records_nothing_but_still_plans(cluster):
    c = cluster.controller
    _setup(c)
    c.create_profile(draft("glm", Topology.TP2))
    job = await cluster.wait_job((await c.activate("glm")).job_id)
    assert job.state.value == "completed", job.error
    assert job.payload["kv_sources"]["glm"]["source"] == "estimated"
    rev = c.get_profile("glm").latest()
    m = Measurements(c.store)
    assert m.startup(rev.draft, rev.identity, "A") is None and m.kv(rev.draft, rev.identity, "A") is None
    assert c.profile_fit("glm")["fingerprinted"] is True


# ---- validation records (§1.4) ------------------------------------------------------------------------------
async def test_a_validation_record_reconstructs_the_tested_combination(cluster):
    import httpx

    from twinspark.controller.app import create_app
    c = cluster.controller
    _setup(c)
    c.create_profile(draft("glm", Topology.TP2))
    from twinspark.controller.validation import build_record, to_markdown
    rec = build_record(c, "glm")
    assert rec["evidence"]["class"] == "none" and rec["procedure"] is None
    job = await cluster.wait_job((await c.activate("glm")).job_id)
    rec = build_record(c, "glm")
    assert rec["format"] == "twinspark-validation/1"
    assert rec["evidence"]["class"] == "dry-run"                # simulated containers prove the plan only
    part = rec["deployment"]["parts"][0]
    assert part["model_revision"] == SHA and part["image_digest"].startswith("sha256:") and part["topology"] == "tp2"
    assert {"A", "B"} == set(rec["nodes"]) and all("agent_version" in f for f in rec["nodes"].values())
    assert rec["procedure"]["job_id"] == job.job_id and rec["procedure"]["state"] == "completed"
    assert [s["stage"] for s in rec["procedure"]["stages"]][-1] == "healthy"
    commands = rec["deployment"]["commands"]
    assert commands and all("vllm" in " ".join(x["command"]) for x in commands)
    md = to_markdown(rec)
    assert "dry-run" in md and "| Topology | tp2 on A, B |" in md
    app = create_app(c, management_key="mk", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "mk"}) as api:
        r = await api.get("/api/v1/profiles/glm/validation")
        assert r.status_code == 200 and r.json()["revision"]["profile"] == "glm"
        r = await api.get("/api/v1/profiles/glm/validation", params={"format": "markdown"})
        assert r.headers["content-type"].startswith("text/markdown") and "### glm" in r.text
        assert (await api.get("/api/v1/profiles/glm/validation", params={"job": "nope"})).status_code == 404


def test_records_leave_out_addresses_and_user_names():
    from twinspark.controller.validation import _scrub
    assert _scrub("--master-addr=10.0.0.1") == "--master-addr=<address>"
    assert _scrub("/home/alice/.cache/hf") == "/home/<user>/.cache/hf"
    assert _scrub("fd00:1:2::5") != "" and "<address>" in _scrub("fe80:0:0:0:1:2:3:4")
    assert _scrub("--tensor-parallel-size 2") == "--tensor-parallel-size 2"
    assert _scrub("connect to [fe80::1]:9443 failed") == "connect to [<address>]:9443 failed"
    assert _scrub("::1 refused") == "<address> refused" and _scrub("at 12:30:45") == "at 12:30:45"
    from twinspark.controller.validation import _scrub_doc
    doc = _scrub_doc({"error": "agent http://10.0.0.2:9443 timed out", "w": ["/home/bob/x"], "n": 3})
    assert doc == {"error": "agent http://<address>:9443 timed out", "w": ["/home/<user>/x"], "n": 3}
