"""A fake DGX Spark for the QSFP tests: a /sys tree, ``ip``, ``ss``, ``ping`` and ``netplan``.

Nothing here touches the machine the tests run on. :class:`FakeNode` keeps the state of the network
(addresses, MTU, link, netplan files under a scratch root) and answers the commands
``twinspark.qsfp`` runs; ``netplan apply`` really reads the YAML files under the scratch root and turns
them into addresses, so an apply / restore round trip is exercised end to end.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any, Optional

import yaml

from twinspark import qsfp

PRIMARY, SECONDARY = "enp1s0f1np1", "enP2p1s0f1np1"
HCA = {PRIMARY: "rocep1s0f1", SECONDARY: "roceP2p1s0f1"}
MGMT = "enP7s7"


def _hex(ip: str) -> str:
    a, b, c, d = (int(x) for x in ip.split("."))
    return f"{a:02x}{b:02x}:{c:02x}{d:02x}"


class FakeNode:
    """One Spark. ``tmp`` holds both the fake /sys (``tmp/sys``) and the scratch root (``tmp/root``)."""

    def __init__(self, tmp: Path, *, primary_ips: Optional[list[str]] = None,
                 secondary_ips: Optional[list[str]] = None, cabled: bool = True, mtu: int = 1500,
                 second_port: bool = False, with_secondary: bool = True):
        self.tmp = tmp
        self.sysfs = tmp / "sys_root"
        self.root = tmp / "root"
        self.root.mkdir(parents=True, exist_ok=True)
        self.ifaces: dict[str, dict[str, Any]] = {
            MGMT: {"ips": ["10.0.0.50/24"], "mtu": 1500, "up": True, "hca": None, "roce": False, "virtual": False},
            PRIMARY: {"ips": list(primary_ips or []), "mtu": mtu, "up": cabled, "hca": HCA[PRIMARY],
                      "roce": True, "virtual": False},
        }
        if with_secondary:
            self.ifaces[SECONDARY] = {"ips": list(secondary_ips or []), "mtu": mtu, "up": cabled,
                                      "hca": HCA[SECONDARY], "roce": True, "virtual": False}
        if second_port:                       # the left-hand port, not cabled
            self.ifaces["enp1s0f0np0"] = {"ips": [], "mtu": 1500, "up": False, "hca": "rocep1s0f0",
                                          "roce": True, "virtual": False}
        self.ifaces["docker0"] = {"ips": ["172.17.0.1/16"], "mtu": 1500, "up": True, "hca": None,
                                  "roce": False, "virtual": True}
        self.default_dev = MGMT
        self.ssh_clients: list[str] = []
        self.peer_alive = True                # the other Spark answers pings
        self.peer_mtu = 9000
        self.ssh_open = True
        self.multipath = False
        self._saved_default: Optional[str] = None
        # generate, apply, apply-once, no-address, ip-addr, stale, drop-mgmt-once, no-route-table
        self.fail: set[str] = set()
        self.netplan_owned: set[str] = set()
        self.calls: list[list[str]] = []
        self.sync()

    # ---- the fake /sys ----------------------------------------------------------------------
    def sync(self) -> None:
        import shutil
        shutil.rmtree(self.sysfs, ignore_errors=True)
        net = self.sysfs / "sys/class/net"
        ib = self.sysfs / "sys/class/infiniband"
        for name, st in self.ifaces.items():
            d = net / name
            d.mkdir(parents=True)
            (d / "operstate").write_text("up\n" if st["up"] else "down\n")
            (d / "mtu").write_text(f"{st['mtu']}\n")
            (d / "address").write_text("aa:bb:cc:00:00:%02x\n" % (abs(hash(name)) % 255))
            if not st["virtual"]:
                (d / "device").mkdir()
            if st["roce"]:
                port = ib / st["hca"] / "ports" / "1"
                for sub in ("gids", "gid_attrs/types", "gid_attrs/ndevs"):
                    (port / sub).mkdir(parents=True)
                (port / "state").write_text("4: ACTIVE\n" if st["up"] else "1: DOWN\n")
                (port / "rate").write_text("100 Gb/sec (4X EDR)\n")
                (port / "link_layer").write_text("Ethernet\n")
                (ib / st["hca"] / "device" / "net" / name).mkdir(parents=True)
                gids = [("fe80:0000:0000:0000:0000:0000:0000:0001", "IB/RoCE v1"),
                        ("fe80:0000:0000:0000:0000:0000:0000:0001", "RoCE v2")]
                for cidr in st["ips"]:
                    g = "0000:0000:0000:0000:0000:ffff:" + _hex(cidr.split("/")[0])
                    gids += [(g, "IB/RoCE v1"), (g, "RoCE v2")]
                for i, (g, typ) in enumerate(gids):
                    (port / "gids" / str(i)).write_text(g + "\n")
                    (port / "gid_attrs" / "types" / str(i)).write_text(typ + "\n")
                    (port / "gid_attrs" / "ndevs" / str(i)).write_text(name + "\n")

    def host(self, *, real: bool = False, euid: int = 0, env: Optional[dict[str, str]] = None) -> qsfp.Host:
        return qsfp.Host(root=self.root, sysfs=self.sysfs, env=env or {}, euid=euid,
                         sleep=lambda s: None, force_real=real)

    # ---- files ------------------------------------------------------------------------------
    def netplan_dir(self) -> Path:
        d = self.root / "etc/netplan"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_netplan(self, name: str, text: str) -> Path:
        p = self.netplan_dir() / name
        p.write_text(text)
        return p

    def _net_for(self, ip: str) -> Optional[str]:
        a = ipaddress.ip_address(ip)
        for name, st in self.ifaces.items():
            for cidr in st["ips"]:
                if a in ipaddress.ip_interface(cidr).network:
                    return name
        return None

    # ---- commands ---------------------------------------------------------------------------
    def run(self, argv: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        a = argv
        if a[:4] == ["ip", "-j", "-4", "addr"]:
            return 0, json.dumps([{"ifname": n, "addr_info": [
                {"family": "inet", "local": c.split("/")[0], "prefixlen": int(c.split("/")[1])} for c in st["ips"]]}
                for n, st in self.ifaces.items()]), ""
        if a[:5] == ["ip", "-j", "route", "show", "default"]:
            if "no-route-table" in self.fail:
                return 1, "", "RTNETLINK answers: Operation not permitted"
            if self.multipath:
                return 0, json.dumps([{"dst": "default", "flags": ["onlink"], "nexthops": [
                    {"gateway": "10.0.0.1", "dev": self.default_dev}, {"gateway": "10.0.1.1", "dev": "docker0"}]}]), ""
            if self.default_dev is None:
                return 0, "[]", ""
            return 0, json.dumps([{"dst": "default", "gateway": "10.0.0.1", "dev": self.default_dev}]), ""
        if a[:4] == ["ip", "-j", "route", "get"]:
            dev = self._net_for(a[4]) or self.default_dev
            return 0, json.dumps([{"dst": a[4], "dev": dev}]), ""
        if a[0] == "ss":
            return 0, "".join(f"0 0 10.0.0.50:22 {c}:51234\n" for c in self.ssh_clients), ""
        if a[0] == "netplan":
            return self._netplan(a[1])
        if a[:3] == ["ip", "link", "set"]:
            dev = a[4] if a[3] == "dev" else a[3]
            if dev not in self.ifaces:
                return 1, "", f"Cannot find device {dev}"
            if "mtu" in a:
                self.ifaces[dev]["mtu"] = int(a[a.index("mtu") + 1])
            self.sync()
            return 0, "", ""
        if a[:3] == ["ip", "addr", "replace"]:
            if "ip-addr" in self.fail and a[5] == SECONDARY:
                return 2, "", "RTNETLINK answers: Operation not supported"
            st = self.ifaces[a[5]]
            st["ips"] = [a[3]]
            self.sync()
            return 0, "", ""
        if a[:3] == ["ip", "addr", "del"]:
            st = self.ifaces[a[5]]
            st["ips"] = [c for c in st["ips"] if c != a[3]]
            self.sync()
            return 0, "", ""
        if a[0] == "ping":
            return self._ping(a)
        if a[0] == "ssh":
            return 0, "NVIDIA GB10\n", ""
        return 127, "", f"{a[0]}: not faked"

    def _ping(self, a: list[str]) -> tuple[int, str, str]:
        iface = a[a.index("-I") + 1]
        peer = a[-1]
        st = self.ifaces.get(iface)
        ok_net = st and any(ipaddress.ip_address(peer) in ipaddress.ip_interface(c).network for c in st["ips"])
        if not (self.peer_alive and ok_net and st["up"]):
            return 1, "", "100% packet loss"
        if "-M" in a:
            size = int(a[a.index("-s") + 1]) + 28
            if size > min(self.peer_mtu, st["mtu"]):
                return 1, "", "message too long"
        return 0, "", ""

    def _netplan(self, verb: str) -> tuple[int, str, str]:
        if verb == "generate":
            if "generate" in self.fail:
                return 1, "", "Error in network definition: expected mapping"
            for p in sorted(self.netplan_dir().glob("*.yaml")):
                try:
                    yaml.safe_load(p.read_text())
                except yaml.YAMLError:
                    return 1, "", f"{p}: invalid YAML"
            return 0, "", ""
        if verb == "apply":
            if "apply" in self.fail:
                return 1, "", "netplan apply failed"
            if "apply-once" in self.fail:
                self.fail.discard("apply-once")
                return 1, "", "netplan apply failed"
            wanted: dict[str, dict[str, Any]] = {}
            for p in sorted(self.netplan_dir().glob("*.yaml")):
                doc = yaml.safe_load(p.read_text()) or {}
                for name, cfg in ((doc.get("network") or {}).get("ethernets") or {}).items():
                    wanted[name] = cfg or {}
            if self._saved_default is not None:                           # the one-off management outage is over
                self.default_dev, self._saved_default = self._saved_default, None
            for name in self.netplan_owned - set(wanted):                 # a file went away: its settings go too
                if name in self.ifaces and "stale" not in self.fail:      # (networkd keeps them: netplan bug 1781459)
                    self.ifaces[name]["ips"], self.ifaces[name]["mtu"] = [], 1500
            for name, cfg in wanted.items():
                if name in self.ifaces and "no-address" not in self.fail:
                    self.ifaces[name]["ips"] = list(cfg.get("addresses") or [])
                    self.ifaces[name]["mtu"] = int(cfg.get("mtu", 1500))
            self.netplan_owned = set(wanted)
            if "drop-mgmt-once" in self.fail:                             # the management route vanishes during apply
                self.fail.discard("drop-mgmt-once")
                self._saved_default, self.default_dev = self.default_dev, None
            self.sync()
            return 0, "", ""
        return 1, "", "unknown verb"
