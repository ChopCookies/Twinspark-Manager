"""``tsm keys`` and ``tsm usage``: one inference key per app, and the tokens each used (docs/gateway.md)."""

from __future__ import annotations

import sys
import time
from pathlib import Path


def cmd_keys(args, api) -> None:
    from .cli import _ago, _out, _table
    action = getattr(args, "keys_cmd", None) or "list"
    if action == "create":
        body = {"name": args.name, "aliases": args.alias or [], "expires_in_days": args.expires, "app": args.app}
        r = api("POST", "/api/v1/keys", json=body)
        if _out(api, r):
            return
        print(r["key"])
        print(f"# key {r['id']} ({r['name']}): shown this once — only its hash is kept"
              + (f"; aliases {', '.join(r['aliases'])}" if r["aliases"] else "; all aliases")
              + (f"; expires {time.strftime('%Y-%m-%d', time.localtime(r['expires_at']))}"
                 if r.get("expires_at") else ""),
              file=sys.stderr)
        return
    if action in ("revoke", "delete"):
        r = api("POST", f"/api/v1/keys/{args.id}/revoke") if action == "revoke" else \
            api("DELETE", f"/api/v1/keys/{args.id}")
        if not _out(api, r):
            print(f"{args.id}: {'revoked — apps using it get 401 from now on' if action == 'revoke' else 'deleted'}")
        return
    rows = api("GET", "/api/v1/keys")
    if _out(api, rows):
        return
    _table([[r["id"], r["name"], r.get("app") or "", r["prefix"] + "…", ", ".join(r["aliases"]) or "all",
             r["state"], _ago(r.get("last_used_at"))] for r in rows],
           ["id", "name", "app", "key", "aliases", "state", "last used"])


def cmd_usage(args, api) -> None:
    from .cli import _out, _table
    if args.csv:
        text = api("GET", "/api/v1/usage", params={"days": args.days, "format": "csv"})
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            print(text, end="")
        return
    r = api("GET", "/api/v1/usage", params={"days": args.days})
    if _out(api, r):
        return
    if not r["rows"]:
        print(f"no requests in the last {args.days} days")
        return
    by: dict[tuple[str, str], dict[str, int]] = {}
    for row in r["rows"]:
        t = by.setdefault((row["key_id"], row["alias"]), {"requests": 0, "prompt_tokens": 0,
                                                          "completion_tokens": 0, "estimated_requests": 0})
        for k in t:
            t[k] += row[k]
    _table([[r["names"].get(k, k), a, f"{v['requests']:,}", f"{v['prompt_tokens']:,}", f"{v['completion_tokens']:,}",
             f"{v['estimated_requests']:,}" if v["estimated_requests"] else ""]
            for (k, a), v in sorted(by.items(), key=lambda kv: -kv[1]["completion_tokens"])],
           ["key", "alias", "requests", "prompt tokens", "completion tokens", "estimated"])
    print(f"\nlast {args.days} days; 'estimated' requests streamed without usage (counted by events)")


def add_parsers(sub, cmd) -> None:
    k = sub.add_parser("keys", help="inference keys: one per app, limited to aliases, revocable")
    k.set_defaults(fn=cmd_keys)
    ks = k.add_subparsers(dest="keys_cmd", metavar="<action>")
    ks.add_parser("list", help="all keys and their state").set_defaults(fn=cmd_keys)
    s = ks.add_parser("create", help="a new key (printed once)")
    s.set_defaults(fn=cmd_keys)
    s.add_argument("name")
    s.add_argument("--alias", action="append", help="only these aliases (repeatable; default all)")
    s.add_argument("--expires", type=int, metavar="DAYS")
    s.add_argument("--app", help="what uses it (shown in the list)")
    for action, help_ in (("revoke", "stop a key working (at once)"), ("delete", "remove a revoked or expired key")):
        s = ks.add_parser(action, help=help_)
        s.set_defaults(fn=cmd_keys)
        s.add_argument("id")
    u = sub.add_parser("usage", help="requests and tokens per key and alias, per day")
    u.set_defaults(fn=cmd_usage)
    u.add_argument("--days", type=int, default=30)
    u.add_argument("--csv", action="store_true")
    u.add_argument("-o", "--output")
