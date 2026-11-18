"""Kits: model set-ups that run with their author's own scripts, next to TwinSpark.

Some published two-Spark set-ups change every day and come with their own launcher (a ``start.sh`` that builds
or pulls the engine, checks memory, starts both ranks and smoke-tests them). Porting them into TwinSpark would
freeze them at one day's state. A kit leaves them as they are:

* ``tsm kit get NAME`` clones the author's repository; ``tsm kit update NAME`` pulls it.
* ``tsm kit start NAME`` hands the Sparks over: TwinSpark stops its own model on both nodes, the kit's
  ``start.sh`` runs as you (your ssh key, your Docker), bound to ``127.0.0.1`` on node A, and TwinSpark's gateway
  forwards to it. Clients keep TwinSpark's endpoint and keys.
* ``tsm kit stop NAME`` runs the kit's ``stop.sh``. Switching to a TwinSpark profile stops the kit's containers
  first.

TwinSpark never runs a kit's code itself: the controller only stops the kit's containers by name and forwards
requests. This module is the catalog (``cookbook/kits/*.json``) and is shared by the CLI and the controller.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

KITS_DIR = Path(__file__).resolve().parent / "cookbook" / "kits"
STATE_KEY = "external"                       # the controller's record of a kit that serves

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}\Z")
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}\Z")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")     # what the agent's foreign_stop accepts
_REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
_SCRIPT_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9_./-]{1,100}\Z")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")
# The environment variables a kit's scripts read for where the server listens and what its containers are called.
# `tsm kit start` and `stop` set them: the server stays on loopback, behind TwinSpark's gateway, and the containers
# carry the name TwinSpark stops when you switch back, whatever the kit's own settings file says.
SCRIPT_ENV = {"host": "HOST", "port": "PORT", "container": "CONTAINER_NAME"}


class KitError(ValueError):
    pass


@dataclass(frozen=True)
class Kit:
    name: str
    title: str
    repo: str
    author: str
    license: str
    engine: str
    model: str
    served_name: str
    container: str                            # the same name on both Sparks
    port: int = 8888
    aliases: tuple[str, ...] = ("default",)
    start: str = "./start.sh"
    stop: str = "./stop.sh"
    prepare: str = "scripts/prepare.sh"
    min_available_gib: float | None = None    # what the kit's start.sh checks on each Spark (shown, not enforced)
    prepare_needs_idle: bool = False          # its preparation competes with a loaded model for memory
    script_env: tuple[tuple[str, str], ...] = tuple(SCRIPT_ENV.items())     # what -> the variable's name
    measured: dict[str, Any] = field(default_factory=dict)
    requirements: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def env(self, port: Optional[int] = None) -> dict[str, str]:
        """What the kit's scripts are started with: loopback, the port, and the container name TwinSpark knows."""
        values = {"host": "127.0.0.1", "port": str(port or self.port), "container": self.container}
        return {var: values[what] for what, var in self.script_env}

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "repo": self.repo, "author": self.author,
                "license": self.license, "engine": self.engine, "model": self.model,
                "served_name": self.served_name, "container": self.container, "port": self.port,
                "aliases": list(self.aliases), "min_available_gib": self.min_available_gib,
                "prepare_needs_idle": self.prepare_needs_idle, "measured": dict(self.measured),
                "requirements": list(self.requirements), "notes": list(self.notes)}


def parse(doc: Any) -> Kit:
    if not isinstance(doc, dict):
        raise KitError("a kit is a JSON object")

    def text(key: str, limit: int = 300, required: bool = True) -> str:
        v = doc.get(key)
        if v is None and not required:
            return ""
        if not isinstance(v, str) or not v.strip() or len(v) > limit:
            raise KitError(f"{key} must be a non-empty string of at most {limit} characters")
        return v.strip()

    name = text("name", 63)
    if not _NAME_RE.match(name):
        raise KitError(f"invalid kit name {name!r}")
    repo = text("repo", 250)
    if not _REPO_RE.match(repo):
        raise KitError(f"{name}: repo must be https://github.com/<owner>/<repo>")
    container = text("container", 128)
    if not _CONTAINER_RE.match(container):
        raise KitError(f"{name}: invalid container name {container!r}")
    port = doc.get("port", 8888)
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise KitError(f"{name}: port must be 1024-65535")
    aliases = doc.get("aliases", ["default"])
    if not isinstance(aliases, list) or not aliases or not all(isinstance(a, str) and _ALIAS_RE.match(a)
                                                                for a in aliases):
        raise KitError(f"{name}: aliases must be a non-empty list of alias names")
    scripts = {}
    for key, default in (("start", "./start.sh"), ("stop", "./stop.sh"), ("prepare", "scripts/prepare.sh")):
        v = doc.get(key, default)
        if not isinstance(v, str) or not _SCRIPT_RE.match(v.removeprefix("./")):
            raise KitError(f"{name}: {key} must be a script path inside the checkout")
        scripts[key] = v
    mem = doc.get("min_available_gib")
    if mem is not None and (isinstance(mem, bool) or not isinstance(mem, (int, float)) or not 0 < mem <= 256):
        raise KitError(f"{name}: min_available_gib must be a number of GiB")
    lists = {}
    for key in ("requirements", "notes"):
        v = doc.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise KitError(f"{name}: {key} must be a list of strings")
        lists[key] = tuple(v)
    measured = doc.get("measured", {})
    if not isinstance(measured, dict):
        raise KitError(f"{name}: measured must be an object")
    env = doc.get("script_env", SCRIPT_ENV)
    if (not isinstance(env, dict) or set(env) - set(SCRIPT_ENV)
            or not all(isinstance(v, str) and _ENV_RE.match(v) for v in env.values())):
        raise KitError(f"{name}: script_env names environment variables for {', '.join(SCRIPT_ENV)}")
    return Kit(name=name, title=text("title"), repo=repo, author=text("author"), license=text("license", 60),
               engine=text("engine"), model=text("model"), served_name=text("served_name", 120),
               container=container, port=port, aliases=tuple(dict.fromkeys(aliases)),
               min_available_gib=float(mem) if mem is not None else None,
               prepare_needs_idle=bool(doc.get("prepare_needs_idle", False)), measured=measured,
               requirements=lists["requirements"], notes=lists["notes"], script_env=tuple(env.items()),
               **scripts)


def load_all(folder: Path = KITS_DIR) -> dict[str, Kit]:
    out: dict[str, Kit] = {}
    for p in sorted(folder.glob("*.json")):
        kit = parse(json.loads(p.read_text(encoding="utf-8")))
        if kit.name != p.stem:
            raise KitError(f"{p.name}: the file name must be the kit name ({kit.name}.json)")
        out[kit.name] = kit
    return out


def get(name: str) -> Kit:
    kits = load_all()
    if name not in kits:
        raise KitError(f"no kit {name!r} (known: {', '.join(kits) or 'none'})")
    return kits[name]
