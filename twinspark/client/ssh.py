"""OpenSSH, driven from Python: host keys, remote commands, the one-time key install, tunnels.

The client uses the system ``ssh`` / ``ssh-keyscan`` (``TSM_SSH`` / ``TSM_SSH_KEYSCAN`` override them)
and never a shell on this side: every call is an argv list, the destination follows ``--``, and the
remote command is one string built with :func:`shlex.join`. :meth:`Connection.validate` runs before any
argv is built, so a host such as ``-oProxyCommand=…`` is refused before ``ssh`` ever sees it.

Host keys are trust-on-first-use with an explicit confirmation: :func:`scan_host_keys` reads them,
the UI shows the fingerprints, :func:`trust_host_keys` writes them to the client's own ``known_hosts``,
and every connection runs with ``StrictHostKeyChecking=yes`` against that file. A changed key is never
replaced silently (:func:`trust_host_keys` needs ``replace=True``).

Identity: when the connection names no key file but the client's dedicated key exists, ``-i <that key>``
is added WITHOUT ``IdentitiesOnly``. The user's agent and default keys therefore keep working (a Spark
that already accepts their usual key needs no setup), and the dedicated key is offered as well. The
cost: with many agent keys a server's ``MaxAuthTries`` can be reached first ("Too many authentication
failures", reported as an ``auth`` error); setting ``identity_file`` explicitly is the way out.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import hashlib
import hmac
import inspect
import logging
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple, Optional

from .connections import Connection, check_host, check_port
from .paths import atomic_write, ensure_private_dir

log = logging.getLogger("twinspark.client")

KEY_NAME = "twinspark_ed25519"
ASKPASS_ENV = "TSM_ASKPASS_SECRET"
_WIN = os.name == "nt"


# ---- errors -----------------------------------------------------------------------------------
class SSHError(Exception):
    """An SSH-level failure, with a ``kind`` the UI can branch on and a message it can show as-is.

    Kinds: ``missing`` (no OpenSSH client), ``auth``, ``host-key-unknown``, ``host-key-changed``,
    ``unreachable``, ``timeout``, ``port-in-use`` (a local forward port is taken), ``other``.
    ``detail`` is the tail of ssh's own stderr (never a password; token-like strings are redacted).
    """

    def __init__(self, kind: str, message: str, *, detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail


def install_hint(platform: Optional[str] = None) -> str:
    plat = platform or sys.platform
    if plat.startswith("win"):
        return ("Install the OpenSSH client: Settings > System > Optional features > Add a feature > "
                "'OpenSSH Client', or in an administrator PowerShell run "
                "Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0")
    if plat == "darwin":
        return "macOS ships ssh in /usr/bin; if it is missing, run: xcode-select --install"
    return ("Install the OpenSSH client: sudo apt install openssh-client (Debian/Ubuntu), "
            "sudo dnf install openssh-clients (Fedora/RHEL) or sudo pacman -S openssh (Arch).")


def _find(tool: str, env_var: str) -> str:
    override = os.environ.get(env_var)
    if override:
        p = Path(override).expanduser()
        if p.is_file() and (_WIN or os.access(p, os.X_OK)):
            return str(p)
        raise SSHError("missing", f"{env_var} is set to {override}, which is not an executable file. "
                                  f"Fix the path or unset {env_var}.")
    found = shutil.which(tool)
    if found:
        return found
    if _WIN:
        cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / f"{tool}.exe"
        if cand.is_file():
            return str(cand)
    raise SSHError("missing", f"The '{tool}' program (the OpenSSH client) was not found. {install_hint()}")


def find_ssh() -> str:
    return _find("ssh", "TSM_SSH")


def find_keyscan() -> str:
    return _find("ssh-keyscan", "TSM_SSH_KEYSCAN")


_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")


def _tail(text: str, lines: int = 4, limit: int = 400) -> str:
    keep = [ln.strip() for ln in (text or "").splitlines()
            if ln.strip() and not ln.lstrip().startswith("@") and "IT IS POSSIBLE" not in ln]
    return _TOKEN_RE.sub("[REDACTED]", " | ".join(keep[-lines:]))[:limit]


_CLASSES: list[tuple[str, re.Pattern[str]]] = [
    # order matters: the "changed" banner also ends with "Host key verification failed."
    ("host-key-changed", re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key for \S+ has changed",
                                    re.I)),
    ("host-key-unknown", re.compile(r"Host key verification failed|No \S+ host key is known for", re.I)),
    ("auth", re.compile(r"Permission denied|Too many authentication failures|No more authentication methods"
                        r"|Authentication failed", re.I)),
    ("port-in-use", re.compile(r"Address already in use|cannot listen to port|Could not request local forwarding",
                               re.I)),
    ("unreachable", re.compile(r"Connection refused|timed out|No route to host|Could not resolve hostname"
                               r"|Name or service not known|nodename nor servname|Temporary failure in name"
                               r"|Network is unreachable|Host is down|Connection reset|Connection closed by"
                               r"|kex_exchange_identification|getaddrinfo|closed by remote host"
                               r"|server host not responding|Broken pipe", re.I)),
]


def classify(stderr: str) -> Optional[str]:
    """The SSHError kind ssh's stderr points to, or None when nothing recognisable is in it."""
    for kind, pat in _CLASSES:
        if pat.search(stderr or ""):
            return kind
    return None


