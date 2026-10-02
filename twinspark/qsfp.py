"""QSFP link automation for a pair of DGX Sparks: discover, plan, apply, verify.

Each QSFP port of a Spark is fed by two PCIe x4 halves that show up as *twin* interfaces
(``enp1s0f1np1`` and ``enP2p1s0f1np1``) and two RoCE devices (``rocep1s0f1``, ``roceP2p1s0f1``).
NCCL only reaches ~200 Gb/s when both twins carry traffic, which needs an address on each of them.

The layout used here follows the two guides people actually use for this:

* eugr/spark-vllm-docker, ``docs/NETWORKING.md`` and ``autodiscover.sh`` (MIT) — static addresses on both
  twins, MTU 9000, ``link-local: []``, a *different* subnet per twin ("DO NOT use the same subnet on
  both twins"), the active RoCE devices found with ``ibdev2netdev``, peers found by scanning the
  subnet for a GB10 over SSH.
* NVIDIA's "Connect two Sparks" playbook — the same two subnets (``192.168.100.x`` / ``192.168.101.x``).

Everything that reads the machine takes its inputs (sysfs roots, a command runner, the netplan
directory) as arguments, so the whole module is exercised against a fake filesystem and canned command
output. Nothing here runs on import and nothing changes the machine unless :func:`apply` or
:func:`revert` is called by a root user who confirmed the plan.

Safety rules for :func:`apply` (the machines are headless; a wrong network change cannot be fixed with a
keyboard):

1. it refuses to touch an interface that carries the default route or the SSH session it runs in,
2. it never edits a netplan file it does not own (a file without our marker is *foreign*; the plan then
   says what to change by hand),
3. it validates with ``netplan generate`` before applying,
4. it verifies the addresses and MTU afterwards and **puts the previous state back** if they are not there.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from . import hostprobe
from .agent import sysinfo

MARKER = "# Managed by TwinSpark (tsm qsfp)"
NETPLAN_FILE = "60-twinspark-qsfp.yaml"          # the name the old setup snippet already suggested
NETPLAN_ALT = "61-twinspark-qsfp.yaml"           # used when a hand-written file already has the name above
DEFAULT_SUBNET = "192.168.100.0/24"               # primary twin; the secondary twin gets the next /24
DEFAULT_MTU = 9000
JUMBO_PAYLOAD = 8972                              # 9000 - 20 (IP) - 8 (ICMP)

Run = Callable[[list[str], float], "tuple[int, str, str]"]
Sleep = Callable[[float], None]

_TWIN = sysinfo.TWIN_RE            # enp1s0f1np1 / enP2p1s0f1np1 are the two twins of port f1np1


class QsfpError(RuntimeError):
    """A readable reason the QSFP link could not be planned or changed."""


def _run(argv: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not installed"
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)
    return p.returncode, p.stdout, p.stderr


RUN: Run = _run              # replaced in tests


# ---- the machine, as paths ---------------------------------------------------------------------
@dataclass
class Host:
    """Where to read from and write to. The defaults are the real machine."""

    root: Path = Path("/")                 # files we write (netplan file, backups) go under this
    sysfs: Path = Path("/")                # /sys is read from under this
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    euid: Optional[int] = None
    sleep: Sleep = time.sleep
    force_real: bool = False               # tests: run the (faked) netplan/ip commands although root is a scratch dir
    owner_uid: int = 0                     # who must own a backup before root copies it back (root)

    @property
    def sandbox(self) -> bool:
        """A scratch root: files are written there and no network command is run."""
        return str(self.root) != "/" and not self.force_real

    @property
    def net(self) -> Path:
        return Path(self.sysfs, "sys/class/net")

    @property
    def ib(self) -> Path:
        return Path(self.sysfs, "sys/class/infiniband")

    @property
    def netplan_dir(self) -> Path:
        return Path(self.root, "etc/netplan")

    @property
    def managed_file(self) -> Path:
        """The file TwinSpark writes: ``60-twinspark-qsfp.yaml``, unless a file of that name is somebody else's
        (the older setup snippet suggested it), in which case ``61-…`` — a foreign file is never overwritten."""
        p = self.netplan_dir / NETPLAN_FILE
        return self.netplan_dir / NETPLAN_ALT if p.exists() and not is_ours(p) else p

    @property
    def backup_dir(self) -> Path:
        """Root-owned on purpose (not under /var/lib/twinspark, which the service user owns): a revert copies
        from here into /etc/netplan as root."""
        return Path(self.root, "var/backups/twinspark-qsfp")

    @property
    def temp_state(self) -> Path:
        """What `apply --temporary` set. In /run, so it disappears at reboot together with the addresses."""
        return Path(self.root, "run/twinspark-qsfp-temporary.json")

    def is_root(self) -> bool:
        return (os.geteuid() if self.euid is None and hasattr(os, "geteuid") else self.euid) == 0

    def run(self, argv: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
        return RUN(argv, timeout)

    def run2(self, argv: list[str], timeout: float = 8.0) -> tuple[int, str]:
        rc, out, _ = RUN(argv, timeout)
        return rc, out


def is_ours(path: Path) -> bool:
    """The file starts with TwinSpark's marker line."""
    try:
        return path.read_text().lstrip().startswith(MARKER)
    except OSError:
        return False


# ---- discovery ---------------------------------------------------------------------------------
@dataclass
class Twin:
    iface: str
    secondary: bool                         # the second PCIe half (enP2p…)
    hca: Optional[str] = None
    link_up: bool = False                   # the RoCE port is ACTIVE (or, without RDMA info, operstate up)
    rate_gbps: Optional[float] = None
    mtu: Optional[int] = None
    ipv4: list[str] = field(default_factory=list)          # CIDRs
    gids: list[dict[str, Any]] = field(default_factory=list)   # RoCE v2 IPv4 GIDs of its RDMA device

    @property
    def addr(self) -> Optional[str]:
        return self.ipv4[0].split("/")[0] if self.ipv4 else None

    def network(self) -> Optional[ipaddress.IPv4Network]:
        return ipaddress.ip_interface(self.ipv4[0]).network if self.ipv4 else None


