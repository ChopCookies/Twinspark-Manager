"""A kit serving instead of TwinSpark's own model (twinspark.kits): hand-over, routing, hand-back.

The controller never runs a kit's scripts: ``tsm kit start`` runs them as the operator on node A. Here:

* ``handover`` stops TwinSpark's containers on both Sparks, waits until their memory is free again (as a switch
  does), and records that the kit is starting; the kit's aliases answer 503 "starting" meanwhile.
* ``attach`` checks the kit's server on ``127.0.0.1:<port>`` and routes the aliases to it. The gateway sends it no
  backend key (it is not TwinSpark's engine); clients keep TwinSpark's endpoint and keys.
* ``detach`` closes the routes and, when asked, stops the kit's containers by name on both Sparks (a switch to a
  TwinSpark profile, ``tsm stop``, or Stop in the GUI).
* ``watch`` marks the routes down while the kit's server does not answer, and up again when it does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

import httpx

from .. import kits
from .agent_client import AgentActionError

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger("twinspark.controller.kits")
HEALTH_TIMEOUT_S = 5.0
SETTLE_MAX_S = 90.0             # how long a node's memory may take to come back after its containers stopped


class KitStateError(RuntimeError):
    """The request does not fit what is serving now (409)."""


class HandoverError(RuntimeError):
    """The Sparks could not be freed for the kit (502): nothing was handed over."""


def state(ctrl: "Controller") -> Optional[dict[str, Any]]:
    rec = ctrl.store.kv_get(kits.STATE_KEY)
    return rec if isinstance(rec, dict) and rec.get("kit") else None


async def probe(url: str) -> tuple[bool, Optional[str]]:
    """(healthy, served model id) of a kit's server: ``/health`` 200, the id from ``/v1/models``."""
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_S) as c:
            r = await c.get(f"{url}/health")
            if r.status_code != 200:
                return False, None
            served = None
            try:
                m = await c.get(f"{url}/v1/models")
                data = m.json().get("data") if m.status_code == 200 else None
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    served = str(data[0].get("id") or "")[:200] or None
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
            return True, served
    except httpx.HTTPError:
        return False, None


def _aliases(kit: kits.Kit, aliases: Optional[list[str]]) -> list[str]:
    out = list(dict.fromkeys(aliases or kit.aliases))
    for a in out:
        if not kits._ALIAS_RE.match(a):
            raise ValueError(f"invalid alias {a!r}")
    return out


async def handover(ctrl: "Controller", name: str, aliases: Optional[list[str]] = None,
                   port: Optional[int] = None, actor: str = "user") -> dict[str, Any]:
    from .controller import BusyError
    kit = kits.get(name)
    use = _aliases(kit, aliases)
    port = port or kit.port
    if not 1024 <= port <= 65535:
        raise ValueError("port must be 1024-65535")
    if ctrl.busy():
        raise BusyError("an activation is in progress")
    if ctrl.selfupdate.blocking():
        raise BusyError("TwinSpark is being updated; start the kit when the update has finished")
    cur = state(ctrl)
    if cur:
        raise KitStateError(f"the kit {cur['kit']} is still tracked: stop it first (tsm kit stop {cur['kit']})")
    # Always, not only with an active model: a failed switch or an unreachable node can leave a TwinSpark
    # container behind, and the kit must never load next to one (on GB10, running out of memory freezes the node).
    was_active = bool(ctrl.active())
    job = await ctrl.stop()
    if job.error:
        raise HandoverError(f"TwinSpark's containers could not be stopped on every Spark: {job.error}")
    # the stop job's step reads "stopped [<node:container>, …]": an empty list means nothing was running
    stopped = [s.message for s in job.steps if s.message and s.message != "stopped []"]
    memory = await settle_memory(ctrl, reclaim=was_active or bool(stopped))
    rec = {"kit": kit.name, "title": kit.title, "state": "starting", "aliases": use, "port": port,
           "url": f"http://127.0.0.1:{port}", "served": None, "containers": {n: kit.container for n in ctrl.agents},
           "since": time.time(), "by": actor}
    ctrl.store.kv_set(kits.STATE_KEY, rec)
    for a in use:
        ctrl.gateway.set_route(a, [], kit.served_name, revision_id=f"kit:{kit.name}", keyless=True)
        ctrl.gateway.mark_down(a, f"{kit.title} is starting", retry_after=30)
    ctrl._audit(actor, "kit.handover", f"kit/{kit.name}", {"aliases": use, "stopped": stopped, "memory": memory})
    return {"external": rec, "stopped_twinspark_model": stopped, "memory": memory,
            "min_available_gib": kit.min_available_gib}


