"""The two halves of a split deployment (master plan §2.2): each node's model lives and fails on its own.

A split revision runs one model on node A and another on node B, behind different aliases. A switch to
another revision of the same profile touches only the half that changed: the other keeps its container,
its route and its running requests. The watchdog restarts only the half that failed, and a failed switch
of one half leaves the other serving. TP2, PP2 and the other two-node topologies stay one unit.

What the controller keeps in its ``active`` record for a split deployment:

* ``half_revisions`` ``{node: revision_id}`` — the revision that started each half's container (a half kept
  across switches still runs under its first revision's container name);
* ``halves_down`` ``[node]`` — halves that are not running because their switch failed and nothing was
  restored; the watchdog leaves them alone until a switch or a restart of that half.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from ..schemas.profile import ProfileRevision
from .launch import LaunchPlan

HALF_NODES = ("A", "B")
# draft fields that do not change what runs: a renamed or re-described profile half is the same container
_COSMETIC = {"name", "description", "tags", "verification", "source", "image_hint", "secondary"}


def is_split(rev: Optional[ProfileRevision]) -> bool:
    return rev is not None and rev.draft.secondary is not None


def halves(rev: Optional[ProfileRevision]) -> dict[str, ProfileRevision]:
    """``{node: part}`` of a split revision ({} otherwise)."""
    if not is_split(rev):
        return {}
    return dict(zip(HALF_NODES, rev.parts(), strict=True))


def half_key(part: ProfileRevision) -> str:
    """Everything that decides a half's container: its settings and its pinned model and image."""
    return json.dumps({"draft": part.draft.model_dump(mode="json", exclude=_COSMETIC),
                       "identity": part.identity.model_dump(mode="json")}, sort_keys=True)


def half_aliases(rev: Optional[ProfileRevision], nodes) -> list[str]:
    parts = halves(rev)
    return sorted({a for n in nodes if n in parts for a in parts[n].draft.simple.aliases})


def container_name(profile: str, node: str, revision_id: str) -> str:
    """The name LaunchPlanner gives a split half's container (see launch.LaunchPlanner.plan)."""
    return f"tsm-{profile}-{node.lower()}-{revision_id.rsplit('-', 1)[-1]}"


def serving_plan(plan: LaunchPlan, rev: ProfileRevision, active: Optional[dict]) -> LaunchPlan:
    """``plan`` with the names of the containers that really serve: a half kept from an earlier revision runs
    under that revision's container name. Halves that are down are left out."""
    if not is_split(rev) or not active or active.get("revision_id") != rev.revision_id:
        return plan
    started = active.get("half_revisions") or {}
    down = set(active.get("halves_down") or [])
    if not started and not down:
        return plan
    p = plan.model_copy(deep=True)
    for c in p.containers:
        rid = started.get(c.node)
        if rid and rid != rev.revision_id:
            c.name = container_name(rev.profile_name, c.node, rid)
    return scoped_plan(p, [n for n in HALF_NODES if n not in down]) if down else p


def scoped_plan(plan: LaunchPlan, nodes) -> LaunchPlan:
    """The part of ``plan`` that runs on ``nodes``: its containers, their routes, their weights."""
    nodes = set(nodes)
    containers = [c for c in plan.containers if c.node in nodes]
    names = {c.name for c in containers}
    urls = {c.health_url for c in containers if c.health_url}
    routes = {a: u for a, u in plan.routes.items() if set(u) & urls}
    waves = [[n for n in wave if n in names] for wave in plan.start_order]
    delays = [d for wave, d in zip(waves, plan.wave_delays_s + [0.0] * len(waves), strict=False) if wave]
    reqs = []
    for m in plan.model_requirements:
        on = [n for n in m.get("nodes") or [] if n in nodes]
        if on:
            reqs.append({**m, "nodes": on})
    return plan.model_copy(update={
        "containers": containers, "routes": routes,
        "route_models": {a: m for a, m in plan.route_models.items() if a in routes},
        "start_order": [w for w in waves if w], "wave_delays_s": delays,
        "node_utilization": {n: u for n, u in plan.node_utilization.items() if n in nodes},
        "model_requirements": reqs,
        "mods": list(dict.fromkeys(m for c in containers for m in c.mods)),
    }, deep=True)


def decide_scope(*, active: Optional[dict], active_rev: Optional[ProfileRevision], rev: ProfileRevision,
                 nodes: Optional[list[str]], serving: Callable[[str], bool]) -> Optional[dict]:
    """Which halves a switch to ``rev`` has to touch.

    None means the whole deployment (every node is drained, stopped and started). Otherwise
    ``{"nodes", "aliases", "kept", "kept_aliases"}``: only ``nodes`` are touched and only ``aliases`` drained.

    ``nodes`` given: restart exactly those halves of the active revision (the GUI's "restart this half", the
    watchdog's recovery). Not given: a half is kept when the active deployment is a split of the same
    profile, that half is identical in ``rev`` and it is serving (``serving(node)``)."""
    if nodes:
        wanted = sorted(set(nodes))
        if not is_split(rev):
            raise ValueError("only a split deployment has halves that can be restarted on their own")
        if any(n not in HALF_NODES for n in wanted):
            raise ValueError("a split deployment has the halves A and B")
        if active_rev is None or active_rev.revision_id != rev.revision_id:
            raise ValueError("a single half can be restarted only for the active revision; a switch to another "
                             "revision touches the halves that changed by itself")
        scope = wanted
    else:
        if not (is_split(rev) and is_split(active_rev) and active and active_rev.profile_name == rev.profile_name):
            return None
        down = set(active.get("halves_down") or [])
        if active_rev.revision_id == rev.revision_id and not down:
            return None                    # the same revision again: a deliberate restart of both halves
        old, new = halves(active_rev), halves(rev)
        keep = [n for n in HALF_NODES if n not in down and half_key(old[n]) == half_key(new[n]) and serving(n)]
        scope = [n for n in HALF_NODES if n not in keep]
    if not scope or len(scope) == len(HALF_NODES):
        return None
    kept = [n for n in HALF_NODES if n not in scope]
    return {"nodes": scope, "kept": kept,
            "aliases": sorted(set(half_aliases(active_rev, scope)) | set(half_aliases(rev, scope))),
            "kept_aliases": half_aliases(rev, kept)}


def merged_record(previous: dict, rev: ProfileRevision, scope: dict, now: float) -> dict:
    """The ``active`` record after a switch that touched only ``scope['nodes']``."""
    started = dict(previous.get("half_revisions") or {n: previous["revision_id"] for n in HALF_NODES})
    since = dict(previous.get("half_since") or {n: previous.get("since") for n in HALF_NODES})
    for n in scope["nodes"]:
        started[n], since[n] = rev.revision_id, now
    rec = {"profile": rev.profile_name, "revision_id": rev.revision_id, "label": rev.label,
           "alias": rev.draft.simple.api_alias, "aliases": rev.draft.aliases, "since": now,
           "half_since": since}
    if any(rid != rev.revision_id for rid in started.values()):
        rec["half_revisions"] = started
    return rec


def down_record(previous: dict, nodes, reason: str) -> dict:
    """``previous`` with ``nodes`` marked down: their switch failed and nothing was restored there."""
    down = sorted(set(previous.get("halves_down") or []) | set(nodes))
    reasons = {**(previous.get("down_reasons") or {}), **{n: reason[:300] for n in nodes}}
    return {**previous, "halves_down": down, "down_reasons": reasons}
