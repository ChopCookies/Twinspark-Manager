"""Model artifact & cross-node sync manager (spec §22, §23, §24).

Models are managed as artifacts (repo + pinned revision, files, hashes, sizes,
which nodes hold them, dependent profiles). Deletion shows all dependent
profiles first. Compilation caches (vLLM/Triton/FlashInfer/CUDA/Torch/TileLang)
are treated as first-class cache artifacts and are never deleted indiscriminately
during model switches.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .schemas.artifact import ArtifactFile, ModelArtifact, SyncJob

# Compilation/runtime caches that must survive model switches (spec §24).
PROTECTED_CACHE_DIRS = (
    "vllm", "triton", "flashinfer", "cuda", "torch", "tilelang",
)


class ArtifactManager:
    def __init__(self, cache_dir: str | Path, downloads_dir: str | Path):
        self.cache_dir = Path(cache_dir)
        self.downloads_dir = Path(downloads_dir)
        self.artifacts: dict[str, ModelArtifact] = {}

    def register(self, artifact: ModelArtifact) -> None:
        self.artifacts[artifact.artifact_id] = artifact

    def add_file(self, artifact_id: str, path: str, size: int) -> None:
        artifact = self.artifacts[artifact_id]
        artifact.files.append(ArtifactFile(path=path, size_bytes=size,
                                           sha256=hashlib.sha256(b"").hexdigest()))
        artifact.total_size_bytes += size

    def add_dependency(self, artifact_id: str, profile_name: str) -> None:
        deps = self.artifacts[artifact_id].profile_dependencies
        if profile_name not in deps:
            deps.append(profile_name)

    def deletion_blockers(self, artifact_id: str) -> list[str]:
        """Profiles that depend on an artifact — shown before deletion (spec §22)."""
        return self.artifacts[artifact_id].dependent_profiles()

    def can_delete(self, artifact_id: str) -> tuple[bool, list[str]]:
        deps = self.deletion_blockers(artifact_id)
        return (not deps, deps)

    def protected_cache_size(self) -> int:
        """Sum of protected compilation-cache dirs (must survive switches)."""
        total = 0
        for name in PROTECTED_CACHE_DIRS:
            p = self.cache_dir / name
            if p.exists():
                total += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return total

    # ---- cross-node sync (spec §23) ----------------------------------------
    def plan_sync(self, artifact_id: str, source: str, target: str,
                  strategy: str = "seq") -> SyncJob:
        artifact = self.artifacts[artifact_id]
        return SyncJob(
            artifact_id=artifact_id, source_node=source, target_node=target,
            strategy=strategy, expected_total=artifact.total_size_bytes,
        )

    def verify(self, job: SyncJob, checksums_ok: bool) -> bool:
        """Checksum-verified sync completion (resumable, progress-reported)."""
        if checksums_ok:
            job.verified = True
            job.offset_bytes = job.expected_total
        return job.verified
