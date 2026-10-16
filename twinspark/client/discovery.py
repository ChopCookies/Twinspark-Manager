"""What is on node A, read over SSH: installed version, services, config, the API keys.

Each function is one remote run (``run`` is injectable; it defaults to :func:`ssh.run_remote`). The
shell parts are small POSIX ``sh -c`` scripts that print ``key=value`` lines; all interpretation
happens here in Python, where it can be tested. Nothing on node A is changed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import yaml

from .. import __version__ as CLIENT_VERSION
from .connections import Connection, check_host
from .ssh import RemoteResult, run_remote

Run = Callable[..., Awaitable[RemoteResult]]

UNITS = ("twinspark-controller", "twinspark-agent", "twinspark-privd", "twinspark-terminal")
INSTALL_COMMAND = ("git clone https://github.com/ChopCookies/Twinspark-Manager && cd Twinspark-Manager && "
                   "sudo ./install.sh")
UPDATE_COMMAND = ("cd Twinspark-Manager && git pull && sudo ./install.sh --no-setup && "
                  "sudo systemctl restart twinspark-agent twinspark-controller")


class DiscoveryError(Exception):
    """Node A answered, but not with what the client needs. ``reason`` is a short code, the message a
    sentence the UI can show (it never contains a key)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class CredentialAccessError(DiscoveryError):
    """The keys could not be read from node A. ``reason``: permission | not-installed | missing | other."""


# Finds tsm the way an interactive login would, plus the installer's own locations (non-interactive
# SSH sessions often have a shorter PATH). No backslash pairs: fish would read them inside quotes.
TSM_CANDIDATES = ("/usr/local/bin/tsm", "/opt/twinspark/venv/bin/tsm", "/opt/twinspark/bin/tsm")
_FIND_TSM = """t=""
for c in "$(command -v tsm 2>/dev/null)" """ + " ".join(TSM_CANDIDATES) + """ "$HOME/.local/bin/tsm"; do
  if [ -n "$c" ] && [ -x "$c" ] && [ ! -d "$c" ]; then t="$c"; break; fi
done
"""

PROBE_SCRIPT = """kv() { printf '%s=%s\\n' "$1" "$2"; }
T=""
if command -v timeout >/dev/null 2>&1; then T="timeout 10"; fi
kv probe 1
kv hostname "$(hostname 2>/dev/null || uname -n 2>/dev/null)"
kv user "$(id -un 2>/dev/null)"
kv groups "$(id -Gn 2>/dev/null)"
kv kernel "$(uname -r 2>/dev/null)"
kv arch "$(uname -m 2>/dev/null)"
if [ -r /etc/os-release ]; then
  ( . /etc/os-release; kv os_id "${ID:-}"; kv os_version "${VERSION_ID:-}"; kv os_name "${PRETTY_NAME:-}" )
fi
""" + _FIND_TSM + """kv tsm_path "$t"
if [ -n "$t" ]; then kv tsm_version "$($T "$t" --version 2>/dev/null | head -n 1)"; fi
if command -v systemctl >/dev/null 2>&1; then
  kv systemctl yes
  for u in """ + " ".join(UNITS) + """; do
    kv "unit.$u.active" "$(systemctl is-active "$u" 2>/dev/null | head -n 1)"
    kv "unit.$u.enabled" "$(systemctl is-enabled "$u" 2>/dev/null | head -n 1)"
  done
else
  kv systemctl no
fi
for f in controller agent; do
  if [ -e "/etc/twinspark/$f.yaml" ]; then kv "config.$f" yes; else kv "config.$f" no; fi
done
if [ -r /etc/twinspark/agent.yaml ]; then
  kv config.agent.readable yes
  kv runtime_mode_line "$(grep -m 1 -E '^runtime_mode:' /etc/twinspark/agent.yaml 2>/dev/null)"
elif [ -e /etc/twinspark/agent.yaml ]; then
  kv config.agent.readable no
fi
if command -v nvidia-smi >/dev/null 2>&1; then
  kv nvidia_smi yes
  kv nvidia_driver "$($T nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n 1)"
else
  kv nvidia_smi no
fi
if command -v docker >/dev/null 2>&1; then
  kv docker yes
  d="$($T docker version --format '{{.Server.Version}}' 2>&1)"
  if [ $? -eq 0 ]; then
    kv docker_version "$(printf '%s' "$d" | head -n 1)"
  else
    e="$(printf '%s' "$d" | grep -i -m 1 -E 'denied|connect|daemon|error')"
    kv docker_error "${e:-$(printf '%s' "$d" | tail -n 1)}"
  fi
else
  kv docker no
fi
if command -v tailscale >/dev/null 2>&1; then kv tailscale_ip "$($T tailscale ip -4 2>/dev/null | head -n 1)"; fi
kv done 1
exit 0
"""