def ssh_failure(stderr: str, *, host: str, port: int = 22, user: str = "") -> SSHError:
    """Turn ssh's stderr (exit code 255) into an SSHError with a message that says what to do."""
    kind = classify(stderr) or "other"
    who = f"{user}@{host}" if user else host
    detail = _tail(stderr)
    if kind == "auth":
        msg = (f"{who} refused the login (permission denied). Install the TwinSpark client key once with the "
               "SSH password, or choose a key file that node A already accepts.")
        if re.search(r"Too many authentication failures", stderr or "", re.I):
            msg = (f"{who} closed the login after too many keys were offered (Too many authentication failures). "
                   "Choose the right key file in the connection settings so only that key is tried first.")
    elif kind == "host-key-unknown":
        msg = (f"The host key of {host} is not trusted yet. Check its fingerprint and confirm it before "
               "connecting.")
    elif kind == "host-key-changed":
        msg = (f"The host key of {host} has CHANGED since you trusted it. That is expected after reinstalling "
               "the Spark, but it can also mean someone is intercepting the connection. Compare the new "
               "fingerprint with the one on the Spark (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub) "
               "before you trust it again.")
    elif kind == "unreachable":
        msg = (f"Could not reach {host} on port {port}. Check that the Spark is switched on, that the address "
               "is right and that this computer is on the same network (or signed in to the tailnet).")
    elif kind == "port-in-use":
        msg = ("A local port of the tunnel is already used by another program on this computer. Close that "
               "program or choose other local ports in the connection settings.")
    else:
        msg = f"ssh to {who} failed" + (f": {detail}" if detail else ".")
    return SSHError(kind, msg, detail=detail)


# ---- files the client owns --------------------------------------------------------------------
def known_hosts_path(home: Path) -> Path:
    return Path(home) / "known_hosts"


def client_key_path(home: Path) -> Path:
    return Path(home) / "ssh" / KEY_NAME


def _opt_path(path: Path) -> str:
    """A path as the value of ``-o Option=…``.

    ssh splits option values at whitespace (``UserKnownHostsFile`` takes several files) and expands
    ``%`` tokens, so spaces (macOS: "Application Support") are double-quoted and ``%`` doubled. Windows
    OpenSSH reads forward slashes reliably; backslashes may be taken as escapes.
    """
    s = path.as_posix() if _WIN else str(path)
    if '"' in s:
        raise SSHError("other", f"The path {s} contains a double quote, which OpenSSH cannot read in an option. "
                                "Set TSM_CLIENT_HOME to a folder without quotes.")
    s = s.replace("%", "%%")
    return f'"{s}"' if any(c.isspace() for c in s) else s


def _identity(conn: Connection, home: Path) -> Optional[str]:
    if conn.identity_file:
        path = Path(conn.identity_file).expanduser()
        if not path.is_file():
            log.warning("key file %s for connection %s does not exist", path, conn.id)
        return str(path)
    key = client_key_path(home)
    if key.is_file():
        if "%" in str(key):        # -i is expanded by ssh: '%' would be read as a placeholder
            log.warning("not offering %s: OpenSSH cannot use a key path containing '%%'", key)
            return None
        return str(key)
    return None


def base_args(conn: Connection, home: Path, *, batch: bool = True) -> list[str]:
    """The options every ssh call of this connection uses (everything before ``--``).

    ``StrictHostKeyChecking=yes`` against the client's own known_hosts (never ``no``/``accept-new``),
    keep-alives so a dead link is noticed within ~45 s, ``ExitOnForwardFailure`` so a tunnel whose port
    cannot be bound exits instead of pretending, ``CheckHostIP=no`` (the address is checked through the
    host entry already) and ``ControlPath=none`` so a ControlMaster in the user's ssh config cannot turn
    the tunnel into a multiplexed session whose exit we would never see. Options given first win in
    OpenSSH, so callers append overrides only for options not set here.
    """
    if not conn.uses_ssh:
        raise ValueError("A direct connection does not use SSH.")
    conn.validate()
    home = Path(home)
    args = ["-p", str(conn.ssh_port), "-l", conn.ssh_user]
    if batch:
        args += ["-o", "BatchMode=yes"]
    args += ["-o", f"UserKnownHostsFile={_opt_path(known_hosts_path(home))}",
             "-o", "StrictHostKeyChecking=yes",
             "-o", "CheckHostIP=no",
             "-o", "ServerAliveInterval=15",
             "-o", "ServerAliveCountMax=3",
             "-o", "ConnectTimeout=10",
             "-o", "ExitOnForwardFailure=yes",
             "-o", "ControlPath=none"]
    ident = _identity(conn, home)
    if ident:
        args += ["-i", ident]
    return args


# ---- host keys --------------------------------------------------------------------------------
_KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa",
              "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
_PREFERENCE = {t: i for i, t in enumerate(_KEY_TYPES)}
_known_lock = threading.Lock()


