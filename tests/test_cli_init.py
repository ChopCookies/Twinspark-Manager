"""``tsm init``: ``--show`` only reads; creating keys is explicit."""

from __future__ import annotations

import pytest

from twinspark import cli
from twinspark.security import SecretsVault


def run(*argv):
    return cli.main([str(a) for a in argv])


def test_show_on_a_missing_vault_creates_nothing(tmp_path):
    sec = tmp_path / "nowhere" / "secrets"
    with pytest.raises(SystemExit, match="no secrets in"):
        run("--secrets-dir", sec, "init", "--show")
    assert not sec.exists() and not sec.parent.exists()


def test_show_prints_existing_keys_and_creates_none(tmp_path, capsys):
    sec = tmp_path / "secrets"
    run("--secrets-dir", sec, "init")
    out = capsys.readouterr().out
    assert "created" in out and "sudo tsm join-code" in out and "tsm init --role agent" not in out
    files = sorted(p.name for p in sec.iterdir())
    run("--secrets-dir", sec, "init", "--show")
    out = capsys.readouterr().out
    assert SecretsVault(sec).get("management_api_key") in out and "created" not in out
    assert sorted(p.name for p in sec.iterdir()) == files


def test_show_on_node_b_lists_what_it_has(tmp_path, capsys):
    sec = tmp_path / "secrets"
    v = SecretsVault(sec)
    v.set("agent_token", "tok-123")
    v.set("backend_api_key", "bk-456")
    run("--secrets-dir", sec, "init", "--show")
    out = capsys.readouterr().out
    assert "tok-123" in out and "bk-456" in out and "(not on this node)" in out
    assert not (sec / "management_api_key.enc").exists()


def test_running_init_twice_keeps_the_keys(tmp_path, capsys):
    sec = tmp_path / "secrets"
    run("--secrets-dir", sec, "init")
    first = SecretsVault(sec).get("management_api_key")
    capsys.readouterr()
    run("--secrets-dir", sec, "init")
    assert SecretsVault(sec).get("management_api_key") == first
    assert "join-code" not in capsys.readouterr().out          # nothing new to hand to node B


def test_show_with_a_lost_master_key_creates_no_new_one(tmp_path):
    sec = tmp_path / "secrets"
    run("--secrets-dir", sec, "init")
    (sec / ".master.key").unlink()
    with pytest.raises(SystemExit, match="missing or empty"):
        run("--secrets-dir", sec, "init", "--show")
    assert not (sec / ".master.key").exists()


def test_first_night_is_hidden_and_needs_no_controller(capsys):
    from twinspark import cli
    assert "first-night" not in cli.build_parser().format_help()
    assert cli.main(["first-night"]) == 0                       # no API, no key: runs anywhere
    assert "node B was still in dry-run" in capsys.readouterr().out
