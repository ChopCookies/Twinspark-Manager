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

**Modes** (master plan §4.2): a recipe can offer variants of itself, e.g. *latency* (a speculative drafter)
and *throughput* (no drafter, a larger batch), each with its own measured results. A JSON recipe (or the
``.meta.json`` of a YAML recipe) carries ``"modes": {name: {title, description, patch, measured}}`` and
``"default_mode"``; ``patch`` is merged into the draft (``null`` removes a setting).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .. import __version__
from ..schemas.enums import VerificationStatus
from ..schemas.profile import ProfileDraft
from . import catalog
from .eugr import RecipeImportError, parse_eugr_recipe, slugify
from .tracking import record_import

RECIPES_DIR = Path(__file__).resolve().parent / "recipes"
_GITHUB_BLOB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$")

__all__ = ["RECIPES_DIR", "RecipeImportError", "build_draft", "import_eugr", "import_text",
           "list_recipes", "load_recipe", "make_catalog", "raw_url", "recipe_detail", "slugify"]


def _slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _package_files() -> dict[str, list[Path]]:
    return catalog.recipe_files(RECIPES_DIR)


def _files() -> dict[str, Path]:
    """Recipe name → file: the package's recipes, with the catalog's new and newer ones over them."""
    out = {name: files[0] for name, files in _package_files().items()}
    cat = catalog.active()
    if cat is not None:
        out.update(cat.overlay()[0])
    return dict(sorted(out.items()))


def make_catalog(cache_dir: Path | str, url: Optional[str] = catalog.DEFAULT_URL, *,
                 refresh_hours: float = 24.0) -> catalog.Catalog:
    """A :class:`~twinspark.cookbook.catalog.Catalog` that knows this package's own recipes."""
    return catalog.Catalog(cache_dir, url, refresh_hours=refresh_hours, package_files=_package_files)


_MODE_PATCH_KEYS = {"simple", "behaviour", "advanced", "description", "image_hint", "engine", "tags"}
_MODE_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def recipe_modes(path: Path) -> tuple[dict[str, dict], Optional[str]]:
    """``(modes, default_mode)`` of a built-in recipe ({} when it has none)."""
    doc: Any = None
    if path.suffix == ".json":
        doc = json.loads(path.read_text(encoding="utf-8"))
    elif path.with_suffix(".meta.json").exists():
        doc = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    modes = (doc or {}).get("modes") if isinstance(doc, dict) else None
    if not isinstance(modes, dict) or not modes:
        return {}, None
    for name, m in modes.items():
        if not _MODE_RE.match(str(name)) or not isinstance(m, dict) or not isinstance(m.get("patch", {}), dict) \
                or set(m.get("patch") or {}) - _MODE_PATCH_KEYS:
            raise RecipeImportError(f"{path.name}: mode {name!r} is malformed (patch may change "
                                    f"{', '.join(sorted(_MODE_PATCH_KEYS))})")
    default = doc.get("default_mode") if doc.get("default_mode") in modes else next(iter(modes))
    return modes, default


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _with_mode(draft: ProfileDraft, path: Path, mode: Optional[str]) -> ProfileDraft:
    modes, default = recipe_modes(path)
    if not modes:
        if mode:
            raise RecipeImportError(f"recipe {path.stem} has no modes")
        return draft
    mode = mode or default
    if mode not in modes:
        raise RecipeImportError(f"recipe {path.stem} has no mode {mode!r} (it has: {', '.join(modes)})")
    m = modes[mode]
    doc = _merge(draft.model_dump(mode="json"), m.get("patch") or {})
    src = dict(doc.get("source") or {})
    src["mode"] = mode
    src["mode_title"] = m.get("title") or mode
    # each mode carries its own measurements; an unmeasured mode shows none instead of another mode's
    src["measured"] = m.get("measured") or {}
    doc["source"] = src
    try:
        return ProfileDraft.model_validate(doc)
    except ValueError as exc:
        raise RecipeImportError(f"{path.stem} mode {mode}: {exc}") from exc