async def settle_memory(ctrl: "Controller", reclaim: bool = True) -> dict[str, dict[str, Any]]:
    """Each node's available memory, once it has stopped changing. After a model was stopped this is what a
    switch does before it starts the next one: the page cache is released (``runtime.drop_caches_before_start``)
    and MemAvailable is read until it is stable. The kit's own start checks its memory the moment it runs."""
    rt = ctrl.config.runtime
    out: dict[str, dict[str, Any]] = {}
    for node, agent in sorted(ctrl.agents.items()):
        try:
            tel: dict[str, Any] = {}
            deadline = time.monotonic()
            if reclaim:
                rec = await agent.call("reclaim_memory", drop_caches=rt.drop_caches_before_start, timeout=180)
                tel = rec.get("after") or {}
                deadline += 1.0 if rec.get("dry_run") else SETTLE_MAX_S
            last, stable = -1.0, 0
            while True:
                tel = await agent.call("memory_telemetry")
                avail = float(tel.get("mem_available_gib") or 0)
                stable = stable + 1 if abs(avail - last) < 0.5 else 0
                last = avail
                if stable >= 2 or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(min(max(ctrl.poll_interval, 0.01), 3.0))
            out[node] = {"available_gib": round(float(tel.get("mem_available_gib") or 0), 1),
                         "total_gib": round(float(tel.get("mem_total_gib") or 0), 1)}
        except AgentActionError as exc:
            out[node] = {"error": exc.detail}
    return out


async def attach(ctrl: "Controller", name: str, actor: str = "user") -> dict[str, Any]:
    rec = state(ctrl)
    if not rec or rec["kit"] != name:
        raise KitStateError(f"the kit {name} was not handed the Sparks (tsm kit start {name} does that first)")
    if rec["state"] != "starting":
        raise KitStateError(f"the kit {name} is not starting")
    kit = kits.get(name)
    healthy, served = await probe(rec["url"])
    if state(ctrl) != rec:
        raise KitStateError(f"the kit {name} changed while checking its health")
    if not healthy:
        raise KitStateError(f"the kit's server does not answer on {rec['url']}/health")
    served = served or kit.served_name
    for a in rec["aliases"]:
        ctrl.gateway.set_route(a, [rec["url"]], served, revision_id=f"kit:{kit.name}", keyless=True)
    rec.update(state="serving", served=served, attached_at=time.time())
    ctrl.store.kv_set(kits.STATE_KEY, rec)
    ctrl._audit(actor, "kit.attach", f"kit/{name}", {"aliases": rec["aliases"], "served": served})
    return {"external": rec}


async def detach(ctrl: "Controller", stop_containers: bool, actor: str = "user",
                 name: Optional[str] = None) -> dict[str, Any]:
    rec = state(ctrl)
    if not rec:
        return {"external": None, "stopped": []}
    if name and rec["kit"] != name:
        raise KitStateError(f"the kit serving is {rec['kit']}, not {name}")
    for a in rec.get("aliases") or []:
        if ctrl.gateway.routes.get(a) is not None and ctrl.gateway.routes[a].revision_id == f"kit:{rec['kit']}":
            ctrl.gateway.clear_route(a)
    rec = {**rec, "state": "stopping"}
    ctrl.store.kv_set(kits.STATE_KEY, rec)
    stopped, problems = await stop_containers_of(ctrl.agents, rec) if stop_containers else ([], [])
    remaining = {**rec, "state": "stop-failed"} if problems else None
    ctrl.store.kv_set(kits.STATE_KEY, remaining)
    ctrl._audit(actor, "kit.detach", f"kit/{rec['kit']}", {"stopped": stopped, "problems": problems})
    return {"external": remaining, "was": rec["kit"], "stopped": stopped, "problems": problems}


async def stop_containers_of(agents: dict[str, Any], rec: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Stop the kit's containers by name on each Spark (a container that is not running is not a problem)."""
    stopped: list[str] = []
    problems: list[str] = []
    for node, cname in (rec.get("containers") or {}).items():
        agent = agents.get(node)
        if agent is None:
            problems.append(f"node {node}: agent is not configured")
            continue
        if not isinstance(cname, str):
            continue
        try:
            running = {c.get("name") for c in (await agent.call("foreign_list", timeout=30))["foreign"]}
            if cname not in running:
                continue
            await agent.call("foreign_stop", name=cname, confirm=cname, timeout=120)
            stopped.append(f"{node}:{cname}")
        except AgentActionError as exc:
            problems.append(f"node {node}: {exc.detail}")
            log.warning("could not stop the kit container %s on node %s: %s", cname, node, exc.detail)
    return stopped, problems


async def watch(ctrl: "Controller") -> Optional[str]:
    """One health check of a serving kit: the routes go down while it does not answer."""
    rec = state(ctrl)
    if not rec or rec.get("state") != "serving":
        return None
    healthy, _ = await probe(rec["url"])
    current = state(ctrl)
    if not current or current.get("state") != "serving" or current.get("since") != rec.get("since"):
        return None                                # handed back meanwhile
    for a in rec["aliases"]:
        if healthy:
            ctrl.gateway.mark_up(a)
        else:
            ctrl.gateway.mark_down(a, f"{rec.get('title') or rec['kit']} does not answer on {rec['url']}/health")
    return None if healthy else f"kit {rec['kit']} does not answer"


async def reattach(ctrl: "Controller") -> None:
    """After a controller restart: route a kit that is still serving again (or mark it down)."""
    rec = state(ctrl)
    if not rec:
        return
    for a in rec["aliases"]:
        ctrl.gateway.set_route(a, [rec["url"]] if rec.get("state") == "serving" else [],
                               rec.get("served") or rec["kit"], revision_id=f"kit:{rec['kit']}", keyless=True)
    if rec.get("state") == "serving":
        await watch(ctrl)
    else:
        for a in rec["aliases"]:
            reason = "is starting" if rec["state"] == "starting" else "has not stopped; retry Stop"
            ctrl.gateway.mark_down(a, f"{rec.get('title') or rec['kit']} {reason}", retry_after=30)
