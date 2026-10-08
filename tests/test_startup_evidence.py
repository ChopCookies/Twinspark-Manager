"""A failed launch keeps both ranks' logs, exit state and memory readings before cleanup removes them."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import httpx
import pytest

from twinspark.controller import evidence
from twinspark.controller.app import create_app
from twinspark.schemas.enums import Topology

from .conftest import draft

VLLM_LOG = """\
INFO 10-08 01:41:02 [api_server.py:1] vLLM API server version 0.11
INFO 10-08 01:41:30 [core.py:1] loading weights
(EngineCore_DP0 pid=812) ERROR 10-08 01:47:51 [core.py:708] EngineCore failed to start.
(EngineCore_DP0 pid=812) Traceback (most recent call last):
(EngineCore_DP0 pid=812)   File "/usr/lib/python3/vllm/v1/engine/core.py", line 699, in run_engine_core
(EngineCore_DP0 pid=812)     engine_core = EngineCoreProc(*args, **kwargs)
(EngineCore_DP0 pid=812)   File "/usr/lib/python3/vllm/v1/worker/gpu_model_runner.py", line 3021, in profile_run
(EngineCore_DP0 pid=812)     hidden = self._dummy_run(self.max_num_tokens)
(EngineCore_DP0 pid=812) torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 3.50 GiB.
INFO 10-08 01:47:55 [shutdown] leaked shared_memory objects to clean up at shutdown
Traceback (most recent call last):
  File "/usr/lib/python3/vllm/entrypoints/openai/api_server.py", line 1900, in run_server
    async with build_async_engine_client(args) as engine_client:
RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}
"""


def test_the_root_cause_is_found_before_the_wrapper():
    s = evidence.error_summary(VLLM_LOG)
    assert "OutOfMemoryError: CUDA out of memory" in s["first_error"]
    assert "Engine core initialization failed" not in s["first_error"]
    assert "Engine core initialization failed" in s["final_error"]


def test_a_log_without_errors_has_no_summary_and_a_single_error_is_not_repeated():
    assert evidence.error_summary("INFO all good\nINFO still good") == {"first_error": None, "final_error": None}
    one = evidence.error_summary("Traceback (most recent call last):\n  File x\nValueError: bad flag")
    assert "ValueError: bad flag" in one["first_error"] and one["final_error"] is None


def test_saved_files_are_private_named_safely_and_pruned(tmp_path):
    root = tmp_path / "evidence"
    name, size = evidence.save(root, "act-0123456789", "A", "tsm-glm-a-r1", "x" * (evidence.MAX_LOG_CHARS + 50))
    path = root / "act-0123456789" / name
    assert size == evidence.MAX_LOG_CHARS and path.read_text() == "x" * evidence.MAX_LOG_CHARS
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(root.stat().st_mode) == 0o700
    for bad in ("../x.log", "A-tsm-x/../../y.log", "C-tsm-x.log", "A-other.log"):
        with pytest.raises(ValueError):
            evidence.open_file(root, "act-0123456789", bad)
    with pytest.raises(ValueError):
        evidence.save(root, "../act", "A", "tsm-x", "log")
    for i in range(evidence.KEEP_JOBS + 3):
        evidence.save(root, f"act-{i:010x}", "A", "tsm-x", "log")
        os.utime(root / f"act-{i:010x}", (1000 + i, 1000 + i))
    removed = evidence.prune(root)
    assert len(removed) == 4 and "act-0000000000" in removed       # the oldest go first
    assert len([d for d in root.iterdir()]) == evidence.KEEP_JOBS


async def test_a_failed_two_node_launch_keeps_both_ranks_evidence(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(draft("glm", Topology.TP2, repo="org/model-c"))
    plan = c.launch_plan(c.get_profile("glm").latest())
    head, worker = plan.containers[0], plan.containers[1]
    rt_b = cluster.runtimes["B"]
    rt_b.fail_start.add(worker.name)
    from twinspark.security import SecretsVault
    token = "agent-secret-0123456789"
    SecretsVault(Path(c.config.secrets_dir).parent / "secrets-B").set("agent_token", token)   # B's vault
    monkeypatch.setattr(rt_b, "logs", lambda name, tail=200, max_chars=200_000: (
        VLLM_LOG + f"\nenv AGENT={token}\napi_key=sk-abcdefghijklmnopqrstuvwxyz123456\n")[-max_chars:])

    job = await cluster.wait_job((await c.activate("glm")).job_id)
    assert job.state.value == "failed" and job.steps[-1].stage == "loading"
    ev = job.payload["startup_evidence"]
    assert ev["profile"] == "glm" and ev["images"] and ev["limits"]["max_log_chars"] == evidence.MAX_LOG_CHARS
    by_node = {e["node"]: e for e in ev["containers"]}
    assert set(by_node) == {"A", "B"}                                  # both ranks, not only the API server
    b = by_node["B"]
    assert b["name"] == worker.name and b["status"] == "exited" and b["exit_code"] == 1
    assert "OutOfMemoryError" in b["first_error"] and "Engine core initialization failed" in b["final_error"]
    assert b["memory"]["mem_total_gib"] >= 0 and b["file"] == f"B-{worker.name}.log"
    assert by_node["A"]["name"] == head.name and by_node["A"]["file"]
    # the containers are gone, the evidence is not
    await cluster.wait_idle()
    assert worker.name not in rt_b.containers
    saved = evidence.open_file(evidence.evidence_dir(c.config.db_path), job.job_id, b["file"]).read_text()
    assert "OutOfMemoryError" in saved and token not in saved and "sk-abcdefghijklmnop" not in saved
    excerpt = job.steps[-1].log_excerpt
    assert "-- first error --" in excerpt and "OutOfMemoryError" in excerpt and "node B" in excerpt
    app = create_app(c, management_key="mk", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "mk"}) as api:
        r = await api.get(f"/api/v1/jobs/{job.job_id}/evidence/{b['file']}")
        assert r.status_code == 200 and "OutOfMemoryError" in r.text
        assert r.headers["content-type"].startswith("text/plain")
        assert (await api.get(f"/api/v1/jobs/{job.job_id}/evidence/..%2Fstate.db")).status_code == 404
        assert (await api.get(f"/api/v1/jobs/{job.job_id}/evidence/A-tsm-other.log")).status_code == 404
        listed = next(j for j in (await api.get("/api/v1/jobs")).json() if j["job_id"] == job.job_id)
        assert listed["payload"]["startup_evidence"] == {"containers": 2, "detail": f"/api/v1/jobs/{job.job_id}"}
        full = (await api.get(f"/api/v1/jobs/{job.job_id}")).json()
        ranks = full["payload"]["startup_evidence"]["containers"]
        assert any("OutOfMemoryError" in (x.get("first_error") or "") for x in ranks)


async def test_kernel_gpu_out_of_memory_messages_explain_an_exit_code_1(cluster, monkeypatch):
    c = cluster.controller
    c.create_profile(draft("glm", Topology.TP2, repo="org/model-c"))
    plan = c.launch_plan(c.get_profile("glm").latest())
    cluster.runtimes["A"].fail_start.add(plan.containers[0].name)
    from twinspark.agent.actions import AgentActions
    monkeypatch.setattr(AgentActions, "_kernel_gpu_errors", lambda self, started_at: {
        "count": 129, "samples": ["NVRM: _memdescAllocInternal: Out of memory [NV_ERR_NO_MEMORY]"], "note": None})
    job = await cluster.wait_job((await c.activate("glm")).job_id)
    assert job.state.value == "failed"
    assert job.guidance[0].startswith("the NVIDIA driver reported out-of-memory errors")
    assert "exits with code 1 can still have failed on GPU memory" in job.guidance[0]
    assert any(e["kernel_gpu_mem_errors"] == 129 for e in job.payload["startup_evidence"]["containers"])


def test_tsm_job_prints_where_the_saved_logs_are(capsys):
    from twinspark import cli
    job = {"job_id": "act-0123456789", "payload": {"startup_evidence": {"profile": "glm", "revision_id": "r1",
           "images": ["img@sha256:1"], "containers": [
               {"node": "B", "name": "tsm-glm-b", "status": "exited", "exit_code": 1, "oom_killed": False,
                "memory": {"mem_available_gib": 3.2, "mem_total_gib": 121.6}, "kernel_gpu_mem_errors": 74,
                "file": "B-tsm-glm-b.log", "bytes": 1234}]}}}
    cli._print_evidence(job)
    out = capsys.readouterr().out
    assert "exit code 1" in out and "74 NVIDIA out-of-memory kernel message(s)" in out
    assert "tsm job act-0123456789 --log B-tsm-glm-b.log" in out and "3.2 of 121.6 GiB available" in out


def test_the_agent_counts_nvidia_memory_errors_since_the_container_started(tmp_path, monkeypatch):
    from twinspark.agent.actions import AgentActions
    from twinspark.remote import logs
    from twinspark.schemas.config import AgentConfig, NodeIdentity

    class Docker:                                    # anything that is not the dry-run runtime
        pass

    acts = AgentActions(AgentConfig(node=NodeIdentity(node_id="A", role="agent"), secrets_dir=str(tmp_path / "s")),
                        runtime=Docker())
    seen = {}

    def read(source, lines, since_s, grep, priv=None):
        seen.update(source=source, since_s=since_s, grep=grep)
        return {"lines": ["NVRM: GPU0 _memdescAllocInternal: Out of memory [NV_ERR_NO_MEMORY]"] * 7
                + ["NVRM: something else"], "note": None}

    monkeypatch.setattr(logs, "read_logs", read)
    monkeypatch.setattr(acts.privd, "available", lambda: False)
    res = acts._kernel_gpu_errors("2000-01-01T00:00:00.123456789Z")
    assert res["count"] == 7 and len(res["samples"]) == 5
    assert seen == {"source": "kernel", "since_s": 86400, "grep": "NVRM"}       # bounded look-back
    assert acts._kernel_gpu_errors(None)["count"] == 7 and seen["since_s"] == 3600


async def test_cleanup_and_rollback_run_even_when_saving_the_evidence_fails(cluster, monkeypatch):
    """Evidence is best effort: a bug or a hang there must never leave containers behind."""
    c = cluster.controller
    c.create_profile(draft("glm", Topology.TP2, repo="org/model-c"))
    plan = c.launch_plan(c.get_profile("glm").latest())
    cluster.runtimes["B"].fail_start.add(plan.containers[1].name)
    monkeypatch.setattr(evidence, "error_summary", lambda log: 1 / 0)
    seen = []
    real_stop = c._stop_everything

    async def stop(*a, **kw):
        seen.append(c.store.load_job(job_id).state.value)         # clients still see it as running
        return await real_stop(*a, **kw)
    monkeypatch.setattr(c, "_stop_everything", stop)
    job_id = (await c.activate("glm")).job_id
    job = await cluster.wait_job(job_id)
    assert job.state.value == "failed" and "internal error" not in (job.error or "")
    assert "ZeroDivisionError" in job.payload["evidence_error"] and seen == ["running"]
    assert job.rollback.startswith("stopped partial containers")
    await cluster.wait_idle()
    assert not any(n.startswith("tsm-") for rt in cluster.runtimes.values() for n in rt.containers
                   if rt.containers[n].status == "running")


def test_redaction_keeps_settings_and_catches_the_other_secret_forms():
    from twinspark.remote.bundle import Redactor
    r = Redactor(["vault-value-123456"])
    log = ("non-default args: {'max_num_batched_tokens': 8192, 'tokenizer_mode': 'auto', 'api_key': ['sk-x1']}\n"
           "cmd: vllm serve --max-num-batched-tokens 8192 --api-key plainsecret --hf-token=hf_short\n"
           "HF_TOKEN=abc123 password: hunter22 vault-value-123456\n"
           "api_key=['k1-first', 'k2-second'] MY_" + "X" * 80 + "_SECRET=long-name-value\n")
    out = r(log)
    assert "'max_num_batched_tokens': 8192" in out and "--max-num-batched-tokens 8192" in out
    assert "'tokenizer_mode': 'auto'" in out
    for secret in ("sk-x1", "plainsecret", "hf_short", "abc123", "hunter22", "vault-value-123456",
                   "k1-first", "k2-second", "long-name-value"):
        assert secret not in out, secret
    assert r(out) == out                                             # agent and controller both apply it


def test_redaction_and_summary_stay_linear_on_hostile_lines():
    import time

    from twinspark.remote.bundle import Redactor
    t0 = time.monotonic()
    Redactor()("a." * 40000 + "\n" + "token." * 40000 + "\n" + "x" * 200000)
    evidence.error_summary("Traceback (most recent call last):\n(" + "a" * 50000 + "\n" + "b." * 40000)
    assert time.monotonic() - t0 < 5


def test_a_full_kernel_log_window_is_reported_as_a_minimum(tmp_path, monkeypatch, capsys):
    from twinspark import cli
    from twinspark.agent.actions import AgentActions
    from twinspark.remote import logs
    from twinspark.schemas.config import AgentConfig, NodeIdentity

    class Docker:
        pass
    acts = AgentActions(AgentConfig(node=NodeIdentity(node_id="A", role="agent"), secrets_dir=str(tmp_path / "s")),
                        runtime=Docker())
    monkeypatch.setattr(logs, "read_logs", lambda *a, **kw: {
        "lines": ["NVRM: Out of memory [NV_ERR_NO_MEMORY]"] * 3, "note": None, "scanned": 2000, "window_full": True})
    monkeypatch.setattr(acts.privd, "available", lambda: False)
    res = acts._kernel_gpu_errors(None)
    assert res["at_least"] and "earlier messages may exist" in res["note"]
    rec = {"node": "A", "name": "tsm-a", "kernel_gpu_mem_errors": 3, "kernel_count_at_least": True}
    assert "node A: at least 3" in evidence.gpu_memory_note([rec])
    cli._print_evidence({"job_id": "act-0123456789", "payload": {"startup_evidence": {"containers": [rec]}}})
    assert "at least 3 NVIDIA" in capsys.readouterr().out


async def test_free_form_recipe_metadata_cannot_break_the_plan(cluster):
    c = cluster.controller
    d = draft("odd", Topology.TP2, repo="org/model-c")
    for i, measured in enumerate((None, "fast", {"boot": None}, {"boot": ["x"]})):
        d.source = {"measured": measured}
        rev = c.create_profile(d.model_copy(update={"name": f"odd-{i}"}, deep=True)).latest()
        assert any(n.startswith("startup limits:") for n in c.launch_plan(rev).notes)


async def test_other_jobs_with_an_evidence_field_are_a_404_not_a_crash(cluster):
    from twinspark.schemas.job import Job
    c = cluster.controller
    c.store.save_job(Job(job_id="evaluation-0123456789", kind="evaluation", payload={"evidence": "reported"}))
    app = create_app(c, management_key="mk", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                 headers={"x-api-key": "mk"}) as api:
        r = await api.get("/api/v1/jobs/evaluation-0123456789/evidence/A-tsm-x.log")
        assert r.status_code == 404
        listed = next(j for j in (await api.get("/api/v1/jobs")).json() if j["job_id"] == "evaluation-0123456789")
        assert listed["payload"]["evidence"] == "reported"
