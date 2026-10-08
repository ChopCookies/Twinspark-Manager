"""Node-side remote management actions: status, logs and the support bundle (no hardware, no root)."""

from __future__ import annotations

import base64
import io
import json
import socket
import tarfile

import pytest

from twinspark.agent.actions import AgentActions
from twinspark.agent.runtime import DryRunRuntime
from twinspark.remote import privops
from twinspark.schemas.config import AgentConfig, NodeIdentity, RemoteMgmtSettings
from twinspark.security import SecretsVault

AGENT_TOKEN = "agent-token-" + "x7Qp2LmN9vB4"
HF_TOKEN = "hf_" + "AbCdEfGhIjKlMnOpQrStUvWx"
PLUG_TOKEN = "plug-" + "s3cr3tValueZ99"


class FakePriv:
    """Stands in for the root helper: records ops and answers from a table."""

    def __init__(self, available=True, answers=None):
        self._available = available
        self.answers = answers or {}
        self.calls: list[tuple[str, dict]] = []

    def available(self):
        return self._available

    def call(self, op, params=None):
        self.calls.append((op, params or {}))
        ans = self.answers.get(op, {})
        if isinstance(ans, Exception):
            raise ans
        return ans


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def policy_path(tmp_path):
    p = tmp_path / "remote-policy.json"
    p.write_text(json.dumps({"terminal": True, "reboot": True}))
    return p


@pytest.fixture
def actions(tmp_path, policy_path, runtime_settings):
    cfg = AgentConfig(node=NodeIdentity(node_id="B", role="agent"), runtime=runtime_settings,
                      secrets_dir=str(tmp_path / "secrets"),
                      remote_mgmt=RemoteMgmtSettings(policy_path=str(policy_path), require_root_owned=False,
                                                     record_dir=str(tmp_path / "rec"),
                                                     terminal_port=free_port()))
    vault = SecretsVault(cfg.secrets_dir)
    vault.set("agent_token", AGENT_TOKEN)
    vault.set("hf_token", HF_TOKEN)
    vault.set("plug_token", PLUG_TOKEN)
    return AgentActions(cfg, runtime=DryRunRuntime(), vault=vault, privd=FakePriv(available=False))


@pytest.fixture
def fake_run(monkeypatch):
    """privops.RUN is the single place every remote command goes through."""
    calls: list[list[str]] = []
    table: dict[str, tuple[int, str, str]] = {}

    def run(argv, timeout=10):
        calls.append(list(argv))
        return table.get(argv[0].rsplit("/", 1)[-1], (127, "", "not found"))

    monkeypatch.setattr(privops, "RUN", run)
    run.calls, run.table = calls, table
    return run


def test_status_reports_policy_services_and_never_needs_root(actions, fake_run):
    st = actions.registry["remote_status"]({})
    assert st["node"] == "B"
    assert set(st["enabled"]) == {"terminal", "reboot"}
    assert st["policy"]["terminal"] is True and st["policy"]["poweroff"] is False
    assert st["policy"]["error"] is None
    assert st["terminal_service"] is False            # nothing listens on the terminal port in this test
    assert st["privd"] is False
    assert st["power_pending"] == []
    assert set(st["tools"]) == {"efibootmgr", "ethtool", "systemd-run", "journalctl"}


def test_unavailable_host_metrics_are_reported_as_unknown(actions, fake_run, monkeypatch):
    from twinspark.agent import remote_actions

    def unavailable():
        raise OSError("not supported")

    monkeypatch.setattr(remote_actions.os, "getloadavg", unavailable, raising=False)
    monkeypatch.setattr(remote_actions, "_uptime_s", lambda: None)
    st = actions.registry["remote_status"]({})
    assert st["loadavg"] is None and st["uptime_s"] is None and st["booted_at"] is None


