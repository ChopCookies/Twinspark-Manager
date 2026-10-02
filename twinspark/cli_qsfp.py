"""``tsm qsfp`` — set up, check and repair the direct QSFP link between the two Sparks.

    tsm qsfp status                    what the machine has: ports, twins, link, addresses, MTU, RoCE
    tsm qsfp plan   [--node A|B]       what ``apply`` would change (changes nothing)
    sudo tsm qsfp apply [--node A|B]   write /etc/netplan/60-twinspark-qsfp.yaml and apply it
    sudo tsm qsfp apply --temporary    only set the addresses until the next reboot
    sudo tsm qsfp revert               put the previous network settings back
    tsm qsfp verify                    local checks, then ping the other Spark through each twin
    tsm qsfp scan                      look for the other Spark on the link (like eugr's autodiscover.sh)

None of it needs the controller. ``status``, ``plan``, ``verify`` and ``scan`` only read; ``apply`` and
``revert`` are the only commands that change the machine, and both refuse to touch the management network.
See docs/qsfp-link.md.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from . import qsfp
from .provision import Layout

_MARK = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}


# ---- shared bits -----------------------------------------------------------------------------------
def _host(args) -> qsfp.Host:
    return qsfp.Host(root=Path(args.root), sysfs=Path(args.sysfs))


def _agent_node(lay: Layout) -> Optional[str]:
    """This machine's node id (A or B) from agent.yaml, when setup has been run here."""
    if not lay.agent_yaml.exists():
        return None
    try:
        from .schemas.config import AgentConfig, load_config
        return load_config(lay.agent_yaml, AgentConfig).node.node_id
    except (OSError, ValueError):
        return None


def _controller_endpoint(lay: Layout, node: Optional[str]) -> tuple[Optional[str], list[str], Optional[int]]:
    """``(qsfp_iface, rdma_hcas, ib_gid_index)`` controller.yaml has for this node (only on the controller node)."""
    if not node or not lay.controller_yaml.exists():
        return None, [], None
    try:
        from .schemas.config import ControllerConfig, load_config
        ep = load_config(lay.controller_yaml, ControllerConfig).nodes.get(node)
    except (OSError, ValueError):
        return None, [], None
    return (ep.qsfp_iface, list(ep.rdma_hcas), ep.ib_gid_index) if ep else (None, [], None)


def _fail(exc: Exception) -> None:
    sys.exit(f"error: {exc}")


def _plan(args, host: qsfp.Host, d: qsfp.Discovery) -> qsfp.Plan:
    lay = Layout(Path(args.root))
    node = args.node or _agent_node(lay)
    configured, _, _ = _controller_endpoint(lay, node)
    try:
        return qsfp.plan_for_node(d, node, iface=args.iface, subnet=args.subnet, mtu=args.mtu,
                                  host_number=args.host_number, configured=configured, host=host)
    except qsfp.QsfpError as exc:
        _fail(exc)
        raise


def _print_checks(results: list[dict[str, str]]) -> None:
    for c in results:
        print(f"[{_MARK.get(c['status'], '?')}] {c['check']:22} {c['detail']}")
        if c.get("fix") and c["status"] in ("warn", "fail"):
            print(f"       fix: {c['fix']}")


def _summary(results: list[dict[str, str]]) -> None:
    bad = sum(c["status"] == "fail" for c in results)
    warn = sum(c["status"] == "warn" for c in results)
    print("\nThe QSFP link looks good." if not bad and not warn else f"\n{bad} failing, {warn} to look at.")


def _print_ports(d: qsfp.Discovery) -> None:
    if not d.ports:
        print("no ConnectX (enp1s0f1np1-style) interface found on this machine")
        return
    for p in d.ports:
        print(f"port {p.key}   {'cabled' if p.cabled else 'no link'}")
        for t in p.twins:
            rate = f"{t.rate_gbps:.0f}G" if t.rate_gbps else "-"
            print(f"  {t.iface:15} {'secondary' if t.secondary else 'primary  '}  roce {t.hca or '-':14} "
                  f"{'ACTIVE' if t.link_up else 'down  '} {rate:>5}  mtu {t.mtu or '-':>5}  "
                  f"{', '.join(t.ipv4) or 'no IPv4'}")


# ---- status / plan ---------------------------------------------------------------------------------
def cmd_status(args, api=None) -> None:
    host = _host(args)
    d = qsfp.discover(host)
    lay = Layout(Path(args.root))
    node = getattr(args, "node", None) or _agent_node(lay)
    iface, hcas, gid = _controller_endpoint(lay, node)
    results = qsfp.checks(d, configured_iface=iface, configured_hcas=hcas, configured_gid=gid)
    if args.json:
        print(json.dumps({"ports": qsfp.describe(d), "checks": results}, indent=2))
    else:
        _print_ports(d)
        print()
        _print_checks(results)
        _summary(results)
    if any(c["status"] == "fail" for c in results):
        sys.exit(1)


