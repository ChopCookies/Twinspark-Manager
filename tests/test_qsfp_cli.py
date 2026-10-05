"""``tsm qsfp …`` on the command line, against the fake Spark. Sandbox roots only; no command is run."""

from __future__ import annotations

import json

import pytest
import yaml

from tests.qsfp_fakes import MGMT, PRIMARY, SECONDARY, FakeNode
from twinspark import cli, cli_qsfp, qsfp


@pytest.fixture
def fake(tmp_path, monkeypatch):
    n = FakeNode(tmp_path)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    monkeypatch.setattr(qsfp, "_which", lambda name: f"/usr/sbin/{name}")
    return n


def run(fake, *argv, expect=0, capsys=None):
    args = ["qsfp", *map(str, argv)]
    if args[1] in ("status", "plan", "apply", "revert", "verify", "scan"):
        args += ["--root", str(fake.root), "--sysfs", str(fake.sysfs)]
    if expect == 0:
        assert cli.main(args) == 0
    else:
        with pytest.raises(SystemExit) as e:
            cli.main(args)
        assert e.value.code == expect or (expect == "msg" and isinstance(e.value.code, str))
        return e.value.code
    return capsys.readouterr().out if capsys else None


def test_status_on_a_machine_without_addresses_fails_and_says_how_to_fix_it(fake, capsys):
    code = run(fake, "status", expect=1)
    out = capsys.readouterr().out
    assert code == 1
    assert "port f1np1   cabled" in out and PRIMARY in out and SECONDARY in out and "no IPv4" in out
    assert "[FAIL] qsfp addresses" in out and "sudo tsm qsfp apply --node A" in out