@dataclass
class Port:
    key: str                                # "f1np1"
    twins: list[Twin]

    @property
    def cabled(self) -> bool:
        return any(t.link_up for t in self.twins)

    @property
    def primary(self) -> Optional[Twin]:
        return next((t for t in self.twins if not t.secondary), self.twins[0] if self.twins else None)

    @property
    def secondary(self) -> Optional[Twin]:
        return next((t for t in self.twins if t.secondary), None)

    def names(self) -> list[str]:
        return [t.iface for t in self.twins]


@dataclass
class Discovery:
    ports: list[Port]
    ifaces: list[hostprobe.Iface]           # every interface on the machine
    default_route_ifaces: list[str]
    rdma: list[dict[str, Any]]
    routes_known: bool = True               # False when the routing table could not be read

    def port_of(self, iface: str) -> Optional[Port]:
        return next((p for p in self.ports if iface in p.names()), None)


port_key = sysinfo.port_key


def _read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def discover(host: Optional[Host] = None, *, ifaces: Optional[list[hostprobe.Iface]] = None,
             rdma: Optional[list[dict[str, Any]]] = None, routes: Optional[list[str]] = None,
             routes_known: bool = True) -> Discovery:
    """Find the ConnectX ports, their twins, link state, MTU, addresses and RoCE devices.

    ``ifaces``, ``rdma`` and ``routes`` can be handed in by a caller that has probed the machine already
    (the setup wizard); otherwise they are read here.
    """
    host = host or Host()
    if rdma is None:
        rdma = sysinfo.rdma_devices(str(host.ib))
    if ifaces is None:
        ifaces = hostprobe.list_interfaces(str(host.net), host.run2, rdma)
    by_hca = {d["hca"]: d for d in rdma}
    groups: dict[str, list[Twin]] = {}
    for i in ifaces:
        if i.virtual:
            continue
        key = port_key(i.name)
        if key is None:
            continue
        dev = next((by_hca[h] for h in i.roce_hcas if h in by_hca), None)
        twin = Twin(
            iface=i.name, secondary=bool(_TWIN.match(i.name).group("dom")),            # type: ignore[union-attr]
            hca=dev["hca"] if dev else None,
            link_up=bool(dev["active"]) if dev else i.state == "up",
            rate_gbps=dev["rate_gbps"] if dev else None,
            mtu=_read_int(host.net / i.name / "mtu"),
            ipv4=[c for c in i.ipv4 if not ipaddress.ip_interface(c).ip.is_link_local],     # 169.254.x is "none"
            gids=list(dev["roce_v2_ipv4"]) if dev else [],
        )
        groups.setdefault(key, []).append(twin)
    ports = [Port(k, sorted(v, key=lambda t: (t.secondary, t.iface))) for k, v in sorted(groups.items())]
    if routes is None:
        found = hostprobe.default_routes(host.run2)
        routes, routes_known = found or [], found is not None
    return Discovery(ports, ifaces, routes, rdma, routes_known)


def choose_port(d: Discovery, iface: Optional[str] = None, configured: Optional[str] = None) -> Port:
    """The port to manage: the one named, else the cabled one (preferring what controller.yaml says)."""
    if not d.ports:
        raise QsfpError("no ConnectX port found (looked for enp1s0f1np1-style interfaces). Is this a DGX Spark? "
                        "Use `ip -br link` to see what the machine has.")
    if iface:
        p = d.port_of(iface)
        if p is None:
            raise QsfpError(f"{iface} is not a ConnectX QSFP interface here (found: "
                            f"{', '.join(n for p in d.ports for n in p.names())})")
        return p
    cabled = [p for p in d.ports if p.cabled]
    if not cabled:
        raise QsfpError("no QSFP link is up. Check that the cable sits in the SAME port on both Sparks, that the "
                        "other Spark is powered on, then try again (`ibdev2netdev` should show '(Up)').")
    if len(cabled) == 1:
        return cabled[0]
    if configured:
        pick = next((p for p in cabled if configured in p.names()), None)
        if pick:
            return pick
    with_addr = [p for p in cabled if any(t.ipv4 for t in p.twins)]
    pool = with_addr or cabled
    return sorted(pool, key=lambda p: p.key, reverse=True)[0]       # f1np1 = the right-hand port, like the guides


# ---- netplan files ---------------------------------------------------------------------------------
@dataclass
class NetplanFile:
    path: Path
    managed: bool
    ifaces: list[str]
    error: Optional[str] = None


def scan_netplan(host: Optional[Host] = None) -> list[NetplanFile]:
    host = host or Host()
    out: list[NetplanFile] = []
    d = host.netplan_dir
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.yaml")):
        try:
            text = p.read_text()
        except OSError as exc:
            out.append(NetplanFile(p, False, [], str(exc)))
            continue
        managed = text.lstrip().startswith(MARKER)
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            out.append(NetplanFile(p, managed, [], f"not valid YAML: {exc}"))
            continue
        net = doc.get("network") if isinstance(doc, dict) else None
        names: list[str] = []
        if isinstance(net, dict):
            for section in ("ethernets", "bonds", "bridges", "vlans"):
                block = net.get(section)
                if isinstance(block, dict):
                    for name, cfg in block.items():
                        names.append(str(name))
                        if isinstance(cfg, dict):
                            m = cfg.get("match")
                            if isinstance(m, dict) and isinstance(m.get("name"), str):
                                names.append(m["name"])
                            for member in cfg.get("interfaces") or []:
                                names.append(str(member))
        out.append(NetplanFile(p, managed, names))
    return out


# ---- the plan --------------------------------------------------------------------------------------
@dataclass
class Entry:
    iface: str
    cidr: str                               # "192.168.100.1/24"
    secondary: bool = False


