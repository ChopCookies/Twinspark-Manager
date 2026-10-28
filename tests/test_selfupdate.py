"""`tsm update`: releases, the root-side request checks, the worker (with rollback), the controller's run."""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx
import pytest

from twinspark import releases
from twinspark.agent import selfupdate as root
from twinspark.agent import update_worker
from twinspark.controller.app import create_app
from twinspark.controller.controller import BusyError

from .conftest import Cluster, draft

WHEEL = b"PK fake wheel"
CONS = b"fastapi==0.162.0\n"


def gh_release(version="9.9.9", *, prerelease=False, draft_=False, digests=True, assets=None):
    names = assets if assets is not None else [releases.wheel_name(version), releases.CONSTRAINTS, releases.SUMS]
    data = {releases.wheel_name(version): WHEEL, releases.CONSTRAINTS: CONS}
    return {"tag_name": f"v{version}", "draft": draft_, "prerelease": prerelease,
            "published_at": "2026-11-01T10:00:00Z", "body": f"notes for {version}", "html_url": f"https://github.com/{releases.REPO}/releases/tag/v{version}",
            "assets": [{"name": n, "size": len(data.get(n, b"x")),
                        "digest": f"sha256:{hashlib.sha256(data[n]).hexdigest()}" if digests and n in data else None}
                       for n in names]}


def sums_text(version="9.9.9", wheel=WHEEL) -> str:
    return (f"{hashlib.sha256(wheel).hexdigest()}  {releases.wheel_name(version)}\n"
            f"{hashlib.sha256(CONS).hexdigest()}  {releases.CONSTRAINTS}\n")


def github(listing, sums=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=listing)
        name = request.url.path.rsplit("/", 1)[-1]
        if name == releases.SUMS:
            return httpx.Response(200, text=sums if sums is not None else sums_text())
        return httpx.Response(404)
    return httpx.MockTransport(handler)


# ---- releases ------------------------------------------------------------------------------------
def test_versions_sort_with_prereleases_before_the_final():
    assert releases.parse_version("0.8.0rc1") < releases.parse_version("0.8.0") < releases.parse_version("0.10.0")
    assert releases.newer("0.8.0", "0.7.1") and not releases.newer("0.7.1", "0.8.0")
    assert releases.parse_version("0.8") is None and releases.parse_version("0.8.0; rm -rf /") is None
    assert releases.is_prerelease("1.0.0b2") and not releases.is_prerelease("1.0.0")


def test_the_newest_installable_release_is_chosen_and_its_files_get_hashes():
    listing = [gh_release("0.8.0"), gh_release("0.9.0rc1", prerelease=True), gh_release("1.0.0", draft_=True),
               {"tag_name": "nightly", "assets": []}]
    with httpx.Client(transport=github(listing, sums_text("0.8.0"))) as c:
        out = releases.latest(c)
    assert out["version"] == "0.8.0" and out["tag"] == "v0.8.0"
    assert out["wheel"]["url"] == f"https://github.com/{releases.REPO}/releases/download/v0.8.0/" \
                                  "twinspark_manager-0.8.0-py3-none-any.whl"
    assert out["wheel"]["sha256"] == hashlib.sha256(WHEEL).hexdigest()
    with httpx.Client(transport=github(listing, sums_text("0.9.0rc1"))) as c:
        assert releases.latest(c, allow_prerelease=True)["version"] == "0.9.0rc1"
        assert releases.latest(c, version="0.9.0rc1")["prerelease"] is True


def test_an_older_version_can_be_named_to_go_back():
    listing = [gh_release("0.9.0"), gh_release("0.8.0")]
    with httpx.Client(transport=github(listing, sums_text("0.8.0"))) as c:
        assert releases.latest(c, version="v0.8.0")["version"] == "0.8.0"
        with pytest.raises(releases.ReleaseError, match="no release 0.7.9"):
            releases.latest(c, version="0.7.9")


def test_a_release_without_the_workflow_files_is_not_installable():
    listing = [gh_release("0.8.0", assets=[releases.wheel_name("0.8.0")])]
    with httpx.Client(transport=github(listing)) as c:
        with pytest.raises(releases.ReleaseError, match="no release with installable files"):
            releases.latest(c)
        with pytest.raises(releases.ReleaseError, match="lacks"):
            releases.latest(c, version="0.8.0")


