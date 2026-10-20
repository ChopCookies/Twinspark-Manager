"""Startup memory timeline on one node: a bounded, read-only sampler (0.6).

The controller starts it right before an activation stops the old model (``timeline_start``), marks
every stage (``timeline_mark``), stops it when the new model serves or the start fails
(``timeline_stop``) and reads it back (``timeline_read``), also after the containers are gone.

One sampler per agent: starting a new one ends the previous one (only one activation runs at a
time, so an older timeline belongs to an activation that is over). Memory is bounded by
``max_samples`` (≤ :data:`MAX_SAMPLES`); the sampler stops by itself after ``max_samples`` samples or
:data:`MAX_DURATION_S`, whichever comes first, so a controller that disappears mid-start leaves
nothing running.

Each sample: offset since start (monotonic) and wall clock, MemAvailable / MemFree / page cache /
swap used, swap-in/out pages per second (/proc/vmstat deltas), memory PSI some/full avg10, each
TwinSpark unit's cgroup memory.current, and for the deployment's containers their cgroup
memory.current/peak/max and memory.events oom/oom_kill. ``driver_allocated_gib`` is always None:
on a GB10, nvidia-smi reports no memory figures, so what the NVIDIA driver holds is not known.
A reading that cannot be taken is None, never 0. A dry-run agent records synthetic values marked
``"simulated": true``.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..controller import timeline as shared
from . import sysinfo

if TYPE_CHECKING:
    from .actions import AgentActions

MAX_SAMPLES = 7200
MAX_DURATION_S = 2 * 3600
MIN_INTERVAL_S, MAX_INTERVAL_S = 0.2, 10.0
MAX_MARKS = 64
MAX_READ = 2000                     # samples per timeline_read answer when no max_points is given
ID_RETRY_S = 5.0                    # a container that is not there yet is looked up again after this
_JOB_RE = re.compile(r"^[a-z]+-[0-9a-f]{6,32}\Z")
_STAGE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}\Z")
_NAME_RE = re.compile(r"^tsm-[a-z0-9._-]+\Z")
_GIB_KB = 1024 ** 2
# dry-run: what MemAvailable (GiB) looks like on a GB10 in each stage of a switch between two big models
# (the old model holds most of the node until it is stopped)
_SIMULATED = {None: 14.8, "stopping": 15.2, "reclaiming": 110.2, "starting-cluster": 109.8, "loading": 21.4,
              "testing": 14.6, "routing": 14.9, "healthy": 15.1, "failed": 13.8}


class TimelineError(ValueError):
    pass


@dataclass
class _Run:
    job_id: str
    interval_s: float
    max_samples: int
    containers: list[str]
    simulated: bool
    started_mono: float
    started_wall: float
    samples: list[dict] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)
    stage: Optional[str] = None
    running: bool = True
    stopped_reason: Optional[str] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    prev_swap: Optional[tuple[float, int, int]] = None
    cids: dict[str, Optional[str]] = field(default_factory=dict)
    cid_tried: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def container_id(runtime: Any, name: str) -> Optional[str]:
    """Full container ID through the Docker runtime's own command runner; None for the dry-run runtime,
    a missing container or any error."""
    run, docker = getattr(runtime, "_run", None), getattr(runtime, "docker", None)
    if run is None or docker is None or runtime.__class__.__name__ == "DryRunRuntime":
        return None
    try:
        out = str(run([docker, "inspect", "--format", "{{.Id}}", name], timeout=10, check=False)).strip()
    except Exception:  # noqa: BLE001 - a lookup that fails leaves the container unknown
        return None
    return out if re.fullmatch(r"[0-9a-f]{64}", out) else None


class TimelineSampler:
    def __init__(self, *, runtime: Any, simulated: bool, proc: str = "/proc",
                 cgroup_root: str = sysinfo.CGROUP_ROOT, clock: Callable[[], float] = time.monotonic):
        self.runtime, self.simulated = runtime, simulated
        self.proc, self.cgroup_root, self.clock = proc, cgroup_root, clock
        self._lock = threading.Lock()
        self._run: Optional[_Run] = None

    # ---- actions ----------------------------------------------------------------------------
    def start(self, job_id: str, interval_s: float = 1.0, max_samples: int = 3600,
              containers: Optional[list[str]] = None) -> dict[str, Any]:
        job_id = _job(job_id)
        try:
            interval = min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, float(interval_s)))
            limit = min(MAX_SAMPLES, max(1, int(max_samples)))
        except (TypeError, ValueError) as exc:
            raise TimelineError("interval_s and max_samples must be numbers") from exc
        names = [str(c) for c in containers or []]
        if len(names) > 16 or any(not _NAME_RE.match(c) for c in names):
            raise TimelineError("containers must be up to 16 tsm-* container names")
        previous = self._end("replaced by the timeline of job " + job_id)
        run = _Run(job_id=job_id, interval_s=interval, max_samples=limit, containers=names,
                   simulated=self.simulated, started_mono=self.clock(), started_wall=time.time())
        first = self._sample(run)
        with self._lock:
            run.samples.append(first)
            self._run = run
        run.thread = threading.Thread(target=self._loop, args=(run,), name=f"tsm-timeline-{job_id}", daemon=True)
        run.thread.start()
        return {"started": True, "job_id": job_id, "interval_s": interval, "max_samples": limit,
                "max_duration_s": min(MAX_DURATION_S, interval * limit), "replaced": previous,
                "simulated": run.simulated}

    def mark(self, job_id: str, stage: str) -> dict[str, Any]:
        job_id = _job(job_id)
        stage = str(stage)
        if not _STAGE_RE.match(stage):
            raise TimelineError("invalid stage name")
        run = self._current(job_id)
        if run is None or not run.running:
            return {"marked": False, "running": False,
                    "note": "no timeline is recording for this job (stopped, or the agent restarted)"}
        with self._lock:
            run.stage = stage
            if len(run.marks) < MAX_MARKS:
                run.marks.append({"stage": stage, "t": round(self.clock() - run.started_mono, 2),
                                  "at": time.time(), "index": len(run.samples)})
        # a sample exactly at the stage edge, so every marker has a reading
        sample = self._sample(run)
        with self._lock:
            if run.running and len(run.samples) < run.max_samples:
                run.samples.append(sample)
        return {"marked": True, "stage": stage, "samples": len(run.samples)}

    def stop(self, job_id: str) -> dict[str, Any]:
        job_id = _job(job_id)
        run = self._current(job_id)
        if run is None:
            return {"stopped": False, "note": "no timeline for this job"}
        if run.running:
            self._finish(run, "stopped")
        return {"stopped": True, "samples": len(run.samples), "reason": run.stopped_reason}

    def read(self, job_id: str, since_index: int = 0, max_points: Optional[int] = None) -> dict[str, Any]:
        job_id = _job(job_id)
        run = self._current(job_id)
        if run is None:
            raise TimelineError("no timeline for this job on this node (another job replaced it, or the agent "
                                "restarted)")
        with self._lock:
            samples, marks = list(run.samples), [dict(m) for m in run.marks]
            running, reason = run.running, run.stopped_reason
        start = max(0, int(since_index or 0))
        part = samples[start:]
        if max_points:
            part = shared.downsample(part, min(MAX_READ, int(max_points)),
                                     [{**m, "index": m["index"] - start} for m in marks])
        else:
            part = part[:MAX_READ]
        nxt = len(samples) if max_points else start + len(part)
        return {"job_id": job_id, "running": running, "stopped_reason": reason, "interval_s": run.interval_s,
                "started_at": run.started_wall, "total_samples": len(samples), "next_index": nxt,
                "samples": part, "marks": marks,
                "summary": shared.summarize(samples, marks), "simulated": run.simulated,
                "driver_allocated": "unknown", "notes": list(run.notes)}

    # ---- internals ----------------------------------------------------------------------------
    def _current(self, job_id: str) -> Optional[_Run]:
        with self._lock:
            run = self._run
        return run if run is not None and run.job_id == job_id else None

    def _end(self, reason: str) -> Optional[str]:
        with self._lock:
            run = self._run
        if run is None:
            return None
        if run.running:
            self._finish(run, reason)
        return run.job_id

    def _finish(self, run: _Run, reason: str) -> None:
        run.stop_event.set()
        if run.thread is not None and run.thread is not threading.current_thread():
            run.thread.join(timeout=2 * MAX_INTERVAL_S)
        last = self._sample(run) if run.running else None
        with self._lock:
            if last is not None and len(run.samples) < run.max_samples:
                run.samples.append(last)
            run.running = False
            run.stopped_reason = run.stopped_reason or reason

    def _loop(self, run: _Run) -> None:
        while not run.stop_event.wait(run.interval_s):
            if not run.running:
                return
            sample = self._sample(run)
            with self._lock:
                if not run.running:
                    return
                run.samples.append(sample)
                full = len(run.samples) >= run.max_samples
                late = self.clock() - run.started_mono >= MAX_DURATION_S
                if full or late:
                    run.running = False
                    run.stopped_reason = "reached max_samples" if full else "reached the maximum duration"
                    run.stop_event.set()
                    return

    def _sample(self, run: _Run) -> dict[str, Any]:
        now = self.clock()
        base = {"t": round(now - run.started_mono, 2), "at": round(time.time(), 2), "stage": run.stage,
                "driver_allocated_gib": None}
        if run.simulated:
            return {**base, **self._synthetic(run)}
        mem = sysinfo.read_meminfo(f"{self.proc}/meminfo")

        def gib(*keys: str) -> Optional[float]:
            if not all(k in mem for k in keys):
                return None
            return round(sum(mem[k] for k in keys) / _GIB_KB, 2)
        swap_used = None
        if "SwapTotal" in mem and "SwapFree" in mem:
            swap_used = round((mem["SwapTotal"] - mem["SwapFree"]) / _GIB_KB, 2)
        vm = sysinfo.read_vmstat(f"{self.proc}/vmstat")
        swap_in = swap_out = None
        if "pswpin" in vm and "pswpout" in vm:
            prev = run.prev_swap
            if prev is not None and now > prev[0]:
                dt = now - prev[0]
                swap_in = round(max(0, vm["pswpin"] - prev[1]) / dt, 1)
                swap_out = round(max(0, vm["pswpout"] - prev[2]) / dt, 1)
            run.prev_swap = (now, vm["pswpin"], vm["pswpout"])
        psi = sysinfo.read_psi(f"{self.proc}/pressure/memory") or {}
        services = {u: v.get("current_mib") for u, v in sysinfo.services_memory(self.cgroup_root).items()}
        return {**base, "mem_total_gib": gib("MemTotal"), "mem_available_gib": gib("MemAvailable"),
                "mem_free_gib": gib("MemFree"), "page_cache_gib": gib("Cached", "Buffers"),
                "swap_used_gib": swap_used, "swap_in_pps": swap_in, "swap_out_pps": swap_out,
                "psi_some_avg10": psi.get("some_avg10"), "psi_full_avg10": psi.get("full_avg10"),
                "services": services, "containers": self._containers(run, now)}

    def _containers(self, run: _Run, now: float) -> dict[str, Optional[dict]]:
        out: dict[str, Optional[dict]] = {}
        for name in run.containers:
            cid = run.cids.get(name)
            if cid is None and now - run.cid_tried.get(name, -1e9) >= ID_RETRY_S:
                run.cid_tried[name] = now
                cid = container_id(self.runtime, name)
                if cid:
                    run.cids[name] = cid
            d = sysinfo.container_cgroup_dir(cid, self.cgroup_root) if cid else None
            out[name] = sysinfo.cgroup_memory(d) if d else None
        return out

    def _synthetic(self, run: _Run) -> dict[str, Any]:
        i = len(run.samples)
        avail = _SIMULATED.get(run.stage, 14.8) - 0.3 * (i % 3)
        return {"mem_total_gib": 121.6, "mem_available_gib": round(avail, 2), "mem_free_gib": round(avail - 2.5, 2),
                "page_cache_gib": 3.1, "swap_used_gib": 0.0, "swap_in_pps": 0.0, "swap_out_pps": 0.0,
                "psi_some_avg10": 0.0, "psi_full_avg10": 0.0, "services": {}, "containers": {},
                "simulated": True}


def _job(job_id: Any) -> str:
    job_id = str(job_id or "")
    if not _JOB_RE.match(job_id):
        raise TimelineError("invalid job id")
    return job_id


def build(actions: "AgentActions") -> dict[str, Callable[[dict], Any]]:
    """The four typed timeline actions. All are read-only towards the host."""
    from .actions import ActionError

    sampler = TimelineSampler(runtime=actions.runtime, simulated=actions.dry_run)
    actions.timeline = sampler

    def guard(fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
        def run(params: dict) -> Any:
            try:
                return fn(params)
            except TimelineError as exc:
                raise ActionError(str(exc), status=404 if str(exc).startswith("no timeline") else 422) from exc
        return run

    return {
        "timeline_start": guard(lambda p: sampler.start(p.get("job_id"), p.get("interval_s", 1.0),
                                                        p.get("max_samples", 3600), p.get("containers"))),
        "timeline_mark": guard(lambda p: sampler.mark(p.get("job_id"), p.get("stage", ""))),
        "timeline_stop": guard(lambda p: sampler.stop(p.get("job_id"))),
        "timeline_read": guard(lambda p: sampler.read(p.get("job_id"), p.get("since_index", 0),
                                                      p.get("max_points"))),
    }

