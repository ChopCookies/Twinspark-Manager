"""Bounded upstream checks; updates become independent, reviewable experiments."""
from __future__ import annotations

import asyncio

from ..schemas.profile import Profile
from . import build_draft, import_text
from .remote import RemoteError, fetch_text
from .tracking import merge_update, settings


def _diff(before, after, path="") -> list[dict]:
    if isinstance(before, dict) and isinstance(after, dict):
        return [row for key in sorted(before.keys() | after.keys())
                for row in _diff(before.get(key), after.get(key), f"{path}.{key}" if path else key)]
    return [] if before == after else [{"field": path, "before": before, "after": after}]


def _review_changes(before, after):
    old, new = settings(before), settings(after)

    def pins(left, right, previous, updated):
        if updated.identity:
            left["identity"] = previous.identity.model_dump(mode="json") if previous and previous.identity else None
            right["identity"] = updated.identity.model_dump(mode="json")
        if updated.secondary:
            left["secondary"] = left.get("secondary") or {}
            pins(left["secondary"], right["secondary"], previous.secondary if previous else None, updated.secondary)

    pins(old, new, before, after)
    return _diff(old, new)


def _name(ctrl, original: str) -> str:
    suffix, index = "-update", 1
    while True:
        candidate = original[:63 - len(suffix)] + suffix
        if ctrl.get_profile(candidate) is None:
            return candidate
        index += 1
        suffix = f"-update-{index}"


async def check_updates(ctrl, profiles: list[Profile]) -> list[dict]:
    limit = asyncio.Semaphore(4)
    fetched = {}

    async def fetch(ref):
        async with limit:
            return await asyncio.to_thread(fetch_text, ref)

    async def check(profile):
        draft = profile.working_draft()
        receipt = (draft.source or {}).get("receipt") if draft else None
        ref = receipt.get("ref") if isinstance(receipt, dict) else None
        row = {"profile": profile.name, "ref": ref}
        if not isinstance(ref, str) or not (ref.startswith("https://") or ref.startswith("builtin:")):
            return {**row, "status": "untracked", "message": "Import from a URL or the Cookbook to track updates."}
        try:
            if not isinstance(receipt.get("overrides", {}), dict):
                raise ValueError("recipe template overrides are invalid — re-import it to restore tracking")
            if ref.startswith("builtin:"):
                upstream = build_draft(ref.removeprefix("builtin:").rsplit(".", 1)[0])
                if upstream is None:
                    raise RemoteError("built-in recipe is no longer available")
                report = {}
            else:
                if ref not in fetched:
                    fetched[ref] = asyncio.create_task(fetch(ref))
                text = await fetched[ref]
                upstream, report = import_text(text, source_ref=ref, overrides=receipt.get("overrides"))
            incoming = upstream.source["receipt"]
            if (incoming["sha256"], incoming.get("metadata_sha256")) == \
                    (receipt.get("sha256"), receipt.get("metadata_sha256")):
                return {**row, "status": "current", "sha256": incoming["sha256"]}
            updated, preserved, conflicts = merge_update(draft, upstream)
            updated.name = _name(ctrl, profile.name)
            updated.source["updated_from"] = {"profile": profile.name, "sha256": receipt.get("sha256")}
            return {**row, "status": "changed", "sha256": incoming["sha256"],
                    "previous_sha256": receipt.get("sha256"), "preserved": preserved, "conflicts": conflicts,
                    "changes": _review_changes(draft, updated), "preview": {
                        "draft": updated.model_dump(mode="json"), "report": report, "exists": False,
                        "warnings": updated.warnings(),
                    }}
        except (RemoteError, ValueError) as exc:
            return {**row, "status": "error", "message": str(exc)}

    return await asyncio.gather(*(check(profile) for profile in profiles))
