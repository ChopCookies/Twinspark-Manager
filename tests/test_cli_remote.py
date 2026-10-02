"""`tsm remote|node|wake|netboot` and the terminal client. Nothing here can touch the real machine:
every command goes through a recording fake, and sandbox roots are used for all files."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import socket
import sys
import tarfile
import time
from pathlib import Path

import httpx
import pytest

from tests.test_setup_flow import answers_a, machine  # noqa: F401  (machine is used as a fixture)
from twinspark import cli, cli_remote, provision
from twinspark.demo import DemoCluster
from twinspark.provision import Layout, Provisioner, SetupError
from twinspark.remote import netboot, privops, termclient
from twinspark.remote import policy as pol
from twinspark.security import SecretsVault


def run_cli(*argv) -> int:
    return cli.main([*map(str, argv)])


@pytest.fixture(autouse=True)
def nothing_real_runs(monkeypatch):
    """Every system command is recorded, none is executed."""
    calls: list[list[str]] = []
    table: dict[str, tuple[int, str, str]] = {}

    def fake(argv, timeout=10):
        calls.append(list(argv))
        return table.get(argv[0].rsplit("/", 1)[-1], (127, "", "not installed"))

    def refuse(argv):
        raise AssertionError(f"a real command would have run: {argv}")

    monkeypatch.setattr(privops, "RUN", fake)
    monkeypatch.setattr(cli_remote, "_real_run", refuse)
    fake.calls, fake.table = calls, table
    return fake


@pytest.fixture
def run(nothing_real_runs):
    return nothing_real_runs


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A sandbox install of node A, and the CLI acting as root on it."""
    lay = Layout(tmp_path / "root")
    Provisioner(answers_a(tmp_path), lay, systemd=False).apply()
    monkeypatch.setattr(cli_remote, "_pick_priv", lambda cfg: _AlwaysRoot())
    return lay


class _AlwaysRoot(privops.LocalPriv):
    def available(self):
        return True


# ---- feature names ---------------------------------------------------------------------------
def test_feature_names_accept_dashes_all_and_none():
    assert cli_remote.parse_features("terminal,boot-next") == ["terminal", "boot_next"]
    assert cli_remote.parse_features("all") == list(pol.FEATURES)
    assert cli_remote.parse_features("none") == [] == cli_remote.parse_features("")
    assert cli_remote.parse_features("wol terminal") == ["terminal", "wol"]
    with pytest.raises(SetupError, match="unknown remote feature 'shell'"):
        cli_remote.parse_features("terminal,shell")


# ---- enable / disable / policy ---------------------------------------------------------------
def test_enable_and_disable_edit_the_policy_and_install_the_terminal_unit(box, capsys):
    root = box.root
    assert run_cli("remote", "enable", "terminal", "boot-next", "--root", root) == 0
    p = json.loads((box.etc / "remote-policy.json").read_text())
    assert p["terminal"] is True and p["boot_next"] is True and p["reboot"] is False and p["wol"] is False
    unit = (box.systemd / "twinspark-terminal.service").read_text()
    assert "User=chopc" in unit and "serve termd" in unit and "ProtectSystem" not in unit
    out = capsys.readouterr().out
    assert "now enabled: terminal, boot-next" in out and "shell as the TwinSpark user" in out
    assert run_cli("remote", "disable", "terminal", "--root", root) == 0
    p = json.loads((box.etc / "remote-policy.json").read_text())
    assert p["terminal"] is False and p["boot_next"] is True
    assert "now enabled: boot-next" in capsys.readouterr().out


def test_enable_all_then_policy_listing_and_dry_run(box, capsys):
    assert run_cli("remote", "enable", "all", "--root", box.root) == 0
    assert all(json.loads((box.etc / "remote-policy.json").read_text())[f] for f in pol.FEATURES)
    capsys.readouterr()
    assert run_cli("remote", "disable", "all", "--root", box.root, "--dry") == 0
    assert "dry run" in capsys.readouterr().out
    assert all(json.loads((box.etc / "remote-policy.json").read_text())[f] for f in pol.FEATURES), "dry run changed it"
    assert run_cli("remote", "policy", "--root", box.root) == 0
    out = capsys.readouterr().out
    assert out.count("on ") >= 5 and "terminal" in out and "boot-next" in out