SHOW_KEYS_SCRIPT = _FIND_TSM + """if [ -z "$t" ]; then echo tsm-not-found >&2; exit 127; fi
if [ "$1" = sudo ]; then
  command -v sudo >/dev/null 2>&1 || { echo sudo-not-found >&2; exit 126; }
  exec sudo -n "$t" init --show
fi
exec "$t" init --show
"""

_KV_KEY = re.compile(r"^[a-z0-9_.-]{1,64}\Z")


def parse_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, sep, val = line.partition("=")
        if sep and _KV_KEY.match(key):
            out[key] = val.strip()
    return out


def _clean_yaml_scalar(line: str) -> Optional[str]:
    """``runtime_mode: dry-run   # comment`` -> ``dry-run``."""
    _, _, val = (line or "").partition(":")
    val = re.sub(r"\s+#.*$", "", val).strip().strip("'\"").strip()
    return val or None


def _docker_problem(text: str) -> str:
    t = (text or "").lower()
    if "permission denied" in t:
        return "permission"
    if "cannot connect" in t or "is the docker daemon running" in t or "no such file" in t:
        return "not-running"
    return "other"


def facts_from_kv(kv: dict[str, str]) -> dict[str, Any]:
    """Structure the probe's ``key=value`` lines (see :func:`probe_node` for the result's shape)."""
    def val(key: str) -> Optional[str]:
        v = kv.get(key)
        return v if v else None

    tsm_version = val("tsm_version")
    if tsm_version and tsm_version.lower().startswith("tsm "):
        tsm_version = tsm_version[4:].strip()
    services: dict[str, dict[str, Optional[str]]] = {}
    has_systemctl = kv.get("systemctl") == "yes"
    for u in UNITS:
        services[u] = {"active": (val(f"unit.{u}.active") or "unknown") if has_systemctl else None,
                       "enabled": (val(f"unit.{u}.enabled") or "not-found") if has_systemctl else None}
    configs = {"controller": kv.get("config.controller") == "yes", "agent": kv.get("config.agent") == "yes"}
    mode = _clean_yaml_scalar(kv.get("runtime_mode_line", "")) if kv.get("runtime_mode_line") else None
    if configs["agent"] and mode is None and kv.get("config.agent.readable") != "no":
        mode = "dry-run"          # AgentConfig's default when the key is absent
    docker: dict[str, Any] = {"installed": kv.get("docker") == "yes", "version": val("docker_version"),
                              "error": None, "error_text": None}
    if docker["installed"] and not docker["version"]:
        docker["error"] = _docker_problem(kv.get("docker_error", ""))
        docker["error_text"] = (kv.get("docker_error") or "")[:200] or None
    elif not docker["installed"]:
        docker["error"] = "missing"
    role = "controller" if configs["controller"] else "agent" if configs["agent"] else "not-installed"
    return {
        "reachable": True,
        "complete": kv.get("done") == "1",
        "hostname": val("hostname"),
        "user": val("user"),
        "groups": (kv.get("groups") or "").split(),
        "os": {"id": val("os_id"), "version": val("os_version"), "name": val("os_name")},
        "kernel": val("kernel"),
        "arch": val("arch"),
        "tsm": {"installed": bool(val("tsm_path")), "path": val("tsm_path"), "version": tsm_version},
        "systemctl": has_systemctl,
        "services": services,
        "configs": configs,
        "agent_config_readable": None if not configs["agent"] else kv.get("config.agent.readable") != "no",
        "runtime_mode": mode,
        "nvidia": {"smi": kv.get("nvidia_smi") == "yes", "driver": val("nvidia_driver")},
        "docker": docker,
        "tailscale_ip": val("tailscale_ip"),
        "role": role,
    }


