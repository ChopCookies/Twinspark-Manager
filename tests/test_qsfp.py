"""QSFP link automation: discovery, planning, the safety rules of apply, verification, scanning.

Everything runs against :class:`tests.qsfp_fakes.FakeNode` (a fake /sys, ``ip``, ``ss``, ``ping`` and a
``netplan`` that really turns YAML into addresses). No test touches the machine it runs on.
"""

from __future__ import annotations

import json
import stat

import pytest
import yaml

from tests.qsfp_fakes import MGMT, PRIMARY, SECONDARY, FakeNode
from twinspark import qsfp


@pytest.fixture
def node(tmp_path, monkeypatch):
    n = FakeNode(tmp_path)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    monkeypatch.setattr(qsfp, "_which", lambda name: f"/usr/sbin/{name}")
    return n


def plan_a(fake, who="A", **kw):
    host = fake.host()
    d = qsfp.discover(host)
    return host, d, qsfp.plan_for_node(d, who, host=host, **kw)


# ---- discovery ------------------------------------------------------------------------------
def test_discovery_groups_the_two_twins_of_a_port(node):
    d = qsfp.discover(node.host())
    assert [p.key for p in d.ports] == ["f1np1"]
    port = d.ports[0]
    assert port.names() == [PRIMARY, SECONDARY] and port.cabled
    assert port.primary.hca == "rocep1s0f1" and port.secondary.secondary
    assert port.primary.mtu == 1500 and port.primary.rate_gbps == 100
    assert d.default_route_ifaces == [MGMT]
    assert MGMT not in port.names() and "docker0" not in port.names()