def test_status_json_is_machine_readable(fake, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--json", "qsfp", "status", "--root", str(fake.root), "--sysfs", str(fake.sysfs)])
    data = json.loads(capsys.readouterr().out)
    assert data["ports"][0]["port"] == "f1np1" and {c["check"] for c in data["checks"]} >= {"qsfp link"}


def test_bare_qsfp_is_status(monkeypatch, tmp_path, capsys):
    n = FakeNode(tmp_path)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    monkeypatch.setattr(cli_qsfp, "_host", lambda args: n.host())
    with pytest.raises(SystemExit):
        cli.main(["qsfp"])
    assert "port f1np1" in capsys.readouterr().out


def test_plan_shows_the_file_and_changes_nothing(fake, capsys):
    out = run(fake, "plan", "--node", "A", capsys=capsys)
    assert "set   enp1s0f1np1" in out and "192.168.100.1/24" in out and "192.168.101.1/24" in out
    assert "the other Spark should answer on: 192.168.100.2, 192.168.101.2" in out
    assert "link-local: []" in out and "Looks safe" in out
    assert not list(fake.netplan_dir().glob("*.yaml"))


def test_plan_json(fake, capsys):
    cli.main(["--json", "qsfp", "plan", "--node", "B", "--root", str(fake.root), "--sysfs", str(fake.sysfs)])
    data = json.loads(capsys.readouterr().out)
    assert [e["cidr"] for e in data["entries"]] == ["192.168.100.2/24", "192.168.101.2/24"]
    assert data["problems"] == []


def test_plan_reports_blockers(fake, capsys):
    fake.default_dev = SECONDARY
    fake.ssh_clients = []
    assert "default route" in run(fake, "plan", "--node", "A", expect="msg")
    out = run(fake, "plan", "--node", "A", "--iface", PRIMARY, capsys=capsys)
    assert "default route" in out and "Not safe to apply yet" in out


def test_plan_explains_a_missing_node(fake, capsys):
    msg = run(fake, "plan", expect="msg")
    assert "say which node this is" in msg


def test_the_node_id_comes_from_agent_yaml_when_setup_was_run(fake, capsys):
    etc = fake.root / "etc/twinspark"
    etc.mkdir(parents=True)
    (etc / "agent.yaml").write_text(yaml.safe_dump({"node": {"node_id": "B", "role": "agent"}}))
    out = run(fake, "plan", capsys=capsys)
    assert "192.168.100.2/24" in out and "host .2" in out


def test_apply_in_a_sandbox_writes_the_file(fake, capsys):
    out = run(fake, "apply", "--node", "A", "--yes", capsys=capsys)
    assert "not applied because this is a sandbox root" in out
    doc = yaml.safe_load((fake.root / "etc/netplan/60-twinspark-qsfp.yaml").read_text())
    assert doc["network"]["ethernets"][SECONDARY]["addresses"] == ["192.168.101.1/24"]
    out = run(fake, "apply", "--node", "A", "--yes", capsys=capsys)
    assert "Already configured; nothing to change" in out


def test_apply_asks_first_and_cancel_changes_nothing(fake, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    msg = run(fake, "apply", "--node", "A", expect="msg")
    assert "cancelled" in msg and not list(fake.netplan_dir().glob("*.yaml"))


def test_apply_without_a_terminal_needs_yes(fake, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    msg = run(fake, "apply", "--node", "A", expect="msg")
    assert "pass --yes" in msg and not list(fake.netplan_dir().glob("*.yaml"))


def test_apply_refuses_when_it_is_not_safe(fake, capsys):
    fake.default_dev = PRIMARY
    assert "default route" in run(fake, "apply", "--node", "A", "--yes", expect="msg")
    capsys.readouterr()
    msg = run(fake, "apply", "--node", "A", "--yes", "--iface", SECONDARY, expect="msg")
    assert "not safe to continue" in msg and "default route" in capsys.readouterr().out
    assert not list(fake.netplan_dir().glob("*.yaml"))


def test_apply_with_everything_configured_says_so(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.101.1/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    out = run(n, "apply", "--yes", capsys=capsys)
    assert "Nothing to change" in out


def test_apply_keeps_the_primary_that_is_already_set(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.2/24"])
    monkeypatch.setattr(qsfp, "RUN", n.run)
    out = run(n, "apply", "--yes", capsys=capsys)
    assert "keep  enp1s0f1np1 192.168.100.2/24" in out and "192.168.101.2/24" in out
    text = (n.root / "etc/netplan/60-twinspark-qsfp.yaml").read_text()
    assert PRIMARY not in text and "192.168.101.2/24" in text


def test_revert_removes_the_file(fake, capsys):
    run(fake, "apply", "--node", "A", "--yes")
    out = run(fake, "revert", "--yes", capsys=capsys)
    assert "removed TwinSpark's netplan file" in out
    assert not (fake.root / "etc/netplan/60-twinspark-qsfp.yaml").exists()


def test_verify_checks_this_side_and_pings_the_other(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.101.1/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    monkeypatch.setattr(qsfp, "_tcp_connect", lambda *a: True)
    out = run(n, "verify", capsys=capsys)
    assert "qsfp peer 192.168.100.2" in out and "qsfp peer 192.168.101.2" in out and "looks good" in out


def test_verify_fails_with_a_clear_message_when_the_other_side_is_missing(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.101.1/24"], mtu=9000)
    n.peer_alive = False
    monkeypatch.setattr(qsfp, "RUN", n.run)
    monkeypatch.setattr(qsfp, "_tcp_connect", lambda *a: False)
    run(n, "verify", expect=1)
    out = capsys.readouterr().out
    assert "[FAIL] qsfp peer 192.168.100.2" in out and "apply --node B" in out


def test_verify_accepts_an_explicit_peer(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.101.1/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    monkeypatch.setattr(qsfp, "_tcp_connect", lambda *a: True)
    out = run(n, "verify", "--peer", "192.168.100.2", capsys=capsys)
    assert "192.168.100.2" in out and "192.168.101.2" not in out


def test_scan_lists_hosts_and_can_name_the_gpu(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.101.1/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    monkeypatch.setattr(qsfp, "_tcp_connect", lambda ip, port, timeout, src: ip.endswith(".2"))
    out = run(n, "scan", "--identify", "--user", "sparkuser", capsys=capsys)
    assert "192.168.100.2" in out and "via enp1s0f1np1" in out and "NVIDIA GB10" in out
    assert "192.168.101.2" in out


def test_scan_with_nothing_found_explains(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"])
    monkeypatch.setattr(qsfp, "RUN", n.run)
    monkeypatch.setattr(qsfp, "_tcp_connect", lambda *a: False)
    assert "nothing answers on SSH" in run(n, "scan", capsys=capsys)


def test_a_real_apply_needs_root(fake, monkeypatch, capsys):
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as e:
        cli.main(["qsfp", "apply", "--node", "A", "--yes", "--sysfs", str(fake.sysfs)])      # --root left at "/"
    assert "run it with sudo" in str(e.value.code)
    assert not list(fake.netplan_dir().glob("*.yaml"))


def test_management_interface_is_never_listed_as_a_qsfp_port(fake, capsys):
    run(fake, "status", expect=1)
    assert MGMT not in capsys.readouterr().out.split("\n\n")[0]


def test_apply_json_prints_only_json(fake, capsys):
    fake.write_netplan("50-x.yaml", "network:\n  version: 2\n")
    cli.main(["--json", "qsfp", "apply", "--node", "A", "--yes", "--root", str(fake.root), "--sysfs", str(fake.sysfs)])
    out = capsys.readouterr().out
    assert json.loads(out)["changed"] is True


def test_verify_rejects_a_bad_peer_politely(fake, capsys):
    msg = run(fake, "verify", "--peer", "not-an-ip", expect="msg")
    assert "is not an IPv4 address" in msg


def test_revert_temporary_needs_no_node_when_it_was_recorded(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    monkeypatch.setattr(qsfp, "_which", lambda name: "/usr/sbin/netplan")
    host = n.host(real=True)
    qsfp.apply(host, qsfp.plan_for_node(qsfp.discover(host), "A", host=host), temporary=True)
    monkeypatch.setattr(cli_qsfp, "_host", lambda args: host)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    out = run(n, "revert", "--temporary", "--yes", capsys=capsys)
    assert "temporary addresses removed" in out and not qsfp.temp_recorded(host)


def test_status_flags_a_stale_gid_index_from_controller_yaml(fake, capsys):
    etc = fake.root / "etc/twinspark"
    etc.mkdir(parents=True)
    (etc / "agent.yaml").write_text(yaml.safe_dump({"node": {"node_id": "A", "role": "controller"}}))
    (etc / "controller.yaml").write_text(yaml.safe_dump({
        "node": {"node_id": "A", "role": "controller"},
        "nodes": {"A": {"agent_url": "http://127.0.0.1:9443", "qsfp_iface": PRIMARY, "ib_gid_index": 7},
                  "B": {"agent_url": "http://192.168.100.2:9443"}}}))
    fake.ifaces[PRIMARY]["ips"] = ["192.168.100.1/24"]
    fake.ifaces[SECONDARY]["ips"] = ["192.168.101.1/24"]
    fake.ifaces[PRIMARY]["mtu"] = fake.ifaces[SECONDARY]["mtu"] = 9000
    fake.sync()
    assert "[WARN] qsfp gid" in run(fake, "status", capsys=capsys)
