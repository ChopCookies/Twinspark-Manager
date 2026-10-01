"""Read-only probes of the machine ``tsm setup`` and ``tsm node doctor`` run on.

Nothing here changes the host. Every probe takes its inputs (sysfs root, command
runner, environment) as arguments so the whole module can be tested against a
fake filesystem and canned command output — which is how it is tested: the
setup path must never need a real Spark to be exercised.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .agent import sysinfo

Runner = Callable[[list[str], float], "tuple[int, str]"]


def run_cmd(argv: list[str], timeout: float = 8.0) -> tuple[int, str]:
    """Run a short read-only command. Returns (returncode, stdout); (-1, '') if it cannot run."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return p.returncode, p.stdout


# ---- network ---------------------------------------------------------------------------------
@dataclass
class Iface:
    name: str
    mac: Optional[str] = None
    state: str = "unknown"
    speed_mbps: Optional[int] = None
    ipv4: list[str] = field(default_factory=list)       # "192.168.100.1/24"
    virtual: bool = False
    roce_hcas: list[str] = field(default_factory=list)  # RDMA devices bound to this netdev
    wol: Optional[str] = None                           # "g" = magic packet, "d" = disabled, None = unknown

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def addr(self) -> Optional[str]:
        return self.ipv4[0].split("/")[0] if self.ipv4 else None


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def list_interfaces(sysfs: str = "/sys/class/net", run: Runner = run_cmd,
                    rdma: Optional[list[dict[str, Any]]] = None) -> list[Iface]:
    """Physical and virtual interfaces with MAC, link speed, IPv4 and bound RoCE devices."""
    root = Path(sysfs)
    if not root.is_dir():
        return []
    addrs: dict[str, list[str]] = {}
    rc, out = run(["ip", "-j", "-4", "addr", "show"], 8)
    if rc == 0 and out.strip():
        try:
            for item in json.loads(out):
                addrs[item["ifname"]] = [f"{a['local']}/{a['prefixlen']}" for a in item.get("addr_info", [])
                                         if a.get("family") == "inet"]
        except (ValueError, KeyError, TypeError):
            pass
    roce: dict[str, list[str]] = {}
    for d in rdma or []:
        for nd in d.get("netdevs", []):
            roce.setdefault(nd, []).append(d["hca"])
    ifaces = []
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        if p.name == "lo":
            continue
        virtual = not (p / "device").exists()
        speed = _read(p / "speed")
        mac = _read(p / "address")
        ifaces.append(Iface(
            name=p.name,
            mac=mac if mac and mac != "00:00:00:00:00:00" else None,
            state=_read(p / "operstate") or "unknown",
            speed_mbps=int(speed) if speed and speed.lstrip("-").isdigit() and int(speed) > 0 else None,
            ipv4=addrs.get(p.name, []),
            virtual=virtual,
            roce_hcas=sorted(roce.get(p.name, [])),
        ))
    return ifaces


def guess_qsfp(ifaces: list[Iface]) -> Optional[Iface]:
    """The interface most likely cabled to the other Spark.

    Prefers a link-up netdev that carries a RoCE device and an IPv4 address, then the fastest
    link-up physical port. Never returns Wi-Fi, Docker bridges or tunnels.
    """
    real = [i for i in ifaces if not i.virtual]

    def score(i: Iface) -> tuple:
        # equal candidates: prefer the first PCIe half (enp1s0f1np1) over the second (enP2p1s0f1np1)
        return (i.state == "up", bool(i.roce_hcas), bool(i.ipv4), i.speed_mbps or 0, "enP" not in i.name, i.name)

    cands = [i for i in real if i.roce_hcas or (i.speed_mbps or 0) >= 100_000]
    return max(cands, key=score) if cands else None


def tailscale_ip(ifaces: list[Iface]) -> Optional[str]:
    for i in ifaces:
        if i.name.startswith("tailscale") and i.addr:
            return i.addr
    return None


