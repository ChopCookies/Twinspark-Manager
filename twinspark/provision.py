"""First-run provisioning: turn a handful of answers into configs, secrets, units and users.

``tsm setup`` (see :mod:`twinspark.cli_setup`) gathers the answers; this module does
the work and is deliberately free of prompts so it can be tested end to end against a
scratch directory (``Layout(root=tmp)``) without touching a real machine. Every path
in the generated files honours that root, so a sandbox install is self-contained and
can even be started with ``tsm serve agent --config <sandbox>/etc/twinspark/agent.yaml``.

Nothing here changes network configuration. It never runs ``netplan``, ``ip`` or
``nmcli``; a missing QSFP address is reported with a snippet for the operator to apply.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from .remote.policy import FEATURES as REMOTE_FEATURES
from .remote.policy import write_policy
from .schemas.config import AgentConfig, ControllerConfig, load_config
from .security import SecretsVault

JOIN_PREFIX = "tsm1."
UNIT_NAMES = ("twinspark-privd.service", "twinspark-agent.service", "twinspark-controller.service",
              "twinspark-terminal.service")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}\Z")
# one single-line OpenSSH public key; a newline would smuggle a second, unrestricted authorized_keys entry
_PUBKEY_RE = re.compile(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) "
                        r"[A-Za-z0-9+/=]{20,2000}(?: [^\r\n]{0,200})?")
MAX_JOIN_BYTES = 64 * 1024


class SetupError(Exception):
    """A problem the operator can fix; the message says how."""


# ---- layout ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Layout:
    """Where everything lives. ``root`` lets tests (and `--root`) build a self-contained sandbox."""

    root: Path = Path("/")

    def p(self, *parts: str) -> Path:
        return Path(self.root, *[x.lstrip("/") for x in parts])

    @property
    def sandbox(self) -> bool:
        return str(self.root) != "/"

    @property
    def etc(self) -> Path: return self.p("etc/twinspark")
    @property
    def secrets(self) -> Path: return self.p("etc/twinspark/secrets")
    @property
    def systemd(self) -> Path: return self.p("etc/systemd/system")
    @property
    def state(self) -> Path: return self.p("var/lib/twinspark")
    @property
    def cache(self) -> Path: return self.p("var/cache/twinspark")
    @property
    def home(self) -> Path: return self.p("opt/twinspark")
    @property
    def tsm_bin(self) -> Path: return self.p("opt/twinspark/venv/bin/tsm")
    @property
    def run(self) -> Path: return self.p("run/twinspark")
    @property
    def controller_yaml(self) -> Path: return self.etc / "controller.yaml"
    @property
    def agent_yaml(self) -> Path: return self.etc / "agent.yaml"
    @property
    def ssh_dir(self) -> Path: return self.state / "ssh"


# ---- answers -----------------------------------------------------------------------------------
@dataclass
class Answers:
    role: str = "controller"                 # controller (node A: controller + agent) | agent (node B) | single
    node_id: str = "A"
    hostname: str = ""
    service_user: str = "twinspark"
    create_user: bool = False
    group: str = "twinspark"                 # owns the privd socket
    docker_group: bool = True                # the host has a 'docker' group the agent must join

    qsfp_iface: Optional[str] = None
    qsfp_ip: Optional[str] = None
    peer_ip: Optional[str] = None
    peer_iface: Optional[str] = None
    rdma_hcas: list[str] = field(default_factory=list)
    ib_gid_index: Optional[int] = None
    peer_ssh_user: str = "twinspark"

    hf_cache_dir: Optional[str] = None
    mgmt_bind: str = "127.0.0.1"
    mgmt_port: int = 8443
    gateway_bind: str = "0.0.0.0"
    gateway_port: int = 8000
    agent_port: int = 9443
    terminal_port: int = 9444
    vllm_port: int = 8100
    runtime_mode: str = "dry-run"

    # secrets handed over from node A (node B only)
    agent_token: Optional[str] = None
    backend_api_key: Optional[str] = None
    authorize_key: Optional[str] = None      # A's SSH public key, authorised on B for weight sync
    hf_token: Optional[str] = None

    # remote-management opt-ins (written to the root-owned policy on this node)
    remote: dict[str, bool] = field(default_factory=dict)

    def validate(self) -> None:
        if self.role not in ("controller", "agent", "single"):
            raise SetupError(f"unknown role {self.role!r}")
        if not _USER_RE.match(self.service_user):
            raise SetupError(f"service user {self.service_user!r} is not a valid Linux user name")
        if not _USER_RE.match(self.peer_ssh_user):
            raise SetupError(f"ssh user {self.peer_ssh_user!r} is not a valid Linux user name")
        if self.runtime_mode not in ("dry-run", "docker"):
            raise SetupError("runtime mode must be dry-run or docker")
        for label, ip in (("QSFP address", self.qsfp_ip), ("peer address", self.peer_ip)):
            if self.role != "single" and not (ip and _IP_RE.match(ip) and all(int(x) < 256 for x in ip.split("."))):
                raise SetupError(f"{label} {ip!r} is not an IPv4 address")
        unknown = sorted(set(self.remote) - set(REMOTE_FEATURES))
        if unknown:
            raise SetupError(f"unknown remote-management option(s): {', '.join(unknown)} "
                             f"(known: {', '.join(REMOTE_FEATURES)})")
        for label, port in (("management", self.mgmt_port), ("gateway", self.gateway_port),
                            ("agent", self.agent_port), ("terminal", self.terminal_port),
                            ("vLLM", self.vllm_port)):
            if not 1024 <= int(port) <= 65535:
                raise SetupError(f"{label} port {port} must be between 1024 and 65535")
        if self.role == "agent" and not (self.agent_token and self.backend_api_key):
            raise SetupError("node B needs the join code from node A (agent token + backend key)")
        if self.mgmt_bind not in ("127.0.0.1", "::1", "localhost") and self.role != "agent":
            pass  # allowed (Tailscale/LAN); the wizard warns, the API key still protects it


# ---- join codes --------------------------------------------------------------------------------
def encode_join(data: dict[str, Any]) -> str:
    raw = zlib.compress(json.dumps(data, separators=(",", ":"), sort_keys=True).encode(), 9)
    return JOIN_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_join(code: str) -> dict[str, Any]:
    code = "".join(code.split())          # tolerate line wraps from a chat or terminal
    if not code.startswith(JOIN_PREFIX):
        raise SetupError("that is not a TwinSpark join code (it should start with 'tsm1.')")
    body = code[len(JOIN_PREFIX):]
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)), MAX_JOIN_BYTES)
        if inflater.unconsumed_tail:                  # bigger than any real join code: a bomb
            raise SetupError("that join code is far larger than a real one — refusing it")
        data = json.loads(raw)
    except (ValueError, zlib.error) as exc:
        raise SetupError("the join code is damaged or cut off — copy the whole line from node A") from exc
    if not isinstance(data, dict) or data.get("v") != 1:
        raise SetupError("unsupported join code version — update TwinSpark on both nodes")
    for k in ("agent_token", "backend_api_key", "a_ip", "b_ip"):
        if not data.get(k):
            raise SetupError(f"the join code is missing {k!r}")
    for k in ("a_ip", "b_ip"):
        if not (isinstance(data[k], str) and _IP_RE.match(data[k])):
            raise SetupError(f"the join code has an invalid address in {k!r}")
    if data.get("b_user") is not None and not (isinstance(data["b_user"], str) and _USER_RE.match(data["b_user"])):
        raise SetupError("the join code has an invalid user name")
    if data.get("pubkey") and not (isinstance(data["pubkey"], str) and _PUBKEY_RE.fullmatch(data["pubkey"].strip())):
        raise SetupError("the join code carries something that is not a single OpenSSH public key")
    return data


def make_join(a: Answers, vault: SecretsVault, pubkey: Optional[str]) -> str:
    return encode_join({
        "v": 1, "created": int(time.time()), "tsm": __version__,
        "agent_token": vault.get("agent_token"), "backend_api_key": vault.get("backend_api_key"),
        "a_ip": a.qsfp_ip, "b_ip": a.peer_ip, "b_iface": a.peer_iface or a.qsfp_iface,
        "b_user": a.peer_ssh_user, "agent_port": a.agent_port, "vllm_port": a.vllm_port,
        "runtime_mode": a.runtime_mode, "rdma_hcas": a.rdma_hcas, "ib_gid_index": a.ib_gid_index,
        "pubkey": pubkey,
    })


def apply_join(a: Answers, info: dict[str, Any]) -> Answers:
    """Fill node B's answers from node A's join code (explicit flags set earlier still win)."""
    a.role, a.node_id = "agent", "B"
    a.agent_token, a.backend_api_key = info["agent_token"], info["backend_api_key"]
    a.qsfp_ip, a.peer_ip = info["b_ip"], info["a_ip"]
    a.qsfp_iface = a.qsfp_iface or info.get("b_iface")
    a.agent_port = int(info.get("agent_port") or a.agent_port)
    a.vllm_port = int(info.get("vllm_port") or a.vllm_port)
    a.runtime_mode = info.get("runtime_mode") or a.runtime_mode
    if not a.rdma_hcas:
        a.rdma_hcas = list(info.get("rdma_hcas") or [])
    if a.ib_gid_index is None:
        a.ib_gid_index = info.get("ib_gid_index")
    a.authorize_key = info.get("pubkey")
    return a


# ---- file contents -----------------------------------------------------------------------------
def _hcas(h: list[str]) -> str:
    return "[" + ", ".join(h) + "]"


def render_controller_yaml(a: Answers, lay: Layout) -> str:
    ib = "null" if a.ib_gid_index is None else str(a.ib_gid_index)
    nodes = [
        "  A:",
        f"    agent_url: http://127.0.0.1:{a.agent_port}      # the agent on this machine",
        f"    qsfp_ip: {a.qsfp_ip or 'null'}               # address on the direct QSFP link",
        f"    qsfp_iface: {a.qsfp_iface or 'null'}",
        f"    rdma_hcas: {_hcas(a.rdma_hcas)}       # `tsm rdma` prints the right values",
        f"    ib_gid_index: {ib}",
        f"    terminal_port: {a.terminal_port}               # tsm-termd (only runs if the terminal is enabled)",
    ]
    if a.role != "single":
        nodes += [
            "  B:",
            f"    agent_url: http://{a.peer_ip}:{a.agent_port}     # agent on node B, reached over the QSFP link",
            f"    qsfp_ip: {a.peer_ip}",
            f"    qsfp_iface: {a.peer_iface or a.qsfp_iface or 'null'}",
            f"    ssh_user: {a.peer_ssh_user}                 # weights are copied A -> B over QSFP as this user",
            f"    rdma_hcas: {_hcas(a.rdma_hcas)}",
            f"    ib_gid_index: {ib}",
            f"    terminal_port: {a.terminal_port}",
        ]
    return f"""# {lay.controller_yaml} — written by `tsm setup` (TwinSpark {__version__})