def fingerprint(key_b64: str) -> str:
    """OpenSSH's SHA256 fingerprint: ``SHA256:`` + unpadded base64 of sha256(key blob)."""
    blob = base64.b64decode(key_b64, validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class HostKey:
    type: str
    key_b64: str
    fingerprint: str

    @classmethod
    def parse(cls, key_type: str, key_b64: str) -> "HostKey":
        """Validate a ``type base64`` pair: known type, strict base64, and the blob names the same type."""
        if key_type not in _KEY_TYPES:
            raise ValueError(f"unsupported host key type {key_type!r}")
        try:
            blob = base64.b64decode(key_b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("the host key is not valid base64") from None
        if len(blob) < 4:
            raise ValueError("the host key is truncated")
        (n,) = struct.unpack(">I", blob[:4])
        if blob[4:4 + n] != key_type.encode("ascii"):
            raise ValueError("the host key does not match its type")
        return cls(key_type, key_b64, fingerprint(key_b64))

    def line(self, pattern: str) -> str:
        return f"{pattern} {self.type} {self.key_b64}"


def host_pattern(host: str, port: int) -> str:
    """How OpenSSH names a host in known_hosts: ``host`` for port 22, ``[host]:port`` otherwise."""
    h = check_host(host).lower()
    return h if check_port(port) == 22 else f"[{h}]:{port}"


def _glob(pattern: str, name: str) -> bool:
    """OpenSSH host patterns: only ``*`` and ``?`` are wildcards ("[host]:port" brackets are literal)."""
    if "*" not in pattern and "?" not in pattern:
        return pattern == name
    rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, name) is not None


def _pattern_matches(field: str, name: str) -> bool:
    hit = False
    for pat in field.split(","):
        if pat.startswith("|1|"):
            try:
                _, _, salt, digest = pat.split("|", 3)
                mac = hmac.new(base64.b64decode(salt), name.encode(), hashlib.sha1).digest()
                if hmac.compare_digest(mac, base64.b64decode(digest)):
                    hit = True
            except (ValueError, binascii.Error):
                continue
        elif pat.startswith("!"):
            if _glob(pat[1:].lower(), name):
                return False
        elif _glob(pat.lower(), name):
            hit = True
    return hit


def _read_known(home: Path) -> list[str]:
    try:
        return known_hosts_path(home).read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def _entry(line: str) -> Optional[tuple[str, str, str]]:
    parts = line.split()
    if len(parts) < 3 or line.lstrip().startswith(("#", "@")):     # comments, @cert-authority, @revoked
        return None
    return parts[0], parts[1], parts[2]


def known_keys(home: Path, host: str, port: int) -> list[HostKey]:
    """The keys the client's known_hosts trusts for ``host``:``port``."""
    name = host_pattern(host, port)
    out: list[HostKey] = []
    for line in _read_known(home):
        e = _entry(line)
        if e and _pattern_matches(e[0], name):
            try:
                out.append(HostKey.parse(e[1], e[2]))
            except ValueError:
                continue
    return out


def host_key_status(home: Path, host: str, port: int, scanned: Iterable[HostKey]) -> str:
    """``trusted`` | ``unknown`` | ``changed`` for the keys the host presents now.

    ``changed`` when the host offers a key of a type the file knows with a different value, or when
    none of the offered keys is known although the host has entries (its known key type was dropped).
    A new additional key type next to a matching one stays ``trusted``.
    """
    scanned = list(scanned)
    if not scanned:
        raise ValueError("no scanned host keys to compare")
    known = {(k.type, k.key_b64) for k in known_keys(home, host, port)}
    if not known:
        return "unknown"
    known_types = {t for t, _ in known}
    matching = [k for k in scanned if (k.type, k.key_b64) in known]
    differing = [k for k in scanned if k.type in known_types and (k.type, k.key_b64) not in known]
    return "changed" if differing or not matching else "trusted"


def scan_host_keys(host: str, port: int = 22, *, timeout: float = 10.0) -> list[HostKey]:
    """Ask the host for its public host keys with ``ssh-keyscan`` (blocking; use a thread in async code).

    Returns the keys, preferred type first (ed25519, ecdsa, rsa). Raises SSHError when none could be read.
    """
    check_host(host)
    check_port(port)
    argv = [find_keyscan(), "-T", str(max(1, int(timeout))), "-p", str(port), "--", host]
    kw: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW} if _WIN else {}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 5, stdin=subprocess.DEVNULL,
                           **kw)
    except subprocess.TimeoutExpired:
        raise SSHError("timeout", f"{host} did not send its host key within {timeout:g} s. Check the address "
                                  "and that the Spark is on.") from None
    except OSError as exc:
        raise SSHError("missing", f"Could not run ssh-keyscan ({exc}). {install_hint()}") from None
    keys: dict[tuple[str, str], HostKey] = {}
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or line.startswith("#"):
            continue
        try:
            k = HostKey.parse(parts[1], parts[2])
        except ValueError:
            continue
        keys[(k.type, k.key_b64)] = k
    if not keys:
        err = ssh_failure(p.stderr, host=host, port=port)
        kind = err.kind if err.kind in ("unreachable", "timeout") else "unreachable"
        raise SSHError(kind, f"No host key could be read from {host} port {port}. Check that the Spark is "
                             "switched on, that the address is right and that SSH runs on that port.",
                       detail=err.detail)
    return sorted(keys.values(), key=lambda k: _PREFERENCE.get(k.type, 99))


