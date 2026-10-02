"""Installer regression checks using Bash with mocked privilege/service operations."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"


def _bash() -> str:
    candidates = [shutil.which("bash"), shutil.which("sh")]
    git = shutil.which("git")
    if git and os.name == "nt":
        candidates.append(str(Path(git).resolve().parent.parent / "usr" / "bin" / "sh.exe"))
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and "GNU bash" in result.stdout:
            return candidate
    pytest.skip("installer regression tests require Bash")


def _shell_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt":
        return "/" + value[0].lower() + value[2:]
    return value


@pytest.mark.parametrize("args", [
    ["--join", "join code with spaces;$(unused)", "--yes"],
    ["--no-setup"],
    ["--uninstall", "--purge"],
])
def test_installer_sudo_keeps_original_arguments(tmp_path, args):
    shell = _bash()
    capture = tmp_path / "sudo-args.bin"
    fake_sudo = tmp_path / "sudo"
    fake_sudo.write_text(
        '#!/bin/sh\nprintf \'%s\\0\' "$@" > "$TSM_SUDO_CAPTURE"\n', encoding="utf-8"
    )
    fake_sudo.chmod(0o700)
    driver = tmp_path / "driver.sh"
    driver.write_text(
        'uname() { printf "Linux\\n"; }\n'
        'id() { printf "1000\\n"; }\n'
        'export PATH="$TSM_TEST_BIN:$PATH"\n'
        'source "$TSM_INSTALL_SOURCE" "$@"\n',
        encoding="utf-8",
    )
    env = {**os.environ, "TSM_TEST_BIN": _shell_path(tmp_path),
           "TSM_SUDO_CAPTURE": _shell_path(capture), "TSM_INSTALL_SOURCE": _shell_path(INSTALL),
           "TSM_HOME": "/tmp/twinspark-installer-test", "TSM_NO_SUDO": ""}
    # Fake sudo records argv and exits. No installer prerequisite or privilege operation runs.
    result = subprocess.run([shell, _shell_path(driver), *args], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    forwarded = capture.read_bytes().decode("utf-8").split("\0")[:-1]
    assert forwarded == ["-E", "bash", _shell_path(driver), *args]


@pytest.mark.parametrize("purge", [False, True])
def test_uninstall_stops_and_removes_terminal_before_other_services(purge):
    source = INSTALL.read_text(encoding="utf-8")
    function = re.search(r"(?ms)^uninstall\(\) \{\n.*?^\}\n(?=if \[ \"\$UNINSTALL\")", source)
    assert function, "could not locate the installer's uninstall function"
    mocks = r'''
set +o posix
set -euo pipefail
say() { :; }
warn() { :; }
systemctl() { printf 'SERVICE %s\n' "$*"; }
rm() { printf 'REMOVE %s\n' "$*"; }
[() {
  if [[ "$1" == "-d" && "$2" == "/run/systemd/system" ]]; then return 0; fi
  builtin [ "$@"
}
TSM_HOME=/tmp/twinspark-installer-test
TSM_BIN_DIR=/tmp/twinspark-installer-bin
'''
    # Run the actual function, replacing systemctl and every deletion with trace-only functions.
    script = mocks + f"PURGE={int(purge)}\n" + function.group() + "\nuninstall\n"
    result = subprocess.run([_bash(), "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    services = ["twinspark-terminal", "twinspark-controller", "twinspark-agent", "twinspark-privd"]
    stops = [f"SERVICE disable --now {service}" for service in services]
    assert [line for line in lines if line.startswith("SERVICE disable")] == stops
    for service in services:
        assert f"REMOVE -f /etc/systemd/system/{service}.service" in lines
    assert "SERVICE daemon-reload" in lines
    assert "REMOVE -rf /tmp/twinspark-installer-test" in lines
    purge_trace = "REMOVE -rf /etc/twinspark /var/lib/twinspark /var/cache/twinspark"
    assert (purge_trace in lines) is purge
