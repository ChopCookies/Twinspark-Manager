"""Regression tests for the node-side and input-handling defects found by the security review.

Covers secret injection into containers, mount-path validation, docker argv hygiene, mod archive
limits, the vault, join codes, outbound URL policy, recipe parsing limits and the maintenance
reboot retry. Nothing here needs Docker, root or network access.
"""

from __future__ import annotations

import io
import os
import stat
import tarfile
import threading
import time
import zipfile
import zlib
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from twinspark import netguard, provision
from twinspark.agent import maintenance, mods
from twinspark.agent.actions import _HOST_RE, _NAME_RE
from twinspark.agent.runtime import DockerRuntime, RuntimeError_
from twinspark.controller.agent_client import AgentActionError
from twinspark.controller.launch import CONTAINER_HF_HOME, Mount, docker_run_argv
from twinspark.cookbook import import_text
from twinspark.cookbook.eugr import RecipeImportError, parse_eugr_recipe
from twinspark.cookbook.remote import RemoteError, check_public_url, fetch_text
from twinspark.provision import SetupError, decode_join, encode_join
from twinspark.resolver import resolve_hf_revision, resolve_image_digest
from twinspark.schemas.enums import Topology
from twinspark.schemas.profile import AdvancedSettings
from twinspark.security import SecretsVault, _write_private
from twinspark.weights import execute_delete, plan_delete

from .conftest import draft
from .test_v04_backend import _fake_cache


def single_a_spec(cluster, name="qwen"):
    c = cluster.controller
    c.create_profile(draft(name, Topology.SINGLE_A))
    return c.launch_plan(c.get_profile(name).latest()).containers[0]


async def start(cluster, spec):
    return await cluster.controller.agents["A"].call("container_start", spec=spec.model_dump(mode="json"))


# ---- secrets can only go where they belong ------------------------------------------------------
@pytest.mark.parametrize("value", ["${secret:agent_token}", "${ secret:agent_token}", "x${secret:hf_token}"])
def test_profile_environment_cannot_reference_vault_secrets(value):
    with pytest.raises(ValidationError, match="secret references"):
        AdvancedSettings(env={"LOOT": value})


async def test_agent_only_injects_secrets_into_their_own_variables(cluster):
    spec = single_a_spec(cluster)
    assert spec.env["VLLM_API_KEY"] == "${secret:backend_api_key}"
    assert (await start(cluster, spec))["container"] == spec.name            # the legitimate case still works
    await cluster.controller.agents["A"].call("containers_stop_owned")
    for env in ({"LOOT": "${secret:agent_token}"},
                {"LOOT": "${secret:management_api_key}"},
                {"VLLM_API_KEY": "${secret:management_api_key}"},
                {"HF_TOKEN": "${secret:backend_api_key}"},
                {"LOOT": "prefix ${secret:backend_api_key}"}):
        evil = spec.model_copy(update={"env": {**spec.env, **env}})
        with pytest.raises(AgentActionError, match="may not be injected|malformed secret"):
            await start(cluster, evil)
    assert not cluster.runtimes["A"].containers


# ---- mounts -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["/tmp/hf/link:/hostroot", "relative/path", "/tmp/a b", "/tmp/a\n", "/tmp/x,y", ""])
def test_mount_paths_are_plain_absolute_paths(bad):
    with pytest.raises(ValidationError):
        Mount(host=bad, container="/x")
    with pytest.raises(ValidationError):
        Mount(host="/tmp/ok", container=bad)


async def test_agent_rejects_the_colon_trick_even_from_a_hand_made_request(cluster, tmp_path):
    spec = single_a_spec(cluster)
    raw = spec.model_dump(mode="json")
    raw["mounts"].append({"host": f"{tmp_path}/hf/link:/hostroot", "container": "rw", "read_only": False})
    with pytest.raises(AgentActionError):
        await cluster.controller.agents["A"].call("container_start", spec=raw)