def _show_plan(plan: qsfp.Plan, problems: list[str]) -> None:
    print(f"QSFP port {plan.port}, this machine = host .{plan.node_host}, MTU {plan.mtu}")
    for e in plan.entries:
        print(f"  set   {e.iface:15} {e.cidr}   ({'secondary' if e.secondary else 'primary'} twin)")
    for k in plan.kept:
        print(f"  keep  {k}   (already configured somewhere else — not touched)")
    if plan.peer_ips:
        print(f"  the other Spark should answer on: {', '.join(plan.peer_ips)}")
    for w in plan.warnings:
        print(f"  ! {w}")
    if plan.entries:
        print(f"\nFile: {plan.path}\n")
        print("".join(f"    {line}\n" for line in plan.text.splitlines()))
    for p in problems:
        print(f"  ✗ {p}")


def cmd_plan(args, api=None) -> None:
    host = _host(args)
    d = qsfp.discover(host)
    plan = _plan(args, host, d)
    problems = qsfp.preflight(host, d, plan, need_root=False) if plan.entries else []
    if args.json:
        print(json.dumps({**plan.as_dict(), "problems": problems}, indent=2))
        return
    _show_plan(plan, problems)
    if not plan.entries:
        print("\nNothing to do: every twin of this port already has an address.")
    elif problems:
        print("\nNot safe to apply yet — fix the ✗ items first.")
    else:
        print("\nLooks safe. Apply it with:  sudo tsm qsfp apply" + (" --temporary" if args.temporary else "")
              + "   (first time on a new setup? add --temporary to try it without writing anything)")


# ---- apply / revert --------------------------------------------------------------------------------
def _need_root(args) -> None:
    if args.root != "/" or not hasattr(os, "geteuid") or os.geteuid() == 0:
        return
    sys.exit("error: this changes the network configuration — run it with sudo:\n"
             f"  sudo {Path(sys.argv[0]).name} {' '.join(sys.argv[1:])}")


def _confirm(args, question: str) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        sys.exit("error: not a terminal — pass --yes to confirm.")
    return input(f"{question} [y/N]: ").strip().lower().startswith("y")


def cmd_apply(args, api=None) -> None:
    _need_root(args)
    host = _host(args)
    d = qsfp.discover(host)
    plan = _plan(args, host, d)
    current = bool(plan.entries) and not args.temporary and qsfp.is_current(host, d, plan)
    problems = qsfp.preflight(host, d, plan, need_netplan=not args.temporary) if plan.entries and not current else []
    if not args.json:
        _show_plan(plan, problems)
    if not plan.entries or current:
        if args.json:
            print(json.dumps({"changed": False, "message": "nothing to change"}, indent=2))
        else:
            print("\nNothing to change." if not current else "\nAlready configured; nothing to change.")
        return
    if problems:
        sys.exit("\nerror: not safe to continue — fix the ✗ items above." if not args.json
                 else "error: not safe to continue: " + "; ".join(problems))
    mode = ("set TEMPORARY addresses (lost at the next reboot; no netplan file is written)" if args.temporary
            else f"write {plan.path} and run `netplan apply`")
    if not _confirm(args, f"\nThis will {mode}. The management network is not touched. Go ahead?"):
        sys.exit("cancelled — nothing was changed.")
    try:
        res = qsfp.apply(host, plan, temporary=args.temporary, d=d)
    except qsfp.QsfpError as exc:
        _fail(exc)
        return
    if args.json:
        print(json.dumps(res, indent=2))
        return
    print(f"\n{res['message']}")
    if res.get("backup"):
        print(f"previous file saved as {res['backup']}  (undo: sudo tsm qsfp revert)")
    elif res.get("changed") and not args.temporary:
        print("undo: sudo tsm qsfp revert")
    if host.sandbox:
        return
    after = qsfp.discover(host)
    _print_checks(qsfp.checks(after, port=after.port_of(plan.entries[0].iface)))
    print("\nNext: do the same on the other Spark (`sudo tsm qsfp apply --node B`), then `tsm qsfp verify` on "
          "either one.\nWhen both are done, `tsm rdma --apply` on node A lists both RoCE devices in controller.yaml.")


def cmd_revert(args, api=None) -> None:
    _need_root(args)
    host = _host(args)
    plan = None
    if args.temporary and not qsfp.temp_recorded(host):
        plan = _plan(args, host, qsfp.discover(host))
    if not _confirm(args, "This puts back the network settings from before `tsm qsfp apply`. Go ahead?"):
        sys.exit("cancelled — nothing was changed.")
    try:
        res = qsfp.revert(host, temporary=args.temporary, plan=plan)
    except qsfp.QsfpError as exc:
        _fail(exc)
        return
    print(json.dumps(res, indent=2) if args.json else res["message"])


# ---- verify / scan ---------------------------------------------------------------------------------
def _peers_of(port: qsfp.Port, given: list[str]) -> list[str]:
    """The other Spark's address on each twin's subnet: host .2 when we are .1 and the other way round."""
    if given:
        return given
    peers = []
    for t in port.twins:
        if t.ipv4:
            import ipaddress
            own = ipaddress.ip_interface(t.ipv4[0])
            peers.append(str(own.network.network_address + (2 if int(own.ip) & 0xFF == 1 else 1)))
    return peers