# Safe to edit. Re-running `tsm setup` keeps this file unless you pass --force (a backup is made).
node: {{node_id: A, role: controller, hostname: {a.hostname or 'spark-a'}}}
db_path: {lay.state}/state.db
secrets_dir: {lay.secrets}

# Management API + web GUI. Localhost only by default: reach it with
#   ssh -L {a.mgmt_port}:localhost:{a.mgmt_port} <this machine>        (or bind to the Tailscale address)
listener: {{bind: {a.mgmt_bind}, port: {a.mgmt_port}}}
# Stable OpenAI-compatible endpoint for your clients (needs the inference_api_key).
gateway_listener: {{bind: {a.gateway_bind}, port: {a.gateway_port}}}

nodes:
{chr(10).join(nodes)}

runtime:
  vllm_port: {a.vllm_port}                      # internal; must not collide with a hand-started vLLM
  hf_cache_dir: {a.hf_cache_dir or lay.state / 'hf-cache'}
  compile_cache_dir: {lay.cache}/compile
  mods_dir: {lay.state}/mods
  ssh_key: {lay.ssh_dir}/id_ed25519
  ssh_known_hosts: {lay.ssh_dir}/known_hosts
  health_timeout_s: 1800
  drain_timeout_s: 30

autostart: true                     # adopt / re-activate the last healthy model after a reboot
auto_rollback: true