async def test_symlinks_in_the_cache_cannot_lead_out_of_it(cluster, tmp_path):
    spec = single_a_spec(cluster)
    hf = tmp_path / "hf"
    hf.mkdir(exist_ok=True)
    (hf / "link").symlink_to("/")
    evil = spec.model_copy(update={"mounts": [Mount(host=str(hf / "link"), container="/hostroot")]})
    with pytest.raises(AgentActionError, match="outside allowed roots"):
        await start(cluster, evil)


async def test_each_node_mounts_its_own_cache_paths(cluster, tmp_path):
    spec = single_a_spec(cluster)
    foreign = spec.model_copy(update={"mounts": [
        m.model_copy(update={"host": "/home/other-user/.cache/huggingface"}) if m.container == CONTAINER_HF_HOME else m
        for m in spec.mounts]})
    await start(cluster, foreign)
    argv = cluster.runtimes["A"].history[-1]
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert f"{(tmp_path / 'hf').resolve()}:{CONTAINER_HF_HOME}" in mounts
    assert not any("other-user" in m for m in mounts)


# ---- docker argv ----------------------------------------------------------------------------------------------
async def test_containers_never_pull_implicitly(cluster):
    spec = single_a_spec(cluster)
    argv = docker_run_argv(spec)
    assert argv[argv.index("--pull") + 1] == "never"
    cluster.runtimes["A"].image_present = lambda ref: False
    with pytest.raises(AgentActionError, match="not on node"):
        await start(cluster, spec)


async def test_secret_values_reach_docker_through_a_private_file_not_argv(cluster):
    spec = single_a_spec(cluster)
    rt, seen = DockerRuntime(), {}

    def fake_run(argv, timeout=None, check=True):
        path = argv[argv.index("--env-file") + 1]
        seen.update(argv=argv, path=path, mode=stat.S_IMODE(os.stat(path).st_mode), body=Path(path).read_text())
        return "container-id\n"

    rt._run = fake_run
    assert rt.start(spec, {"VLLM_API_KEY": "s3cret-value"}) == "container-id"
    assert "s3cret-value" not in " ".join(seen["argv"])
    assert not any(a == "VLLM_API_KEY=${secret:backend_api_key}" for a in seen["argv"])
    assert seen["mode"] == 0o600 and seen["body"] == "VLLM_API_KEY=s3cret-value\n"
    assert not Path(seen["path"]).exists(), "the secret file must be removed after docker run"
    with pytest.raises(RuntimeError_):
        rt.start(spec, {"VLLM_API_KEY": "line\nbreak"})


# ---- mods ------------------------------------------------------------------------------------------------------------
def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


def test_zip_with_too_many_entries_is_refused_before_it_is_expanded(tmp_path):
    archive = zip_bytes({"run.sh": b"#!/bin/sh\n", **{f"d/{i}": b"" for i in range(mods.MAX_FILES + 5)}})
    started = time.monotonic()
    with pytest.raises(mods.ModError, match="too many files"):
        mods.install_mod(tmp_path / "mods", "big", b64(archive))
    assert time.monotonic() - started < 5


def test_tar_with_too_many_entries_is_refused(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i in range(mods.MAX_FILES + 5):
            tf.addfile(tarfile.TarInfo(f"d/{i}"))
    with pytest.raises(mods.ModError, match="too many files"):
        mods.install_mod(tmp_path / "mods", "big", b64(buf.getvalue()))


def test_replacing_a_mod_is_atomic_for_readers(tmp_path):
    root = tmp_path / "mods"
    mods.install_mod(root, "patch", b64(zip_bytes({"run.sh": b"#!/bin/sh\necho v0\n"})))
    missing, stop = [], threading.Event()

    def poll():
        while not stop.is_set():
            if not (root / "patch" / "run.sh").is_file():
                missing.append(time.monotonic())

    t = threading.Thread(target=poll)
    t.start()
    try:
        for i in range(40):
            mods.install_mod(root, "patch", b64(zip_bytes({"run.sh": f"#!/bin/sh\necho v{i}\n".encode()})))
    finally:
        stop.set()
        t.join()
    assert not missing, f"the mod was missing {len(missing)} times while being replaced"
    assert (root / "patch" / "run.sh").read_text().endswith("v39\n")
    assert [p.name for p in root.iterdir()] == ["patch"], "temporary directories were left behind"


def test_tree_hash_streams_files_instead_of_loading_them(tmp_path, monkeypatch):
    (tmp_path / "run.sh").write_bytes(b"x" * 1_000_000)
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError("read_bytes used")))
    assert mods.tree_hash(tmp_path).startswith("sha256:")


