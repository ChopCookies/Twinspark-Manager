"""Client-app integrations for the workstation client: one adapter per local agent app.

Every adapter follows :class:`~twinspark.client.adapters.base.ClientAdapter` (detect, inspect, preview, apply,
validate, launch, stop, status, logs, restore, disconnect) and works on a :class:`HostOps` — the machine where
the app runs. ``validate_endpoint`` is usable on its own (the Integrations page's "Test connection").
"""

from .base import (
    AdapterError,
    ApplyResult,
    Backup,
    Check,
    ClientAdapter,
    Detection,
    Endpoint,
    FileChange,
    LaunchResult,
    Preview,
    RestoreResult,
    State,
    Status,
    StopResult,
    checks_passed,
)
from .hermes import HermesAdapter
from .hosts import HostOps, LocalHost, PathInfo, RunResult, SSHHost
from .validate import validate_endpoint

ADAPTERS = {HermesAdapter.id: HermesAdapter}

__all__ = [
    "ADAPTERS", "AdapterError", "ApplyResult", "Backup", "Check", "ClientAdapter", "Detection", "Endpoint",
    "FileChange", "HermesAdapter", "HostOps", "LaunchResult", "LocalHost", "PathInfo", "Preview", "RestoreResult",
    "RunResult", "SSHHost", "State", "Status", "StopResult", "checks_passed", "validate_endpoint",
]