def test_enable_with_no_features_shows_the_switches_and_how_to_use_them(box, capsys):
    assert run_cli("remote", "enable", "--root", box.root) == 0
    out = capsys.readouterr().out
    assert "everything is off" in out and "sudo tsm remote enable terminal" in out
    assert not (box.etc / "remote-policy.json").exists()


def test_unknown_feature_is_a_clean_error_and_changes_nothing(box):
    with pytest.raises(SystemExit) as err:
        run_cli("remote", "enable", "shell", "--root", box.root)
    assert "unknown remote feature" in str(err.value)
    assert not (box.etc / "remote-policy.json").exists()


def test_enable_terminal_needs_an_installed_node(tmp_path):
    with pytest.raises(SetupError, match="run `sudo tsm setup`"):
        provision.installed_terminal_unit(Layout(tmp_path / "empty"))


def test_plug_token_goes_into_the_vault_not_the_config(tmp_path, capsys):
    f = tmp_path / "tok"
    f.write_text("s3cr3t-plug-token\n")
    sec = tmp_path / "secrets"
    assert run_cli("--secrets-dir", sec, "remote", "plug-token", "--from-file", f) == 0
    assert SecretsVault(sec).get("plug_token") == "s3cr3t-plug-token"
    assert "s3cr3t-plug-token" not in capsys.readouterr().out
    f.write_text("two\nlines\n")
    with pytest.raises(SystemExit, match="one non-empty line"):
        run_cli("--secrets-dir", sec, "remote", "plug-token", "--from-file", f)


# ---- the setup wizard ------------------------------------------------------------------------
@pytest.mark.usefixtures("machine")
def test_wizard_leaves_remote_management_off_when_unattended(tmp_path):
    root = tmp_path / "a"
    assert run_cli("setup", "--root", root, "--yes", "--service-user", "chopc", "--no-start",
                   "--hf-cache-dir", tmp_path / "hf") == 0
    lay = Layout(root)
    assert not (lay.systemd / "twinspark-terminal.service").exists()
    assert not (lay.etc / "remote-policy.json").exists() or not any(
        json.loads((lay.etc / "remote-policy.json").read_text()).values())


@pytest.mark.usefixtures("machine")
def test_wizard_flag_turns_features_on_and_writes_the_terminal_unit(tmp_path, capsys):
    root = tmp_path / "a"
    assert run_cli("setup", "--root", root, "--yes", "--service-user", "chopc", "--no-start",
                   "--hf-cache-dir", tmp_path / "hf", "--remote", "terminal,reboot") == 0
    lay = Layout(root)
    p = json.loads((lay.etc / "remote-policy.json").read_text())
    assert p["terminal"] and p["reboot"] and not p["poweroff"] and not p["wol"]
    assert "--remote-policy" in (lay.systemd / "twinspark-privd.service").read_text()
    assert (lay.systemd / "twinspark-terminal.service").exists()
    cfg = (lay.etc / "agent.yaml").read_text()
    assert "remote_mgmt:" in cfg and "terminal_port: 9444" in cfg
    assert "remote          terminal, reboot" in capsys.readouterr().out


@pytest.mark.usefixtures("machine")
def test_wizard_rejects_a_misspelt_feature_before_writing_anything(tmp_path):
    root = tmp_path / "a"
    with pytest.raises(SystemExit, match="unknown remote feature"):
        run_cli("setup", "--root", root, "--yes", "--service-user", "chopc", "--no-start",
                "--hf-cache-dir", tmp_path / "hf", "--remote", "terminal,shel")
    assert not (Layout(root).etc / "agent.yaml").exists()


# ---- wake and netboot ------------------------------------------------------------------------
def test_wake_sends_a_real_magic_packet(capsys):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(5)
        port = rx.getsockname()[1]
        assert run_cli("wake", "AA-BB-CC-DD-EE-FF", "--broadcast", "127.0.0.1", "--port", port) == 0
        pkt, _ = rx.recvfrom(2048)
    assert pkt == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16
    assert "cannot be acknowledged" in capsys.readouterr().out