def trust_host_keys(home: Path, host: str, port: int, keys: Iterable[HostKey], *, replace: bool = False) -> int:
    """Add ``keys`` for ``host``:``port`` to the client's known_hosts; return how many lines were added.

    Call it only after the user confirmed the fingerprints. When the stored key has CHANGED this refuses
    unless ``replace=True`` (the user confirmed the new key explicitly); then the old entries are removed,
    so the old key is no longer trusted.
    """
    keys = list(keys)
    if not keys:
        raise ValueError("There is no host key to trust.")
    pattern = host_pattern(host, port)
    with _known_lock:
        if host_key_status(home, host, port, keys) == "changed" and not replace:
            raise ValueError(f"The host key of {host} has changed. Confirm the new fingerprint explicitly "
                             "before trusting it (this replaces the old key).")
        lines = _read_known(home)
        if replace:
            lines = [ln for ln in lines if not ((e := _entry(ln)) and _pattern_matches(e[0], pattern))]
        present = {(e[1], e[2]) for ln in lines if (e := _entry(ln)) and _pattern_matches(e[0], pattern)}
        new = [k.line(pattern) for k in keys if (k.type, k.key_b64) not in present]
        if not new and not replace:
            return 0
        ensure_private_dir(Path(home))
        atomic_write(known_hosts_path(home), ("\n".join(lines + new) + "\n").encode("utf-8"), mode=0o600)
        return len(new)


def forget_host(home: Path, host: str, port: int) -> int:
    """Remove every known_hosts entry of ``host``:``port``; return how many lines went."""
    pattern = host_pattern(host, port)
    with _known_lock:
        lines = _read_known(home)
        keep = [ln for ln in lines if not ((e := _entry(ln)) and _pattern_matches(e[0], pattern))]
        if len(keep) != len(lines):
            atomic_write(known_hosts_path(home), ("\n".join(keep) + "\n" if keep else "").encode("utf-8"),
                         mode=0o600)
        return len(lines) - len(keep)


# ---- running ssh ------------------------------------------------------------------------------
@dataclass
class RemoteResult:
    """What the remote command did. A non-zero ``rc`` is the command's own exit code, not an SSH failure."""

    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _spawn_kwargs(new_session: bool = False) -> dict[str, Any]:
    if _WIN:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True} if new_session else {}


async def _reap(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    """Make sure ``proc`` is gone and reaped: terminate, then kill after ``grace`` seconds."""
    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def _exec(cmd: list[str], *, conn: Connection, timeout: float, input: Optional[bytes] = None,
                env: Optional[dict[str, str]] = None, new_session: bool = False) -> RemoteResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, **_spawn_kwargs(new_session))
    except OSError as exc:
        raise SSHError("missing", f"Could not start ssh ({exc}). {install_hint()}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout)
    except asyncio.TimeoutError:
        await _reap(proc, grace=1.0)
        raise SSHError("timeout", f"{conn.host} did not finish within {timeout:g} s. The Spark may be busy or "
                                  "the network slow; try again.") from None
    except BaseException:                       # cancelled: never leave the ssh child behind
        await _reap(proc, grace=1.0)
        raise
    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace")
    if proc.returncode == 255:                  # ssh's own failures; the remote command's codes pass through
        raise ssh_failure(stderr, host=conn.host, port=conn.ssh_port, user=conn.ssh_user)
    return RemoteResult(proc.returncode if proc.returncode is not None else -1, stdout, stderr)


def _remote_command(argv: list[str]) -> str:
    if not argv or not all(isinstance(a, str) for a in argv) or any("\0" in a for a in argv):
        raise ValueError("the remote command must be a non-empty list of strings")
    return shlex.join(argv)


async def _ssh(conn: Connection, argv: list[str], *, home: Path, timeout: float, input: Optional[bytes] = None,
               extra: Iterable[str] = (), batch: bool = True, env: Optional[dict[str, str]] = None,
               new_session: bool = False) -> RemoteResult:
    cmd = [find_ssh(), *base_args(conn, home, batch=batch), *extra, "--", conn.host, _remote_command(argv)]
    log.debug("ssh %s@%s:%s %s", conn.ssh_user, conn.host, conn.ssh_port, argv[0])
    return await _exec(cmd, conn=conn, timeout=timeout, input=input, env=env, new_session=new_session)


async def run_remote(conn: Connection, argv: list[str], *, home: Path, timeout: float = 30.0,
                     input: Optional[bytes] = None) -> RemoteResult:
    """Run ``argv`` on the connection's host: ``ssh <base args> -- host <shlex.join(argv)>``.

    The remote login shell parses that one string, so each argv element arrives as one word. Raises
    SSHError for ssh's own failures (exit code 255, classified by ``kind``) and on ``timeout``; any
    other exit code is the remote command's and comes back in ``RemoteResult.rc``.
    """
    return await _ssh(conn, argv, home=Path(home), timeout=timeout, input=input)


# ---- the dedicated client key and its one-time installation -------------------------------------
_PUBLIC_LINE_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|ssh-rsa) ([A-Za-z0-9+/]{20,2000}={0,3})"
                             r"(?: ([A-Za-z0-9@._+:-]{1,100}))?\Z")