def _strip_modes(text: str) -> str:
    doc = json.loads(text)
    if isinstance(doc, dict) and ("modes" in doc or "default_mode" in doc):
        doc = {k: v for k, v in doc.items() if k not in ("modes", "default_mode")}
        return json.dumps(doc)
    return text


def _draft_from_file(path: Path, profile_name: Optional[str] = None,
                     mode: Optional[str] = None) -> tuple[ProfileDraft, dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        draft, report = import_text(_strip_modes(text), profile_name, source_ref=f"builtin:{path.name}")
        draft.source["recipe"] = path.stem
        draft = _with_mode(draft, path, mode)
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
    update["source"] = {k: v for k, v in {**extra, "recipe": path.stem}.items() if k not in ("modes", "default_mode")}
    draft = _with_mode(draft.model_copy(update=update), path, mode)
    return record_import(draft, text, f"builtin:{path.name}", "eugr", metadata=metadata), report


def list_recipes() -> list[dict[str, Any]]:
    """The recipe index with provenance for the GUI/CLI. ``origin`` says where a recipe comes from (this
    TwinSpark's package or the catalog) and ``catalog`` whether the catalog made it ``new``/``updated``."""
    out = []
    cat = catalog.active()
    notes = cat.overlay()[1] if cat is not None else {}
    files = _files()
    for stem, path in files.items():
        note = notes.get(stem) or {}
        origin: dict[str, Any] = {"origin": "catalog" if note.get("origin") == "catalog" else "package",
                                  "catalog": note.get("catalog")}
        if note.get("needs"):
            origin["catalog_note"] = (f"a newer version of this recipe needs TwinSpark {note['needs']} "
                                      f"(this is {__version__})")
        try:
            draft, _ = _draft_from_file(path)
        except (ValueError, RecipeImportError) as exc:
            out.append({"name": stem, "error": str(exc), **origin})
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
            "modes": [{"name": n, "title": m.get("title") or n, "description": m.get("description") or "",
                       "measured": m.get("measured") or {}} for n, m in recipe_modes(path)[0].items()],
            "default_mode": recipe_modes(path)[1],
            "requirements": src.get("requirements") or [],
            "notes": src.get("notes") or [],
            "source": "cookbook",
            **origin,
        })
    for stem, note in sorted(notes.items()):
        if note.get("needs") and stem not in files:
            out.append({"name": stem, "title": note.get("title") or stem, "model": note.get("model"),
                        "origin": "catalog", "catalog": "new", "needs_twinspark": note["needs"],
                        "error": f"needs TwinSpark {note['needs']} or newer (this is {__version__}) — "
                                 "update TwinSpark to import it"})
    return out


def recipe_detail(name: str, mode: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Index entry + the full draft (of ``mode``, else the default mode) + the importer report."""
    path = _files().get(_slug(name))
    if path is None:
        return None
    draft, report = _draft_from_file(path, mode=mode)
    entry = next((r for r in list_recipes() if r["name"] == path.stem), {"name": path.stem})
    src = draft.source or {}
    return {**entry, "mode": src.get("mode"), "measured": src.get("measured") or {},
            "draft": draft.model_dump(mode="json"), "report": report,
            "file": path.name, "text": path.read_text(encoding="utf-8")}


def load_recipe(name: str) -> Optional[dict]:
    """Raw recipe document by slug (JSON recipes only). Returns None if unknown."""
    path = RECIPES_DIR / f"{_slug(name)}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_draft(name: str, profile_name: Optional[str] = None, mode: Optional[str] = None) -> Optional[ProfileDraft]:
    """Turn a built-in recipe into a validated :class:`ProfileDraft` (no identity), in ``mode``."""
    path = _files().get(_slug(name))
    if path is None:
        return None
    return _draft_from_file(path, profile_name, mode)[0]


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