def peer_default(own_ip: Optional[str]) -> Optional[str]:
    """192.168.100.1 <-> 192.168.100.2 (the convention in the DGX Spark clustering guide)."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+)\.(\d+)", own_ip or "")
    if not m:
        return None
    last = int(m.group(2))
    return f"{m.group(1)}.{2 if last == 1 else 1 if last == 2 else last + 1 if last < 254 else 1}"


# ---- docker / python / hf cache ---------------------------------------------------------------
def docker_status(user: Optional[str] = None, run: Runner = run_cmd) -> dict[str, Any]:
    exe = shutil.which("docker")
    if not exe:
        return {"installed": False, "reachable": False, "in_group": False,
                "detail": "docker is not installed"}
    rc, out = run([exe, "version", "--format", "{{.Server.Version}}"], 10)
    in_group = False
    if user:
        rc2, groups = run(["id", "-nG", user], 5)
        in_group = rc2 == 0 and "docker" in groups.split()
    return {"installed": True, "reachable": rc == 0 and bool(out.strip()),
            "version": out.strip() or None, "in_group": in_group,
            "detail": f"server {out.strip()}" if rc == 0 and out.strip()
            else "docker is installed but the daemon is not reachable for this user"}


def hf_cache_candidates(home: Optional[str] = None, env: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
    """Existing Hugging Face homes, most likely first, with size info for the wizard to show."""
    env = env if env is not None else dict(os.environ)
    paths: list[str] = []
    if env.get("HF_HOME"):
        paths.append(env["HF_HOME"])
    if home:
        paths.append(str(Path(home) / ".cache" / "huggingface"))
    out, seen = [], set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        hub = Path(p) / "hub"
        models = [d.name for d in hub.glob("models--*")] if hub.is_dir() else []
        out.append({"path": p, "exists": Path(p).is_dir(), "models": len(models),
                    "writable": os.access(p, os.W_OK) if Path(p).exists() else None})
    return out


def python_ok() -> tuple[bool, str]:
    v = sys.version_info
    return v >= (3, 12), f"{v.major}.{v.minor}.{v.micro}"


def systemd_available(root: str = "/") -> bool:
    return Path(root, "run/systemd/system").is_dir()


def port_free(port: int, host: str = "0.0.0.0") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def user_home(user: str) -> Optional[str]:
    try:
        import pwd
        return pwd.getpwnam(user).pw_dir
    except (ImportError, KeyError):
        return None


def group_exists(name: str) -> bool:
    try:
        import grp
        grp.getgrnam(name)
        return True
    except (ImportError, KeyError):
        return False


def default_service_user(env: Optional[dict[str, str]] = None) -> str:
    """The person who ran ``sudo`` (their HF cache, docker group and SSH keys are what we need)."""
    env = env if env is not None else dict(os.environ)
    su = env.get("SUDO_USER")
    if su and su != "root":
        return su
    try:
        import getpass
        u = getpass.getuser()
    except Exception:  # noqa: BLE001
        u = "root"
    return u if u != "root" else "twinspark"


# ---- the host report used by the wizard and `tsm node doctor` ----------------------------------
@dataclass
class HostReport:
    hostname: str
    arch: str
    kernel: str
    python: str
    python_ok: bool
    systemd: bool
    docker: dict[str, Any]
    gpu: dict[str, Optional[str]]
    desktop: dict[str, Any]
    ifaces: list[Iface]
    rdma: list[dict[str, Any]]
    disk_free_gib: Optional[float]
    mem_total_gib: float
    privd_socket: bool
    ports: dict[int, bool]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def probe_host(user: Optional[str] = None, hf_path: Optional[str] = None,
               ports: tuple[int, ...] = (8000, 8100, 8443, 9443),
               sysfs_net: str = "/sys/class/net", sysfs_ib: str = "/sys/class/infiniband",
               run: Runner = run_cmd) -> HostReport:
    u = platform.uname()
    ok, pyv = python_ok()
    rdma = sysinfo.rdma_devices(sysfs_ib)
    mem = sysinfo.memory_snapshot()
    return HostReport(
        hostname=u.node, arch=u.machine, kernel=u.release, python=pyv, python_ok=ok,
        systemd=systemd_available(),
        docker=docker_status(user, run),
        gpu=sysinfo.nvidia_facts(),
        desktop=sysinfo.desktop_facts(),
        ifaces=list_interfaces(sysfs_net, run, rdma),
        rdma=rdma,
        disk_free_gib=sysinfo.disk_free_gib(hf_path) if hf_path else None,
        mem_total_gib=mem.get("mem_total_gib", 0.0),
        privd_socket=Path("/run/twinspark/privd.sock").exists(),
        ports={p: port_free(p) for p in ports},
    )


def check(results: list[dict[str, str]], name: str, status: str, detail: str, fix: str = "") -> None:
    results.append({"check": name, "status": status, "detail": detail, "fix": fix})


def host_checks(rep: HostReport, role: str = "agent") -> list[dict[str, str]]:
    """Pre-install findings (status: pass | info | warn | fail) with the fix for each."""
    res: list[dict[str, str]] = []
    check(res, "python", "pass" if rep.python_ok else "fail", f"Python {rep.python}",
          "" if rep.python_ok else "TwinSpark needs Python 3.12+ (Ubuntu 24.04 ships it: apt install python3.12-venv)")
    if rep.arch not in ("aarch64", "arm64"):
        check(res, "machine", "info", f"{rep.arch} (not a DGX Spark / GB10, which is aarch64)",
              "fine for testing with `tsm demo`; hardware features will report unavailable")
    d = rep.docker
    if not d["installed"]:
        check(res, "docker", "fail", d["detail"], "install Docker (DGX OS ships it) and retry")
    elif not d["reachable"]:
        check(res, "docker", "warn", d["detail"],
              "add the service user to the docker group: sudo usermod -aG docker <user> (then re-login)")
    else:
        check(res, "docker", "pass", d["detail"])
    if role in ("controller", "agent"):
        missing = [b for b in ("ssh", "rsync") if not shutil.which(b)]
        if missing:
            check(res, "sync tools", "warn", f"missing: {', '.join(missing)}",
                  "copying model files to the other Spark needs them: sudo apt install openssh-client rsync")
        else:
            check(res, "sync tools", "pass", "ssh and rsync available")
    if rep.gpu.get("driver_version"):
        check(res, "gpu driver", "pass", f"{rep.gpu.get('gpu_name') or 'GPU'} driver {rep.gpu['driver_version']}")
    else:
        check(res, "gpu driver", "info", "no NVIDIA driver detected here",
              "expected on a dev machine; on a Spark check `nvidia-smi`")
    if rep.desktop.get("desktop_running"):
        check(res, "desktop", "warn",
              f"graphical session running ({rep.desktop.get('desktop_rss_gib', 0):.1f} GiB held)",
              "after setup: tsm headless headless-max   (frees that memory for the KV cache)")
    else:
        check(res, "desktop", "pass", "headless (no graphical session)")
    if not rep.systemd:
        check(res, "systemd", "warn", "systemd not detected — units will be written but not started",
              "run the services another way, or use `tsm demo` to try the GUI")
    if role in ("controller", "single"):
        for port, label in ((8000, "gateway"), (8443, "management API")):
            if not rep.ports.get(port, True):
                check(res, f"port {port}", "warn", f"already in use ({label} default)",
                      "setup will ask for a different port")
    if not rep.ports.get(9443, True):
        check(res, "port 9443", "warn", "already in use (agent default)", "pick another agent port")
    if not rep.ports.get(8100, True):
        check(res, "port 8100", "warn", "already in use — a hand-started vLLM?",
              "TwinSpark's internal vLLM port; stop the other server or choose another port")
    if rep.disk_free_gib is not None:
        st = "pass" if rep.disk_free_gib > 250 else "warn" if rep.disk_free_gib > 50 else "fail"
        check(res, "disk", st, f"{rep.disk_free_gib:.0f} GiB free for model files",
              "" if st == "pass" else "large models need hundreds of GiB; free space or point hf_cache_dir elsewhere")
    return res