def test_privileged_client_without_unix_sockets_reports_unavailable(monkeypatch):
    from twinspark.agent.privd import PrivClient, PrivdUnavailable

    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    client = PrivClient("unused")
    assert not client.available()
    with pytest.raises(PrivdUnavailable, match="tsm-privd requires Unix sockets"):
        client.call("remote_power", {"action": "reboot"})


def test_status_with_no_policy_file_means_everything_is_off(actions, policy_path, fake_run):
    policy_path.unlink()
    st = actions.registry["remote_status"]({})
    assert st["enabled"] == [] and st["policy"]["present"] is False


def test_status_reports_an_unsafe_policy_as_off_with_the_reason(actions, policy_path, fake_run):
    policy_path.write_text("{not json")
    st = actions.registry["remote_status"]({})
    assert st["enabled"] == [] and st["policy"]["error"]


def test_status_sees_a_pending_power_timer(actions, fake_run):
    fake_run.table["systemctl"] = (0, "active\n", "")
    assert actions.registry["remote_status"]({})["power_pending"] == ["reboot", "poweroff"]


def test_status_asks_the_helper_about_wake_on_lan_when_it_is_there(actions, fake_run):
    actions.privd = FakePriv(answers={"remote_wol_status": {"available": True, "interfaces": {}}})
    st = actions.registry["remote_status"]({})
    assert st["privd"] is True and st["wol_available"] is True
    assert ("remote_wol_status", {}) in actions.privd.calls


def test_logs_are_cleaned_filtered_and_bounded(actions, fake_run):
    fake_run.table["journalctl"] = (0, "boot ok\n\x1b[31mERROR\x1b[0m nvme timeout\x07\nlink up\n", "")
    r = actions.registry["remote_logs"]({"source": "kernel", "lines": 50, "grep": "error"})
    assert r["lines"] == ["ERROR nvme timeout"]
    assert not any("\x1b" in line or "\x07" in line for line in r["lines"])
    for bad in ({"source": "shadow"}, {"source": "agent", "lines": 0}, {"source": "agent", "lines": 5000},
                {"source": "agent", "grep": "x" * 101}):
        with pytest.raises(ValueError):
            actions.registry["remote_logs"](bad)


def test_logs_never_put_user_text_on_the_command_line_unquoted(actions, fake_run):
    fake_run.table["journalctl"] = (0, "line\n", "")
    actions.registry["remote_logs"]({"source": "agent", "lines": 10, "grep": "'; rm -rf / #"})
    argv = fake_run.calls[0]
    assert argv[0].endswith("journalctl")
    assert not any("rm -rf" in a for a in argv), "the filter is applied in Python, never by a shell"


def test_logs_fall_back_to_the_helper_when_the_service_user_cannot_read_the_journal(actions, fake_run):
    fake_run.table["journalctl"] = (0, "Hint: You are currently not seeing messages from other users and the "
                                       "system.\n-- No entries --\n", "")
    actions.privd = FakePriv(answers={"remote_journal": {"text": "from the helper\n"}})
    r = actions.registry["remote_logs"]({"source": "agent", "lines": 20})
    assert r["lines"] == ["from the helper"]
    assert actions.privd.calls and actions.privd.calls[0][0] == "remote_journal"


def untar(b64: str) -> dict[str, str]:
    raw = base64.b64decode(b64)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        return {m.name.split("/", 1)[1]: tar.extractfile(m).read().decode()
                for m in tar.getmembers() if m.isfile()}


