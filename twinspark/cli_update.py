"""``tsm update`` — new TwinSpark versions, from the project's GitHub releases.

    tsm update                    # what runs where, and what is published (the same as `tsm update check`)
    tsm update sparks             # both Sparks: node B, then node A; the model keeps serving
    tsm update here               # this computer (a pipx or venv install of the client)
    tsm update status             # progress of the Sparks' update
    sudo tsm update policy off    # on a Spark: allow only manual updates there (on | off | show)

``--version X`` installs that published version, also an older one: that is how to go back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from . import __version__, local_update, releases

DEFAULT_POLICY = Path("/etc/twinspark/update-policy.json")
POLICY = Path(os.environ.get("TSM_UPDATE_POLICY") or DEFAULT_POLICY)        # another path: staging and tests
FOLLOW_TIMEOUT_S = 3600


def _api(args):
    from .cli import Api, _key
    return Api(args.api, _key(args), args.json)


def _headers(args) -> dict[str, str]:
    from .cli import _key
    key = _key(args)
    return {"x-api-key": key} if key else {}


def _release(args) -> tuple[Optional[dict], Optional[str]]:
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as c:
            return releases.latest(c, version=args.target_version, allow_prerelease=args.prerelease), None
    except releases.ReleaseError as exc:
        return None, str(exc)
    except httpx.HTTPError as exc:
        return None, f"GitHub is not reachable from this computer ({type(exc).__name__})"


def _sparks(args, refresh: bool = True) -> tuple[Optional[dict], Optional[str]]:
    """The controller's view, without exiting when node A is not reachable from here."""
    try:
        r = httpx.post(f"{args.api.rstrip('/')}/api/v1/system/update/check",
                       params={"refresh": str(refresh).lower(), "prerelease": str(args.prerelease).lower()},
                       headers=_headers(args), timeout=90)
    except httpx.HTTPError as exc:
        return None, (f"node A is not reachable at {args.api} ({type(exc).__name__}); from a workstation: "
                      f'eval "$(tsm client env <name>)" first')
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        return None, f"node A answered {r.status_code}: {detail}"
    return r.json(), None


def _line(label: str, version: Optional[str], note: str = "") -> None:
    print(f"  {label:<16} {version or '?':<12} {note}".rstrip())


def cmd_check(args) -> None:
    rel, rel_err = _release(args)
    kind = local_update.install_kind()
    sparks, sp_err = _sparks(args)
    if args.json:
        print(json.dumps({"here": {"version": __version__, **kind}, "release": rel, "release_error": rel_err,
                          "sparks": sparks, "sparks_error": sp_err}, indent=2, default=str))
        return
    latest = rel["version"] if rel else None
    print("TwinSpark versions")
    if kind["kind"] != "spark":
        note = f"({kind['kind']})"
        if latest and releases.newer(latest, __version__):
            note += f"  → {latest}: tsm update here" if kind["ok"] else f"  → {latest}: {kind['why']}"
        _line("this computer", __version__, note)
    if sparks:
        for name, v in sorted((k, v) for k, v in sparks["versions"].items() if k != "controller"):
            behind = latest and (not v or releases.newer(latest, v))
            _line(f"node {name}", v, f"→ {latest}" if behind else "")
        if sparks.get("problems"):
            for p in sparks["problems"]:
                print(f"    ! {p}")
        elif sparks.get("available"):
            print("  update both: tsm update sparks")
        st = sparks.get("state") or {}
        if st.get("state") in ("running", "failed"):
            print(f"  last update run: {st['state']} — {st.get('error') or st.get('phase')}")
    elif sp_err:
        print(f"  Sparks           {sp_err}")
    if rel:
        pre = " (pre-release)" if rel.get("prerelease") else ""
        when = (rel.get("published_at") or "")[:10]
        print(f"  newest release   {rel['version']}{pre}  {when}  {rel.get('html_url', '')}")
    else:
        print(f"  newest release   {rel_err}")


def _confirm(prompt: str, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        sys.exit("not confirmed (add --yes when running without a terminal)")
    if not input(f"{prompt} [y/N] ").strip().lower().startswith("y"):
        sys.exit("cancelled")


def _get_state(args) -> Optional[dict]:
    try:
        r = httpx.get(f"{args.api.rstrip('/')}/api/v1/system/update", headers=_headers(args), timeout=20)
    except httpx.HTTPError:
        return None
    return r.json() if r.status_code == 200 else None


def follow(args, run_id: str, *, timeout: float = FOLLOW_TIMEOUT_S, sleep=time.sleep) -> dict:
    """Print each step until the run ends; node A's controller restarting in between is expected."""
    deadline, last, away = time.monotonic() + timeout, None, False
    while time.monotonic() < deadline:
        st = _get_state(args)
        if st is None:
            if not away:
                print("  … node A's controller is restarting with the new version; waiting for it")
                away = True
            sleep(3)
            continue
        away = False
        if st.get("run_id") != run_id:
            sys.exit(f"another update run ({st.get('run_id', '?')[:12]}) replaced this one")
        line = st.get("waiting") or st.get("phase")
        if line != last:
            print(f"  {line}")
            last = line
        if st.get("state") == "completed":
            print(f"done: both Sparks run TwinSpark {st['target']['version']}"
                  + (" (simulated: dry-run nodes)" if st.get("dry_run") else ""))
            return st
        if st.get("state") == "failed":
            sys.exit(f"the update failed: {st.get('error')}")
        sleep(2)
    sys.exit("still running after an hour — `tsm update status` shows where it is")


def cmd_sparks(args) -> None:
    api = _api(args)
    info = api("POST", f"/api/v1/system/update/check?refresh=true&prerelease={str(args.prerelease).lower()}")
    if info.get("problems"):
        sys.exit("cannot update the Sparks:\n  " + "\n  ".join(info["problems"]))
    rel = info.get("release") or {}
    want = args.target_version or rel.get("latest")
    if not want:
        sys.exit(info.get("release_error") or "no release found")
    nodes = ", ".join(f"{n} {v or '?'}" for n, v in sorted(info["versions"].items()) if n != "controller")
    print(f"Sparks: {nodes} → {want}")
    print(f"  {info['note']}")
    if info.get("dry_run"):
        print("  (dry-run nodes: the update is simulated, nothing is installed)")
    _confirm(f"Update node {' then node '.join(info['order'])} to {want}?", args.yes)
    st = api("POST", "/api/v1/system/update/start",
             json={"confirm": "UPDATE TWINSPARK", "version": args.target_version, "prerelease": args.prerelease})
    if args.json:
        print(json.dumps(st, indent=2))
        return
    print(f"started update {st['run_id'][:12]}")
    if args.no_wait:
        print("follow it with: tsm update status")
        return
    follow(args, st["run_id"])


def cmd_status(args) -> None:
    st = _api(args)("GET", "/api/v1/system/update")
    if args.json:
        print(json.dumps(st, indent=2, default=str))
        return
    if st.get("state") == "idle":
        print("no TwinSpark update has run yet")
        return
    t = st.get("target") or {}
    print(f"update {st.get('run_id', '')[:12]} → {t.get('version')}: {st.get('state')}")
    print(f"  {st.get('waiting') or st.get('phase')}")
    for n in st.get("order", []):
        node = (st.get("nodes") or {}).get(n) or {}
        print(f"  node {n}: {node.get('state', 'waiting')}" + (f" — {node['error']}" if node.get("error") else ""))
    if st.get("error"):
        print(f"  error: {st['error']}")


def _relaunch_on_windows(plan: dict, args) -> bool:
    """Windows cannot replace the running tsm.exe: continue in python.exe and let this process end."""
    if os.name != "nt" or os.environ.get("TSM_UPDATE_CHILD") == "1":
        return False
    argv = [sys.executable, "-m", "twinspark.cli", "update", "here", "--yes", "--version", plan["version"]]
    if args.prerelease:
        argv.append("--prerelease")
    subprocess.Popen(argv, env={**os.environ, "TSM_UPDATE_CHILD": "1"})
    print("continuing in a separate Python process, so that tsm.exe can be replaced — wait for its last line")
    return True


def cmd_here(args) -> None:
    kind = local_update.install_kind()
    if not kind["ok"]:
        sys.exit(kind["why"])
    rel, err = _release(args)
    if rel is None:
        sys.exit(err)
    if not args.target_version and not releases.newer(rel["version"], __version__):
        print(f"this computer runs TwinSpark {__version__}, the newest release is {rel['version']}: nothing to do")
        return
    print(f"this computer: {__version__} → {rel['version']} ({kind['kind']}, {kind['prefix']})")
    _confirm(f"Install TwinSpark {rel['version']} here?", args.yes)
    if _relaunch_on_windows(rel, args):
        return
    try:
        got = local_update.update_here(rel)
    except (RuntimeError, releases.ReleaseError, httpx.HTTPError) as exc:
        sys.exit(f"update failed: {exc}")
    print(f"done: this computer runs TwinSpark {got} — restart `tsm client` if it is open")


def read_policy(path: Path = POLICY) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def cmd_policy(args) -> None:
    what = args.value or "show"
    if what == "show":
        pol = read_policy()
        if not pol:
            print(f"{POLICY} is missing: updates through `tsm update sparks` are off on this node "
                  f"(sudo tsm update policy on)")
        else:
            print(f"{POLICY}: updates {'on' if pol.get('enabled') is True else 'off'}"
                  + (", pre-releases allowed" if pol.get("allow_prerelease") is True else ""))
        return
    if what not in ("on", "off"):
        sys.exit("usage: sudo tsm update policy on|off|show [--prerelease]")
    if POLICY == DEFAULT_POLICY and hasattr(os, "geteuid") and os.geteuid() != 0:
        sys.exit("the update policy is root's: sudo tsm update policy " + what)
    pol = {"enabled": what == "on", "allow_prerelease": bool(args.prerelease and what == "on")}
    POLICY.parent.mkdir(parents=True, exist_ok=True)
    tmp = POLICY.with_suffix(".tmp")
    tmp.write_text(json.dumps(pol) + "\n", encoding="utf-8", newline="\n")
    os.chmod(tmp, 0o644)
    tmp.replace(POLICY)
    print(f"{POLICY}: updates {what}" + (", pre-releases allowed" if pol["allow_prerelease"] else ""))


def cmd_update(args, api=None) -> None:
    {"check": cmd_check, "sparks": cmd_sparks, "here": cmd_here, "status": cmd_status,
     "policy": cmd_policy}[args.action](args)


cmd_update.local = True  # type: ignore[attr-defined]  # each action decides whether it needs node A's API


def add_parsers(sub, cmd) -> None:
    s = cmd("update", cmd_update, "update TwinSpark from its GitHub releases: both Sparks, or this computer")
    s.add_argument("action", nargs="?", default="check", choices=["check", "sparks", "here", "status", "policy"],
                   help="check (default) | sparks | here | status | policy")
    s.add_argument("value", nargs="?", help="for policy: on | off | show")
    s.add_argument("--version", dest="target_version", metavar="X.Y.Z",
                   help="install this published version (an older one goes back)")
    s.add_argument("--prerelease", action="store_true", help="consider pre-releases (rc, beta)")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask")
    s.add_argument("--no-wait", action="store_true", help="sparks: start and return")
