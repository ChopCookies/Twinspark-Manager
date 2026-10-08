"""Link tests: both sides in the same mode, failures reported as unavailable, responders cleaned up."""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

from twinspark.agent import linktest
from twinspark.controller.agent_client import AgentActionError

GOOD = """
 #bytes     #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
 65536      213004           0.00               111.57               0.212801
"""


def _patch_mode(cluster, monkeypatch, node: str, mode: str, calls: list):
    agent = cluster.controller.agents[node]
    real = agent.call

    async def call(action, /, timeout=60, **params):
        calls.append((node, action))
        res = await real(action, timeout=timeout, **params)
        if action == "hardware_facts":
            res = {**res, "runtime_mode": mode}
        return res
    monkeypatch.setattr(agent, "call", call)


async def test_mixed_runtime_modes_are_refused_before_anything_starts(cluster, monkeypatch):
    calls: list = []
    _patch_mode(cluster, monkeypatch, "A", "docker", calls)
    _patch_mode(cluster, monkeypatch, "B", "dry-run", calls)
    with pytest.raises(ValueError, match=r"node B runs in dry-run.*sudo tsm go-live` on node B"):
        await cluster.controller.run_link_test(duration_s=2)
    assert not [c for c in calls if c[1] == "link_test"]


async def test_an_all_simulated_test_says_so_and_is_remembered(cluster):
    res = await cluster.controller.run_link_test(mode="rdma", duration_s=2)
    assert res["simulated"] is True and res["runtime_modes"] == {"A": "dry-run", "B": "dry-run"}
    assert set(res["hcas"]) == {"A", "B"} and res["streams"] is None
    assert "no packet crossed the link" in res["initiator"]["note"]
    last = cluster.controller.store.kv_get("linktest:rdma")
    assert last["simulated"] is True and last["duration_s"] == 2


async def test_a_failed_initiator_start_cancels_the_responder(cluster, monkeypatch):
    calls: list = []
    a, b = cluster.controller.agents["A"], cluster.controller.agents["B"]
    real_a, real_b = a.call, b.call

    async def call_a(action, /, timeout=60, **params):
        if action == "link_test":
            raise AgentActionError("link_test", "A", "boom")
        return await real_a(action, timeout=timeout, **params)

    async def call_b(action, /, timeout=60, **params):
        calls.append(action)
        return await real_b(action, timeout=timeout, **params)
    monkeypatch.setattr(a, "call", call_a)
    monkeypatch.setattr(b, "call", call_b)
    with pytest.raises(AgentActionError):
        await cluster.controller.run_link_test(duration_s=2)
    assert calls[-1] == "task_cancel"


class FakeProc:
    def __init__(self, rc: int, out: str):
        self.returncode, self._out = rc, out.encode()

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass


async def test_rdma_failures_are_unavailable_not_zero(monkeypatch):
    outputs = {"rocep1s0f1": (0, GOOD),
               "roceP2p1s0f1": (1, "Couldn't connect to 192.168.101.2:18516\nUnable to init")}

    async def spawn(*argv, **kw):
        return FakeProc(*outputs[argv[argv.index("-d") + 1]])
    monkeypatch.setattr(linktest, "perftest_available", lambda: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    res = await linktest.rdma_run(["rocep1s0f1", "roceP2p1s0f1"], 3, 18515, 2, "192.168.100.2")
    assert res["bandwidth_gbps"] is None and res["measured_gbps"] == 111.57
    bad = next(r for r in res["per_hca"] if r["hca"] == "roceP2p1s0f1")
    assert bad["gbps"] is None and "exited with code 1" in bad["error"] and "Couldn't connect" in bad["error"]
    assert len(res["errors"]) == 1 and any("partial result" in n for n in res["notes"])
    one = await linktest.rdma_run(["rocep1s0f1"], 3, 18515, 2, "192.168.100.2")
    assert one["bandwidth_gbps"] == 111.57 and one["hcas"] == ["rocep1s0f1"] and one["duration_s"] == 2
    assert any("one PCIe half measured (rocep1s0f1)" in n and "tsm qsfp plan" in n for n in one["notes"])
    outputs["rocep1s0f1"] = (0, "no table here")
    empty = await linktest.rdma_run(["rocep1s0f1"], 3, 18515, 2, "192.168.100.2")
    assert empty["bandwidth_gbps"] is None and "no bandwidth reading" in empty["per_hca"][0]["error"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_the_tcp_responder_closes_when_the_streams_are_done():
    port = _free_port()
    started = time.monotonic()
    responder = asyncio.create_task(linktest.tcp_responder("127.0.0.1", port, lifetime=30, expect_streams=2))
    await asyncio.sleep(0.2)
    init = await linktest.tcp_initiator("127.0.0.1", port, duration=0.3, streams=2)
    resp = await asyncio.wait_for(responder, 10)
    assert resp["complete"] and resp["streams"] == 2 and init["streams"] == 2 and init["duration_s"] >= 0.3
    assert time.monotonic() - started < 10                    # not the 30 s lifetime
