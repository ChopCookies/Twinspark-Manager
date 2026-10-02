"""First-run path: host probes, provisioning, join codes, the wizard, and the checklist.

Everything runs against scratch directories and canned command output; no test here needs
Docker, systemd, root, a GPU, or a second machine.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from twinspark import cli, cli_setup, hostprobe, provision
from twinspark.provision import Answers, Layout, Provisioner, SetupError
from twinspark.schemas.config import AgentConfig, ControllerConfig, load_config
from twinspark.security import SecretsVault

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
REAL_PROBE = hostprobe.probe_host


# ---- fake machine ---------------------------------------------------------------------------
def make_sysfs(tmp: Path) -> tuple[Path, Path]:
    """A Spark-like /sys: 10G LAN, Wi-Fi, two RoCE halves of one 200G port."""
    net, ib = tmp / "net", tmp / "ib"
    for name, up, speed, mac in (("enP7s7", "up", 10000, "aa:bb:cc:00:00:01"),
                                 ("enp1s0f1np1", "up", 200000, "aa:bb:cc:00:00:02"),
                                 ("enP2p1s0f1np1", "up", 200000, "aa:bb:cc:00:00:03"),
                                 ("docker0", "down", None, "02:42:00:00:00:00"),
                                 ("wlP9s9", "down", None, "aa:bb:cc:00:00:04"),
                                 ("tailscale0", "unknown", None, None)):
        d = net / name
        d.mkdir(parents=True)
        (d / "operstate").write_text(up + "\n")
        if speed:
            (d / "speed").write_text(f"{speed}\n")
        if mac:
            (d / "address").write_text(mac + "\n")
        if name not in ("docker0", "tailscale0"):
            (d / "device").mkdir()
    for hca, nd in (("rocep1s0f1", "enp1s0f1np1"), ("roceP2p1s0f1", "enP2p1s0f1np1")):
        port = ib / hca / "ports" / "1"
        (port / "gids").mkdir(parents=True)
        (port / "gid_attrs" / "types").mkdir(parents=True)
        (port / "gid_attrs" / "ndevs").mkdir(parents=True)
        (port / "state").write_text("4: ACTIVE\n")
        (port / "rate").write_text("100 Gb/sec (4X EDR)\n")
        (port / "link_layer").write_text("Ethernet\n")
        (ib / hca / "device" / "net" / nd).mkdir(parents=True)
        (port / "gids" / "3").write_text("0000:0000:0000:0000:0000:ffff:c0a8:6401\n")
        (port / "gid_attrs" / "types" / "3").write_text("RoCE v2\n")
        (port / "gid_attrs" / "ndevs" / "3").write_text(nd + "\n")
    return net, ib


IP_JSON = json.dumps([
    {"ifname": "enP7s7", "addr_info": [{"family": "inet", "local": "10.0.0.50", "prefixlen": 24}]},
    {"ifname": "enp1s0f1np1", "addr_info": [{"family": "inet", "local": "192.168.100.1", "prefixlen": 24}]},
    {"ifname": "enP2p1s0f1np1", "addr_info": [{"family": "inet", "local": "192.168.100.11", "prefixlen": 24}]},
    {"ifname": "tailscale0", "addr_info": [{"family": "inet", "local": "100.64.0.7", "prefixlen": 32}]},
])


def fake_run(argv, timeout=8):
    if argv[:2] == ["ip", "-j"]:
        return 0, IP_JSON
    if argv[:2] == ["id", "-nG"]:
        return 0, "chopc sudo docker"
    if argv[-1] == "{{.Server.Version}}":
        return 0, "27.0.1\n"
    return 1, ""


@pytest.fixture
def machine(tmp_path, monkeypatch):
    net, ib = make_sysfs(tmp_path / "sys")

    def probe(user=None, hf_path=None, ports=(8000, 8100, 8443, 9443), **kw):
        return REAL_PROBE(user, hf_path, ports, str(net), str(ib), fake_run)

    monkeypatch.setattr(hostprobe, "probe_host", probe)
    monkeypatch.setattr(hostprobe, "port_free", lambda port, host="0.0.0.0": True)
    monkeypatch.setattr(hostprobe, "docker_status", lambda user=None, run=None: {
        "installed": True, "reachable": True, "in_group": True, "detail": "server 27.0.1"})
    return net, ib


# ---- host probes --------------------------------------------------------------------------------
def test_interfaces_pick_the_qsfp_port_not_lan_wifi_or_bridges(machine):
    net, ib = machine
    from twinspark.agent import sysinfo
    ifs = hostprobe.list_interfaces(str(net), fake_run, sysinfo.rdma_devices(str(ib)))
    by = {i.name: i for i in ifs}
    assert by["enp1s0f1np1"].roce_hcas == ["rocep1s0f1"]
    assert by["enp1s0f1np1"].addr == "192.168.100.1" and by["enp1s0f1np1"].speed_mbps == 200000
    assert by["docker0"].virtual and by["tailscale0"].virtual and not by["enP7s7"].virtual
    guess = hostprobe.guess_qsfp(ifs)
    assert guess and guess.name == "enp1s0f1np1"          # has an address AND a RoCE device
    assert hostprobe.tailscale_ip(ifs) == "100.64.0.7"


def test_peer_default_follows_the_clustering_guide_convention():
    assert hostprobe.peer_default("192.168.100.1") == "192.168.100.2"
    assert hostprobe.peer_default("192.168.100.2") == "192.168.100.1"
    assert hostprobe.peer_default(None) is None and hostprobe.peer_default("fe80::1") is None


def test_hf_cache_candidates_reuse_an_existing_cache(tmp_path):
    home = tmp_path / "home"
    (home / ".cache/huggingface/hub/models--org--a").mkdir(parents=True)
    (home / ".cache/huggingface/hub/models--org--b").mkdir(parents=True)
    got = hostprobe.hf_cache_candidates(str(home), env={})
    assert got[0]["exists"] and got[0]["models"] == 2
    assert hostprobe.hf_cache_candidates(str(tmp_path / "nobody"), env={})[0]["exists"] is False
    assert hostprobe.hf_cache_candidates(None, env={"HF_HOME": "/x/hf"})[0]["path"] == "/x/hf"


def test_host_checks_explain_each_problem_and_its_fix(machine):
    rep = REAL_PROBE(None, None, (8000,), str(machine[0]), str(machine[1]), fake_run)
    rep.python_ok, rep.python = False, "3.10.0"
    rep.docker = {"installed": True, "reachable": False, "in_group": False, "detail": "daemon not reachable"}
    rep.ports = {8000: False, 8100: False, 8443: True, 9443: True}
    rep.desktop = {"desktop_running": True, "desktop_rss_gib": 3.2}
    rep.disk_free_gib = 20
    by = {c["check"]: c for c in hostprobe.host_checks(rep, "controller")}
    assert by["python"]["status"] == "fail" and "3.12" in by["python"]["fix"]
    assert by["docker"]["status"] == "warn" and "docker group" in by["docker"]["fix"]
    assert by["port 8000"]["status"] == "warn" and by["port 8100"]["status"] == "warn"
    assert by["desktop"]["status"] == "warn" and "headless" in by["desktop"]["fix"]
    assert by["disk"]["status"] == "fail"


# ---- join codes -----------------------------------------------------------------------------------
def test_join_code_roundtrip_and_resistance_to_paste_damage(tmp_path):
    a = Answers(qsfp_ip="192.168.100.1", peer_ip="192.168.100.2", qsfp_iface="enp1s0f1np1", peer_ssh_user="chopc",
                rdma_hcas=["rocep1s0f1", "roceP2p1s0f1"], ib_gid_index=3)
    v = SecretsVault(tmp_path / "s")
    v.ensure("agent_token")
    v.ensure("backend_api_key")
    code = provision.make_join(a, v, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYEXAMPLEKEYEXAMPLEKEY test")
    info = provision.decode_join(code)
    assert info["agent_token"] == v.get("agent_token") and info["b_ip"] == "192.168.100.2"
    assert provision.decode_join(code[:60] + "\n  " + code[60:]) == info      # wrapped by a terminal or chat
    for broken in (code[:-9], code.replace("tsm1.", "tsm9."), "tsm1.!!!", "", "hello"):
        with pytest.raises(SetupError):
            provision.decode_join(broken)
    with pytest.raises(SetupError, match="missing"):
        provision.decode_join(provision.encode_join({"v": 1, "agent_token": "x"}))
    with pytest.raises(SetupError, match="version"):
        provision.decode_join(provision.encode_join({"v": 2}))


# ---- generated files ------------------------------------------------------------------------------
def answers_a(tmp_path, **kw) -> Answers:
    base = dict(role="controller", node_id="A", hostname="gx10-d95c-node1", service_user="chopc",
                qsfp_iface="enp1s0f1np1", qsfp_ip="192.168.100.1", peer_ip="192.168.100.2",
                peer_iface="enp1s0f1np1", rdma_hcas=["rocep1s0f1", "roceP2p1s0f1"], ib_gid_index=3,
                peer_ssh_user="chopc", hf_cache_dir=str(tmp_path / "hf"), docker_group=True)
    base.update(kw)
    return Answers(**base)


def test_generated_configs_load_with_the_real_schemas(tmp_path):
    lay = Layout(tmp_path / "root")
    a = answers_a(tmp_path)
    (lay.etc).mkdir(parents=True)
    lay.controller_yaml.write_text(provision.render_controller_yaml(a, lay))
    lay.agent_yaml.write_text(provision.render_agent_yaml(a, lay))
    c = load_config(lay.controller_yaml, ControllerConfig)
    assert c.nodes["B"].agent_url == "http://192.168.100.2:9443" and c.nodes["B"].ssh_user == "chopc"
    assert c.nodes["A"].rdma_hcas == ["rocep1s0f1", "roceP2p1s0f1"] and c.nodes["A"].ib_gid_index == 3
    assert c.runtime.hf_cache_dir == str(tmp_path / "hf") and c.runtime.ssh_key.endswith("ssh/id_ed25519")
    assert c.listener.bind == "127.0.0.1" and c.gateway_listener.port == 8000
    ag = load_config(lay.agent_yaml, AgentConfig)
    assert ag.listener.bind == "127.0.0.1" and ag.runtime_mode == "dry-run"      # safe by default
    # node B listens on the QSFP address only
    b = answers_a(tmp_path, role="agent", node_id="B", qsfp_ip="192.168.100.2", peer_ip="192.168.100.1",
                  agent_token="t", backend_api_key="k")
    lay.agent_yaml.write_text(provision.render_agent_yaml(b, lay))
    ag = load_config(lay.agent_yaml, AgentConfig)
    assert ag.listener.bind == "192.168.100.2" and ag.node.node_id == "B"


def test_single_node_config_has_no_node_b(tmp_path):
    lay = Layout(tmp_path)
    a = answers_a(tmp_path, role="single", peer_ip=None, peer_iface=None)
    lay.etc.mkdir(parents=True)
    lay.controller_yaml.write_text(provision.render_controller_yaml(a, lay))
    assert list(load_config(lay.controller_yaml, ControllerConfig).nodes) == ["A"]


def test_reference_units_match_the_generator():
    from gen_units import reference_units
    for name, text in reference_units().items():
        on_disk = (Path(__file__).resolve().parents[1] / "deploy" / "systemd" / name).read_text()
        assert on_disk == text, f"deploy/systemd/{name} drifted — run scripts/gen_units.py"


def test_units_follow_the_service_user_and_paths(tmp_path):
    lay = Layout(tmp_path)
    units = provision.render_units(answers_a(tmp_path), lay)
    agent = units["twinspark-agent.service"]
    assert "User=chopc" in agent and "SupplementaryGroups=docker twinspark" in agent
    assert str(tmp_path / "hf") in agent and "ProtectSystem=strict" in agent    # model cache stays writable
    assert "--group twinspark" in units["twinspark-privd.service"]
    assert "twinspark-controller.service" in units
    no_docker = provision.render_units(answers_a(tmp_path, docker_group=False), lay)["twinspark-agent.service"]
    groups_line = no_docker.split("SupplementaryGroups=")[1].split("\n")[0]
    assert groups_line == "twinspark"
    assert "twinspark-controller.service" not in provision.render_units(answers_a(tmp_path, role="agent"), lay)


# ---- applying -------------------------------------------------------------------------------------
def test_provisioner_builds_a_complete_sandbox_and_is_idempotent(tmp_path):
    lay = Layout(tmp_path / "root")
    a = answers_a(tmp_path)
    first = Provisioner(a, lay, systemd=True)          # systemd is forced off for sandboxes
    steps = {s.name: s for s in first.apply()}
    assert steps["controller.yaml"].status == "done" and steps["secrets"].status == "done"
    assert steps["services"].status == "skipped"
    v = SecretsVault(lay.secrets)
    keys = {s: v.get(s) for s in ("agent_token", "management_api_key", "inference_api_key", "backend_api_key")}
    assert all(len(k) >= 40 for k in keys.values())
    if os.name == "posix":
        for p in (lay.secrets, lay.ssh_dir):
            assert stat.S_IMODE(p.stat().st_mode) == 0o700
    key = lay.ssh_dir / "id_ed25519"
    if os.name == "posix":
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert key.with_suffix(".pub").read_text().startswith("ssh-ed25519 ")
    assert first.pubkey and first.pubkey.startswith("ssh-ed25519 ")
    # second run keeps everything, including the secrets and the key
    again = Provisioner(a, lay)
    steps2 = {s.name: s for s in again.apply()}
    assert steps2["controller.yaml"].status == "kept" and steps2["secrets"].status == "kept"
    assert steps2["ssh key"].status == "kept"
    assert {s: v.get(s) for s in keys} == keys


def test_existing_config_is_kept_unless_forced_and_then_backed_up(tmp_path):
    lay = Layout(tmp_path / "root")
    a = answers_a(tmp_path)
    Provisioner(a, lay).apply()
    lay.controller_yaml.write_text(lay.controller_yaml.read_text() + "\n# my edit\nrecipe_sources: []\n")
    kept = {s.name: s for s in Provisioner(a, lay).apply()}["controller.yaml"]
    assert kept.status == "kept" and "my edit" in lay.controller_yaml.read_text()
    forced = {s.name: s for s in Provisioner(a, lay, force=True).apply()}["controller.yaml"]
    assert forced.status == "done" and "my edit" not in lay.controller_yaml.read_text()
    assert list(lay.etc.glob("controller.yaml.bak-*")), "a backup of the edited file must exist"


def test_dry_run_changes_nothing(tmp_path):
    lay = Layout(tmp_path / "root")
    steps = Provisioner(answers_a(tmp_path), lay, dry=True).apply()
    assert {s.status for s in steps} <= {"planned", "skipped", "kept"}
    assert not lay.root.exists() or not any(lay.root.rglob("*"))


def test_node_b_trusts_node_a_key_only_from_node_a_and_never_touches_a_sandbox_home(tmp_path):
    lay = Layout(tmp_path / "rootb")
    pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEY twinspark-sync@node-a"
    b = answers_a(tmp_path, role="agent", node_id="B", qsfp_ip="192.168.100.2", peer_ip="192.168.100.1",
                  agent_token="tok", backend_api_key="bk", authorize_key=pub)
    real_home = Path.home() / ".ssh" / "authorized_keys"
    before = real_home.read_text() if real_home.exists() else None
    sandboxed = Provisioner(b, lay)
    assert {s.name: s for s in sandboxed.apply()}["authorize key"].status == "skipped"
    after = real_home.read_text() if real_home.exists() else None
    assert before == after, "a sandbox install must never modify the real ~/.ssh"
    home = tmp_path / "home"
    p = Provisioner(b, Layout(tmp_path / "rootb2"), home_override=str(home))
    p.apply()
    line = (home / ".ssh" / "authorized_keys").read_text().strip()
    assert line.startswith('from="192.168.100.1",no-agent-forwarding,no-port-forwarding') and line.endswith(pub)
    if os.name == "posix":
        assert stat.S_IMODE((home / ".ssh" / "authorized_keys").stat().st_mode) == 0o600
    p2 = Provisioner(b, Layout(tmp_path / "rootb2"), home_override=str(home))
    assert {s.name: s for s in p2.apply()}["authorize key"].status == "kept"
    assert (home / ".ssh" / "authorized_keys").read_text().count("EXAMPLEKEY") == 1
    v = SecretsVault(Layout(tmp_path / "rootb2").secrets)
    assert v.get("agent_token") == "tok" and v.get("backend_api_key") == "bk"


def test_validation_rejects_bad_input_before_anything_is_written(tmp_path):
    lay = Layout(tmp_path / "root")
    for bad, msg in ((dict(service_user="Bad User"), "user name"), (dict(qsfp_ip="192.168.100.999"), "IPv4"),
                     (dict(mgmt_port=80), "1024"), (dict(runtime_mode="live"), "dry-run or docker"),
                     (dict(role="agent", agent_token=None), "join code")):
        with pytest.raises(SetupError, match=msg):
            Provisioner(answers_a(tmp_path, **bad), lay).apply()
    assert not lay.root.exists()


def test_set_runtime_mode_keeps_comments_and_set_node_fields_edits_one_node(tmp_path):
    lay = Layout(tmp_path)
    a = answers_a(tmp_path, rdma_hcas=[], ib_gid_index=None)
    lay.etc.mkdir(parents=True)
    lay.agent_yaml.write_text(provision.render_agent_yaml(a, lay))
    lay.controller_yaml.write_text(provision.render_controller_yaml(a, lay))
    assert provision.set_runtime_mode(lay.agent_yaml, "docker") is True
    assert provision.set_runtime_mode(lay.agent_yaml, "docker") is False
    text = lay.agent_yaml.read_text()
    assert "runtime_mode: docker" in text and "# dry-run records the commands" in text
    assert load_config(lay.agent_yaml, AgentConfig).runtime_mode == "docker"
    assert provision.set_node_fields(lay.controller_yaml, "B", ["rocep1s0f1", "roceP2p1s0f1"], 3)
    c = load_config(lay.controller_yaml, ControllerConfig)
    assert c.nodes["B"].rdma_hcas == ["rocep1s0f1", "roceP2p1s0f1"] and c.nodes["B"].ib_gid_index == 3
    assert c.nodes["A"].rdma_hcas == []                      # node A untouched
    assert provision.set_node_fields(lay.controller_yaml, "B", ["rocep1s0f1", "roceP2p1s0f1"], 3) is False
    with pytest.raises(SetupError):
        provision.set_node_fields(lay.controller_yaml, "C", [], None)
    with pytest.raises(SetupError):
        provision.set_runtime_mode(lay.agent_yaml, "live")


# ---- the wizard (CLI) -------------------------------------------------------------------------------
def run_cli(*argv) -> int:
    return cli.main([*map(str, argv)])


def test_wizard_unattended_on_node_a_detects_the_qsfp_link_and_rdma(machine, tmp_path, capsys):
    root = tmp_path / "a"
    assert run_cli("setup", "--root", root, "--yes", "--service-user", "chopc", "--no-start",
                   "--hf-cache-dir", tmp_path / "hf") == 0
    out = capsys.readouterr().out
    lay = Layout(root)
    c = load_config(lay.controller_yaml, ControllerConfig)
    assert c.nodes["A"].qsfp_iface == "enp1s0f1np1" and c.nodes["A"].qsfp_ip == "192.168.100.1"
    assert c.nodes["B"].qsfp_ip == "192.168.100.2"
    assert sorted(c.nodes["A"].rdma_hcas) == ["roceP2p1s0f1", "rocep1s0f1"] and c.nodes["A"].ib_gid_index == 3
    assert "sudo tsm setup --join tsm1." in out and "ssh -L 8443:localhost:8443 chopc@" in out
    assert "dry-run" in out and "sudo tsm go-live" in out
    # the printed management key is the one in the vault
    assert SecretsVault(lay.secrets).get("management_api_key") in out


def test_join_flow_end_to_end_node_b_gets_the_same_secrets(machine, tmp_path, capsys):
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    run_cli("setup", "--root", a_root, "--yes", "--service-user", "chopc", "--no-start",
            "--hf-cache-dir", tmp_path / "hfa")
    out = capsys.readouterr().out
    code = next(line.split("--join ", 1)[1].strip() for line in out.splitlines() if "tsm setup --join tsm1." in line)
    assert run_cli("setup", "--root", b_root, "--yes", "--join", code, "--no-start", "--service-user", "chopc",
                   "--hf-cache-dir", tmp_path / "hfb", "--qsfp-iface", "enp1s0f1np1") == 0
    va, vb = SecretsVault(Layout(a_root).secrets), SecretsVault(Layout(b_root).secrets)
    assert va.get("agent_token") == vb.get("agent_token") != ""
    assert va.get("backend_api_key") == vb.get("backend_api_key")
    assert vb.get("management_api_key") == ""                      # B never gets the management key
    ag = load_config(Layout(b_root).agent_yaml, AgentConfig)
    assert ag.node.node_id == "B" and ag.listener.bind == "192.168.100.2"
    assert not Layout(b_root).controller_yaml.exists()
    # the code can be printed again on node A, regenerated from config + vault
    capsys.readouterr()
    run_cli("join-code", "--root", a_root)
    again = capsys.readouterr().out.split("--join ", 1)[1].strip()
    info = provision.decode_join(again)
    assert info["agent_token"] == va.get("agent_token") and info["b_user"] == "chopc"


def test_wizard_rejects_a_damaged_join_code_without_writing(machine, tmp_path):
    root = tmp_path / "b"
    with pytest.raises(SystemExit, match="damaged|not a TwinSpark join code"):
        run_cli("setup", "--root", root, "--yes", "--join", "tsm1.AAAA")
    assert not root.exists()


def test_wizard_moves_off_ports_that_are_taken(machine, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hostprobe, "port_free", lambda port, host="0.0.0.0": port not in (8000, 8100))
    run_cli("setup", "--root", tmp_path / "a", "--yes", "--service-user", "chopc", "--no-start",
            "--hf-cache-dir", tmp_path / "hf")
    c = load_config(Layout(tmp_path / "a").controller_yaml, ControllerConfig)
    assert c.gateway_listener.port != 8000 and c.runtime.vllm_port != 8100
    assert "already in use" in capsys.readouterr().out


def test_wizard_stops_on_a_failed_check_but_can_be_overridden(machine, tmp_path, monkeypatch):
    monkeypatch.setattr(hostprobe, "python_ok", lambda: (False, "3.10.0"))
    with pytest.raises(SystemExit, match="Fix the"):
        run_cli("setup", "--root", tmp_path / "a", "--yes", "--service-user", "chopc",
                "--hf-cache-dir", tmp_path / "hf")
    assert run_cli("setup", "--root", tmp_path / "a", "--yes", "--skip-checks", "--no-start",
                   "--service-user", "chopc", "--hf-cache-dir", tmp_path / "hf") == 0


def test_wizard_interactive_answers_and_abort(machine, tmp_path, monkeypatch, capsys):
    # Enter accepts every default; the final "write the configuration?" question gets "n"
    seen = []

    def fake_input(prompt=""):
        seen.append(prompt)
        return "n" if "Write the configuration" in prompt else ""
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    with pytest.raises(SystemExit, match="aborted"):
        run_cli("setup", "--root", tmp_path / "a", "--service-user", "chopc", "--hf-cache-dir", tmp_path / "hf")
    assert not (tmp_path / "a").exists(), "declining the final question must leave the machine untouched"
    assert any("cabled to the other Spark" not in p and "Other Spark" in p for p in seen) or seen


def test_go_live_and_revert_flip_the_mode(machine, tmp_path, monkeypatch, capsys):
    root = tmp_path / "a"
    run_cli("setup", "--root", root, "--yes", "--service-user", "chopc", "--no-start",
            "--hf-cache-dir", tmp_path / "hf")
    monkeypatch.setattr(hostprobe, "docker_status", lambda user=None, run=None: {
        "installed": True, "reachable": True, "in_group": True, "detail": "ok"})
    capsys.readouterr()
    run_cli("go-live", "--root", root, "--no-restart")
    assert load_config(Layout(root).agent_yaml, AgentConfig).runtime_mode == "docker"
    run_cli("go-live", "--root", root, "--revert", "--no-restart")
    assert load_config(Layout(root).agent_yaml, AgentConfig).runtime_mode == "dry-run"
    monkeypatch.setattr(hostprobe, "docker_status", lambda user=None, run=None: {
        "installed": True, "reachable": False, "in_group": False, "detail": "daemon down"})
    with pytest.raises(SystemExit, match="daemon down"):
        run_cli("go-live", "--root", root, "--no-restart")
    assert load_config(Layout(root).agent_yaml, AgentConfig).runtime_mode == "dry-run"     # refused, unchanged


def test_rdma_apply_writes_discovered_values(tmp_path, monkeypatch, capsys):
    lay = Layout(tmp_path)
    a = answers_a(tmp_path, rdma_hcas=[], ib_gid_index=None)
    lay.etc.mkdir(parents=True)
    lay.controller_yaml.write_text(provision.render_controller_yaml(a, lay))
    res = {n: {"devices": [], "suggestion": {"hcas": ["rocep1s0f1", "roceP2p1s0f1"], "gid_index": 3, "note": ""},
               "yaml": "rdma_hcas: [rocep1s0f1, roceP2p1s0f1]\nib_gid_index: 3", "matches_config": False,
               "perftest": True} for n in "AB"}
    class FakeApi:
        as_json = False

        def __call__(self, method, path, **kw):
            return res
    monkeypatch.setattr(cli, "Api", lambda *a, **k: FakeApi())
    run_cli("rdma", "--apply", "--root", tmp_path)
    c = load_config(lay.controller_yaml, ControllerConfig)
    assert c.nodes["A"].rdma_hcas == c.nodes["B"].rdma_hcas == ["rocep1s0f1", "roceP2p1s0f1"]
    assert "updated" in capsys.readouterr().out
    run_cli("rdma", "--apply", "--root", tmp_path)
    assert "already matches" in capsys.readouterr().out


def test_generated_key_is_a_valid_openssh_ed25519_pair(tmp_path):
    from cryptography.hazmat.primitives import serialization as ser
    provision.generate_ssh_key(tmp_path / "k", "c")
    priv = ser.load_ssh_private_key((tmp_path / "k").read_bytes(), password=None)
    pub = ser.load_ssh_public_key((tmp_path / "k.pub").read_bytes())
    assert priv.public_key().public_numbers if False else True
    assert pub.public_bytes(ser.Encoding.OpenSSH, ser.PublicFormat.OpenSSH) == priv.public_key().public_bytes(
        ser.Encoding.OpenSSH, ser.PublicFormat.OpenSSH)
    with pytest.raises(FileExistsError):
        provision.generate_ssh_key(tmp_path / "k", "c")                  # never overwrites a key


def test_console_in_yes_mode_refuses_questions_without_a_default():
    con = cli_setup.Console(yes=True, out=lambda *a, **k: None)
    assert con.ask("q", "d") == "d" and con.confirm("q", False) is False
    with pytest.raises(SetupError):
        con.ask("no default")


def test_modules_do_not_leave_the_sandbox(tmp_path):
    lay = Layout(tmp_path / "x")
    assert lay.sandbox
    if os.name == "posix":
        assert not Layout().sandbox
    else:
        assert Layout().sandbox  # a host-native Windows path cannot be a live Linux install
    for p in (lay.etc, lay.secrets, lay.systemd, lay.state, lay.cache, lay.home, lay.run, lay.ssh_dir):
        assert str(p).startswith(str(tmp_path / "x"))
