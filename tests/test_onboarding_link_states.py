"""Get started keeps "configured", "measured" and "both PCIe halves" apart."""

from __future__ import annotations

import time

from twinspark.controller import onboarding


def steps(ob):
    return {s["id"]: s for s in ob["steps"]}


async def test_configured_is_not_measured(cluster):
    c = cluster.controller
    s = steps(await onboarding.build(c))
    assert s["link"]["status"] == "done" and s["link-test"]["status"] == "todo"
    assert s["link-test"]["detail"] == "not tested yet" and s["halves"]["status"] == "done"
    await c.run_link_test(mode="rdma", duration_s=2)                     # both dry-run: a simulation
    assert "only simulated" in steps(await onboarding.build(c))["link-test"]["detail"]


async def test_a_real_measurement_and_a_failed_one_are_shown_as_such(cluster):
    c = cluster.controller
    c.store.kv_set("linktest:rdma", {"at": time.time() - 120, "ok": True, "simulated": False,
                                     "bandwidth_gbps": 111.57,
                                     "per_hca": [{"hca": "rocep1s0f1", "gbps": 111.57, "error": None}]})
    t = steps(await onboarding.build(c))["link-test"]
    assert t["status"] == "done" and "RDMA 111.57 Gb/s (rocep1s0f1 111.57" in t["detail"]
    c.store.kv_set("linktest:rdma", {"at": time.time(), "ok": False, "simulated": False,
                                     "errors": ["ib_write_bw on roceP2p1s0f1 exited with code 1"]})
    t = steps(await onboarding.build(c))["link-test"]
    assert t["status"] == "warn" and "exited with code 1" in t["detail"]


async def test_one_pcie_half_is_valid_but_flagged(cluster):
    c = cluster.controller
    c.config.nodes["A"].rdma_hcas = ["rocep1s0f1"]
    c.config.nodes["B"].rdma_hcas = ["rocep1s0f1"]
    s = steps(await onboarding.build(c))
    assert s["link"]["status"] == "done"                                 # a working, configured link
    h = s["halves"]
    assert h["status"] == "warn" and not h["required"] and "node A, B" in h["detail"]
    assert "tsm qsfp plan" in h["fix"]["command"]


async def test_a_failed_rdma_test_next_to_a_good_tcp_test_is_named(cluster):
    c = cluster.controller
    c.store.kv_set("linktest:tcp", {"at": time.time(), "ok": True, "simulated": False, "bandwidth_gbps": 42.18,
                                    "streams": 2})
    c.store.kv_set("linktest:rdma", {"at": time.time(), "ok": False, "simulated": False,
                                     "errors": ["ib_write_bw on rocep1s0f1 exited with code 1"]})
    t = steps(await onboarding.build(c))["link-test"]
    assert "TCP 42.18 Gb/s" in t["detail"] and "the last RDMA test (what NCCL uses) failed: ib_write_bw" in t["detail"]
    assert "not measured yet" not in t["detail"]


def test_node_b_counts_as_over_the_link_only_for_its_exact_address():
    assert onboarding._url_host("http://10.0.0.25:8100") == "10.0.0.25" != "10.0.0.2"
    assert onboarding._url_host("http://[fd00::2]:8100") == "fd00::2" and onboarding._url_host(None) == ""
