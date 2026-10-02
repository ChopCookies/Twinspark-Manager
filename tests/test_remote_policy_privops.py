"""Remote-management switch board and the privileged operations behind it. No hardware needed:
commands are replaced by canned output, /sys and /proc by scratch directories."""

from __future__ import annotations

import json
import os
import time

import pytest

from twinspark.agent.privd import PRIV_OPS
from twinspark.remote import policy as pol
from twinspark.remote import privops

# ---- policy -----------------------------------------------------------------------------------


def test_everything_is_off_without_a_policy_file(tmp_path):
    p = pol.load_policy(tmp_path / "missing.json", require_root=False)
    assert p.enabled == [] and p.error is None and p.present is False


def test_only_json_true_enables_a_feature(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"terminal": True, "reboot": "yes", "poweroff": 1, "boot_next": "true", "wol": None}))
    p = pol.load_policy(f, require_root=False)
    assert p.enabled == ["terminal"]


@pytest.mark.parametrize("content", ["{not json", "[]", '"terminal"', ""])
def test_unreadable_policy_fails_closed_and_says_why(tmp_path, content):
    f = tmp_path / "p.json"
    f.write_text(content)
    p = pol.load_policy(f, require_root=False)
    assert p.enabled == [] and p.error and p.present


def test_symlinked_policy_is_ignored(tmp_path, require_symlinks):
    real = tmp_path / "real.json"
    real.write_text('{"terminal": true}')
    link = tmp_path / "link.json"
    link.symlink_to(real)
    p = pol.load_policy(link, require_root=False)
    assert p.enabled == [] and "symlink" in p.error


@pytest.mark.skipif(not hasattr(os, "geteuid") or getattr(os, "geteuid", lambda: 0)() == 0,
                    reason="requires POSIX file ownership and a non-root test user")
def test_policy_not_owned_by_root_is_ignored_when_root_is_required(tmp_path):
    f = tmp_path / "p.json"
    f.write_text('{"terminal": true}')
    p = pol.load_policy(f, require_root=True)
    assert p.enabled == [] and "owned by root" in p.error


def test_limits_are_clamped_and_junk_limits_fall_back(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"terminal_idle_s": 1, "terminal_max_s": 10 ** 9, "terminal_max_sessions": "many"}))
    p = pol.load_policy(f, require_root=False)
    assert (p.terminal_idle_s, p.terminal_max_s, p.terminal_max_sessions) == (60, 86400, 2)


def test_write_policy_merges_atomically_and_keeps_limits(tmp_path):
    f = tmp_path / "etc" / "p.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"terminal": True, "terminal_idle_s": 300, "future_key": 1}))
    p = pol.write_policy(f, {"reboot": True, "terminal": False}, chown_root=False)
    assert p.enabled == ["reboot"] and p.terminal_idle_s == 300
    data = json.loads(f.read_text())
    assert data["future_key"] == 1 and data["poweroff"] is False
    if os.name == "posix":
        assert (f.stat().st_mode & 0o777) == 0o644
    assert not [x for x in f.parent.iterdir() if x.name.startswith(".remote-policy")]


def test_write_policy_rejects_unknown_features(tmp_path):
    with pytest.raises(ValueError, match="unknown remote feature"):
        pol.write_policy(tmp_path / "p.json", {"shell_everything": True}, chown_root=False)


# ---- privd registration -----------------------------------------------------------------------


def test_remote_ops_are_registered_and_nothing_generic_is():
    names = {k for k in PRIV_OPS if k.startswith("remote_")}
    assert names == set(privops.REMOTE_PRIV_OPS)
    assert not any(k in PRIV_OPS for k in ("run", "exec", "shell", "command", "reboot", "poweroff"))


