"""Remote management on the command line.

* ``tsm remote …``   — through the controller: status, terminal, logs, support bundle, power,
                       Wake-on-LAN, smart plug, recordings. Also ``enable`` / ``disable``, which edit
                       THIS machine's root-owned policy (run them on the node you are changing).
* ``tsm node …``     — on a Spark itself, WITHOUT the controller: status, doctor, logs, bundle,
                       reboot, power-off, boot-once-from-network, Wake-on-LAN. This is what you use
                       from an SSH session when the controller or the agent is the thing that is broken.
* ``tsm wake MAC``   — send a magic packet from this machine.
* ``tsm netboot …``  — plan or run a short-lived PXE helper for one machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from . import hostprobe
from .provision import Layout, SetupError, _real_run, installed_terminal_unit
from .remote import netboot, privops, termclient
from .remote import policy as pol
from .remote.policy import FEATURE_HELP, FEATURES

UNIT = "twinspark-terminal"


# ---- shared helpers ---------------------------------------------------------------------------
def parse_features(text: str) -> list[str]:
    """``terminal,boot-next`` / ``all`` / ``none`` -> feature names in policy order."""
    names: list[str] = []
    for part in re.split(r"[,\s]+", (text or "").strip().lower()):
        if not part or part == "none":
            continue
        if part == "all":
            names.extend(FEATURES)
            continue
        name = part.replace("-", "_")
        if name not in FEATURES:
            raise SetupError(f"unknown remote feature '{part}' (known: "
                             f"{', '.join(f.replace('_', '-') for f in FEATURES)}, all, none)")
        names.append(name)
    return [f for f in FEATURES if f in names]


def _dash(feature: str) -> str:
    return feature.replace("_", "-")


def _typed_confirmation(phrase: str, warning: str, yes: bool) -> str:
    """The phrase the API wants. ``--yes`` supplies it; otherwise the operator has to type it."""
    if yes:
        return phrase
    if not sys.stdin.isatty():
        sys.exit(f"{warning}\nThis needs a typed confirmation; pass --yes to confirm non-interactively.")
    print(warning)
    got = input(f"Type '{phrase}' to continue (anything else cancels): ").strip()
    if got != phrase:
        sys.exit("cancelled — nothing was changed.")
    return phrase


def _yn(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        sys.exit(f"{question}\nPass --yes to confirm non-interactively.")
    return input(f"{question} [y/N]: ").strip().lower().startswith("y")


def _uptime(sec: Optional[float]) -> str:
    if not sec:
        return "-"
    d, rem = divmod(int(sec), 86400)
    h, m = divmod(rem // 60, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m"


def _sudo_again(args) -> None:
    """Re-run this command with sudo when it needs root (never in a sandbox root or a dry run)."""
    if args.root != "/" or getattr(args, "dry", False):
        return
    if not hasattr(os, "geteuid"):
        sys.exit("Changing the local node policy needs Linux. Use `tsm remote` to manage a Spark from this machine.")
    if os.geteuid() == 0:
        return
    print("This changes root-owned files, so it needs root — re-running with sudo.")
    try:
        os.execvp("sudo", ["sudo", sys.executable, "-m", "twinspark.cli", *sys.argv[1:]])
    except OSError as exc:
        sys.exit(f"could not run sudo ({exc}). Run: sudo tsm {' '.join(sys.argv[1:])}")


# ---- tsm remote enable / disable / policy (this machine) ---------------------------------------
def _systemctl(*argv: str) -> tuple[int, str]:
    return _real_run(["systemctl", *argv])


def _terminal_unit(lay: Layout, enable: bool) -> list[str]:
    unit = lay.systemd / f"{UNIT}.service"
    notes: list[str] = []
    if enable:
        if not unit.exists():
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(installed_terminal_unit(lay))
            unit.chmod(0o644)
            notes.append(f"installed {unit}")
        if lay.sandbox:
            notes.append("sandbox: service not started")
        else:
            _systemctl("daemon-reload")
            rc, out = _systemctl("enable", "--now", UNIT)
            notes.append("terminal service started" if rc == 0 else f"could not start the terminal service: {out}")
    elif unit.exists() and not lay.sandbox:
        rc, out = _systemctl("disable", "--now", UNIT)
        notes.append("terminal service stopped (open shells ended)" if rc == 0 else
                     f"could not stop the terminal service: {out}")
    return notes


def cmd_remote_switch(args, enable: bool) -> None:
    try:
        feats = parse_features(" ".join(args.features))
    except SetupError as exc:
        sys.exit(f"error: {exc}")
    lay = Layout(Path(args.root))
    path = lay.etc / "remote-policy.json"
    if not feats:
        return cmd_remote_policy(args)
    _sudo_again(args)
    verb = "enable" if enable else "disable"
    for f in feats:
        print(f"  {verb:7} {_dash(f):10} {FEATURE_HELP[f]}")
    if enable and "terminal" in feats:
        print("\n  The terminal gives anyone who holds the management key a shell as the TwinSpark user on this\n"
              "  node (recorded under /var/lib/twinspark/terminal). Keep the GUI on localhost, an SSH tunnel\n"
              "  or Tailscale.")
    if getattr(args, "dry", False):
        print("\n(dry run: nothing was changed)")
        return
    try:
        new = pol.write_policy(path, {f: enable for f in feats}, chown_root=not lay.sandbox)
    except (OSError, ValueError) as exc:
        sys.exit(f"could not write {path}: {exc}")
    notes = _terminal_unit(lay, enable) if "terminal" in feats else []
    for n in notes:
        print(f"  - {n}")
    print(f"\n{path}\n  now enabled: {', '.join(_dash(f) for f in new.enabled) or 'nothing'}")
    if enable and "terminal" in feats and "terminal service started" in " ".join(notes):
        print("  Open the Remote page in the GUI, or:  tsm remote terminal <node>")


def cmd_remote_policy(args) -> None:
    lay = Layout(Path(args.root))
    path = lay.etc / "remote-policy.json"
    p = pol.load_policy(path, require_root=not lay.sandbox)
    print(f"{path}" + ("" if p.present else "  (missing: everything is off)"))
    if p.error:
        print(f"  ! ignored, everything is off: {p.error}")
    for f in FEATURES:
        print(f"  {'on ' if p.allows(f) else 'off'}  {_dash(f):10} {FEATURE_HELP[f]}")
    off = [f for f in FEATURES if not p.allows(f)]
    if off:
        print(f"\nTurn one on:  sudo tsm remote enable {_dash(off[0])}      (all: sudo tsm remote enable all)")


def cmd_remote_plug_token(args) -> None:
    from .security import SecretsVault

    if args.from_file:
        token = Path(args.from_file).read_text().strip()
    elif not sys.stdin.isatty():
        token = sys.stdin.read().strip()
    else:
        token = getpass.getpass("Smart-plug token (input hidden): ").strip()
    if not token or "\n" in token or len(token) > 512:
        sys.exit("the token must be one non-empty line of at most 512 characters")
    try:
        SecretsVault(args.secrets_dir).set("plug_token", token)
    except PermissionError:
        sys.exit(f"cannot write the vault in {args.secrets_dir}: run with sudo, or as the TwinSpark user")
    print("stored as the vault slot 'plug_token' (encrypted). Use it in controller.yaml as ${secret:plug_token}.")


# ---- tsm remote … (through the controller) ----------------------------------------------------
def _remote(api, method: str, path: str, **kw) -> Any:
    return api(method, f"/api/v1/remote{path}", **kw)


def cmd_remote_status(args, api) -> None:
    ov = _remote(api, "GET", "/overview")
    if api.as_json:
        print(json.dumps(ov, indent=2, default=str))
        return
    off_hint: set[str] = set()
    for name, n in ov["nodes"].items():
        me = " (controller)" if name == ov["controller_node"] else ""
        if not n.get("reachable"):
            print(f"node {name}{me}: NOT REACHABLE — {n.get('error', 'no answer')}")
            print(f"    why? tsm remote reach {name}")
        else:
            print(f"node {name}{me}: {n.get('hostname', '?')}   up {_uptime(n.get('uptime_s'))}   "
                  f"agent {n.get('version', '?')}   helper {'yes' if n.get('privd') else 'NO'}")
            on = n.get("enabled", [])
            off = [f for f in FEATURES if f not in on]
            print(f"    on : {', '.join(_dash(f) for f in on) or '-'}")
            print(f"    off: {', '.join(_dash(f) for f in off) or '-'}")
            off_hint.update(off)
            if "terminal" in on:
                print(f"    terminal service: {'running' if n.get('terminal_service') else 'NOT RUNNING'}")
            if n["policy"].get("error"):
                print(f"    ! policy ignored: {n['policy']['error']}")
            if n.get("power_pending"):
                print(f"    ! pending: {', '.join(n['power_pending'])} (cancel: tsm remote cancel {name})")
        oob = [x for x, ok in (("Wake-on-LAN", n.get("wake_configured")), ("smart plug", n.get("plug_configured")))
               if ok]
        print(f"    out-of-band power: {', '.join(oob) or 'none configured (docs/remote-management.md)'}")
    if ov.get("active"):
        print(f"\nactive model: {ov['active']}")
    if off_hint:
        print("\nOn a node, switch a feature on with:  sudo tsm remote enable <feature>   "
              f"({', '.join(_dash(f) for f in FEATURES)})")


def cmd_remote_reach(args, api) -> None:
    r = _remote(api, "GET", f"/{args.node}/reach")
    if api.as_json:
        print(json.dumps(r, indent=2, default=str))
        return
    mark = {"ok": "ok", "agent_error": "PROBLEM", "agent_down": "PROBLEM", "link_down": "PROBLEM",
            "host_down": "DOWN"}.get(r["verdict"], "?")
    print(f"[{mark}] {r['summary']}")
    print(f"    agent port {'open' if r['agent_port_open'] else 'closed'} · "
          f"ssh {'open' if r['ssh_port_open'] else 'closed'} · "
          f"terminal {'open' if r['terminal_port_open'] else 'closed'}")
    for i, s in enumerate(r["steps"], 1):
        print(f"  {i}. {s}")


def _since(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.fullmatch(r"(\d+)\s*([smhd]?)", text.strip().lower())
    if not m:
        sys.exit("--since takes a number with s, m, h or d (for example 30m, 2h, 1d)")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def cmd_remote_logs(args, api) -> None:
    q: dict[str, Any] = {"source": args.source, "lines": args.lines}
    if args.since:
        q["since_s"] = _since(args.since)
    if args.grep:
        q["grep"] = args.grep
    r = _remote(api, "GET", f"/{args.node}/logs", params=q, timeout=60)
    if api.as_json:
        print(json.dumps(r, indent=2, default=str))
        return
    if r.get("note"):
        print(f"! {r['note']}", file=sys.stderr)
    print("\n".join(r["lines"]))
    if not r["lines"]:
        print(f"(no lines; known sources: {', '.join(r['sources'])})", file=sys.stderr)


def cmd_remote_bundle(args, api) -> None:
    headers = {"x-api-key": api.key} if api.key else {}
    try:
        r = httpx.get(f"{api.base}/api/v1/remote/{args.node}/bundle", headers=headers, timeout=180)
    except httpx.HTTPError as exc:
        sys.exit(f"cannot reach the controller ({type(exc).__name__})")
    if r.status_code != 200:
        with contextlib.suppress(ValueError):
            sys.exit(f"error {r.status_code}: {r.json().get('detail')}")
        sys.exit(f"error {r.status_code}")
    out = Path(args.output or f"tsm-bundle-{args.node}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    out.write_bytes(r.content)
    print(f"{out}  ({len(r.content) // 1024} KiB, {r.headers.get('x-tsm-problems', '0')} item(s) could not be "
          f"collected). Secrets are masked; look it over before sharing.")


def cmd_remote_recordings(args, api) -> None:
    if args.name:
        r = _remote(api, "GET", f"/{args.node}/recordings/{args.name}")
        sys.stdout.write(r["cast"])
        return
    r = _remote(api, "GET", f"/{args.node}/recordings")
    if api.as_json:
        print(json.dumps(r, indent=2, default=str))
        return
    from .cli import _table
    rows = [[x["name"], x.get("size"), x.get("modified") and time.strftime("%F %T", time.localtime(x["modified"]))]
            for x in r["recordings"]]
    _table(rows, ["NAME", "BYTES", "WHEN"]) if rows else print("no recordings")
    if rows:
        print("\nPlay one:  tsm remote recordings", args.node, "<name> > s.cast && asciinema play s.cast")


def cmd_remote_power(args, api) -> None:
    ov = _remote(api, "GET", "/overview")
    notes = [f"This will {args.action} node {args.node} in {args.delay} seconds."]
    if args.node == ov["controller_node"]:
        notes.append(f"Node {args.node} runs the controller: the GUI, the CLI and the inference gateway go away "
                     f"until it is back.")
    if ov.get("active"):
        notes.append(f"The active model '{ov['active']}' will stop.")
    phrase = f"{args.action.upper()} {args.node}"
    confirm = _typed_confirmation(phrase, "\n".join(notes), args.yes)
    r = _remote(api, "POST", f"/{args.node}/power", json={"action": args.action, "confirm": confirm,
                                                          "delay_s": args.delay, "force": args.force})
    if api.as_json:
        print(json.dumps(r, indent=2, default=str))
        return
    print(f"{args.action} scheduled in {r.get('in_s')} s on node {args.node}."
          f"  Cancel with: tsm remote cancel {args.node}")


def cmd_remote_cancel(args, api) -> None:
    r = _remote(api, "POST", f"/{args.node}/power/cancel")
    print("cancelled: " + (", ".join(r.get("cancelled", [])) or "nothing was pending"))


def cmd_remote_boot(args, api) -> None:
    target = args.target
    if target in (None, "status"):
        r = _remote(api, "GET", f"/{args.node}/boot")
        if api.as_json:
            print(json.dumps(r, indent=2, default=str))
        else:
            _print_boot(r)
    elif target == "clear":
        _remote(api, "POST", f"/{args.node}/boot/clear")
        print("BootNext cleared: the next boot uses the normal order.")
    else:
        r = _remote(api, "POST", f"/{args.node}/boot/next", json={"target": target})
        print(f"next boot only: {r.get('next')} {r.get('label', '')}.  "
              f"Reboot it:  tsm remote power {args.node} reboot")


def _print_boot(r: dict[str, Any]) -> None:
    if not r.get("available"):
        print(f"boot entries not available: {r.get('reason')}")
        return
    print(f"booted from: {r.get('current')}   next boot only: {r.get('next') or '-'}   order: "
          f"{','.join(r.get('order', [])) or '-'}")
    for e in r.get("entries", []):
        flag = "*" if e.get("active") else " "
        print(f"  {flag} {e['num']}  {e.get('kind', '?'):8} {e['label']}")


def cmd_remote_wol(args, api) -> None:
    mode = "g" if args.mode == "on" else "d"
    r = _remote(api, "POST", f"/{args.node}/wol/set", json={"iface": args.iface, "mode": mode})
    print(f"{r.get('interface')}: Wake-on = {r.get('mode')}")
    if r.get("persist"):
        print(r["persist"])


def cmd_remote_wake(args, api) -> None:
    r = _remote(api, "POST", f"/{args.node}/wake")
    print(f"magic packet for {r['mac']} sent to {r['broadcast']}.")
    print(r["note"])


def cmd_remote_plug(args, api) -> None:
    confirm = ""
    if args.action != "on":
        confirm = _typed_confirmation(
            f"CUT POWER {args.node}",
            f"This cuts power to node {args.node} without a shutdown. Files being written can be lost.", args.yes)
    r = _remote(api, "POST", f"/{args.node}/plug", json={"action": args.action, "confirm": confirm,
                                                         "force": args.force})
    print(f"plug {r['action']}: HTTP {', '.join(str(x) for x in r['http'])}")


def cmd_remote_terminal(args, api) -> None:
    cols, rows = shutil.get_terminal_size((80, 24))
    t = _remote(api, "POST", "/terminal/ticket", json={"node": args.node, "cols": cols, "rows": rows})
    url = termclient.ws_url(api.base, t["ws_path"], t["ticket"])
    print(f"[tsm] terminal on node {args.node} — recorded on the node. Disconnect: Ctrl-] then .", file=sys.stderr)
    try:
        res = asyncio.run(termclient.run_session(
            url, stdin_fd=sys.stdin.fileno(), stdout_fd=sys.stdout.fileno(),
            size=lambda: tuple(shutil.get_terminal_size((80, 24))),                  # type: ignore[arg-type,return-value]
            raw=True))
    except termclient.TerminalClientError as exc:
        sys.exit(f"\r\nerror: {exc}")
    code = (res.get("exit") or {}).get("code")
    print(f"\r\n[tsm] {res['reason']}" + (f" (exit {code})" if code is not None else ""), file=sys.stderr)
    if isinstance(code, int) and 0 < code < 256:
        sys.exit(code)


# ---- tsm node … (on this Spark, no controller needed) ------------------------------------------
def _pick_priv(cfg):
    """Root does the privileged work itself; anyone else asks tsm-privd (and so meets the policy)."""
    if not hasattr(os, "geteuid"):
        sys.exit("Local node operations need Linux. Use `tsm remote` to manage a Spark from this machine.")
    from .agent.privd import PrivClient
    return privops.LocalPriv() if os.geteuid() == 0 else PrivClient(cfg.runtime.privd_socket)


class LocalNode:
    """The agent's remote-management actions, run in this process.

    Root gets the privileged operations directly (it can reboot the machine anyway); anyone else goes
    through ``tsm-privd`` and therefore through the remote-management policy.
    """

    def __init__(self, args):
        from .agent.actions import AgentActions
        from .schemas.config import AgentConfig, load_config

        self.lay = Layout(Path(args.root))
        path = Path(args.config) if getattr(args, "config", None) else self.lay.agent_yaml
        if not path.exists():
            sys.exit(f"{path} not found — run this on a Spark where `sudo tsm setup` has been run")
        try:
            self.cfg = load_config(path, AgentConfig)
        except (OSError, ValueError) as exc:
            sys.exit(f"cannot read {path}: {exc}")
        self.actions = AgentActions(self.cfg, privd=_pick_priv(self.cfg))

    def run(self, action: str, /, **params: Any) -> Any:
        try:
            return self.actions.registry[action](params)
        except Exception as exc:  # noqa: BLE001 - one readable line instead of a traceback
            sys.exit(f"error: {getattr(exc, 'detail', None) or exc}")

    @property
    def node(self) -> str:
        return self.cfg.node.node_id


def _unit_state(name: str) -> str:
    rc, out, _ = privops.RUN([privops._which("systemctl"), "is-active", name], 5)
    return out.strip() or ("unknown" if rc == 127 else "inactive")


def cmd_node_status(args) -> None:
    n = LocalNode(args)
    st = n.run("remote_status")
    if args.json:
        print(json.dumps(st, indent=2, default=str))
        return
    print(f"node {st['node']}  {st['hostname']}   kernel {st['kernel']}   up {_uptime(st['uptime_s'])}   "
          f"TwinSpark {st['version']}")
    for u in ("twinspark-agent", "twinspark-privd", UNIT, "twinspark-controller", "docker"):
        installed = (n.lay.systemd / f"{u}.service").exists() or u == "docker"
        if installed:
            print(f"  {u:22} {_unit_state(u)}")
    p = st["policy"]
    print(f"  remote policy: on = {', '.join(_dash(f) for f in st['enabled']) or '-'}" +
          (f"   ! ignored: {p['error']}" if p.get("error") else ""))
    if st.get("power_pending"):
        print(f"  ! pending: {', '.join(st['power_pending'])}   (cancel: sudo tsm node cancel)")
    print("\n  network ports (use the MAC for Wake-on-LAN in controller.yaml):")
    for i in st["interfaces"]:
        print(f"    {i['name']:16} {i.get('mac') or '-':18} {i.get('state', '?'):8} "
              f"{(str(i['speed_mbps']) + ' Mb/s') if i.get('speed_mbps') else '-':11} "
              f"{', '.join(i.get('ipv4') or []) or '-':20} wol={i.get('wol') or '?'}")


@dataclass
class Check:
    level: str          # ok | warn | fail
    name: str
    detail: str = ""
    fix: str = ""


def node_checks(n: LocalNode) -> list[Check]:
    out: list[Check] = []
    lay = n.lay
    sandbox = lay.sandbox
    units = ["twinspark-agent", "twinspark-privd", "docker"]
    if lay.controller_yaml.exists():
        units.append("twinspark-controller")
    if (lay.systemd / f"{UNIT}.service").exists():
        units.append(UNIT)
    for u in units:
        if sandbox:
            break
        state = _unit_state(u)
        if state == "active":
            out.append(Check("ok", f"service {u}", "running"))
        else:
            out.append(Check("fail", f"service {u}", state,
                             f"sudo systemctl restart {u}; sudo journalctl -u {u} -n 80 --no-pager"))
    host = n.cfg.listener.bind if n.cfg.listener.bind not in ("0.0.0.0", "::") else "127.0.0.1"
    if privops.shutil.which("true") and not sandbox:
        from .agent.remote_actions import _tcp_open
        up = _tcp_open(host, n.cfg.listener.port)
        out.append(Check("ok" if up else "fail", "agent API", f"{host}:{n.cfg.listener.port}"
                         + ("" if up else " is not answering"),
                         "" if up else "sudo systemctl restart twinspark-agent"))
    p = pol.load_policy(Path(n.cfg.remote_mgmt.policy_path), require_root=n.cfg.remote_mgmt.require_root_owned)
    if p.error:
        out.append(Check("warn", "remote policy", f"ignored, everything off: {p.error}",
                         "sudo chown root:root /etc/twinspark/remote-policy.json && sudo chmod 644 "
                         "/etc/twinspark/remote-policy.json"))
    else:
        out.append(Check("ok", "remote policy", "on: " + (", ".join(_dash(f) for f in p.enabled) or "nothing")))
    for label, path in (("system disk", "/"), ("model disk", n.cfg.runtime.hf_cache_dir)):
        try:
            probe = path
            while not os.path.exists(probe) and probe != "/":
                probe = os.path.dirname(probe)
            du = shutil.disk_usage(probe)
        except OSError:
            continue
        pct = 100 * du.free / du.total if du.total else 100
        lvl = "fail" if pct < 3 else "warn" if pct < 10 else "ok"
        out.append(Check(lvl, label, f"{du.free / 1024**3:.0f} GiB free ({pct:.0f}%)",
                         "" if lvl == "ok" else "free space: docker system prune; tsm models ls"))
    rc, txt, _ = privops.RUN([privops._which("timedatectl"), "show", "-p", "NTPSynchronized", "--value"], 5)
    if rc == 0:
        synced = txt.strip() == "yes"
        out.append(Check("ok" if synced else "warn", "clock", "synchronised" if synced else "not synchronised",
                         "" if synced else "model downloads and TLS fail with a wrong clock: sudo timedatectl "
                                           "set-ntp true"))
    st = n.run("remote_status")
    for t in ("efibootmgr", "ethtool"):
        if not st["tools"].get(t):
            out.append(Check("warn", f"tool {t}", "not installed", f"sudo apt install {t}"))
    return out


def cmd_node_doctor(args) -> None:
    n = LocalNode(args)
    checks = node_checks(n)
    if args.json:
        print(json.dumps([c.__dict__ for c in checks], indent=2))
    else:
        for c in checks:
            mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}[c.level]
            print(f"[{mark}] {c.name:24} {c.detail}")
            if c.fix:
                print(f"       fix: {c.fix}")
        bad = [c for c in checks if c.level == "fail"]
        print("\nAll good." if not bad and all(c.level == "ok" for c in checks) else
              f"\n{len(bad)} failing, {sum(c.level == 'warn' for c in checks)} to look at.")
    if any(c.level == "fail" for c in checks):
        sys.exit(1)


def cmd_node_logs(args) -> None:
    n = LocalNode(args)
    r = n.run("remote_logs", source=args.source, lines=args.lines, since_s=_since(args.since), grep=args.grep)
    if r.get("note"):
        print(f"! {r['note']}", file=sys.stderr)
    print("\n".join(r["lines"]))


def cmd_node_bundle(args) -> None:
    import base64
    n = LocalNode(args)
    r = n.run("remote_bundle")
    out = Path(args.output or f"tsm-bundle-{n.node}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    out.write_bytes(base64.b64decode(r["b64"]))
    print(f"{out}  ({r['size'] // 1024} KiB, {len(r['problems'])} item(s) could not be collected). "
          f"Secrets are masked; look it over before sharing.")


def cmd_node_power(args) -> None:
    n = LocalNode(args)
    if args.action == "cancel":
        r = n.run("remote_power_cancel")
        print("cancelled: " + (", ".join(r.get("cancelled", [])) or "nothing was pending"))
        return
    if not _yn(f"{args.action.capitalize()} node {n.node} in {args.delay} seconds? Running models stop.", args.yes):
        sys.exit("cancelled — nothing was changed.")
    r = n.run("remote_power", action=args.action, delay_s=args.delay, force=args.force)
    print(f"{args.action} in {r.get('in_s')} s.  Cancel: sudo tsm node cancel")


def cmd_node_boot(args) -> None:
    n = LocalNode(args)
    if args.target in (None, "status"):
        r = n.run("remote_boot_status")
        print(json.dumps(r, indent=2) if args.json else "", end="")
        if not args.json:
            _print_boot(r)
    elif args.target == "clear":
        n.run("remote_boot_next_clear")
        print("BootNext cleared.")
    else:
        r = n.run("remote_boot_next", target=args.target)
        print(f"next boot only: {r['next']} {r['label']}.  Reboot:  sudo tsm node reboot")


def cmd_node_wol(args) -> None:
    n = LocalNode(args)
    if args.mode in (None, "status"):
        r = n.run("remote_wol_status")
        if not r.get("available"):
            sys.exit(r.get("reason", "Wake-on-LAN status is not available"))
        for name, i in r["interfaces"].items():
            print(f"  {name:16} {i.get('mac') or '-':18} supports={i.get('supports') or '?':6} "
                  f"wake-on={i.get('mode') or '?'}")
        return
    if not args.iface:
        sys.exit("name the port:  sudo tsm node wol on <iface>")
    r = n.run("remote_wol_set", iface=args.iface, mode="g" if args.mode == "on" else "d")
    print(f"{r['interface']}: wake-on = {r['mode']}")
    if r.get("persist"):
        print(r["persist"])


def cmd_node(args, api=None) -> None:
    {"status": cmd_node_status, "doctor": cmd_node_doctor, "logs": cmd_node_logs, "bundle": cmd_node_bundle,
     "boot": cmd_node_boot, "wol": cmd_node_wol}.get(args.sub, cmd_node_power)(args)


# ---- tsm wake / tsm netboot -------------------------------------------------------------------
def cmd_wake(args, api=None) -> None:
    from .controller.remote import send_magic_packet
    from .schemas.config import WakeSettings

    try:
        w = WakeSettings(mac=args.mac, iface=args.iface, broadcast=args.broadcast, port=args.port)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    bind = None
    if args.iface:
        ifc = next((i for i in hostprobe.list_interfaces() if i.name == args.iface), None)
        if not ifc or not ifc.addr:
            sys.exit(f"interface {args.iface} has no IPv4 address on this machine")
        bind = ifc.addr
    try:
        send_magic_packet(w.mac, w.broadcast, w.port, bind)
    except OSError as exc:
        sys.exit(f"could not send the packet: {exc.strerror or exc}")
    print(f"magic packet for {w.mac} sent to {w.broadcast}:{w.port}"
          + (f" from {args.iface}" if args.iface else "")
          + ".\nIt cannot be acknowledged. If the machine does not come up within a minute or two, Wake-on-LAN "
            "is not working on that port.")


def cmd_netboot(args, api=None) -> None:
    try:
        plan = netboot.build_plan(args.mac, args.iface, args.bootfile, args.tftp_root, subnet=args.subnet,
                                  minutes=args.minutes)
    except netboot.NetbootError as exc:
        sys.exit(f"error: {exc}")
    if args.what == "plan":
        print(plan.config)
        for w in plan.warnings:
            print(f"! {w}")
        print("\nSteps:")
        for i, s in enumerate(plan.steps, 1):
            print(f"  {i}. {s}")
        if args.out:
            Path(args.out).mkdir(parents=True, exist_ok=True)
            (Path(args.out) / "dnsmasq-netboot.conf").write_text(plan.config)
            print(f"\nwritten: {Path(args.out) / 'dnsmasq-netboot.conf'}")
        return
    if not hasattr(os, "geteuid"):
        sys.exit("netboot serve needs Linux; generate a plan here and run the helper on the Spark.")
    if os.geteuid() != 0:
        sys.exit("netboot serve needs root (DHCP and TFTP use privileged ports): sudo tsm netboot serve …")
    for w in plan.warnings:
        print(f"! {w}")
    print(f"proxyDHCP + TFTP for {plan.mac} on {plan.iface} for {plan.minutes} minutes (Ctrl-C stops it)")
    try:
        sys.exit(netboot.serve(plan))
    except netboot.NetbootError as exc:
        sys.exit(f"error: {exc}")


# ---- dispatch & parsers -----------------------------------------------------------------------
def cmd_remote(args, api) -> None:
    sub = args.sub
    if sub in ("enable", "disable"):
        return cmd_remote_switch(args, sub == "enable")
    if sub == "policy":
        return cmd_remote_policy(args)
    if sub == "plug-token":
        return cmd_remote_plug_token(args)
    {"status": cmd_remote_status, "reach": cmd_remote_reach, "logs": cmd_remote_logs,
     "bundle": cmd_remote_bundle, "recordings": cmd_remote_recordings, "power": cmd_remote_power,
     "reboot": cmd_remote_power, "poweroff": cmd_remote_power, "cancel": cmd_remote_cancel,
     "boot": cmd_remote_boot, "wol": cmd_remote_wol, "wake": cmd_remote_wake, "plug": cmd_remote_plug,
     "terminal": cmd_remote_terminal}[sub](args, api)


def add_parsers(sub, cmd) -> None:
    node_arg = {"choices": ["A", "B"], "type": str.upper}

    s = cmd("remote", cmd_remote,
            "remote management: terminal, logs, power, Wake-on-LAN (see docs/remote-management.md)")
    rs = s.add_subparsers(dest="sub", metavar="<action>", required=True)

    def r(name, help_, node=True, **kw):
        p = rs.add_parser(name, help=help_)
        if node:
            p.add_argument("node", **node_arg)
        return p

    r("status", "what is switched on, per node (through the controller)", node=False)
    r("reach", "why can't I reach this node? probes agent, SSH and terminal ports and says what to do")
    p = r("terminal", "open a shell on a node (needs: sudo tsm remote enable terminal, on that node)")
    p = r("logs", "read a node's logs")
    p.add_argument("--source", default="agent", help="agent, controller, privd, terminal, docker, kernel, "
                                                     "previous-boot, ssh, network, containerd, networkd, nvidia")
    p.add_argument("-n", "--lines", type=int, default=200)
    p.add_argument("--since", help="30m, 2h, 1d …")
    p.add_argument("--grep", help="only lines containing this text")
    p = r("bundle", "download a redacted support bundle (tar.gz)")
    p.add_argument("-o", "--output")
    p = r("recordings", "list terminal recordings, or print one (asciicast, for asciinema play)")
    p.add_argument("name", nargs="?")
    for name, text in (("reboot", "reboot a node (typed confirmation)"), ("poweroff", "power a node off")):
        p = r(name, text)
        p.set_defaults(action=name)
        p.add_argument("--delay", type=int, default=5, help="seconds before it happens (2-600)")
        p.add_argument("--force", action="store_true", help="even while an activation or maintenance run is active")
        p.add_argument("-y", "--yes", action="store_true", help="supply the typed confirmation")
    r("cancel", "cancel a scheduled reboot / power-off")
    p = r("boot", "show boot entries, or boot ONCE from 'network' (or an entry number); 'clear' undoes it")
    p.add_argument("target", nargs="?", help="status | network | clear | 4-digit entry such as 0003")
    p = r("wol", "turn Wake-on-LAN on or off for one of the node's ports")
    p.add_argument("iface")
    p.add_argument("mode", choices=["on", "off"])
    r("wake", "send the Wake-on-LAN packet for a node (from the controller machine)")
    p = r("plug", "smart plug: on, off (cuts power!) or cycle")
    p.add_argument("action", choices=["on", "off", "cycle"])
    p.add_argument("--force", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    for name, text in (("enable", "switch features on (on THIS node, as root)"),
                       ("disable", "switch features off (on THIS node, as root)")):
        p = r(name, text, node=False)
        p.add_argument("features", nargs="*", help=f"{', '.join(_dash(f) for f in FEATURES)}, or all")
        p.add_argument("--root", default="/")
        p.add_argument("--dry", action="store_true")
    p = r("policy", "show this node's remote-management switches", node=False)
    p.add_argument("--root", default="/")
    p = r("plug-token", "store the smart-plug token in the vault (controller node)", node=False)
    p.add_argument("--from-file")
    cmd_remote.local = False

    import argparse
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default="/", help=argparse.SUPPRESS)
    common.add_argument("--config", help="agent.yaml (default /etc/twinspark/agent.yaml)")
    s = cmd("node", cmd_node, "work on THIS Spark without the controller: status, doctor, logs, reboot, boot, wol")
    ns = s.add_subparsers(dest="sub", metavar="<action>", required=True)
    for name, text in (("status", "this node at a glance, with the MAC addresses"),
                       ("doctor", "checks services, disk, clock, policy — and the fix for each problem")):
        ns.add_parser(name, help=text, parents=[common])
    p = ns.add_parser("logs", help="read logs", parents=[common])
    p.add_argument("source", nargs="?", default="agent")
    p.add_argument("-n", "--lines", type=int, default=200)
    p.add_argument("--since")
    p.add_argument("--grep")
    p = ns.add_parser("bundle", help="write a redacted support bundle", parents=[common])
    p.add_argument("-o", "--output")
    for name, text in (("reboot", "reboot this machine"), ("poweroff", "power this machine off")):
        p = ns.add_parser(name, help=text, parents=[common])
        p.set_defaults(action=name)
        p.add_argument("--delay", type=int, default=5)
        p.add_argument("--force", action="store_true",
                       help="even during a maintenance run or while apt/dpkg is working")
        p.add_argument("-y", "--yes", action="store_true")
    p = ns.add_parser("cancel", help="cancel a scheduled reboot / power-off", parents=[common])
    p.set_defaults(action="cancel")
    p = ns.add_parser("boot", help="boot entries; 'network' boots once from the network", parents=[common])
    p.add_argument("target", nargs="?")
    p = ns.add_parser("wol", help="Wake-on-LAN status, or on/off for a port", parents=[common])
    p.add_argument("mode", nargs="?", choices=["status", "on", "off"])
    p.add_argument("iface", nargs="?")
    cmd_node.local = True

    s = cmd("wake", cmd_wake, "send a Wake-on-LAN magic packet from this machine")
    s.add_argument("mac")
    s.add_argument("--iface", help="send from this interface (for the QSFP link)")
    s.add_argument("--broadcast", default="255.255.255.255", help="e.g. 192.168.100.255 for the QSFP subnet")
    s.add_argument("--port", type=int, default=9)
    cmd_wake.local = True

    s = cmd("netboot", cmd_netboot, "plan or run a short PXE helper to boot a node over the network (rescue)")
    s.add_argument("what", choices=["plan", "serve"])
    s.add_argument("--mac", required=True, help="the ONE machine that may boot from this helper")
    s.add_argument("--iface", required=True, help="the port facing it (for example the QSFP port)")
    s.add_argument("--bootfile", default="snp.arm64.efi", help="ARM64 UEFI bootloader inside the TFTP root")
    s.add_argument("--tftp-root", default="/srv/tftp")
    s.add_argument("--subnet", help="override the network (default: the port's own)")
    s.add_argument("--minutes", type=int, default=netboot.DEFAULT_MINUTES)
    s.add_argument("--out", help="plan: also write the dnsmasq config into this directory")
    cmd_netboot.local = True