async def probe_node(conn: Connection, *, home: Path, run: Run = run_remote, timeout: float = 30.0) -> dict[str, Any]:
    """One SSH run that reports what is on the node.

    Result: ``reachable``, ``hostname``, ``user``, ``groups``, ``os`` {id, version, name}, ``kernel``,
    ``arch``, ``tsm`` {installed, path, version}, ``systemctl`` (bool), ``services`` {unit: {active,
    enabled}} (None values without systemctl), ``configs`` {controller, agent}, ``runtime_mode``
    (``dry-run`` | ``docker`` | None), ``nvidia`` {smi, driver}, ``docker`` {installed, version, error:
    None | permission | not-running | missing | other}, ``tailscale_ip``, ``role`` (``controller`` when
    /etc/twinspark/controller.yaml exists, ``agent`` with only agent.yaml, else ``not-installed``).
    SSH failures raise :class:`ssh.SSHError`.
    """
    res = await run(conn, ["sh", "-c", PROBE_SCRIPT, "tsm-probe"], home=home, timeout=timeout)
    facts = facts_from_kv(parse_kv(res.stdout))
    facts["probe_rc"] = res.rc
    return facts


# ---- controller.yaml --------------------------------------------------------------------------
def forward_host(bind: Any) -> str:
    """Where a forward on node A must connect to reach a listener bound to ``bind``."""
    b = str(bind or "").strip().strip("[]")
    if b in ("", "0.0.0.0", "::", "*", "localhost") or b.startswith("127."):
        return "127.0.0.1"
    try:
        return check_host(b)
    except ValueError:
        return "127.0.0.1"


