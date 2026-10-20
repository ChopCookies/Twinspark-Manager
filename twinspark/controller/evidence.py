"""Startup evidence: what a failed activation leaves behind before its containers are removed.

When a launch fails after containers were started, the controller asks every node for its
containers' state, a bounded log, a memory snapshot and the NVIDIA kernel messages since the
container started (agent action ``container_evidence``), *before* cleanup removes the containers.
The logs are redacted on the agent and again here, written to
``<state dir>/evidence/<job id>/<node>-<container>.log`` (0600), and summarised in the job:
exit status, OOM-killer flag, the first relevant exception and the final one.

Limits are explicit: each log is at most :data:`MAX_LOG_CHARS` characters (the end of the log is
kept), and only the evidence of the newest :data:`KEEP_JOBS` failed jobs is retained.

Since 0.6 the evidence also holds each node's startup memory timeline (``controller/timeline.py``)
and a classification of the failure (:func:`classify`): driver allocation, host OOM, container
limit, timeout, NCCL/RDMA, application exception or unknown, with the evidence behind it.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable, Optional

MAX_LOG_CHARS = 2_000_000          # per container log (end kept)
KEEP_JOBS = 20                     # failed jobs whose evidence is kept
_FILE_RE = re.compile(r"^[AB]-tsm-[a-z0-9._-]+\.log\Z")
_JOB_RE = re.compile(r"^[a-z]+-[0-9a-f]{6,32}\Z")

# A line that starts an error report: a Python traceback, an ERROR log record, or a fatal line.
_START = re.compile(r"Traceback \(most recent call last\)|\bERROR\b|\b(?:CRITICAL|FATAL)\b")
# The exception line that ends a traceback ("SomeError: message", "torch.OutOfMemoryError: ...").
# bounded quantifiers: "(" + a long word on one line must not backtrack quadratically
_EXC = re.compile(r"^\s*(?:\([^()\n]{1,80}pid=\d+\)\s*)?[A-Za-z_][\w.]{0,200}"
                  r"(?:Error|Exception|Exit|Interrupt|Failure)\b.*")
# Wrapper errors that only say "see above": shown as the final error, never as the root cause.
_WRAPPERS = ("engine core initialization failed", "see root cause above", "worker proc", "workerproc",
             "failed core proc", "engine process failed to start", "background loop has errored")
_STRONG = re.compile(r"out of memory|outofmemory|nv_err_no_memory|cuda error|nccl|illegal memory|segmentation"
                     r"|assert|no module named|unrecognized arguments|keyerror|valueerror|notimplementederror",
                     re.IGNORECASE)


def _blocks(lines: list[str]) -> list[list[str]]:
    """Error blocks in order: a traceback up to its exception line, or one ERROR line with its continuation."""
    out: list[list[str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "Traceback (most recent call last)" in line:
            block = [line]
            i += 1
            while i < len(lines) and len(block) < 80:
                block.append(lines[i])
                if _EXC.match(_strip_prefix(lines[i])):
                    break
                i += 1
            out.append(block)
        elif _START.search(line):
            block = [line]
            while i + 1 < len(lines) and len(block) < 12 and lines[i + 1][:1] in (" ", "\t") \
                    and "Traceback" not in lines[i + 1]:
                i += 1
                block.append(lines[i])
            out.append(block)
        i += 1
    return out


def _strip_prefix(line: str) -> str:
    """vLLM prefixes worker output with '(EngineCore_DP0 pid=123) ' or 'ERROR 10-08 01:47:50 [x.py:1] '."""
    line = re.sub(r"^\(\w[\w ]*pid=\d+\)\s*", "", line)
    return re.sub(r"^(?:ERROR|WARNING|INFO|DEBUG)\s+\d\d-\d\d \d\d:\d\d:\d\d\s+\[[^\]]*\]\s*", "", line)


def _is_wrapper(block: list[str]) -> bool:
    text = " ".join(block[-3:]).lower()
    return any(w in text for w in _WRAPPERS)


def error_summary(log: str) -> dict[str, Optional[str]]:
    """The first relevant error and the final one, from a container log.

    The *first* error is the earliest block that is not a mere wrapper ("Engine core initialization
    failed, see root cause above"); a block naming a concrete cause (out of memory, NCCL, assert, ...)
    is preferred over an earlier generic one. The *final* error is the last block in the log.
    """
    lines = log.splitlines()
    blocks = _blocks(lines)
    if not blocks:
        return {"first_error": None, "final_error": None}
    causes = [b for b in blocks if not _is_wrapper(b)]
    strong = [b for b in causes if _STRONG.search("\n".join(b))]
    first = (strong or causes or blocks)[0]
    final = blocks[-1]
    return {"first_error": "\n".join(first)[-6000:],
            "final_error": None if final is first else "\n".join(final)[-6000:]}


def excerpt(evidence: Iterable[dict[str, Any]]) -> str:
    """One text for the job step: every container's root cause first, then its final error."""
    parts = []
    for ev in evidence:
        head = f"== node {ev.get('node')} · {ev.get('name')} · {describe_exit(ev)}"
        body = [head]
        if ev.get("first_error"):
            body += ["-- first error --", ev["first_error"]]
        if ev.get("final_error"):
            body += ["-- final error --", ev["final_error"]]
        if ev.get("kernel_gpu_mem_errors"):
            body.append(f"-- kernel: {'at least ' if ev.get('kernel_count_at_least') else ''}"
                        f"{ev['kernel_gpu_mem_errors']} NVIDIA out-of-memory message(s) since the container started --")
        if len(body) > 1:
            parts.append("\n".join(body))
    return "\n\n".join(parts)[-12000:]


def describe_exit(ev: dict[str, Any]) -> str:
    if ev.get("error"):
        return f"no evidence: {ev['error']}"
    status = ev.get("status") or "unknown"
    if status == "missing":
        return "no such container (it never started, or was already removed)"
    bits = [status]
    if ev.get("exit_code") is not None and status != "running":
        bits.append(f"exit code {ev['exit_code']}")
    if ev.get("oom_killed"):
        bits.append("killed by the kernel OOM killer")
    return ", ".join(bits)


def gpu_memory_note(evidence: Iterable[dict[str, Any]]) -> Optional[str]:
    """Plain-language note when the kernel saw NVIDIA allocation failures but the exit code says little."""
    hits = [ev for ev in evidence if ev.get("kernel_gpu_mem_errors")]
    if not hits:
        return None
    where = ", ".join(f"node {ev['node']}: {'at least ' if ev.get('kernel_count_at_least') else ''}"
                      f"{ev['kernel_gpu_mem_errors']}" for ev in hits)
    return (f"the NVIDIA driver reported out-of-memory errors while the model was starting ({where}). "
            f"A container that exits with code 1 can still have failed on GPU memory: lower "
            f"max_num_batched_tokens, context or concurrency, or gpu_memory_utilization, go headless, "
            f"and compare the first error below with the memory readings")


# ---- failure classification --------------------------------------------------------------------------
CAUSES = ("driver-allocation", "host-oom", "container-limit", "timeout", "rdma-nccl", "application-exception",
          "unknown")
# a CUDA/driver allocation that failed (on a GB10 that is unified memory the driver could not hand out)
_DRIVER_OOM = re.compile(r"NV_ERR_NO_MEMORY|CUDA out of memory|OutOfMemoryError|cudaErrorMemoryAllocation"
                         r"|CUDA_ERROR_OUT_OF_MEMORY|CUBLAS_STATUS_ALLOC_FAILED|cuda error: out of memory", re.I)
_NCCL = re.compile(r"ncclSystemError|ncclInternalError|ncclRemoteError|ncclUnhandledCudaError|ncclInvalidUsage"
                   r"|NCCL (?:error|WARN|failure)|ProcessGroupNCCL|NET/IB|\bibv_[a-z_]{2,40}|\bIBV_[A-Z_]{2,40}"
                   r"|\bRDMA\b|\bmlx5_[a-z0-9_]{1,40}|RoCE", re.I)
_TIMEOUT = re.compile(r"not healthy after \d+|timed out|timeout waiting|did not become ready|"
                      r"engine core .{0,40}not ready", re.I)
_EXC_NAME = re.compile(r"(?:^|\n)\s*(?:\([^()\n]{1,80}pid=\d+\)\s*)?([A-Za-z_][\w.]{0,120}"
                       r"(?:Error|Exception|Exit|Interrupt|Failure))\b:?\s?([^\n]{0,120})")


def _job_text(job: Any) -> str:
    if job is None:
        return ""
    get = (lambda k: job.get(k)) if isinstance(job, dict) else (lambda k: getattr(job, k, None))
    steps = get("steps") or []
    msgs = [(s.get("message") if isinstance(s, dict) else getattr(s, "message", "")) or "" for s in steps
            if (s.get("status") if isinstance(s, dict) else getattr(s, "status", "")) == "failed"]
    return " ".join([str(get("error") or "")] + msgs)


def classify(records: Iterable[dict[str, Any]], timeline: Optional[dict[str, dict]] = None,
             job: Any = None) -> dict[str, Any]:
    """The most likely cause of a failed start, how sure that is, and the evidence behind it.

    ``records`` are the containers' startup evidence, ``timeline`` the per-node timeline summaries,
    ``job`` the job (or its dict). Order: an OOM kill counted against a container limit, an OOM kill
    by the kernel, NVIDIA driver allocation failures (kernel NV_ERR_NO_MEMORY, or CUDA out-of-memory in
    the first error), NCCL / RDMA errors, a health or engine timeout, any other Python exception. An
    exit code 1 alone says nothing: that stays "unknown".
    """
    recs = [r for r in records or [] if isinstance(r, dict)]
    summaries = {n: s for n, s in (timeline or {}).items() if isinstance(s, dict)}
    evidence: list[str] = []
    cause, confidence = "unknown", "low"

    def where(r: dict) -> str:
        return f"node {r.get('node')} {r.get('name')}"

    # cgroup OOM events the timeline saw, with or without a container memory limit
    cg_limit, cg_host = [], []
    for node, s in summaries.items():
        for e in s.get("oom_events") or []:
            if not isinstance(e, dict) or not (e.get("oom_kill") or 0) > 0:
                continue
            (cg_limit if e.get("limit_mib") else cg_host).append((node, e))
    oom_killed = [r for r in recs if r.get("oom_killed")]
    nv = [r for r in recs if (r.get("kernel_gpu_mem_errors") or 0) > 0]
    first = [(r, r.get("first_error") or "") for r in recs]
    log_oom = [(r, t) for r, t in first if _DRIVER_OOM.search(t)]
    nccl = [(r, t) for r, t in first if _NCCL.search(t)]
    exc = [(r, _EXC_NAME.search(t)) for r, t in first]
    exc = [(r, m) for r, m in exc if m]
    job_text = _job_text(job)

    if cg_limit:
        cause, confidence = "container-limit", "high"
        evidence += [f"node {n} {e['container']}: memory.events oom_kill={int(e['oom_kill'])} with a container "
                     f"limit of {e['limit_mib'] / 1024:.1f} GiB" for n, e in cg_limit[:3]]
    elif oom_killed or cg_host:
        cause, confidence = "host-oom", "high" if oom_killed else "medium"
        evidence += [f"{where(r)}: killed by the kernel OOM killer" for r in oom_killed[:3]]
        evidence += [f"node {n} {e['container']}: memory.events oom_kill={int(e['oom_kill'])}, no container limit"
                     for n, e in cg_host[:2]]
    elif nv:
        cause, confidence = "driver-allocation", "high"
        evidence += [f"{where(r)}: {'at least ' if r.get('kernel_count_at_least') else ''}"
                     f"{r['kernel_gpu_mem_errors']} NVIDIA NV_ERR_NO_MEMORY kernel message(s)" for r in nv[:3]]
    elif log_oom:
        cause, confidence = "driver-allocation", "medium"
        evidence += [f"{where(r)}: first error: {_DRIVER_OOM.search(t).group(0)}" for r, t in log_oom[:2]]
    elif nccl:
        cause, confidence = "rdma-nccl", "high" if any("nccl" in t.lower() for _, t in nccl) else "medium"
        evidence += [f"{where(r)}: first error mentions {_NCCL.search(t).group(0)}" for r, t in nccl[:2]]
    elif _TIMEOUT.search(job_text):
        cause, confidence = "timeout", "high"
        evidence.append("the job ended on a timeout: " + _TIMEOUT.search(job_text).group(0))
    elif exc:
        cause, confidence = "application-exception", "medium"
        evidence += [f"{where(r)}: {m.group(1)}: {m.group(2).strip()}"[:200].rstrip(": ") for r, m in exc[:2]]

    if cause in ("driver-allocation", "host-oom") and log_oom and not any("first error" in e for e in evidence):
        evidence.append(f"{where(log_oom[0][0])}: first error: {_DRIVER_OOM.search(log_oom[0][1]).group(0)}")
    # supporting readings from the timeline (shown whatever the cause)
    for node, s in sorted(summaries.items()):
        low = s.get("min_mem_available_gib")
        if isinstance(low, (int, float)) and low < 2.0:
            evidence.append(f"node {node}: MemAvailable fell to {low:.1f} GiB"
                            + (f" during {s['min_stage']}" if s.get("min_stage") else ""))
        full = s.get("max_psi_full_avg10")
        if isinstance(full, (int, float)) and full >= 10:
            evidence.append(f"node {node}: memory pressure reached {full:.0f}% (PSI full avg10)")
        out = s.get("peak_swap_out_pps")
        if isinstance(out, (int, float)) and out >= 1000:
            evidence.append(f"node {node}: swapping out up to {out:,.0f} pages/s")
    if cause == "unknown":
        exits = [r for r in recs if r.get("exit_code") not in (None, 0) and r.get("status") != "running"]
        if exits:
            evidence.append(f"{where(exits[0])}: exit code {exits[0]['exit_code']} with no error message, OOM flag "
                            f"or NVIDIA kernel messages — the exit code alone does not tell the cause")
        elif not recs and job_text.strip():
            evidence.append("no container had started: " + job_text.strip()[:160])
    return {"cause": cause, "confidence": confidence, "evidence": [e[:220] for e in evidence[:8]]}


CAUSE_TEXT = {
    "driver-allocation": "the NVIDIA driver could not allocate memory",
    "host-oom": "the kernel ran out of memory and killed a process",
    "container-limit": "a container hit its own memory limit",
    "timeout": "the start ran out of time",
    "rdma-nccl": "the nodes could not communicate (NCCL / RDMA)",
    "application-exception": "vLLM stopped on an error in its own code or settings",
    "unknown": "the cause could not be determined from the evidence",
}


# ---- storage ---------------------------------------------------------------------------------------
def evidence_dir(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / "evidence"


def save(root: Path, job_id: str, node: str, name: str, log: str) -> tuple[str, int]:
    """Write one log privately; returns (file name, bytes)."""
    if not _JOB_RE.match(job_id):
        raise ValueError("invalid job id")
    fname = f"{node}-{name}.log"
    if not _FILE_RE.match(fname):
        raise ValueError("invalid evidence file name")
    d = root / job_id
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(d, 0o700)
    data = log[-MAX_LOG_CHARS:].encode("utf-8", "replace")
    path = d / fname
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return fname, len(data)


def open_file(root: Path, job_id: str, fname: str) -> Path:
    """The saved log, or ValueError/FileNotFoundError — names are checked, never joined blindly."""
    if not _JOB_RE.match(job_id) or not _FILE_RE.match(fname):
        raise ValueError("invalid evidence reference")
    path = root / job_id / fname
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(fname)
    return path


def prune(root: Path, keep: int = KEEP_JOBS) -> list[str]:
    """Delete the evidence of all but the newest ``keep`` jobs; returns the removed job ids."""
    if not root.is_dir():
        return []
    dirs = sorted((d for d in root.iterdir() if d.is_dir() and not d.is_symlink() and _JOB_RE.match(d.name)),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    removed = []
    for d in dirs[keep:]:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed
