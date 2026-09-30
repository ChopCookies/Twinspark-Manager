"""0.4.0 backend: eugr import, RoCE/mods launch wiring, pinning, model files, mods,
metrics, gateway failover, watchdog, cancel, staging, diagnostics helpers.

Everything runs against in-process dry-run agents; nothing touches Docker, the
network or a real vLLM.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest

from twinspark.agent import mods as modlib
from twinspark.agent.linktest import parse_ib_write_bw
from twinspark.agent.sysinfo import rdma_devices, suggest_rdma
from twinspark.controller import pinning
from twinspark.controller.files import DependencyError
from twinspark.controller.launch import LaunchPlanner
from twinspark.cookbook import RecipeImportError, build_draft, import_text, recipe_detail
from twinspark.cookbook.remote import list_source, parse_source
from twinspark.gateway.app import build_gateway_app
from twinspark.gateway.gateway import Gateway
from twinspark.metrics import MetricsSampler, _parse_text, combine_snapshots, derive
from twinspark.schemas.enums import Topology
from twinspark.schemas.profile import ImmutableIdentity, Profile, check_raw_args
from twinspark.weights import execute_delete, plan_delete, scan_cache

from .conftest import SHA, draft

DS_RECIPE = "deepseek-v4-flash-0731-b12x"
LOCAL_ID = "sha256:" + "d4" * 32


def _pinned(d, image_digest=LOCAL_ID, source="local"):
    ident = ImmutableIdentity(model_repo=d.simple.model, model_revision=SHA,
                              quantization=d.simple.quantization, image="vllm-node-b12x",
                              image_digest=image_digest, image_source=source)
    p = Profile(name=d.name)
    return p.add_revision(d, ident)


def _zip(files: dict[str, str], top: str = "") -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, body in files.items():
            z.writestr(f"{top}{n}", body)
    return base64.b64encode(buf.getvalue()).decode()


# ---- eugr importer ------------------------------------------------------------------------
def test_eugr_recipe_import_is_faithful():
    d = build_draft(DS_RECIPE)
    s, a, b = d.simple, d.advanced, d.behaviour
    assert s.model == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert s.topology == Topology.TP2 and s.context_length == "auto"
    assert s.thinking and s.tool_calling
    assert (b.reasoning_parser, b.tool_call_parser) == ("deepseek_v4", "deepseek_v4")
    assert b.manage_thinking_kwarg is False            # the recipe's own kwargs win
    assert a.kv_dtype == "fp8" and a.block_size == 256 and a.max_num_seqs == 8
    assert a.gpu_memory_utilization == 0.85 and a.weight_loader == "instanttensor"
    assert a.speculative_config["method"] == "dspark"
    assert a.speculative_config["num_speculative_tokens"] == 5
    assert a.compilation_config == {"cudagraph_mode": "FULL_AND_PIECEWISE", "custom_ops": ["all"]}
    assert a.mods == ["instanttensor-hybrid-draft-loader"]
    assert a.env["VLLM_USE_B12X_MOE"] == "1" and a.env["CUTE_DSL_ARCH"] == "sm_121a"
    raw = a.extra_vllm_args
    for tok in ("--moe-backend", "b12x", "--linear-backend", "--max-cudagraph-capture-size",
                "--default-chat-template-kwargs.thinking=true",
                "--default-chat-template-kwargs.reasoning_effort=high", "--reasoning-config"):
        assert tok in raw
    assert "--host" not in raw and "--port" not in raw
    assert d.image_hint == "vllm-node-b12x"
    assert d.verification.value == "verified"            # from the .meta.json
    detail = recipe_detail(DS_RECIPE)
    assert "--host 0.0.0.0" in " ".join(detail["report"]["dropped"]) and detail["text"]


def test_eugr_import_rejects_what_two_sparks_cannot_run():
    base = "name: x\ncommand: vllm serve {m} --tensor-parallel-size {tp}\n"
    with pytest.raises(RecipeImportError, match="4 GPUs"):
        import_text(base.format(m="org/m", tp=4))
    with pytest.raises(RecipeImportError, match="local path"):
        import_text(base.format(m="/models/m", tp=2))
    with pytest.raises(RecipeImportError, match="only vLLM"):
        import_text("name: x\ncommand: python -m sglang.launch_server --model org/m\n")
    d, rep = import_text("name: Tiny\ncommand: vllm serve org/tiny --max-model-len {ctx}\n"
                         "defaults: {ctx: 8192}\n", overrides={"ctx": 16384})
    assert d.name == "tiny" and d.simple.context_length == 16384 and rep["format"] == "eugr"


def test_import_text_accepts_twinspark_json():
    doc = json.loads(Path("twinspark/cookbook/recipes/glm-5.3-flash-nvfp4-tp2.json").read_text())
    d, rep = import_text(json.dumps(doc), profile_name="glm-mine", source_ref="https://x/y.json")
    assert d.name == "glm-mine" and rep["format"] == "twinspark"
    assert d.advanced.block_size == 2304


def test_raw_args_cannot_smuggle_manager_owned_flags():
    with pytest.raises(ValueError):
        check_raw_args(["--port", "9000"])
    with pytest.raises(ValueError):
        check_raw_args(["-tp", "4"])
    check_raw_args(["--moe-backend", "b12x", "--default-chat-template-kwargs.thinking=true"])


# ---- launch wiring -------------------------------------------------------------------------
def test_eugr_launch_plan_matches_the_proven_setup(controller_config):
    rev = _pinned(build_draft(DS_RECIPE))
    plan = LaunchPlanner(controller_config).plan(rev, 0.85)
    head, worker = plan.containers
    assert plan.start_order == [[worker.name], [head.name]]       # worker first
    assert plan.wave_delays_s[0] >= 2
    for c in (head, worker):
        assert c.env["NCCL_IB_HCA"] == "rocep1s0f1,roceP2p1s0f1"
        assert c.env["NCCL_IB_GID_INDEX"] == "3"
        assert c.env["NCCL_SOCKET_IFNAME"] == c.env["GLOO_SOCKET_IFNAME"] == "enp1s0f1np1"
        assert c.env["VLLM_USE_B12X_MOE"] == "1"                     # recipe env kept
        assert c.devices == ["/dev/infiniband"] and c.cap_add == ["IPC_LOCK"]
        assert c.command[:2] == ["bash", "-c"]                        # mods wrapper
        script = c.command[2]
        assert "instanttensor-hybrid-draft-loader" in script and "exec vllm serve" in script
        assert any(m.container == "/opt/twinspark/mods" and m.read_only for m in c.mounts)
    cmd = plan.rendered_commands()[head.name]
    assert "--ulimit nofile=1048576:1048576" in cmd and "--entrypoint=" in cmd
    script = head.command[2]
    assert script.count("--max-model-len") == 1 and "--max-model-len auto" in script
    assert "--gpu-memory-utilization 0.85" in script
    assert script.index("--moe-backend") > script.index("--speculative-config")   # raw args last
    assert "--node-rank 0" in script and "--node-rank 1 --headless" in worker.command[2]
    assert plan.routes == {"default": ["http://127.0.0.1:18100"]}


def test_single_node_plan_has_no_rdma_and_privileged_mode(controller_config):
    single = LaunchPlanner(controller_config).plan(_pinned(draft("s", Topology.SINGLE_A)), 0.8)
    (c,) = single.containers
    assert "NCCL_IB_HCA" not in c.env and not c.devices and not c.privileged
    controller_config.runtime.container_mode = "privileged"
    tp2 = LaunchPlanner(controller_config).plan(_pinned(draft("t", Topology.TP2)), 0.8)
    assert all(c.privileged and not c.devices for c in tp2.containers)


def test_extra_aliases_all_route(controller_config):
    d = draft("glm", Topology.TP2, extra_aliases=["glm", "coder"])
    plan = LaunchPlanner(controller_config).plan(_pinned(d), 0.8)
    assert set(plan.routes) == {"default", "glm", "coder"}


# ---- pinning -----------------------------------------------------------------------------
_CFG = {"architectures": ["X"], "num_hidden_layers": 4, "num_attention_heads": 8,
        "num_key_value_heads": 2, "head_dim": 64, "hidden_size": 512}


def _fake_resolver(calls):
    def resolve(ref, token=None, *a, **kw):
        calls.append(ref)
        repo, _, branch = ref.partition("@")
        return {"repo": repo, "branch": branch, "revision": SHA, "config": _CFG,
                "weight_bytes": 20 * 1024**3}
    return resolve


async def test_pin_local_image_then_activate_needs_mods(cluster, monkeypatch):
    c = cluster.controller
    calls: list[str] = []
    monkeypatch.setattr(pinning, "resolve_hf_revision", _fake_resolver(calls))
    c.create_profile(build_draft(DS_RECIPE))
    res = await c.pin_profile(DS_RECIPE)
    rev = res["revision"]
    assert rev["identity"]["image_source"] == "local"
    assert rev["identity"]["image_digest"].startswith("sha256:")
    assert rev["identity"]["model_revision"] == SHA and calls == [
        "deepseek-ai/DeepSeek-V4-Flash-0731@main"]
    assert c.model_spec("deepseek-ai/DeepSeek-V4-Flash-0731").weight_bytes == 20 * 1024**3
    again = await c.pin_profile(DS_RECIPE)
    assert "nothing changed" in " ".join(again["notes"])
    assert len(c.get_profile(DS_RECIPE).revisions) == 1

    # the mod is missing on both nodes: preflight stops before the old model is touched
    job = await cluster.wait_job((await c.activate(DS_RECIPE)).job_id)
    assert job.state.value == "failed" and "not installed" in job.error
    res = await c.install_mod("instanttensor-hybrid-draft-loader",
                              _zip({"run.sh": "#!/bin/bash\n# patch loader\necho ok\n"}))
    assert res["consistent"]
    job = await cluster.wait_job((await c.activate(DS_RECIPE)).job_id)
    assert job.state.value == "completed", job.error
    started = " ".join(cluster.runtimes["A"].history[-1])
    assert "/opt/twinspark/mods:ro" in started and "--device /dev/infiniband" in started
    fit = c.profile_fit(DS_RECIPE)
    assert fit["known"] and fit["est_kv_tokens"] > 0 and fit["observed"]


async def test_pin_registry_image_pins_digest_and_drafter(cluster, monkeypatch):
    c = cluster.controller
    monkeypatch.setattr(pinning, "resolve_hf_revision", _fake_resolver([]))
    monkeypatch.setattr(pinning, "resolve_model_ref", lambda ref, token=None: (ref.split("@")[0], "e" * 40))
    monkeypatch.setattr(pinning, "resolve_image_digest",
                        lambda ref: "ghcr.io/tonyd2wild/vllm-glm53-flash@sha256:" + "f" * 64)
    c.create_profile(build_draft("glm-5.3-flash-nvfp4-dflash2-tp2"))
    res = await c.pin_profile("glm-5.3-flash-nvfp4-dflash2-tp2")
    ident = res["revision"]["identity"]
    assert ident["image_source"] == "registry" and ident["image_digest"] == "sha256:" + "f" * 64
    adv = res["revision"]["draft"]["advanced"]
    assert adv["extra_models"] == ["incoai/GLM-5.3-Flash-DFlash2@" + "e" * 40]
    assert adv["speculative_config"]["revision"] == "e" * 40
    plan = c.launch_plan(c.get_profile("glm-5.3-flash-nvfp4-dflash2-tp2").latest())
    assert "ghcr.io/tonyd2wild/vllm-glm53-flash@sha256:" in plan.containers[0].image_ref


async def test_pin_rejects_image_that_differs_between_nodes(cluster, monkeypatch):
    c = cluster.controller
    monkeypatch.setattr(pinning, "resolve_hf_revision", _fake_resolver([]))
    orig = cluster.runtimes["B"].image_inspect
    cluster.runtimes["B"].image_inspect = lambda ref: {**orig(ref), "id": "sha256:" + "0" * 64}
    c.create_profile(build_draft(DS_RECIPE))
    with pytest.raises(pinning.PinError, match="differs between nodes"):
        await c.pin_profile(DS_RECIPE)


# ---- model files -------------------------------------------------------------------------
def _fake_cache(hf: Path) -> None:
    """repo X with two snapshots sharing one blob; repo Y sharing an HF2 hub blob."""
    hub = hf / "hub"
    x = hub / "models--org--x"
    (x / "blobs").mkdir(parents=True)
    (x / "refs").mkdir()
    (x / "blobs" / "shared").write_bytes(b"s" * 1000)
    (x / "blobs" / "only1").write_bytes(b"1" * 300)
    (x / "blobs" / "only2").write_bytes(b"2" * 500)
    for sha, own in (("1" * 40, "only1"), ("2" * 40, "only2")):
        snap = x / "snapshots" / sha
        snap.mkdir(parents=True)
        (snap / "model.safetensors").symlink_to(f"../../blobs/{own}")
        (snap / "config.json").symlink_to("../../blobs/shared")
    (x / "refs" / "main").write_text("2" * 40)
    (hub / "blobs" / "ab").mkdir(parents=True)
    (hub / "blobs" / "ab" / "abcd").write_bytes(b"h" * 4000)
    for name in ("models--org--y", "models--org--z"):
        r = hub / name
        (r / "blobs").mkdir(parents=True)
        (r / "blobs" / "w").symlink_to("../../blobs/ab/abcd")
        snap = r / "snapshots" / ("3" * 40)
        snap.mkdir(parents=True)
        (snap / "model.safetensors").symlink_to("../../blobs/w")


def test_scan_and_delete_keep_shared_blobs(tmp_path, require_symlinks):
    hf = tmp_path / "hf"
    _fake_cache(hf)
    inv = {r["repo"]: r for r in scan_cache(hf)["repos"]}
    assert set(inv) == {"org/x", "org/y", "org/z"}
    assert [r["size_bytes"] for r in inv["org/x"]["revisions"]] == [1300, 1500]
    assert inv["org/x"]["revisions"][1]["refs"] == ["main"]

    p = plan_delete(hf, "org/x", "1" * 40)
    assert p["freed_bytes"] == 300 and not p["whole_repo"] and p["kept_shared_blobs"] == 1
    execute_delete(p, hf)
    assert (hf / "hub/models--org--x/blobs/shared").exists()
    assert not (hf / "hub/models--org--x/blobs/only1").exists()

    p = plan_delete(hf, "org/y", None)                 # hub blob still used by org/z
    assert p["whole_repo"] and p["freed_bytes"] == 0
    execute_delete(p, hf)
    assert (hf / "hub/blobs/ab/abcd").exists() and not (hf / "hub/models--org--y").exists()
    p = plan_delete(hf, "org/z", None)                 # now the last user
    assert p["freed_bytes"] == 4000


async def test_model_inventory_dependents_and_delete_guard(cluster, controller_config, require_symlinks):
    _fake_cache(Path(controller_config.runtime.hf_cache_dir))
    c = cluster.controller
    d = draft("x", Topology.SINGLE_A, repo="org/x")
    d.identity.model_revision = "2" * 40
    c.create_profile(d)
    inv = await c.model_files()
    x = next(m for m in inv["models"] if m["repo"] == "org/x")
    rev2 = next(r for r in x["revisions"] if r["revision"] == "2" * 40)
    assert rev2["profiles"] == ["x"] and set(rev2["nodes"]) == {"A", "B"}
    with pytest.raises(DependencyError) as ei:
        await c.delete_model_files("org/x", "2" * 40, ["A"])
    assert ei.value.dependents == ["x"]
    prev = await c.delete_model_files("org/x", "2" * 40, ["A"], preview=True)
    assert prev["results"]["A"]["freed_bytes"] == 500
    # dry-run agents only ever preview
    res = await c.delete_model_files("org/x", "1" * 40, ["A", "B"])
    assert all(r["preview"] for r in res["results"].values())


async def test_stage_job_runs_in_background(cluster):
    c = cluster.controller
    job = await c.stage(f"org/model-a@{SHA}", nodes=["A", "B"])
    assert job.kind == "stage"
    done = await cluster.wait_job(job.job_id)
    assert done.state.value == "completed", done.error
    assert not c._staging


# ---- mods --------------------------------------------------------------------------------
def test_mod_install_is_safe_and_deterministic(tmp_path):
    root = tmp_path / "mods"
    a = modlib.install_mod(root, "fix", _zip({"run.sh": "echo a\n", "p/x.py": "print(1)\n"}, "fix/"))
    b = modlib.install_mod(root, "fix", _zip({"run.sh": "echo a\n", "p/x.py": "print(1)\n"}))
    assert a["hash"] == b["hash"]                                   # top dir stripped
    with pytest.raises(modlib.ModError, match="run.sh"):
        modlib.install_mod(root, "bad", _zip({"README.md": "x"}))
    with pytest.raises(modlib.ModError):
        modlib.install_mod(root, "evil", _zip({"run.sh": "x", "../../escape": "x"}))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo("run.sh")
        info.size = 3
        t.addfile(info, io.BytesIO(b"x\n\n"))
        link = tarfile.TarInfo("etc")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc"
        t.addfile(link)
    with pytest.raises(modlib.ModError):
        modlib.install_mod(root, "tarlink", base64.b64encode(buf.getvalue()).decode())
    assert not (root / "evil").exists() and not (tmp_path / "escape").exists()
    assert [m["name"] for m in modlib.list_mods(root)] == ["fix"]
    assert modlib.remove_mod(root, "fix") and modlib.list_mods(root) == []


@pytest.mark.skipif(os.name != "posix", reason="executable permission bits require a POSIX filesystem")
def test_mod_install_makes_run_script_executable(tmp_path):
    modlib.install_mod(tmp_path, "fix", _zip({"run.sh": "echo a\n"}))
    assert (tmp_path / "fix" / "run.sh").stat().st_mode & 0o111


# ---- metrics -----------------------------------------------------------------------------
def test_labeled_v1_metrics_are_summed_and_derived():
    text = """