_key_lock = threading.Lock()


def check_public_line(line: Any) -> str:
    """One OpenSSH public key line, strictly: type, base64 blob of that type, optional plain comment.

    No options (``command=…``), no second line, no shell metacharacters: this is what goes into the
    remote ``authorized_keys``.
    """
    s = line.strip() if isinstance(line, str) else ""
    m = _PUBLIC_LINE_RE.match(s)
    if not m:
        raise ValueError("That is not a single OpenSSH public key line (type, key, optional comment).")
    HostKey.parse(m.group(1), m.group(2))          # the blob must really be a key of that type
    return s


def _public_line(key: Path) -> str:
    pub = Path(str(key) + ".pub")
    try:
        return check_public_line(pub.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    from cryptography.hazmat.primitives import serialization as ser
    priv = ser.load_ssh_private_key(key.read_bytes(), password=None)
    line = priv.public_key().public_bytes(ser.Encoding.OpenSSH, ser.PublicFormat.OpenSSH).decode()
    line = f"{line} twinspark-client"
    pub.write_text(line + "\n", encoding="utf-8")
    return check_public_line(line)


def ensure_client_key(home: Path) -> tuple[Path, str]:
    """The client's own ed25519 key (``<home>/ssh/twinspark_ed25519``), created on first use.

    Returns ``(private key path, public key line)``.
    """
    from ..provision import generate_ssh_key

    key = client_key_path(home)
    with _key_lock:
        ensure_private_dir(key.parent.parent)
        ensure_private_dir(key.parent)
        if not key.exists():
            host = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname() or "")[:60].strip("-.")
            try:
                generate_ssh_key(key, f"twinspark-client@{host}" if host else "twinspark-client")
            except FileExistsError:                # another client process was first: use its key
                pass
        return key, _public_line(key)


# Adds the key once. $1 = the full line, $2 = "type base64" (a key already present with another comment
# counts as present). No backslash pairs in here: fish, as a login shell, treats \\ and \' specially
# even inside the single quotes shlex.join produces.
INSTALL_KEY_SCRIPT = """umask 077
mkdir -p "$HOME/.ssh" || exit 3
f="$HOME/.ssh/authorized_keys"
if [ -f "$f" ] && grep -qF -- "$2" "$f"; then echo twinspark-key=present; exit 0; fi
if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then echo >> "$f"; fi
printf '%s\\n' "$1" >> "$f" || exit 3
echo twinspark-key=added
"""

# Runs as ssh's SSH_ASKPASS: prints the password from the environment ssh passed down. It refuses
# anything that looks like a host-key question, so it can never confirm an unknown key.
_ASKPASS_PY = """import os, sys
secret = os.environ.get("%s")
prompt = " ".join(sys.argv[1:]).lower()
if secret is None or "yes/no" in prompt or "fingerprint" in prompt or "continue connecting" in prompt:
    sys.exit(1)
sys.stdout.write(secret + "\\n")
""" % ASKPASS_ENV


def _write_askpass(tmp: Path) -> Path:
    exe = sys.executable
    if _WIN:
        py = tmp / "askpass.py"
        py.write_text(_ASKPASS_PY, encoding="utf-8")
        cmd = tmp / "askpass.cmd"
        cmd.write_text(f'@"{exe}" -I "{py}" %*\r\n', encoding="utf-8")
        return cmd
    script = tmp / "askpass"
    if exe and not any(c.isspace() for c in exe) and len(exe) < 120:
        script.write_text(f"#!{exe} -I\n{_ASKPASS_PY}", encoding="utf-8")
    else:                                        # a shebang cannot carry spaces: go through sh
        script.write_text(f"#!/bin/sh\nexec {shlex.quote(exe)} -I -c {shlex.quote(_ASKPASS_PY)} \"$@\"\n",
                          encoding="utf-8")
    os.chmod(script, 0o700)
    return script


