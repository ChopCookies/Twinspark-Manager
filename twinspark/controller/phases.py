"""What a preparation or activation is doing right now, item by item.

A job's steps say *which stage* runs ("downloading"); ``job.payload["phases"]`` says *what* it works on:
an image pull, the main checkpoint, a drafter, a checksum verification or a copy over QSFP, each with
the exact repository and revision (or image reference), the node, a state and, when known, progress.
"""

from __future__ import annotations

import time
from typing import Any, Optional

KINDS = ("image", "checkpoint", "drafter", "verify", "copy")


def phase(job: Any, key: str, *, kind: str, label: str, state: str = "running", detail: str = "",
          node: Optional[str] = None, ref: Optional[str] = None, progress: Optional[float] = None) -> dict:
    """Create or update one work item (state: running | done | reused | failed | skipped)."""
    items: list[dict[str, Any]] = job.payload.setdefault("phases", [])
    item = next((p for p in items if p.get("key") == key), None)
    if item is None:
        item = {"key": key, "kind": kind, "label": label, "node": node, "ref": ref, "started": time.time()}
        items.append(item)
    item.update(state=state, detail=detail, label=label)
    if progress is not None:
        item["progress"] = round(max(0.0, min(1.0, progress)), 3)
    if state != "running":
        item["finished"] = time.time()
        item.pop("progress", None)
    return item


def settle(job: Any, why: Optional[str] = None) -> None:
    """A job that ended leaves no item "in progress": whatever still runs is marked failed."""
    for item in (getattr(job, "payload", None) or {}).get("phases") or []:
        if item.get("state") == "running":
            item.update(state="failed", detail=(why or "the job ended before this finished")[:300],
                        finished=time.time())
            item.pop("progress", None)
