"""Saved connections: which Spark (node A) to reach, how, and on which local ports.

A :class:`Connection` is plain data. ``validate()`` is the single gate for everything that later ends
up on an ``ssh`` command line, so a hostile ``connections.json`` (or a hostile form field) can never turn
into an option such as ``-oProxyCommand=…``. :class:`ConnectionStore` keeps them in
``connections.json`` in the client home.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .paths import ensure_private_dir, read_json, write_json

log = logging.getLogger("twinspark.client")

MODES = ("ssh", "tailscale", "direct")
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
# POSIX "portable" user names (letters, digits, . _ -), not starting with '-' or '.', max 32 like Linux
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,31}\Z")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}\Z")

Run = Callable[[list[str], float], tuple[int, str]]


# ---- address checks ---------------------------------------------------------------------------
def _ip(host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    h = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    scope = ""
    if "%" in h:                                  # link-local with a zone: fe80::1%en0
        h, _, scope = h.partition("%")
        if not _SCOPE_RE.match(scope):
            return None
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return None
    return None if scope and ip.version == 4 else ip


def check_host(host: Any) -> str:
    """Return ``host`` if it is a hostname, an IPv4 or an IPv6 address; raise ValueError otherwise."""
    if not isinstance(host, str) or not host:
        raise ValueError("Enter the address of node A: a hostname, an IP address or a Tailscale name.")
    if host.startswith("-"):
        raise ValueError(f"{host!r} is not an address: it starts with '-'. Enter a hostname or an IP address.")
    if any(c.isspace() for c in host) or any(ord(c) < 32 for c in host):
        raise ValueError(f"{host!r} contains spaces or control characters. Enter only the hostname or IP address.")
    if _ip(host) is not None:
        if host.startswith("["):
            raise ValueError(f"Enter the IPv6 address without brackets: {host[1:-1]}")
        return host
    name = host[:-1] if host.endswith(".") else host
    if len(name) > 253 or not name or not all(_LABEL_RE.match(lbl) for lbl in name.split(".")):
        raise ValueError(f"{host!r} is not a valid hostname or IP address (letters, digits, '-' and '.' only; "
                         "no user@ part and no :port — the user and the port have their own fields).")
    if re.fullmatch(r"[0-9.]+", name):
        raise ValueError(f"{host!r} is not a valid IPv4 address.")
    return host


def check_user(user: Any) -> str:
    if not isinstance(user, str) or not user:
        raise ValueError("Enter the SSH user name you log in to node A with.")
    if not _USER_RE.match(user):
        raise ValueError(f"{user!r} is not a valid user name: use letters, digits, '.', '_' or '-' "
                         "(no spaces, no ';', not starting with '-'), at most 32 characters.")
    return user


def check_port(port: Any, what: str = "port") -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"The {what} must be a number from 1 to 65535 (got {port!r}).")
    return port


def is_loopback(host: str) -> bool:
    if host.lower().rstrip(".") == "localhost":
        return True
    ip = _ip(host)
    return bool(ip and ip.is_loopback)


def is_tailnet_address(host: str) -> bool:
    """True for Tailscale addresses: 100.64.0.0/10, fd7a:115c:a1e0::/48 or a MagicDNS ``*.ts.net`` name."""
    ip = _ip(host or "")
    if ip is not None:
        return ip in (TAILNET_V4 if ip.version == 4 else TAILNET_V6)
    return (host or "").lower().rstrip(".").endswith(".ts.net")


def suggest_mode(host: str) -> str:
    """The mode the UI should preselect for an address the user typed."""
    return "tailscale" if is_tailnet_address(host) else "ssh"


def check_direct_url(url: Any) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError("Direct mode needs the URL of the management API, for example https://spark-a:8443")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError(f"{url!r} is not a valid URL.") from None
    host = parts.hostname or ""
    if parts.scheme not in ("https", "http") or not host:
        raise ValueError(f"{url!r} is not an http(s) URL. Use the form https://host:8443")
    if parts.username or parts.password:
        raise ValueError("Do not put a user name or password into the URL; the client sends the management key "
                         "itself.")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError(f"Use only scheme, host and port in the URL (for example https://{host}:8443).")
    check_host(host)
    if port is not None:
        check_port(port, "URL port")
    if parts.scheme == "http" and not is_loopback(host):
        raise ValueError("A direct connection over the network must use https: plain http would send the "
                         "management key readable to anyone on the path. Use https, or SSH / Tailscale mode.")
    return url


# ---- the connection record --------------------------------------------------------------------
@dataclass
class Connection:
    """One saved way to reach node A.

    ``mode``: ``ssh`` (LAN address), ``tailscale`` (SSH over the tailnet address) or ``direct`` (an
    https URL in ``direct_url``, no SSH). ``manager_port`` / ``gateway_port`` are node A's ports;
    ``local_api_port`` / ``local_gateway_port`` are the 127.0.0.1 ports on this computer (``None``
    until the first connect picks them). Timestamps are Unix seconds.
    """

    id: str
    name: str
    mode: str = "ssh"
    host: str = ""
    ssh_user: str = ""
    ssh_port: int = 22
    identity_file: Optional[str] = None
    manager_port: int = 8443
    gateway_port: int = 8000
    local_api_port: Optional[int] = None
    local_gateway_port: Optional[int] = None
    direct_url: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_connected_at: Optional[float] = None

    @property
    def uses_ssh(self) -> bool:
        return self.mode in ("ssh", "tailscale")

    def validate(self) -> "Connection":
        """Raise ValueError with a sentence the UI can show as-is; return self when everything is fine."""
        if not isinstance(self.id, str) or not _ID_RE.match(self.id):
            raise ValueError(f"{self.id!r} is not a valid connection id (lowercase letters, digits and '-').")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 80 \
                or any(ord(c) < 32 for c in self.name):
            raise ValueError("Give the connection a name of 1 to 80 characters.")
        if self.mode not in MODES:
            raise ValueError(f"Unknown connection mode {self.mode!r}: use ssh, tailscale or direct.")
        for what, port in (("SSH port", self.ssh_port), ("management port on node A", self.manager_port),
                           ("gateway port on node A", self.gateway_port)):
            check_port(port, what)
        for what, port in (("local API port", self.local_api_port),
                           ("local gateway port", self.local_gateway_port)):
            if port is not None:
                check_port(port, what)
        if self.local_api_port is not None and self.local_api_port == self.local_gateway_port:
            raise ValueError("The local API port and the local gateway port must be different.")
        if self.identity_file is not None:
            if not isinstance(self.identity_file, str) or not self.identity_file.strip() \
                    or any(ord(c) < 32 for c in self.identity_file):
                raise ValueError("The key file must be a path to a private key file.")
            if "%" in self.identity_file:
                raise ValueError("The key file path contains '%', which OpenSSH reads as a placeholder. "
                                 "Move the key to a path without '%'.")
        if self.direct_url is not None or self.mode == "direct":
            check_direct_url(self.direct_url)
        if self.mode == "direct":
            if self.host:
                check_host(self.host)
            return self
        check_host(self.host)
        check_user(self.ssh_user)
        short_name = _ip(self.host) is None and "." not in self.host.rstrip(".")
        if self.mode == "tailscale" and not (is_tailnet_address(self.host) or short_name):
            # a MagicDNS short name ("spark-a") is fine; a LAN name or a LAN IP in this mode is a mistake
            raise ValueError(f"{self.host} is not a Tailscale address. Use the node's 100.x.y.z address or its "
                             "MagicDNS name (…ts.net), or choose SSH mode for a LAN address.")
        return self

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Connection":
        """Build from stored JSON. Unknown keys (written by a newer client) are ignored; not validated."""
        if not isinstance(data, dict):
            raise ValueError("a connection must be a JSON object")
        names = {f.name for f in dataclasses.fields(cls)}
        known = {k: v for k, v in data.items() if k in names}
        if "id" not in known or "name" not in known:
            raise ValueError("a stored connection lacks its id or name")
        return cls(**known)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].strip("-")
    return slug or "spark"


class ConnectionStore:
    """``connections.json`` in the client home: ``{"version": 1, "connections": [ … ]}``."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.path = self.home / "connections.json"
        self._lock = threading.Lock()

    def _load(self) -> list[Connection]:
        doc = read_json(self.path, {})
        rows = doc.get("connections") if isinstance(doc, dict) else None
        out: list[Connection] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                out.append(Connection.from_dict(row))
            except (TypeError, ValueError) as exc:
                log.warning("ignoring an unreadable entry in %s: %s", self.path, exc)
        return out

    def _save(self, conns: list[Connection]) -> None:
        ensure_private_dir(self.home)
        write_json(self.path, {"version": 1, "connections": [c.to_dict() for c in conns]})

    def list(self) -> list[Connection]:
        with self._lock:
            return self._load()

    def get(self, conn_id: str) -> Optional[Connection]:
        return next((c for c in self.list() if c.id == conn_id), None)

    def new_id(self, name: str) -> str:
        taken = {c.id for c in self.list()}
        base = slugify(name)
        cand, n = base, 2
        while cand in taken:
            cand, n = f"{base}-{n}", n + 1
        return cand

    def add(self, conn: Connection) -> Connection:
        """Validate and save a new connection. An empty ``id`` is derived from the name."""
        with self._lock:
            conns = self._load()
            if not conn.id:
                taken = {c.id for c in conns}
                base, n = slugify(conn.name), 2
                conn.id = base
                while conn.id in taken:
                    conn.id, n = f"{base}-{n}", n + 1
            conn.validate()
            if any(c.id == conn.id for c in conns):
                raise ValueError(f"A connection with the id {conn.id!r} already exists.")
            conns.append(conn)
            self._save(conns)
            return conn

    def update(self, conn_id: str, **changes: Any) -> Connection:
        """Change fields of a saved connection (``id`` and ``created_at`` stay); validates the result."""
        bad = set(changes) - {f.name for f in dataclasses.fields(Connection)}
        if bad:
            raise ValueError(f"Unknown connection field(s): {', '.join(sorted(bad))}")
        if {"id", "created_at"} & set(changes):
            raise ValueError("The id and the creation time of a connection cannot be changed.")
        with self._lock:
            conns = self._load()
            for i, c in enumerate(conns):
                if c.id == conn_id:
                    new = dataclasses.replace(c, **changes).validate()
                    conns[i] = new
                    self._save(conns)
                    return new
        raise KeyError(conn_id)

    def mark_connected(self, conn_id: str, when: Optional[float] = None) -> Connection:
        return self.update(conn_id, last_connected_at=time.time() if when is None else when)

    def remove(self, conn_id: str, *, credentials: Any = None) -> bool:
        """Delete a connection; with ``credentials`` (a CredentialStore) its stored keys go too."""
        with self._lock:
            conns = self._load()
            keep = [c for c in conns if c.id != conn_id]
            if len(keep) != len(conns):
                self._save(keep)
        if credentials is not None:
            credentials.delete_all(conn_id)
        return len(keep) != len(conns)


