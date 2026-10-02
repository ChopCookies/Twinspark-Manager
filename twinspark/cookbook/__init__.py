"""Cookbook: curated dual-Spark recipes + importers for community recipe formats.

Built-in recipes live in :mod:`twinspark.cookbook.recipes`:

* ``*.json`` — TwinSpark drafts (a :class:`ProfileDraft` with ``image_hint`` and
  ``source`` provenance: where the recipe comes from, what it needs, what it
  measured).
* ``*.yaml`` — eugr/spark-vllm-docker recipes, converted on the fly by
  :mod:`twinspark.cookbook.eugr` (the same importer used for your own files).

Importing creates a real profile with every setting in its working draft;
pinning (commit sha + image digest) turns it into an activatable revision.
Import never downloads anything and never touches the running vLLM.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..schemas.enums import VerificationStatus
from ..schemas.profile import ProfileDraft
from .eugr import RecipeImportError, parse_eugr_recipe, slugify
from .tracking import record_import

RECIPES_DIR = Path(__file__).resolve().parent / "recipes"
_GITHUB_BLOB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$")

__all__ = ["RECIPES_DIR", "RecipeImportError", "build_draft", "import_eugr", "import_text",
           "list_recipes", "load_recipe", "raw_url", "recipe_detail", "slugify"]


def _slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _files() -> dict[str, Path]:
    out = {}
    for p in sorted(RECIPES_DIR.glob("*")):
        if p.suffix in (".json", ".yaml", ".yml") and not p.name.endswith(".meta.json"):
            out[p.stem] = p
    return out


def _draft_from_file(path: Path, profile_name: Optional[str] = None) -> tuple[ProfileDraft, dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        draft, report = import_text(text, profile_name, source_ref=f"builtin:{path.name}")
        draft.source["recipe"] = path.stem
        return record_import(draft, text, f"builtin:{path.name}", "twinspark"), report
    draft, report = parse_eugr_recipe(text, profile_name or path.stem,
                                      source_ref=f"builtin:{path.name}")
    extra = (draft.source or {})
    meta_path = path.with_suffix(".meta.json")
    update: dict[str, Any] = {}
    metadata = None
    if meta_path.exists():
        metadata = meta_path.read_text(encoding="utf-8")
        meta = json.loads(metadata)
        if meta.get("verification"):
            update["verification"] = VerificationStatus(meta["verification"])
        if meta.get("kv_bytes_per_token"):
            extra = {**extra, "kv_bytes_per_token": meta["kv_bytes_per_token"]}
        extra = {**extra, **meta}
    update["source"] = {**extra, "recipe": path.stem}
    draft = draft.model_copy(update=update)
    return record_import(draft, text, f"builtin:{path.name}", "eugr", metadata=metadata), report


def list_recipes() -> list[dict[str, Any]]:
    """The recipe index with provenance for the GUI/CLI."""
    out = []
    for stem, path in _files().items():
        try:
            draft, _ = _draft_from_file(path)
        except (ValueError, RecipeImportError) as exc:
            out.append({"name": stem, "error": str(exc)})
            continue
        src = draft.source or {}
        out.append({
            "name": stem,
            "title": src.get("title") or draft.description.split(".")[0][:80] or stem,
            "description": draft.description,
            "model": draft.simple.model,
            "topology": draft.simple.topology.value,
            "quantization": draft.simple.quantization.value,
            "verification": draft.verification.value,
            "format": "eugr" if path.suffix in (".yaml", ".yml") else "twinspark",
            "image_hint": draft.image_hint,
            "mods": draft.advanced.mods,
            "extra_models": draft.advanced.extra_models,
            "url": src.get("url"),
            "author": src.get("author"),
            "measured": src.get("measured") or {},
            "requirements": src.get("requirements") or [],
            "notes": src.get("notes") or [],
            "source": "cookbook",
        })
    return out


def recipe_detail(name: str) -> Optional[dict[str, Any]]:
    """Index entry + the full draft + the importer report (what went where)."""
    path = _files().get(_slug(name))
    if path is None:
        return None
    draft, report = _draft_from_file(path)
    entry = next((r for r in list_recipes() if r["name"] == path.stem), {"name": path.stem})
    return {**entry, "draft": draft.model_dump(mode="json"), "report": report,
            "file": path.name, "text": path.read_text(encoding="utf-8")}


def load_recipe(name: str) -> Optional[dict]:
    """Raw recipe document by slug (JSON recipes only). Returns None if unknown."""
    path = RECIPES_DIR / f"{_slug(name)}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_draft(name: str, profile_name: Optional[str] = None) -> Optional[ProfileDraft]:
    """Turn a built-in recipe into a validated :class:`ProfileDraft` (no identity)."""
    path = _files().get(_slug(name))
    if path is None:
        return None
    return _draft_from_file(path, profile_name)[0]


def import_eugr(text: str, profile_name: Optional[str] = None,
                source_ref: Optional[str] = None,
                overrides: Optional[dict[str, Any]] = None) -> tuple[ProfileDraft, dict]:
    return parse_eugr_recipe(text, profile_name, overrides=overrides, source_ref=source_ref)


def import_text(text: str, profile_name: Optional[str] = None, source_ref: Optional[str] = None,
                overrides: Optional[dict[str, Any]] = None) -> tuple[ProfileDraft, dict]:
    """Any supported recipe text: a TwinSpark draft (JSON) or an eugr recipe (YAML)."""
    if len(text.encode("utf-8")) > 512 * 1024:
        raise RecipeImportError("recipe file larger than 512 KiB")
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("{"):
        try:
            doc = json.loads(stripped)
        except ValueError as exc:
            raise RecipeImportError(f"not valid JSON: {exc}") from exc
        if profile_name:
            doc["name"] = profile_name
        try:
            draft = ProfileDraft.model_validate(doc)
        except ValueError as exc:
            raise RecipeImportError(f"not a valid TwinSpark recipe: {exc}") from exc
        if source_ref:
            draft.source.setdefault("ref", source_ref)
        return record_import(draft, text, source_ref, "twinspark"), {
            "format": "twinspark", "mapped": {}, "dropped": [], "raw": [], "notes": []}
    draft, report = parse_eugr_recipe(stripped, profile_name, overrides=overrides, source_ref=source_ref)
    report["format"] = "eugr"
    return record_import(draft, text, source_ref, "eugr", overrides), report


def raw_url(url: str) -> str:
    """GitHub blob URLs -> raw.githubusercontent.com (what you copy from the browser)."""
    m = _GITHUB_BLOB.match(url.strip())
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return url.strip()
