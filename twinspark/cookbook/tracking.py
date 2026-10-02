"""Recipe receipts and three-way merges for upstream experiments."""
from __future__ import annotations

import copy
import hashlib
from typing import Any

from ..schemas.enums import VerificationStatus
from ..schemas.profile import ProfileDraft


def settings(draft: ProfileDraft) -> dict:
    doc = draft.model_dump(mode="json", exclude={"name", "source", "identity", "verification"})
    if draft.secondary:
        doc["secondary"] = settings(draft.secondary)
    return doc


def record_import(draft: ProfileDraft, text: str, ref: str | None, fmt: str,
                  overrides: dict | None = None, metadata: str | None = None) -> ProfileDraft:
    receipt = {"ref": ref or "pasted", "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
               "format": fmt, "overrides": copy.deepcopy(overrides or {}), "baseline": settings(draft)}
    if metadata is not None:
        receipt["metadata_sha256"] = hashlib.sha256(metadata.encode("utf-8")).hexdigest()
    return draft.model_copy(update={"source": {**draft.source, "receipt": receipt}}, deep=True)


_MISSING = object()


def merge_update(current: ProfileDraft, upstream: ProfileDraft) -> tuple[ProfileDraft, list[str], list[str]]:
    """Preserve local edits; expose conflicts for review rather than overwriting them."""
    baseline = current.source.get("receipt", {}).get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("recipe baseline is missing — re-import it to enable update tracking")
    preserved, conflicts = [], []

    def merge(base: Any, local: Any, remote: Any, path: str):
        if local == base:
            return copy.deepcopy(remote) if remote is not _MISSING else _MISSING
        if local == remote:
            return copy.deepcopy(local) if local is not _MISSING else _MISSING
        if all(isinstance(x, dict) for x in (base, local, remote)):
            result = {}
            for key in sorted(base.keys() | local.keys() | remote.keys()):
                value = merge(base.get(key, _MISSING), local.get(key, _MISSING), remote.get(key, _MISSING),
                              f"{path}.{key}" if path else key)
                if value is not _MISSING:
                    result[key] = value
            return result
        preserved.append(path)
        if remote != base:
            conflicts.append(path)
        return copy.deepcopy(local) if local is not _MISSING else _MISSING

    merged = merge(baseline, settings(current), settings(upstream), "")

    def restore(doc, template):
        doc.update(name=template.name, source=template.source,
                   verification=VerificationStatus.EXPERIMENTAL.value)
        identity = template.identity
        # An upstream JSON may explicitly pin its model/image. Preserve compatible
        # pins, but never carry the old experiment's resolved pins into an update.
        compatible = identity and identity.model_repo == doc["simple"]["model"] and \
            identity.quantization.value == doc["simple"]["quantization"]
        doc["identity"] = identity.model_dump(mode="json") if compatible else None
        if identity and not doc.get("image_hint") and not compatible:
            doc["image_hint"] = identity.image_ref
        if doc.get("secondary"):
            if template.secondary is None:
                raise ValueError("upstream changed the split topology — review and import it as a new recipe")
            restore(doc["secondary"], template.secondary)

    restore(merged, upstream)
    # The new baseline describes upstream, not the locally customized merged result.
    return ProfileDraft.model_validate(merged), preserved, conflicts