def _port(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise DiscoveryError("invalid", f"controller.yaml has an invalid port number: {value!r}")
    return value


def _opt_str(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


def parse_controller_yaml(text: str) -> dict[str, Any]:
    """The parts of node A's controller.yaml the client needs.

    ``nodes`` {node_id: {qsfp_ip, agent_url, ssh_user}}, ``manager_port`` / ``manager_bind`` /
    ``manager_tls`` / ``manager_forward_host`` (from ``listener``), the same four for ``gateway_*``
    (from ``gateway_listener``), ``management_auth`` and ``hostname``.
    """
    try:
        doc = yaml.safe_load(text or "") or {}
    except yaml.YAMLError as exc:
        raise DiscoveryError("invalid", f"controller.yaml on node A is not valid YAML ({type(exc).__name__}); "
                                        "fix it there, then try again.") from None
    if not isinstance(doc, dict):
        raise DiscoveryError("invalid", "controller.yaml on node A is not a YAML mapping; fix it there.")
    nodes: dict[str, dict[str, Optional[str]]] = {}
    raw_nodes = doc.get("nodes") if isinstance(doc.get("nodes"), dict) else {}
    for nid, n in raw_nodes.items():
        if isinstance(n, dict):
            nodes[str(nid)] = {"qsfp_ip": _opt_str(n.get("qsfp_ip")), "agent_url": _opt_str(n.get("agent_url")),
                               "ssh_user": _opt_str(n.get("ssh_user"))}
    lst = doc.get("listener") if isinstance(doc.get("listener"), dict) else {}
    gw = doc.get("gateway_listener") if isinstance(doc.get("gateway_listener"), dict) else {}
    node = doc.get("node") if isinstance(doc.get("node"), dict) else {}
    m_bind, g_bind = str(lst.get("bind") or "127.0.0.1"), str(gw.get("bind") or "0.0.0.0")
    return {
        "nodes": nodes,
        "hostname": _opt_str(node.get("hostname")),
        "manager_port": _port(lst.get("port"), 8443),
        "manager_bind": m_bind,
        "manager_tls": bool(lst.get("tls_cert")),
        "manager_forward_host": forward_host(m_bind),
        "gateway_port": _port(gw.get("port"), 8000),
        "gateway_bind": g_bind,
        "gateway_tls": bool(gw.get("tls_cert")),
        "gateway_forward_host": forward_host(g_bind),
        "management_auth": str(doc.get("management_auth") or "apikey"),
        # where the client browses community recipes (the node's configured list; default as on the node)
        "recipe_sources": [str(x) for x in doc.get("recipe_sources") or [] if isinstance(x, str)][:20]
        or ["eugr/spark-vllm-docker:recipes"],
    }


async def read_controller_nodes(conn: Connection, *, home: Path, run: Run = run_remote,
                                timeout: float = 20.0) -> dict[str, Any]:
    """Read ``/etc/twinspark/controller.yaml`` from node A (see :func:`parse_controller_yaml`)."""
    res = await run(conn, ["cat", "/etc/twinspark/controller.yaml"], home=home, timeout=timeout)
    if res.rc != 0:
        err = res.stderr or ""
        if "No such file" in err:
            raise DiscoveryError("missing", f"{conn.host} has no /etc/twinspark/controller.yaml, so it is not set up "
                                            "as node A (the controller). Enter the address of the Spark that runs "
                                            "the controller, or run `sudo tsm setup` there.")
        if "Permission denied" in err:
            raise DiscoveryError("permission", f"The SSH user {conn.ssh_user} cannot read "
                                               "/etc/twinspark/controller.yaml on node A. Connect as the user that "
                                               "installed TwinSpark, or run on node A: "
                                               "sudo chmod 644 /etc/twinspark/controller.yaml")
        raise DiscoveryError("other", f"Could not read /etc/twinspark/controller.yaml on {conn.host} "
                                      f"(exit code {res.rc}).")
    return parse_controller_yaml(res.stdout)


# ---- API keys ---------------------------------------------------------------------------------
_SLOT_LINE = re.compile(r"^([a-z_]{1,40})\s+(\S.*?)\s*$")
_KEY_VALUE = re.compile(r"^[\x21-\x7e]{8,512}\Z")
_PERMISSION_HINTS = ("permission denied", "permissionerror", "errno 13", "belongs to another user")
_SUDO_HINTS = ("a password is required", "a terminal is required", "not in the sudoers", "may not run sudo",
               "is not allowed to run sudo")


def parse_init_show(text: str) -> dict[str, Optional[str]]:
    """``tsm init --show`` output (``<slot><spaces><value>``) -> {slot: value or None}."""
    out: dict[str, Optional[str]] = {}
    for line in (text or "").splitlines():
        m = _SLOT_LINE.match(line)
        if not m:
            continue
        val = m.group(2)
        out[m.group(1)] = val if val != "(not on this node)" and _KEY_VALUE.match(val) else None
    return out


async def fetch_credentials(conn: Connection, *, home: Path, run: Run = run_remote, sudo: bool = False,
                            timeout: float = 30.0) -> dict[str, Optional[str]]:
    """Read the management and inference keys with ``tsm init --show`` on node A.

    Returns ``{"management_key": …, "inference_key": …}`` (a key node A does not have is None).
    ``sudo=True`` runs ``sudo -n tsm init --show`` instead: only useful when the user has passwordless
    sudo; the UI should offer it explicitly, never try it on its own. Raises
    :class:`CredentialAccessError`; its message never contains a key, nothing here logs output.
    """
    res = await run(conn, ["sh", "-c", SHOW_KEYS_SCRIPT, "tsm-show-keys", "sudo" if sudo else "plain"],
                    home=home, timeout=timeout)
    err = (res.stderr or "").lower()            # only stderr is inspected: stdout may hold keys
    if res.rc != 0:
        if "tsm-not-found" in err or (res.rc == 127 and not sudo):
            raise CredentialAccessError("not-installed", f"TwinSpark (tsm) is not installed on {conn.host}. "
                                                         f"Install it there first: {INSTALL_COMMAND}")
        if sudo and ("sudo-not-found" in err or any(h in err for h in _SUDO_HINTS)):
            raise CredentialAccessError("permission", f"{conn.ssh_user} cannot run tsm with sudo without a password "
                                                      "on node A. Paste the keys instead: `sudo tsm init --show` "
                                                      "on node A prints them.")
        if any(h in err for h in _PERMISSION_HINTS):
            raise CredentialAccessError("permission", f"The SSH user {conn.ssh_user} cannot read the TwinSpark vault "
                                                      "on node A. Either connect as the user that installed "
                                                      "TwinSpark, or paste the keys (printed by "
                                                      "`sudo tsm init --show` on node A).")
        if "no secrets in" in err:
            raise CredentialAccessError("missing", f"{conn.host} has no TwinSpark secrets: it is not set up as node A. "
                                                   "Run `sudo tsm setup` there, or connect to the Spark that runs "
                                                   "the controller.")
        raise CredentialAccessError("other", f"`tsm init --show` failed on {conn.host} (exit code {res.rc}). Paste "
                                             "the keys instead: `sudo tsm init --show` on node A prints them.")
    slots = parse_init_show(res.stdout)
    keys = {"management_key": slots.get("management_api_key"), "inference_key": slots.get("inference_api_key")}
    if not any(keys.values()):
        raise CredentialAccessError("missing", f"{conn.host} printed no management or inference key, so it is probably "
                                               "node B. Connect to node A (the Spark that runs the controller).")
    return keys


# ---- the client's setup checklist ---------------------------------------------------------------
def _check(cid: str, title: str, status: str, detail: str, *, command: str = "", href: str = "",
           label: str = "") -> dict[str, Any]:
    return {"id": cid, "title": title, "status": status, "detail": detail,
            "fix": {"href": href, "command": command, "label": label} if (href or command) else None}


def _minor(version: Optional[str]) -> Optional[tuple[int, int]]:
    m = re.match(r"^\s*v?(\d+)\.(\d+)", version or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def setup_checks(facts_a: Optional[dict[str, Any]], nodes: Optional[dict[str, Any]], creds_ok: Optional[bool],
                 *, client_version: str = CLIENT_VERSION) -> list[dict[str, Any]]:
    """The client's "Get connected" checklist: ``[{id, title, status, detail, fix}]``.

    ``facts_a``: :func:`probe_node` of node A, or ``{"error": message}`` / None when SSH failed.
    ``nodes``: :func:`read_controller_nodes`, or None when it could not be read. ``creds_ok``: True when
    the keys are stored on this computer, False when reading them failed, None when not tried yet.
    ``status`` is ok | warn | todo | blocked; ``fix`` is None or {href, command, label} like the
    controller's onboarding checklist.
    """
    checks: list[dict[str, Any]] = []
    f = facts_a or {}
    if not facts_a or f.get("error") or not f.get("reachable"):
        checks.append(_check("ssh", "Node A is reachable over SSH", "blocked",
                             str(f.get("error") or "not connected yet")[:300]))
        later = "checked once SSH works"
        for cid, title in (("installed", "TwinSpark is installed"), ("role", "Node A runs the controller"),
                           ("live", "Real containers are enabled"), ("nvidia", "NVIDIA driver"),
                           ("docker", "Docker"), ("peer", "Node B is configured"),
                           ("credentials", "API keys are on this computer")):
            checks.append(_check(cid, title, "todo", later))
        return checks

    os_name = (f.get("os") or {}).get("name") or (f.get("os") or {}).get("id") or "unknown OS"
    who = f"{f.get('user') or '?'}@{f.get('hostname') or '?'}"
    checks.append(_check("ssh", "Node A is reachable over SSH", "ok",
                         f"{who} ({os_name}, kernel {f.get('kernel') or '?'})"))

    tsm = f.get("tsm") or {}
    if not tsm.get("installed"):
        checks.append(_check("installed", "TwinSpark is installed", "todo", "tsm was not found on node A",
                             command=INSTALL_COMMAND, label="Install TwinSpark"))
    else:
        ver = tsm.get("version")
        if not ver:
            checks.append(_check("installed", "TwinSpark is installed", "warn",
                                 f"{tsm.get('path')} is there but `tsm --version` did not answer",
                                 command=f"{tsm.get('path')} --version   (on node A)", label="Check tsm"))
        elif _minor(ver) != _minor(client_version):
            checks.append(_check("installed", "TwinSpark is installed", "warn",
                                 f"node A runs TwinSpark {ver}, this client is {client_version} — update node A",
                                 command=UPDATE_COMMAND, label="Update node A"))
        else:
            checks.append(_check("installed", "TwinSpark is installed", "ok", f"TwinSpark {ver} ({tsm.get('path')})"))

    role = f.get("role")
    ctl = (f.get("services") or {}).get("twinspark-controller") or {}
    if role == "controller":
        active = ctl.get("active")
        if active in (None, "active"):
            checks.append(_check("role", "Node A runs the controller", "ok",
                                 "twinspark-controller is running" if active else
                                 "controller.yaml present (service state unknown: no systemctl)"))
        else:
            checks.append(_check("role", "Node A runs the controller", "warn",
                                 f"controller.yaml exists but twinspark-controller is {active}",
                                 command="sudo systemctl status twinspark-controller", label="Check the service"))
    elif role == "agent":
        checks.append(_check("role", "Node A runs the controller", "blocked",
                             "this Spark is set up as node B (agent only). Enter the address of node A, the Spark "
                             "that runs the controller and the OpenAI endpoint"))
    elif tsm.get("installed"):
        checks.append(_check("role", "Node A runs the controller", "todo", "TwinSpark is installed but not set up",
                             command="sudo tsm setup   (on node A)", label="Run setup"))
    else:
        checks.append(_check("role", "Node A runs the controller", "todo", "after TwinSpark is installed"))

    mode = f.get("runtime_mode")
    if mode == "docker":
        checks.append(_check("live", "Real containers are enabled", "ok", "docker mode"))
    elif mode == "dry-run":
        checks.append(_check("live", "Real containers are enabled", "todo",
                             "dry-run — activations are simulated",
                             command="sudo tsm go-live    (run on each node when `tsm plan` looks right)",
                             label="Go live"))
    elif f.get("agent_config_readable") is False:
        checks.append(_check("live", "Real containers are enabled", "warn",
                             "the SSH user cannot read /etc/twinspark/agent.yaml, so the mode is unknown"))
    else:
        checks.append(_check("live", "Real containers are enabled", "todo", "after setup"))

    nv = f.get("nvidia") or {}
    if nv.get("driver"):
        checks.append(_check("nvidia", "NVIDIA driver", "ok", f"driver {nv['driver']}"))
    elif nv.get("smi"):
        checks.append(_check("nvidia", "NVIDIA driver", "warn", "nvidia-smi did not report a driver version",
                             command="nvidia-smi   (on node A)", label="Check the GPU"))
    else:
        checks.append(_check("nvidia", "NVIDIA driver", "warn",
                             "nvidia-smi not found — is this a DGX Spark with the NVIDIA driver installed?"))

    dk = f.get("docker") or {}
    needed = mode == "docker"
    if dk.get("version"):
        checks.append(_check("docker", "Docker", "ok", f"Docker {dk['version']}"))
    elif dk.get("error") == "permission":
        checks.append(_check("docker", "Docker", "warn",
                             f"Docker is installed, but {f.get('user') or 'the SSH user'} is not in the docker group "
                             "(TwinSpark's service user needs it in docker mode)",
                             command=f"sudo usermod -aG docker {f.get('user') or '$USER'}   (then log in again)",
                             label="Add to docker group"))
    elif dk.get("error") == "not-running":
        checks.append(_check("docker", "Docker", "blocked" if needed else "warn", "the Docker daemon is not running",
                             command="sudo systemctl start docker", label="Start Docker"))
    elif dk.get("error") == "missing":
        checks.append(_check("docker", "Docker", "blocked" if needed else "warn",
                             "docker not found (DGX OS ships it; elsewhere install Docker Engine)"))
    else:
        checks.append(_check("docker", "Docker", "warn",
                             f"docker did not answer: {(dk.get('error_text') or 'unknown error')[:160]}",
                             command="docker version   (on node A)", label="Check Docker"))

    if nodes is None:
        checks.append(_check("peer", "Node B is configured",
                             "warn" if role == "controller" else "todo",
                             "controller.yaml could not be read" if role == "controller" else "after setup"))
    else:
        b = (nodes.get("nodes") or {}).get("B")
        if b and b.get("qsfp_ip"):
            checks.append(_check("peer", "Node B is configured", "ok",
                                 f"node B at {b['qsfp_ip']} over the QSFP link (agent {b.get('agent_url') or '?'})"))
        elif b:
            checks.append(_check("peer", "Node B is configured", "warn",
                                 "node B has no QSFP address in controller.yaml",
                                 command="tsm qsfp plan   (on each node)", label="Plan the link"))
        else:
            checks.append(_check("peer", "Node B is configured", "todo", "controller.yaml has no node B yet",
                                 command="sudo tsm join-code   (on node A, prints the command for node B)",
                                 label="Add node B"))

    if creds_ok:
        checks.append(_check("credentials", "API keys are on this computer", "ok",
                             "management and inference keys stored"))
    elif creds_ok is False:
        checks.append(_check("credentials", "API keys are on this computer", "todo",
                             "the SSH user cannot read the TwinSpark vault on node A",
                             command="sudo tsm init --show   (on node A; paste the two keys here)",
                             label="Paste the keys"))
    else:
        checks.append(_check("credentials", "API keys are on this computer", "todo", "not fetched yet"))
    return checks