async def install_key_with_password(conn: Connection, password: str, public_line: str, *, home: Path,
                                    private_key: Optional[Path] = None, timeout: float = 60.0) -> bool:
    """Append ``public_line`` to ``~/.ssh/authorized_keys`` on node A, logging in once with ``password``.

    One ssh run with OpenSSH's ``SSH_ASKPASS`` (``SSH_ASKPASS_REQUIRE=force``): a helper in a private
    temp dir prints the password from an environment variable that only that ssh process (and the
    helper it starts) receives. The password is never in an argv, a file or a log, and the temp dir is
    removed afterwards. The host key must already be trusted (StrictHostKeyChecking=yes), so the
    password is only ever sent to the confirmed host. Afterwards a BatchMode login with the new key
    (``private_key``, default the client key) must succeed.

    Returns True when the key was added, False when it was already there. Raises SSHError.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("Enter the SSH password of the user on node A.")
    if any(c in password for c in "\r\n\0"):
        raise ValueError("The password cannot contain line breaks.")
    line = check_public_line(public_line)
    body = " ".join(line.split()[:2])
    home = Path(home)
    exe = find_ssh()
    extra = ["-o", "BatchMode=no",
             "-o", "PreferredAuthentications=password,keyboard-interactive",
             "-o", "PasswordAuthentication=yes",
             "-o", "KbdInteractiveAuthentication=yes",
             "-o", "NumberOfPasswordPrompts=1"]
    cmd = [exe, *base_args(conn, home, batch=False), *extra, "--", conn.host,
           _remote_command(["sh", "-c", INSTALL_KEY_SCRIPT, "tsm-key-install", line, body])]
    tmp = Path(tempfile.mkdtemp(prefix="tsm-askpass-"))
    try:
        env = dict(os.environ)
        env.update({"SSH_ASKPASS": str(_write_askpass(tmp)), "SSH_ASKPASS_REQUIRE": "force", ASKPASS_ENV: password})
        env.setdefault("DISPLAY", ":0")           # OpenSSH before 8.4 only uses askpass when DISPLAY is set
        log.info("installing the TwinSpark client key for %s@%s", conn.ssh_user, conn.host)
        try:
            # new session (POSIX): without a controlling terminal an old ssh cannot fall back to /dev/tty
            res = await _exec(cmd, conn=conn, timeout=timeout, env=env, new_session=True)
        except SSHError as exc:
            if exc.kind == "auth":
                raise SSHError("auth", f"The password was not accepted for {conn.ssh_user}@{conn.host}, or node A "
                                       "does not allow password logins (PasswordAuthentication in "
                                       "/etc/ssh/sshd_config). Check the password, or choose a key file that "
                                       "node A already accepts.", detail=exc.detail) from None
            raise
        finally:
            env.clear()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if res.rc != 0 or "twinspark-key=" not in res.stdout:
        raise SSHError("other", f"Could not add the key to ~/.ssh/authorized_keys on {conn.host} "
                                f"(exit code {res.rc}): {_tail(res.stderr) or 'no error output'}")
    added = "twinspark-key=added" in res.stdout
    vconn = dataclasses.replace(conn, identity_file=str(private_key or client_key_path(home)))
    try:
        check = await _ssh(vconn, ["true"], home=home, timeout=min(timeout, 30.0),
                           extra=["-o", "IdentitiesOnly=yes"])       # prove THIS key works, not an agent key
    except SSHError as exc:
        if exc.kind == "auth":
            raise SSHError("auth", f"The key is in ~/.ssh/authorized_keys on {conn.host}, but node A still refuses "
                                   "it. Check that sshd allows public keys (PubkeyAuthentication) and that "
                                   "~/.ssh and the home directory are not writable by other users.",
                           detail=exc.detail) from None
        raise
    if check.rc != 0:
        raise SSHError("other", f"Logging in with the new key worked, but a test command failed on {conn.host} "
                                f"(exit code {check.rc}).")
    return added


# ---- local ports ------------------------------------------------------------------------------
def port_is_free(port: int, *, like_ssh: bool = False) -> bool:
    """Whether 127.0.0.1:``port`` can be bound right now.

    By default without SO_REUSEADDR: on macOS that option lets a bind succeed next to a wildcard
    listener, so a port another program serves would look free (Windows: SO_EXCLUSIVEADDRUSE for the
    same strictness). ``like_ssh=True`` binds the way ssh does (SO_REUSEADDR on POSIX), so a port that
    only has connections in TIME_WAIT from the previous tunnel counts as free.
    """
    check_port(port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if _WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif like_ssh:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_local_port(preferred: Optional[int] = None, *, avoid: Iterable[int] = ()) -> int:
    """``preferred`` when it is free on 127.0.0.1 (and not in ``avoid``), else a free port from the OS."""
    avoid = set(avoid)
    if preferred is not None and preferred not in avoid and port_is_free(preferred):
        return preferred
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in avoid:
            return port
    raise OSError("no free local port on 127.0.0.1")


# ---- tunnels ----------------------------------------------------------------------------------
class Forward(NamedTuple):
    """``127.0.0.1:local_port`` on this computer -> ``remote_host:remote_port`` as seen from node A."""

    local_port: int
    remote_port: int
    label: str
    remote_host: str = "127.0.0.1"

    def spec(self) -> str:
        rh = f"[{self.remote_host}]" if ":" in self.remote_host else self.remote_host
        return f"127.0.0.1:{self.local_port}:{rh}:{self.remote_port}"


def plan_forwards(conn: Connection, *, manager_host: str = "127.0.0.1",
                  gateway_host: str = "127.0.0.1") -> list[Forward]:
    """The standard forwards: ``api`` (management API + GUI) and ``gateway`` (OpenAI-compatible endpoint).

    Local ports come from the connection; unset ones are picked now (node A's port number when it is
    free here, else any free port). Save the picked ports back to the connection so apps configured
    with ``http://127.0.0.1:<port>`` keep working after a restart.
    """
    api = conn.local_api_port or free_local_port(conn.manager_port, avoid={conn.local_gateway_port or 0})
    gw = conn.local_gateway_port or free_local_port(conn.gateway_port, avoid={api})
    return [Forward(api, conn.manager_port, "api", manager_host),
            Forward(gw, conn.gateway_port, "gateway", gateway_host)]


STATES = ("stopped", "connecting", "up", "reconnecting", "auth-failed", "host-key-problem", "unreachable")
TERMINAL_STATES = frozenset({"auth-failed", "host-key-problem"})
_REFUSED_RE = re.compile(r"open failed: connect failed|channel \d+: open failed", re.I)

Sleep = Callable[[float], Any]


class Tunnel:
    """Supervises one ``ssh -N`` process that carries one or more ``-L`` forwards.

    ``state`` is one of :data:`STATES`. ``connecting`` (first attempt) -> ``up`` once every local port
    accepts TCP connections while ssh runs. When ssh exits it is restarted after a back-off that starts
    at ``backoff_initial`` (1 s) and doubles to ``backoff_max`` (30 s); the back-off starts over after
    the tunnel was up for ``stable_after`` seconds. While waiting the state is ``unreachable`` (host not
    reachable / timed out) or ``reconnecting`` (anything else). Authentication and host-key failures are
    terminal (``auth-failed``, ``host-key-problem``): retrying cannot fix them, the user has to act, then
    call :meth:`start` again. ``last_error`` holds the plain-language reason, ``since`` the time of the
    last state change (Unix seconds). ``on_change(tunnel)`` is called after every change (it may be a
    coroutine function). ``sleep`` (the back-off wait) and the poll intervals are injectable for tests.

    The ssh child is always terminated (then killed) on :meth:`stop`, on cancellation of the
    supervising task and on terminal failures; only ``terminate``/``kill`` are used, so this works on
    Windows too.
    """

    def __init__(self, conn: Connection, forwards: Iterable[Any], *, home: Path,
                 on_change: Optional[Callable[["Tunnel"], Any]] = None, sleep: Optional[Sleep] = None,
                 backoff_initial: float = 1.0, backoff_max: float = 30.0, poll_connecting: float = 1.0,
                 poll_up: float = 10.0, connect_timeout: float = 45.0, stable_after: float = 30.0,
                 health_timeout: float = 2.0):
        if not conn.uses_ssh:
            raise ValueError("A direct connection has no tunnel.")
        conn.validate()
        fwd = [Forward(*f) for f in forwards]
        if not fwd:
            raise ValueError("A tunnel needs at least one forward.")
        for f in fwd:
            check_port(f.local_port, "local port")
            check_port(f.remote_port, "remote port")
            check_host(f.remote_host)
            if not isinstance(f.label, str):
                raise ValueError("a forward label must be a string")
        if len({f.local_port for f in fwd}) != len(fwd):
            raise ValueError("Two forwards of one tunnel cannot use the same local port.")
        self.conn = conn
        self.home = Path(home)
        self.forwards: list[Forward] = fwd
        self.on_change = on_change
        self._sleep: Sleep = sleep or asyncio.sleep
        self.backoff_initial, self.backoff_max = backoff_initial, backoff_max
        self.poll_connecting, self.poll_up = poll_connecting, poll_up
        self.connect_timeout, self.stable_after, self.health_timeout = connect_timeout, stable_after, health_timeout
        self.state = "stopped"
        self.last_error: Optional[str] = None
        self.since = time.time()
        self.restarts = 0
        self.pid: Optional[int] = None
        self.last_refused_at: Optional[float] = None    # node A refused a forwarded connection (service down)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._stopping = False
        self._changed = asyncio.Event()

    @classmethod
    def for_connection(cls, conn: Connection, *, home: Path, manager_host: str = "127.0.0.1",
                       gateway_host: str = "127.0.0.1", **kw: Any) -> "Tunnel":
        return cls(conn, plan_forwards(conn, manager_host=manager_host, gateway_host=gateway_host), home=home, **kw)

    # ---- public API ---------------------------------------------------------------------------
    def command(self) -> list[str]:
        """The ssh argv this tunnel runs."""
        args = [find_ssh(), *base_args(self.conn, self.home), "-N", "-T"]
        for f in self.forwards:
            args += ["-L", f.spec()]
        return [*args, "--", self.conn.host]

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start supervising (returns at once; watch ``state``). Raises SSHError if ssh is missing."""
        if self.running:
            return
        self.command()                            # fail now, plainly, if ssh is missing or the config is bad
        self._stopping = False
        self.restarts = 0
        self._set("connecting", None)
        self._task = asyncio.create_task(self.run(), name=f"tsm-tunnel-{self.conn.id}")

    async def stop(self) -> None:
        """Stop supervising and end the ssh child. Safe to call at any time, also twice."""
        self._stopping = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                cur = asyncio.current_task()
                if cur is not None and cur.cancelling():
                    raise
        await self._kill()
        self._set("stopped", None)

    async def wait_for(self, *states: str, timeout: Optional[float] = None) -> str:
        """Wait until ``state`` is one of ``states``; returns it (asyncio.TimeoutError after ``timeout``)."""
        async def _wait() -> str:
            while self.state not in states:
                await self._changed.wait()
            return self.state
        return await asyncio.wait_for(_wait(), timeout)

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state, "since": self.since, "last_error": self.last_error,
                "terminal": self.state in TERMINAL_STATES, "restarts": self.restarts,
                "last_refused_at": self.last_refused_at,
                "forwards": [{"label": f.label, "local_port": f.local_port, "remote_port": f.remote_port,
                              "remote_host": f.remote_host, "local": f"127.0.0.1:{f.local_port}"}
                             for f in self.forwards]}

    # ---- supervision --------------------------------------------------------------------------
    async def run(self) -> None:
        """The supervision loop (``start`` runs it as a task). Returns on a terminal failure or stop."""
        delay = self.backoff_initial
        try:
            while not self._stopping:
                kind, message, up_for = await self._attempt()
                if self._stopping:
                    break
                if kind == "auth":
                    self._set("auth-failed", message)
                    return
                if kind in ("host-key-unknown", "host-key-changed"):
                    self._set("host-key-problem", message)
                    return
                if up_for >= self.stable_after:
                    delay = self.backoff_initial
                self._set("unreachable" if kind in ("unreachable", "timeout") else "reconnecting", message)
                self.restarts += 1
                await self._sleep(delay)
                delay = min(delay * 2, self.backoff_max)
        finally:
            await self._kill()
            if self.state not in TERMINAL_STATES:      # stopped, or the supervising task was cancelled
                self._set("stopped", None)

    async def _attempt(self) -> tuple[str, str, float]:
        """Run ssh once until it exits; return (error kind, message, seconds it was up)."""
        try:
            cmd = self.command()
        except SSHError as exc:
            return exc.kind, exc.message, 0.0
        busy = [f.local_port for f in self.forwards if not port_is_free(f.local_port, like_ssh=True)]
        if busy:
            # checked first: something else answering on the port would otherwise look like a healthy tunnel
            return "port-in-use", (f"Local port {busy[0]} is already used by another program on this computer. "
                                   "Close that program or choose another local port in the connection "
                                   "settings."), 0.0
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, **_spawn_kwargs())
        except OSError as exc:
            return "other", f"Could not start ssh ({exc}).", 0.0
        self._proc, self.pid = proc, proc.pid
        lines: deque[str] = deque(maxlen=60)
        reader = asyncio.create_task(self._drain(proc, lines))
        exited = asyncio.create_task(proc.wait())
        started = time.monotonic()
        up_since: Optional[float] = None
        forced: Optional[tuple[str, str]] = None
        try:
            while not exited.done():
                healthy = await self._healthy()
                if exited.done():
                    break
                if healthy and up_since is None:
                    up_since = time.monotonic()
                    self._set("up", None)
                elif not healthy and up_since is not None:
                    forced = ("other", "The tunnel stopped accepting local connections; restarting it.")
                    await _reap(proc)
                    break
                elif not healthy and time.monotonic() - started > self.connect_timeout:
                    forced = ("timeout", f"The tunnel to {self.conn.host} did not come up within "
                                         f"{self.connect_timeout:g} s; trying again.")
                    await _reap(proc)
                    break
                await asyncio.wait({exited}, timeout=self.poll_up if up_since is not None else self.poll_connecting)
            await exited
            try:
                await asyncio.wait_for(reader, 2.0)     # the rest of stderr, for the classification
            except asyncio.TimeoutError:
                pass
        finally:
            if proc.returncode is None:
                await _reap(proc)
            for t in (reader, exited):
                if not t.done():
                    t.cancel()
            self._proc = None
        up_for = time.monotonic() - up_since if up_since is not None else 0.0
        if forced:
            return forced[0], forced[1], up_for
        stderr = "\n".join(lines)
        err = ssh_failure(stderr, host=self.conn.host, port=self.conn.ssh_port, user=self.conn.ssh_user)
        if err.kind == "other":
            msg = (f"The SSH connection to {self.conn.host} ended (exit code {proc.returncode})"
                   + (f": {err.detail}" if err.detail else "") + "; reconnecting.")
            return "other", msg, up_for
        return err.kind, err.message, up_for

    async def _drain(self, proc: asyncio.subprocess.Process, lines: deque[str]) -> None:
        """Keep reading ssh's stderr (a full pipe would stall it); remember the last lines."""
        assert proc.stderr is not None
        buf = b""
        while True:
            chunk = await proc.stderr.read(4096)       # chunks, not readline(): no line-length limit to trip
            if not chunk:
                break
            *complete, buf = (buf + chunk).split(b"\n")
            if len(buf) > 8192:
                complete, buf = [*complete, buf], b""
            for raw in complete:
                self._note(raw, lines)
        if buf:
            self._note(buf, lines)

    def _note(self, raw: bytes, lines: deque[str]) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if _REFUSED_RE.search(line):
            self.last_refused_at = time.time()
        elif line:
            lines.append(line)

    async def _healthy(self) -> bool:
        for f in self.forwards:
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", f.local_port),
                                                   self.health_timeout)
            except (OSError, asyncio.TimeoutError):
                return False
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        return True

    async def _kill(self) -> None:
        proc = self._proc
        if proc is not None:
            await _reap(proc)
            self._proc = None

    def _set(self, state: str, error: Optional[str]) -> None:
        if state == self.state and error == self.last_error:
            return
        self.state, self.last_error, self.since = state, error, time.time()
        log.info("tunnel %s: %s%s", self.conn.id, state, f" ({error})" if error else "")
        ev, self._changed = self._changed, asyncio.Event()
        ev.set()
        if self.on_change is not None:
            try:
                res = self.on_change(self)
                if inspect.isawaitable(res):
                    asyncio.ensure_future(res)
            except Exception:  # noqa: BLE001 - a UI callback must not kill the supervisor
                log.exception("tunnel on_change callback failed")