def cmd_verify(args, api=None) -> None:
    import ipaddress
    for p in args.peer or []:
        try:
            ipaddress.IPv4Address(p)
        except ValueError:
            sys.exit(f"error: --peer {p!r} is not an IPv4 address")
    host = _host(args)
    d = qsfp.discover(host)
    lay = Layout(Path(args.root))
    node = getattr(args, "node", None) or _agent_node(lay)
    iface, hcas, gid = _controller_endpoint(lay, node)
    results = qsfp.checks(d, configured_iface=args.iface or iface, configured_hcas=hcas, configured_gid=gid,
                          mtu=args.mtu)
    try:
        port = qsfp.choose_port(d, args.iface, iface)
    except qsfp.QsfpError:
        port = None
    if port is not None and any(t.ipv4 for t in port.twins):
        results += qsfp.peer_checks(host, port, _peers_of(port, args.peer or []))
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_checks(results)
        _summary(results)
    if any(c["status"] == "fail" for c in results):
        sys.exit(1)


def cmd_scan(args, api=None) -> None:
    host = _host(args)
    d = qsfp.discover(host)
    try:
        port = qsfp.choose_port(d, args.iface)
    except qsfp.QsfpError as exc:
        _fail(exc)
        return
    found: list[dict[str, Any]] = []
    for t in port.twins:
        if not t.addr:
            continue
        for ip in qsfp.scan(t):
            row: dict[str, Any] = {"ip": ip, "via": t.iface, "gpu": None}
            if args.identify:
                try:
                    row["gpu"] = qsfp.identify(ip, args.user, host)
                except qsfp.QsfpError as exc:
                    _fail(exc)
            found.append(row)
    if args.json:
        print(json.dumps(found, indent=2))
        return
    if not found:
        print("nothing answers on SSH (port 22) on the link subnets. Is the other Spark on, cabled to the same "
              "port, and configured (`sudo tsm qsfp apply --node B` there)?")
        return
    for r in found:
        gpu = f"   {r['gpu']}" if r["gpu"] else ""
        print(f"{r['ip']:16} via {r['via']}{gpu}")
    if args.identify and not any(r["gpu"] for r in found):
        print("(no GPU name came back: SSH key login to that machine is needed for --identify)")


# ---- parsers ---------------------------------------------------------------------------------------
def add_parsers(sub, cmd) -> None:
    import argparse
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default="/", help=argparse.SUPPRESS)
    common.add_argument("--sysfs", default="/", help=argparse.SUPPRESS)
    common.add_argument("--iface", help="a QSFP interface of the port to use (default: the cabled port)")

    plan_opts = argparse.ArgumentParser(add_help=False)
    plan_opts.add_argument("--node", type=str.upper, choices=["A", "B"],
                           help="which Spark this is (default: from agent.yaml, or from an existing address)")
    plan_opts.add_argument("--host-number", type=int, help="last octet instead of A=1 / B=2")
    plan_opts.add_argument("--subnet", help="first twin's /24 (default 192.168.100.0/24; the second twin gets the "
                                            "next one)")
    plan_opts.add_argument("--mtu", type=int, default=qsfp.DEFAULT_MTU)
    plan_opts.add_argument("--temporary", action="store_true", help="addresses only until the next reboot")

    s = cmd("qsfp", cmd_status, "set up and check the QSFP link between the Sparks (see docs/qsfp-link.md)")
    qs = s.add_subparsers(dest="sub", metavar="<action>")
    s.set_defaults(root="/", sysfs="/", iface=None, node=None)       # `tsm qsfp` alone = status

    p = qs.add_parser("status", parents=[common], help="ports, twins, link, addresses, MTU, RoCE (read-only)")
    p.add_argument("--node", type=str.upper, choices=["A", "B"])
    p.set_defaults(fn=cmd_status)
    p = qs.add_parser("plan", parents=[common, plan_opts], help="show what apply would do (changes nothing)")
    p.set_defaults(fn=cmd_plan)
    p = qs.add_parser("apply", parents=[common, plan_opts], help="configure both twins (needs sudo)")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(fn=cmd_apply)
    p = qs.add_parser("revert", parents=[common, plan_opts], help="put the previous settings back (needs sudo)")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(fn=cmd_revert)
    p = qs.add_parser("verify", parents=[common], help="check this side, then ping the other Spark (read-only)")
    p.add_argument("--peer", action="append", help="the other Spark's address (repeat for both twins)")
    p.add_argument("--mtu", type=int, default=qsfp.DEFAULT_MTU)
    p.add_argument("--node", type=str.upper, choices=["A", "B"])
    p.set_defaults(fn=cmd_verify)
    p = qs.add_parser("scan", parents=[common], help="find the other Spark on the link subnets")
    p.add_argument("--identify", action="store_true", help="ask each host for its GPU name over SSH (key login)")
    p.add_argument("--user", default=os.environ.get("USER") or "twinspark", help="SSH user for --identify")
    p.set_defaults(fn=cmd_scan)
    for fn in (cmd_status, cmd_plan, cmd_apply, cmd_revert, cmd_verify, cmd_scan):
        fn.local = True
