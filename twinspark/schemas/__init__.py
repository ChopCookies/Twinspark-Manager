"""Domain schema exports for TwinSpark.

The names below resolve lazily (PEP 562): importing ``twinspark.schemas.config`` on a Spark's agent
does not build the profile, job and artifact pydantic models as a side effect of this package being
imported. Those models are the largest part of a TwinSpark process's memory after the framework.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "artifact": ("ArtifactFile", "ModelArtifact", "SyncJob"),
    "config": ("AgentConfig", "ControllerConfig", "NodeEndpoint", "NodeIdentity", "RuntimeSettings"),
    "enums": ("ActivationStage", "DataClass", "DistributedBackend", "HeadlessMode", "JobState", "MemoryStrategy",
              "NodeRole", "Quantization", "RuntimeAdapter", "Topology", "VerificationStatus"),
    "job": ("AuditEntry", "Job", "JobStep"),
    "profile": ("AdvancedSettings", "BehaviourSettings", "ImmutableIdentity", "Profile", "ProfileDraft",
                "ProfileRevision", "SimpleSettings"),
}
_WHERE = {name: module for module, names in _EXPORTS.items() for name in names}

__all__ = sorted(_WHERE)


def __getattr__(name: str) -> Any:
    module = _WHERE.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
