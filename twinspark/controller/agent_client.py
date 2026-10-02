"""Controller-side client for tsm-agent (typed actions only, spec §4.2)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

import httpx

ALLOWED_AGENT_ACTIONS = {
    "system_telemetry", "maintenance_probe", "maintenance_quiet", "maintenance_start",
    "maintenance_status", "maintenance_reboot", "maintenance_verify",
    "hardware_facts", "memory_telemetry", "preflight", "image_ensure", "image_inspect",
    "container_start", "containers_stop_owned", "container_state", "container_logs",
    "containers_list", "health_probe", "foreign_list", "foreign_stop",
    "weights_present", "weights_inventory", "weights_delete", "download", "verify", "sync",
    "ssh_check", "task_status", "task_cancel", "tasks_list",
    "link_test", "rdma_facts", "reclaim_memory", "headless_status", "headless_apply",
    "mods_list", "mods_status", "mods_install", "mods_remove",
    "remote_status", "remote_logs", "remote_bundle", "remote_power", "remote_power_cancel",
    "remote_boot_status", "remote_boot_next", "remote_boot_next_clear", "remote_wol_status", "remote_wol_set",
}


class AgentActionError(RuntimeError):
    def __init__(self, action: str, node: str, detail: str, excerpt: Optional[str] = None,
                 status: int = 0):
        super().__init__(f"agent {node} action '{action}' failed: {detail}")
        self.action, self.node, self.excerpt, self.status = action, node, excerpt, status
        self.detail = detail


class AgentClient:
    def __init__(self, node: str, base_url: str, token: str,
                 client: Optional[httpx.AsyncClient] = None, verify: bool | str = True):
        self.node = node
        self.base_url = base_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {token}"}
        # one pooled client per agent instead of one per call
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, verify=verify, timeout=httpx.Timeout(60, connect=5))

    async def call(self, action: str, /, timeout: float = 60, **params: Any) -> Any:
        if action not in ALLOWED_AGENT_ACTIONS:
            raise AgentActionError(action, self.node, "action not allowlisted")
        try:
            resp = await self._client.post("/v1/action", json={"action": action, "params": params},
                                           headers=self._headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise AgentActionError(action, self.node,
                                   f"agent unreachable ({type(exc).__name__})") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or not data.get("ok"):
            raise AgentActionError(action, self.node,
                                   data.get("error") or data.get("detail") or resp.text[:300],
                                   (data.get("log_excerpt") or "")[:4000], resp.status_code)
        return data.get("result")

    async def wait_task(self, task_id: str, timeout: float,
                        on_progress: Optional[Callable[[dict], None]] = None,
                        poll: float = 2.0, cancel_check: Optional[Callable[[], bool]] = None) -> dict:
        """Poll a background task until it finishes. Raises on failure/timeout."""
        deadline = time.monotonic() + timeout
        while True:
            t = await self.call("task_status", task_id=task_id)
            if on_progress:
                on_progress(t)
            if t["state"] == "completed":
                return t
            if t["state"] in ("failed", "cancelled"):
                raise AgentActionError(t.get("kind", "task"), self.node,
                                       t.get("error") or t["state"])
            if cancel_check and cancel_check():
                await self.call("task_cancel", task_id=task_id)
                raise AgentActionError(t.get("kind", "task"), self.node, "cancelled by user")
            if time.monotonic() > deadline:
                await self.call("task_cancel", task_id=task_id)
                raise AgentActionError(t.get("kind", "task"), self.node, "timed out")
            await asyncio.sleep(poll)

    async def aclose(self) -> None:
        await self._client.aclose()