def test_wake_rejects_a_bad_mac_and_a_missing_interface():
    with pytest.raises(SystemExit, match="error"):
        run_cli("wake", "not-a-mac")
    with pytest.raises(SystemExit, match="no IPv4 address"):
        run_cli("wake", "aa:bb:cc:dd:ee:ff", "--iface", "doesnotexist0")


def test_netboot_plan_is_locked_to_one_mac_and_writes_the_config(tmp_path, capsys):
    root = tmp_path / "tftp"
    root.mkdir()
    (root / "snp.arm64.efi").write_bytes(b"x")
    out = tmp_path / "out"
    assert run_cli("netboot", "plan", "--mac", "AA:BB:CC:DD:EE:FF", "--iface", "enp1s0f1np1",
                   "--subnet", "192.168.100.0/24", "--tftp-root", root, "--out", out) == 0
    conf = (out / "dnsmasq-netboot.conf").read_text()
    for needle in ("port=0", "interface=enp1s0f1np1", "dhcp-range=192.168.100.0,proxy",
                   "dhcp-mac=set:target,aa:bb:cc:dd:ee:ff", "dhcp-ignore=tag:!target",
                   'pxe-service=tag:target,ARM64_EFI,"TwinSpark rescue boot",snp.arm64.efi',
                   f"tftp-root={root}"):
        assert needle in conf, needle
    assert "tsm remote boot <node> network" in capsys.readouterr().out


@pytest.mark.parametrize("kw,msg", [
    ({"mac": "nope"}, "not a MAC"),
    ({"bootfile": "../etc/passwd"}, "relative path"),
    ({"bootfile": "x;y"}, "relative path"),
    ({"tftp_root": "relative/dir"}, "absolute path"),
    ({"tftp_root": '/srv/tf tp"'}, "absolute path"),
    ({"iface": "eth0 -x"}, "interface name"),
    ({"minutes": 0}, "minutes"),
    ({"minutes": 100000}, "minutes"),
    ({"subnet": "300.1.1.1/8"}, "not a network"),
])
def test_netboot_refuses_values_that_could_end_up_in_the_config(kw, msg):
    args = {"mac": "aa:bb:cc:dd:ee:ff", "iface": "eth0", "bootfile": "snp.efi", "tftp_root": "/srv/tftp",
            "subnet": "10.0.0.0/24"}
    args.update(kw)
    minutes = args.pop("minutes", 30)
    with pytest.raises(netboot.NetbootError, match=msg):
        netboot.build_plan(args.pop("mac"), args.pop("iface"), args.pop("bootfile"), args.pop("tftp_root"),
                           minutes=minutes, **args)


def test_netboot_derives_the_network_from_the_port_and_warns_about_missing_pieces(tmp_path):
    from twinspark.hostprobe import Iface
    plan = netboot.build_plan("aa:bb:cc:dd:ee:ff", "enp1s0f1np1", "snp.efi", str(tmp_path / "t"),
                              list_ifaces=lambda: [Iface(name="enp1s0f1np1", ipv4=["192.168.100.1/24"])],
                              which=lambda n: None)
    assert plan.network == "192.168.100.0"
    assert any("does not exist" in w for w in plan.warnings) and any("dnsmasq is not installed" in w
                                                                       for w in plan.warnings)
    with pytest.raises(netboot.NetbootError, match="no IPv4"):
        netboot.build_plan("aa:bb:cc:dd:ee:ff", "x0", "snp.efi", "/srv/tftp",
                           list_ifaces=lambda: [Iface(name="x0")])


def test_netboot_serve_runs_dnsmasq_in_the_foreground_for_a_bounded_time_and_cleans_up(tmp_path):
    import subprocess
    root = tmp_path / "t"
    root.mkdir()
    plan = netboot.build_plan("aa:bb:cc:dd:ee:ff", "lo", "snp.efi", str(root), subnet="10.0.0.0/24", minutes=1,
                              which=lambda n: "/usr/sbin/dnsmasq")
    seen = {}

    class Proc:
        def __init__(self, argv, **kw):
            seen["argv"] = argv
            conf = Path(argv[2].split("=", 1)[1])
            seen["conf"] = conf
            seen["text"] = conf.read_text()
            seen["mode"] = conf.stat().st_mode & 0o777
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("dnsmasq", timeout)
            return 0

        def terminate(self):
            seen["terminated"] = True

    assert netboot.serve(plan, popen=Proc, which=lambda n: "/usr/sbin/dnsmasq") == 0
    assert seen["argv"][:2] == ["/usr/sbin/dnsmasq", "--keep-in-foreground"]
    assert seen["text"] == plan.config and seen["mode"] == 0o600 and seen["terminated"]
    assert not seen["conf"].exists(), "the config must not outlive the helper"
    with pytest.raises(netboot.NetbootError, match="not installed"):
        netboot.serve(plan, popen=Proc, which=lambda n: None)


