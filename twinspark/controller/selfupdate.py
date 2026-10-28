"""`tsm update` for the Sparks: install a TwinSpark release on every node, the controller's node last.

The model keeps running throughout. Restarting the agents, privd and the controller does not touch the
inference containers: a restarted controller re-attaches the running deployment (``_adopt_running``).
Requests in flight at the moment node A's gateway restarts are cut, so before that step the run waits
(up to ``runtime.drain_timeout_s``) for the gateway to be idle.

Order and checkpoints, saved in the state database under ``selfupdate``:

1. each other node (B), then the controller's node (A): ``update_start`` through its agent and privd,
   then poll ``update_status`` until it is completed, rolled back or failed
2. a completed node must report the new version through its agent before the next node starts
3. node A's update restarts this controller. The new process finds the run in the database, polls on
   and finishes it (`on_startup` → `ensure_running`)

A failed run holds nothing: the model was never stopped. A rolled-back node runs its previous version
again. While a run is in progress, model switches and other management writes are refused (409).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

from .. import __version__, releases

log = logging.getLogger("twinspark.selfupdate")

KEY = "selfupdate"
NODE_TIMEOUT_S = 2400          # one node: download, backup, pip install, restarts
CHECK_TTL_S = 600


class SelfUpdate:
    def __init__(self, ctrl: Any) -> None:
        self.ctrl = ctrl
        self.task: Optional[asyncio.Task] = None
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._start_lock = asyncio.Lock()

    # ---- state ------------------------------------------------------------------------------
    def state(self) -> dict:
        return self.ctrl.store.kv_get(KEY) or {"state": "idle"}

    def blocking(self) -> bool:
        return self.state().get("state") == "running"

    def save(self, record: dict, **changes) -> None:
        record.update(**changes, updated_at=time.time())
        self.ctrl.store.kv_set(KEY, record)

    @property
    def own_node(self) -> str:
        return self.ctrl.config.node.node_id

    def order(self) -> list[str]:
        return sorted(self.ctrl.agents, key=lambda n: (n == self.own_node, n))

    # ---- what is published --------------------------------------------------------------------
    async def fetch(self, version: Optional[str], allow_prerelease: bool) -> dict:
        """The release from GitHub (tests replace this)."""
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            return await releases.latest_async(client, version=version, allow_prerelease=allow_prerelease)

    async def release(self, version: Optional[str] = None, allow_prerelease: bool = False,
                      refresh: bool = False) -> dict:
        key = (version, allow_prerelease)
        hit = self._cache.get(key)
        if hit and not refresh and time.time() - hit[0] < CHECK_TTL_S:
            if isinstance(hit[1], Exception):
                raise hit[1]
            return hit[1]
        try:
            out = await self.fetch(version, allow_prerelease)
        except releases.ReleaseError as exc:
            self._cache[key] = (time.time(), exc)
            raise
        except Exception as exc:  # noqa: BLE001 - offline, DNS, TLS: one readable line
            err = releases.ReleaseError(f"GitHub is not reachable from node {self.own_node}: "
                                        f"{type(exc).__name__}: {str(exc)[:200]}")
            self._cache[key] = (time.time(), err)
            raise err from exc
        self._cache[key] = (time.time(), out)
        return out

    async def check(self, refresh: bool = False, allow_prerelease: bool = False) -> dict:
        async def node(name, agent):
            try:
                return name, await agent.call("update_probe", timeout=60)
            except Exception as exc:  # noqa: BLE001 - shown per node
                msg = str(exc)
                if "unknown action" in msg or "unknown privileged op" in msg:
                    msg = ("this node runs a TwinSpark older than 0.8.0, which cannot update itself — update it "
                           "once by hand: git pull && sudo ./install.sh --no-setup")
                return name, {"enabled": False, "error": msg[:500]}

        nodes = dict(await asyncio.gather(*(node(n, a) for n, a in self.ctrl.agents.items())))
        target, error = None, None
        try:
            target = await self.release(allow_prerelease=allow_prerelease, refresh=refresh)
        except releases.ReleaseError as exc:
            error = str(exc)
        problems = []
        for n, p in sorted(nodes.items()):
            if p.get("error"):
                problems.append(f"node {n}: {p['error']}")
            elif p.get("install_error"):
                problems.append(f"node {n}: {p['install_error']}")
            elif not p.get("enabled"):
                problems.append(f"node {n}: updates are switched off by /etc/twinspark/update-policy.json"
                                + (" (missing — run sudo ./install.sh --no-setup once)" if p.get("missing") else ""))
            elif p.get("active_run") and p["active_run"] != self.state().get("run_id"):
                problems.append(f"node {n}: another update is still running there")
        versions = {"controller": __version__, **{n: p.get("version") for n, p in nodes.items()}}
        latest = target["version"] if target else None
        behind = sorted(k for k, v in versions.items() if latest and (not v or releases.newer(latest, v)))
        return {"versions": versions, "nodes": nodes, "release": releases.summary(__version__, target),
                "release_error": error, "behind": behind, "available": bool(behind),
                "ready": not problems and target is not None and not self.blocking(),
                "problems": problems, "order": self.order(),
                "dry_run": bool(nodes) and all(p.get("dry_run") for p in nodes.values()),
                "state": self.state(), "at": time.time(),
                "note": ("Updates node B first, then node A. The model keeps serving; node A's gateway restarts "
                         "once, after waiting for requests in flight to finish.")}

    # ---- start ------------------------------------------------------------------------------
    async def start(self, version: Optional[str] = None, allow_prerelease: bool = False) -> dict:
        async with self._start_lock:
            if self.blocking():
                raise ValueError("a TwinSpark update is already running")
            if self.ctrl._lock.locked() or self.ctrl.maintenance.blocking():
                raise ValueError("a model switch or a maintenance run is in progress; try again when it is done")
            info = await self.check(allow_prerelease=allow_prerelease)
            if info["problems"]:
                raise ValueError("; ".join(info["problems"]))
            target = await self.release(version, allow_prerelease)
            if target.get("prerelease") and not allow_prerelease and not version:
                raise ValueError(f"{target['tag']} is a pre-release")
            if not version and not any(v != target["version"] for v in info["versions"].values()):
                raise ValueError(f"every node already runs {target['version']}")
            # no await between the checks above and this save: it reserves the cluster
            if self.ctrl._lock.locked() or self.ctrl.maintenance.blocking() or self.blocking():
                raise ValueError("the cluster became busy; try again")
            state = {
                "run_id": uuid.uuid4().hex, "state": "running", "phase": "starting",
                "target": {k: target[k] for k in ("tag", "version", "wheel", "constraints", "prerelease")},
                "notes": target.get("notes", "")[:4000], "from": info["versions"], "order": self.order(),
                "index": 0, "nodes": {}, "dispatched": [], "started_at": time.time(),
                "deadline": time.time() + NODE_TIMEOUT_S, "dry_run": info["dry_run"],
            }
            self.save(state)
        self.ctrl._audit("user", "update.start", "cluster", {"run_id": state["run_id"], "to": target["version"],
                                                             "from": info["versions"]})
        self.ensure_running()
        return state

    def ensure_running(self) -> None:
        if self.blocking() and (self.task is None or self.task.done()):
            self.task = self.ctrl._spawn(self.run())

    # ---- the run ------------------------------------------------------------------------------
    async def run(self) -> None:
        while self.blocking():
            state = self.state()
            try:
                await self.tick(state)
            except asyncio.CancelledError:
                raise                       # node A's update restarts this process: the checkpoint stays
            except Exception as exc:  # noqa: BLE001
                self.save(state, state="failed", error=str(exc)[:2000], waiting=None)
                self.ctrl._audit("controller", "update.failed", "cluster", {"error": str(exc)[:1000]})
                log.warning("TwinSpark update failed: %s", exc)
                break
            await asyncio.sleep(max(0.01, min(2.0, self.ctrl.poll_interval)))

    async def _wait_idle(self, state: dict) -> None:
        """Before node A restarts its gateway: let requests in flight finish (bounded, nothing is refused)."""
        limit = float(self.ctrl.config.runtime.drain_timeout_s)
        aliases = list(self.ctrl.gateway.routes)
        self.save(state, phase=f"waiting for requests in flight to finish (up to {int(limit)} s)")
        deadline = time.monotonic() + limit
        busy = self.ctrl.gateway.inflight(aliases)[0] if aliases else 0
        while busy and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            busy = self.ctrl.gateway.inflight(aliases)[0]
        if busy:
            state["cut_requests"] = busy
            self.ctrl._audit("controller", "update.restart_busy", "gateway", {"in_flight": busy})

    async def tick(self, state: dict) -> None:
        c, rid, target = self.ctrl, state["run_id"], state["target"]
        if time.time() > state["deadline"]:
            node = state["order"][min(state["index"], len(state["order"]) - 1)]
            raise RuntimeError(f"node {node} did not finish its update in time; check it "
                               f"(journalctl -u 'twinspark-update-*') before starting another")
        if state["index"] >= len(state["order"]):
            await self._finish(state)
            return
        name = state["order"][state["index"]]
        agent = c.agents.get(name)
        if agent is None:
            raise RuntimeError(f"node {name} is no longer configured")
        try:
            r = await agent.call("update_status", run_id=rid, timeout=30)
        except Exception as exc:  # noqa: BLE001 - a restarting agent (or, on node A, privd) is expected
            self.save(state, waiting=f"waiting for node {name}: {str(exc)[:200]}")
            return
        state["nodes"][name] = {k: r.get(k) for k in ("state", "phase", "error", "from_version", "dry_run",
                                                      "worker_active", "updated_at")}
        if r["state"] == "missing":
            if name in state["dispatched"]:
                raise RuntimeError(f"node {name} lost its update record; check it before starting another")
            if name == self.own_node and not state.get("idle_checked") and not state.get("dry_run"):
                await self._wait_idle(state)
                state["idle_checked"] = True
            state["dispatched"].append(name)
            self.save(state, phase=f"node {name}: starting", waiting=None, deadline=time.time() + NODE_TIMEOUT_S)
            await agent.call("update_start", run_id=rid, target=target, timeout=60)
            return
        if r["state"] in ("queued", "running"):
            self.save(state, phase=f"node {name}: {r.get('phase', r['state'])}", waiting=None)
            return
        if r["state"] == "rolled_back":
            raise RuntimeError(f"node {name}: the update failed and was rolled back to "
                               f"{r.get('from_version') or 'its previous version'}: {r.get('error', '')}")
        if r["state"] == "failed":
            raise RuntimeError(f"node {name}: {r.get('error') or 'the update failed'}")
        if r["state"] == "completed":
            if not r.get("dry_run"):
                try:
                    facts = await agent.call("hardware_facts", timeout=30)
                except Exception as exc:  # noqa: BLE001
                    self.save(state, waiting=f"node {name} updated; waiting for its agent: {str(exc)[:200]}")
                    return
                if facts.get("agent_version") != target["version"]:
                    self.save(state, waiting=f"node {name} updated; its agent still reports "
                                             f"{facts.get('agent_version')}")
                    return
            self.save(state, index=state["index"] + 1, waiting=None, phase=f"node {name}: done")
            return
        raise RuntimeError(f"node {name} reports an unknown update state {r['state']!r}")

    async def _finish(self, state: dict) -> None:
        target = state["target"]["version"]
        if not state.get("dry_run") and __version__ != target:
            # the controller's node completed, but this process is still the old program
            if self.own_node in self.ctrl.agents:
                raise RuntimeError(f"node {self.own_node} was updated, but this controller still runs "
                                   f"{__version__}; restart it (sudo systemctl restart twinspark-controller)")
        try:
            await self.ctrl.refresh_hardware()
        except Exception:  # noqa: BLE001
            log.debug("refresh after update failed", exc_info=True)
        self.save(state, state="completed", phase="done", finished_at=time.time())
        self.ctrl._audit("controller", "update.completed", "cluster", {"run_id": state["run_id"], "version": target})