@pytest.fixture
def priv(tmp_path, monkeypatch):
    """privops with a scratch policy and a recording fake for every command."""
    policy_file = tmp_path / "policy.json"
    monkeypatch.setattr(pol, "PRIVD_POLICY", pol.PolicySource(path=policy_file, require_root=False))
    monkeypatch.setattr(privops, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    calls: list[list[str]] = []
    answers: dict[str, tuple[int, str, str]] = {}

    def fake(argv, timeout=15):
        calls.append(argv)
        key = os.path.basename(argv[0]) + " " + " ".join(argv[1:])
        for k, v in answers.items():
            if key.startswith(k):
                return v
        return 0, "", ""

    monkeypatch.setattr(privops, "RUN", fake)
    monkeypatch.setattr(privops, "_maintenance_running", lambda: False)

    def enable(**features):
        policy_file.write_text(json.dumps(features))

    return type("Priv", (), {"calls": calls, "answers": answers, "enable": staticmethod(enable),
                             "tmp": tmp_path})


@pytest.mark.parametrize("op,params", [
    ("remote_power", {"action": "reboot", "delay_s": 5}),
    ("remote_power", {"action": "poweroff", "delay_s": 5}),
    ("remote_boot_next", {"target": "network"}),
    ("remote_boot_next_clear", {}),
    ("remote_wol_set", {"iface": "eth0", "mode": "g"}),
])
def test_changing_ops_refuse_without_policy_and_run_no_command(priv, op, params):
    with pytest.raises(RuntimeError, match="not enabled on this node"):
        PRIV_OPS[op](params)
    assert priv.calls == []


def test_refusal_names_the_command_that_enables_it(priv):
    with pytest.raises(RuntimeError, match="sudo tsm remote enable boot-next"):
        PRIV_OPS["remote_boot_next"]({"target": "network"})


def test_policy_for_reboot_does_not_allow_poweroff(priv):
    priv.enable(reboot=True)
    assert PRIV_OPS["remote_power"]({"action": "reboot", "delay_s": 5})["scheduled"] == "reboot"
    with pytest.raises(RuntimeError, match="'poweroff' is not enabled"):
        PRIV_OPS["remote_power"]({"action": "poweroff", "delay_s": 5})


def test_reboot_is_scheduled_through_a_transient_timer_with_fixed_argv(priv):
    priv.enable(reboot=True)
    priv.answers["systemctl is-active"] = (3, "inactive\n", "")
    out = PRIV_OPS["remote_power"]({"action": "reboot", "delay_s": 7})
    assert out == {"scheduled": "reboot", "in_s": 7, "already_pending": False}
    run = [c for c in priv.calls if "systemd-run" in c[0]][0]
    assert run[1:3] == ["--unit=twinspark-remote-reboot", "--on-active=7s"]
    assert run[-2:] == [privops._which("systemctl"), "reboot"]


def test_a_second_reboot_request_does_not_stack(priv):
    priv.enable(reboot=True)
    priv.answers["systemctl is-active"] = (0, "active\n", "")
    out = PRIV_OPS["remote_power"]({"action": "reboot", "delay_s": 5})
    assert out["already_pending"] is True
    assert not [c for c in priv.calls if "systemd-run" in c[0]]


@pytest.mark.parametrize("delay", [0, 1, 601, -5, "5", None, True, 2.5])
def test_power_delay_is_validated(priv, delay):
    priv.enable(reboot=True)
    with pytest.raises(ValueError):
        PRIV_OPS["remote_power"]({"action": "reboot", "delay_s": delay})
    assert priv.calls == []


def test_power_refuses_while_a_package_manager_runs(priv):
    priv.enable(reboot=True)
    d = priv.tmp / "proc" / "4242"
    d.mkdir()
    (d / "comm").write_text("dpkg\n")
    with pytest.raises(RuntimeError, match="dpkg is running"):
        PRIV_OPS["remote_power"]({"action": "reboot", "delay_s": 5})


def test_power_refuses_during_a_maintenance_run(priv, monkeypatch):
    priv.enable(reboot=True)
    monkeypatch.setattr(privops, "_maintenance_running", lambda: True)
    with pytest.raises(RuntimeError, match="maintenance run is in progress"):
        PRIV_OPS["remote_power"]({"action": "reboot", "delay_s": 5})


def test_cancel_only_stops_timers_that_exist_and_needs_no_policy(priv):
    priv.answers["systemctl is-active twinspark-remote-poweroff.timer"] = (0, "active\n", "")
    priv.answers["systemctl is-active twinspark-remote-reboot.timer"] = (3, "inactive\n", "")
    assert PRIV_OPS["remote_power_cancel"]({}) == {"cancelled": ["poweroff"]}
    assert any(c[1:3] == ["stop", "twinspark-remote-poweroff.timer"] for c in priv.calls)


EFI = """BootCurrent: 0001
Timeout: 1 seconds
BootOrder: 0001,0000,0002
Boot0000* UEFI: PXE IPv4 Realtek PCIe 10GBE Family Controller
Boot0001* ubuntu
Boot0002* UEFI: HTTP IPv4 Realtek PCIe 10GBE Family Controller
Boot0003  UEFI: USB Flash Disk
"""


def test_efibootmgr_output_is_parsed_and_classified():
    info = privops.parse_efibootmgr(EFI + "BootNext: 0000\n")
    assert info["current"] == "0001" and info["next"] == "0000" and info["order"] == ["0001", "0000", "0002"]
    kinds = {e["num"]: (e["kind"], e["active"]) for e in info["entries"]}
    assert kinds == {"0000": ("network", True), "0001": ("disk", True), "0002": ("network", True),
                     "0003": ("usb", False)}


def test_boot_next_network_picks_the_single_pxe_ipv4_entry_and_verifies(priv):
    priv.enable(boot_next=True)
    state = {"next": None}

    def fake(argv, timeout=15):
        priv.calls.append(argv)
        if argv[0].endswith("efibootmgr") and "--bootnext" in argv:
            state["next"] = argv[-1]
            return 0, "", ""
        if argv[0].endswith("efibootmgr"):
            return 0, EFI + (f"BootNext: {state['next']}\n" if state["next"] else ""), ""
        return 0, "", ""

    privops.RUN = fake
    out = PRIV_OPS["remote_boot_next"]({"target": "network"})
    assert out["next"] == "0000" and out["kind"] == "network"
    assert any(c[-2:] == ["--bootnext", "0000"] for c in priv.calls)


def test_boot_next_refuses_when_the_firmware_ignores_it(priv):
    priv.enable(boot_next=True)
    priv.answers["efibootmgr"] = (0, EFI, "")          # BootNext never appears
    with pytest.raises(RuntimeError, match="did not change"):
        PRIV_OPS["remote_boot_next"]({"target": "0000"})


def test_boot_next_with_several_network_cards_asks_for_a_number(priv):
    priv.enable(boot_next=True)
    two = EFI + "Boot0004* UEFI: PXE IPv4 Mellanox ConnectX-7\n"
    priv.answers["efibootmgr"] = (0, two, "")
    with pytest.raises(RuntimeError, match="pick one by number: 0000 .*; 0004"):
        PRIV_OPS["remote_boot_next"]({"target": "network"})


@pytest.mark.parametrize("target", ["", "0x03", "000", "network; reboot", "../../x", "00000", "zzzz"])
def test_boot_next_rejects_odd_targets_before_running_anything_dangerous(priv, target):
    priv.enable(boot_next=True)
    priv.answers["efibootmgr"] = (0, EFI, "")
    with pytest.raises((ValueError, RuntimeError)):
        PRIV_OPS["remote_boot_next"]({"target": target})
    assert not [c for c in priv.calls if "--bootnext" in c]


def test_boot_next_unknown_entry_number_is_refused(priv):
    priv.enable(boot_next=True)
    priv.answers["efibootmgr"] = (0, EFI, "")
    with pytest.raises(RuntimeError, match="no boot entry 00FF"):
        PRIV_OPS["remote_boot_next"]({"target": "00ff"})


def test_boot_status_without_efibootmgr_explains_how_to_install_it(priv):
    priv.answers["efibootmgr"] = (127, "", "efibootmgr: command not found")
    out = PRIV_OPS["remote_boot_status"]({})
    assert out["available"] is False and "apt install efibootmgr" in out["reason"]


ETHTOOL = """Settings for enP7s7:
\tSupported ports: [ TP ]
\tSupports Wake-on: pumbg
\tWake-on: d
\tLink detected: yes
"""


@pytest.fixture
def nics(priv, monkeypatch):
    net = priv.tmp / "net"
    for name in ("enP7s7", "docker0", "lo"):
        (net / name).mkdir(parents=True)
    (net / "enP7s7" / "device").mkdir()
    (net / "enP7s7" / "address").write_text("3c:6d:66:01:02:03\n")
    monkeypatch.setattr(privops, "SYS_NET", net)
    return net


def test_wol_status_lists_only_physical_ports_with_their_mac(priv, nics):
    priv.answers["ethtool enP7s7"] = (0, ETHTOOL, "")
    out = PRIV_OPS["remote_wol_status"]({})
    assert list(out["interfaces"]) == ["enP7s7"]
    assert out["interfaces"]["enP7s7"] == {"supports": "pumbg", "mode": "d", "mac": "3c:6d:66:01:02:03"}


def test_wol_set_checks_policy_interface_and_result(priv, nics):
    with pytest.raises(RuntimeError, match="not enabled"):
        PRIV_OPS["remote_wol_set"]({"iface": "enP7s7", "mode": "g"})
    priv.enable(wol=True)
    for bad in ("docker0", "lo", "enP7s7; rm", "../enP7s7", ""):
        with pytest.raises(ValueError, match="physical network interface"):
            PRIV_OPS["remote_wol_set"]({"iface": bad, "mode": "g"})
    with pytest.raises(ValueError, match="mode"):
        PRIV_OPS["remote_wol_set"]({"iface": "enP7s7", "mode": "x"})
    priv.answers["ethtool enP7s7"] = (0, ETHTOOL.replace("Wake-on: d", "Wake-on: g"), "")
    out = PRIV_OPS["remote_wol_set"]({"iface": "enP7s7", "mode": "g"})
    assert out["mode"] == "g" and "netplan" in out["persist"]
    assert any(c[-4:] == ["-s", "enP7s7", "wol", "g"] or c[1:] == ["-s", "enP7s7", "wol", "g"] for c in priv.calls)


def test_wol_set_reports_when_the_driver_ignores_the_setting(priv, nics):
    priv.enable(wol=True)
    priv.answers["ethtool enP7s7"] = (0, ETHTOOL, "")      # still "d" afterwards
    with pytest.raises(RuntimeError, match="did not take the setting"):
        PRIV_OPS["remote_wol_set"]({"iface": "enP7s7", "mode": "g"})


@pytest.mark.parametrize("source", ["kernel", "previous-boot", "agent", "docker"])
def test_journal_reads_are_bounded_and_allowlisted(priv, source):
    priv.answers["journalctl"] = (0, "line one\nline two\n", "")
    out = PRIV_OPS["remote_journal"]({"source": source, "lines": 99999, "since_s": 60})
    argv = priv.calls[-1]
    assert "--lines=2000" in argv and "--since=-60s" in argv and out["text"].startswith("line one")


@pytest.mark.parametrize("source", ["", "shadow", "../../var/log/auth.log", "agent; id", "twinspark-agent"])
def test_journal_rejects_sources_outside_the_allowlist(priv, source):
    with pytest.raises(ValueError, match="unknown log source"):
        PRIV_OPS["remote_journal"]({"source": source})
    assert priv.calls == []


@pytest.mark.parametrize("params", [{"lines": 0}, {"lines": -1}, {"lines": "10"}, {"lines": True}, {"since_s": 0},
                                    {"since_s": "5"}, {"since_s": 10 ** 9}])
def test_journal_rejects_bad_numbers(priv, params):
    with pytest.raises(ValueError):
        PRIV_OPS["remote_journal"]({"source": "agent", **params})


def test_kernel_log_falls_back_to_dmesg_without_a_journal(priv):
    priv.answers["journalctl"] = (1, "", "No journal files were found.")
    priv.answers["dmesg"] = (0, "a\nb\nc\n", "")
    out = PRIV_OPS["remote_journal"]({"source": "kernel", "lines": 2})
    assert out["text"] == "b\nc"


def test_time_budget_of_every_command_is_bounded(priv):
    priv.enable(reboot=True)
    seen = []
    orig = privops.RUN

    def watching(argv, timeout=15):
        seen.append(timeout)
        return orig(argv, timeout)

    privops.RUN = watching
    t0 = time.time()
    PRIV_OPS["remote_power"]({"action": "reboot", "delay_s": 5})
    assert seen and max(seen) <= 30 and time.time() - t0 < 5
