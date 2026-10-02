"""``tsm setup`` and the QSFP step: it offers, shows the plan, and only changes the network when told to.

Sandbox roots and a fake Spark (tests/qsfp_fakes.py); no netplan or ip command is run for real.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.qsfp_fakes import MGMT, PRIMARY, SECONDARY, FakeNode
from twinspark import cli, cli_setup, hostprobe, qsfp
from twinspark.provision import Layout
from twinspark.schemas.config import ControllerConfig, load_config

REAL_PROBE = hostprobe.probe_host


def install(monkeypatch, node: FakeNode) -> None:
    """Make the wizard see ``node`` as the machine it runs on."""
    net, ib = node.sysfs / "sys/class/net", node.sysfs / "sys/class/infiniband"

    def run2(argv, timeout=8):
        rc, out, _ = node.run(argv, timeout)
        return rc, out

    def probe(user=None, hf_path=None, ports=(8000, 8100, 8443, 9443), **kw):
        return REAL_PROBE(user, hf_path, ports, str(net), str(ib), run2)

    monkeypatch.setattr(hostprobe, "probe_host", probe)
    monkeypatch.setattr(hostprobe, "port_free", lambda port, host="0.0.0.0": True)
    monkeypatch.setattr(hostprobe, "docker_status", lambda user=None, run=None: {
        "installed": True, "reachable": True, "in_group": True, "detail": "server 27.0.1"})
    monkeypatch.setattr(qsfp, "RUN", node.run)
    monkeypatch.setattr(qsfp, "_which", lambda name: f"/usr/sbin/{name}")


def setup(*argv):
    return cli.main(["setup", "--yes", "--service-user", "chopc", "--no-start", *map(str, argv)])


def netplan_file(root: Path) -> Path:
    return Layout(root).p("etc/netplan/60-twinspark-qsfp.yaml")


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    n = FakeNode(tmp_path / "spark")
    install(monkeypatch, n)
    return n


def test_unattended_setup_shows_the_plan_but_does_not_touch_the_network(fresh, tmp_path, capsys):
    root = tmp_path / "a"
    assert setup("--root", root, "--hf-cache-dir", tmp_path / "hf") == 0
    out = capsys.readouterr().out
    assert "two PCIe halves" in out and "192.168.101.1/24" in out
    assert "not changing the network unattended" in out and "--configure-qsfp" in out
    assert not netplan_file(root).exists()
    c = load_config(Layout(root).controller_yaml, ControllerConfig)
    assert c.nodes["A"].qsfp_ip == "192.168.100.1"                # the default it would have configured


def test_configure_qsfp_writes_the_netplan_file_for_both_twins(fresh, tmp_path, capsys):
    root = tmp_path / "a"
    setup("--root", root, "--hf-cache-dir", tmp_path / "hf", "--configure-qsfp")
    out = capsys.readouterr().out
    assert "written; not applied because this is a sandbox root" in out
    doc = yaml.safe_load(netplan_file(root).read_text())["network"]["ethernets"]
    assert doc[PRIMARY]["addresses"] == ["192.168.100.1/24"] and doc[SECONDARY]["addresses"] == ["192.168.101.1/24"]
    assert doc[PRIMARY]["mtu"] == 9000
    assert not any(c[0] == "netplan" for c in fresh.calls)


def test_an_interactive_yes_applies_and_the_default_is_no(fresh, tmp_path, capsys):
    root = tmp_path / "a"
    args = type("A", (), {"root": str(root), "dry": False, "configure_qsfp": False, "service_user": "chopc"})()
    rep = hostprobe.probe_host()
    answers = iter(["", "y"])
    con = cli_setup.Console(yes=False, input_fn=lambda prompt: next(answers), out=lambda *a: None)
    assert cli_setup._qsfp_step(args, con, rep, node_id="A") is False         # Enter = no
    assert not netplan_file(root).exists()
    assert cli_setup._qsfp_step(args, con, rep, node_id="A") is True          # y
    assert netplan_file(root).exists()


def test_an_existing_primary_address_is_kept_and_only_the_second_twin_is_added(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path / "spark", primary_ips=["192.168.100.1/24"], mtu=9000)
    install(monkeypatch, n)
    root = tmp_path / "a"
    setup("--root", root, "--hf-cache-dir", tmp_path / "hf", "--configure-qsfp")
    out = capsys.readouterr().out
    assert "already set up; left alone" in out
    doc = yaml.safe_load(netplan_file(root).read_text())["network"]["ethernets"]
    assert list(doc) == [SECONDARY] and doc[SECONDARY]["addresses"] == ["192.168.101.1/24"]
    c = load_config(Layout(root).controller_yaml, ControllerConfig)
    assert c.nodes["A"].qsfp_ip == "192.168.100.1"                # taken from the interface, as before


def test_a_complete_setup_is_left_completely_alone(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path / "spark", primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.101.1/24"], mtu=9000)
    install(monkeypatch, n)
    root = tmp_path / "a"
    setup("--root", root, "--hf-cache-dir", tmp_path / "hf", "--configure-qsfp")
    assert "two PCIe halves" not in capsys.readouterr().out and not netplan_file(root).exists()
    c = load_config(Layout(root).controller_yaml, ControllerConfig)
    assert sorted(c.nodes["A"].rdma_hcas) == ["roceP2p1s0f1", "rocep1s0f1"] and c.nodes["A"].ib_gid_index == 3


def test_unsafe_plans_are_reported_and_setup_carries_on(fresh, tmp_path, capsys):
    fresh.default_dev = PRIMARY                                   # the QSFP twin carries the default route (!)
    fresh.sync()
    root = tmp_path / "a"
    assert setup("--root", root, "--hf-cache-dir", tmp_path / "hf", "--configure-qsfp") == 0
    out = capsys.readouterr().out
    assert "✗" in out and "default route" in out and "Not changing the network" in out
    assert not netplan_file(root).exists()


def test_a_netplan_file_we_did_not_write_blocks_the_step_but_not_setup(fresh, tmp_path, capsys):
    root = tmp_path / "a"
    foreign = Layout(root).p("etc/netplan/50-cloud-init.yaml")
    foreign.parent.mkdir(parents=True)
    foreign.write_text(yaml.safe_dump({"network": {"version": 2, "ethernets": {SECONDARY: {"dhcp4": True}}}}))
    before = foreign.read_text()
    assert setup("--root", root, "--hf-cache-dir", tmp_path / "hf", "--configure-qsfp") == 0
    assert "does not edit files it did not write" in capsys.readouterr().out
    assert foreign.read_text() == before and not netplan_file(root).exists()


def test_dry_run_changes_nothing(fresh, tmp_path, capsys):
    root = tmp_path / "a"
    setup("--root", root, "--dry", "--configure-qsfp")
    assert "dry run: the network is not changed" in capsys.readouterr().out
    assert not root.exists()


def test_a_failing_apply_does_not_stop_setup(fresh, tmp_path, monkeypatch, capsys):
    def boom(host, plan, **kw):
        raise qsfp.QsfpError("netplan apply failed: boom — the previous network settings were put back")

    monkeypatch.setattr(qsfp, "apply", boom)
    root = tmp_path / "a"
    assert setup("--root", root, "--hf-cache-dir", tmp_path / "hf", "--configure-qsfp") == 0
    out = capsys.readouterr().out
    assert "previous network settings were put back" in out and "setup continues without this" in out
    assert Layout(root).controller_yaml.exists()


def test_no_cable_means_no_offer(tmp_path, monkeypatch, capsys):
    n = FakeNode(tmp_path / "spark", cabled=False)
    install(monkeypatch, n)
    assert setup("--root", tmp_path / "a", "--hf-cache-dir", tmp_path / "hf", "--configure-qsfp", "--skip-checks") == 0
    assert "two PCIe halves" not in capsys.readouterr().out


def test_a_single_spark_never_sees_the_step(fresh, tmp_path, capsys):
    setup("--root", tmp_path / "s", "--single", "--hf-cache-dir", tmp_path / "hf", "--configure-qsfp")
    assert "two PCIe halves" not in capsys.readouterr().out and not netplan_file(tmp_path / "s").exists()


def test_node_b_takes_its_host_number_and_subnet_from_the_join_code(tmp_path, monkeypatch, capsys):
    a = FakeNode(tmp_path / "spark-a")
    install(monkeypatch, a)
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    setup("--root", a_root, "--hf-cache-dir", tmp_path / "hfa", "--configure-qsfp")
    out = capsys.readouterr().out
    code = next(line.split("--join ", 1)[1].strip() for line in out.splitlines() if "tsm setup --join tsm1." in line)
    b = FakeNode(tmp_path / "spark-b")
    install(monkeypatch, b)
    setup("--root", b_root, "--join", code, "--hf-cache-dir", tmp_path / "hfb", "--qsfp-iface", PRIMARY,
          "--configure-qsfp")
    doc = yaml.safe_load(netplan_file(b_root).read_text())["network"]["ethernets"]
    assert doc[PRIMARY]["addresses"] == ["192.168.100.2/24"] and doc[SECONDARY]["addresses"] == ["192.168.101.2/24"]


def test_node_b_without_the_flag_is_told_the_exact_command(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeNode(tmp_path / "spark-a"))
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    setup("--root", a_root, "--hf-cache-dir", tmp_path / "hfa")
    out = capsys.readouterr().out
    code = next(line.split("--join ", 1)[1].strip() for line in out.splitlines() if "tsm setup --join tsm1." in line)
    install(monkeypatch, FakeNode(tmp_path / "spark-b"))
    setup("--root", b_root, "--join", code, "--hf-cache-dir", tmp_path / "hfb", "--qsfp-iface", PRIMARY)
    out = capsys.readouterr().out
    assert "no interface has 192.168.100.2 yet; setup does NOT change your network" in out
    assert "sudo tsm qsfp apply --node B" in out and not netplan_file(b_root).exists()


def test_the_management_interface_is_never_offered(fresh, tmp_path, capsys):
    setup("--root", tmp_path / "a", "--hf-cache-dir", tmp_path / "hf")
    plan_lines = [line for line in capsys.readouterr().out.splitlines() if "mtu 9000" in line]
    assert plan_lines and not any(MGMT in line for line in plan_lines)


def test_running_setup_again_after_the_network_step_stays_quiet(fresh, tmp_path, capsys):
    root = tmp_path / "a"
    setup("--root", root, "--hf-cache-dir", tmp_path / "hf", "--configure-qsfp")
    capsys.readouterr()
    setup("--root", root, "--hf-cache-dir", tmp_path / "hf", "--configure-qsfp", "--force")
    out = capsys.readouterr().out
    assert "two PCIe halves" not in out and "apply it now" not in out


def test_node_b_skips_the_step_when_the_join_address_is_on_the_second_interface(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeNode(tmp_path / "spark-a"))
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    setup("--root", a_root, "--hf-cache-dir", tmp_path / "hfa", "--qsfp-iface", SECONDARY)
    code = next(line.split("--join ", 1)[1].strip() for line in capsys.readouterr().out.splitlines()
                if "tsm setup --join tsm1." in line)
    install(monkeypatch, FakeNode(tmp_path / "spark-b"))
    setup("--root", b_root, "--join", code, "--hf-cache-dir", tmp_path / "hfb", "--configure-qsfp")
    out = capsys.readouterr().out
    assert "the QSFP step is skipped" in out and not netplan_file(b_root).exists()