# Out-of-band power for a node that is off or hung (optional; see docs/remote-management.md).
# `tsm node info` on the node prints its MAC addresses. Uncomment, adjust, restart the controller:
#   nodes:
#     B:
#       wake: {{mac: "aa:bb:cc:dd:ee:ff", iface: {a.qsfp_iface or 'enp1s0f1np1'}, broadcast: 255.255.255.255}}
#       plug:                         # a smart plug / PDU outlet with an HTTP API (Tasmota, Shelly, Home Assistant)
#         off: {{url: "http://192.168.1.50/cm?cmnd=Power%20Off"}}
#         on:  {{url: "http://192.168.1.50/cm?cmnd=Power%20On"}}
"""


def render_agent_yaml(a: Answers, lay: Layout) -> str:
    bind = "127.0.0.1" if a.role in ("controller", "single") else a.qsfp_ip
    note = "      # QSFP address only — not reachable from the LAN" if a.role == "agent" else ""
    return f"""# {lay.agent_yaml} — written by `tsm setup` (TwinSpark {__version__})
node: {{node_id: {a.node_id}, role: agent}}
listener: {{bind: {bind}, port: {a.agent_port}}}{note}
secrets_dir: {lay.secrets}

# dry-run records the commands it WOULD run and simulates healthy containers.
# Switch to real containers with:  sudo tsm go-live     (after `tsm plan` looks right)
runtime_mode: {a.runtime_mode}
runtime:
  vllm_port: {a.vllm_port}
  hf_cache_dir: {a.hf_cache_dir or lay.state / 'hf-cache'}
  compile_cache_dir: {lay.cache}/compile
  mods_dir: {lay.state}/mods
  ssh_key: {lay.ssh_dir}/id_ed25519
  ssh_known_hosts: {lay.ssh_dir}/known_hosts

