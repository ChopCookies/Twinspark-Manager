"""Agent adapter for `tsm update`: passes the controller's requests to tsm-privd.

A dry-run agent (the demo, the tests, a node set up with ``runtime_mode: dry-run``) only simulates: it never
calls privd, downloads nothing and restarts nothing; a simulated run completes at once and says so.
"""

from __future__ import annotations

from typing import Any

from .. import __version__
from .selfupdate import check_target, run_id


class NodeUpdate:
    def __init__(self, actions: Any) -> None:
        self.actions = actions
        self.runs: dict[str, dict] = {}

    def probe(self, params: dict) -> dict:
        a = self.actions
        if a.dry_run:
            return {"enabled": True, "allow_prerelease": True, "dry_run": True, "version": __version__,
                    "units": [], "active_run": None}
        return {**a.privd.call("update_probe"), "agent_version": __version__}

    def start(self, params: dict) -> dict:
        rid = run_id(params)
        target = check_target(params.get("target"))
        if self.actions.dry_run:
            return self.runs.setdefault(rid, {"run_id": rid, "state": "completed", "phase": "simulated",
                                              "dry_run": True, "target": target, "from_version": __version__})
        return self.actions.privd.call("update_start", {"run_id": rid, "target": params.get("target")})

    def status(self, params: dict) -> dict:
        rid = run_id(params)
        if self.actions.dry_run:
            return self.runs.get(rid, {"run_id": rid, "state": "missing"})
        return self.actions.privd.call("update_status", {"run_id": rid})