vllm:num_requests_running{engine="0",model_name="m"} 2
vllm:num_requests_running{engine="1",model_name="m"} 1
vllm:kv_cache_usage_perc{engine="0"} 0.5
vllm:generation_tokens_total{engine="0"} 1000
vllm:spec_decode_num_drafts_total{engine="0"} 100
vllm:spec_decode_num_draft_tokens_total{engine="0"} 500
vllm:spec_decode_num_accepted_tokens_total{engine="0"} 300
vllm:time_to_first_token_seconds_bucket{le="1"} 9
vllm:time_to_first_token_seconds_sum 2.0
vllm:time_to_first_token_seconds_count 4
"""
    v = _parse_text(text)
    assert v["requests-running"] == 3 and v["kv-cache-usage"] == 0.5
    d = derive(v)
    assert d["spec_mean_accept_len"] == 4.0 and d["spec_accept_rate_pct"] == 60.0
    assert d["avg_ttft_ms"] == 500.0 and d["kv_cache_usage_pct"] == 50.0
    s = MetricsSampler()
    s.add({"ok": True, "ts": 100.0, "gen-tokens-total": 1000.0}, "r1")
    p = s.add({"ok": True, "ts": 110.0, "gen-tokens-total": 1500.0}, "r1")
    assert p["decode_tok_s"] == 50.0
    assert s.add({"ok": True, "ts": 111.0}, "r2") and len(s.samples) == 1   # new revision resets
    m = combine_snapshots([{"ok": True, "ts": 1, "base_url": "a", "kv-cache-usage": 0.2,
                            "requests-running": 1.0},
                           {"ok": True, "ts": 2, "base_url": "b", "kv-cache-usage": 0.4,
                            "requests-running": 2.0}])
    assert m["requests-running"] == 3.0 and abs(m["kv-cache-usage"] - 0.3) < 1e-9


# ---- gateway ------------------------------------------------------------------------------
async def test_gateway_fails_over_to_the_healthy_backend():
    hits = []

    async def handler(request: httpx.Request):
        hits.append(request.url.host)
        if request.url.host == "dead":
            raise httpx.ConnectError("refused")
        body = json.dumps({"host": request.url.host}).encode()
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(body))   # unread, like a real socket

    g = Gateway(None, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    g.set_route("default", ["http://dead:1", "http://live:1"], "m", max_model_len=32768)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(g)),
                               base_url="http://gw")
    for _ in range(3):
        r = await client.post("/v1/chat/completions", json={"model": "default"})
        assert r.status_code == 200 and r.json()["host"] == "live"
    models = (await client.get("/v1/models")).json()["data"]
    assert models[0]["max_model_len"] == 32768
    health = await client.get("/health")
    assert health.status_code == 200
    g.mark_down("default", "container exited")
    r = await client.post("/v1/chat/completions", json={"model": "default"})
    assert r.status_code == 503 and "container exited" in r.text
    g.mark_up("default")
    assert (await client.post("/v1/chat/completions", json={"model": "default"})).status_code == 200


# ---- watchdog / cancel -------------------------------------------------------------------
async def test_watchdog_recovers_a_crashed_deployment(cluster):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.TP2))
    await cluster.wait_job((await c.activate("qwen")).job_id)
    worker = next(n for n in cluster.runtimes["B"].containers)
    cluster.runtimes["B"].containers[worker].status = "exited"
    cluster.runtimes["B"].containers[worker].exit_code = 137
    problem = await c.watchdog_tick()
    assert "exited with code 137" in problem
    assert c.store.kv_get("last_incident")["action"].startswith("restarting")
    await cluster.wait_idle()
    jobs = c.store.list_jobs(limit=5)
    assert jobs[0].kind == "recovery" and jobs[0].state.value == "completed"
    assert await c.watchdog_tick() is None
    assert cluster.gateway.routes["default"].down_reason is None


async def test_cancel_before_stop_keeps_the_old_model(cluster):
    c = cluster.controller
    c.create_profile(draft("qwen", Topology.SINGLE_A))
    c.create_profile(draft("glm", Topology.TP2, repo="org/model-c"))
    await cluster.wait_job((await c.activate("qwen")).job_id)
    job = await c.activate("glm")
    assert c.cancel_job(job.job_id)["cancelled"]
    job = await cluster.wait_job(job.job_id)
    assert job.state.value == "failed" and "cancel" in (job.error or "").lower()
    assert c.active()["profile"] == "qwen"
    assert cluster.gateway.routes["default"].served_model == "qwen"


# ---- diagnostics helpers -----------------------------------------------------------------
def _fake_sysfs(root: Path, dev: str, ndev: str, ip_hex: str, active=True):
    port = root / dev / "ports" / "1"
    (port / "gids").mkdir(parents=True)
    (port / "gid_attrs" / "types").mkdir(parents=True)
    (port / "gid_attrs" / "ndevs").mkdir(parents=True)
    (port / "state").write_text("4: ACTIVE\n" if active else "1: DOWN\n")
    (port / "rate").write_text("200 Gb/sec (4X HDR)\n")
    (port / "link_layer").write_text("Ethernet\n")
    (root / dev / "device" / "net" / ndev).mkdir(parents=True)
    for i, (gid, typ) in enumerate([("fe80:0000:0000:0000:0000:0000:0000:0001", "IB/RoCE v1"),
                                    ("fe80:0000:0000:0000:0000:0000:0000:0001", "RoCE v2"),
                                    (f"0000:0000:0000:0000:0000:ffff:{ip_hex}", "IB/RoCE v1"),
                                    (f"0000:0000:0000:0000:0000:ffff:{ip_hex}", "RoCE v2")]):
        (port / "gids" / str(i)).write_text(gid + "\n")
        (port / "gid_attrs" / "types" / str(i)).write_text(typ + "\n")
        (port / "gid_attrs" / "ndevs" / str(i)).write_text(ndev + "\n")


def test_rdma_discovery_suggests_both_pcie_halves(tmp_path):
    _fake_sysfs(tmp_path, "rocep1s0f1", "enp1s0f1np1", "c0a8:6401")        # 192.168.100.1
    _fake_sysfs(tmp_path, "roceP2p1s0f1", "enP2p1s0f1np1", "c0a8:6403")    # 192.168.100.3
    _fake_sysfs(tmp_path, "rocep1s0f0", "enp1s0f0np0", "0a00:0001", active=False)
    devs = rdma_devices(str(tmp_path))
    sug = suggest_rdma(devs, "192.168.100.1")
    assert sug == {"hcas": ["roceP2p1s0f1", "rocep1s0f1"], "gid_index": 3, "note": ""}
    one = suggest_rdma([d for d in devs if d["hca"] == "rocep1s0f1"], "192.168.100.1")
    assert "100 Gb/s" in one["note"]


def test_parse_ib_write_bw():
    out = """
 #bytes     #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
 65536      1830000          0.00               97.61                0.186182