# What the web GUI / CLI may do to this machine is decided by {lay.etc}/remote-policy.json,
# which only root can change:   sudo tsm remote enable terminal reboot ...
remote_mgmt:
  policy_path: {lay.etc}/remote-policy.json
  require_root_owned: {"false" if lay.sandbox else "true"}
  record_dir: {lay.state}/terminal
  terminal_port: {a.terminal_port}
"""


def _rw_paths(a: Answers, lay: Layout, controller: bool) -> str:
    paths = [lay.state, lay.secrets]
    if not controller:
        paths += [lay.cache, a.hf_cache_dir or lay.state / "hf-cache"]
    return " ".join(str(p) for p in dict.fromkeys(paths))


def render_units(a: Answers, lay: Layout) -> dict[str, str]:
    """systemd units for this node, pinned to the service user and the paths in the configs."""
    tsm = lay.tsm_bin
    policy_args = f"--remote-policy {lay.etc}/remote-policy.json" + (" --unsafe-policy-owner" if lay.sandbox else "")
    privd = f"""[Unit]
Description=TwinSpark privileged helper (drop page cache, headless mode, node power)
After=local-fs.target

[Service]
Type=simple
ExecStart={tsm} serve privd --socket {lay.run}/privd.sock --group {a.group} {policy_args}
Restart=on-failure
User=root
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""
    agent = f"""[Unit]
Description=TwinSpark agent (typed container/runtime actions)
After=network-online.target docker.service twinspark-privd.service
Wants=network-online.target docker.service

[Service]
User={a.service_user}
SupplementaryGroups={"docker " if a.docker_group else ""}{a.group}
ExecStart={tsm} serve agent --config {lay.agent_yaml}
Restart=on-failure
RestartSec=3
# lightweight, and never the OOM victim before vLLM
MemoryMax=512M
OOMScoreAdjust=-500
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths={_rw_paths(a, lay, controller=False)}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""
    controller = f"""[Unit]
