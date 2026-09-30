"""Persisted two-node maintenance coordinator. No implicit schedule or firmware opt-in."""

from __future__ import annotations

import asyncio
import time
import uuid


class Maintenance:
    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.task = None
        self.control_lock = asyncio.Lock()

    def state(self):
        return self.ctrl.store.kv_get("maintenance") or {"state": "idle"}

    def blocking(self):
        return self.state()["state"] in ("running", "failed")

    def save(self, record, **changes):
        record.update(**changes, updated_at=time.time())
        self.ctrl.store.kv_set("maintenance", record)

    async def plan(self):
        async def node(name, agent):
            try:
                return name, await agent.call("maintenance_probe", timeout=90)
            except Exception as exc:
                return name, {"enabled": False, "error": str(exc)}

        nodes = dict(await asyncio.gather(*(node(n, a) for n, a in self.ctrl.agents.items())))
        return {
            "nodes": nodes,
            "order": sorted(nodes, reverse=True),
            "active": self.ctrl.active(),
            "ready": bool(nodes) and all(n.get("enabled") and not n.get("error") for n in nodes.values()),
            "at": time.time(),
            "note": (
                "Serving stops during maintenance. B updates before A; the exact active revision "
                "is restored after both nodes pass checks."
            ),
        }

    async def start(self, firmware=False):
        plan = await self.plan()
        if self.ctrl.busy() or self.ctrl._staging:
            raise ValueError("cluster is busy or held for maintenance")
        if not plan["ready"]:
            raise ValueError("every configured node must be reachable and opted in through its root policy")
        if firmware and not all(n.get("allow_firmware") for n in plan["nodes"].values()):
            raise ValueError("firmware updates are not enabled on every node")
        # No await between the busy check and persistence: this reserves the cluster.
        state = {
            "run_id": uuid.uuid4().hex,
            "state": "running",
            "phase": "drain",
            "firmware": firmware,
            "previous": self.ctrl.active(),
            "order": plan["order"],
            "index": 0,
            "nodes": {},
            "started_at": time.time(),
            "deadline": time.time() + 10800,
            "dry_run": all(n.get("dry_run") for n in plan["nodes"].values()),
        }
        self.save(state)
        self.ctrl._audit("user", "maintenance.start", "cluster", {"run_id": state["run_id"], "firmware": firmware})
        self.ensure_running()
        return state

    def ensure_running(self):
        if self.state()["state"] == "running" and (self.task is None or self.task.done()):
            self.task = self.ctrl._spawn(self.run())

    async def resume(self):
        async with self.control_lock:
            return self._resume()

    def _resume(self):
        state = self.state()
        if state["state"] != "failed":
            raise ValueError("only a held maintenance run can be rechecked")
        self.save(state, state="running", error=None, deadline=time.time() + 10800)
        self.ensure_running()
        return state

    async def release(self):
        async with self.control_lock:
            return await self._release()

    async def _release(self):
        state = self.state()
        if state["state"] != "failed":
            raise ValueError("only a held maintenance run can be released")
        if self.ctrl._lock.locked():
            raise ValueError("wait for the model activation to finish before releasing maintenance")
        if set(state["order"]) != set(self.ctrl.agents):
            raise ValueError("restore the original node configuration before releasing maintenance")
        for name, agent in self.ctrl.agents.items():
            result = await agent.call("maintenance_status", run_id=state["run_id"], timeout=90)
            if result["state"] not in ("missing", "failed", "completed") or result.get("worker_active"):
                raise ValueError(f"node {name} is still updating or awaiting reboot")
            await agent.call(
                "maintenance_verify", run_id=state["run_id"], rdma_hcas=self.ctrl.config.nodes[name].rdma_hcas
            )
        self.save(state, state="released")
        self.ctrl._audit("user", "maintenance.release", "cluster", {"run_id": state["run_id"]})
        return state

    async def run(self):
        while self.state()["state"] == "running":
            state = self.state()
            try:
                await self.tick(state)
            except asyncio.CancelledError:
                raise  # Restart/shutdown retains the last durable checkpoint.
            except Exception as exc:
                self.save(state, state="failed", error=str(exc)[:2000])
                self.ctrl._audit("controller", "maintenance.failed", "cluster", {"error": str(exc)[:1000]})
                break
            await asyncio.sleep(max(0.01, self.ctrl.poll_interval))

    async def tick(self, state):
        c = self.ctrl
        if time.time() > state["deadline"]:
            raise RuntimeError("maintenance timed out; inspect the node before rechecking")
        phase = state["phase"]
        rid = state["run_id"]
        if phase == "drain":
            await c.stop(_maintenance=True)
            # stop() is best effort; explicit verification must succeed on BOTH nodes.
            for agent in c.agents.values():
                await agent.call("maintenance_quiet", timeout=45)
            self.save(state, phase="update")
            return
        if state["index"] < len(state["order"]):
            name = state["order"][state["index"]]
            agent = c.agents[name]
            try:
                result = await agent.call("maintenance_status", run_id=rid, timeout=90)
            except Exception as exc:
                # Reconnection is expected while rebooting; never advance without proof.
                if phase == "reboot":
                    self.save(state, waiting=f"Waiting for node {name}: {str(exc)[:200]}")
                    return
                raise
            state["nodes"][name] = result
            self.save(state, waiting=None)
            if result["state"] == "failed":
                raise RuntimeError(f"node {name}: {result.get('error', 'update failed')}")
            if phase == "update":
                if result["state"] == "missing":
                    dispatched = state.setdefault("dispatched", [])
                    if name in dispatched:
                        raise RuntimeError(
                            f"node {name} lost or never saved its update record; review before a new run"
                        )
                    dispatched.append(name)
                    self.save(state)
                    # Both the run ID and dispatch intent are persisted before sending.
                    await agent.call("maintenance_start", run_id=rid, firmware=state["firmware"], timeout=90)
                elif result["state"] == "awaiting_reboot":
                    self.save(state, phase="reboot", deadline=time.time() + 1800)
                elif result["state"] in ("rebooting", "completed"):
                    self.save(state, phase="reboot", deadline=time.time() + 1800)
            elif phase == "reboot":
                if result["state"] == "awaiting_reboot":
                    try:
                        await agent.call("maintenance_reboot", run_id=rid, timeout=30)
                    except Exception:
                        # The host may close the connection before acknowledging reboot.
                        # Poll durable state next; never infer completion from disconnect.
                        pass
                elif result["state"] == "completed":
                    if not result.get("new_boot_id") or result.get("new_boot_id") == result.get("boot_id"):
                        raise RuntimeError(f"node {name} did not prove a new boot")
                    await agent.call(
                        "maintenance_verify", run_id=rid, rdma_hcas=c.config.nodes[name].rdma_hcas, timeout=90
                    )
                    self.save(state, index=state["index"] + 1, phase="update", deadline=time.time() + 10800)
                elif result["state"] == "missing":
                    raise RuntimeError(f"node {name} lost its maintenance record; manual review required")
            return
        previous = state.get("previous")
        if not previous:
            self.save(state, state="completed", phase="done")
            return
        if phase != "restoring":
            self.save(state, phase="restoring", restore_job=None)
            job = await c.activate(previous["profile"], previous["revision_id"], _maintenance=True)
            self.save(state, restore_job=job.job_id)
            return
        job = c.store.load_job(state["restore_job"]) if state.get("restore_job") else None
        if job and job.state.value in ("pending", "running"):
            return
        if await c._adopt_running(previous):
            c.store.kv_set("active", previous)
            self.save(state, state="completed", phase="done")
            c._audit("controller", "maintenance.completed", "cluster", {"run_id": rid})
        else:
            raise RuntimeError("nodes updated, but restoring the model failed; inspect its activation job")