def test_checksums_must_list_the_files_and_agree_with_github():
    rel = releases.parse_release(gh_release("9.9.9"))
    with pytest.raises(releases.ReleaseError, match="does not list"):
        releases.plan(rel, f"{hashlib.sha256(CONS).hexdigest()}  {releases.CONSTRAINTS}\n")
    with pytest.raises(releases.ReleaseError, match="GitHub reports"):
        releases.plan(rel, sums_text(wheel=b"something else"))
    no_digest = releases.parse_release(gh_release("9.9.9", digests=False))
    assert releases.plan(no_digest, sums_text())["wheel"]["sha256"] == hashlib.sha256(WHEEL).hexdigest()
    with pytest.raises(releases.ReleaseError, match="unreadable"):
        releases.parse_sums("not a checksum line")
    with pytest.raises(releases.ReleaseError, match="twice"):
        releases.parse_sums(f"{'a' * 64}  x\n{'b' * 64}  x\n")
    assert releases.parse_sums(f"{'A' * 64} *x.whl\n") == {"x.whl": "a" * 64}


def test_urls_only_ever_point_at_this_repository():
    with pytest.raises(releases.ReleaseError):
        releases.asset_url("v1.0.0/../../evil", "x.whl")
    with pytest.raises(releases.ReleaseError):
        releases.asset_url("v1.0.0", "../x.whl")
    assert releases.asset_url("v1.0.0", "a.whl").startswith(f"https://github.com/{releases.REPO}/releases/download/")


# ---- root side: what privd accepts -----------------------------------------------------------------
def target(version="9.9.9", wheel=WHEEL):
    return {"tag": f"v{version}", "version": version,
            "wheel": {"name": releases.wheel_name(version), "sha256": hashlib.sha256(wheel).hexdigest(),
                      "url": "https://evil.example/x.whl"},
            "constraints": {"name": releases.CONSTRAINTS, "sha256": hashlib.sha256(CONS).hexdigest()}}


def test_privd_builds_names_and_urls_itself_and_takes_only_tag_and_hashes():
    t = root.check_target(target())
    assert t["wheel"]["url"].startswith(f"https://github.com/{releases.REPO}/releases/download/v9.9.9/")
    assert t["wheel"]["name"] == "twinspark_manager-9.9.9-py3-none-any.whl"
    for bad in ({**target(), "tag": "main"}, {**target(), "tag": "v1.0.0 --index-url x"},
                {**target(), "wheel": {"sha256": "A" * 64}}, {**target(), "constraints": None}, "v1.0.0"):
        with pytest.raises(ValueError):
            root.check_target(bad)


@pytest.fixture
def privd_root(tmp_path, monkeypatch):
    venv = tmp_path / "opt" / "venv"
    (venv / "bin").mkdir(parents=True)
    (tmp_path / "opt" / root.MARKER).write_text("x")
    monkeypatch.setattr(root, "ROOT", tmp_path / "runs")
    monkeypatch.setattr(root, "policy", lambda: {"enabled": True, "allow_prerelease": False})
    monkeypatch.setattr(root, "install_venv", lambda: venv)
    monkeypatch.setattr(root, "base_python", lambda: "/usr/bin/python3.12")
    monkeypatch.setattr(root, "running_units", lambda: ["twinspark-agent.service"])
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(argv, **kw):
        calls.append(argv)
        return Done()

    monkeypatch.setattr(root.subprocess, "run", fake_run)
    return venv, calls


def test_privd_start_runs_a_copied_worker_with_the_base_interpreter_once(privd_root):
    venv, calls = privd_root
    rid = "c" * 32
    state = root.start({"run_id": rid, "target": target()})
    assert state["state"] == "queued" and state["venv"] == str(venv)
    assert state["units"] == ["twinspark-agent.service"]
    argv = calls[-1]
    assert argv[:2] == ["/usr/bin/systemd-run", f"--unit=twinspark-update-{rid}"]
    assert argv[-5:-2] == ["/usr/bin/python3.12", "-I", "-S"]
    worker = Path(argv[-2])
    assert worker.parent == root.ROOT / rid and worker.read_bytes() == Path(update_worker.__file__).read_bytes()
    assert root.start({"run_id": rid, "target": target()})["run_id"] == rid      # a lost reply: same run
    assert len([c for c in calls if c[0].endswith("systemd-run")]) == 1
    with pytest.raises(ValueError, match="another release"):
        root.start({"run_id": rid, "target": target(wheel=b"other")})
    with pytest.raises(RuntimeError, match="still running"):
        root.start({"run_id": "d" * 32, "target": target()})


