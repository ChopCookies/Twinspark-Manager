"""A profile's required mods: their state on every node, and installing a recipe's pinned patch.

See :mod:`twinspark.mod_artifacts` for the descriptor. This module adds the cluster side:

* ``status``: per required mod, per node: present, hash, and whether it is the recipe's pinned patch
* ``install``: fetch the pinned file, build the mod, install identical content on every node, check again
* ``check_plan``: called while a switch or a preparation resolves, before anything is downloaded. A mod
  that a revision pins but that the nodes have with other content stops the job, with the fix in the message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from .. import mod_artifacts as ma
from .agent_client import AgentActionError

if TYPE_CHECKING:
    from .controller import Controller


class PrerequisiteError(RuntimeError):
    pass


def _draft(ctrl: "Controller", profile: str):
    p = ctrl.get_profile(profile)
    if p is None:
        raise PrerequisiteError(f"no profile {profile}")
    d = p.working_draft() or (p.revisions[-1].draft if p.revisions else None)
    if d is None:
        raise PrerequisiteError(f"profile {profile} has no draft")
    return d


def required_names(draft) -> list[str]:
    return sorted({m for part in draft.parts() for m in part.advanced.mods})


def hint(profile: str) -> str:
    return (f"the recipe pins this patch: install it with `tsm mods required {profile} --install` (or Mods → "
            f"Install pinned patch in the GUI), then retry")


async def status(ctrl: "Controller", profile: str) -> dict[str, Any]:
    draft = _draft(ctrl, profile)
    names = required_names(draft)
    try:
        arts = ma.of_source(draft.source)
        art_error = None
    except ma.ArtifactError as exc:
        arts, art_error = {}, str(exc)
    per_node: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for n, agent in sorted(ctrl.agents.items()):
        try:
            per_node[n] = (await agent.call("mods_status", names=names, timeout=30))["status"] if names else {}
        except AgentActionError as exc:
            errors[n] = exc.detail
    mods = []
    for name in names:
        art = arts.get(name)
        nodes = {}
        for n, st in per_node.items():
            s = st.get(name) or {}
            nodes[n] = {"present": bool(s.get("present")), "hash": s.get("hash"),
                        "matches_recipe": ma.matches(art, s.get("files")) if art and s.get("present") else None}
        present = [v for v in nodes.values() if v["present"]]
        hashes = {v["hash"] for v in present}
        if len(present) < len(nodes) or errors:
            state = "missing"
        elif len(hashes) > 1:
            state = "inconsistent"
        else:
            state = "installed"
        matches = None
        if art and present:
            flags = [v["matches_recipe"] for v in present]
            matches = None if None in flags else all(flags) and len(present) == len(nodes)
        entry: dict[str, Any] = {"name": name, "state": state, "nodes": nodes, "matches_recipe": matches,
                                 "installable": art is not None}
        if art:
            entry["artifact"] = {k: art.get(k) for k in ("kind", "repo", "commit", "path", "sha256", "target",
                                                       "license", "image", "why")}
            entry["artifact"].update(review_url=ma.review_url(art), raw_url=ma.raw_url(art))
        mods.append(entry)
    ready = all(m["state"] == "installed" and m["matches_recipe"] is not False for m in mods)
    return {"profile": profile, "mods": mods, "ready": ready and not errors, "errors": errors,
            "artifact_error": art_error}


async def install(ctrl: "Controller", profile: str, name: str,
                  fetch: Optional[Callable[[dict], Any]] = None) -> dict[str, Any]:
    """Install the recipe's pinned patch ``name`` on every node (the same content everywhere)."""
    from .files import install_mod
    draft = _draft(ctrl, profile)
    if name not in required_names(draft):
        raise PrerequisiteError(f"profile {profile} does not use mod {name}")
    try:
        art = ma.of_source(draft.source).get(name)
    except ma.ArtifactError as exc:
        raise PrerequisiteError(str(exc)) from exc
    if art is None:
        raise PrerequisiteError(f"the recipe of {profile} does not pin mod {name}: install it by hand "
                                f"(`tsm mods install` or `tsm mods file-patch`)")
    data = await (fetch or ma.fetch)(art)
    archive = ma.pack(ma.files(art, data))
    res = await install_mod(ctrl, name, archive)
    ctrl._audit("user", "mod.install_pinned", f"mod/{name}",
                {"profile": profile, "repo": art["repo"], "commit": art["commit"], "path": art["path"],
                 "sha256": art["sha256"], "ok": res.get("ok")})
    after = await status(ctrl, profile)
    return {"install": res, "mod": next(m for m in after["mods"] if m["name"] == name), "ready": after["ready"]}


def check_plan(profile: str, source: Any, mods_by_node: dict[str, dict[str, Any]]) -> None:
    """While a revision resolves: each mod it pins must be the pinned patch on every node."""
    try:
        arts = ma.of_source(source)
    except ma.ArtifactError:
        return                                # an invalid descriptor was reported when the recipe was imported
    for node, st in sorted(mods_by_node.items()):
        for name, s in st.items():
            art = arts.get(name)
            if art and s.get("present") and ma.matches(art, s.get("files")) is False:
                raise RuntimeError(f"mod {name} on node {node} is not the patch this revision pins "
                                   f"({art['repo']}@{art['commit'][:12]}:{art['path']}) — {hint(profile)}")