def test_bundle_has_the_diagnostics_and_not_one_secret(actions, fake_run):
    fake_run.table["journalctl"] = (
        0, f"agent started token={AGENT_TOKEN}\nAuthorization: Bearer abcdefghijkl123456\n"
           f"hf login with {HF_TOKEN}\npassword=hunter2hunter2\nplug call {PLUG_TOKEN}\n", "")
    fake_run.table["uname"] = (0, "Linux spark-b 6.11 aarch64\n", "")
    out = actions.registry["remote_bundle"]({})
    files = untar(out["b64"])
    assert out["name"].endswith(".tar.gz") and "B" in out["name"]
    assert "Linux spark-b 6.11 aarch64" in files["system/uname.txt"]
    assert files["logs/agent.log"].count("[REDACTED]") >= 4
    # nothing secret anywhere in the archive, in any file, in any form we planted
    blob = "\n".join(files.values())
    for secret in (AGENT_TOKEN, HF_TOKEN, PLUG_TOKEN, "hunter2hunter2", "abcdefghijkl123456"):
        assert secret not in blob, f"{secret!r} leaked into the support bundle"
    # the vault is listed by slot name, never by content
    slots = files["tsm/vault-slots.txt"].split()
    assert {"agent_token", "hf_token", "plug_token"} <= set(slots)
    assert "effective-config" in " ".join(files) and "version.txt" in " ".join(files)
    assert files["_errors.txt"].strip()                 # missing tools are listed, not fatal
    assert out["problems"]                              # the same list is returned to the controller


def test_bundle_is_bounded(actions, fake_run):
    fake_run.table["journalctl"] = (0, ("x" * 200 + "\n") * 5000, "")
    files = untar(actions.registry["remote_bundle"]({})["b64"])
    assert all(len(v) <= 300 * 1024 for v in files.values())


def test_bundle_uses_the_helper_for_boot_and_wol_details_when_present(actions, fake_run):
    actions.privd = FakePriv(answers={"remote_boot_status": {"entries": [{"num": "0001", "label": "ubuntu"}]},
                                      "remote_wol_status": {"available": True},
                                      "remote_journal": {"text": "j\n"}})
    files = untar(actions.registry["remote_bundle"]({})["b64"])
    assert "ubuntu" in files["boot/efi-entries.json"]
    assert "network/wol.json" in files


async def test_actions_travel_through_the_real_agent_api(cluster, controller_config):
    """The allowlist, the agent route and the client agree on the new action names."""
    from twinspark.controller.agent_client import ALLOWED_AGENT_ACTIONS
    from twinspark.remote.privops import REMOTE_PRIV_OPS
    agent = cluster.controller.agents["B"]
    st = await agent.call("remote_status")
    assert st["node"] == "B" and st["enabled"] == []        # default: nothing on, no policy file
    for action in ("remote_status", "remote_logs", "remote_bundle", "remote_power", "remote_power_cancel",
                   "remote_boot_status", "remote_boot_next", "remote_boot_next_clear", "remote_wol_status",
                   "remote_wol_set"):
        assert action in ALLOWED_AGENT_ACTIONS
    assert {"remote_" + n for n in ("journal", "boot_status", "boot_next", "boot_next_clear", "power",
                                    "power_cancel", "wol_status", "wol_set")} == set(REMOTE_PRIV_OPS)
    cluster.controller.agents["B"].node  # noqa: B018 - fixture sanity


async def test_power_without_the_helper_is_a_clean_error_not_a_crash(cluster):
    from twinspark.controller.agent_client import AgentActionError
    with pytest.raises(AgentActionError) as err:
        await cluster.controller.agents["B"].call("remote_power", action="reboot", delay_s=5)
    assert "tsm-privd" in err.value.detail or "privileged" in err.value.detail


def test_a_shell_outside_the_privd_group_is_told_to_log_in_again(tmp_path, monkeypatch):
    import errno
    import os
    import socket as socket_mod

    from twinspark.agent import privd

    sock = tmp_path / "privd.sock"
    sock.write_text("")

    class Refused:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect(self, path):
            raise PermissionError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(socket_mod, "socket", Refused)
    monkeypatch.setattr(os, "getgroups", lambda: [])
    with pytest.raises(privd.PrivdUnavailable, match=r"not in it yet.*log out and back in.*newgrp"):
        privd.PrivClient(str(sock)).call("status")
    monkeypatch.setattr(os, "getgroups", lambda: [os.stat(sock).st_gid])
    with pytest.raises(privd.PrivdUnavailable, match="check the socket's mode"):
        privd.PrivClient(str(sock)).call("status")
