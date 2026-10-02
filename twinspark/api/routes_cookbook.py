"""Cookbook routes: built-in dual-Spark recipes and community recipe import.

* Built-in recipes (``twinspark/cookbook/recipes``) — TwinSpark drafts and eugr
  YAML files with provenance, requirements and measured numbers.
* Community sources — browse eugr/spark-vllm-docker (or any GitHub recipe
  folder in ``recipe_sources``) and import a file with a preview of exactly how
  every flag was mapped, dropped or kept raw.
* Paste / URL — any eugr YAML or TwinSpark JSON.

Importing creates a profile holding a working draft. Nothing is downloaded and
the running model is never touched; pinning makes it activatable.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..controller.controller import Controller, DuplicateProfileError
from ..cookbook import RecipeImportError, build_draft, import_text, list_recipes, recipe_detail
from ..cookbook.remote import RemoteError, fetch_text, list_source
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1/cookbook", tags=["cookbook"],
                   dependencies=[Depends(require_auth)])


@router.get("")
def index(ctrl: Controller = Depends(controller_dep)) -> dict:
    existing = {p.name: (p.working_draft().source or {}).get("recipe") if p.working_draft() else None
                for p in ctrl.list_profiles()}
    recipes = list_recipes()
    for r in recipes:
        r["imported_as"] = sorted(n for n, src in existing.items() if src == r["name"])
    return {"recipes": recipes, "sources": ctrl.config.recipe_sources}


@router.get("/recipes/{name}")
def recipe(name: str):
    d = recipe_detail(name)
    if d is None:
        raise HTTPException(404, f"no such recipe: {name}")
    return d


def _create(ctrl: Controller, draft) -> dict:
    try:
        profile = ctrl.create_profile(draft)
    except DuplicateProfileError as e:
        raise HTTPException(409, str(e))
    return profile.model_dump(mode="json")


@router.post("/import/{name}")
def import_recipe(name: str, profile_name: Optional[str] = None,
                  ctrl: Controller = Depends(controller_dep)):
    try:
        draft = build_draft(name, profile_name)
    except (RecipeImportError, ValueError) as e:
        raise HTTPException(422, str(e))
    if draft is None:
        raise HTTPException(404, f"no such recipe: {name}")
    return {"ok": True, "profile": _create(ctrl, draft)}


@router.get("/community")
async def community(source: Optional[str] = None, refresh: bool = False,
                    ctrl: Controller = Depends(controller_dep)):
    """Recipe files in the configured GitHub sources (cached ~10 min)."""
    sources = [source] if source else ctrl.config.recipe_sources
    out: list[dict[str, Any]] = []
    for src in sources:
        try:
            out.append(await asyncio.to_thread(list_source, src, None, refresh))
        except RemoteError as e:
            out.append({"source": src, "error": str(e), "files": []})
    return {"sources": out}


class ImportRequest(BaseModel):
    text: Optional[str] = Field(None, max_length=512 * 1024)
    url: Optional[str] = None                         # GitHub blob/raw URL or any https URL
    profile_name: Optional[str] = None
    overrides: dict[str, Any] = Field(default_factory=dict)   # eugr template defaults (-e)
    preview: bool = False                             # convert + report, create nothing

    @model_validator(mode="after")
    def _one(self) -> "ImportRequest":
        if bool(self.text) == bool(self.url):
            raise ValueError("send exactly one of text or url")
        return self


@router.post("/import-recipe")
async def import_any(req: ImportRequest, ctrl: Controller = Depends(controller_dep)):
    """Import an eugr YAML or TwinSpark JSON recipe (pasted or by URL)."""
    text, ref = req.text, "pasted"
    if req.url:
        try:
            text = await asyncio.to_thread(fetch_text, req.url)
        except RemoteError as e:
            raise HTTPException(422, str(e))
        ref = req.url
    try:
        # parsing is CPU work on untrusted text: keep it off the event loop
        draft, report = await asyncio.to_thread(
            import_text, text or "", req.profile_name, source_ref=ref, overrides=req.overrides or None)
    except (RecipeImportError, ValueError) as e:
        raise HTTPException(422, str(e))
    out: dict[str, Any] = {"draft": draft.model_dump(mode="json"), "report": report,
                           "warnings": draft.warnings(),
                           "exists": ctrl.get_profile(draft.name) is not None}
    if req.preview:
        return {"ok": True, "preview": True, **out}
    return {"ok": True, "preview": False, "profile": _create(ctrl, draft), **out}
