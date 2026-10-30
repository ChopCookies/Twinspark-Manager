"""The program is readable by the service user whatever the operator's umask, and setup checks that it is.

Found on a real Spark: a maintenance shell with ``umask 077`` ran the install, pip created files only root could
read, root's import check passed, and the agent then failed with ``ModuleNotFoundError: twinspark.cli``.
The CI job "install on systemd" runs install.sh under umask 077 and imports as the service user for real.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from twinspark import provision
from twinspark.agent import update_worker

from .test_selfupdate import FakeHost, make_worker, worker_run

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"
POSIX = os.name == "posix"


def test_install_sh_fixes_the_umask_before_anything_is_installed():
    text = INSTALL.read_text(encoding="utf-8")
    umask = text.index("\numask 022\n")
    assert umask < text.index("pip install")                       # before pip creates a single file
    assert re.search(r'chmod -R u\+rwX,go\+rX,go-w "\$TSM_HOME"', text)    # readable by all, writable by root


class PipWithUmask(FakeHost):
    """pip as it behaves under umask 077, and runuser for the service user's import."""

    def __init__(self, *a, service_user_can_import=True, **k):
        super().__init__(*a, **k)
        self.service_user_can_import = service_user_can_import

    def run(self, argv, timeout=120):
        if argv[0] == "runuser":
            self.calls.append(list(argv))
            if not self.service_user_can_import:
                raise RuntimeError("runuser -u spark failed (1): ModuleNotFoundError: No module named 'twinspark.cli'")
            return ""
        out = super().run(argv, timeout)
        if argv[1:4] == ["-m", "pip", "install"]:
            pkg = self.venv / "lib" / "site-packages" / "twinspark"
            pkg.mkdir(parents=True, exist_ok=True)
            os.chmod(pkg, 0o700)
            (pkg / "cli.py").write_text("x = 1\n")
            os.chmod(pkg / "cli.py", 0o600)
            (pkg / "tool").write_text("#!/bin/sh\n")
            os.chmod(pkg / "tool", 0o700)
        return out


@pytest.mark.skipif(not POSIX, reason="permission bits need a POSIX filesystem")
def test_the_update_leaves_the_program_readable_and_imports_it_as_the_service_user(tmp_path):
    venv, run_dir = worker_run(tmp_path)
    state = json.loads((run_dir / "state.json").read_text())
    state["service_user"] = "spark"
    update_worker.save(str(run_dir), state)
    host = PipWithUmask(venv)
    old = os.umask(0o077)
    try:
        out = make_worker(run_dir, host).main()
    finally:
        os.umask(old)
    assert out["state"] == "completed", out
    pkg = venv / "lib" / "site-packages" / "twinspark"
    assert pkg.stat().st_mode & 0o777 == 0o755
    assert (pkg / "cli.py").stat().st_mode & 0o777 == 0o644
    assert (pkg / "tool").stat().st_mode & 0o777 == 0o755           # executables stay executable
    runuser = [c for c in host.calls if c[0] == "runuser"]
    assert runuser and runuser[0][:4] == ["runuser", "-u", "spark", "--"]


def test_an_update_the_service_user_cannot_import_is_rolled_back(tmp_path):
    venv, run_dir = worker_run(tmp_path)
    state = json.loads((run_dir / "state.json").read_text())
    state["service_user"] = "spark"
    update_worker.save(str(run_dir), state)
    out = make_worker(run_dir, PipWithUmask(venv, service_user_can_import=False)).main()
    assert out["state"] == "rolled_back" and "ModuleNotFoundError" in out["error"]
    assert (venv / "lib" / "program.txt").read_text() == "version 0.8.0"


def test_privd_records_the_service_user_and_starts_the_worker_with_umask_022(tmp_path, monkeypatch):
    from twinspark.agent import selfupdate as root

    from .test_selfupdate import target
    venv = tmp_path / "opt" / "venv"
    (venv / "bin").mkdir(parents=True)
    monkeypatch.setattr(root, "ROOT", tmp_path / "runs")
    monkeypatch.setattr(root, "policy", lambda: {"enabled": True, "allow_prerelease": False})
    monkeypatch.setattr(root, "install_venv", lambda: venv)
    monkeypatch.setattr(root, "base_python", lambda: "/usr/bin/python3.12")
    monkeypatch.setattr(root, "running_units", lambda: ["twinspark-agent.service"])
    calls = []

    class Done:
        returncode, stderr = 0, ""

        def __init__(self, stdout=""):
            self.stdout = stdout

    def fake_run(argv, **kw):
        calls.append(argv)
        return Done("spark\n" if argv[1:3] == ["show", "-p"] else "")

    monkeypatch.setattr(root.subprocess, "run", fake_run)
    state = root.start({"run_id": "e" * 32, "target": target()})
    assert state["service_user"] == "spark"
    assert "--property=UMask=0022" in calls[-1]


