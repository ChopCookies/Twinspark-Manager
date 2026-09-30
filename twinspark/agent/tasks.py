"""Background task registry for long agent operations (download, sync, tests).

Tasks live in the agent process; the controller polls ``task_status``. Work that
can run for hours (downloads, rsync) runs in child processes so a cancel really
stops it and a crash cannot take the agent down with it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable, Optional


class TaskRegistry:
    def __init__(self, keep: int = 200):
        self.tasks: dict[str, dict[str, Any]] = {}
        self._refs: dict[str, asyncio.Task] = {}
        self._procs: dict[str, list[asyncio.subprocess.Process]] = {}
        self.keep = keep

    def spawn(self, kind: str, fn: Callable[..., Awaitable[Any]], *args, **meta) -> dict:
        tid = f"{kind}-{uuid.uuid4().hex[:10]}"
        self.tasks[tid] = {"task_id": tid, "kind": kind, "state": "running",
                           "started": time.time(), "finished": None, "error": None,
                           "detail": "", "phase": None, "done_bytes": None,
                           "total_bytes": None, "rate_bps": None, "eta_s": None,
                           "result": None, **meta}

        async def runner():
            try:
                res = await fn(tid, *args)
                self.tasks[tid].update(state="completed", result=res)
            except asyncio.CancelledError:
                self.tasks[tid].update(state="cancelled", error="cancelled")
            except Exception as exc:  # noqa: BLE001
                self.tasks[tid].update(state="failed", error=str(exc)[:4000])
            finally:
                self.tasks[tid]["finished"] = time.time()
                self._refs.pop(tid, None)
                self._procs.pop(tid, None)
                self._trim()

        self._refs[tid] = asyncio.create_task(runner())
        return {"task_id": tid}

    def update(self, tid: str, **values) -> None:
        if tid in self.tasks:
            self.tasks[tid].update(values)

    def progress(self, tid: str, done: int, total: int, started: float, phase: str,
                 label: str = "") -> None:
        elapsed = max(1e-3, time.time() - started)
        rate = done / elapsed if done else 0.0
        eta = (total - done) / rate if rate > 0 and total >= done else None
        pct = (100.0 * done / total) if total else 0.0
        detail = f"{label or phase}: {done / 1024**3:.1f} / {total / 1024**3:.1f} GiB ({pct:.0f}%)"
        if rate > 0:
            detail += f" · {rate / 1024**2:.0f} MiB/s"
        if eta is not None and eta > 1:
            detail += f" · ETA {int(eta // 60)}m{int(eta % 60):02d}s"
        self.update(tid, phase=phase, done_bytes=done, total_bytes=total, rate_bps=rate,
                    eta_s=eta, detail=detail)

    def register_proc(self, tid: str, proc: asyncio.subprocess.Process) -> None:
        self._procs.setdefault(tid, []).append(proc)

    def status(self, tid: str) -> Optional[dict[str, Any]]:
        t = self.tasks.get(tid)
        return None if t is None else {k: v for k, v in t.items() if not k.startswith("_")}

    def cancel(self, tid: str) -> bool:
        task = self._refs.get(tid)
        for p in self._procs.get(tid, []):
            if p.returncode is None:
                try:
                    p.terminate()
                except ProcessLookupError:
                    pass
        if task and not task.done():
            task.cancel()
            return True
        return False

    def running(self, kind: Optional[str] = None) -> list[dict[str, Any]]:
        return [self.status(t) for t, v in self.tasks.items()
                if v["state"] == "running" and (kind is None or v["kind"] == kind)]

    def _trim(self) -> None:
        done = [t for t, v in self.tasks.items() if v["state"] != "running"]
        for t in done[:-self.keep] if len(done) > self.keep else []:
            self.tasks.pop(t, None)
