"""``tsm setup`` / ``tsm join-code`` / ``tsm go-live`` — the quick-start path.

The wizard asks only what it cannot detect, shows what it found, and prints the one
command to run on the other node. ``--yes`` accepts every detected default so the same
code path serves unattended installs and the tests.
"""

from __future__ import annotations

import getpass
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import yaml

from . import __version__, hostprobe, provision
from .agent import sysinfo
from .provision import Answers, Layout, Provisioner, SetupError

_ICON = {"pass": "✓", "info": "·", "warn": "!", "fail": "✗"}


class Console:
    """Prompts with defaults. ``yes`` makes every question answer itself (and say so)."""

    def __init__(self, yes: bool = False, input_fn: Optional[Callable[[str], str]] = None,
                 secret_fn: Optional[Callable[[str], str]] = None,
                 out: Callable[..., None] = print):
        # resolved at call time so tests (and wrappers) can replace input()/getpass()
        self.yes, self.out = yes, out
        self._in = input_fn or (lambda prompt: input(prompt))
        self._secret = secret_fn or (lambda prompt: getpass.getpass(prompt))

    def say(self, text: str = "") -> None:
        self.out(text)

    def head(self, text: str) -> None:
        self.out(f"\n\033[1m{text}\033[0m" if sys.stdout.isatty() else f"\n== {text}")

    def ask(self, question: str, default: Optional[str] = None,
            validate: Optional[Callable[[str], Optional[str]]] = None) -> str:
        while True:
            if self.yes:
                if default is None:
                    raise SetupError(f"{question} — no default available, pass it as a flag")
                self.out(f"  {question}: {default}")
                return default
            suffix = f" [{default}]" if default not in (None, "") else ""
            ans = self._in(f"  {question}{suffix}: ").strip() or (default or "")
            problem = validate(ans) if validate else None
            if problem:
                self.out(f"    ✗ {problem}")
                continue
            return ans

    def confirm(self, question: str, default: bool = True) -> bool:
        if self.yes:
            self.out(f"  {question}: {'yes' if default else 'no'}")
            return default
        ans = self._in(f"  {question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not ans else ans.startswith("y")

    def choose(self, question: str, options: list[tuple[str, str]], default: int = 0) -> str:
        self.out(f"  {question}")
        for i, (_, label) in enumerate(options, 1):
            self.out(f"    {i}) {label}{'   (default)' if i - 1 == default else ''}")
        if self.yes:
            self.out(f"  → {options[default][1]}")
            return options[default][0]
        while True:
            ans = self._in(f"  choose 1-{len(options)} [{default + 1}]: ").strip()
            if not ans:
                return options[default][0]
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                return options[int(ans) - 1][0]
            self.out("    ✗ enter one of the numbers above")

    def secret(self, question: str) -> str:
        if self.yes:
            return ""
        return self._secret(f"  {question} (Enter to skip): ").strip()


def _need_root(args) -> None:
    if args.root != "/" or os.geteuid() == 0 or args.dry:
        return
    print("Setup writes /etc/twinspark and installs services, so it needs root — re-running with sudo.")
    argv = ["sudo", sys.executable, "-m", "twinspark.cli", *sys.argv[1:]]
    try:
        os.execvp("sudo", argv)
    except OSError as exc:
        sys.exit(f"could not run sudo ({exc}). Run: sudo tsm {' '.join(sys.argv[1:])}")


def _iface_label(i: hostprobe.Iface) -> str:
    speed = f"{i.speed_mbps // 1000}G" if i.speed_mbps and i.speed_mbps >= 1000 else (
        f"{i.speed_mbps}M" if i.speed_mbps else "?")
    extra = f", RoCE {','.join(i.roce_hcas)}" if i.roce_hcas else ""
    return f"{i.name:16} {i.state:5} {speed:>4}  {i.addr or 'no IPv4':15}{extra}"


def _print_checks(con: Console, checks: list[dict[str, str]]) -> None:
    for c in checks:
        con.say(f"  {_ICON.get(c['status'], '?')} {c['check']:14} {c['detail']}")
        if c.get("fix") and c["status"] in ("warn", "fail"):
            for line in c["fix"].splitlines():
                con.say(f"    → {line}")


def gather_controller(args, con: Console, rep: hostprobe.HostReport, a: Answers) -> None:
    """Questions for node A (or a single machine)."""
    real = [i for i in rep.ifaces if not i.virtual]
    guess = hostprobe.guess_qsfp(rep.ifaces)
    con.head("1/4  The QSFP link between the two Sparks")
    if args.qsfp_iface:
        iface = next((i for i in rep.ifaces if i.name == args.qsfp_iface), None)
        a.qsfp_iface = args.qsfp_iface
    elif real:
        opts = [(i.name, _iface_label(i)) for i in real]
        default = next((n for n, (v, _) in enumerate(opts) if guess and v == guess.name), 0)
        a.qsfp_iface = con.choose("Which interface is cabled to the other Spark?", opts, default)
        iface = next(i for i in rep.ifaces if i.name == a.qsfp_iface)
    else:
        iface = None
        a.qsfp_iface = con.ask("QSFP interface name", "enp1s0f1np1")
    a.qsfp_ip = args.qsfp_ip or (iface.addr if iface and iface.addr else None)
    if a.role == "single":
        a.qsfp_ip = a.qsfp_ip or "127.0.0.1"
    else:
        if not a.qsfp_ip:
            a.qsfp_ip = con.ask("This node's address on the QSFP link", "192.168.100.1")
            con.say("    ! that interface has no IPv4 address yet; setup does NOT change your network.")
            con.say("      Put this in a netplan file and apply it yourself:")
            for line in provision.netplan_snippet(a.qsfp_iface or "enp1s0f1np1", a.qsfp_ip).splitlines():
                con.say(f"        {line}")
        else:
            con.say(f"  This node: {a.qsfp_ip}")
        a.peer_ip = args.peer_ip or con.ask("Other Spark's address on the link",
                                            hostprobe.peer_default(a.qsfp_ip) or "192.168.100.2")
        a.peer_iface = a.qsfp_iface
    sug = sysinfo.suggest_rdma(rep.rdma, a.qsfp_ip) if a.role != "single" else {"hcas": [], "gid_index": None}
    if sug["hcas"]:
        a.rdma_hcas, a.ib_gid_index = sug["hcas"], sug["gid_index"]
        con.say(f"  RDMA (RoCE): {', '.join(sug['hcas'])}  GID index {sug['gid_index']}")
        if sug.get("note"):
            con.say(f"    ! {sug['note']}")
    elif a.role != "single":
        con.say("  ! no active RoCE device found on that subnet — NCCL would fall back to TCP.")
        con.say("    After the link is up run `tsm rdma --apply` to fill this in.")

    con.head("2/4  Who runs it, and where the models live")
    a.service_user = args.service_user or con.ask("Run TwinSpark as user", hostprobe.default_service_user(),
                                                  _user_check)
    a.create_user = args.create_user or (not Layout(Path(args.root)).sandbox and not _user_exists(a.service_user))
    if a.role != "single":
        a.peer_ssh_user = args.peer_ssh_user or con.ask("User on the other Spark (receives model files over ssh)",
                                                         a.service_user, _user_check)
    home = hostprobe.user_home(a.service_user) or str(Path.home())
    cands = [c for c in hostprobe.hf_cache_candidates(home) if c["exists"]]
    default_hf = cands[0]["path"] if cands else str(Layout(Path(args.root)).state / "hf-cache")
    if cands:
        con.say(f"  Found a Hugging Face cache: {cands[0]['path']} ({cands[0]['models']} models) — reusing it.")
    a.hf_cache_dir = args.hf_cache_dir or con.ask("Model files (Hugging Face home)", default_hf)

    con.head("3/4  How you will reach it")
    ts = hostprobe.tailscale_ip(rep.ifaces)
    if args.mgmt_bind:
        a.mgmt_bind = args.mgmt_bind
    else:
        opts = [("127.0.0.1", "this machine only — reach it with an SSH tunnel (safest)")]
        if ts:
            opts.append((ts, f"Tailscale address {ts} (encrypted by Tailscale)"))
        opts.append(("0.0.0.0", "every network interface (LAN-visible; only with a strong key)"))
        a.mgmt_bind = con.choose("Where should the web GUI / management API listen?", opts, 0)
    a.mgmt_port = int(args.mgmt_port or _free_port(rep, 8443, con, "management API"))
    a.gateway_port = int(args.gateway_port or _free_port(rep, 8000, con, "OpenAI gateway"))
    a.vllm_port = int(args.vllm_port or _free_port(rep, 8100, con, "internal vLLM"))

    con.head("4/4  Safety")
    mode = args.runtime_mode or con.choose(
        "Start in dry-run (nothing is run on this machine) or with real containers?",
        [("dry-run", "dry-run — simulates containers; switch later with `sudo tsm go-live` (recommended)"),
         ("docker", "docker — real containers right away")], 0)
    a.runtime_mode = mode
    gather_remote(args, con, a)
    if args.hf_token_file:
        a.hf_token = Path(args.hf_token_file).read_text().strip()
    elif not con.yes:
        a.hf_token = con.secret("Hugging Face token for gated models") or None


def gather_remote(args, con: Console, a: Answers) -> None:
    """Optional remote management for a headless node. Everything stays OFF unless chosen here."""
    from .cli_remote import parse_features

    con.head("Remote management (optional)")
    con.say("  Both Sparks run headless. These switches let you fix problems without a monitor:")
    con.say("  a shell in the GUI, reboot / power-off, boot once from the network, Wake-on-LAN.")
    con.say("  They are OFF by default and decided by a root-owned file on this machine, so nothing")
    con.say("  but you (with sudo) can switch them on. Change them any time: sudo tsm remote enable …")
    if args.remote is not None:
        try:
            chosen = parse_features(args.remote)
        except SetupError as exc:
            sys.exit(f"error: {exc}")
    elif con.yes:
        chosen = []
        con.say("  → off (pass --remote terminal,reboot,… or --remote all to enable some)")
    else:
        pick = con.choose("What should be switched on for this node?", [
            ("none", "nothing for now (safest; enable later with `sudo tsm remote enable …`)"),
            ("terminal", "terminal only — a recorded shell from the GUI or `tsm remote terminal`"),
            ("terminal,reboot,poweroff,boot-next", "terminal + reboot / power-off / boot from network once"),
            ("all", "everything, including turning Wake-on-LAN on")], 0)
        chosen = parse_features(pick)
    a.remote = {f: True for f in chosen}
    con.say("  remote management: " + (", ".join(f.replace("_", "-") for f in chosen) or "off"))


def _user_check(v: str) -> Optional[str]:
    return None if provision._USER_RE.match(v) else "use a lowercase Linux user name"


def _user_exists(name: str) -> bool:
    try:
        import pwd
        pwd.getpwnam(name)
        return True
    except (ImportError, KeyError):
        return False


def _free_port(rep: hostprobe.HostReport, port: int, con: Console, label: str) -> int:
    if rep.ports.get(port, True):
        return port
    alt = next((p for p in range(port + 1, port + 50) if hostprobe.port_free(p)), port + 1)
    con.say(f"  ! port {port} is already in use (a hand-started vLLM?). {label} will use {alt} instead.")
    return alt


def gather_agent(args, con: Console, rep: hostprobe.HostReport, a: Answers, info: dict[str, Any]) -> None:
    con.head("Node B — values come from the join code")
    provision.apply_join(a, info)
    con.say(f"  This node: {a.qsfp_ip}   Node A: {a.peer_ip}   runtime: {a.runtime_mode}")
    ifs = {i.name: i for i in rep.ifaces}
    mine = next((i for i in rep.ifaces if a.qsfp_ip and a.qsfp_ip in [x.split('/')[0] for x in i.ipv4]), None)
    if mine:
        a.qsfp_iface = mine.name
        con.say(f"  ✓ {a.qsfp_ip} is configured on {mine.name}")
    else:
        con.say(f"  ! no interface has {a.qsfp_ip} yet; setup does NOT change your network.")
        g = hostprobe.guess_qsfp(rep.ifaces)
        iface = (args.qsfp_iface or (g.name if g else None) or a.qsfp_iface or "enp1s0f1np1")
        a.qsfp_iface = iface
        for line in provision.netplan_snippet(iface, a.qsfp_ip or "192.168.100.2").splitlines():
            con.say(f"      {line}")
    if a.qsfp_iface in ifs:
        sug = sysinfo.suggest_rdma(rep.rdma, a.qsfp_ip)
        if sug["hcas"]:
            a.rdma_hcas, a.ib_gid_index = sug["hcas"], sug["gid_index"]
    a.service_user = args.service_user or con.ask("Run TwinSpark as user", info.get("b_user") or
                                                  hostprobe.default_service_user(), _user_check)
    a.create_user = args.create_user or (not Layout(Path(args.root)).sandbox and not _user_exists(a.service_user))
    if a.service_user != info.get("b_user"):
        con.say(f"  ! node A will copy files as '{info.get('b_user')}' but you chose '{a.service_user}'. "
                f"Edit nodes.B.ssh_user on node A or pick '{info.get('b_user')}'.")
    home = hostprobe.user_home(a.service_user) or str(Path.home())
    cands = [c for c in hostprobe.hf_cache_candidates(home) if c["exists"]]
    default_hf = cands[0]["path"] if cands else str(Layout(Path(args.root)).state / "hf-cache")
    if cands:
        con.say(f"  Found a Hugging Face cache: {cands[0]['path']} ({cands[0]['models']} models) — reusing it.")
    a.hf_cache_dir = args.hf_cache_dir or con.ask("Model files (Hugging Face home)", default_hf)
    if args.runtime_mode:
        a.runtime_mode = args.runtime_mode
    gather_remote(args, con, a)
    if args.hf_token_file:
        a.hf_token = Path(args.hf_token_file).read_text().strip()
    elif not con.yes:
        a.hf_token = con.secret("Hugging Face token (only needed here if node B downloads models)") or None


def _wait_health(url: str, headers: dict[str, str], seconds: float = 25.0) -> Optional[dict]:
    end = time.time() + seconds
    while time.time() < end:
        try:
            r = httpx.get(url, headers=headers, timeout=3)
            if r.status_code == 200:
                return r.json()
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return None


def cmd_setup(args, api=None):
    _need_root(args)
    lay = Layout(Path(args.root))
    con = Console(yes=args.yes)
    a = Answers()
    a.hostname = hostprobe.platform.node()
    a.docker_group = hostprobe.group_exists("docker")
    info: Optional[dict[str, Any]] = None
    if args.join:
        try:
            info = provision.decode_join(args.join)
        except SetupError as exc:
            sys.exit(f"error: {exc}")
        a.role, a.node_id = "agent", "B"
    elif args.single:
        a.role = "single"
    else:
        a.role, a.node_id = "controller", "A"
    title = {"controller": "node A (controller + agent)", "agent": "node B (agent)",
             "single": "single Spark (controller + agent)"}[a.role]
    con.say(f"TwinSpark Manager {__version__} — setting up {title}"
            + (f"   [sandbox: {lay.root}]" if lay.sandbox else "")
            + ("   [dry run: nothing is written]" if args.dry else ""))

    con.head("Checking this machine")
    default_user = args.service_user or hostprobe.default_service_user()
    rep = hostprobe.probe_host(user=default_user)
    checks = hostprobe.host_checks(rep, a.role)
    _print_checks(con, checks)
    if any(c["status"] == "fail" for c in checks) and not args.skip_checks:
        sys.exit("\nFix the ✗ items above and run again (or pass --skip-checks to continue anyway).")

    try:
        if a.role == "agent":
            gather_agent(args, con, rep, a, info or {})
        else:
            gather_controller(args, con, rep, a)
        a.validate()
    except SetupError as exc:
        sys.exit(f"error: {exc}")

    con.head("Summary")
    con.say(f"  role            {title}")
    con.say(f"  runs as         {a.service_user}")
    con.say(f"  QSFP            {a.qsfp_iface} {a.qsfp_ip}" + (f" ↔ {a.peer_ip}" if a.peer_ip else ""))
    con.say(f"  model files     {a.hf_cache_dir}")
    if a.role != "agent":
        con.say(f"  web GUI         http://{a.mgmt_bind}:{a.mgmt_port}/   gateway :{a.gateway_port}")
    con.say(f"  mode            {a.runtime_mode}")
    con.say("  remote          " + (", ".join(k.replace("_", "-") for k, v in a.remote.items() if v) or "off"))
    if not con.confirm("Write the configuration and start the services?", True):
        sys.exit("aborted — nothing was changed.")

    pv = Provisioner(a, lay, dry=args.dry, force=args.force, systemd=not args.no_start)
    try:
        steps = pv.apply()
    except SetupError as exc:
        for s in pv.steps:
            con.say(f"  {s.name:16} {s.status:8} {s.detail}")
        sys.exit(f"\nerror: {exc}")
    con.head("Done" if not args.dry else "Plan (nothing was changed)")
    for s in steps:
        mark = {"done": "✓", "kept": "·", "skipped": "-", "failed": "✗", "planned": "→"}[s.status]
        con.say(f"  {mark} {s.name:16} {s.detail}")
    if args.dry:
        return
    _after(args, con, lay, a, pv)


def _after(args, con: Console, lay: Layout, a: Answers, pv: Provisioner) -> None:
    v = pv.vault
    live = pv.systemd and not any(s.status == "failed" for s in pv.steps)
    if a.role == "agent":
        if live:
            tok = v.get("agent_token")
            h = _wait_health(f"http://{a.qsfp_ip}:{a.agent_port}/v1/actions", {"Authorization": f"Bearer {tok}"}, 20)
            con.say("  ✓ agent is answering" if h else "  ! agent did not answer yet — journalctl -u twinspark-agent")
        con.head("Next")
        con.say("  Back on node A:   tsm doctor")
        if not a.remote.get("terminal"):
            con.say("  Headless? Shell, reboot and logs from the GUI:  sudo tsm remote enable terminal")
        pub = getattr(pv, "pubkey", None)
        if pub:
            con.say("  Only if you ever set download_node: B (B downloads, A receives), authorise this key on node A:")
            con.say(f"    {pub}")
        return
    code = None
    if a.role == "controller":
        code = provision.make_join(a, v, getattr(pv, "pubkey", None))
    if live:
        host = "127.0.0.1" if a.mgmt_bind in ("0.0.0.0", "::") else a.mgmt_bind
        h = _wait_health(f"http://{host}:{a.mgmt_port}/api/v1/health", {}, 25)
        con.say("  ✓ controller is up" if h else "  ! controller not answering — journalctl -u twinspark-controller")
    con.head("Next")
    n = 1
    if code:
        con.say(f"  {n}. On the OTHER Spark run (this code contains secrets — it expires when you rotate keys):")
        con.say(f"       sudo tsm setup --join {code}")
        con.say("       (no tsm there yet?  git clone … && sudo ./install.sh --join <code>)")
        n += 1
    key = v.get("management_api_key")
    host = a.hostname or "this-machine"
    if a.mgmt_bind in ("127.0.0.1", "::1", "localhost"):
        con.say(f"  {n}. On your laptop:  ssh -L {a.mgmt_port}:localhost:{a.mgmt_port} {a.service_user}@{host}"
                f"   then open http://localhost:{a.mgmt_port}/")
    else:
        con.say(f"  {n}. Open http://{a.mgmt_bind if a.mgmt_bind != '0.0.0.0' else host}:{a.mgmt_port}/")
    n += 1
    con.say(f"  {n}. The GUI asks for the management key:  {key}")
    con.say("       (print it again any time with `tsm init --show`)")
    n += 1
    con.say(f"  {n}. Follow the 'Get started' page in the GUI, or:  tsm doctor")
    if not a.remote.get("terminal"):
        n += 1
        con.say(f"  {n}. Headless? A shell, reboot and logs from the GUI:  sudo tsm remote enable terminal   "
                f"(on each node; docs/remote-management.md)")
    if a.runtime_mode == "dry-run":
        con.say("\n  Running in dry-run: activations are simulated. When `tsm plan <profile>` looks right:")
        con.say("       sudo tsm go-live        (run on each node)")


def cmd_join_code(args, api=None):
    """Print the join code for node B again (regenerated from config + vault)."""
    lay = Layout(Path(args.root))
    if not lay.controller_yaml.exists():
        sys.exit(f"{lay.controller_yaml} not found — run this on node A")
    from .schemas.config import ControllerConfig, load_config
    from .security import SecretsVault
    cfg = load_config(lay.controller_yaml, ControllerConfig)
    vault = SecretsVault(cfg.secrets_dir)
    a, b = cfg.nodes.get("A"), cfg.nodes.get("B")
    if not b:
        sys.exit("this is a single-node install — there is no node B to join")
    ans = Answers(role="controller", qsfp_ip=a.qsfp_ip, peer_ip=b.qsfp_ip, qsfp_iface=a.qsfp_iface,
                  peer_iface=b.qsfp_iface, peer_ssh_user=b.ssh_user or "twinspark",
                  rdma_hcas=a.rdma_hcas, ib_gid_index=a.ib_gid_index,
                  agent_port=int(b.agent_url.rsplit(":", 1)[-1].rstrip("/")), vllm_port=cfg.runtime.vllm_port)
    pub = Path(str(cfg.runtime.ssh_key) + ".pub") if cfg.runtime.ssh_key else None
    ans.runtime_mode = "dry-run"
    try:
        agent_cfg = yaml.safe_load(lay.agent_yaml.read_text()) if lay.agent_yaml.exists() else {}
        ans.runtime_mode = (agent_cfg or {}).get("runtime_mode", "dry-run")
    except (OSError, yaml.YAMLError):
        pass
    pubkey = pub.read_text().strip() if pub and pub.exists() else None
    print(f"sudo tsm setup --join {provision.make_join(ans, vault, pubkey)}")


def cmd_go_live(args, api=None):
    """Switch this node's agent between dry-run and real containers."""
    lay = Layout(Path(args.root))
    path = Path(args.config) if args.config else lay.agent_yaml
    if not path.exists():
        sys.exit(f"{path} not found — run `sudo tsm setup` first")
    target = "dry-run" if args.revert else "docker"
    if os.geteuid() != 0 and lay.root == Path("/"):
        os.execvp("sudo", ["sudo", sys.executable, "-m", "twinspark.cli", *sys.argv[1:]])
    if target == "docker":
        d = hostprobe.docker_status(hostprobe.default_service_user())
        if not d["reachable"] and not args.force:
            sys.exit(f"docker is not usable here: {d['detail']}\n(use --force to switch anyway)")
        try:
            from .agent.runtime import DockerRuntime
            foreign = DockerRuntime("docker").list_foreign()
        except Exception:  # noqa: BLE001 - docker missing/denied: reported above
            foreign = []
        if foreign:
            names = ", ".join(c.get("name", "?") if isinstance(c, dict) else getattr(c, "name", "?") for c in foreign)
            print(f"! inference containers are running outside TwinSpark: {names}")
            print("  They hold GPU memory; stop them (or `tsm foreign stop`) before the first activation.")
    changed = provision.set_runtime_mode(path, target)
    print(f"{path}: runtime_mode: {target}" + ("" if changed else "  (already set)"))
    if Layout(Path(args.root)).sandbox or args.no_restart:
        print("restart the agent to apply: sudo systemctl restart twinspark-agent")
        return
    rc, out = provision._real_run(["systemctl", "restart", "twinspark-agent"])
    print("agent restarted" if rc == 0 else f"could not restart the agent: {out}\n"
          "restart it yourself: sudo systemctl restart twinspark-agent")
    if target == "docker":
        print("Next: tsm doctor   →   tsm activate <profile>")


def local(fn):
    """Mark a command that works on this machine only (no management API / key needed)."""
    fn.local = True
    return fn


for _fn in (cmd_setup, cmd_join_code, cmd_go_live):
    local(_fn)


def add_parsers(sub, cmd) -> None:
    s = cmd("setup", cmd_setup, "guided first-run setup (config, secrets, services); --join for node B")
    s.add_argument("--join", metavar="CODE", help="node B: the join code printed by node A")
    s.add_argument("--single", action="store_true", help="one Spark only (controller + agent)")
    s.add_argument("-y", "--yes", action="store_true", help="accept every detected default (unattended)")
    s.add_argument("--dry", action="store_true", help="show what would be written; change nothing")
    s.add_argument("--force", action="store_true", help="regenerate existing config files (backups are kept)")
    s.add_argument("--root", default="/", help="write everything under this directory (testing / staging)")
    s.add_argument("--no-start", action="store_true", help="write files but do not enable/start services")
    s.add_argument("--skip-checks", action="store_true")
    s.add_argument("--service-user")
    s.add_argument("--create-user", action="store_true")
    s.add_argument("--qsfp-iface")
    s.add_argument("--qsfp-ip")
    s.add_argument("--peer-ip")
    s.add_argument("--peer-ssh-user")
    s.add_argument("--hf-cache-dir")
    s.add_argument("--hf-token-file")
    s.add_argument("--mgmt-bind")
    s.add_argument("--mgmt-port", type=int)
    s.add_argument("--gateway-port", type=int)
    s.add_argument("--vllm-port", type=int)
    s.add_argument("--runtime-mode", choices=["dry-run", "docker"])
    s.add_argument("--remote", metavar="FEATURES",
                   help="remote management to switch on: terminal,reboot,poweroff,boot-next,wol | all | none "
                        "(default: ask; off when unattended)")

    s = cmd("join-code", cmd_join_code, "print the node-B join command again (node A)")
    s.add_argument("--root", default="/")

    s = cmd("go-live", cmd_go_live, "switch this node's agent from dry-run to real containers")
    s.add_argument("--revert", action="store_true", help="back to dry-run")
    s.add_argument("--config")
    s.add_argument("--root", default="/")
    s.add_argument("--force", action="store_true")
    s.add_argument("--no-restart", action="store_true")