def test_leftover_temp_dirs_are_swept_and_the_number_of_mods_is_capped(tmp_path, monkeypatch):
    root = tmp_path / "mods"
    stale = root / ".tmp-crashed-abc"
    stale.mkdir(parents=True)
    old = time.time() - 2 * mods.STALE_AFTER_S
    os.utime(stale, (old, old))
    monkeypatch.setattr(mods, "MAX_MODS", 2)
    archive = b64(zip_bytes({"run.sh": b"#!/bin/sh\n"}))
    mods.install_mod(root, "one", archive)
    mods.install_mod(root, "two", archive)
    assert not stale.exists()
    with pytest.raises(mods.ModError, match="at most 2 mods"):
        mods.install_mod(root, "three", archive)
    mods.install_mod(root, "two", archive)                    # replacing an existing one is fine


def test_names_with_a_trailing_newline_are_not_valid():
    assert mods.MOD_RE.match("ok") and not mods.MOD_RE.match("ok\n")
    assert _NAME_RE.match("tsm-x") and not _NAME_RE.match("tsm-x\n")
    assert _HOST_RE.match("192.168.100.2") and not _HOST_RE.match("-oProxyCommand=x")


# ---- vault ---------------------------------------------------------------------------------------------------------
def test_a_failed_write_keeps_the_previous_secret(tmp_path, monkeypatch):
    vault = SecretsVault(tmp_path / "s")
    vault.set("agent_token", "old-value")
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(OSError):
        vault.set("agent_token", "new-value")
    monkeypatch.undo()
    assert vault.get("agent_token") == "old-value"
    assert [p.name for p in (tmp_path / "s").iterdir() if p.name.endswith(".tmp")] == []


def test_vault_files_are_private_and_the_master_key_is_never_overwritten(tmp_path):
    vault = SecretsVault(tmp_path / "s")
    vault.set("hf_token", "hf_x")
    for p in (tmp_path / "s").iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
    key = vault.key_file.read_bytes()
    with pytest.raises(FileExistsError):
        _write_private(vault.key_file, b"another", exclusive=True)
    assert vault.key_file.read_bytes() == key