def test_discovery_reads_addresses_mtu_and_roce_gids(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.101.1/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    port = qsfp.discover(n.host()).ports[0]
    assert port.primary.ipv4 == ["192.168.100.1/24"] and port.secondary.ipv4 == ["192.168.101.1/24"]
    assert port.primary.mtu == 9000
    assert [g["ipv4"] for g in port.primary.gids] == ["192.168.100.1"]
    assert port.secondary.gids[0]["index"] == 3


def test_a_port_without_a_cable_is_not_chosen(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, cabled=False)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    d = qsfp.discover(n.host())
    assert not d.ports[0].cabled
    with pytest.raises(qsfp.QsfpError, match="no QSFP link is up"):
        qsfp.choose_port(d)
    assert qsfp.choose_port(d, SECONDARY).key == "f1np1"          # naming it is allowed (e.g. before cabling)


def test_choose_port_prefers_the_cabled_and_the_configured_port(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, second_port=True)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    d = qsfp.discover(n.host())
    assert {p.key for p in d.ports} == {"f0np0", "f1np1"}
    assert qsfp.choose_port(d).key == "f1np1"                     # only f1np1 has a link
    n.ifaces["enp1s0f0np0"]["up"] = True
    n.sync()
    d = qsfp.discover(n.host())
    assert qsfp.choose_port(d).key == "f1np1"                     # both cabled: the right-hand port, like the guides
    assert qsfp.choose_port(d, configured="enp1s0f0np0").key == "f0np0"
    with pytest.raises(qsfp.QsfpError, match="not a ConnectX QSFP interface"):
        qsfp.choose_port(d, MGMT)


def test_no_connectx_at_all_gets_a_helpful_error(tmp_path, monkeypatch):
    n = FakeNode(tmp_path)
    for name in (PRIMARY, SECONDARY):
        del n.ifaces[name]
    n.sync()
    monkeypatch.setattr(qsfp, "RUN", n.run)
    with pytest.raises(qsfp.QsfpError, match="Is this a DGX Spark"):
        qsfp.choose_port(qsfp.discover(n.host()))


def test_port_key_matches_both_twins_only():
    assert qsfp.port_key("enp1s0f1np1") == qsfp.port_key("enP2p1s0f1np1") == "f1np1"
    assert qsfp.port_key("enP7s7") is None and qsfp.port_key("docker0") is None


# ---- planning -------------------------------------------------------------------------------
def test_a_fresh_node_gets_two_subnets_mtu_9000_and_no_dhcp(node):
    _, _, plan = plan_a(node)
    assert [(e.iface, e.cidr) for e in plan.entries] == [(PRIMARY, "192.168.100.1/24"),
                                                          (SECONDARY, "192.168.101.1/24")]
    assert plan.peer_ips == ["192.168.100.2", "192.168.101.2"] and plan.kept == []
    doc = yaml.safe_load(plan.text)["network"]
    assert doc["version"] == 2
    for name, ip in ((PRIMARY, "192.168.100.1/24"), (SECONDARY, "192.168.101.1/24")):
        cfg = doc["ethernets"][name]
        assert cfg == {"dhcp4": False, "dhcp6": False, "link-local": [], "optional": True, "mtu": 9000,
                       "addresses": [ip]}
    assert plan.text.startswith(qsfp.MARKER) and plan.path.endswith("etc/netplan/60-twinspark-qsfp.yaml")


def test_node_b_is_host_two_and_the_peer_is_host_one(node):
    _, _, plan = plan_a(node, "B")
    assert [e.cidr for e in plan.entries] == ["192.168.100.2/24", "192.168.101.2/24"]
    assert plan.peer_ips == ["192.168.100.1", "192.168.101.1"]


def test_custom_subnet_mtu_and_host_number(node):
    _, _, plan = plan_a(node, subnet="10.77.0.0/24", mtu=9216, host_number=7)
    assert [e.cidr for e in plan.entries] == ["10.77.0.7/24", "10.77.1.7/24"]
    assert plan.mtu == 9216 and "mtu: 9216" in plan.text
    assert plan.peer_ips == ["10.77.0.1", "10.77.1.1"]


@pytest.mark.parametrize("kw,msg", [
    ({"subnet": "8.8.8.0/24"}, "not a private range"),
    ({"subnet": "192.168.0.0/16"}, "IPv4 /24"),
    ({"subnet": "garbage"}, "not a network"),
    ({"subnet": "192.168.255.0/24"}, "end of its range"),
    ({"mtu": 100}, "MTU must be between"),
    ({"host_number": 0}, "between 1 and 254"),
    ({"node": "C"}, "unknown node"),
    ({"node": None}, "say which node"),
])
def test_bad_plan_inputs_are_explained(node, kw, msg):
    host = node.host()
    d = qsfp.discover(host)
    args = {"node": "A", **kw}
    with pytest.raises(qsfp.QsfpError, match=msg):
        qsfp.plan_for_node(d, args.pop("node"), host=host, **args)


def test_one_twin_only_still_plans_and_warns(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, with_secondary=False)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    plan = qsfp.plan_for_node(qsfp.discover(host), "A", host=host)
    assert [e.iface for e in plan.entries] == [PRIMARY]
    assert any("100 Gb/s" in w for w in plan.warnings)


def test_an_existing_address_on_the_primary_is_kept_and_the_secondary_matches_it(tmp_path, monkeypatch):
    """The common case: README-era setup, only enp1s0f1np1 has 192.168.100.1 (set in some other netplan file)."""
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"])
    n.write_netplan("50-cloud-init.yaml", yaml.safe_dump(
        {"network": {"version": 2, "ethernets": {PRIMARY: {"addresses": ["192.168.100.1/24"]}}}}))
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    plan = qsfp.plan_for_node(qsfp.discover(host), None, host=host)              # no --node: the address says it
    assert [(e.iface, e.cidr) for e in plan.entries] == [(SECONDARY, "192.168.101.1/24")]
    assert plan.kept == [f"{PRIMARY} 192.168.100.1/24"] and plan.node_host == 1
    assert any("MTU 1500" in w for w in plan.warnings)                          # jumbo frames need the primary at 9000
    assert PRIMARY not in plan.text
    assert qsfp.preflight(host, qsfp.discover(host), plan) == []                # the foreign file is not in the way


def test_the_kept_primary_decides_subnet_and_host_number(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, primary_ips=["10.20.30.2/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    plan = qsfp.plan_for_node(qsfp.discover(host), None, host=host)
    assert [(e.iface, e.cidr) for e in plan.entries] == [(SECONDARY, "10.20.31.2/24")]
    assert plan.node_host == 2 and plan.warnings == [] and plan.peer_ips == ["10.20.30.1", "10.20.31.1"]


def test_a_conflicting_node_flag_is_called_out(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    plan = qsfp.plan_for_node(qsfp.discover(host), "B", host=host)
    assert any("same host number" in w for w in plan.warnings)


def test_a_new_twin_may_not_land_on_the_subnet_of_a_kept_one(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    with pytest.raises(qsfp.QsfpError, match="same subnet"):
        # --subnet 192.168.99.0/24 would give the secondary 192.168.100.1 — the primary's network
        qsfp.plan_for_node(qsfp.discover(host), None, subnet="192.168.99.0/24", host=host)


def test_two_kept_twins_on_one_subnet_are_reported_not_hidden(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.100.11/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    plan = qsfp.plan_for_node(qsfp.discover(host), None, host=host)
    assert plan.entries == [] and len(plan.kept) == 2
    assert any("share" in w and "192.168.100.0/24" in w for w in plan.warnings)


def test_every_twin_already_configured_means_nothing_to_do(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.101.1/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    plan = qsfp.plan_for_node(qsfp.discover(host), None, host=host)
    assert plan.entries == [] and len(plan.kept) == 2 and plan.text == ""
    out = qsfp.apply(host, plan)
    assert out["changed"] is False and "already configured" in out["message"]


# ---- pre-flight: the rules that protect a headless machine ----------------------------------------
def test_preflight_passes_on_a_clean_machine(node):
    host, d, plan = plan_a(node)
    assert qsfp.preflight(host, d, plan) == []


def test_it_refuses_the_interface_with_the_default_route(node):
    node.default_dev = SECONDARY
    with pytest.raises(qsfp.QsfpError, match="also carries this machine's default route.*--iface"):
        plan_a(node)                                                 # never guessed
    host, d, plan = plan_a(node, iface=PRIMARY)                      # named: the guard still stops it
    problems = qsfp.preflight(host, d, plan)
    assert any(SECONDARY in p and "default route" in p for p in problems)
    with pytest.raises(qsfp.QsfpError, match="default route"):
        qsfp.apply(host, plan, d=d)
    assert not host.managed_file.exists()


def _second_port(node, *, up=True, uplink=False):
    """Give the fake the left-hand port f0np0 with both twins; ``uplink`` cables it to the LAN (DHCP, default)."""
    for name, hca in (("enp1s0f0np0", "rocep1s0f0"), ("enP2p1s0f0np0", "roceP2p1s0f0")):
        node.ifaces[name] = {"ips": [], "mtu": 1500, "up": up, "hca": hca, "roce": True, "virtual": False}
    if uplink:
        node.ifaces["enp1s0f0np0"]["ips"] = ["10.0.1.50/24"]
        node.default_dev = "enp1s0f0np0"
    node.sync()


def test_a_port_cabled_to_the_network_is_never_chosen_even_with_an_address(node):
    """f0 goes to a switch (DHCP address, default route), f1 is the fresh cable to the other Spark."""
    _second_port(node, uplink=True)
    host, d, plan = plan_a(node)
    assert plan.port == "f1np1" and {e.iface for e in plan.entries} == {PRIMARY, SECONDARY}
    assert qsfp.choose_port(d).key == "f1np1"                        # status picks the same port


def test_the_twin_of_a_network_port_is_only_configured_when_named_and_with_a_warning(node):
    _second_port(node, uplink=True)
    host, d, plan = plan_a(node, iface="enP2p1s0f0np0")
    assert plan.named and any("enp1s0f0np0" in w and "default route" in w for w in plan.warnings)
    assert qsfp.preflight(host, d, plan) == []                       # the person said this port; they were warned
    plan.named = False                                               # a guessed port is refused
    assert any("enp1s0f0np0" in p and "looks cabled to your network" in p for p in qsfp.preflight(host, d, plan))


def test_node_b_reaching_the_internet_through_node_a_can_add_its_second_twin(node):
    """The only port carries B's default route (via A); named by configuration, the second twin is added."""
    node.ifaces[PRIMARY]["ips"] = ["192.168.100.2/24"]
    node.write_netplan("50-mine.yaml", yaml.safe_dump({"network": {"version": 2, "ethernets": {PRIMARY: {}}}}))
    node.default_dev = PRIMARY
    node.sync()
    with pytest.raises(qsfp.QsfpError, match="--iface"):
        plan_a(node, "B")
    host, d, plan = plan_a(node, "B", configured=PRIMARY)
    assert [e.iface for e in plan.entries] == [SECONDARY] and plan.kept[0].startswith(PRIMARY)
    assert qsfp.preflight(host, d, plan) == []


def test_a_session_on_the_kept_twin_does_not_block_adding_the_other_one(node):
    node.ifaces[PRIMARY]["ips"] = ["192.168.100.2/24"]
    node.write_netplan("50-mine.yaml", yaml.safe_dump({"network": {"version": 2, "ethernets": {PRIMARY: {}}}}))
    node.ssh_clients = ["192.168.100.1"]                             # logged in from node A over the kept twin
    node.sync()
    host = node.host(real=True)
    d = qsfp.discover(host)
    plan = qsfp.plan_for_node(d, "B", host=host)
    assert [e.iface for e in plan.entries] == [SECONDARY]
    assert qsfp.preflight(host, d, plan) == []


def test_status_does_not_count_a_network_uplink_as_a_second_cable(node, monkeypatch):
    _second_port(node, uplink=True)
    by = checks_by_name(node)
    assert "qsfp ports" not in by                                    # no "unplug the second cable" advice


def test_two_spark_cables_need_iface_before_anything_is_planned(node):
    _second_port(node)
    with pytest.raises(qsfp.QsfpError, match="--iface"):
        plan_a(node)
    assert plan_a(node, iface="enp1s0f0np0")[2].port == "f0np0"
    assert plan_a(node, configured=PRIMARY)[2].port == "f1np1"       # controller.yaml decides


def test_it_refuses_an_interface_an_ssh_session_arrives_through(node):
    node.ifaces[PRIMARY]["ips"] = ["192.168.100.1/24"]               # someone is logged in over the QSFP link
    node.ssh_clients = ["192.168.100.2"]
    node.write_netplan("50-mine.yaml", yaml.safe_dump({"network": {"version": 2, "ethernets": {PRIMARY: {}}}}))
    node.netplan_owned = {PRIMARY}
    host = node.host(real=True)
    d = qsfp.discover(host)
    plan = qsfp.make_plan(d.ports[0], 1, host=host, owned={PRIMARY, SECONDARY})        # force a plan that touches it
    problems = qsfp.preflight(host, d, plan)
    assert any("remote session (192.168.100.2) arrives through " + PRIMARY in p for p in problems)


def test_the_ssh_session_is_found_through_the_environment_too(node):
    node.ifaces[PRIMARY]["ips"] = ["192.168.100.1/24"]
    host = node.host(real=True, env={"SSH_CONNECTION": "192.168.100.2 5555 192.168.100.1 22"})
    d = qsfp.discover(host)
    plan = qsfp.make_plan(d.ports[0], 1, host=host, owned={PRIMARY, SECONDARY})
    assert any("remote session" in p for p in qsfp.preflight(host, d, plan))
    assert qsfp.ssh_clients(host)[0] == "192.168.100.2"


def test_ssh_clients_reads_ss_output_including_ipv6_mapped_peers(node):
    node.ssh_clients = ["10.0.0.7", "[::ffff:10.0.0.8]"]
    got = qsfp.ssh_clients(node.host(real=True))
    assert got == ["10.0.0.7", "10.0.0.8"]


def test_a_management_ssh_session_does_not_block_the_qsfp_change(node):
    node.ssh_clients = ["10.0.0.7"]                                   # arrives via enP7s7, not via the QSFP twins
    host = node.host(real=True)
    d = qsfp.discover(host)
    plan = qsfp.plan_for_node(d, "A", host=host)
    assert qsfp.preflight(host, d, plan) == []


def test_it_never_edits_a_netplan_file_it_did_not_write(node):
    foreign = node.write_netplan("50-mine.yaml", yaml.safe_dump(
        {"network": {"version": 2, "ethernets": {SECONDARY: {"dhcp4": True}}}}))
    host, d, plan = plan_a(node)
    problems = qsfp.preflight(host, d, plan)
    assert any(str(foreign) in p and SECONDARY in p and "does not edit" in p for p in problems)
    before = foreign.read_text()
    with pytest.raises(qsfp.QsfpError, match="not safe to continue"):
        qsfp.apply(host, plan, d=d)
    assert foreign.read_text() == before and not host.managed_file.exists()


def test_a_netplan_file_that_is_not_yaml_does_not_crash_the_scan(node):
    node.write_netplan("99-broken.yaml", "network: [unclosed")
    files = qsfp.scan_netplan(node.host())
    assert files[0].error and "not valid YAML" in files[0].error
    host, d, plan = plan_a(node)
    assert qsfp.preflight(host, d, plan) == []


def test_scan_netplan_sees_matches_bonds_and_vlans(node):
    node.write_netplan("10-x.yaml", yaml.safe_dump({"network": {"version": 2, "ethernets": {
        "lan0": {"match": {"name": PRIMARY}}}, "bonds": {"bond0": {"interfaces": [SECONDARY]}}}}))
    (names,) = [f.ifaces for f in qsfp.scan_netplan(node.host())]
    assert {PRIMARY, SECONDARY, "lan0", "bond0"} <= set(names)


def test_a_subnet_already_used_elsewhere_is_refused_with_a_way_out(node):
    node.ifaces[MGMT]["ips"] = ["192.168.100.50/24"]
    host, d, plan = plan_a(node)
    (problem,) = [p for p in qsfp.preflight(host, d, plan) if "overlaps" in p]
    assert MGMT in problem and "--subnet" in problem


def test_missing_interface_root_and_netplan_are_reported_on_a_real_run(node, monkeypatch):
    host = node.host(real=True, euid=1000)
    d = qsfp.discover(host)
    plan = qsfp.plan_for_node(d, "A", host=host)
    assert "this needs root: run it with sudo" in qsfp.preflight(host, d, plan)
    assert "this needs root: run it with sudo" not in qsfp.preflight(host, d, plan, need_root=False)
    monkeypatch.setattr(qsfp, "_which", lambda name: None)
    assert any("netplan is not installed" in p for p in qsfp.preflight(node.host(real=True), d, plan))
    assert not any("netplan" in p for p in qsfp.preflight(node.host(real=True), d, plan, need_netplan=False))
    plan.entries[0].iface = "enp9s9f9np9"
    assert any("does not exist" in p for p in qsfp.preflight(node.host(real=True), d, plan))


# ---- apply / revert in a scratch root (nothing is "applied") ------------------------------------
def test_apply_in_a_sandbox_writes_only_the_file_with_private_permissions(node):
    host, d, plan = plan_a(node)
    out = qsfp.apply(host, plan, d=d)
    assert out["mode"] == "sandbox" and out["changed"]
    assert host.managed_file.read_text() == plan.text
    assert stat.S_IMODE(host.managed_file.stat().st_mode) == 0o600
    assert not any(c[0] in ("netplan", "ip") and c[1:3] != ["-j", "-4"] and c[1] not in ("-j",) for c in node.calls)


def test_applying_twice_changes_nothing_the_second_time(node):
    host, d, plan = plan_a(node)
    qsfp.apply(host, plan, d=d)
    again = qsfp.apply(host, plan, d=d)
    assert again["changed"] is False and again["message"] == "already configured"
    assert not host.backup_dir.exists()


def test_changing_the_plan_keeps_the_old_file_as_a_backup_and_revert_restores_it(node):
    host, d, plan = plan_a(node)
    qsfp.apply(host, plan, d=d)
    first = host.managed_file.read_text()
    plan2 = qsfp.plan_for_node(d, "A", subnet="10.77.0.0/24", host=host)
    out = qsfp.apply(host, plan2, d=d)
    assert out["backup"] and host.managed_file.read_text() == plan2.text != first
    rev = qsfp.revert(host)
    assert rev["restored"] and host.managed_file.read_text() == first
    rev2 = qsfp.revert(host)                                           # the used backup is gone: back to "no file"
    assert rev2["message"] == "removed TwinSpark's netplan file" and not host.managed_file.exists()
    assert qsfp.revert(host)["changed"] is False


def test_revert_refuses_a_file_without_the_marker(node):
    path = node.write_netplan(qsfp.NETPLAN_ALT, "network:\n  version: 2\n")      # the file name TwinSpark would use
    node.write_netplan(qsfp.NETPLAN_FILE, "network:\n  version: 2\n")           # ... because 60-… is somebody's
    with pytest.raises(qsfp.QsfpError, match="was not written by TwinSpark"):
        qsfp.revert(node.host())
    assert path.exists()


def test_a_hand_written_file_with_our_name_is_never_overwritten(tmp_path, monkeypatch):
    """The old setup snippet told people to create 60-twinspark-qsfp.yaml themselves — without our marker."""
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], mtu=9000)
    mine = n.write_netplan(qsfp.NETPLAN_FILE, yaml.safe_dump(
        {"network": {"version": 2, "ethernets": {PRIMARY: {"addresses": ["192.168.100.1/24"], "mtu": 9000}}}}))
    before = mine.read_text()
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    plan = qsfp.plan_for_node(qsfp.discover(host), None, host=host)
    assert [e.iface for e in plan.entries] == [SECONDARY] and plan.kept == [f"{PRIMARY} 192.168.100.1/24"]
    assert plan.path.endswith(qsfp.NETPLAN_ALT)
    assert any(qsfp.NETPLAN_FILE in w and "left alone" in w for w in plan.warnings)
    qsfp.apply(host, plan)
    assert mine.read_text() == before                                   # untouched
    assert host.managed_file.name == qsfp.NETPLAN_ALT and qsfp.is_ours(host.managed_file)
    assert qsfp.revert(host)["message"] == "removed TwinSpark's netplan file"
    assert mine.read_text() == before and not (host.netplan_dir / qsfp.NETPLAN_ALT).exists()


def test_a_second_twinspark_file_for_the_same_interfaces_is_refused(node):
    node.write_netplan(qsfp.NETPLAN_FILE, "network:\n  version: 2\n")                     # foreign -> we use 61-…
    node.write_netplan("62-extra.yaml", qsfp.MARKER + "\nnetwork:\n  version: 2\n  ethernets:\n    "
                       + PRIMARY + ": {dhcp4: false}\n")
    host, d, plan = plan_a(node)
    assert any("another TwinSpark file" in p for p in qsfp.preflight(host, d, plan))


def test_the_planned_file_is_valid_for_the_fake_netplan_and_round_trips(node):
    host, d, plan = plan_a(node, subnet="172.20.4.0/24", host_number=2)
    qsfp.apply(host, plan, d=d)
    assert qsfp.managed_layout(host) == (2, "172.20.4.0/24")
    assert qsfp.owned_ifaces(host) == {PRIMARY, SECONDARY}
    # planning again without flags reuses what the file says
    again = qsfp.plan_for_node(d, None, host=host)
    assert [e.cidr for e in again.entries] == ["172.20.4.2/24", "172.20.5.2/24"]


# ---- apply for real (the commands are faked; the files are in the scratch root) ----------------------
def real(node):
    host = node.host(real=True)
    d = qsfp.discover(host)
    return host, d, qsfp.plan_for_node(d, "A", host=host)


def test_apply_validates_then_applies_then_verifies(node):
    host, d, plan = real(node)
    out = qsfp.apply(host, plan, d=d)
    assert out["mode"] == "netplan" and out["message"] == "applied and verified"
    verbs = [c[1] for c in node.calls if c[0] == "netplan"]
    assert verbs == ["generate", "apply"]
    now = qsfp.discover(host).ports[0]
    assert now.primary.ipv4 == ["192.168.100.1/24"] and now.secondary.ipv4 == ["192.168.101.1/24"]
    assert now.primary.mtu == now.secondary.mtu == 9000
    assert qsfp.apply(host, plan)["message"] == "already configured"


def test_a_file_netplan_rejects_is_removed_again_and_nothing_is_applied(node):
    node.fail.add("generate")
    host, d, plan = real(node)
    with pytest.raises(qsfp.QsfpError, match="netplan rejected the file.*previous network settings were put back"):
        qsfp.apply(host, plan, d=d)
    assert not host.managed_file.exists()
    assert [c[1] for c in node.calls if c[0] == "netplan"][:1] == ["generate"]
    assert qsfp.discover(host).ports[0].primary.ipv4 == []


def test_a_failing_netplan_apply_puts_the_old_file_back_and_reapplies_it(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, d=d)
    good = host.managed_file.read_text()
    plan2 = qsfp.plan_for_node(d, "A", subnet="10.77.0.0/24", host=host)
    node.fail.add("apply-once")
    with pytest.raises(qsfp.QsfpError, match="netplan apply failed.*put back"):
        qsfp.apply(host, plan2, d=d)
    assert host.managed_file.read_text() == good
    now = qsfp.discover(host).ports[0]
    assert now.primary.ipv4 == ["192.168.100.1/24"]                  # the earlier layout is live again
    assert not list(host.backup_dir.glob(f"{qsfp.NETPLAN_FILE}.*"))   # the copy is not left to confuse `revert`
    assert qsfp.revert(host)["message"] == "removed TwinSpark's netplan file"


def test_addresses_that_never_come_up_trigger_the_rollback(node):
    node.fail.add("no-address")
    host, d, plan = real(node)
    with pytest.raises(qsfp.QsfpError, match="did not come up.*put back"):
        qsfp.apply(host, plan, d=d, wait_s=0.0)
    assert not host.managed_file.exists()
    assert [c[1] for c in node.calls if c[0] == "netplan"].count("apply") == 2      # apply, then re-apply the old state


def test_when_the_rollback_itself_fails_the_error_says_so(node, monkeypatch):
    node.fail.add("no-address")
    host, d, plan = real(node)
    real_run = node.run

    def run(argv, timeout=15.0):
        if argv[:2] == ["netplan", "apply"] and len([c for c in node.calls if c[:2] == ["netplan", "apply"]]) >= 1:
            node.calls.append(argv)
            return 1, "", "still broken"
        return real_run(argv, timeout)

    monkeypatch.setattr(qsfp, "RUN", run)
    with pytest.raises(qsfp.QsfpError, match="re-applying the old settings also failed: still broken"):
        qsfp.apply(host, plan, d=d, wait_s=0.0)


def test_a_finished_setup_is_a_no_op_even_over_an_ssh_session_on_the_link(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, d=d)
    node.ssh_clients = ["192.168.100.2"]                              # now somebody is logged in through the twin
    d2 = qsfp.discover(host)
    assert qsfp.preflight(host, d2, plan)                              # a real change would be refused ...
    assert qsfp.is_current(host, d2, plan)
    assert qsfp.apply(host, plan, d=d2)["message"] == "already configured"      # ... but there is nothing to change


def test_changing_a_twin_that_is_in_use_says_to_stop_the_model_first(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, d=d)
    d2 = qsfp.discover(host)
    plan2 = qsfp.plan_for_node(d2, "A", subnet="10.77.0.0/24", host=host)
    assert any("in use" in w and "tsm stop" in w for w in plan2.warnings)
    assert not any("in use" in w for w in qsfp.plan_for_node(d2, "A", host=host).warnings)


def test_revert_after_a_real_apply_removes_the_addresses(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, d=d)
    out = qsfp.revert(host)
    assert out["changed"] and not host.managed_file.exists()
    assert qsfp.discover(host).ports[0].primary.ipv4 == []
    assert [c[1] for c in node.calls if c[0] == "netplan"][-1] == "apply"


# ---- temporary addresses ------------------------------------------------------------------------------
def test_temporary_sets_addresses_without_writing_netplan(node):
    host, d, plan = real(node)
    out = qsfp.apply(host, plan, temporary=True, d=d)
    assert out["mode"] == "temporary" and not host.managed_file.exists()
    assert ["ip", "addr", "replace", "192.168.100.1/24", "dev", PRIMARY] in node.calls
    assert ["ip", "link", "set", "dev", SECONDARY, "mtu", "9000"] in node.calls
    assert not any(c[0] == "netplan" for c in node.calls)
    port = qsfp.discover(host).ports[0]
    assert port.primary.ipv4 == ["192.168.100.1/24"] and port.secondary.mtu == 9000
    assert qsfp.owned_ifaces(host) == {PRIMARY, SECONDARY}


def test_a_failure_halfway_removes_the_temporary_addresses_again(node):
    node.fail.add("ip-addr")
    host, d, plan = real(node)
    with pytest.raises(qsfp.QsfpError, match="addresses were removed again"):
        qsfp.apply(host, plan, temporary=True, d=d)
    assert all(t.ipv4 == [] for t in qsfp.discover(host).ports[0].twins)


def test_a_later_permanent_apply_takes_over_the_temporary_addresses(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, temporary=True, d=d)
    d2 = qsfp.discover(host)
    plan2 = qsfp.plan_for_node(d2, "A", host=host)                  # the addresses are live, but ours: still planned
    assert len(plan2.entries) == 2 and plan2.kept == []
    qsfp.apply(host, plan2, d=d2)
    assert host.managed_file.exists() and qsfp.owned_ifaces(host) == {PRIMARY, SECONDARY}
    assert not host.temp_state.exists()


def test_revert_temporary_removes_just_those_addresses(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, temporary=True, d=d)
    qsfp.revert(host, temporary=True, plan=plan)
    assert all(t.ipv4 == [] for t in qsfp.discover(host).ports[0].twins)
    assert not host.temp_state.exists()
    with pytest.raises(qsfp.QsfpError, match="no temporary addresses are recorded"):
        qsfp.revert(host, temporary=True)


def test_revert_temporary_leaves_the_permanent_layout_alone(node):
    """After a permanent apply (or a reboot that cleared /run) there is nothing temporary to remove."""
    host, d, plan = real(node)
    qsfp.apply(host, plan, d=d)
    node.calls.clear()
    with pytest.raises(qsfp.QsfpError, match="without --temporary"):
        qsfp.revert(host, temporary=True, plan=plan)
    assert not [c for c in node.calls if c[:3] == ["ip", "addr", "del"]]
    assert [t.ipv4 for t in qsfp.discover(host).ports[0].twins] == [["192.168.100.1/24"], ["192.168.101.1/24"]]


def test_remote_terminal_sessions_count_as_sessions(node):
    node.ssh_clients = ["192.168.100.2"]
    host = node.host(real=True)
    assert qsfp.ssh_clients(host) == ["192.168.100.2"]
    ss = next(c for c in node.calls if c[0] == "ss")
    assert ":22" in ss and f":{qsfp.TERMINAL_PORT}" in ss and "or" in ss
    agent = node.root / "etc/twinspark/agent.yaml"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(yaml.safe_dump({"remote_mgmt": {"terminal_port": 9555}}))
    node.calls.clear()
    qsfp.ssh_clients(host)
    assert ":9555" in next(c for c in node.calls if c[0] == "ss")      # the port this node's agent.yaml sets


def test_temporary_in_a_sandbox_records_the_commands_without_running_them(node):
    host, d, plan = plan_a(node)
    out = qsfp.apply(host, plan, temporary=True, d=d)
    assert len(out["commands"]) == 6 and not any(c[0] == "ip" and c[1] in ("addr", "link") for c in node.calls)


# ---- local findings ---------------------------------------------------------------------------------------
def checks_by_name(node, **kw):
    out = qsfp.checks(qsfp.discover(node.host()), **kw)
    return {c["check"]: c for c in out}


def good_node(tmp_path, monkeypatch, **kw):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.101.1/24"], mtu=9000, **kw)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    return n


def test_checks_are_all_ok_for_a_correct_setup(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    by = checks_by_name(n, configured_hcas=["rocep1s0f1", "roceP2p1s0f1"])
    assert {c["status"] for c in by.values()} == {"ok"}
    assert {"qsfp link", "qsfp addresses", "qsfp mtu", "qsfp roce"} <= set(by)
    assert "GID index 3" in by["qsfp roce"]["detail"]


def test_checks_name_the_fix_for_each_problem(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, cabled=False)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    (c,) = qsfp.checks(qsfp.discover(n.host()))
    assert c["status"] == "fail" and c["check"] == "qsfp link" and "SAME port" in c["detail"] + c["fix"]

    n = FakeNode(tmp_path / "b", primary_ips=["192.168.100.1/24"])             # one twin without an address, MTU 1500
    monkeypatch.setattr(qsfp, "RUN", n.run)
    by = checks_by_name(n)
    assert by["qsfp addresses"]["status"] == "warn" and "tsm qsfp apply" in by["qsfp addresses"]["fix"]
    assert by["qsfp mtu"]["status"] == "warn" and "1500" in by["qsfp mtu"]["detail"]
    assert by["qsfp roce"]["status"] == "ok"            # the twin without an address is reported by "addresses"

    n = FakeNode(tmp_path / "c")
    monkeypatch.setattr(qsfp, "RUN", n.run)
    assert checks_by_name(n)["qsfp addresses"]["status"] == "fail"


def test_both_twins_on_one_subnet_is_a_failure(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], secondary_ips=["192.168.100.11/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    c = checks_by_name(n)["qsfp subnets"]
    assert c["status"] == "fail" and "eugr" in c["detail"] and "192.168.101.x" in c["fix"]


def test_a_stale_rdma_hcas_setting_is_flagged(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    c = checks_by_name(n, configured_hcas=["rocep1s0f1"])["qsfp config"]
    assert c["status"] == "warn" and "tsm rdma --apply" in c["fix"]


def test_two_cabled_ports_and_a_missing_twin_are_noticed(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, second_port=True, with_secondary=False)
    n.ifaces["enp1s0f0np0"]["up"] = True
    n.sync()
    monkeypatch.setattr(qsfp, "RUN", n.run)
    by = checks_by_name(n)
    assert by["qsfp ports"]["status"] == "warn" and "f0np0" in by["qsfp ports"]["detail"]
    assert by["qsfp twins"]["status"] == "warn"


def test_different_gid_indices_are_flagged(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    d = qsfp.discover(n.host())
    d.ports[0].secondary.gids = [{"index": 5, "ipv4": "192.168.101.1"}]
    (c,) = [c for c in qsfp.checks(d) if c["check"] == "qsfp roce"]
    assert c["status"] == "warn" and "GID" in c["detail"] and "NCCL_IB_GID_INDEX" in c["fix"]


# ---- the other Spark ------------------------------------------------------------------------------------
def test_peer_checks_pass_with_jumbo_frames_and_open_ssh(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    port = qsfp.discover(n.host()).ports[0]
    out = qsfp.peer_checks(n.host(), port, ["192.168.100.2", "192.168.101.2"], tcp=lambda *a: True)
    assert [c["status"] for c in out] == ["ok", "ok", "ok"]
    assert "9000-byte frames" in out[0]["detail"]
    pings = [c for c in n.calls if c[0] == "ping"]
    assert pings[0][:3] == ["ping", "-c", "3"] and "-M" in pings[0] and "8972" in pings[0]


def test_peer_with_a_smaller_mtu_is_told_apart_from_an_unreachable_one(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    port = qsfp.discover(n.host()).ports[0]
    n.peer_mtu = 1500
    out = qsfp.peer_checks(n.host(), port, ["192.168.100.2", "192.168.101.2"], tcp=lambda *a: True)
    assert out[0]["status"] == "fail" and "NOT with jumbo" in out[0]["detail"]
    n.peer_alive = False
    out = qsfp.peer_checks(n.host(), port, ["192.168.100.2", "192.168.101.2"], tcp=lambda *a: False)
    assert out[0]["status"] == "fail" and "no answer" in out[0]["detail"] and "apply --node B" in out[0]["fix"]
    assert out[-1]["check"] == "qsfp ssh" and out[-1]["status"] == "warn"


def test_peer_checks_without_a_matching_peer_ask_for_one(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    port = qsfp.discover(n.host()).ports[0]
    (c,) = qsfp.peer_checks(n.host(), port, ["10.9.9.9"])
    assert c["status"] == "warn" and "--peer" in c["fix"]


def test_scan_finds_hosts_that_answer_on_ssh_and_skips_itself(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    twin = qsfp.discover(n.host()).ports[0].primary
    seen = []

    def connect(ip, port, timeout, src):
        seen.append((ip, port, src))
        return ip in ("192.168.100.2", "192.168.100.1")

    assert qsfp.scan(twin, connect=connect) == ["192.168.100.2"]
    assert len(seen) == 253 and all(p == 22 and s == "192.168.100.1" for _, p, s in seen)
    twin.ipv4 = ["10.0.0.0/8"]
    assert qsfp.scan(twin, connect=connect) == []                      # never sweeps a huge range


def test_identify_asks_ssh_for_the_gpu_and_rejects_odd_user_names(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    assert qsfp.identify("192.168.100.2", "sparkuser", n.host()) == "NVIDIA GB10"
    ssh = [c for c in n.calls if c[0] == "ssh"][0]
    assert "BatchMode=yes" in ssh and "sparkuser@192.168.100.2" in ssh
    with pytest.raises(qsfp.QsfpError, match="not a user name"):
        qsfp.identify("192.168.100.2", "x; rm -rf /", n.host())
    with pytest.raises(qsfp.QsfpError):
        qsfp.identify("192.168.100.2", "-oProxyCommand=evil", n.host())
    with pytest.raises(qsfp.QsfpError, match="not an IPv4 address"):
        qsfp.identify("-oProxyCommand=evil", "sparkuser", n.host())


def test_describe_is_json_safe(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    out = qsfp.describe(qsfp.discover(n.host()))
    assert json.loads(json.dumps(out))[0]["twins"][1]["iface"] == SECONDARY


# ---- what the independent review found -----------------------------------------------------------------
def test_backups_live_in_a_root_owned_place_outside_the_service_users_state(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, d=d)
    qsfp.apply(host, qsfp.plan_for_node(d, "A", subnet="10.77.0.0/24", host=host), d=d)
    assert host.backup_dir == host.root / "var/backups/twinspark-qsfp"
    (backup,) = host.backup_dir.glob("*.*")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600 and stat.S_IMODE(host.backup_dir.stat().st_mode) == 0o700
    assert "var/lib/twinspark" not in str(host.backup_dir)


def test_revert_will_not_copy_back_a_backup_it_cannot_trust(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, d=d)
    qsfp.apply(host, qsfp.plan_for_node(d, "A", subnet="10.77.0.0/24", host=host), d=d)
    (backup,) = host.backup_dir.glob("*.*")
    # 1. somebody swapped the content: no marker
    good = backup.read_text()
    backup.write_text("network:\n  version: 2\n  ethernets: {enp1s0f1np1: {addresses: [10.9.9.9/8]}}\n")
    with pytest.raises(qsfp.QsfpError, match="not a backup TwinSpark can trust"):
        qsfp.revert(host)
    # 2. right content, wrong owner (the service user owns /var/lib/twinspark, not root)
    backup.write_text(good)
    import os
    stranger = node.host(real=True)
    stranger.owner_uid = os.getuid() + 1
    with pytest.raises(qsfp.QsfpError, match="not a backup TwinSpark can trust"):
        qsfp.revert(stranger)
    # 3. group-writable
    backup.chmod(0o664)
    with pytest.raises(qsfp.QsfpError, match="not a backup TwinSpark can trust"):
        qsfp.revert(host)
    backup.chmod(0o600)
    assert qsfp.revert(host)["restored"]


def test_the_critical_section_ignores_ctrl_c_and_hangup_and_restores_the_handlers(node, monkeypatch):
    import signal
    seen = {}
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    real_run = node.run

    def run(argv, timeout=15.0):
        if argv[:2] == ["netplan", "apply"]:
            seen["during"] = {s: signal.getsignal(s) for s in before}
        return real_run(argv, timeout)

    monkeypatch.setattr(qsfp, "RUN", run)
    host, d, plan = real(node)
    qsfp.apply(host, plan, d=d)
    assert all(h == signal.SIG_IGN for h in seen["during"].values())
    assert {s: signal.getsignal(s) for s in before} == before


def test_losing_the_management_route_during_apply_triggers_the_rollback(node):
    node.fail.update({"drop-mgmt-once", "stale"})
    host, d, plan = real(node)
    with pytest.raises(qsfp.QsfpError, match=r"did not come up.*enP7s7 lost its default route.*put back"):
        qsfp.apply(host, plan, d=d, wait_s=0.0)
    assert not host.managed_file.exists()
    # networkd kept the new addresses after the file went away: the rollback took exactly those back, and the MTU
    twins = qsfp.discover(host).ports[0].twins
    assert all(t.ipv4 == [] and t.mtu == 1500 for t in twins)
    assert node.ifaces[MGMT]["ips"] == ["10.0.0.50/24"] and node.default_dev == MGMT


def test_a_rollback_never_removes_an_address_that_was_there_before(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, primary_ips=["192.168.100.1/24"], mtu=9000)
    monkeypatch.setattr(qsfp, "RUN", n.run)
    monkeypatch.setattr(qsfp, "_which", lambda name: "/usr/sbin/netplan")
    n.fail.update({"apply-once", "stale"})
    host = n.host(real=True)
    plan = qsfp.plan_for_node(qsfp.discover(host), None, host=host)               # only the secondary is ours
    with pytest.raises(qsfp.QsfpError, match="put back"):
        qsfp.apply(host, plan)
    assert n.ifaces[PRIMARY]["ips"] == ["192.168.100.1/24"] and n.ifaces[SECONDARY]["ips"] == []


def test_revert_removes_addresses_that_networkd_would_have_kept(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, d=d)
    node.fail.add("stale")
    out = qsfp.revert(host)
    assert all(t.ipv4 == [] for t in qsfp.discover(host).ports[0].twins)
    assert "keeps its MTU until the next reboot" in out["message"] and "mtu 1500" in out["message"]


def test_revert_has_the_same_guards_as_apply(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, d=d)
    node.ssh_clients = ["192.168.100.2"]
    with pytest.raises(qsfp.QsfpError, match="remote session"):
        qsfp.revert(host)
    assert host.managed_file.exists()
    node.ssh_clients = []
    node.default_dev = PRIMARY
    with pytest.raises(qsfp.QsfpError, match="default route"):
        qsfp.revert(host)


def test_an_ipv6_link_local_ssh_session_names_its_interface(node):
    host = node.host(real=True)
    d = qsfp.discover(host)
    plan = qsfp.plan_for_node(d, "A", host=host)
    node.ssh_clients = [f"[fe80::1%{SECONDARY}]"]
    assert any("remote session (fe80::1%" in p for p in qsfp.preflight(host, d, plan))
    node.ssh_clients = ["[fe80::1%enP7s7]"]
    assert qsfp.preflight(host, d, plan) == []


def test_an_unreadable_routing_table_blocks_instead_of_switching_the_guard_off(node):
    node.fail.add("no-route-table")
    host = node.host(real=True)
    d = qsfp.discover(host)
    assert d.routes_known is False
    plan = qsfp.plan_for_node(d, "A", host=host)
    assert any("routing table cannot be read" in p for p in qsfp.preflight(host, d, plan))
    assert qsfp.preflight(node.host(), d, plan) == []                    # a sandbox root runs nothing, so no complaint


def test_multipath_default_routes_count(node):
    node.multipath = True
    from twinspark import hostprobe
    assert hostprobe.default_routes(lambda argv, t=8: node.run(argv, t)[:2]) == ["docker0", MGMT]
    assert hostprobe.default_routes(lambda argv, t=8: (1, "")) is None
    assert hostprobe.default_routes(lambda argv, t=8: (0, "")) == []
    assert hostprobe.default_routes(lambda argv, t=8: (0, "not json")) is None


def test_link_local_addresses_do_not_count_as_configured(tmp_path, monkeypatch):
    n = FakeNode(tmp_path, primary_ips=["169.254.11.1/16"], secondary_ips=["169.254.12.1/16"])
    monkeypatch.setattr(qsfp, "RUN", n.run)
    host = n.host()
    d = qsfp.discover(host)
    assert all(t.ipv4 == [] for t in d.ports[0].twins)
    plan = qsfp.plan_for_node(d, "A", host=host)
    assert [e.cidr for e in plan.entries] == ["192.168.100.1/24", "192.168.101.1/24"] and plan.kept == []


def test_temporary_addresses_are_recorded_in_run_and_revert_needs_no_node(node):
    host, d, plan = real(node)
    qsfp.apply(host, plan, temporary=True, d=d)
    assert host.temp_state == host.root / "run/twinspark-qsfp-temporary.json"
    rec = json.loads(host.temp_state.read_text())
    assert rec[PRIMARY] == {"cidrs": ["192.168.100.1/24"], "mtu": 1500}
    assert qsfp.temp_recorded(host)
    out = qsfp.revert(host, temporary=True)                              # no plan: the record says what to undo
    assert out["changed"] and not qsfp.temp_recorded(host)
    twins = qsfp.discover(host).ports[0].twins
    assert all(t.ipv4 == [] and t.mtu == 1500 for t in twins)             # addresses gone, MTU back


def test_the_netplan_file_marks_the_links_optional_so_boot_never_waits_for_them(node):
    _, _, plan = plan_a(node)
    assert all(cfg["optional"] is True for cfg in yaml.safe_load(plan.text)["network"]["ethernets"].values())


def test_a_configured_gid_index_that_the_devices_no_longer_use_is_flagged(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    d = qsfp.discover(n.host())
    ok = {c["check"]: c for c in qsfp.checks(d, configured_gid=3)}
    assert "qsfp gid" not in ok
    for t in d.ports[0].twins:                                           # link-local gone: IPv4 GIDs move to 0/1
        t.gids = [{"index": 1, "ipv4": t.addr}]
    bad = {c["check"]: c for c in qsfp.checks(d, configured_gid=3)}
    assert bad["qsfp gid"]["status"] == "warn" and "tsm rdma --apply" in bad["qsfp gid"]["fix"]


def test_identify_accepts_user_names_with_dots(tmp_path, monkeypatch):
    n = good_node(tmp_path, monkeypatch)
    assert qsfp.identify("192.168.100.2", "first.last", n.host()) == "NVIDIA GB10"
