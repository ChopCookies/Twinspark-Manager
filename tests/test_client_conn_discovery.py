"""Workstation client: reading node A over SSH (probe, controller.yaml, API keys) and the setup checklist.

Most tests inject ``run`` with canned remote output. The POSIX-only ones run the real remote scripts
through the fake ssh in a sandbox whose PATH has fake systemctl / tsm / docker / nvidia-smi / tailscale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_client_ssh_fake import HOSTKEY, POSIX, FakeSSH, Sandbox
from twinspark.client import discovery as D
from twinspark.client import ssh as S
from twinspark.client.connections import Connection
from twinspark.client.discovery import CredentialAccessError, DiscoveryError
from twinspark.client.ssh import RemoteResult

REPO = Path(__file__).resolve().parents[1]
MGMT = "m" * 43
INF = "i" * 43


def conn(**kw) -> Connection:
    base = dict(id="spark-a", name="Spark A", host="spark-a.local", ssh_user="spark")
    base.update(kw)
    return Connection(**base)


class Remote:
    """An injectable ``run``: records argv, answers from a canned RemoteResult."""

    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = ""):
        self.result = RemoteResult(rc, stdout, stderr)
        self.calls: list[list[str]] = []

    async def __call__(self, c, argv, *, home, timeout, input=None):
        self.calls.append(argv)
        return self.result


CONTROLLER_PROBE = "\n".join([
    "probe=1", "hostname=spark-a", "user=spark", "groups=spark docker twinspark", "kernel=6.11.0-1016-nvidia",
    "arch=aarch64", "os_id=ubuntu", "os_version=24.04", "os_name=Ubuntu 24.04.3 LTS",
    "tsm_path=/usr/local/bin/tsm", "tsm_version=tsm 0.6.0", "systemctl=yes",
    "unit.twinspark-controller.active=active", "unit.twinspark-controller.enabled=enabled",
    "unit.twinspark-agent.active=active", "unit.twinspark-agent.enabled=enabled",
    "unit.twinspark-privd.active=inactive", "unit.twinspark-privd.enabled=disabled",
    "unit.twinspark-terminal.active=inactive", "unit.twinspark-terminal.enabled=",
    "config.controller=yes", "config.agent=yes", "config.agent.readable=yes",
    "runtime_mode_line=runtime_mode: docker   # was dry-run", "nvidia_smi=yes", "nvidia_driver=580.95.05",
    "docker=yes", "docker_version=28.3.3", "tailscale_ip=100.64.0.1", "noise without equals", "BAD KEY=x", "done=1",
]) + "\n"

AGENT_PROBE = "\n".join([
    "probe=1", "hostname=spark-b", "user=spark", "tsm_path=/opt/twinspark/venv/bin/tsm", "tsm_version=tsm 0.6.0",
    "systemctl=yes", "unit.twinspark-agent.active=active", "unit.twinspark-agent.enabled=enabled",
    "config.controller=no", "config.agent=yes", "config.agent.readable=yes", "runtime_mode_line=",
    "nvidia_smi=no", "docker=yes",
    "docker_error=permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock",
    "done=1",
]) + "\n"

BARE_PROBE = "probe=1\nhostname=fresh\nuser=me\ntsm_path=\nsystemctl=no\nconfig.controller=no\nconfig.agent=no\n" \
             "nvidia_smi=yes\nnvidia_driver=\ndocker=no\ndone=1\n"


# ---- probe ----------------------------------------------------------------------------------------
async def test_probe_installed_controller(tmp_path):
    run = Remote(stdout=CONTROLLER_PROBE)
    f = await D.probe_node(conn(), home=tmp_path, run=run)
    assert run.calls == [["sh", "-c", D.PROBE_SCRIPT, "tsm-probe"]]
    assert f["role"] == "controller" and f["complete"] and f["probe_rc"] == 0
    assert f["tsm"] == {"installed": True, "path": "/usr/local/bin/tsm", "version": "0.6.0"}
    assert f["os"] == {"id": "ubuntu", "version": "24.04", "name": "Ubuntu 24.04.3 LTS"}
    assert f["services"]["twinspark-controller"] == {"active": "active", "enabled": "enabled"}
    assert f["services"]["twinspark-terminal"] == {"active": "inactive", "enabled": "not-found"}
    assert f["runtime_mode"] == "docker"
    assert f["nvidia"] == {"smi": True, "driver": "580.95.05"}
    assert f["docker"]["version"] == "28.3.3" and f["docker"]["error"] is None
    assert f["tailscale_ip"] == "100.64.0.1" and f["groups"] == ["spark", "docker", "twinspark"]


async def test_probe_agent_only(tmp_path):
    f = await D.probe_node(conn(), home=tmp_path, run=Remote(stdout=AGENT_PROBE))
    assert f["role"] == "agent"
    assert f["runtime_mode"] == "dry-run"                     # absent key: AgentConfig's default
    assert f["docker"]["error"] == "permission" and f["nvidia"] == {"smi": False, "driver": None}
    assert f["tsm"]["path"] == "/opt/twinspark/venv/bin/tsm"


async def test_probe_not_installed(tmp_path):
    f = await D.probe_node(conn(), home=tmp_path, run=Remote(stdout=BARE_PROBE))
    assert f["role"] == "not-installed" and f["tsm"]["installed"] is False
    assert f["systemctl"] is False and f["services"]["twinspark-agent"] == {"active": None, "enabled": None}
    assert f["docker"]["error"] == "missing" and f["runtime_mode"] is None


def test_probe_script_is_safe_for_the_remote_login_shell():
    for script in (D.PROBE_SCRIPT, D.SHOW_KEYS_SCRIPT, S.INSTALL_KEY_SCRIPT):
        assert "\\\\" not in script and "\\'" not in script        # fish reads these inside quotes
    for tool in ("systemctl is-active", "systemctl is-enabled", "nvidia-smi --query-gpu=driver_version",
                 "docker version --format '{{.Server.Version}}'", "tailscale ip -4", "/etc/os-release",
                 "/usr/local/bin/tsm", "/opt/twinspark/bin/tsm", "$HOME/.local/bin/tsm", "command -v tsm"):
        assert tool in D.PROBE_SCRIPT


def _sandbox_node(tmp_path: Path, *, controller: bool = True, agent_mode: str = "dry-run") -> Sandbox:
    box = Sandbox(tmp_path / "node")
    if controller:
        (box.etc / "controller.yaml").write_text((REPO / "deploy" / "controller.example.yaml").read_text())
    (box.etc / "agent.yaml").write_text(f"node: {{node_id: A, role: agent}}\nruntime_mode: {agent_mode}"
                                        "   # switch later\n")
    box.install_tsm("0.6.0", show=f"agent_token          {'a' * 43}\nmanagement_api_key   {MGMT}\n"
                                  f"inference_api_key    {INF}\nbackend_api_key      {'b' * 43}\n")
    box.systemctl({"twinspark-controller": ("active", "enabled"), "twinspark-agent": ("active", "enabled"),
                   "twinspark-privd": ("failed", "enabled")})
    box.tool("nvidia-smi", 'echo "580.95.05"\n')
    box.tool("docker", 'echo "permission denied while trying to connect to the Docker daemon socket" >&2; exit 1\n')
    box.tool("tailscale", 'echo 100.64.0.1\n')
    return box


@pytest.mark.skipif(not POSIX, reason="runs the real probe script under sh")
async def test_probe_script_runs_in_a_sandbox_node(tmp_path, monkeypatch):
    fake = FakeSSH(tmp_path, monkeypatch)
    home = tmp_path / "client"
    home.mkdir()
    S.trust_host_keys(home, "spark-a.local", 22, [S.HostKey.parse(*HOSTKEY.split())])
    box = _sandbox_node(tmp_path)
    fake.configure(shell=box.shell_config())
    f = await D.probe_node(conn(), home=home)
    assert f["complete"] and f["role"] == "controller"
    assert f["tsm"]["version"] == "0.6.0" and f["tsm"]["path"].endswith("/usr/local/bin/tsm")
    assert f["services"]["twinspark-privd"] == {"active": "failed", "enabled": "enabled"}
    assert f["services"]["twinspark-terminal"]["active"] == "inactive"
    assert f["runtime_mode"] == "dry-run"
    assert f["nvidia"]["driver"] == "580.95.05" and f["docker"]["error"] == "permission"
    assert f["tailscale_ip"] == "100.64.0.1" and f["kernel"]

    nodes = await D.read_controller_nodes(conn(), home=home)
    assert nodes["nodes"]["B"]["qsfp_ip"] == "192.168.100.2"
    keys = await D.fetch_credentials(conn(), home=home)
    assert keys == {"management_key": MGMT, "inference_key": INF}

    box.tool("sudo", 'echo "sudo: a password is required" >&2; exit 1\n')       # a fake sudo, never the real one
    with pytest.raises(CredentialAccessError) as exc:
        await D.fetch_credentials(conn(), home=home, sudo=True)
    assert exc.value.reason == "permission" and "sudo" in exc.value.message


@pytest.mark.skipif(not POSIX, reason="runs the real probe script under sh")
async def test_probe_script_on_a_bare_machine(tmp_path, monkeypatch):
    fake = FakeSSH(tmp_path, monkeypatch)
    home = tmp_path / "client"
    home.mkdir()
    S.trust_host_keys(home, "spark-a.local", 22, [S.HostKey.parse(*HOSTKEY.split())])
    box = Sandbox(tmp_path / "node")                          # no tsm, no systemctl, no docker, no GPU
    fake.configure(shell=box.shell_config())
    f = await D.probe_node(conn(), home=home)
    assert f["complete"] and f["role"] == "not-installed" and not f["tsm"]["installed"]
    assert f["systemctl"] is False and f["docker"]["error"] == "missing" and f["nvidia"]["smi"] is False
    with pytest.raises(CredentialAccessError) as exc:
        await D.fetch_credentials(conn(), home=home)
    assert exc.value.reason == "not-installed"
    with pytest.raises(CredentialAccessError) as exc:
        await D.fetch_credentials(conn(), home=home, sudo=True)
    assert exc.value.reason == "not-installed"
    with pytest.raises(DiscoveryError) as exc:
        await D.read_controller_nodes(conn(), home=home)
    assert exc.value.reason == "missing"


# ---- controller.yaml ---------------------------------------------------------------------------------
async def test_read_controller_nodes_from_the_reference_config(tmp_path):
    run = Remote(stdout=(REPO / "deploy" / "controller.example.yaml").read_text())
    got = await D.read_controller_nodes(conn(), home=tmp_path, run=run)
    assert run.calls == [["cat", "/etc/twinspark/controller.yaml"]]
    assert got["nodes"] == {
        "A": {"qsfp_ip": "192.168.100.1", "agent_url": "http://127.0.0.1:9443", "ssh_user": None},
        "B": {"qsfp_ip": "192.168.100.2", "agent_url": "http://192.168.100.2:9443", "ssh_user": "twinspark"},
    }
    assert (got["manager_port"], got["manager_forward_host"], got["manager_tls"]) == (8443, "127.0.0.1", False)
    assert (got["gateway_port"], got["gateway_bind"], got["gateway_forward_host"]) == (8000, "0.0.0.0", "127.0.0.1")
    assert got["hostname"] == "spark-a" and got["management_auth"] == "apikey"


def test_parse_controller_yaml_variants():
    got = D.parse_controller_yaml("listener: {bind: 100.64.0.1, port: 9443, tls_cert: /c.pem, tls_key: /k.pem}\n"
                                  "gateway_listener: {bind: '::1', port: 8001}\n")
    assert (got["manager_port"], got["manager_forward_host"], got["manager_tls"]) == (9443, "100.64.0.1", True)
    assert (got["gateway_port"], got["gateway_forward_host"]) == (8001, "::1")
    assert D.parse_controller_yaml("")["manager_port"] == 8443 and D.parse_controller_yaml("")["nodes"] == {}
    for bad in ("listener: {port: 70000}", "listener: {port: '8443'}", "- a\n- b", "a: [unclosed"):
        with pytest.raises(DiscoveryError):
            D.parse_controller_yaml(bad)
    assert D.forward_host("-oProxyCommand=x") == "127.0.0.1"


@pytest.mark.parametrize("stderr,reason", [
    ("cat: /etc/twinspark/controller.yaml: No such file or directory", "missing"),
    ("cat: /etc/twinspark/controller.yaml: Permission denied", "permission"),
    ("cat: weird", "other"),
])
async def test_read_controller_nodes_errors(tmp_path, stderr, reason):
    with pytest.raises(DiscoveryError) as exc:
        await D.read_controller_nodes(conn(), home=tmp_path, run=Remote(1, "", stderr))
    assert exc.value.reason == reason and exc.value.message


# ---- API keys --------------------------------------------------------------------------------------------
def _init_show(**vals) -> str:
    """Exactly how `tsm init --show` prints (cmd_init: f"{slot:20} {value or '(not on this node)'}")."""
    return "".join(f"{s:20} {vals.get(s) or '(not on this node)'}\n"
                   for s in ("agent_token", "management_api_key", "inference_api_key", "backend_api_key"))


async def test_fetch_credentials(tmp_path):
    run = Remote(stdout=_init_show(agent_token="a" * 43, management_api_key=MGMT, inference_api_key=INF,
                                   backend_api_key="b" * 43))
    assert await D.fetch_credentials(conn(), home=tmp_path, run=run) == {"management_key": MGMT, "inference_key": INF}
    assert run.calls == [["sh", "-c", D.SHOW_KEYS_SCRIPT, "tsm-show-keys", "plain"]]
    partial = Remote(stdout=_init_show(management_api_key=MGMT))
    assert await D.fetch_credentials(conn(), home=tmp_path, run=partial) == {"management_key": MGMT,
                                                                             "inference_key": None}
    sudo = Remote(stdout=_init_show(management_api_key=MGMT))
    await D.fetch_credentials(conn(), home=tmp_path, run=sudo, sudo=True)
    assert sudo.calls[0][-1] == "sudo"


@pytest.mark.parametrize("rc,stdout,stderr,reason,phrase", [
    (1, "", "Traceback (most recent call last):\n  ...\nPermissionError: [Errno 13] Permission denied: "
            "'/etc/twinspark/secrets/.master.key'\n", "permission", "cannot read the TwinSpark vault"),
    (1, "", "PermissionError: cannot read /etc/twinspark/secrets/x.enc: it belongs to another user\n", "permission",
     "sudo tsm init --show"),
    (127, "", "tsm-not-found\n", "not-installed", "not installed"),
    (1, "", "no secrets in /etc/twinspark/secrets: this is not an installed node\n", "missing", "not set up"),
    (0, _init_show(agent_token="a" * 43), "", "missing", "node B"),
    (2, "", "something else\n", "other", "Paste the keys"),
])
async def test_fetch_credentials_errors(tmp_path, rc, stdout, stderr, reason, phrase):
    with pytest.raises(CredentialAccessError) as exc:
        await D.fetch_credentials(conn(), home=tmp_path, run=Remote(rc, stdout, stderr))
    assert exc.value.reason == reason and phrase in exc.value.message


async def test_credential_errors_never_carry_key_values(tmp_path):
    leaky = Remote(1, _init_show(management_api_key=MGMT), f"PermissionError: {MGMT}\n")
    with pytest.raises(CredentialAccessError) as exc:
        await D.fetch_credentials(conn(), home=tmp_path, run=leaky)
    assert MGMT not in str(exc.value) and MGMT not in exc.value.message


def test_parse_init_show_ignores_junk():
    got = D.parse_init_show("management_api_key   has spaces in it\ninference_api_key    short\n"
                            f"WARNING something\nmanagement_api_key   {MGMT}\n")
    assert got == {"management_api_key": MGMT, "inference_api_key": None}


# ---- setup checklist ----------------------------------------------------------------------------------------
def _by_id(checks):
    assert all(set(c) == {"id", "title", "status", "detail", "fix"} for c in checks)
    assert all(c["status"] in ("ok", "warn", "todo", "blocked") for c in checks)
    return {c["id"]: c for c in checks}


IDS = ["ssh", "installed", "role", "live", "nvidia", "docker", "peer", "credentials"]


def test_setup_checks_all_good():
    facts = D.facts_from_kv(D.parse_kv(CONTROLLER_PROBE))
    nodes = D.parse_controller_yaml((REPO / "deploy" / "controller.example.yaml").read_text())
    checks = D.setup_checks(facts, nodes, True, client_version="0.6.0")
    assert [c["id"] for c in checks] == IDS
    by = _by_id(checks)
    assert all(c["status"] == "ok" for c in checks), checks
    assert "192.168.100.2" in by["peer"]["detail"] and "0.6.0" in by["installed"]["detail"]


def test_setup_checks_unreachable():
    by = _by_id(D.setup_checks({"error": "Could not reach spark-a on port 22."}, None, None))
    assert by["ssh"]["status"] == "blocked" and "Could not reach" in by["ssh"]["detail"]
    assert all(by[i]["status"] == "todo" for i in IDS[1:])
    assert _by_id(D.setup_checks(None, None, None))["ssh"]["status"] == "blocked"


def test_setup_checks_problems_and_fixes():
    facts = D.facts_from_kv(D.parse_kv(AGENT_PROBE))
    by = _by_id(D.setup_checks(facts, None, False, client_version="0.6.1"))
    assert by["role"]["status"] == "blocked" and "node B" in by["role"]["detail"]
    assert by["live"]["status"] == "todo" and by["live"]["fix"]["command"].startswith("sudo tsm go-live")
    assert by["docker"]["status"] == "warn" and "usermod -aG docker spark" in by["docker"]["fix"]["command"]
    assert by["nvidia"]["status"] == "warn"
    assert by["credentials"]["status"] == "todo" and "sudo tsm init --show" in by["credentials"]["fix"]["command"]
    assert by["installed"]["status"] == "ok"                          # 0.6.0 vs 0.6.1: same minor

    old = D.facts_from_kv(D.parse_kv(CONTROLLER_PROBE.replace("tsm 0.6.0", "tsm 0.5.1")
                                     .replace("controller.active=active", "controller.active=failed")))
    by = _by_id(D.setup_checks(old, {"nodes": {"A": {}}}, None, client_version="0.6.0"))
    assert by["installed"]["status"] == "warn" and "git pull" in by["installed"]["fix"]["command"]
    assert by["role"]["status"] == "warn" and "failed" in by["role"]["detail"]
    assert by["peer"]["status"] == "todo" and "join-code" in by["peer"]["fix"]["command"]
    assert by["credentials"]["detail"] == "not fetched yet"

    bare = D.facts_from_kv(D.parse_kv(BARE_PROBE))
    by = _by_id(D.setup_checks(bare, None, None))
    assert by["installed"]["status"] == "todo" and "install.sh" in by["installed"]["fix"]["command"]
    assert by["role"]["status"] == "todo" and by["live"]["status"] == "todo"
    assert by["docker"]["status"] == "warn" and by["nvidia"]["status"] == "warn"
