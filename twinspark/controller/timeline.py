"""Startup memory timeline: what each node's memory did while an activation replaced the model.

Each agent samples its own node (``agent/timeline.py``) once a second from the moment the old model
is about to be stopped until the new one serves or the start fails: MemAvailable, MemFree, swap use
and swap traffic, memory pressure (PSI), the TwinSpark units' and the deployment's containers' cgroup
memory. The controller marks every stage transition, so a curve can be read against "stopping",
"loading", "testing", ...

On a GB10, ``nvidia-smi`` reports no memory figures, so what the NVIDIA driver itself holds is not
sampled ("driver_allocated": unknown); its allocations show up only as falling MemAvailable.

Everything here is best effort: an agent that is down, slow or too old to know these actions turns
the timeline off for that node, never the activation. Stored copies are bounded: at most
:data:`EVIDENCE_POINTS` points per node for a failed start, :data:`SUCCESS_POINTS` for a successful one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable, Optional

log = logging.getLogger("twinspark.controller")

EVIDENCE_POINTS = 600               # per node, kept in a failed job's startup evidence
SUCCESS_POINTS = 120                # per node, kept in a successful job (job lists carry it)
INTERVAL_S = 1.0
MAX_SAMPLES = 3600                  # one hour at one sample per second; the agent stops by itself after that
CALL_TIMEOUT_S = 4.0                # start / mark / stop
READ_TIMEOUT_S = 15.0
STAGES = ("stopping", "reclaiming", "starting-cluster", "loading", "testing", "routing", "healthy")
# columns of a stored series: name in the series -> key in a sample
SERIES = {"mem_available_gib": "mem_available_gib", "mem_free_gib": "mem_free_gib",
          "swap_used_gib": "swap_used_gib", "swap_in_pps": "swap_in_pps", "swap_out_pps": "swap_out_pps",
          "psi_some_avg10": "psi_some_avg10", "psi_full_avg10": "psi_full_avg10"}
EVIDENCE_SERIES = (*SERIES, "containers_mib")
SUCCESS_SERIES = ("mem_available_gib", "mem_free_gib")


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return float(v)


# ---- pure helpers (the agent uses them too) ---------------------------------------------------------
def downsample(samples: list[dict], max_points: int, marks: Iterable[dict] = ()) -> list[dict]:
    """At most ``max_points`` samples: the first and last, the lowest MemAvailable, the sample at every
    stage mark, and evenly spread ones in between — so the minimum and the stage edges survive."""
    n = len(samples)
    max_points = max(2, int(max_points))
    if n <= max_points:
        return list(samples)
    keep = {0, n - 1}
    avail = [(i, _num(s.get("mem_available_gib"))) for i, s in enumerate(samples)]
    avail = [(i, v) for i, v in avail if v is not None]
    if avail:
        keep.add(min(avail, key=lambda x: x[1])[0])
    for m in marks:
        idx = m.get("index")
        if isinstance(idx, int) and 0 <= idx < n:
            keep.add(idx)
    keep = set(sorted(keep)[:max_points])
    room = max_points - len(keep)
    if room > 0:
        step = n / (room + 1)
        for k in range(1, room + 1):
            keep.add(min(n - 1, int(k * step)))
    return [samples[i] for i in sorted(keep)][:max_points]


def compact(samples: list[dict], columns: Iterable[str] = EVIDENCE_SERIES) -> dict[str, list]:
    """Columnar copy for storage: ``{"t": [...], "mem_available_gib": [...], ...}``; unknown stays null."""
    out: dict[str, list] = {"t": [round(_num(s.get("t")) or 0.0, 1) for s in samples]}
    for col in columns:
        if col == "containers_mib":
            out[col] = [_containers_mib(s) for s in samples]
        else:
            key = SERIES.get(col, col)
            out[col] = [None if _num(s.get(key)) is None else round(float(s[key]), 2) for s in samples]
    return out


def _containers_mib(sample: dict) -> Optional[float]:
    cs = sample.get("containers") if isinstance(sample.get("containers"), dict) else {}
    vals = [_num((c or {}).get("current_mib")) for c in cs.values() if isinstance(c, dict)]
    vals = [v for v in vals if v is not None]
    return round(sum(vals), 1) if vals else None


def summarize(samples: list[dict], marks: list[dict]) -> dict[str, Any]:
    """Per-node summary of a timeline: the lowest MemAvailable and when, the startup footprint, swap
    and pressure peaks, OOM events. Missing readings stay None, never 0."""
    pts = [s for s in samples if isinstance(s, dict)]
    out: dict[str, Any] = {"samples": len(pts), "driver_allocated": "unknown",
                           "simulated": any(bool(s.get("simulated")) for s in pts),
                           "marks": [{"stage": m.get("stage"), "t": m.get("t")} for m in marks if isinstance(m, dict)]}
    if not pts:
        return out
    t0, t1 = _num(pts[0].get("t")) or 0.0, _num(pts[-1].get("t")) or 0.0
    out["duration_s"] = round(t1 - t0, 1)
    out["mem_total_gib"] = next((_num(s.get("mem_total_gib")) for s in pts if _num(s.get("mem_total_gib"))), None)
    avail = [(_num(s.get("t")) or 0.0, _num(s.get("mem_available_gib")), s) for s in pts]
    avail = [(t, v, s) for t, v, s in avail if v is not None]
    if avail:
        t_min, v_min, s_min = min(avail, key=lambda x: x[1])
        out.update(min_mem_available_gib=round(v_min, 2), min_at_s=round(t_min, 1), min_at=s_min.get("at"),
                   min_stage=s_min.get("stage"), start_mem_available_gib=round(avail[0][1], 2))
        start = next((m for m in marks if isinstance(m, dict) and m.get("stage") == "starting-cluster"), None)
        if start is not None and _num(start.get("t")) is not None:
            ts = float(start["t"])
            before = [a for a in avail if a[0] <= ts + 1e-6]
            after = [a for a in avail if a[0] >= ts - 1e-6]
            base = before[-1] if before else (after[0] if after else None)
            if base is not None and after:
                low = min(after, key=lambda x: x[1])
                # what this deployment took at the startup's lowest point, relative to just before it started
                out.update(baseline_available_gib=round(base[1], 2),
                           footprint_gib=round(max(0.0, base[1] - low[1]), 2),
                           startup_min_available_gib=round(low[1], 2), startup_min_stage=low[2].get("stage"),
                           startup_min_at_s=round(low[0], 1))
                # serving, not starting: what the deployment holds once it answers (the healthy mark)
                ready = next((m for m in marks if isinstance(m, dict) and m.get("stage") == "healthy"
                              and _num(m.get("t")) is not None), None)
                if ready is not None:
                    at_ready = [a for a in avail if a[0] >= float(ready["t"]) - 1e-6]
                    if at_ready:
                        out["steady_footprint_gib"] = round(max(0.0, base[1] - at_ready[0][1]), 2)
    free = [_num(s.get("mem_free_gib")) for s in pts]
    free = [v for v in free if v is not None]
    if free:
        out["min_mem_free_gib"] = round(min(free), 2)
    for key, name in (("swap_in_pps", "peak_swap_in_pps"), ("swap_out_pps", "peak_swap_out_pps"),
                      ("swap_used_gib", "max_swap_used_gib"), ("psi_some_avg10", "max_psi_some_avg10"),
                      ("psi_full_avg10", "max_psi_full_avg10")):
        vals = [_num(s.get(key)) for s in pts]
        vals = [v for v in vals if v is not None]
        out[name] = round(max(vals), 2) if vals else None
    sw = [_num(s.get("swap_used_gib")) for s in pts]
    sw = [v for v in sw if v is not None]
    out["start_swap_used_gib"] = round(sw[0], 2) if sw else None
    # containers: the largest reading of each, its limit and any OOM event the cgroup counted
    containers: dict[str, dict[str, Any]] = {}
    for s in pts:
        for name, c in (s.get("containers") or {}).items() if isinstance(s.get("containers"), dict) else ():
            if not isinstance(c, dict):
                continue
            rec = containers.setdefault(str(name), {"peak_mib": None, "limit_mib": None, "oom": None,
                                                    "oom_kill": None})
            for key, src in (("peak_mib", "peak_mib"), ("peak_mib", "current_mib"), ("oom", "oom"),
                             ("oom_kill", "oom_kills")):
                v = _num(c.get(src))
                if v is not None:
                    rec[key] = v if rec[key] is None else max(rec[key], v)
            if _num(c.get("limit_mib")) is not None:
                rec["limit_mib"] = float(c["limit_mib"])
    out["containers"] = containers
    out["oom_events"] = [{"container": n, "oom": c["oom"], "oom_kill": c["oom_kill"], "limit_mib": c["limit_mib"]}
                         for n, c in containers.items() if (c["oom"] or 0) > 0 or (c["oom_kill"] or 0) > 0]
    return out


def observation(summaries: dict[str, dict], job_id: str) -> dict[str, Any]:
    """What a successful start leaves for the planner: per node, the startup footprint and its lowest point."""
    keep = ("footprint_gib", "baseline_available_gib", "startup_min_available_gib", "min_mem_available_gib",
            "min_stage", "startup_min_stage", "mem_total_gib", "max_psi_full_avg10", "peak_swap_out_pps",
            "steady_footprint_gib", "simulated")
    return {"job_id": job_id, "at": time.time(),
            "nodes": {n: {k: s.get(k) for k in keep} for n, s in summaries.items() if isinstance(s, dict)}}


# ---- talking to the agents -----------------------------------------------------------------------------
async def _call(agents: dict, node: str, action: str, timeout: float, **params: Any) -> tuple[Any, Optional[str]]:
    """(result, None) or (None, why). Never raises, except when the activation itself is cancelled."""
    client = agents.get(node)
    if client is None:
        return None, "node is not configured"
    try:
        return await asyncio.wait_for(client.call(action, timeout=timeout, **params), timeout + 1), None
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return None, f"{action} took longer than {timeout:.0f} s"
    except Exception as exc:  # noqa: BLE001 - an agent problem never breaks the activation
        detail = getattr(exc, "detail", None) or str(exc)
        if "unknown action" in detail:
            detail = "this agent does not record startup timelines (update it to 0.6)"
        return None, f"{action}: {detail}"[:300]


async def collect(agents: dict, job_id: str, nodes: Iterable[str], max_points: int,
                  stop: bool = False) -> dict[str, dict[str, Any]]:
    """Each node's timeline for ``job_id``: summary over every sample, a downsampled series and the marks.
    A node that cannot answer gets ``{"error": ...}``."""
    async def one(node: str) -> tuple[str, dict]:
        if stop:
            await _call(agents, node, "timeline_stop", CALL_TIMEOUT_S, job_id=job_id)
        res, err = await _call(agents, node, "timeline_read", READ_TIMEOUT_S, job_id=job_id, since_index=0,
                               max_points=max_points)
        if err or not isinstance(res, dict):
            return node, {"error": err or "no timeline"}
        samples = [s for s in res.get("samples") or [] if isinstance(s, dict)]
        marks = [m for m in res.get("marks") or [] if isinstance(m, dict)][:64]
        samples = downsample(samples, max_points, marks)
        summary = res.get("summary") if isinstance(res.get("summary"), dict) else summarize(samples, marks)
        return node, {"summary": summary, "samples": samples, "marks": marks,
                      "total_samples": res.get("total_samples", len(samples)), "running": res.get("running"),
                      "stopped_reason": res.get("stopped_reason"), "interval_s": res.get("interval_s"),
                      "simulated": bool(res.get("simulated")), "notes": res.get("notes") or []}
    pairs = await asyncio.gather(*(one(n) for n in dict.fromkeys(nodes)))
    return dict(pairs)


def stored(node_data: dict[str, dict], columns: Iterable[str] = EVIDENCE_SERIES) -> dict[str, dict]:
    """The bounded copy that goes into a job: series in columns, marks, summary."""
    out = {}
    for node, d in node_data.items():
        if d.get("error"):
            out[node] = {"error": d["error"]}
            continue
        # the stage marks (with their offsets) travel in the summary
        out[node] = {"summary": d["summary"], "series": compact(d["samples"], columns),
                     "points": len(d["samples"]), "total_samples": d.get("total_samples"),
                     "interval_s": d.get("interval_s"), "simulated": d.get("simulated"),
                     "stopped_reason": d.get("stopped_reason")}
    return out


class StartupTimeline:
    """The controller's side of one activation's timeline: start, mark each stage, stop, collect.

    A node whose agent fails a call is left out for the rest of the activation (one slow agent would
    otherwise add a timeout to every stage); the reason is kept in the job.
    """

    def __init__(self, agents: dict, job, nodes: Iterable[str], containers: Optional[dict[str, list[str]]] = None,
                 interval_s: float = INTERVAL_S, max_samples: int = MAX_SAMPLES):
        self.agents, self.job = agents, job
        self.nodes = list(dict.fromkeys(nodes))
        self.containers = containers or {}
        self.interval_s, self.max_samples = interval_s, max_samples
        self.off: dict[str, str] = {}
        self.started = False
        self.stopped = False

    @property
    def record(self) -> dict:
        return self.job.payload.setdefault("startup_timeline", {"nodes": self.nodes, "marks": [], "errors": {}})

    def _note(self, node: str, why: Optional[str]) -> None:
        if why:
            self.off[node] = why
            self.record.setdefault("errors", {})[node] = why
            log.info("startup timeline off on node %s: %s", node, why)

    async def _each(self, action: str, **params: Any) -> None:
        nodes = [n for n in self.nodes if n not in self.off]

        async def one(n: str) -> None:
            extra = {"containers": self.containers.get(n, [])} if action == "timeline_start" else {}
            _, why = await _call(self.agents, n, action, CALL_TIMEOUT_S, job_id=self.job.job_id, **params, **extra)
            self._note(n, why)
        await asyncio.gather(*(one(n) for n in nodes))

    async def start(self) -> None:
        self.started = True
        rec = self.record
        rec.update(nodes=self.nodes, started_at=time.time(), interval_s=self.interval_s)
        await self._each("timeline_start", interval_s=self.interval_s, max_samples=self.max_samples)

    async def mark(self, stage: str) -> None:
        if not self.started or self.stopped:
            return
        marks = self.record.setdefault("marks", [])
        if len(marks) < 32:
            marks.append({"stage": stage, "at": time.time()})
        await self._each("timeline_mark", stage=stage)

    async def stop(self) -> None:
        if not self.started or self.stopped:
            return
        self.stopped = True
        await self._each("timeline_stop")

    async def finish_success(self) -> Optional[dict[str, Any]]:
        """Stop, keep a small copy in the job and return the per-node summaries (None when nothing came back)."""
        await self.stop()
        nodes = [n for n in self.nodes if n not in self.off]
        if not nodes:
            return None
        data = await collect(self.agents, self.job.job_id, nodes, SUCCESS_POINTS)
        for n, d in data.items():
            self._note(n, d.get("error"))
        kept = stored({n: d for n, d in data.items() if not d.get("error")}, SUCCESS_SERIES)
        if not kept:
            return None
        self.record["nodes_data"] = kept
        return {n: d["summary"] for n, d in kept.items()}
