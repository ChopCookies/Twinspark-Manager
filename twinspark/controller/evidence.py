"""Startup evidence: what a failed activation leaves behind before its containers are removed.

When a launch fails after containers were started, the controller asks every node for its
containers' state, a bounded log, a memory snapshot and the NVIDIA kernel messages since the
container started (agent action ``container_evidence``), *before* cleanup removes the containers.
The logs are redacted on the agent and again here, written to
``<state dir>/evidence/<job id>/<node>-<container>.log`` (0600), and summarised in the job:
exit status, OOM-killer flag, the first relevant exception and the final one.

Limits are explicit: each log is at most :data:`MAX_LOG_CHARS` characters (the end of the log is
kept), and only the evidence of the newest :data:`KEEP_JOBS` failed jobs is retained.
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
