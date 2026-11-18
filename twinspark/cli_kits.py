"""``tsm kit`` — model set-ups that run with their author's own scripts (twinspark.kits).

    tsm kit                          # the kits, and which one serves
    tsm kit get NAME                 # clone the kit's repository (~/twinspark-kits/NAME)
    tsm kit update NAME              # pull the author's newest version
    tsm kit prepare NAME             # its one-time set-up (downloads, image), TwinSpark's model keeps serving
    sudo tsm kit start NAME          # TwinSpark's model stops, the kit's start.sh runs, the gateway forwards to it
    sudo tsm kit stop NAME           # the kit's stop.sh; nothing serves until you activate a profile

Run them on node A. The kit's scripts run as you (with ``sudo``: as the user who ran sudo, in that user's login
shell), never as root: they use your ssh key to node B, your Docker and your PATH. TwinSpark only stops its own
model, routes, and stops the kit's containers by name when you switch back.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from . import kits

KITS_HOME = "twinspark-kits"


def _api(args):
    from .cli import Api, _key
    return Api(args.api, _key(args), args.json)


def _user() -> tuple[str, Path, Optional[int]]:
    """(name, home, uid) of the person the kit's scripts run as: the sudo caller, or this user."""
    if not hasattr(os, "geteuid"):
        sys.exit("tsm kit runs on node A (Linux)")
    import pwd
    if os.geteuid() == 0:
        name = os.environ.get("SUDO_USER") or ""
        if not name or name == "root":
            sys.exit("the kit's scripts do not run as root: run tsm kit as your own user, or with sudo from it")
    else:
        name = pwd.getpwuid(os.geteuid()).pw_name
    pw = pwd.getpwnam(name)
    return name, Path(pw.pw_dir), pw.pw_uid


def _checkout(args, kit: kits.Kit, home: Path) -> Path:
    # resolved in both cases: a home folder behind a symlink must not make the scripts look "outside" it
    return (Path(args.dir).expanduser() if args.dir else home / KITS_HOME / kit.name).resolve()


def _as_user(argv: list[str], cwd: Path, env: Optional[dict[str, str]] = None) -> tuple[list[str], dict[str, Any]]:
    """The command and the subprocess arguments that run ``argv`` in ``cwd`` as the kit's user.

    As root (sudo) it runs in that user's login shell: sudo's own PATH has no ``~/.local/bin``, which is where
    ``pip install --user`` puts the ``hf`` command the kits download with. An ssh agent that sudo was told to
    keep (``sudo --preserve-env=SSH_AUTH_SOCK``) is passed on; without one the scripts use the user's key files."""
    name, _, _ = _user()
    extra = dict(env or {})
    if os.geteuid() != 0:
        return argv, {"cwd": cwd, "env": {**os.environ, **extra}}
    if os.environ.get("SSH_AUTH_SOCK"):
        extra.setdefault("SSH_AUTH_SOCK", os.environ["SSH_AUTH_SOCK"])
    inner = (f"cd {shlex.quote(str(cwd))} && exec env "
             + " ".join(shlex.quote(f"{k}={v}") for k, v in extra.items()) + " " + shlex.join(argv))
    return ["runuser", "-l", name, "-c", inner], {}


def _run(argv: list[str], cwd: Path, env: Optional[dict[str, str]] = None) -> int:
    """Run as the kit's user, with this terminal. Returns the exit status."""
    cmd, kw = _as_user(argv, cwd, env)
    return subprocess.call(cmd, **kw)


def _output(argv: list[str], cwd: Path) -> str:
    """The command's output, run as the kit's user ("" when it fails)."""
    cmd, kw = _as_user(argv, cwd)
    p = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, **kw)
    return p.stdout.strip() if p.returncode == 0 else ""


def _owned(path: Path, uid: Optional[int]) -> None:
    """The scripts run from this folder: it must be the user's own, not writable by anyone else."""
    st = path.stat()
    if uid is not None and st.st_uid != uid:
        sys.exit(f"{path} belongs to another user (uid {st.st_uid}): the kit's scripts run from the folder of the "
                 f"user who runs them")
    if st.st_mode & 0o002:
        sys.exit(f"{path} is writable by every user: chmod o-w {path}")


def _script(path: Path, rel: str) -> Path:
    f = (path / rel.removeprefix("./")).resolve()
    if path not in f.parents or not f.is_file():
        sys.exit(f"{rel} is not in {path}: is this the kit's checkout? (tsm kit get)")
    return f


def _on_node_a(args) -> None:
    host = urlparse(args.api).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("run tsm kit start / stop on node A: the kit's server runs there, next to TwinSpark's gateway")


def _ask(args, question: str) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty():
        sys.exit(question + " — answer with --yes")
    if input(question + " [y/N] ").strip().lower() not in ("y", "yes"):
        sys.exit("nothing changed")


# ---- actions ----------------------------------------------------------------------------------------
def _list(args) -> None:
    catalog = kits.load_all()
    external = None
    try:
        external = _api_quiet(args, "GET", "/api/v1/kits").get("external")
    except Exception:  # noqa: BLE001 - the list works without the controller
        pass
    for k in catalog.values():
        mark = ""
        if external and external.get("kit") == k.name:
            mark = f"   ← {external.get('state')}, aliases {', '.join(external.get('aliases') or [])}"
        m = k.measured
        speed = f"{m['single_stream_tok_s']} tok/s one request" if m.get("single_stream_tok_s") else ""
        print(f"{k.name}{mark}\n    {k.title}\n    {k.engine} · {k.model} · {speed}\n"
              f"    container {k.container} on each Spark · port {k.port} on node A\n    {k.repo}")
    if not catalog:
        print("no kits")


def _api_quiet(args, method: str, path: str) -> Any:
    import httpx

    from .cli import _key
    key = _key(args)
    r = httpx.request(method, f"{args.api.rstrip('/')}{path}", headers={"x-api-key": key} if key else {},
                      timeout=10)
    r.raise_for_status()
    return r.json()


def _get(args, kit: kits.Kit) -> None:
    _, home, _ = _user()
    path = _checkout(args, kit, home)
    if (path / ".git").exists():
        print(f"{path} exists already: tsm kit update {kit.name} pulls the newest version")
    else:
        if _run(["mkdir", "-p", str(path.parent)], cwd=home) != 0:      # as the user: the folder is theirs
            sys.exit(f"cannot create {path.parent}")
        if _run(["git", "clone", kit.repo, str(path)], cwd=path.parent) != 0:
            sys.exit(f"git clone {kit.repo} failed")
    print(f"\n{kit.title}\n  {path}\n\nBefore the first start:")
    for r in kit.requirements:
        print(f"  - {r}")
    print(f"\nThen: tsm kit prepare {kit.name}   (once; downloads and the engine image)\n"
          f"      sudo tsm kit start {kit.name}")


def _update(args, kit: kits.Kit) -> None:
    _, home, uid = _user()
    path = _checkout(args, kit, home)
    _owned(path, uid)
    # as the user: root's git refuses a repository that belongs to someone else ("dubious ownership")
    before = _output(["git", "rev-parse", "--short", "HEAD"], cwd=path)
    if _run(["git", "pull", "--ff-only"], cwd=path) != 0:
        sys.exit("git pull failed (local changes? see git status in the checkout)")
    after = _output(["git", "rev-parse", "--short", "HEAD"], cwd=path)
    if not after:
        sys.exit(f"{path} is not a git checkout: tsm kit get {kit.name}")
    if before == after:
        print(f"{kit.name} is at the newest version ({after})")
    else:
        print(f"{kit.name}: {before} → {after}. The next tsm kit start uses it (the kit's start.sh rebuilds or "
              f"pulls what changed).")


def _prepare(args, kit: kits.Kit) -> None:
    _, home, uid = _user()
    path = _checkout(args, kit, home)
    _owned(path, uid)
    script = _script(path, kit.prepare)
    if kit.prepare_needs_idle:
        print(f"note: {kit.name}'s preparation writes large files and competes for the Sparks' shared memory with "
              f"a loaded model: stop TwinSpark's model first (tsm stop) if it is large")
        _ask(args, "prepare now?")
    sys.exit(_run([str(script)], cwd=path))


def _start(args, kit: kits.Kit) -> None:
    _on_node_a(args)
    _, home, uid = _user()
    path = _checkout(args, kit, home)
    if not path.exists():
        sys.exit(f"{path} does not exist: tsm kit get {kit.name}")
    _owned(path, uid)
    script = _script(path, kit.start)
    port = args.port or kit.port
    api = _api(args)
    status = api("GET", "/api/v1/status")
    active = (status.get("active") or {}).get("profile")
    what = f"TwinSpark's model {active!r} stops on both Sparks, then " if active else ""
    _ask(args, f"{what}{kit.name}'s start.sh runs on 127.0.0.1:{port} and the gateway forwards "
               f"{', '.join(args.alias or kit.aliases)} to it (its first start downloads and builds for a long "
               f"time: tsm kit prepare {kit.name} does that while TwinSpark's model keeps serving). Go on?")
    handed = api("POST", f"/api/v1/kits/{kit.name}/handover",
                 json={"confirm": kit.name, "aliases": args.alias or None, "port": port}, timeout=900)
    _memory_report(kit, handed)
    env = kit.env(port)
    print(f"[tsm] running {script.relative_to(path)} in {path} ({' '.join(f'{k}={v}' for k, v in env.items())})",
          flush=True)
    try:
        code = _run([str(script), *args.extra], cwd=path, env=env)
    except KeyboardInterrupt:
        code = 130                                  # interrupted: clean up like any other failed start
    except OSError as exc:                          # the script could not be run at all: the Sparks are handed
        print(f"[tsm] cannot run {kit.start}: {exc}", file=sys.stderr)       # over already, so give them back
        code = 126
    if code != 0:
        print(f"[tsm] {kit.start} ended with code {code}: cleaning up with {kit.stop}", file=sys.stderr)
        _give_back(api, kit, path, env)
        sys.exit(f"{kit.name} did not start. Nothing serves now: tsm activate <profile> brings a TwinSpark model "
                 f"back.")
    res = api("POST", f"/api/v1/kits/{kit.name}/attach", timeout=60, ok=(409,))
    ext = res.get("external") if isinstance(res, dict) else None
    if not ext:
        # the kit's own start succeeded, yet TwinSpark cannot route to it (or the Sparks were taken back
        # meanwhile): an engine nobody can reach must not keep both Sparks' memory
        why = res.get("detail") if isinstance(res, dict) else res
        print(f"[tsm] {kit.name} started but cannot be served through the gateway: {why}", file=sys.stderr)
        _give_back(api, kit, path, env)
        sys.exit(f"{kit.name} was stopped again. Nothing serves now: tsm activate <profile> brings a TwinSpark "
                 f"model back.")
    print(f"\n{kit.title} serves through TwinSpark's gateway: model {', '.join(ext.get('aliases') or [])} "
          f"(served as {ext.get('served')}). sudo tsm kit stop {kit.name} ends it.")


def _memory_report(kit: kits.Kit, handed: Any) -> None:
    """What each Spark has free now that TwinSpark's model is gone, against what the kit asks for."""
    memory = handed.get("memory") if isinstance(handed, dict) else None
    need = kit.min_available_gib
    for node, m in sorted((memory or {}).items()):
        if m.get("error"):
            print(f"[tsm] node {node}: memory unknown ({m['error']})", file=sys.stderr)
            continue
        print(f"[tsm] node {node}: {m['available_gib']:.1f} of {m['total_gib']:.1f} GiB available", flush=True)
        if need and m["available_gib"] < need:
            print(f"[tsm] WARNING: {kit.name} asks for {need:g} GiB available on each Spark with its default "
                  f"settings. Node {node} has less: the kit's own check may refuse to start. Close other programs "
                  f"on the node, or lower the kit's context in its scripts/local.sh.", file=sys.stderr)