# ---- tsm node (on the Spark itself) ----------------------------------------------------------
def test_node_status_shows_macs_and_policy(box, run, capsys):
    run.table["systemctl"] = (0, "active\n", "")
    assert run_cli("node", "status", "--root", box.root) == 0
    out = capsys.readouterr().out
    assert "node A" in out and "twinspark-agent" in out and "remote policy" in out and "network ports" in out


def test_node_doctor_explains_each_problem_with_a_fix(box, run, capsys, monkeypatch):
    run.table["timedatectl"] = (0, "no\n", "")
    pol.write_policy(box.etc / "remote-policy.json", {"terminal": True}, chown_root=False)
    (box.etc / "remote-policy.json").chmod(0o666)                 # group/other-writable: must be refused
    monkeypatch.setattr(cli_remote.shutil, "disk_usage", lambda p: type("U", (), {
        "total": 100 * 1024**3, "free": 2 * 1024**3, "used": 98 * 1024**3})())
    monkeypatch.setattr(pol, "load_policy", lambda path, require_root=True: pol.RemotePolicy(
        error="policy file is writable by group/other"))
    with pytest.raises(SystemExit) as err:                       # a failing check exits non-zero
        run_cli("node", "doctor", "--root", box.root)
    assert err.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL] system disk" in out and "docker system prune" in out
    assert "[WARN] clock" in out and "timedatectl set-ntp true" in out
    assert "[WARN] remote policy" in out and "writable by group/other" in out
    assert "[WARN] tool efibootmgr" in out and "apt install efibootmgr" in out


def test_node_logs_and_bundle_work_without_a_controller(box, run, tmp_path, capsys):
    run.table["journalctl"] = (0, "one\n\x1b[31mtwo error\x1b[0m\nthree\n", "")
    assert run_cli("node", "logs", "kernel", "--root", box.root, "--grep", "error") == 0
    assert capsys.readouterr().out.strip() == "two error"
    out = tmp_path / "b.tar.gz"
    assert run_cli("node", "bundle", "-o", out, "--root", box.root) == 0
    with tarfile.open(out) as t:
        names = [m.name for m in t.getmembers()]
    assert any(n.endswith("tsm/vault-slots.txt") for n in names) and any(n.endswith("README.txt") for n in names)


def test_node_reboot_schedules_through_systemd_and_can_be_cancelled(box, run, capsys):
    run.table["systemd-run"] = (0, "", "")
    run.table["systemctl"] = (3, "inactive\n", "")
    assert run_cli("node", "reboot", "--root", box.root, "-y", "--delay", "9") == 0
    sched = [c for c in run.calls if c[0].endswith("systemd-run")]
    assert sched and "--on-active=9s" in sched[0] and sched[0][-1] == "reboot"
    assert "reboot in 9 s" in capsys.readouterr().out
    run.calls.clear()
    run.table["systemctl"] = (0, "active\n", "")
    assert run_cli("node", "cancel", "--root", box.root) == 0
    assert any(c[:2] == [c[0], "stop"] and "twinspark-remote-reboot.timer" in c for c in run.calls)


def test_node_reboot_asks_first_and_force_skips_the_safety_checks(box, run, monkeypatch):
    run.table["systemd-run"] = (0, "", "")
    run.table["systemctl"] = (3, "inactive\n", "")
    monkeypatch.setattr(cli_remote.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="--yes"):
        run_cli("node", "reboot", "--root", box.root)
    assert not [c for c in run.calls if c[0].endswith("systemd-run")]
    monkeypatch.setattr(privops, "busy_package_manager", lambda proc=None: "dpkg")
    with pytest.raises(SystemExit, match="dpkg is running"):
        run_cli("node", "reboot", "--root", box.root, "-y")
    assert run_cli("node", "reboot", "--root", box.root, "-y", "--force") == 0


