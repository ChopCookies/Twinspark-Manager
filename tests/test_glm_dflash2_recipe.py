"""The GLM DFlash2 recipe follows its author's launcher, names its patch, and plans show both startup clocks."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from twinspark import cli
from twinspark.controller.launch import VLLM_ENGINE_READY_DEFAULT_S, startup_limits
from twinspark.cookbook import build_draft
from twinspark.schemas.enums import Topology

from .conftest import draft

TARGET = "/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/sparse_attn_indexer_kpool.py"


def test_dflash2_matches_the_cited_launcher():
    d = build_draft("glm-5.3-flash-nvfp4-dflash2-tp2")
    a = d.advanced
    assert a.max_num_batched_tokens == 8192 and a.kv_dtype == "fp8_e4m3" and a.block_size == 2304
    assert a.max_num_seqs == 6 and a.gpu_memory_utilization == 0.85 and a.eager_mode
    assert a.env["VLLM_ENGINE_READY_TIMEOUT_S"] == "3600"
    assert a.mods == ["glm53-sm121-sparse-attn"]                      # the SM121 top-k fix is required
    assert not any(k in a.env for k in ("NCCL_IB_HCA", "NCCL_SOCKET_IFNAME", "VLLM_HOST_IP"))   # node wiring
    req = " ".join(d.source["requirements"])
    assert "tsm mods file-patch glm53-sm121-sparse-attn" in req and TARGET in req and "swappiness=0" in req


async def test_the_plan_names_the_patch_and_both_startup_limits(cluster):
    c = cluster.controller
    d = build_draft("glm-5.3-flash-nvfp4-dflash2-tp2")
    pinned = draft("glm", Topology.TP2, repo=d.simple.model)
    d.identity = pinned.identity
    d.name = "glm"
    c.create_profile(d)
    rev = c.get_profile("glm").add_revision(d, d.identity)
    plan = c.launch_plan(rev)
    assert plan.startup_limits["engine_ready_timeout_s"] == 3600
    assert plan.startup_limits["health_timeout_s"] == c.config.runtime.health_timeout_s
    note = next(n for n in plan.notes if n.startswith("startup limits:"))
    assert "vLLM engine ready 3600 s (VLLM_ENGINE_READY_TIMEOUT_S)" in note and "runtime.health_timeout_s" in note
    assert any("glm53-sm121-sparse-attn" in n for n in plan.notes)
    cmds = " ".join(plan.rendered_commands().values())
    assert "--max-num-batched-tokens 8192" in cmds and "VLLM_ENGINE_READY_TIMEOUT_S=3600" in cmds


def test_a_long_documented_boot_without_an_engine_limit_is_flagged():
    quiet = startup_limits({}, 2400)
    assert quiet["engine_ready_timeout_s"] is None and "warning" not in quiet
    assert f"{VLLM_ENGINE_READY_DEFAULT_S} s (image default)" in quiet["note"] and quiet["ends_first"] == "vLLM"
    slow = startup_limits({}, 2400, documented_boot="~13-15 min (checkpoint load dominates)")
    assert "VLLM_ENGINE_READY_TIMEOUT_S" in slow["warning"]
    ok = startup_limits({"VLLM_ENGINE_READY_TIMEOUT_S": "3600"}, 2400, documented_boot="~15 min")
    assert "warning" not in ok and ok["ends_first"] == "TwinSpark"
    tight = startup_limits({"VLLM_ENGINE_READY_TIMEOUT_S": "3600"}, 900, documented_boot="~15 min")
    assert "runtime.health_timeout_s" in tight["warning"]


def test_file_patch_mod_replaces_one_file_and_refuses_a_wrong_image(tmp_path):
    src = tmp_path / "sparse_attn_indexer_kpool_sm121.py"
    src.write_text("PATCHED = True\n")
    image = tmp_path / "image"
    target = image / "vllm" / "layers" / "sparse_attn_indexer_kpool.py"
    target.parent.mkdir(parents=True)
    target.write_text("PATCHED = False\n")
    d = cli.file_patch_mod("glm53-sm121-sparse-attn", src, str(target), tmp_path / "mods")
    run = d / "run.sh"
    assert os.access(run, os.X_OK) and (d / src.name).read_text() == "PATCHED = True\n"
    assert "sha256" in run.read_text()
    if not shutil.which("bash"):
        pytest.skip("needs bash to run the mod")
    r = subprocess.run(["bash", "run.sh"], cwd=d, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and target.read_text() == "PATCHED = True\n" and "replaced" in r.stdout
    other = cli.file_patch_mod("x", src, str(tmp_path / "no-such-dir" / "f.py"), tmp_path / "mods2")
    r = subprocess.run(["bash", "run.sh"], cwd=other, capture_output=True, text=True, timeout=10)
    assert r.returncode == 1 and "is this the image the patch was made for" in r.stderr
    typo = cli.file_patch_mod("y", src, str(target.parent / "sparse_attn_indexer_kpol.py"), tmp_path / "mods3")
    r = subprocess.run(["bash", "run.sh"], cwd=typo, capture_output=True, text=True, timeout=10)
    assert r.returncode == 1 and not (target.parent / "sparse_attn_indexer_kpol.py").exists()   # no new file
    named_run = tmp_path / "run.sh"
    named_run.write_text("echo hi\n")
    with pytest.raises(SystemExit, match="rename run.sh"):
        cli.file_patch_mod("z", named_run, str(target), tmp_path / "mods4")
    for bad_target in ("relative/path.py", "/opt/../etc/x"):
        with pytest.raises(SystemExit):
            cli.file_patch_mod("x", src, bad_target, tmp_path / "m")
    with pytest.raises(SystemExit):
        cli.file_patch_mod("bad name!", src, str(target), tmp_path / "m")


def test_file_patch_out_writes_a_reviewable_directory(tmp_path, capsys):
    src = tmp_path / "f.py"
    src.write_text("x = 1\n")
    assert cli.main(["--key=k", "mods", "file-patch", "fix-1", "--file", str(src), "--target", TARGET,
                     "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "fix-1" / "run.sh").is_file()
    assert "tsm mods install" in capsys.readouterr().out
    assert Path(tmp_path / "out" / "fix-1" / "f.py").read_text() == "x = 1\n"


def test_pin_compares_the_image_with_the_digest_the_recipe_was_written_for():
    from twinspark.controller.pinning import recipe_digest_note
    d = build_draft("glm-5.3-flash-nvfp4-dflash2-tp2")
    name, want = "ghcr.io/tonyd2wild/vllm-glm53-flash", d.source["image_digest"]
    same = recipe_digest_note(d, {"image": name, "digest": want, "source": "registry"})
    assert same.startswith("image digest matches")
    moved = recipe_digest_note(d, {"image": name, "digest": "sha256:" + "1" * 64, "source": "registry"})
    assert moved.startswith("WARNING:") and f"--image {name}@{want}" in moved
    assert recipe_digest_note(d, {"image": "other/image", "digest": "x", "source": "registry"}) is None
    assert recipe_digest_note(d, {"image": name, "digest": "x", "source": "local"}) is None
    assert recipe_digest_note(build_draft("glm-5.3-flash-nvfp4-tp2"),
                              {"image": name, "digest": "x", "source": "registry"}) is None