def _give_back(api, kit: kits.Kit, path: Path, env: dict[str, str]) -> None:
    """The kit's own stop, then TwinSpark checks both Sparks itself and stops what still runs there."""
    stop = path / kit.stop.removeprefix("./")
    if stop.is_file():
        _run([str(stop)], cwd=path, env=env)
    res = api("POST", "/api/v1/kits/detach", json={"stop": True, "kit": kit.name}, timeout=300)
    for p in (res.get("problems") if isinstance(res, dict) else None) or []:
        print(f"[tsm] {p}", file=sys.stderr)


def _stop(args, kit: kits.Kit) -> None:
    _on_node_a(args)
    _, home, uid = _user()
    path = _checkout(args, kit, home)
    api = _api(args)
    ext = api("GET", "/api/v1/kits").get("external") or {}
    port = ext.get("port") if ext.get("kit") == kit.name else None
    if (path / kit.stop.removeprefix("./")).is_file():
        _owned(path, uid)
        _run([str(_script(path, kit.stop))], cwd=path, env=kit.env(port))
    else:
        print(f"[tsm] no checkout at {path}: TwinSpark stops the kit's containers by name", file=sys.stderr)
    # Whatever the kit's stop reported: TwinSpark looks at both Sparks itself. A stop script that could not reach
    # node B can still end with success, and a rank left there holds that Spark's memory.
    res = api("POST", "/api/v1/kits/detach", json={"stop": True, "kit": kit.name}, timeout=300)
    for p in res.get("problems") or []:
        print(f"[tsm] {p}", file=sys.stderr)
    if res.get("problems"):
        sys.exit(f"{kit.name} did not stop on every Spark. Its routes are closed; retry stop after fixing the nodes.")
    print(f"{kit.name} stopped. Nothing serves now: tsm activate <profile> brings a TwinSpark model back.")


def _status(args) -> None:
    res = _api(args)("GET", "/api/v1/kits")
    ext = res.get("external")
    if not ext:
        print("no kit serves: TwinSpark's own model (if any) does — tsm status")
        return
    print(f"{ext['kit']}: {ext.get('state')} on {ext.get('url')}, model {', '.join(ext.get('aliases') or [])}"
          + (f" (served as {ext['served']})" if ext.get("served") else ""))


def cmd_kit(args, api) -> None:
    if args.action in (None, "list"):
        return _list(args)
    if args.action == "status":
        return _status(args)
    if not args.name:
        sys.exit(f"tsm kit {args.action} NAME (tsm kit lists them)")
    try:
        kit = kits.get(args.name)
    except kits.KitError as exc:
        sys.exit(str(exc))
    {"get": _get, "update": _update, "prepare": _prepare, "start": _start, "stop": _stop}[args.action](args, kit)


cmd_kit.local = True  # type: ignore[attr-defined]     # builds its own API client where it needs one


def add_parsers(sub, cmd) -> None:
    s = cmd("kit", cmd_kit, "model set-ups run by their author's own scripts (Mia's AI Lab), via TwinSpark's gateway")
    s.add_argument("action", nargs="?", choices=["list", "get", "update", "prepare", "start", "stop", "status"])
    s.add_argument("name", nargs="?")
    s.add_argument("--dir", help=f"the kit's checkout (default ~/{KITS_HOME}/NAME of the user running it)")
    s.add_argument("--alias", action="append", help="start: the alias(es) the gateway routes to the kit")
    s.add_argument("--port", type=int, help="start: the kit's port on 127.0.0.1 (default: the kit's own)")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask")
    s.add_argument("extra", nargs="*", help="start: extra arguments for the kit's start.sh (after --)")