def test_node_boot_and_wol_use_the_helper_functions(box, run, capsys, tmp_path, monkeypatch):
    run.table["efibootmgr"] = (0, "BootCurrent: 0001\nBootOrder: 0001,0003\nBoot0001* ubuntu\n"
                                  "Boot0003* UEFI PXE IPv4 Intel(R) Ethernet\n", "")
    assert run_cli("node", "boot", "--root", box.root) == 0
    out = capsys.readouterr().out
    assert "booted from: 0001" in out and "0003" in out and "network" in out
    monkeypatch.setattr(privops, "_physical_ifaces", lambda: ["enP7s7"])
    sysnet = tmp_path / "sys"
    (sysnet / "enP7s7").mkdir(parents=True)
    (sysnet / "enP7s7" / "address").write_text("aa:bb:cc:00:00:01\n")
    monkeypatch.setattr(privops, "SYS_NET", sysnet)
    run.table["ethtool"] = (0, "Settings for enP7s7:\n\tSupports Wake-on: pumbg\n\tWake-on: g\n", "")
    assert run_cli("node", "wol", "--root", box.root) == 0
    assert "aa:bb:cc:00:00:01" in capsys.readouterr().out
    assert run_cli("node", "wol", "on", "enP7s7", "--root", box.root) == 0
    assert any(c[-3:] == ["enP7s7", "wol", "g"] or c[-4:] == ["-s", "enP7s7", "wol", "g"] for c in run.calls)
    with pytest.raises(SystemExit, match="name the port"):
        run_cli("node", "wol", "on", "--root", box.root)