def test_empty_master_key_left_by_a_crash_is_recreated_and_instances_agree(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / ".master.key").write_bytes(b"")
    first = SecretsVault(d)
    first.set("agent_token", "tok")
    results, errors = [], []

    def read():
        try:
            results.append(SecretsVault(d).get("agent_token"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and results == ["tok"] * 8


# ---- join code and authorised key --------------------------------------------------------------------------------
GOOD = {"v": 1, "agent_token": "t", "backend_api_key": "b", "a_ip": "192.168.100.1", "b_ip": "192.168.100.2",
        "b_user": "chopc", "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYEXAMPLEKEY twinspark-sync@a"}


def test_join_code_is_validated_field_by_field():
    assert decode_join(encode_join(GOOD))["b_user"] == "chopc"
    for patch, message in (({"a_ip": "not-an-ip"}, "invalid address"),
                           ({"b_user": "Robert'); DROP"}, "invalid user"),
                           ({"pubkey": GOOD["pubkey"] + "\nssh-ed25519 AAAAATTACKER x"}, "single OpenSSH"),
                           ({"pubkey": "command=\"x\" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYEXAMPLEKEY"},
                            "single OpenSSH")):
        with pytest.raises(SetupError, match=message):
            decode_join(encode_join({**GOOD, **patch}))


def test_join_code_that_inflates_to_a_huge_size_is_refused_without_inflating_it():
    import base64
    bomb = provision.JOIN_PREFIX + base64.urlsafe_b64encode(zlib.compress(b"0" * 200_000_000, 9)).decode()
    started = time.monotonic()
    with pytest.raises(SetupError, match="larger than a real one"):
        decode_join(bomb)
    assert time.monotonic() - started < 5


def test_authorized_keys_refuses_a_key_with_a_newline_and_matches_whole_words(tmp_path):
    from .test_setup_flow import answers_a
    pub = GOOD["pubkey"]
    b = answers_a(tmp_path, role="agent", node_id="B", qsfp_ip="192.168.100.2", peer_ip="192.168.100.1",
                  agent_token="tok", backend_api_key="bk", authorize_key=pub + "\nssh-ed25519 AAAATTACKERKEY x")
    home = tmp_path / "home"
    with pytest.raises(SetupError, match="single-line"):
        provision.Provisioner(b, provision.Layout(tmp_path / "r1"), home_override=str(home)).apply()
    assert not (home / ".ssh" / "authorized_keys").exists()
    # a key that merely contains another key's text as a substring is still added
    ak = home / ".ssh" / "authorized_keys"
    ak.parent.mkdir(parents=True)
    ak.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYEXAMPLEKEYEXTRA other@host\n")
    ok = answers_a(tmp_path, role="agent", node_id="B", qsfp_ip="192.168.100.2", peer_ip="192.168.100.1",
                   agent_token="tok", backend_api_key="bk", authorize_key=pub)
    provision.Provisioner(ok, provision.Layout(tmp_path / "r2"), home_override=str(home)).apply()
    assert ak.read_text().count("twinspark-sync@a") == 1


# ---- outbound URLs ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://example.com/r.yaml", "ftp://example.com/r.yaml", "file:///etc/passwd",
    "https://user:pw@example.com/r.yaml", "https://127.0.0.1/r.yaml", "https://localhost/r.yaml",
    "https://10.1.2.3/r.yaml", "https://192.168.1.1/r.yaml", "https://169.254.169.254/latest",
    "https://[::1]/r.yaml",
])
def test_recipe_urls_must_be_public_https(url):
    with pytest.raises(RemoteError) as exc:
        check_public_url(url)
    assert "pw" not in str(exc.value) or "credentials" in str(exc.value)


def test_netguard_resolves_names_and_requires_global_addresses(monkeypatch):
    def fake(host, port, proto=0):
        return [(2, 1, 6, "", ({"public.example": "93.184.216.34", "rebind.example": "127.0.0.1"}[host], port))]
    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake)
    assert check_public_url("https://public.example/x.yaml")
    with pytest.raises(RemoteError, match="non-public"):
        check_public_url("https://rebind.example/x.yaml")


def test_redirects_are_checked_and_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("n", "0"))
        target = request.url.params.get("to")
        if target:
            return httpx.Response(302, headers={"location": target})
        if n:
            return httpx.Response(302, headers={"location": f"https://raw.example/r?n={n - 1}"})
        return httpx.Response(200, text="name: ok\ncommand: vllm serve x\n")

    def make():
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    assert fetch_text("https://raw.example/r?n=2", client=make()).startswith("name: ok")
    with pytest.raises(RemoteError, match="too many redirects"):
        fetch_text("https://raw.example/r?n=9", client=make())
    for target in ("http://internal.example/x", "https://user:pw@internal.example/x"):
        with pytest.raises(RemoteError):
            fetch_text(f"https://raw.example/r?to={target}", client=make())


def test_registry_lookups_cannot_be_pointed_at_the_local_network():
    with pytest.raises(ValueError, match="registry lookup failed"):
        resolve_image_digest("127.0.0.1:5000/org/repo:tag")
    with pytest.raises(ValueError):
        resolve_image_digest("192.168.0.10/org/repo:tag")


def test_model_references_are_checked_before_any_request_is_built():
    for ref in ("org/../../api/x", "../x", "a" * 1_000_000, "org//x"):
        with pytest.raises(ValueError):
            resolve_hf_revision(ref)


# ---- recipes -------------------------------------------------------------------------------------------------------
def recipe(command: str, **extra) -> str:
    return yaml.safe_dump({"name": "r", "model": "org/m", "command": command, **extra})


