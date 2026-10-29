"""Node A copies weights to node B over SSH with B's host key pinned, not taken on trust."""

from __future__ import annotations

import os

import pytest

from twinspark.agent import transfers
from twinspark.agent.actions import ActionError, AgentActions
from twinspark.agent.runtime import DryRunRuntime
from twinspark.controller.staging import peer_host_keys
from twinspark.schemas.config import AgentConfig, NodeIdentity, RuntimeSettings

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
EC = ("ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5"
      "YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=")


def rt(tmp_path, **kw) -> RuntimeSettings:
    return RuntimeSettings(hf_cache_dir=str(tmp_path / "hf"), ssh_key=str(tmp_path / "ssh" / "id_ed25519"), **kw)


def test_own_host_keys_are_read_without_comments(tmp_path):
    (tmp_path / "ssh_host_ed25519_key.pub").write_text(ED + " root@spark-b\n")
    (tmp_path / "ssh_host_ecdsa_key.pub").write_text(EC + "\n")
    (tmp_path / "ssh_host_rsa_key.pub").write_text("garbage\n")
    assert transfers.own_host_keys(tmp_path) == [ED, EC]


def test_pinning_replaces_that_host_only_and_ssh_then_accepts_nothing_else(tmp_path):
    r = rt(tmp_path)
    kh = tmp_path / "ssh" / "known_hosts"
    kh.parent.mkdir()
    kh.write_text(f"10.0.0.2 ssh-rsa {'A' * 60}\nother-host {ED}\n")
    path = transfers.pin_host_keys(r, "10.0.0.2", [ED + " comment", EC])
    assert path == kh
    lines = kh.read_text().splitlines()
    assert lines == [f"other-host {ED}", f"10.0.0.2 {ED}", f"10.0.0.2 {EC}"]
    if os.name != "nt":
        assert oct(kh.stat().st_mode & 0o777) == "0o600"
    cmd = transfers.ssh_command(r, pinned=True)
    assert "StrictHostKeyChecking=yes" in cmd and f"UserKnownHostsFile={kh}" in cmd
    assert "StrictHostKeyChecking=accept-new" in transfers.ssh_command(r)       # without keys: as before
    argv = transfers.rsync_argv(r, tmp_path / "hub", "spark", "10.0.0.2", "/hf/hub", pinned=True)
    assert "StrictHostKeyChecking=yes" in argv[argv.index("-e") + 1]


@pytest.mark.parametrize("keys,host", [([], "10.0.0.2"), (["ssh-dss AAAA"], "10.0.0.2"),
                                       ([ED + "\n10.0.0.9 " + ED], "10.0.0.2"), ([ED], "-oProxyCommand=x"),
                                       ([ED], "a b")])
def test_pinning_refuses_anything_that_is_not_a_host_key(tmp_path, keys, host):
    with pytest.raises(ValueError):
        transfers.pin_host_keys(rt(tmp_path), host, keys)


def test_the_agent_passes_pinned_keys_to_its_ssh_check_and_sync(tmp_path):
    cfg = AgentConfig(node=NodeIdentity(node_id="A", role="agent"), runtime=rt(tmp_path),
                      secrets_dir=str(tmp_path / "secrets"))
    a = AgentActions(cfg, runtime=DryRunRuntime())
    out = a.ssh_check({"target_host": "10.0.0.2", "ssh_user": "spark", "host_keys": [ED]})
    assert out["pinned"] and "StrictHostKeyChecking=yes" in out["argv"]
    assert not a.ssh_check({"target_host": "10.0.0.2", "ssh_user": "spark"})["pinned"]
    with pytest.raises(ActionError):
        a.ssh_check({"target_host": "10.0.0.2", "ssh_user": "spark", "host_keys": "ssh-ed25519 x"})
    assert a.ssh_host_keys({}) == {"keys": [], "dry_run": True}


async def test_the_controller_asks_the_target_agent_and_tolerates_an_old_one():
    class New:
        async def call(self, action, timeout=None, **kw):
            assert action == "ssh_host_keys"
            return {"keys": [ED, 42, EC]}

    class Old:
        async def call(self, action, timeout=None, **kw):
            raise RuntimeError("unknown action: ssh_host_keys")

    assert await peer_host_keys(New()) == [ED, EC]
    assert await peer_host_keys(Old()) == []