@dataclass
class Plan:
    port: str
    node_host: int
    entries: list[Entry]
    mtu: int
    path: str
    text: str
    peer_ips: list[str]
    warnings: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)       # twins that already have an address from elsewhere

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def split_subnets(base: str = DEFAULT_SUBNET) -> tuple[ipaddress.IPv4Network, ipaddress.IPv4Network]:
    """The primary twin's /24 and the next /24 for the secondary twin (192.168.100.0 -> 192.168.101.0)."""
    try:
        net = ipaddress.ip_network(base, strict=False)
    except ValueError as exc:
        raise QsfpError(f"{base!r} is not a network (expected something like 192.168.100.0/24)") from exc
    if not isinstance(net, ipaddress.IPv4Network) or net.prefixlen != 24:
        raise QsfpError("the QSFP subnet must be an IPv4 /24 such as 192.168.100.0/24")
    if not net.is_private:
        raise QsfpError(f"{net} is not a private range; use 192.168.x.0/24, 172.16-31.x.0/24 or 10.x.y.0/24")
    nxt = ipaddress.ip_network((int(net.network_address) + 256, 24))
    if nxt.network_address.packed[:2] != net.network_address.packed[:2] or not nxt.is_private:
        raise QsfpError(f"{net} is at the end of its range; pick a subnet whose next /24 is also free")
    return net, nxt


def _host_in(net: ipaddress.IPv4Network, n: int) -> str:
    if not 1 <= n <= 254:
        raise QsfpError("the host number must be between 1 and 254 (node A = 1, node B = 2)")
    return f"{net.network_address + n}/{net.prefixlen}"


def render_netplan(entries: list[Entry], mtu: int) -> str:
    lines = [MARKER + " — change it with `tsm qsfp`, undo it with `sudo tsm qsfp revert`.",
             "# Two subnets on purpose: both twins of one port on one subnet confuses routing",
             "# (eugr/spark-vllm-docker docs/NETWORKING.md; NVIDIA's two-Spark playbook does the same).",
             "# optional: boot does not wait for these links (the other Spark may be off or unplugged).",
             "network:", "  version: 2", "  ethernets:"]
    for e in entries:
        lines += [f"    {e.iface}:", "      dhcp4: false", "      dhcp6: false", "      link-local: []",
                  "      optional: true", f"      mtu: {mtu}", f"      addresses: [{e.cidr}]"]
    return "\n".join(lines) + "\n"


def _temp_state(host: Host) -> dict[str, dict[str, Any]]:
    """``{iface: {"cidrs": [...], "mtu": previous MTU}}`` for addresses set by `apply --temporary`."""
    try:
        data = json.loads(host.temp_state.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): {"cidrs": [str(c) for c in (v or {}).get("cidrs", [])], "mtu": (v or {}).get("mtu")}
            for k, v in data.items() if isinstance(v, dict)}


def temp_recorded(host: Host) -> bool:
    return bool(_temp_state(host))


def _temp_ifaces(host: Host) -> set[str]:
    return set(_temp_state(host))