def test_privd_refuses_when_switched_off_or_for_prereleases(privd_root, monkeypatch):
    with pytest.raises(RuntimeError, match="does not allow"):
        root.start({"run_id": "e" * 32, "target": target("9.9.9rc1")})
    monkeypatch.setattr(root, "policy", lambda: {"enabled": False, "allow_prerelease": False})
    with pytest.raises(RuntimeError, match="switched off"):
        root.start({"run_id": "e" * 32, "target": target()})
    with pytest.raises(ValueError, match="run ID"):
        root.start({"run_id": "../x", "target": target()})


def test_policy_is_off_without_a_file_and_must_be_root_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "POLICY", tmp_path / "missing.json")
    assert root.policy() == {"enabled": False, "allow_prerelease": False, "missing": True}
    p = tmp_path / "update-policy.json"
    p.write_text('{"enabled": true}')
    monkeypatch.setattr(root, "POLICY", p)
    import os
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        with pytest.raises(RuntimeError, match="owned by root"):
            root.policy()


def test_install_venv_requires_the_install_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(root.sys, "prefix", str(tmp_path / "somewhere"))
    with pytest.raises(RuntimeError, match="not installed by install.sh"):
        root.install_venv()


def test_status_notices_a_worker_that_died(privd_root, monkeypatch):
    rid = "f" * 32
    root.start({"run_id": rid, "target": target()})
    state = json.loads((root.ROOT / rid / "state.json").read_text())
    state["updated_at"] = time.time() - 120
    (root.ROOT / rid / "state.json").write_text(json.dumps(state))

    class Inactive:
        returncode, stdout, stderr = 3, "inactive\n", ""

    monkeypatch.setattr(root.subprocess, "run", lambda argv, **kw: Inactive())
    out = root.status({"run_id": rid})
    assert out["state"] == "failed" and "stopped without finishing" in out["error"]
    assert root.status({"run_id": "0" * 32})["state"] == "missing"


