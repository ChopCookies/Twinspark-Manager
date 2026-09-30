"""Model artifact model (spec §22) and cross-node sync state (spec §23)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArtifactFile(BaseModel):
    path: str
    size_bytes: int
    sha256: str


class ModelArtifact(BaseModel):
    """A model managed as an artifact, not just a directory (spec §22)."""

    artifact_id: str
    repo: str
    revision: str
    files: list[ArtifactFile] = Field(default_factory=list)
    total_size_bytes: int = 0
    nodes: list[str] = Field(default_factory=list)      # which nodes hold it
    profile_dependencies: list[str] = Field(default_factory=list)  # profile names
    last_used: Optional[str] = None
    created_at: str = Field(default_factory=_now)

    def dependent_profiles(self) -> list[str]:
        """For delete-confirmation UI (spec §22)."""
        return list(self.profile_dependencies)

    @property
    def downloaded(self) -> bool:
        return bool(self.files) and self.total_size_bytes > 0


class SyncJob(BaseModel):
    """Cross-node model sync state (spec §23)."""

    artifact_id: str
    source_node: str = "A"
    target_node: str = "B"
    strategy: str = "seq"               # seq: A→QSFP→B ; parallel: both from source
    offset_bytes: int = 0               # resumable
    expected_total: int = 0
    verified: bool = False
    resume_token: Optional[str] = None
    updated_at: str = Field(default_factory=_now)