def _file_state(text: Optional[str]) -> dict[str, dict[str, Any]]:
    """``{iface: {"addresses": [...], "mtu": int|None}}`` of a netplan file's ethernets."""
    try:
        eth = (yaml.safe_load(text or "") or {})["network"]["ethernets"]
        items = list(eth.items())
    except (KeyError, TypeError, AttributeError, yaml.YAMLError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, cfg in items:
        if isinstance(cfg, dict):
            mtu = cfg.get("mtu")
            out[str(name)] = {"addresses": [a for a in cfg.get("addresses") or [] if isinstance(a, str)],
                              "mtu": mtu if isinstance(mtu, int) else None}
    return out


def owned_ifaces(host: Host) -> set[str]:
    """Interfaces TwinSpark may (re)configure: those in its own netplan file or set by `apply --temporary`."""
    names = _temp_ifaces(host)
    for f in scan_netplan(host):
        if f.managed:
            names |= set(f.ifaces)
    return names


def managed_layout(host: Host) -> Optional[tuple[int, str]]:
    """``(host number, primary /24)`` read back from TwinSpark's own netplan file, if there is one."""
    try:
        doc = yaml.safe_load(host.managed_file.read_text()) or {}
        eth = doc["network"]["ethernets"]
        addrs = [ipaddress.ip_interface(a) for cfg in eth.values() for a in cfg.get("addresses", [])]
    except (OSError, KeyError, TypeError, ValueError, AttributeError, yaml.YAMLError):
        return None
    v4 = sorted((a for a in addrs if a.version == 4), key=lambda a: a.network)
    if not v4:
        return None
    return int(v4[0].ip) & 0xFF, str(v4[0].network)


def make_plan(port: Port, node_host: int, *, subnet: str = DEFAULT_SUBNET, mtu: int = DEFAULT_MTU,
              host: Optional[Host] = None, owned: Optional[set[str]] = None) -> Plan:
    """The addresses for both twins of ``port``.

    A twin that already has an address which TwinSpark did not set (``owned``) is **kept as it is**: it is
    left out of the file we write, and the other twin gets the next subnet to match it.
    """
    if not 576 <= mtu <= 9216:
        raise QsfpError("MTU must be between 576 and 9216 (9000 is what the Spark guides use)")
    host = host or Host()
    owned = owned or set()
    s0, s1 = split_subnets(subnet)
    peer_host = 2 if node_host == 1 else 1
    entries: list[Entry] = []
    kept: list[str] = []
    peers: list[str] = []
    warnings: list[str] = []
    for t in port.twins:
        net = s1 if t.secondary else s0
        if t.ipv4 and t.iface not in owned:
            own = ipaddress.ip_interface(t.ipv4[0])
            kept.append(f"{t.iface} {t.ipv4[0]}")
            own_n = int(own.ip) & 0xFF
            peers.append(str(own.network.network_address + (2 if own_n == 1 else 1)))
            if own_n != node_host:
                warnings.append(f"{t.iface} keeps {t.ipv4[0]} (host .{own_n}) but the other twin gets host "
                                f".{node_host}; use the same host number on both twins so the other Spark can "
                                f"be found (--host-number {own_n})")
            if t.mtu is not None and t.mtu < mtu:
                warnings.append(f"{t.iface} keeps its own settings and has MTU {t.mtu}; set `mtu: {mtu}` where it is "
                                f"configured, or jumbo frames (and full RoCE speed) will not work")
            continue
        cidr = _host_in(net, node_host)
        if t.ipv4 and (t.ipv4[0] != cidr or (t.mtu is not None and t.mtu != mtu)):
            warnings.append(f"{t.iface} is configured and in use ({t.ipv4[0]}, MTU {t.mtu}); this changes it. Stop the "
                            f"running model first (tsm stop) — traffic over the link is interrupted for a moment"
                            + (" — and qsfp_ip in controller.yaml / the agent's listener bind must follow the new "
                               "address" if t.ipv4[0] != cidr else ""))
        entries.append(Entry(t.iface, cidr, t.secondary))
        peers.append(str(net.network_address + peer_host))
    for e in entries:
        mine = ipaddress.ip_interface(e.cidr).network
        for t in port.twins:
            if t.ipv4 and t.iface not in owned and ipaddress.ip_interface(t.ipv4[0]).network == mine:
                raise QsfpError(f"{t.iface} already has {t.ipv4[0]}, the same subnet {e.iface} would get. Both twins "
                                f"of a port need different subnets (192.168.100.x and 192.168.101.x): change "
                                f"{t.iface} where it is configured, or pass --subnet to move the plan")
    kept_nets = [ipaddress.ip_interface(t.ipv4[0]).network for t in port.twins if t.ipv4 and t.iface not in owned]
    if len(kept_nets) == 2 and kept_nets[0] == kept_nets[1]:
        warnings.append(f"both twins already share {kept_nets[0]}, which they are configured with elsewhere. Routing "
                        f"then favours one of them; give the second twin the next subnet "
                        f"({kept_nets[0].network_address + 256}/24) where it is configured")
    if len(port.twins) < 2:
        warnings.append("this port shows only one twin interface, so only one RoCE device can carry traffic "
                        "(NCCL tops out around 100 Gb/s)")
    if not port.twins:
        raise QsfpError(f"port {port.key} has no interfaces")
    if host.managed_file.name != NETPLAN_FILE:
        warnings.append(f"{NETPLAN_FILE} exists and was not written by TwinSpark (the older setup snippet suggested "
                        f"that name); it is left alone and TwinSpark's part goes to {host.managed_file.name}")
    return Plan(port=port.key, node_host=node_host, entries=entries, mtu=mtu, path=str(host.managed_file),
                text=render_netplan(entries, mtu) if entries else "", peer_ips=peers, warnings=warnings, kept=kept)


def plan_for_node(d: Discovery, node_id: Optional[str] = None, *, iface: Optional[str] = None,
                  subnet: Optional[str] = None, mtu: int = DEFAULT_MTU, host_number: Optional[int] = None,
                  configured: Optional[str] = None, host: Optional[Host] = None) -> Plan:
    """Work out the plan for this machine.

    The host number is node A = 1, node B = 2 unless an address that is already there says otherwise; the
    subnet follows an existing foreign address of the primary twin, else TwinSpark's own file, else
    ``192.168.100.0/24``.
    """
    host = host or Host()
    port = choose_port(d, iface, configured)
    owned = owned_ifaces(host)
    foreign = next((t for t in port.twins if t.ipv4 and t.iface not in owned), None)
    state = managed_layout(host)
    n = host_number
    if n is None and node_id:
        n = {"A": 1, "B": 2}.get(node_id.upper())
        if n is None:
            raise QsfpError(f"unknown node {node_id!r}: use A or B (or --host-number N)")
    if n is None and foreign is not None:
        n = int(ipaddress.ip_interface(foreign.ipv4[0]).ip) & 0xFF
    if n is None and state:
        n = state[0]
    if n is None:
        raise QsfpError("say which node this is: --node A or --node B (or --host-number N)")
    base = subnet
    if base is None:
        if foreign is not None and not foreign.secondary:
            base = str(ipaddress.ip_network(f"{ipaddress.ip_interface(foreign.ipv4[0]).ip}/24", strict=False))
        elif state:
            base = state[1]
        else:
            base = DEFAULT_SUBNET
    return make_plan(port, n, subnet=base, mtu=mtu, host=host, owned=owned)


# ---- pre-flight ------------------------------------------------------------------------------------
def ssh_client_ip(env: dict[str, str]) -> Optional[str]:
    parts = (env.get("SSH_CONNECTION") or "").split()
    return parts[0] if len(parts) == 4 else None


def ssh_clients(host: Host) -> list[str]:
    """Addresses of everybody connected to this machine's sshd — this session included.

    ``SSH_CONNECTION`` is lost across ``sudo``, so the live connection table is read as well.
    """
    found: list[str] = []
    ip = ssh_client_ip(host.env)
    if ip:
        found.append(ip)
    rc, out = host.run2(["ss", "-Hnt", "state", "established", "sport", "=", ":22"], 5)
    if rc == 0:
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 4:                                   # Recv-Q Send-Q Local:Port Peer:Port
                peer = cols[3].rsplit(":", 1)[0].strip("[]")
                if peer.startswith("::ffff:"):
                    peer = peer[7:]
                if peer not in found:
                    found.append(peer)
    return found


def _session_problems(host: Host, d: Discovery, names: list[str]) -> list[str]:
    """Reasons not to touch ``names``: they carry the default route or an SSH session, or we cannot tell."""
    problems: list[str] = []
    on_route = sorted(set(names) & set(d.default_route_ifaces))
    if on_route:
        problems.append(f"{', '.join(on_route)} carries this machine's default route — it is the management "
                        f"network, not the QSFP link; changing it could cut you off")
    if host.sandbox:
        return problems
    if not d.routes_known:
        problems.append("the routing table cannot be read (`ip route`), so the management network cannot be told "
                        "apart from the QSFP link")
    for client in ssh_clients(host):
        addr, _, zone = client.partition("%")                    # fe80::1%enp1s0f1np1 names its interface
        if zone:
            dev: Optional[str] = zone
        else:
            rc, out = host.run2(["ip", "-j", "route", "get", addr], 5)
            dev = None
            if rc == 0:
                with contextlib.suppress(ValueError, TypeError, KeyError, IndexError, AttributeError):
                    dev = json.loads(out)[0].get("dev")
            if dev is None:
                problems.append(f"cannot tell which interface the SSH session from {client} arrives through")
                continue
        if dev in names:
            problems.append(f"an SSH session ({client}) arrives through {dev}; changing it could drop you. "
                            f"Run this from the management network, or from the Remote terminal there")
    return problems


def preflight(host: Host, d: Discovery, plan: Plan, *, need_netplan: bool = True,
              need_root: bool = True) -> list[str]:
    """Problems that must stop :func:`apply`. An empty list means it is safe to go on."""
    names = [e.iface for e in plan.entries]
    present = {i.name for i in d.ifaces}
    problems = [f"interface {n} does not exist on this machine" for n in names if n not in present]
    problems += _session_problems(host, d, names)
    mine = host.managed_file
    if mine.exists() and not is_ours(mine):
        problems.append(f"{mine} exists and was not written by TwinSpark; not touching it")
    for f in scan_netplan(host):
        if f.error or f.path == mine:
            continue
        clash = sorted(set(names) & set(f.ifaces))
        if not clash:
            continue
        if f.managed:
            problems.append(f"{f.path} is another TwinSpark file that also configures {', '.join(clash)}; "
                            f"remove it first")
        else:
            problems.append(f"{f.path} already configures {', '.join(clash)} — TwinSpark does not edit files it "
                            f"did not write. Check it with `tsm qsfp verify`, or move it away and run this again")
    # subnet overlap with interfaces that are not part of the plan
    for e in plan.entries:
        net = ipaddress.ip_interface(e.cidr).network
        for i in d.ifaces:
            if i.name in names:
                continue
            for cidr in i.ipv4:
                if net.overlaps(ipaddress.ip_interface(cidr).network):
                    problems.append(f"{net} overlaps {i.name} ({cidr}); choose another range with "
                                    f"--subnet, for example 10.77.0.0/24")
    if not host.sandbox:
        if need_root and not host.is_root():
            problems.append("this needs root: run it with sudo")
        if need_netplan and not _which("netplan"):
            problems.append("netplan is not installed (sudo apt install netplan.io); use --temporary to set the "
                            "addresses until the next reboot instead")
    return problems


def _which(name: str) -> Optional[str]:
    import shutil
    return shutil.which(name)


# ---- apply / revert --------------------------------------------------------------------------------
def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tsm-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _uninterruptible():
    """Ignore Ctrl-C, hang-up and terminate while the network is half-changed, so the rollback always runs.

    Signal handlers can only be changed from the main thread; elsewhere this does nothing.
    """
    import signal
    saved: dict[Any, Any] = {}
    for name in ("SIGINT", "SIGHUP", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved[sig] = signal.signal(sig, signal.SIG_IGN)
        except ValueError:
            break
    try:
        yield
    finally:
        for sig, handler in saved.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)


def _live_ok(d: Discovery, plan: Plan) -> list[str]:
    """What is still missing after an apply: addresses and MTU on every planned interface."""
    miss = []
    by = {t.iface: t for p in d.ports for t in p.twins}
    for e in plan.entries:
        t = by.get(e.iface)
        want = e.cidr.split("/")[0]
        if t is None or want not in [c.split("/")[0] for c in t.ipv4]:
            miss.append(f"{e.iface} has no {want}")
        elif t.mtu is not None and t.mtu != plan.mtu:
            miss.append(f"{e.iface} has MTU {t.mtu}, wanted {plan.mtu}")
    return miss


def _mgmt_lost(before: Discovery, after: Discovery) -> list[str]:
    """Did the management network survive? Every interface that had a default route must still have it and
    still have its addresses."""
    if not before.routes_known:
        return []
    miss = []
    if not after.routes_known:
        return ["the routing table cannot be read"]
    now = {i.name: set(i.ipv4) for i in after.ifaces}
    for i in before.ifaces:
        if i.name not in before.default_route_ifaces:
            continue
        if i.name not in after.default_route_ifaces:
            miss.append(f"{i.name} lost its default route")
        miss += [f"{i.name} lost {c}" for c in i.ipv4 if c not in now.get(i.name, set())]
    return miss


def is_current(host: Host, d: Discovery, plan: Plan) -> bool:
    """The plan's file is already in place and (outside a sandbox) its addresses and MTU are live."""
    path = host.managed_file
    try:
        same = path.read_text() == plan.text
    except OSError:
        same = False
    return same and (host.sandbox or not _live_ok(d, plan))


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() // 1_000_000 % 1000:03d}"


def _rollback(host: Host, plan: Plan, before: dict[str, tuple[list[str], Optional[int]]], path: Path,
              old: Optional[str], backup: Optional[Path], why: str) -> QsfpError:
    """Put the previous file back, re-apply it, and take back what the failed apply left behind."""
    if old is None:
        with contextlib.suppress(OSError):
            path.unlink()
    else:
        _atomic_write(path, old)
    rc, _, err = host.run(["netplan", "apply"], 60)
    tail = "" if rc == 0 else f" (re-applying the old settings also failed: {err.strip()[:200]})"
    # networkd keeps the addresses of a file that is gone: remove exactly what the new file added
    was = _file_state(old)
    live = {t.iface: t for p in discover(host).ports for t in p.twins}
    for e in plan.entries:
        t = live.get(e.iface)
        if t is None:
            continue
        keep = set(before.get(e.iface, ([], None))[0]) | set(was.get(e.iface, {}).get("addresses", []))
        for cidr in t.ipv4:
            if cidr not in keep:
                host.run(["ip", "addr", "del", cidr, "dev", e.iface], 10)
        target = was.get(e.iface, {}).get("mtu") or before.get(e.iface, ([], None))[1]
        if target and t.mtu is not None and t.mtu != target:
            host.run(["ip", "link", "set", "dev", e.iface, "mtu", str(target)], 10)
    if backup is not None:
        with contextlib.suppress(OSError):
            backup.unlink()                                  # the old file is back; the copy would only confuse revert
    return QsfpError(f"{why} — the previous network settings were put back{tail}")


def apply(host: Host, plan: Plan, *, temporary: bool = False, wait_s: float = 20.0,
          d: Optional[Discovery] = None) -> dict[str, Any]:
    """Make the plan true. Raises :class:`QsfpError` (after restoring the previous state) on failure."""
    d = d or discover(host)
    if not plan.entries:
        return {"mode": "none", "changed": False,
                "message": "nothing to change: " + ("; ".join(plan.kept) + " already configured" if plan.kept
                                                   else "no interface to configure")}
    if not temporary and is_current(host, d, plan):
        return {"mode": "netplan", "changed": False, "file": str(host.managed_file), "message": "already configured"}
    problems = preflight(host, d, plan, need_netplan=not temporary)
    if problems:
        raise QsfpError("not safe to continue:\n  - " + "\n  - ".join(problems))
    if temporary:
        return _apply_temporary(host, plan, d)
    path = host.managed_file
    old = path.read_text() if path.exists() else None
    twins = {t.iface: t for p in d.ports for t in p.twins}
    before = {e.iface: (list(twins[e.iface].ipv4), twins[e.iface].mtu) for e in plan.entries if e.iface in twins}
    backup = None
    if old is not None:
        host.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = host.backup_dir / f"{path.name}.{_stamp()}"
        _atomic_write(backup, old)
    _atomic_write(path, plan.text)
    if host.sandbox:
        return {"mode": "sandbox", "changed": True, "file": str(path), "backup": str(backup) if backup else None,
                "message": "written; not applied because this is a sandbox root"}

    def rollback(why: str) -> QsfpError:
        return _rollback(host, plan, before, path, old, backup, why)

    with _uninterruptible():
        rc, _, err = host.run(["netplan", "generate"], 30)
        if rc != 0:
            raise rollback(f"netplan rejected the file: {err.strip()[:300]}")
        rc, _, err = host.run(["netplan", "apply"], 90)
        if rc != 0:
            raise rollback(f"netplan apply failed: {err.strip()[:300]}")
        deadline = time.monotonic() + wait_s
        while True:
            after = discover(host)
            miss = _live_ok(after, plan) + _mgmt_lost(d, after)
            if not miss:
                break
            if time.monotonic() >= deadline:
                raise rollback("the addresses did not come up (" + "; ".join(miss) + ")")
            host.sleep(0.5)
    state = _temp_state(host)
    if state:
        _write_temp_state(host, {k: v for k, v in state.items() if k not in {e.iface for e in plan.entries}})
    return {"mode": "netplan", "changed": True, "file": str(path), "backup": str(backup) if backup else None,
            "message": "applied and verified"}


def _write_temp_state(host: Host, state: dict[str, dict[str, Any]]) -> None:
    try:
        if state:
            _atomic_write(host.temp_state, json.dumps(state, sort_keys=True) + "\n", 0o600)
        else:
            host.temp_state.unlink(missing_ok=True)
    except OSError:
        pass                                    # only bookkeeping; the addresses themselves are what matters


def _apply_temporary(host: Host, plan: Plan, d: Discovery) -> dict[str, Any]:
    twins = {t.iface: t for p in d.ports for t in p.twins}
    state = _temp_state(host)
    done: list[list[str]] = []
    for e in plan.entries:
        for argv in (["ip", "link", "set", "dev", e.iface, "mtu", str(plan.mtu)],
                     ["ip", "addr", "replace", e.cidr, "dev", e.iface],
                     ["ip", "link", "set", "dev", e.iface, "up"]):
            if host.sandbox:
                done.append(argv)
                continue
            rc, _, err = host.run(argv, 15)
            if rc != 0:
                for e2 in plan.entries:
                    host.run(["ip", "addr", "del", e2.cidr, "dev", e2.iface], 10)
                    t = twins.get(e2.iface)
                    if t is not None and t.mtu:
                        host.run(["ip", "link", "set", "dev", e2.iface, "mtu", str(t.mtu)], 10)
                raise QsfpError(f"`{' '.join(argv)}` failed: {err.strip()[:200]} — the addresses were removed again")
            done.append(argv)
        rec = state.setdefault(e.iface, {"cidrs": [], "mtu": twins[e.iface].mtu if e.iface in twins else None})
        if e.cidr not in rec["cidrs"]:
            rec["cidrs"].append(e.cidr)
    _write_temp_state(host, state)
    return {"mode": "temporary", "changed": True, "commands": done,
            "message": "addresses set until the next reboot (no netplan file written); undo now with "
                       "`sudo tsm qsfp revert --temporary`"}


def _trusted_backup(host: Host, p: Path) -> bool:
    """A backup is only copied back as root if it is ours (marker) and nobody else could have written it."""
    try:
        text, st, dst = p.read_text(), p.stat(), p.parent.stat()
    except OSError:
        return False
    if not text.lstrip().startswith(MARKER):
        return False
    if host.sandbox or not hasattr(os, "getuid"):
        return True
    return all(s.st_uid == host.owner_uid and not s.st_mode & 0o022 for s in (st, dst))


def revert(host: Host, *, temporary: bool = False, plan: Optional[Plan] = None) -> dict[str, Any]:
    """Undo :func:`apply`: put the newest backup back (or remove our file) and re-apply netplan."""
    if temporary:
        todo: dict[str, dict[str, Any]] = {k: {"cidrs": list(v["cidrs"]), "mtu": v["mtu"]}
                                           for k, v in _temp_state(host).items()}
        for e in (plan.entries if plan else []):
            rec = todo.setdefault(e.iface, {"cidrs": [], "mtu": None})
            if e.cidr not in rec["cidrs"]:
                rec["cidrs"].append(e.cidr)
        if not todo:
            raise QsfpError("no temporary addresses are recorded (a reboot removes them anyway); to remove a "
                            "particular layout, say which with --node A|B")
        if not host.sandbox:
            problems = _session_problems(host, discover(host), list(todo))
            if problems:
                raise QsfpError("not safe to continue:\n  - " + "\n  - ".join(problems))
            for iface, rec in todo.items():
                for cidr in rec["cidrs"]:
                    host.run(["ip", "addr", "del", cidr, "dev", iface], 10)
                if rec["mtu"]:
                    host.run(["ip", "link", "set", "dev", iface, "mtu", str(rec["mtu"])], 10)
        left = {k: v for k, v in _temp_state(host).items() if k not in todo}
        _write_temp_state(host, left)
        return {"mode": "temporary", "changed": True, "message": "temporary addresses removed"}
    path = host.managed_file
    if not path.exists():
        return {"mode": "netplan", "changed": False, "message": "nothing to revert: TwinSpark has no netplan file here"}
    if not is_ours(path):
        raise QsfpError(f"{path} was not written by TwinSpark; not touching it")
    mine = _file_state(path.read_text())
    if not host.sandbox:
        problems = _session_problems(host, discover(host), list(mine))
        if problems:
            raise QsfpError("not safe to continue:\n  - " + "\n  - ".join(problems))
    backups = sorted(host.backup_dir.glob(f"{path.name}.*")) if host.backup_dir.is_dir() else []
    restored, new_text = None, None
    if backups:
        newest = backups[-1]
        if not _trusted_backup(host, newest):
            raise QsfpError(f"{newest} is not a backup TwinSpark can trust (it needs the marker line and, outside a "
                            f"sandbox, root ownership without group/other write access); not restoring it. Remove it "
                            f"or restore the file by hand")
        new_text = newest.read_text()
        _atomic_write(path, new_text)
        restored = str(newest)
        newest.unlink()                      # used up: reverting again steps back to the one before
    else:
        path.unlink()
    note = ""
    if not host.sandbox:
        rc, _, err = host.run(["netplan", "apply"], 90)
        if rc != 0:
            raise QsfpError(f"netplan apply failed after the revert: {err.strip()[:300]}")
        # networkd keeps the addresses of a file that is gone: remove those that file set and nothing keeps
        want = _file_state(new_text)
        live = {t.iface: t for p in discover(host).ports for t in p.twins}
        for iface, st in mine.items():
            t = live.get(iface)
            for cidr in (t.ipv4 if t else []):
                if cidr in st["addresses"] and cidr not in want.get(iface, {}).get("addresses", []):
                    host.run(["ip", "addr", "del", cidr, "dev", iface], 10)
        stuck = [i for i, st in mine.items() if st["mtu"] and i in live and live[i].mtu == st["mtu"]]
        if stuck and not restored:
            note = (f" ({', '.join(stuck)} keeps its MTU until the next reboot; "
                    f"`sudo ip link set dev <interface> mtu 1500` sets it now)")
    return {"mode": "sandbox" if host.sandbox else "netplan", "changed": True, "restored": restored,
            "message": ("restored the previous file" if restored else "removed TwinSpark's netplan file") + note}


# ---- verification ----------------------------------------------------------------------------------
def _chk(status: str, name: str, detail: str, fix: str = "") -> dict[str, str]:
    return {"status": status, "check": name, "detail": detail, "fix": fix}


def checks(d: Discovery, *, port: Optional[Port] = None, configured_iface: Optional[str] = None,
           configured_hcas: Optional[list[str]] = None, configured_gid: Optional[int] = None,
           mtu: int = DEFAULT_MTU) -> list[dict[str, str]]:
    """Local findings about the QSFP link, each with the command that fixes it (status: ok|warn|fail)."""
    out: list[dict[str, str]] = []
    if port is None:
        try:
            port = choose_port(d, configured=configured_iface)
        except QsfpError as exc:
            out.append(_chk("fail" if d.ports else "warn", "qsfp link", str(exc)))
            return out
    cabled = [p.key for p in d.ports if p.cabled]
    twins = port.twins
    up = [t for t in twins if t.link_up]
    if not up:
        out.append(_chk("fail", "qsfp link", f"port {port.key} has no link",
                        "cable in the SAME port on both Sparks; other Spark on?"))
        return out
    rate = max((t.rate_gbps or 0 for t in up), default=0)
    out.append(_chk("ok", "qsfp link", f"port {port.key} is up ({', '.join(t.iface for t in up)}"
                    + (f", {rate:.0f} Gb/s per RoCE device" if rate else "") + ")"))
    if len(cabled) > 1:
        out.append(_chk("warn", "qsfp ports", f"more than one port has a link ({', '.join(cabled)}); TwinSpark "
                        f"uses {port.key}. One cable gives the full bandwidth",
                        "unplug the second cable, or pass --iface to choose"))
    if len(twins) < 2:
        out.append(_chk("warn", "qsfp twins", f"only {twins[0].iface} exists for this port",
                        "the second PCIe half did not appear — driver/firmware; `dmesg | grep mlx5`"))
    missing = [t for t in twins if not t.ipv4]
    if len(missing) == len(twins):
        out.append(_chk("fail", "qsfp addresses", "no interface of the port has an IPv4 address",
                        "sudo tsm qsfp apply --node A   (and --node B on the other Spark)"))
    elif missing:
        out.append(_chk("warn", "qsfp addresses", f"{', '.join(t.iface for t in missing)} has no IPv4 address — "
                        f"NCCL can only use one RoCE device (about 100 Gb/s)",
                        "sudo tsm qsfp apply --node A|B   (gives both twins an address on separate subnets)"))
    else:
        nets = [t.network() for t in twins]
        if len(set(nets)) < len(nets):
            out.append(_chk("fail", "qsfp subnets", f"both twins share {nets[0]}; routing and ARP get confused "
                            f"(eugr/spark-vllm-docker and NVIDIA both use one subnet per twin)",
                            "sudo tsm qsfp apply --node A|B   (192.168.100.x and 192.168.101.x)"))
        else:
            out.append(_chk("ok", "qsfp addresses", ", ".join(f"{t.iface} {t.ipv4[0]}" for t in twins)))
    bad_mtu = [t for t in twins if t.mtu is not None and t.mtu < mtu]
    if bad_mtu:
        out.append(_chk("warn", "qsfp mtu", ", ".join(f"{t.iface} {t.mtu}" for t in bad_mtu) + f" (want {mtu}) — "
                        "RoCE then runs with a small MTU and loses bandwidth",
                        f"sudo tsm qsfp apply --node A|B   (sets mtu {mtu}); or: sudo ip link set dev <if> mtu {mtu}"))
    elif any(t.mtu for t in twins):
        out.append(_chk("ok", "qsfp mtu", ", ".join(f"{t.iface} {t.mtu}" for t in twins)))
    inactive = [t for t in twins if t.hca and not t.link_up]
    no_roce = [t for t in twins if not t.hca]
    if no_roce:
        out.append(_chk("warn", "qsfp roce", f"no RDMA device is bound to {', '.join(t.iface for t in no_roce)}",
                        "is the mlx5_ib module loaded? `ibdev2netdev`; sudo modprobe mlx5_ib"))
    elif inactive:
        out.append(_chk("warn", "qsfp roce", f"{', '.join(t.hca or t.iface for t in inactive)} is not ACTIVE"))
    else:
        no_gid = [t for t in twins if t.ipv4 and not t.gids]
        if no_gid:
            out.append(_chk("warn", "qsfp roce", f"{', '.join(t.hca or t.iface for t in no_gid)} has no RoCE v2 IPv4 "
                            "address yet", "give the interface an IPv4 address; `tsm rdma` shows the GIDs"))
        else:
            idx = sorted({g["index"] for t in twins for g in t.gids})
            out.append(_chk("ok" if len(idx) <= 1 else "warn", "qsfp roce",
                            f"{', '.join(t.hca for t in twins if t.hca)} active, GID index "
                            f"{', '.join(map(str, idx)) or '-'}",
                            "" if len(idx) <= 1 else "the devices use different GID indices; NCCL_IB_GID_INDEX "
                                                      "cannot cover both"))
    live_idx = sorted({g["index"] for t in twins for g in t.gids})
    if configured_gid is not None and len(live_idx) == 1 and live_idx[0] != configured_gid:
        out.append(_chk("warn", "qsfp gid", f"ib_gid_index in the config is {configured_gid} but the RoCE devices use "
                        f"{live_idx[0]} (turning IPv6 link-local off, as this layout does, shifts the indices)",
                        "tsm rdma --apply  (then restart the controller)"))
    if configured_hcas:
        found = sorted(t.hca for t in twins if t.hca and t.ipv4)
        if found and sorted(configured_hcas) != found:
            out.append(_chk("warn", "qsfp config", f"rdma_hcas in the config is {','.join(configured_hcas)} but the "
                            f"link offers {','.join(found)}", "tsm rdma --apply  (then restart the controller)"))
    return out


def peer_checks(host: Host, port: Port, peers: list[str], *, jumbo: int = JUMBO_PAYLOAD,
                tcp: Optional[Callable[[str, int, float, Optional[str]], bool]] = None) -> list[dict[str, str]]:
    """Ping the other Spark through each twin (jumbo frames first) and check that SSH answers."""
    tcp = tcp or _tcp_connect
    out: list[dict[str, str]] = []
    pairs = []
    for t in port.twins:
        net = t.network()
        if net is None:
            continue
        peer = next((p for p in peers if ipaddress.ip_address(p) in net), None)
        if peer:
            pairs.append((t, peer))
    if not pairs:
        return [_chk("warn", "qsfp peer", "no peer address on the same subnet as an address of this node",
                     "pass --peer <ip> (node A is usually 192.168.100.1, node B 192.168.100.2)")]
    for t, peer in pairs:
        rc, _, _ = host.run(["ping", "-c", "3", "-W", "1", "-I", t.iface, "-M", "do", "-s", str(jumbo), peer], 15)
        if rc == 0:
            out.append(_chk("ok", f"qsfp peer {peer}", f"answers through {t.iface} with {jumbo + 28}-byte frames"))
            continue
        rc2, _, _ = host.run(["ping", "-c", "3", "-W", "1", "-I", t.iface, peer], 15)
        if rc2 == 0:
            out.append(_chk("fail", f"qsfp peer {peer}", f"answers through {t.iface}, but NOT with jumbo frames — "
                            f"the other side has a smaller MTU",
                            "sudo tsm qsfp apply --node A|B on the other Spark (MTU 9000 on both twins)"))
        else:
            out.append(_chk("fail", f"qsfp peer {peer}", f"no answer through {t.iface}",
                            "the other Spark must have its half of the layout (`tsm qsfp apply --node B` there) "
                            "and be cabled to the same port"))
    ssh_peer = pairs[0][1]
    src = pairs[0][0].addr
    ok = tcp(ssh_peer, 22, 1.5, src)
    out.append(_chk("ok" if ok else "warn", "qsfp ssh", f"{ssh_peer}:22 " + ("is open" if ok else "does not answer"),
                    "" if ok else "weight sync (rsync over ssh) needs it: sudo systemctl enable --now ssh"))
    return out


# ---- scanning for the other Spark (the idea of eugr's autodiscover.sh) ------------------------------
def _tcp_connect(ip: str, port: int, timeout: float, src: Optional[str]) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        if src:
            s.bind((src, 0))
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def scan(twin: Twin, *, connect: Optional[Callable[[str, int, float, Optional[str]], bool]] = None,
         timeout: float = 0.5, workers: int = 64) -> list[str]:
    """Hosts on this twin's subnet that answer on SSH (port 22), excluding this machine."""
    net = twin.network()
    if net is None or net.num_addresses > 1024:
        return []
    connect = connect or _tcp_connect
    me = twin.addr
    cands = [str(h) for h in net.hosts() if str(h) != me]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        hits = list(ex.map(lambda ip: ip if connect(ip, 22, timeout, me) else None, cands))
    return [h for h in hits if h]


def identify(ip: str, user: str, host: Optional[Host] = None) -> Optional[str]:
    """The GPU name over SSH (key login only), like eugr's check for "NVIDIA GB10"."""
    host = host or Host()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", user):
        raise QsfpError(f"not a user name: {user!r}")
    try:
        ip = str(ipaddress.IPv4Address(ip))
    except ValueError as exc:
        raise QsfpError(f"not an IPv4 address: {ip!r}") from exc
    rc, out, _ = host.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-o",
                           "StrictHostKeyChecking=accept-new", f"{user}@{ip}",
                           "nvidia-smi --query-gpu=name --format=csv,noheader"], 10)
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else None


# ---- summary for humans and for the API ------------------------------------------------------------
def describe(d: Discovery) -> list[dict[str, Any]]:
    return [{"port": p.key, "cabled": p.cabled,
             "twins": [{"iface": t.iface, "secondary": t.secondary, "hca": t.hca, "link_up": t.link_up,
                        "rate_gbps": t.rate_gbps, "mtu": t.mtu, "ipv4": t.ipv4,
                        "gids": [g["index"] for g in t.gids]} for t in p.twins]} for p in d.ports]