Description=TwinSpark controller + stable gateway
After=network-online.target twinspark-agent.service
Wants=network-online.target twinspark-agent.service

[Service]
User={a.service_user}
SupplementaryGroups={a.group}
ExecStart={tsm} serve controller --config {lay.controller_yaml}
Restart=on-failure
RestartSec=3
MemoryMax=768M
OOMScoreAdjust=-500
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths={_rw_paths(a, lay, controller=True)}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""
    units = {"twinspark-privd.service": privd, "twinspark-agent.service": agent}
    if a.role in ("controller", "single"):
        units["twinspark-controller.service"] = controller
    if a.remote.get("terminal"):
        units["twinspark-terminal.service"] = render_terminal_unit(a, lay)
    return units


def render_terminal_unit(a: Answers, lay: Layout) -> str:
    """The terminal service. It is deliberately NOT sandboxed like the agent: a troubleshooting shell
    needs sudo, docker and a writable filesystem, exactly as over SSH. It is a separate unit so the
    agent keeps its restrictions, and it only does anything while the root-owned policy allows it."""
    return f"""[Unit]
Description=TwinSpark terminal (opt-in browser/CLI shell on this node, recorded)
After=network-online.target twinspark-agent.service
Wants=network-online.target

[Service]
User={a.service_user}
ExecStart={lay.tsm_bin} serve termd --config {lay.agent_yaml}
Restart=on-failure
RestartSec=3
# stopping the unit ends every shell it started
KillMode=control-group
TasksMax=1024

[Install]
WantedBy=multi-user.target
"""


def installed_terminal_unit(lay: Layout) -> str:
    """The terminal unit for a node that is already set up (same user as its agent unit)."""
    from types import SimpleNamespace
    agent_unit = lay.systemd / "twinspark-agent.service"
    try:
        m = re.search(r"(?m)^User=(\S+)\s*$", agent_unit.read_text())
    except OSError:
        m = None
    if not m:
        raise SetupError(f"cannot tell which user runs the agent ({agent_unit} not found) — run "
                         f"`sudo tsm setup` on this node first")
    return render_terminal_unit(SimpleNamespace(service_user=m.group(1)), lay)       # type: ignore[arg-type]


# ---- editing generated files in place ----------------------------------------------------------
def set_runtime_mode(path: Path, mode: str) -> bool:
    """Flip ``runtime_mode`` keeping comments. Returns True if the file changed."""
    if mode not in ("dry-run", "docker"):
        raise SetupError("mode must be dry-run or docker")
    text = path.read_text()
    new, n = re.subn(r"(?m)^runtime_mode:\s*[\w-]+", f"runtime_mode: {mode}", text)
    if n == 0:
        new = text.rstrip("\n") + f"\nruntime_mode: {mode}\n"
    if new == text:
        return False
    path.write_text(new)
    return True


def set_node_fields(path: Path, node: str, hcas: list[str], gid: Optional[int]) -> bool:
    """Rewrite ``nodes.<node>.rdma_hcas`` / ``ib_gid_index`` in a wizard-generated controller.yaml."""
    lines = path.read_text().splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.fullmatch(rf"  {node}:\s*", ln.rstrip("\n"))), None)
    if start is None:
        raise SetupError(f"node {node} not found in {path} — edit rdma_hcas / ib_gid_index by hand")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and not lines[i].startswith("    ")), len(lines))
    changed = False
    for key, val in (("rdma_hcas", _hcas(hcas)), ("ib_gid_index", "null" if gid is None else str(gid))):
        idx = next((i for i in range(start + 1, end) if lines[i].lstrip().startswith(f"{key}:")), None)
        new = f"    {key}: {val}\n"
        if idx is None:
            lines.insert(end, new)
            end += 1
            changed = True
        elif lines[idx].split("#")[0].rstrip() != new.rstrip():
            comment = ("   #" + lines[idx].split("#", 1)[1].rstrip("\n")) if "#" in lines[idx] else ""
            lines[idx] = new.rstrip("\n") + comment + "\n"
            changed = True
    if changed:
        path.write_text("".join(lines))
    return changed


