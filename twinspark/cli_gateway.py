"""``tsm gateway``: how the gateway picks a backend (master plan §3.3; docs/recipe-workflow.md).

    tsm gateway                                   # the settings in effect
    tsm gateway set --balance least-inflight --weight A=2 --weight B=1 --affinity on
    tsm gateway set --fallback default=alias:secondary
    tsm gateway set --fallback default=url:https://api.example.com/v1,model:small-model
    tsm gateway set --fallback default=none
    tsm gateway fallback-key                      # the external fallback's key, read from stdin
    tsm gateway reset                             # back to controller.yaml
"""

from __future__ import annotations

import getpass
import json
import sys


def _show(api, view: dict) -> None:
    from .cli import _out
    if _out(api, view):
        return
    s = view["settings"]
    print(f"settings  from {view['source']}")
    weights = ", ".join(f"{n}={w:g}" for n, w in sorted((s.get("weights") or {}).items()))
    print(f"balance   {s['balance']}" + (f"  weights {weights}" if weights else ""))
    print(f"affinity  {'on' if s['affinity'] else 'off'} (session-id header or prompt_cache_key; replicated models)")
    if not s.get("fallbacks"):
        print("fallback  none: an alias that is switching or down answers 503")
    for alias, fb in sorted((s.get("fallbacks") or {}).items()):
        where = f"alias {fb['alias']}" if fb.get("alias") else f"{fb['url']} (model {fb['model']})"
        print(f"fallback  {alias} → {where}")
    if any((fb or {}).get("url") for fb in (s.get("fallbacks") or {}).values()):
        print(f"          external key {'set' if view['fallback_key_set'] else 'NOT SET (tsm gateway fallback-key)'}")


def parse_fallback(text: str) -> tuple[str, dict | None]:
    alias, sep, target = text.partition("=")
    if not sep or not alias:
        raise SystemExit(f"--fallback {text!r}: use ALIAS=alias:OTHER, ALIAS=url:URL,model:NAME or ALIAS=none")
    if target == "none":
        return alias, None
    if target.startswith("alias:"):
        return alias, {"alias": target[6:]}
    if target.startswith("url:"):
        url, _, rest = target[4:].partition(",model:")
        return alias, {"url": url, "model": rest or None}
    raise SystemExit(f"--fallback {text!r}: the target is alias:OTHER, url:URL,model:NAME or none")


def cmd_gateway(args, api) -> None:
    action = getattr(args, "gateway_cmd", None) or "show"
    if action == "show":
        _show(api, api("GET", "/api/v1/gateway"))
        return
    if action == "reset":
        _show(api, api("DELETE", "/api/v1/gateway"))
        return
    if action == "fallback-key":
        key = "" if args.clear else (sys.stdin.readline().strip() if not sys.stdin.isatty()
                                     else getpass.getpass("key for the external fallback: ").strip())
        r = api("PUT", "/api/v1/gateway/fallback-key", json={"key": key})
        print("external fallback key " + ("set" if r["fallback_key_set"] else "removed"))
        return
    settings = api("GET", "/api/v1/gateway")["settings"]
    if args.affinity:
        settings["affinity"] = args.affinity == "on"
    if args.balance:
        settings["balance"] = args.balance
    for w in args.weight or []:
        node, _, value = w.partition("=")
        try:
            settings.setdefault("weights", {})[node.upper()] = float(value)
        except ValueError:
            raise SystemExit(f"--weight {w!r}: use A=2 or B=0.5") from None
    for fb in args.fallback or []:
        alias, target = parse_fallback(fb)
        if target is None:
            settings.setdefault("fallbacks", {}).pop(alias, None)
        else:
            settings.setdefault("fallbacks", {})[alias] = target
    view = api("PUT", "/api/v1/gateway", json=settings)
    if not api.as_json:
        print("saved; applies to the next request")
    if api.as_json:
        print(json.dumps(view, indent=2))
    else:
        _show(api, view)


def add_parsers(sub, cmd) -> None:
    g = sub.add_parser("gateway", help="how the gateway picks a backend: balance, weights, affinity, fallbacks")
    g.set_defaults(fn=cmd_gateway)
    gs = g.add_subparsers(dest="gateway_cmd", metavar="<action>")
    gs.add_parser("show", help="the settings in effect").set_defaults(fn=cmd_gateway)
    s = gs.add_parser("set", help="change them (applies to the next request)")
    s.set_defaults(fn=cmd_gateway)
    s.add_argument("--affinity", choices=["on", "off"])
    s.add_argument("--balance", choices=["least-inflight", "round-robin"])
    s.add_argument("--weight", action="append", metavar="NODE=W", help="replicated: share of node A or B")
    s.add_argument("--fallback", action="append", metavar="ALIAS=TARGET",
                   help="alias:OTHER, url:URL,model:NAME, or none (repeatable)")
    gs.add_parser("reset", help="back to controller.yaml").set_defaults(fn=cmd_gateway)
    s = gs.add_parser("fallback-key", help="the external fallback's API key (read from stdin)")
    s.set_defaults(fn=cmd_gateway)
    s.add_argument("--clear", action="store_true")