# ---- tailscale --------------------------------------------------------------------------------
def _tailscale_binary() -> Optional[str]:
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = []
    if sys.platform == "darwin":
        candidates.append("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    elif sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(str(Path(base) / "Tailscale" / "tailscale.exe"))
    return next((c for c in candidates if Path(c).is_file()), None)


def _run_tailscale(argv: list[str], timeout: float) -> tuple[int, str]:
    exe = _tailscale_binary()
    if not exe:
        return 127, "tailscale is not installed"
    try:
        p = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return p.returncode, p.stdout            # stdout only: a warning on stderr would break the JSON


def tailscale_peers(run: Optional[Run] = None, *, timeout: float = 8.0) -> list[dict[str, Any]]:
    """Devices on this computer's tailnet, from ``tailscale status --json``, online ones first.

    Each item: ``name``, ``dns_name`` (MagicDNS, no trailing dot), ``address`` (first tailnet IPv4, else
    IPv6), ``ips``, ``os``, ``online``. Returns ``[]`` when Tailscale is missing, logged out or answers
    with something unexpected — a picker that stays empty, never an error.
    ``run(argv, timeout) -> (rc, stdout)`` is injectable for tests.
    """
    try:
        rc, out = (run or _run_tailscale)(["tailscale", "status", "--json"], timeout)
        if rc != 0:
            return []
        doc = json.loads(out or "{}")
    except Exception as exc:  # noqa: BLE001 - a picker must never break the connect page
        log.debug("tailscale status failed: %s", exc)
        return []
    peers = doc.get("Peer") if isinstance(doc, dict) else None
    out_list: list[dict[str, Any]] = []
    for p in (peers or {}).values() if isinstance(peers, dict) else []:
        if not isinstance(p, dict):
            continue
        ips = [str(ip) for ip in p.get("TailscaleIPs") or [] if isinstance(ip, str) and _ip(ip)]
        if not ips:
            continue
        v4 = [ip for ip in ips if ":" not in ip]
        out_list.append({
            "name": str(p.get("HostName") or ""),
            "dns_name": str(p.get("DNSName") or "").rstrip("."),
            "address": (v4 or ips)[0],
            "ips": ips,
            "os": str(p.get("OS") or ""),
            "online": bool(p.get("Online")),
        })
    out_list.sort(key=lambda d: (not d["online"], d["name"].lower(), d["address"]))
    return out_list
