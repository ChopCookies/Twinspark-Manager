"""Pinning registers the drafter's ModelSpec, and the fit of the GLM-5.3 DFlash2 recipe counts it on both nodes."""

from __future__ import annotations

from twinspark.controller import pinning
from twinspark.cookbook import build_draft

from .conftest import SHA

RECIPE = "glm-5.3-flash-nvfp4-dflash2-tp2"
DRAFTER = "incoai/GLM-5.3-Flash-DFlash2"
_MAIN_CFG = {"num_hidden_layers": 47, "num_attention_heads": 96, "num_key_value_heads": 8, "head_dim": 128,
             "hidden_size": 6144}
_DRAFT_CFG = {"num_hidden_layers": 2, "num_attention_heads": 32, "num_key_value_heads": 8, "head_dim": 128,
              "hidden_size": 4096}


def _hub(calls, offline_drafter=False):
    def resolve(ref, token=None, *a, **kw):
        calls.append(ref)
        repo = ref.partition("@")[0]
        if repo == DRAFTER:
            if offline_drafter:
                raise ValueError("cannot reach Hugging Face (ConnectError)")
            return {"repo": repo, "revision": "e" * 40, "config": _DRAFT_CFG, "weight_bytes": 3 * 1024 ** 3}
        return {"repo": repo, "revision": SHA, "config": _MAIN_CFG, "weight_bytes": 64 * 1024 ** 3}
    return resolve


def _pin_setup(monkeypatch, calls, offline=False):
    monkeypatch.setattr(pinning, "resolve_hf_revision", _hub(calls, offline))
    monkeypatch.setattr(pinning, "resolve_model_ref", lambda ref, token=None: (ref.split("@")[0], "e" * 40))
    monkeypatch.setattr(pinning, "resolve_image_digest",
                        lambda ref: "ghcr.io/tonyd2wild/vllm-glm53-flash@sha256:" + "f" * 64)


async def test_pinning_registers_the_drafter_and_the_fit_counts_it_on_both_nodes(cluster, monkeypatch):
    c = cluster.controller
    calls: list[str] = []
    _pin_setup(monkeypatch, calls)
    c.create_profile(build_draft(RECIPE))
    res = await c.pin_profile(RECIPE)
    assert any(n.startswith(f"{DRAFTER}: memory spec registered (3.0 GiB") for n in res["notes"])
    assert f"{DRAFTER}@{'e' * 40}" in calls
    spec = c.model_spec(DRAFTER)
    assert spec.weight_bytes == 3 * 1024 ** 3 and spec.hidden_size == 4096
    # pinning again does not ask the Hub for the drafter a second time
    calls.clear()
    await c.pin_profile(RECIPE)
    assert not any(x.startswith(DRAFTER) for x in calls)

    for n in ("A", "B"):
        c.store.kv_set(f"hardware:{n}", {"mem_total_gib": 121.6, "mem_available_gib": 113.0, "desktop_running": False,
                                         "desktop_rss_gib": 0.0, "other_processes": [], "services_memory": {}})
    fit = c.profile_fit(RECIPE)
    assert fit["drafters"] == [DRAFTER] and fit["drafter_gib_per_node"] == 3.0
    for n in ("A", "B"):
        p = fit["plan"][n]
        w = next(b for b in p["buckets"] if b["key"] == "drafter_weights")
        assert w["gib"] == 3.0 and "unconfirmed (counted on both nodes)" in w["note"]
        assert p["gpu_memory_utilization"] == 0.85 and p["vllm_claim_gib"] == round(0.85 * 121.6, 2)
        prefill = next(b for b in p["buckets"] if b["key"] == "first_prefill")
        assert "max_num_batched_tokens 8,192" in prefill["note"]           # the recipe's prefill batch
        assert next(b for b in p["buckets"] if b["key"] == "cuda_graphs")["gib"] == 0     # eager
        assert p["one_context"]["source"] == "measured"                     # the recipe's KV bytes/token
    assert set(fit["verdicts"]) == {"startup", "one_context", "full_concurrency"}


async def test_an_offline_drafter_lookup_does_not_stop_the_pin(cluster, monkeypatch):
    c = cluster.controller
    _pin_setup(monkeypatch, [], offline=True)
    c.create_profile(build_draft(RECIPE))
    res = await c.pin_profile(RECIPE)
    assert res["revision"]["draft"]["advanced"]["extra_models"] == [f"{DRAFTER}@{'e' * 40}"]
    assert any("memory spec not registered" in n and "unknown" in n for n in res["notes"])
    assert c.model_spec(DRAFTER) is None
    fit = c.profile_fit(RECIPE)
    w = next(b for b in fit["plan"]["A"]["buckets"] if b["key"] == "drafter_weights")
    assert w["gib"] is None and w["source"] == "unknown"
    assert "Drafter weights" in fit["plan"]["A"]["startup"]["not_counted"]