# ---- the worker --------------------------------------------------------------------------------------
def test_the_worker_imports_only_the_standard_library():
    tree = ast.parse(Path(update_worker.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports: the worker runs from a copy"
            names.add((node.module or "").split(".")[0])
    names.discard("__future__")
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)


class FakeHost:
    """pip, python and systemctl for the worker; a unit can be made to crash after its restart."""

    def __init__(self, venv: Path, version="9.9.9", *, pip_fails=False, crashing=()):
        self.venv, self.version, self.pip_fails, self.crashing = venv, version, pip_fails, set(crashing)
        self.calls: list[list[str]] = []
        self.clock = 0.0
        self.pid = 100

    def now(self):
        return self.clock

    def sleep(self, s):
        self.clock += s

    def fetch(self, url):
        files = {"twinspark_manager-9.9.9-py3-none-any.whl": WHEEL, "constraints-release.txt": CONS}
        return files[url.rsplit("/", 1)[-1]]

    def run(self, argv, timeout=120):
        self.calls.append(list(argv))
        if argv[1:4] == ["-m", "pip", "install"]:
            if self.pip_fails:
                (self.venv / "lib" / "half-installed").write_text("broken")
                raise RuntimeError("pip install failed (1): no matching distribution")
            (self.venv / "lib" / "program.txt").write_text(f"version {self.version}")
            return ""
        if argv[1:3] == ["-I", "-c"]:
            return f"{(self.venv / 'lib' / 'program.txt').read_text().split()[-1]}\n"
        if argv[1] == "restart":
            self.pid += 1
            return ""
        if argv[1] == "show":
            unit = argv[2]
            if unit in self.crashing:
                self.pid += 1
                return f"ActiveState=activating\nSubState=auto-restart\nMainPID=0\nNRestarts={self.pid}\n"
            return f"ActiveState=active\nSubState=running\nMainPID={self.pid}\nNRestarts=0\n"
        raise AssertionError(argv)


def worker_run(tmp_path, units=("twinspark-agent.service", "twinspark-controller.service")):
    venv = tmp_path / "opt" / "venv"
    (venv / "lib").mkdir(parents=True)
    (venv / "lib" / "program.txt").write_text("version 0.8.0")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    t = root.check_target(target())
    update_worker.save(str(run_dir), {"run_id": "a" * 32, "state": "queued", "phase": "queued", "target": t,
                                      "venv": str(venv), "units": list(units), "from_version": "0.8.0"})
    return venv, run_dir


def make_worker(run_dir, host):
    return update_worker.Worker(str(run_dir), fetch=host.fetch, run=host.run, sleep=host.sleep, now=host.now)


def test_worker_installs_verifies_restarts_and_keeps_the_previous_environment(tmp_path):
    venv, run_dir = worker_run(tmp_path)
    host = FakeHost(venv)
    out = make_worker(run_dir, host).main()
    assert out["state"] == "completed", out
    assert (venv / "lib" / "program.txt").read_text() == "version 9.9.9"
    assert (Path(f"{venv}.previous") / "lib" / "program.txt").read_text() == "version 0.8.0"
    assert not Path(f"{venv}.rollback").exists()
    restarts = [c[2] for c in host.calls if c[1] == "restart"]
    assert restarts == ["twinspark-agent.service", "twinspark-controller.service"]       # controller last
    pip = next(c for c in host.calls if c[1:4] == ["-m", "pip", "install"])
    assert "-c" in pip and pip[-1].endswith("twinspark_manager-9.9.9-py3-none-any.whl[hf]")
    assert (venv.parent / "source-commit").read_text() == "release:v9.9.9\n"
    assert [p for p, _ in json.loads((run_dir / "state.json").read_text())["phases"]] == \
        ["download", "backup", "install", "verify", "restart"]


def test_a_download_with_the_wrong_hash_changes_nothing(tmp_path):
    venv, run_dir = worker_run(tmp_path)
    host = FakeHost(venv)
    host.fetch = lambda url: b"tampered"
    out = make_worker(run_dir, host).main()
    assert out["state"] == "failed" and "not the published" in out["error"]
    assert (venv / "lib" / "program.txt").read_text() == "version 0.8.0"
    assert not any(c[1:3] == ["-m", "pip"] or c[1] == "restart" for c in host.calls)


def test_a_failed_install_puts_the_environment_back_without_restarting(tmp_path):
    venv, run_dir = worker_run(tmp_path)
    host = FakeHost(venv, pip_fails=True)
    out = make_worker(run_dir, host).main()
    assert out["state"] == "rolled_back" and "no matching distribution" in out["error"]
    assert (venv / "lib" / "program.txt").read_text() == "version 0.8.0"
    assert not (venv / "lib" / "half-installed").exists()
    assert not any(c[1] == "restart" for c in host.calls)        # the services never ran the new program


def test_a_service_that_crashes_on_the_new_version_is_rolled_back_and_restarted(tmp_path):
    venv, run_dir = worker_run(tmp_path)
    host = FakeHost(venv, crashing={"twinspark-controller.service"})
    w = make_worker(run_dir, host)
    real_rollback = w.rollback

    def rollback(rb, reason):
        host.crashing.clear()            # the old version starts fine again
        real_rollback(rb, reason)

    w.rollback = rollback
    out = w.main()
    assert out["state"] == "rolled_back" and "did not stay up" in out["error"]
    assert (venv / "lib" / "program.txt").read_text() == "version 0.8.0"
    restarts = [c[2] for c in host.calls if c[1] == "restart"]
    assert restarts == ["twinspark-agent.service", "twinspark-controller.service",
                        "twinspark-agent.service", "twinspark-controller.service"]


def test_a_version_mismatch_after_install_is_rolled_back(tmp_path):
    venv, run_dir = worker_run(tmp_path)
    host = FakeHost(venv, version="0.0.1")
    out = make_worker(run_dir, host).main()
    assert out["state"] == "rolled_back" and "expected 9.9.9" in out["error"]


def test_the_worker_never_runs_a_run_twice(tmp_path):
    venv, run_dir = worker_run(tmp_path)
    host = FakeHost(venv)
    make_worker(run_dir, host).main()
    with pytest.raises(RuntimeError, match="newly queued"):
        make_worker(run_dir, host).main()


# ---- the controller's run ------------------------------------------------------------------------------
def plan_for(version="9.9.9"):
    rel = releases.parse_release(gh_release(version))
    return releases.plan(rel, sums_text(version))


@pytest.fixture
def upd(cluster):
    c = cluster.controller

    async def fetch(version, allow_prerelease):
        if version and version != "9.9.9":
            raise releases.ReleaseError(f"there is no release {version}")
        return plan_for()

    c.selfupdate.fetch = fetch
    return cluster


async def test_a_simulated_update_runs_b_first_then_a_and_completes(upd):
    c = upd.controller
    info = await c.selfupdate.check()
    assert info["ready"] and info["dry_run"] and info["order"] == ["B", "A"], info
    assert info["release"]["latest"] == "9.9.9" and info["available"]
    await c.selfupdate.start()
    await c.selfupdate.task
    done = c.selfupdate.state()
    assert done["state"] == "completed", done
    assert done["dispatched"] == ["B", "A"] and done["target"]["version"] == "9.9.9"
    await c.aclose()


class RealNode:
    """An agent in docker mode as the controller sees it: privd states, then the new version."""

    log: list[str] = []                      # every node's calls, in order

    def __init__(self, inner, name, version="0.8.0"):
        self.inner, self.name, self.version = inner, name, version
        self.runs: dict[str, dict] = {}
        self.script: list[str] = []          # states the next status calls return
        self.calls: list[str] = []

    async def call(self, action, timeout=None, **params):
        self.calls.append(action)
        RealNode.log.append(f"{self.name}:{action}")
        if action == "update_probe":
            return {"enabled": True, "dry_run": False, "version": self.version, "units": [], "active_run": None}
        if action == "update_start":
            self.runs[params["run_id"]] = {"state": "queued", "phase": "queued", "from_version": self.version}
            return self.runs[params["run_id"]]
        if action == "update_status":
            run = self.runs.get(params["run_id"])
            if run is None:
                return {"state": "missing"}
            if self.script:
                run["state"] = self.script.pop(0)
                if run["state"] == "completed":
                    self.version = "9.9.9"
                if run["state"] == "rolled_back":
                    run["error"] = "the controller did not stay up"
            return run
        if action == "hardware_facts":
            return {"agent_version": self.version}
        return await self.inner.call(action, timeout=timeout, **params)

    async def aclose(self):
        await self.inner.aclose()


async def test_b_then_a_with_the_controller_restarting_in_between(upd, controller_config, tmp_path, monkeypatch):
    c = upd.controller
    RealNode.log.clear()
    nodes = {n: RealNode(a, n) for n, a in c.agents.items()}
    c.agents.update(nodes)
    nodes["B"].script = ["running", "completed"]
    nodes["A"].script = ["running", "running"]
    await c.selfupdate.start()
    for _ in range(400):
        st = c.selfupdate.state()
        if st["index"] == 1 and "A" in st["dispatched"] and not nodes["A"].script:
            break
        await __import__("asyncio").sleep(0.01)
    assert RealNode.log.index("B:update_start") < RealNode.log.index("A:update_start")
    assert RealNode.log.index("B:hardware_facts") < RealNode.log.index("A:update_start")   # B proved its version
    # node A's worker restarts the controller here: the old process stops in the middle of the run
    c.selfupdate.task.cancel()
    await c.aclose()
    # …and the new one (version 9.9.9) finishes the run from the database
    from twinspark.controller.controller import Controller
    monkeypatch.setattr("twinspark.controller.selfupdate.__version__", "9.9.9")
    c2 = Controller(controller_config, upd.gateway, dict(nodes), store=c.store, poll_interval=0.01)
    c2.selfupdate.fetch = c.selfupdate.fetch
    nodes["A"].script = ["completed"]
    c2.selfupdate.ensure_running()
    await c2.selfupdate.task
    assert c2.selfupdate.state()["state"] == "completed", c2.selfupdate.state()
    await c2.aclose()


async def test_a_rolled_back_node_b_stops_the_run_before_node_a(upd):
    c = upd.controller
    nodes = {n: RealNode(a, n) for n, a in c.agents.items()}
    c.agents.update(nodes)
    nodes["B"].script = ["running", "rolled_back"]
    await c.selfupdate.start()
    await c.selfupdate.task
    st = c.selfupdate.state()
    assert st["state"] == "failed" and "node B" in st["error"] and "rolled back" in st["error"]
    assert "update_start" not in nodes["A"].calls
    assert not c.selfupdate.blocking()          # nothing is held: the model was never stopped
    await c.aclose()


async def test_node_a_waits_for_requests_in_flight_before_its_restart(upd, monkeypatch):
    c = upd.controller
    nodes = {n: RealNode(a, n) for n, a in c.agents.items()}
    c.agents.update(nodes)
    nodes["B"].script = ["completed"]
    c.gateway.set_route("default", ["http://10.0.0.1:18100"], "m", "rev")
    inflight = [2]
    monkeypatch.setattr(c.gateway, "inflight", lambda aliases: (inflight[0], 1.0))
    c.config.runtime.drain_timeout_s = 0.3
    await c.selfupdate.start()
    for _ in range(300):
        if "A" in c.selfupdate.state()["dispatched"]:
            break
        await __import__("asyncio").sleep(0.01)
    st = c.selfupdate.state()
    assert st["cut_requests"] == 2 and "A" in st["dispatched"]
    c.selfupdate.task.cancel()
    await c.aclose()


async def test_switches_and_writes_wait_while_an_update_runs(upd):
    c = upd.controller
    c.selfupdate.save({"state": "running", "run_id": "1" * 32, "order": [], "index": 0, "nodes": {},
                       "dispatched": [], "deadline": time.time() + 60, "target": {"version": "9.9.9"}})
    p = c.create_profile(draft("u1"))
    with pytest.raises(BusyError, match="being updated"):
        await c.activate(p.name, "latest")
    app = create_app(c, "mgmt", run_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as h:
        h.headers["x-api-key"] = "mgmt"
        r = await h.post("/api/v1/profiles", json=draft("u2").model_dump(mode="json"))
        assert r.status_code == 409 and "being updated" in r.text
        assert (await h.get("/api/v1/system/update")).json()["state"] == "running"
    with pytest.raises(ValueError, match="being updated"):
        await c.maintenance.start()
    await c.aclose()


async def test_the_api_needs_the_confirmation_and_reports_refusals(upd):
    c = upd.controller
    app = create_app(c, "mgmt", run_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as h:
        h.headers["x-api-key"] = "mgmt"
        assert (await h.post("/api/v1/system/update/start", json={"confirm": "yes"})).status_code == 422
        r = await h.post("/api/v1/system/update/start", json={"confirm": "UPDATE TWINSPARK", "version": "1.2.3"})
        assert r.status_code == 409 and "no release 1.2.3" in r.text
        chk = (await h.post("/api/v1/system/update/check")).json()
        assert chk["versions"]["A"] and chk["release"]["latest"] == "9.9.9"
        r = await h.post("/api/v1/system/update/start", json={"confirm": "UPDATE TWINSPARK"})
        assert r.status_code == 200 and r.json()["state"] == "running"
    await c.selfupdate.task
    await c.aclose()


async def test_an_agent_older_than_0_8_is_named(upd):
    c = upd.controller

    class Old:
        async def call(self, action, timeout=None, **params):
            raise RuntimeError("unknown action: update_probe")

        async def aclose(self):
            pass

    c.agents["B"] = Old()
    info = await c.selfupdate.check()
    assert not info["ready"] and "older than 0.8.0" in info["problems"][0]
    with pytest.raises(ValueError, match="older than 0.8.0"):
        await c.selfupdate.start()
    await c.aclose()


async def test_github_unreachable_is_one_readable_line(cluster):
    c = cluster.controller

    async def offline(version, allow_prerelease):
        raise httpx.ConnectError("name resolution failed")

    c.selfupdate.fetch = offline
    info = await c.selfupdate.check()
    assert not info["ready"] and "not reachable from node A" in info["release_error"]
    await c.aclose()


def test_cluster_helper_is_importable():
    assert Cluster
