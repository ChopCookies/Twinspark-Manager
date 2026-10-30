"""Install one TwinSpark release into the program environment, restart the services, roll back on failure.

tsm-privd starts this as root in its own transient unit (``twinspark-update-<run id>``):

    <system python3> -I -S /var/lib/twinspark-update/<run id>/worker.py /var/lib/twinspark-update/<run id>

It runs from a copy of this file, with the base interpreter, isolated from site-packages (-I -S). It
never imports from the environment it replaces, and it keeps running while the agent, privd and (on
node A) the controller restart. **Standard library only**; a test checks this.

Steps (each saved to ``state.json`` before it starts):

1. download — the wheel and ``constraints-release.txt`` from the release; each must have the published
   sha256 (privd checked the URLs; the controller took the hashes from the release's SHA256SUMS)
2. backup   — copy the environment to ``venv.rollback``
3. install  — ``pip install --upgrade -c constraints-release.txt <wheel>[hf]`` into the environment
4. verify   — the new environment imports and reports the target version
5. restart  — restart the TwinSpark services that were running, then check each stays up

A failure after step 2 puts ``venv.rollback`` back and restarts the services again ("rolled_back"). After a
success, the old environment is kept as ``venv.previous`` until the next update.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request

STATE = "state.json"
UNIT_SETTLE_S = 20          # a restarted service must stay up this long with the same PID
UNIT_TIMEOUT_S = 180        # …within this long after the restart
SYSTEMCTL = "/usr/bin/systemctl"
TERMINAL = ("completed", "failed", "rolled_back")


def load(run_dir: str) -> dict:
    with open(os.path.join(run_dir, STATE), encoding="utf-8") as f:
        return json.load(f)


def save(run_dir: str, state: dict) -> None:
    state["updated_at"] = time.time()
    path = os.path.join(run_dir, STATE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def fetch(url: str, timeout: float = 60, attempts: int = 3) -> bytes:
    last: Exception = RuntimeError("no attempt")
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"user-agent": "twinspark-update-worker"})
            with urllib.request.urlopen(req, timeout=timeout) as r:          # noqa: S310 - https URL built by privd
                return r.read(200 * 1024 * 1024)
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"download of {url} failed: {last}")


def run(argv: list[str], timeout: float = 120) -> str:
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8", "PIP_NO_INPUT": "1",
           "PIP_DISABLE_PIP_VERSION_CHECK": "1", "HOME": "/root"}
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        if k in os.environ:
            env[k] = os.environ[k]
    p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode != 0:
        raise RuntimeError(f"{os.path.basename(argv[0])} {' '.join(argv[1:3])} failed ({p.returncode}): "
                           f"{(p.stderr or p.stdout)[-1500:]}")
    return p.stdout


def readable(top: str) -> None:
    """``chmod -R u+rwX,go+rX,go-w``: the program is read by the service user, written only by root."""
    for root, dirs, files in os.walk(top):
        for name in dirs + files:
            path = os.path.join(root, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                continue
            mode = stat.S_IMODE(st.st_mode)
            want = (mode | 0o644 | (0o111 if stat.S_ISDIR(st.st_mode) or mode & 0o100 else 0)) & ~0o022
            if want != mode:
                os.chmod(path, want)


class Worker:
    def __init__(self, run_dir: str, *, fetch=fetch, run=run, sleep=time.sleep, now=time.monotonic):
        self.dir = run_dir
        self.fetch, self.run, self.sleep, self.now = fetch, run, sleep, now
        self.state = load(run_dir)
        self.restarted = False

    # ---- helpers -------------------------------------------------------------------------------
    def phase(self, phase: str, **extra) -> None:
        self.state.update(state="running", phase=phase, **extra)
        self.state.setdefault("phases", []).append([phase, time.time()])
        save(self.dir, self.state)

    @property
    def venv(self) -> str:
        return self.state["venv"]

    def py(self) -> str:
        return os.path.join(self.venv, "bin", "python")

    def unit_info(self, unit: str) -> dict:
        out = self.run([SYSTEMCTL, "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
                        "-p", "NRestarts"], timeout=30)
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def wait_up(self, unit: str) -> None:
        """Active and running with the same PID for UNIT_SETTLE_S — a crash loop never passes."""
        deadline = self.now() + UNIT_TIMEOUT_S
        stable_since, pid = None, None
        last: dict = {}
        while self.now() < deadline:
            last = self.unit_info(unit)
            up = last.get("ActiveState") == "active" and last.get("SubState") == "running"
            if up and last.get("MainPID") not in (None, "", "0") and last.get("MainPID") == pid:
                if stable_since is not None and self.now() - stable_since >= UNIT_SETTLE_S:
                    return
            elif up:
                pid, stable_since = last.get("MainPID"), self.now()
            else:
                pid, stable_since = None, None
            self.sleep(2)
        raise RuntimeError(f"{unit} did not stay up after the restart (last: {last.get('ActiveState')}/"
                           f"{last.get('SubState')}, restarts {last.get('NRestarts')}); "
                           f"see journalctl -u {unit}")

    def restart_all(self) -> None:
        for unit in self.state.get("units") or []:
            self.run([SYSTEMCTL, "restart", unit], timeout=180)
            self.wait_up(unit)

    # ---- the steps -----------------------------------------------------------------------------
    def download(self) -> dict[str, str]:
        paths = {}
        for key in ("wheel", "constraints"):
            f = self.state["target"][key]
            data = self.fetch(f["url"])
            got = hashlib.sha256(data).hexdigest()
            if got != f["sha256"]:
                raise RuntimeError(f"{f['name']}: downloaded sha256 {got} is not the published {f['sha256']}")
            path = os.path.join(self.dir, f["name"])
            with open(path, "wb") as fh:
                fh.write(data)
            paths[key] = path
        return paths

    def backup(self) -> str:
        rollback = self.venv + ".rollback"
        if os.path.lexists(rollback):
            shutil.rmtree(rollback)
        shutil.copytree(self.venv, rollback, symlinks=True)
        return rollback

    def install(self, paths: dict[str, str]) -> None:
        pip = [self.py(), "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
        self.run([*pip, "--upgrade", "-c", paths["constraints"], f"{paths['wheel']}[hf]"], timeout=1800)
        # the same version again (a repair) still replaces the program files
        self.run([*pip, "--force-reinstall", "--no-deps", paths["wheel"]], timeout=600)
        readable(self.venv)


    def verify(self) -> None:
        want = self.state["target"]["version"]
        got = self.run([self.py(), "-I", "-c", "import twinspark, twinspark.cli, twinspark.serve, "
                        "twinspark.agent.agent, twinspark.controller.app; print(twinspark.__version__)"],
                       timeout=120).strip().splitlines()[-1:]
        if got != [want]:
            raise RuntimeError(f"the new environment reports version {got[0] if got else '?'}, expected {want}")
        user = self.state.get("service_user")
        if user and user != "root":
            # the agent and the controller import it as their own user: root's import above proves less
            self.run(["runuser", "-u", user, "--", self.py(), "-I", "-c", "import twinspark.cli, twinspark.serve"],
                     timeout=120)

    def rollback(self, rollback: str, reason: str) -> None:
        failed = f"{self.venv}.failed-{self.state['run_id'][:12]}"
        self.phase("rolling back", error=reason[-2000:])
        if os.path.lexists(failed):
            shutil.rmtree(failed)
        os.replace(self.venv, failed)
        os.replace(rollback, self.venv)
        # Before the restart step the services still run the old program from memory, and the restored
        # files are the ones they loaded: restarting them is only needed once the new one was started.
        if self.restarted:
            self.restart_all()
        shutil.rmtree(failed, ignore_errors=True)

    def main(self) -> dict:
        if self.state.get("state") != "queued":
            raise RuntimeError("the worker only starts a newly queued run")
        os.umask(0o022)                    # pip's new files must be readable by the service user
        rollback = None
        try:
            self.phase("download")
            paths = self.download()
            self.phase("backup")
            rollback = self.backup()
            self.phase("install")
            self.install(paths)
            self.phase("verify")
            self.verify()
            with open(os.path.join(os.path.dirname(self.venv), "source-commit"), "w", encoding="utf-8") as f:
                f.write(f"release:{self.state['target']['tag']}\n")
            self.phase("restart")
            self.restarted = True
            self.restart_all()
            previous = self.venv + ".previous"
            if os.path.lexists(previous):
                shutil.rmtree(previous)
            os.replace(rollback, previous)
            self.state.update(state="completed", phase="done", previous=previous)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded; a backup is put back
            reason = str(exc)
            if rollback is None:
                self.state.update(state="failed", error=reason[-2000:])
            else:
                try:
                    self.rollback(rollback, reason)
                    self.state.update(state="rolled_back", error=reason[-2000:])
                except Exception as exc2:  # noqa: BLE001
                    self.state.update(state="failed", error=(
                        f"{reason[-1200:]} — and putting the previous environment back failed too: {exc2}. "
                        f"Repair by hand: the previous environment is in {rollback}")[-2000:])
        save(self.dir, self.state)
        return self.state


if __name__ == "__main__":
    Worker(sys.argv[1]).main()