"""
    assert parse_ib_write_bw(out) == 97.61
    assert parse_ib_write_bw("nothing") is None


def test_community_source_listing():
    assert parse_source("eugr/spark-vllm-docker:recipes@main") == (
        "eugr", "spark-vllm-docker", "recipes", "main")

    def handler(request: httpx.Request):
        assert request.url.path == "/repos/eugr/spark-vllm-docker/contents/recipes"
        return httpx.Response(200, json=[
            {"type": "file", "name": "glm.yaml", "path": "recipes/glm.yaml", "size": 10,
             "html_url": "https://github.com/x", "download_url": "https://raw/x", "sha": "1"},
            {"type": "file", "name": "README.md", "path": "recipes/README.md"},
            {"type": "dir", "name": "old", "path": "recipes/old"}])

    out = list_source("eugr/spark-vllm-docker:recipes", httpx.Client(
        transport=httpx.MockTransport(handler)), refresh=True)
    assert [f["name"] for f in out["files"]] == ["glm.yaml"]


def test_cookbook_import_recipe_route_preview(cluster):
    from fastapi.testclient import TestClient

    from twinspark.controller.app import create_app

    text = Path("twinspark/cookbook/recipes/deepseek-v4-flash-0731-b12x.yaml").read_text()
    h = {"x-api-key": "k"}
    with TestClient(create_app(cluster.controller, "k", run_startup=False)) as client:
        r = client.post("/api/v1/cookbook/import-recipe", headers=h,
                        json={"text": text, "preview": True, "profile_name": "ds"})
        assert r.status_code == 200 and r.json()["preview"] and "--moe-backend" in r.json()["report"]["raw"]
        assert cluster.controller.get_profile("ds") is None
        r = client.post("/api/v1/cookbook/import-recipe", headers=h,
                        json={"text": text, "profile_name": "ds"})
        assert r.status_code == 200 and cluster.controller.get_profile("ds") is not None
        r = client.post("/api/v1/cookbook/import-recipe", headers=h, json={"text": "a: [", })
        assert r.status_code == 422
        r = client.post("/api/v1/cookbook/import-recipe", headers=h, json={"url": "http://x/y.yaml"})
        assert r.status_code == 422 and "https" in r.json()["detail"]
        r = client.get("/api/v1/profiles/ds/fit", headers=h)
        assert r.status_code == 200 and r.json()["known"] is False
        r = client.post("/api/v1/mods", headers=h, json={"name": "m1", "archive_b64": "@@"})
        assert r.status_code == 422
        r = client.post("/api/v1/mods", headers=h,
                        json={"name": "m1", "archive_b64": _zip({"run.sh": "echo\n"})})
        assert r.status_code == 200 and r.json()["consistent"]
        assert client.get("/api/v1/mods", headers=h).json()["mods"][0]["name"] == "m1"
        r = client.post("/api/v1/system/foreign/stop", headers=h,
                        json={"node": "A", "name": "vllm_node", "confirm": "vllm_node"})
        assert r.status_code == 422                     # not running → clean error
        cluster.runtimes["A"].foreign = [{"name": "vllm_node", "image": "vllm-node", "status": "running"}]
        r = client.post("/api/v1/system/foreign/stop", headers=h,
                        json={"node": "A", "name": "vllm_node", "confirm": "vllm_node"})
        assert r.status_code == 200 and r.json()["stopped"] == "vllm_node"