def test_node_commands_say_so_on_a_machine_without_twinspark(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        run_cli("node", "status", "--root", tmp_path / "nothing")


# ---- the terminal client ---------------------------------------------------------------------
def test_ws_url_follows_the_api_scheme():
    assert termclient.ws_url("http://127.0.0.1:8443/", "/api/v1/remote/terminal/ws", "T") == \
        "ws://127.0.0.1:8443/api/v1/remote/terminal/ws?ticket=T"
    assert termclient.ws_url("https://tsm.example:443", "/p", "T") == "wss://tsm.example:443/p?ticket=T"
    with pytest.raises(termclient.TerminalClientError):
        termclient.ws_url("ftp://x", "/p", "T")


@pytest.fixture
async def demo():
    async with DemoCluster(remote={"terminal": True}) as d:
        yield d


async def open_ticket(demo, node="B"):
    async with httpx.AsyncClient(base_url=demo.url, headers={"x-api-key": demo.key}, timeout=20) as c:
        t = (await c.post("/api/v1/remote/terminal/ticket", json={"node": node})).json()
    return termclient.ws_url(demo.url, t["ws_path"], t["ticket"])


async def drive(url, script: bytes, timeout=30, wait_for: bytes = b"", then: bytes = b""):
    """Run the client with pipes for stdin/stdout. With ``wait_for``, ``then`` is typed only once that
    text has appeared on the screen (like a person waiting for output before pressing a key)."""
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    os.set_blocking(r_out, False)
    screen = bytearray()

    def drain() -> None:
        while True:
            try:
                data = os.read(r_out, 65536)
            except BlockingIOError:
                return
            if not data:
                return
            screen.extend(data)

    async def typist() -> None:
        os.write(w_in, script)
        if wait_for:
            end = time.monotonic() + 15
            while wait_for not in screen and time.monotonic() < end:
                drain()
                await asyncio.sleep(0.05)
            os.write(w_in, then)
        os.close(w_in)

    typing = asyncio.ensure_future(typist())
    try:
        res = await asyncio.wait_for(termclient.run_session(
            url, stdin_fd=r_in, stdout_fd=w_out, size=lambda: (100, 30), raw=True), timeout)
    finally:
        await asyncio.gather(typing, return_exceptions=True)
        os.close(w_out)
        drain()
        os.close(r_in)
        os.close(r_out)
    return res, bytes(screen)


@pytest.mark.skipif(sys.platform != "linux", reason="needs Linux PTYs")
async def test_client_runs_a_piped_session_to_the_end(demo):
    res, out = await drive(await open_ticket(demo), b"echo cli$((3*5))end\nexit 3\n")
    assert b"cli15end" in out
    assert res["session"] and (res["exit"] or {}).get("code") == 3


@pytest.mark.skipif(sys.platform != "linux", reason="needs Linux PTYs")
async def test_client_ends_a_session_when_stdin_closes_without_exit(demo):
    res, out = await drive(await open_ticket(demo, "A"), b"echo only$((4*5))\n")
    assert b"only20" in out and res["reason"]


@pytest.mark.skipif(sys.platform != "linux", reason="needs Linux PTYs")
async def test_client_escape_disconnects_and_keeps_what_follows_it_off_the_node(demo):
    res, out = await drive(await open_ticket(demo), b"echo before$((2*21))\n",
                           wait_for=b"before42", then=b"\x1d.echo after-never\n")
    assert res["reason"] == "disconnected (Ctrl-] .)"
    assert b"before42" in out and b"after-never" not in out


async def test_client_reports_a_refused_ticket_plainly(demo):
    bad = termclient.ws_url(demo.url, "/api/v1/remote/terminal/ws", "not-a-ticket")
    with pytest.raises(termclient.TerminalClientError, match="refused the terminal"):
        await drive(bad, b"")


async def test_client_surfaces_an_error_frame_from_the_node(tmp_path):
    from twinspark.remote.policy import write_policy
    async with DemoCluster(remote={"terminal": True}) as d:
        url = await open_ticket(d)
        write_policy(d.b_layout.etc / "remote-policy.json", {"terminal": False}, chown_root=False)
        with pytest.raises(termclient.TerminalClientError, match="sudo tsm remote enable terminal"):
            await drive(url, b"")


# ---- `tsm remote …` through a live controller ------------------------------------------------
async def test_remote_status_reach_and_logs_commands_against_a_live_controller(demo, capsys):
    base = ["--api", demo.url, "--key", demo.key]
    assert await asyncio.to_thread(run_cli, *base, "remote", "status") == 0
    out = capsys.readouterr().out
    assert "node A (controller)" in out and "node B" in out and "terminal" in out
    assert await asyncio.to_thread(run_cli, *base, "remote", "reach", "B") == 0
    assert "[ok]" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="error 422"):
        await asyncio.to_thread(run_cli, *base, "remote", "logs", "B", "--source", "shadow")
    with pytest.raises(SystemExit, match="--since"):
        await asyncio.to_thread(run_cli, *base, "remote", "logs", "B", "--since", "yesterday")


async def test_remote_power_commands_demand_the_typed_phrase_or_yes(demo, capsys, monkeypatch):
    base = ["--api", demo.url, "--key", demo.key]
    monkeypatch.setattr(cli_remote.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="--yes"):
        await asyncio.to_thread(run_cli, *base, "remote", "reboot", "B")
    with pytest.raises(SystemExit, match="--yes"):
        await asyncio.to_thread(run_cli, *base, "remote", "plug", "B", "off")
    typed = iter(["reboot b"])
    monkeypatch.setattr(cli_remote.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))
    with pytest.raises(SystemExit, match="cancelled"):
        await asyncio.to_thread(run_cli, *base, "remote", "reboot", "B")
    # confirmed, the (switched-off) node refuses with the command that fixes it
    with pytest.raises(SystemExit, match="error 409"):
        await asyncio.to_thread(run_cli, *base, "remote", "reboot", "B", "--yes")


async def test_remote_bundle_command_downloads_a_file(demo, tmp_path):
    out = tmp_path / "b.tar.gz"
    base = ["--api", demo.url, "--key", demo.key]
    await asyncio.to_thread(run_cli, *base, "remote", "bundle", "B", "-o", out)
    with tarfile.open(out) as t:
        assert any(m.name.endswith("tsm/vault-slots.txt") for m in t.getmembers())
    assert io.BytesIO(out.read_bytes()).read(2) == b"\x1f\x8b"
    assert base64 and time  # keep imports honest for linters