# ---- applying ----------------------------------------------------------------------------------
Runner = Callable[[list[str]], "tuple[int, str]"]


def _real_run(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


@dataclass
class Step:
    name: str
    status: str            # done | kept | skipped | failed | planned
    detail: str = ""


class Provisioner:
    """Applies :class:`Answers`. ``run`` executes system commands (stubbed in tests)."""

    def __init__(self, a: Answers, lay: Layout, run: Runner = _real_run, dry: bool = False,
                 force: bool = False, systemd: bool = True, home_override: Optional[str] = None):
        self.a, self.lay, self.run, self.dry, self.force = a, lay, run, dry, force
        self.home_override = home_override
        self.pubkey: Optional[str] = None
        self.systemd = systemd and not lay.sandbox
        self.steps: list[Step] = []
        self.vault = SecretsVault(lay.secrets)

    def _step(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append(Step(name, status, detail))

    # -- helpers
    def _write(self, path: Path, text: str, mode: int = 0o644, label: str = "") -> None:
        label = label or path.name
        if path.exists():
            if path.read_text() == text:
                self._step(label, "kept", "unchanged")
                return
            if not self.force and path.suffix in (".yaml",):
                self._step(label, "kept", f"{path} exists (use --force to regenerate; a backup is kept)")
                return
            if not self.dry:
                shutil.copy2(path, path.with_name(path.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"))
        if self.dry:
            self._step(label, "planned", str(path))
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.chmod(path, mode)
        self._step(label, "done", str(path))

    def _owner(self) -> Optional[tuple[int, int]]:
        try:
            import grp
            import pwd
            u = pwd.getpwnam(self.a.service_user)
            return u.pw_uid, grp.getgrnam(self.a.group).gr_gid if self._group_exists() else u.pw_gid
        except (ImportError, KeyError):
            return None

    def _group_exists(self) -> bool:
        try:
            import grp
            grp.getgrnam(self.a.group)
            return True
        except (ImportError, KeyError):
            return False

    def _chown(self, path: Path, uid: int, gid: int) -> None:
        for p in [path, *path.rglob("*")]:
            try:
                os.chown(p, uid, gid, follow_symlinks=False)
            except OSError:
                pass

    # -- steps
    def users(self) -> None:
        a = self.a
        if self.lay.sandbox:
            self._step("service user", "skipped", f"sandbox install — would run as {a.service_user}")
            return
        import grp
        import pwd
        try:
            pwd.getpwnam(a.service_user)
            self._step("service user", "kept", a.service_user)
        except KeyError:
            if not a.create_user:
                raise SetupError(
                    f"user {a.service_user!r} does not exist (use --create-user or --service-user)") from None
            if self.dry:
                self._step("service user", "planned", f"create {a.service_user}")
            else:
                rc, out = self.run(["useradd", "--system", "--create-home", "--home-dir", str(self.lay.state),
                                    "--shell", "/usr/sbin/nologin", a.service_user])
                if rc != 0:
                    raise SetupError(f"could not create user {a.service_user}: {out}") from None
                self._step("service user", "done", f"created {a.service_user}")
        try:
            grp.getgrnam(a.group)
        except KeyError:
            if not self.dry:
                self.run(["groupadd", "--system", a.group])
            self._step("group", "planned" if self.dry else "done", a.group)
        if not self.dry:
            self.run(["usermod", "-aG", a.group, a.service_user])
        try:
            grp.getgrnam("docker")
            if not self.dry:
                self.run(["usermod", "-aG", "docker", a.service_user])
            self._step("docker group", "planned" if self.dry else "done", f"{a.service_user} in docker")
        except KeyError:
            self._step("docker group", "skipped", "no docker group on this machine")

    def directories(self) -> None:
        lay, a = self.lay, self.a
        hf = Path(a.hf_cache_dir or lay.state / "hf-cache")
        dirs = [lay.etc, lay.state, lay.cache, lay.state / "mods", lay.ssh_dir, lay.cache / "compile", hf]
        if self.dry:
            self._step("directories", "planned", ", ".join(str(d) for d in dirs))
            return
        hf_existed = hf.exists()
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        lay.secrets.mkdir(parents=True, exist_ok=True)
        os.chmod(lay.etc, 0o755)            # root-owned: the maintenance policy requires it
        os.chmod(lay.ssh_dir, 0o700)
        os.chmod(lay.secrets, 0o700)
        own = self._owner()
        if own and os.geteuid() == 0:
            targets = [lay.state, lay.cache, lay.secrets]
            if not hf_existed:               # never re-own an existing (possibly huge) model cache
                targets.append(hf)
            for d in targets:
                self._chown(d, *own)
        self._step("directories", "done", f"{lay.etc}  {lay.state}  {lay.cache}")

    def secrets(self) -> None:
        a, v = self.a, self.vault
        if self.dry:
            self._step("secrets", "planned", str(self.lay.secrets))
            return
        if a.role == "agent":
            v.set("agent_token", a.agent_token or "")
            v.set("backend_api_key", a.backend_api_key or "")
            self._step("secrets", "done", "agent token + backend key from the join code")
        else:
            created = [s for s in ("agent_token", "management_api_key", "inference_api_key", "backend_api_key")
                       if v.ensure(s)[1]]
            self._step("secrets", "done" if created else "kept",
                       f"created: {', '.join(created)}" if created else "already present")
        if a.hf_token:
            v.set("hf_token", a.hf_token)
            self._step("hf token", "done", "stored in the vault")
        own = self._owner()
        if own and os.geteuid() == 0 and not self.dry:
            self._chown(self.lay.secrets, *own)

    def _ensure_key(self) -> Optional[str]:
        """Create this node's dedicated sync key if missing; return its public half."""
        lay, a = self.lay, self.a
        key = lay.ssh_dir / "id_ed25519"
        if key.exists():
            self._step("ssh key", "kept", str(key))
        elif self.dry:
            self._step("ssh key", "planned", f"generate {key}")
        else:
            generate_ssh_key(key, f"twinspark-sync@{a.hostname or a.node_id}")
            own = self._owner()
            if own and os.geteuid() == 0:
                self._chown(lay.ssh_dir, *own)
            self._step("ssh key", "done", f"generated {key}")
        pub = Path(str(key) + ".pub")
        return pub.read_text().strip() if pub.exists() else None

    def _authorize(self, pubkey: str, from_ip: Optional[str]) -> None:
        """Append a restricted authorized_keys entry for the service user (idempotent)."""
        home = self._home()
        if self.dry or home is None:
            self._step("authorize key", "planned" if self.dry else "skipped",
                       "authorise the other node's sync key" if self.dry else "no home directory for the user")
            return
        pubkey = pubkey.strip()
        if not _PUBKEY_RE.fullmatch(pubkey):
            raise SetupError("the key from node A is not a single-line OpenSSH public key")
        ak = Path(home, ".ssh", "authorized_keys")
        opts = "no-agent-forwarding,no-port-forwarding,no-X11-forwarding"
        line = f'from="{from_ip}",{opts} {pubkey}' if from_ip else f"{opts} {pubkey}"
        ak.parent.mkdir(parents=True, exist_ok=True)
        existing = ak.read_text() if ak.exists() else ""
        blob = pubkey.split()[1]
        if any(blob == tok for ln in existing.splitlines() for tok in ln.split()):
            self._step("authorize key", "kept", f"the other node's key is already authorised for {self.a.service_user}")
            return
        ak.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + line + "\n")
        os.chmod(ak, 0o600)
        os.chmod(ak.parent, 0o700)
        own = self._owner()
        if own and os.geteuid() == 0:
            self._chown(ak.parent, *own)
        self._step("authorize key", "done",
                   f"{self.a.service_user} accepts the other node's sync key"
                   + (f" (only from {from_ip})" if from_ip else ""))

    def ssh_key(self) -> Optional[str]:
        """Every node gets its own sync key; node B also trusts the key from node A's join code."""
        if self.a.role == "single":
            return None
        pub = self._ensure_key()
        if self.a.role == "agent" and self.a.authorize_key:
            self._authorize(self.a.authorize_key, self.a.peer_ip)
        elif self.a.role == "agent":
            self._step("authorize key", "skipped", "no key in the join code — copy node A's key by hand")
        return pub

    def _home(self) -> Optional[str]:
        """The service user's real home — never used for a sandbox install (``--root``)."""
        if self.home_override is not None:
            return self.home_override
        if self.lay.sandbox:
            return None
        try:
            import pwd
            return pwd.getpwnam(self.a.service_user).pw_dir
        except (ImportError, KeyError):
            return None

    def configs(self) -> None:
        a, lay = self.a, self.lay
        if a.role in ("controller", "single"):
            self._write(lay.controller_yaml, render_controller_yaml(a, lay), label="controller.yaml")
        self._write(lay.agent_yaml, render_agent_yaml(a, lay), label="agent.yaml")
        if not self.dry:     # prove the files we just wrote load with the real schema
            try:
                if a.role in ("controller", "single"):
                    load_config(lay.controller_yaml, ControllerConfig)
                load_config(lay.agent_yaml, AgentConfig)
            except Exception as exc:  # noqa: BLE001
                raise SetupError(f"the written configuration does not validate: {exc}") from exc

    def remote_policy(self) -> None:
        """Write the remote-management switches the operator chose (root-owned, merged with any existing)."""
        want = {k: bool(v) for k, v in self.a.remote.items()}
        path = self.lay.etc / "remote-policy.json"
        if not want:
            self._step("remote management", "kept" if path.exists() else "skipped",
                       "policy unchanged" if path.exists() else
                       "everything off (enable later with: sudo tsm remote enable …)")
            return
        if self.dry:
            self._step("remote management", "planned", ", ".join(k for k, v in want.items() if v) or "all off")
            return
        pol = write_policy(path, want, chown_root=not self.lay.sandbox)
        self._step("remote management", "done",
                   "enabled: " + (", ".join(pol.enabled) or "nothing") + f"  ({path})")

    def units(self) -> list[str]:
        written = []
        for name, text in render_units(self.a, self.lay).items():
            self._write(self.lay.systemd / name, text, label=name)
            written.append(name)
        return written

    def start(self, unit_names: list[str]) -> None:
        if not self.systemd:
            self._step("services", "skipped", "sandbox install or systemd not available — start them manually")
            return
        if self.dry:
            self._step("services", "planned", "systemctl enable --now " + " ".join(unit_names))
            return
        self.run(["systemctl", "daemon-reload"])
        failed = []
        for u in unit_names:
            rc, out = self.run(["systemctl", "enable", "--now", u])
            if rc == 0:
                self.run(["systemctl", "restart", u])      # pick up changed config
            else:
                failed.append(f"{u}: {out[-200:]}")
        self._step("services", "failed" if failed else "done",
                   "; ".join(failed) if failed else "enabled and started: " + ", ".join(unit_names))

    def apply(self) -> list[Step]:
        self.a.validate()
        self.users()
        self.directories()
        self.secrets()
        pub = self.ssh_key()
        self.configs()
        self.remote_policy()
        names = self.units()
        self.start(names)
        self.pubkey = pub
        return self.steps


def generate_ssh_key(path: Path, comment: str) -> None:
    """Write an unencrypted ed25519 keypair (OpenSSH format) without needing ssh-keygen."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path.parent.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, ser.NoEncryption())
    pub = priv.public_key().public_bytes(ser.Encoding.OpenSSH, ser.PublicFormat.OpenSSH)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    Path(str(path) + ".pub").write_text(pub.decode() + f" {comment}\n")
    os.chmod(str(path) + ".pub", 0o644)


def shell_hint(argv: list[str]) -> str:
    return shlex.join(argv)
