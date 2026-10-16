"""0.6 startup memory timeline: agent sampler, /proc and cgroup readers, stage hooks, evidence, classification."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from twinspark.agent import sysinfo
from twinspark.agent.timeline import MAX_SAMPLES, TimelineError, TimelineSampler
from twinspark.controller import evidence
from twinspark.controller import timeline as tl
from twinspark.controller.planner import ModelSpec
from twinspark.schemas.enums import Topology

from .conftest import draft

CID = "c" * 64
MEMINFO = """MemTotal:       127506432 kB
MemFree:         9437184 kB
MemAvailable:   15728640 kB
Buffers:           10240 kB
Cached:          6291456 kB
SwapTotal:      16777216 kB
SwapFree:       16252928 kB
AnonPages:     100000000 kB
"""


# ---- fake /proc and cgroup trees -----------------------------------------------------------------------
def fake_proc(root: Path, pswpin: int = 100, pswpout: int = 200) -> Path:
    proc = root / "proc"
    (proc / "pressure").mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text(MEMINFO)
    (proc / "vmstat").write_text(f"nr_free_pages 1\npswpin {pswpin}\npswpout {pswpout}\npgfault 9\n")
    (proc / "pressure" / "memory").write_text("some avg10=1.25 avg60=0.50 avg300=0.10 total=123\n"
                                              "full avg10=0.40 avg60=0.10 avg300=0.00 total=45\n")

    def process(pid: int, name: str, rss_kb: int | None, cgroup: str, cmdline: str = "") -> None:
        d = proc / str(pid)
        d.mkdir(exist_ok=True)
        status = f"Name:\t{name}\nState:\tS\n" + (f"VmRSS:\t{rss_kb} kB\n" if rss_kb is not None else "")
        (d / "status").write_text(status)
        (d / "cgroup").write_text(cgroup + "\n")
        (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode())
    process(1, "systemd", 12000, "0::/init.scope", "/sbin/init")
    process(2, "kthreadd", None, "0::/")                                     # kernel thread: no VmRSS
    process(812, "python3", 403456, "0::/user.slice/user-1000.slice/session-2.scope",
            "python3 /opt/hermes/gateway.py --port 8080")
    process(900, "chrome", 6246400, "0::/user.slice/user-1000.slice/app.slice")
    process(901, "smallthing", 50000, "0::/user.slice")                       # below 100 MiB
    process(950, "python3", 2097152, f"0::/system.slice/docker-{CID}.scope", "python3 -m vllm.entrypoints")
    process(960, "python3", 300000, "0::/system.slice/twinspark-agent.service", "python3 -m twinspark agent")
    process(970, "gnome-shell", 900000, "0::/user.slice/user-120.slice/session-c1.scope")
    process(980, "dockerd", 150000, "0::/system.slice/docker.service")
    process(990, "python3", 250000, "0::/user.slice", "python3 -m twinspark controller")   # manual install
    for pid, name, rss in ((1001, "node", 300000), (1002, "java", 280000), (1003, "ruby", 260000)):
        process(pid, name, rss, "0::/user.slice", f"{name} -m mod{pid}")
    return proc


def fake_cgroup(root: Path, oom_kill: int = 0, limit: str = "max") -> Path:
    cg = root / "cgroup"
    for unit, cur, peak in (("twinspark-agent", 100 * 2 ** 20, 150 * 2 ** 20),
                            ("twinspark-controller", 220 * 2 ** 20, None)):
        d = cg / "system.slice" / f"{unit}.service"
        d.mkdir(parents=True)
        (d / "memory.current").write_text(f"{cur}\n")
        if peak:
            (d / "memory.peak").write_text(f"{peak}\n")
        (d / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    (cg / "system.slice" / "twinspark-privd.service").mkdir()                 # present, unreadable
    box = cg / "system.slice" / f"docker-{CID}.scope"
    box.mkdir()
    (box / "memory.current").write_text(str(90 * 2 ** 30))
    (box / "memory.peak").write_text(str(95 * 2 ** 30))
    (box / "memory.max").write_text(limit)
    (box / "memory.events").write_text(f"low 0\nhigh 0\nmax 0\noom {oom_kill}\noom_kill {oom_kill}\n")
    return cg


def test_cgroup_and_proc_readers(tmp_path):
    proc, cg = fake_proc(tmp_path), fake_cgroup(tmp_path)
    svc = sysinfo.services_memory(str(cg))
    assert set(svc) == {"twinspark-agent", "twinspark-controller", "twinspark-privd"}   # terminal: not installed
    assert svc["twinspark-agent"] == {"current_mib": 100.0, "peak_mib": 150.0, "oom_kills": 0}
    assert svc["twinspark-controller"]["peak_mib"] is None                    # kernel without memory.peak
    assert svc["twinspark-privd"] == {"current_mib": None, "peak_mib": None, "oom_kills": None}   # null, not 0
    box = sysinfo.cgroup_memory(sysinfo.container_cgroup_dir(CID, str(cg)))
    assert box == {"current_mib": 92160.0, "peak_mib": 97280.0, "oom": 0, "oom_kills": 0, "limit_mib": None}
    assert sysinfo.container_cgroup_dir("not-an-id", str(cg)) is None
    assert sysinfo.cgroup_memory(tmp_path / "missing") is None

    procs = sysinfo.other_processes(str(proc))
    assert [p["name"] for p in procs] == ["chrome", "gateway.py", "mod1001", "mod1002", "mod1003"]
    assert procs[1] == {"name": "gateway.py", "pid": 812, "rss_mib": 394}
    names = {p["name"] for p in sysinfo.other_processes(str(proc), limit=50)}
    # neither containers, TwinSpark, a desktop session, the runtime, kernel threads nor small processes
    assert names.isdisjoint({"vllm.entrypoints", "twinspark", "gnome-shell", "dockerd", "kthreadd", "smallthing",
                             "systemd"})
    assert sysinfo.read_psi(str(proc / "pressure" / "memory")) == {"some_avg10": 1.25, "full_avg10": 0.4}
    assert sysinfo.read_psi(str(tmp_path / "nope")) is None
    assert sysinfo.read_vmstat(str(proc / "vmstat")) == {"pswpin": 100, "pswpout": 200}


class FakeDocker:
    """Stands in for DockerRuntime: answers `docker inspect --format {{.Id}}` for one container."""

    docker = "docker"

    def __init__(self):
        self.calls = 0

    def _run(self, argv, timeout=None, check=True):
        self.calls += 1
        return CID + "\n" if argv[-1] == "tsm-glm-a-r1" else ""


def test_the_sampler_records_marks_rates_and_containers(tmp_path):
    proc, cg = fake_proc(tmp_path), fake_cgroup(tmp_path, oom_kill=1, limit=str(100 * 2 ** 30))
    now = [1000.0]
    docker = FakeDocker()
    s = TimelineSampler(runtime=docker, simulated=False, proc=str(proc), cgroup_root=str(cg), clock=lambda: now[0])
    started = s.start("act-0123456789", interval_s=5, max_samples=50, containers=["tsm-glm-a-r1", "tsm-glm-b-r1"])
    assert started["started"] and started["replaced"] is None and not started["simulated"]
    now[0] += 2.0
    (proc / "vmstat").write_text("pswpin 140\npswpout 1200\n")
    assert s.mark("act-0123456789", "loading")["marked"]
    first, second = s.read("act-0123456789")["samples"][:2]
    assert first["swap_in_pps"] is None                                     # no previous reading yet
    assert second["swap_in_pps"] == 20.0 and second["swap_out_pps"] == 500.0
    assert second["mem_available_gib"] == 15.0 and second["mem_free_gib"] == 9.0 and second["swap_used_gib"] == 0.5
    assert second["psi_full_avg10"] == 0.4 and second["stage"] == "loading" and second["t"] == 2.0
    assert second["driver_allocated_gib"] is None                            # GB10: not reported
    assert second["services"]["twinspark-agent"] == 100.0 and second["services"]["twinspark-privd"] is None
    box = second["containers"]["tsm-glm-a-r1"]
    assert box["current_mib"] == 92160.0 and box["oom_kills"] == 1 and box["limit_mib"] == 102400.0
    assert second["containers"]["tsm-glm-b-r1"] is None                     # not found: unknown
    assert s.stop("act-0123456789")["stopped"]
    out = s.read("act-0123456789")
    assert not out["running"] and out["stopped_reason"] == "stopped" and out["marks"][0]["stage"] == "loading"
    summary = out["summary"]
    assert summary["peak_swap_out_pps"] == 500.0 and summary["max_psi_full_avg10"] == 0.4
    assert summary["oom_events"] == [{"container": "tsm-glm-a-r1", "oom": 1.0, "oom_kill": 1.0, "limit_mib": 102400.0}]
    assert summary["driver_allocated"] == "unknown"
    assert not s.mark("act-0123456789", "testing")["marked"]               # stopped: nothing more is recorded


def test_the_sampler_is_bounded_and_one_per_agent(tmp_path):
    s = TimelineSampler(runtime=None, simulated=True)
    s.start("act-aaaaaaaaaa", interval_s=0.2, max_samples=3)
    deadline = time.monotonic() + 5
    while s.read("act-aaaaaaaaaa")["running"] and time.monotonic() < deadline:
        time.sleep(0.05)
    out = s.read("act-aaaaaaaaaa")
    assert not out["running"] and out["stopped_reason"] == "reached max_samples" and out["total_samples"] == 3
    assert out["simulated"] and all(x["simulated"] for x in out["samples"])
    s.start("act-bbbbbbbbbb", interval_s=10, max_samples=10 ** 9)
    assert s.read("act-bbbbbbbbbb")["running"]
    replaced = s.start("act-cccccccccc", interval_s=10)
    assert replaced["replaced"] == "act-bbbbbbbbbb"
    with pytest.raises(TimelineError, match="no timeline"):
        s.read("act-bbbbbbbbbb")
    with pytest.raises(TimelineError, match="invalid job id"):
        s.start("../etc")
    with pytest.raises(TimelineError, match="tsm-"):
        s.start("act-dddddddddd", containers=["not-ours"])
    with pytest.raises(TimelineError, match="stage"):
        s.mark("act-cccccccccc", "Bad Stage!")
    assert s._run.max_samples <= MAX_SAMPLES
    s.stop("act-cccccccccc")


def test_downsampling_keeps_the_minimum_and_the_stage_edges():
    samples = [{"t": float(i), "mem_available_gib": 100.0 - (i if i < 1500 else 3000 - i) * 0.05} for i in range(3000)]
    samples[2222]["mem_available_gib"] = 0.7
    marks = [{"stage": "starting-cluster", "t": 1000.0, "index": 1000},
             {"stage": "loading", "t": 1777.0, "index": 1777}]
    kept = tl.downsample(samples, 600, marks)
    ts = [s["t"] for s in kept]
    assert len(kept) <= 600 and ts == sorted(ts) and {0.0, 2999.0, 2222.0, 1000.0, 1777.0} <= set(ts)
    s = tl.summarize(samples, marks)
    assert s["min_mem_available_gib"] == 0.7 and s["min_at_s"] == 2222.0
    assert s["baseline_available_gib"] == 50.0 and s["footprint_gib"] == pytest.approx(49.3)
    col = tl.compact(kept[:3])
    assert col["t"] == [0.0, 5.0, 10.0] and col["swap_out_pps"] == [None, None, None]   # unknown stays null
    assert tl.summarize([], [])["samples"] == 0


@pytest.mark.parametrize("records,summaries,error,cause,confidence", [
    ([{"node": "A", "name": "tsm-a", "kernel_gpu_mem_errors": 129, "exit_code": 1, "status": "exited"}], {}, "",
     "driver-allocation", "high"),
    ([{"node": "B", "name": "tsm-b", "first_error": "torch.OutOfMemoryError: CUDA out of memory. Tried 3.5 GiB",
       "exit_code": 1, "status": "exited"}], {}, "", "driver-allocation", "medium"),
    ([{"node": "A", "name": "tsm-a", "oom_killed": True, "exit_code": 137, "status": "exited"}], {}, "",
     "host-oom", "high"),
    ([{"node": "A", "name": "tsm-a", "exit_code": 137, "status": "exited"}],
     {"A": {"oom_events": [{"container": "tsm-a", "oom": 1, "oom_kill": 1, "limit_mib": 65536.0}]}}, "",
     "container-limit", "high"),
    ([{"node": "B", "name": "tsm-b", "first_error": "RuntimeError: NCCL error in: ProcessGroupNCCL.cpp:1970, "
       "ncclSystemError: System call (e.g. socket, malloc) or external library call failed", "exit_code": 1}],
     {}, "", "rdma-nccl", "high"),
    ([{"node": "A", "name": "tsm-a", "status": "running"}], {}, "loading: not healthy after 1800s", "timeout",
     "high"),
    ([{"node": "A", "name": "tsm-a", "first_error": "Traceback (most recent call last):\n  File x\n"
       "KeyError: 'model.layers.0.mlp.experts'", "exit_code": 1}], {}, "", "application-exception", "medium"),
    ([{"node": "A", "name": "tsm-a", "exit_code": 1, "status": "exited"}], {}, "", "unknown", "low"),
])
def test_failure_classification(records, summaries, error, cause, confidence):
    res = evidence.classify(records, summaries, {"error": error, "steps": []})
    assert (res["cause"], res["confidence"]) == (cause, confidence), res
    assert res["evidence"] and all(len(e) <= 220 for e in res["evidence"])
    if cause == "unknown":
        assert "exit code 1" in res["evidence"][0] and "does not tell the cause" in res["evidence"][0]


def test_classification_adds_the_timeline_readings():
    res = evidence.classify([{"node": "A", "name": "tsm-a", "kernel_gpu_mem_errors": 5}],
                            {"A": {"min_mem_available_gib": 0.4, "min_stage": "loading", "max_psi_full_avg10": 31.0,
                                   "peak_swap_out_pps": 25000.0}}, None)
    joined = " | ".join(res["evidence"])
    assert "MemAvailable fell to 0.4 GiB during loading" in joined and "31% (PSI full avg10)" in joined
    assert "swapping out up to 25,000 pages/s" in joined
    assert evidence.classify([], None, {"error": "reclaiming: only 3.0 GiB available"})["evidence"] == [
        "no container had started: reclaiming: only 3.0 GiB available"]


# ---- in the dry-run cluster -----------------------------------------------------------------------------
def agent_actions(cluster, node):
    return cluster.controller.agents[node]._client._transport.app.state.actions


SPEC = ModelSpec(num_params=0, layers=40, num_kv_heads=8, head_dim=128, weight_bytes=40 * 1024 ** 3,
                 hidden_size=4096)


async def test_an_activation_records_a_timeline_with_stage_marks_and_keeps_the_startup_peak(cluster):
    c = cluster.controller
    c.set_model_spec("org/model-c", SPEC)
    c.create_profile(draft("glm", Topology.TP2, repo="org/model-c"))
    job = await cluster.wait_job((await c.activate("glm")).job_id)
    assert job.state.value == "completed", job.error
    rec = job.payload["startup_timeline"]
    assert rec["nodes"] == ["A", "B"] and not rec["errors"]
    assert [m["stage"] for m in rec["marks"]] == list(tl.STAGES)
    for node in ("A", "B"):
        data = rec["nodes_data"][node]
        assert [m["stage"] for m in data["summary"]["marks"]] == list(tl.STAGES)
        assert data["simulated"] and data["points"] <= tl.SUCCESS_POINTS
        assert set(data["series"]) == {"t", "mem_available_gib", "mem_free_gib"}
        assert data["summary"]["footprint_gib"] > 50                          # the simulated GB10 switch
        assert data["summary"]["driver_allocated"] == "unknown"
        # every agent stopped sampling
        assert not agent_actions(cluster, node).timeline.read(job.job_id)["running"]
    obs = c.store.kv_get(f"observed_startup:{job.profile_revision}")
    assert obs["job_id"] == job.job_id and set(obs["nodes"]) == {"A", "B"} and obs["nodes"]["A"]["footprint_gib"] > 50
    # the next plan of the same revision uses it, and the 0.5 observed record is still there
    fit = c.profile_fit("glm")
    assert fit["observed"]["startup"]["job_id"] == job.job_id and "load_seconds" in fit["observed"]
    assert {fit["plan"][n]["startup"]["source"] for n in ("A", "B")} == {"calibrated"}


async def test_the_fit_route_keeps_the_old_keys_and_adds_the_per_node_plan(cluster):
    import httpx

    from twinspark.controller.app import create_app
    c = cluster.controller
    c.set_model_spec("org/model-c", SPEC)
    c.create_profile(draft("glm", Topology.TP2, repo="org/model-c"))
    c.store.kv_set("hardware:A", {"mem_total_gib": 121.6, "mem_available_gib": 110.0, "desktop_running": False,
                                  "desktop_rss_gib": 0.0, "other_processes": [], "services_memory": {}})
    app = create_app(c, management_key="mk", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "mk"}) as api:
        fit = (await api.get("/api/v1/profiles/glm/fit")).json()
    for key in ("known", "fits", "full_concurrency_fits", "headroom_gib", "level", "gpu_memory_utilization",
                "weights_gib_per_node", "kv_pool_gib_per_node", "kv_needed_gib_per_node", "est_kv_tokens",
                "est_full_context_seqs", "context_length", "concurrency", "kv_bytes_per_token_source", "budget"):
        assert key in fit, key
    assert fit["known"] and isinstance(fit["fits"], bool) and fit["budget"]["mem_total"] > 0
    assert set(fit["plan"]) == {"A", "B"} and set(fit["verdicts"]) == {"startup", "one_context", "full_concurrency"}
    a, b = fit["plan"]["A"], fit["plan"]["B"]
    assert a["mem_total_source"] == "measured" and b["mem_total_source"] == "estimated"      # B never reported
    assert next(x for x in b["buckets"] if x["key"] == "desktop")["source"] == "unknown"
    assert fit["headroom_gib"] == min(a["startup"]["margin_gib"], b["startup"]["margin_gib"])
    assert fit["confidence"].startswith("measured inputs: ")


async def test_a_failed_start_keeps_a_bounded_timeline_and_a_classification(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(draft("glm", Topology.TP2, repo="org/model-c"))
    plan = c.launch_plan(c.get_profile("glm").latest())
    cluster.runtimes["B"].fail_start.add(plan.containers[1].name)
    from twinspark.agent.actions import AgentActions
    monkeypatch.setattr(AgentActions, "_kernel_gpu_errors", lambda self, started_at: {
        "count": 74, "samples": ["NVRM: Out of memory [NV_ERR_NO_MEMORY]"], "note": None})
    monkeypatch.setattr(tl, "EVIDENCE_POINTS", 40)
    sampler = agent_actions(cluster, "A").timeline
    real_mark = sampler.mark

    def mark(job_id, stage):          # a long start on node A: many more samples than the evidence keeps
        out = real_mark(job_id, stage)
        if stage == "loading":
            run = sampler._run
            run.samples += [{**run.samples[-1], "t": run.samples[-1]["t"] + i, "mem_available_gib": 30 - i * 0.01}
                            for i in range(1, 400)]
        return out
    monkeypatch.setattr(sampler, "mark", mark)
    job = await cluster.wait_job((await c.activate("glm")).job_id)
    await cluster.wait_idle()
    assert job.state.value == "failed"
    ev = job.payload["startup_evidence"]
    assert ev["limits"]["timeline_points_per_node"] == 40
    a = ev["timeline"]["A"]
    assert a["points"] <= 40 and a["total_samples"] > 400 and len(a["series"]["t"]) == a["points"]
    assert a["summary"]["samples"] == a["total_samples"]                      # the summary saw every sample
    assert [m["stage"] for m in a["summary"]["marks"]][-1] == "failed"
    assert set(ev["timeline"]) == {"A", "B"}
    assert ev["classification"]["cause"] == "driver-allocation" and ev["classification"]["confidence"] == "high"
    assert any("74 NVIDIA NV_ERR_NO_MEMORY" in e for e in ev["classification"]["evidence"])
    # the agents stopped sampling
    assert not sampler.read(job.job_id)["running"]


async def test_timeline_problems_never_break_an_activation(cluster, monkeypatch):
    """An agent without the timeline actions (older version), and one whose calls hang."""
    c = cluster.controller
    c.create_profile(draft("glm", Topology.TP2, repo="org/model-c"))
    for name in ("timeline_start", "timeline_mark", "timeline_stop", "timeline_read"):
        agent_actions(cluster, "B").registry.pop(name)
    monkeypatch.setattr(tl, "CALL_TIMEOUT_S", 0.2)
    slow = agent_actions(cluster, "A").timeline

    def hang(job_id, stage):
        time.sleep(2.0)                                  # longer than the call timeout + its 1 s slack
        return {"marked": True}
    monkeypatch.setattr(slow, "mark", hang)
    t0 = time.monotonic()
    job = await cluster.wait_job((await c.activate("glm")).job_id, timeout=30)
    assert job.state.value == "completed", job.error
    errors = job.payload["startup_timeline"]["errors"]
    assert "does not record startup timelines" in errors["B"] and "took longer" in errors["A"]
    assert time.monotonic() - t0 < 15                    # a node that timed out once is left out afterwards
    await asyncio.sleep(2.1)                             # let the hanging worker threads finish


async def test_a_crash_inside_the_timeline_code_is_contained(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(draft("glm", Topology.TP2, repo="org/model-c"))

    async def boom(self, *a, **kw):
        raise ZeroDivisionError("bug")
    monkeypatch.setattr(tl.StartupTimeline, "mark", boom)
    monkeypatch.setattr(tl.StartupTimeline, "finish_success", boom)
    job = await cluster.wait_job((await c.activate("glm")).job_id)
    assert job.state.value == "completed", job.error


async def test_dry_run_hardware_facts_carry_simulated_planner_inputs(cluster):
    a = await cluster.controller.agents["A"].call("hardware_facts")
    b = await cluster.controller.agents["B"].call("hardware_facts")
    assert a["services_memory"]["twinspark-controller"]["simulated"] is True
    assert "twinspark-controller" not in b["services_memory"]                 # the controller lives on A
    assert a["other_processes"] is None                                       # not invented
    assert a["owned_containers"] == {"running": 0, "current_mib": 0.0, "simulated": True}
    assert a["read_at"] > 0


def test_tsm_job_prints_the_cause_and_the_timeline(capsys):
    from twinspark import cli
    job = {"job_id": "act-0123456789", "payload": {"startup_evidence": {
        "profile": "glm", "revision_id": "r1", "images": [], "containers": [],
        "classification": {"cause": "driver-allocation", "confidence": "high",
                           "evidence": ["node A tsm-a: 74 NVIDIA NV_ERR_NO_MEMORY kernel message(s)"]},
        "timeline": {"A": {"points": 300, "summary": {"min_mem_available_gib": 0.4, "min_at_s": 312.0,
                                                      "min_stage": "loading", "footprint_gib": 98.1,
                                                      "max_psi_full_avg10": 12.0,
                                                      "oom_events": [{"container": "tsm-a", "oom_kill": 1}]}},
                     "B": {"error": "timeline_read: agent unreachable (ConnectError)"}}}}}
    cli._print_evidence(job)
    out = capsys.readouterr().out
    assert "likely cause: driver-allocation (high confidence) — the NVIDIA driver could not allocate memory" in out
    assert "74 NVIDIA NV_ERR_NO_MEMORY" in out
    assert "lowest MemAvailable 0.4 GiB at 312 s (loading)" in out and "the new model took 98.1 GiB" in out
    assert "memory pressure peak 12% (PSI full)" in out and "tsm-a: oom_kill 1" in out
    assert "node B memory timeline: not recorded (timeline_read: agent unreachable" in out