class Recorder:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        for prefix, answer in self.answers:
            if argv[:len(prefix)] == prefix:
                return answer(argv) if callable(answer) else answer
        return 0, ""


def provisioner(tmp_path, run) -> provision.Provisioner:
    a = provision.Answers(role="controller", node_id="A", hostname="spark-a", qsfp_iface="enp1s0f1np1",
                          qsfp_ip="192.168.100.1", peer_ip="192.168.100.2", service_user="spark")
    p = provision.Provisioner(a, provision.Layout(tmp_path), run=run)
    p.systemd = True
    return p


def test_setup_refuses_to_start_services_that_cannot_read_their_program(tmp_path, monkeypatch):
    run = Recorder([(["runuser"], (1, "ModuleNotFoundError: No module named 'twinspark.cli'"))])
    p = provisioner(tmp_path, run)
    python = Path(p.lay.tsm_bin).parent / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    monkeypatch.setattr(provision.shutil, "which", lambda name: f"/usr/sbin/{name}")
    p.start(["twinspark-agent.service"])
    step = p.steps[-1]
    assert step.status == "failed" and "cannot load the program" in step.detail and "chmod -R" in step.detail
    assert not any(c[:2] == ["systemctl", "enable"] for c in run.calls)


def test_a_unit_that_restarts_in_a_loop_fails_setup_with_its_journal(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(provision.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(provision.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    pid = [100]

    def show(argv):
        if argv[2] == "twinspark-gateway.service":
            pid[0] += 1
            return 0, f"ActiveState=activating\nSubState=auto-restart\nMainPID={pid[0]}\nType=simple\n"
        return 0, "ActiveState=active\nSubState=running\nMainPID=42\nType=simple\n"

    run = Recorder([(["systemctl", "show"], show),
                    (["journalctl"], (0, "Started gateway\nOSError: [Errno 30] Read-only file system: "
                                         "'/etc/twinspark/secrets'\nMain process exited\n"))])
    p = provisioner(tmp_path, run)
    p.start(["twinspark-agent.service", "twinspark-gateway.service"])
    step = p.steps[-1]
    assert step.status == "failed"
    assert "twinspark-gateway.service did not stay up" in step.detail and "Read-only file system" in step.detail
    assert "twinspark-agent.service" not in step.detail                 # the healthy one is not blamed


def test_units_that_stay_up_pass(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(provision.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(provision.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    run = Recorder([(["systemctl", "show"], (0, "ActiveState=active\nSubState=running\nMainPID=7\nType=simple\n"))])
    p = provisioner(tmp_path, run)
    p.start(["twinspark-agent.service"])
    assert p.steps[-1].status == "done"


# ---- rendered socket addresses -----------------------------------------------------------------------
@pytest.mark.parametrize("host,port,want", [("127.0.0.1", 9444, "127.0.0.1:9444"),
                                            ("192.168.100.2", "9444", "192.168.100.2:9444"),
                                            ("::1", 9444, "[::1]:9444")])
def test_listen_stream_addresses(host, port, want):
    assert provision.listen_stream(host, port) == want


@pytest.mark.parametrize("host,port", [(None, 9444), ("None", 9444), ("", 9444), ("spark-b", 9444),
                                       ("127.0.0.1", 0), ("127.0.0.1", 70000), ("127.0.0.1", "x")])
def test_a_missing_or_bad_address_is_refused_not_rendered(host, port):
    with pytest.raises(provision.SetupError):
        provision.listen_stream(host, port)


def test_node_b_without_a_qsfp_address_never_renders_none_9444(tmp_path):
    a = provision.Answers(role="agent", node_id="B", hostname="spark-b", qsfp_iface="enp1s0f1np1",
                          qsfp_ip=None, service_user="spark")
    with pytest.raises(provision.SetupError, match="QSFP address"):
        provision.render_terminal_socket(a, provision.Layout(tmp_path))