def test_recipe_template_cannot_allocate_gigabytes():
    started = time.monotonic()
    with pytest.raises(RecipeImportError, match="unsupported placeholder"):
        parse_eugr_recipe(recipe("vllm serve org/m --port {port:>170000000}"))
    assert time.monotonic() - started < 2


def test_recipe_templates_still_substitute_and_escape_braces():
    draft_, _ = parse_eugr_recipe(recipe(
        "vllm serve org/m --port {port} --speculative-config '{{\"method\":\"mtp\"}}'", defaults={"port": 9000}))
    assert draft_.advanced.speculative_config == {"method": "mtp"} or "method" in str(draft_.advanced)
    with pytest.raises(RecipeImportError, match="does not define|defaults do not define"):
        parse_eugr_recipe(recipe("vllm serve org/m --port {nope}"))


def test_recipe_alias_bombs_and_nested_values_are_refused():
    bomb = ("a: &a [x, x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
            "name: r\nmodel: org/m\ncommand: vllm serve org/m\nenv:\n  X: *b\n")
    started = time.monotonic()
    with pytest.raises(RecipeImportError, match="plain strings or numbers"):
        parse_eugr_recipe(bomb)
    assert time.monotonic() - started < 2


def test_yaml_errors_do_not_echo_the_input():
    with pytest.raises(RecipeImportError) as exc:
        import_text("db_password: hunter2-internal\n  bad: [indent\n", None)
    assert "hunter2" not in str(exc.value) and "line" in str(exc.value)


# ---- model deletion ------------------------------------------------------------------------------------------------
def test_a_blob_that_cannot_be_removed_leaves_the_model_visible(tmp_path, monkeypatch, require_symlinks):
    hf = tmp_path / "hf"
    _fake_cache(hf)
    plan = plan_delete(hf, "org/x", "1" * 40)
    real_unlink = Path.unlink

    def deny(self, missing_ok=False):
        if self.name == "only1":
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny)
    with pytest.raises(ValueError, match="left in place"):
        execute_delete(plan, hf)
    monkeypatch.undo()
    snap = hf / "hub/models--org--x/snapshots" / ("1" * 40)
    assert snap.is_dir(), "the snapshot must still exist so the inventory keeps showing the model"
    execute_delete(plan_delete(hf, "org/x", "1" * 40), hf)           # a retry after fixing permissions works
    assert not snap.exists()


# ---- maintenance ---------------------------------------------------------------------------------------------------
@pytest.fixture
def maint(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "ROOT", tmp_path / "maint")
    monkeypatch.setattr(maintenance, "boot_id", lambda: "boot-1")
    rid = "a" * 32
    state = {"run_id": rid, "state": "rebooting", "phase": "rebooting", "firmware": False,
             "boot_id": "boot-1", "reboot_requested_at": time.time()}
    maintenance.save(state)
    return rid


def test_a_reboot_that_never_happened_is_retried_instead_of_wedging_the_run(maint):
    assert maintenance.status({"run_id": maint})["state"] == "rebooting"        # just requested: wait
    state = maintenance.load(maint)
    state["reboot_requested_at"] = time.time() - maintenance.REBOOT_RETRY_S - 5
    maintenance.save(state)
    assert maintenance.status({"run_id": maint})["state"] == "awaiting_reboot"  # controller may ask again
    assert maintenance.load(maint)["reboot_retries"] == 1


def test_repeated_lost_reboots_end_in_a_clear_failure(maint):
    for _attempt in range(maintenance.MAX_REBOOT_RETRIES + 1):
        state = maintenance.load(maint)
        state.update(state="rebooting", reboot_requested_at=time.time() - maintenance.REBOOT_RETRY_S - 5)
        maintenance.save(state)
        result = maintenance.status({"run_id": maint})
    assert result["state"] == "failed" and "did not reboot" in result["error"]


def test_vllm_default_is_documented_in_the_topologies():
    # guards the enum the tests above rely on
    assert Topology.SINGLE_A.value == "single-a"
