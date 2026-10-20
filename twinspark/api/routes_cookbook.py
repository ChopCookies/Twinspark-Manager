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
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..controller.controller import BusyError, Controller, DuplicateProfileError
from ..controller.integration import IntegrationRequest, RetryRequest, integrate, retry
from ..cookbook import RecipeImportError, build_draft, catalog, import_text, list_recipes, recipe_detail
from ..cookbook import community as reports
from ..cookbook.remote import RemoteError, fetch_text, list_source
from ..cookbook.updates import check_updates
from ..schemas.job import Job
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1/cookbook", tags=["cookbook"],
                   dependencies=[Depends(require_auth)])


def refresh_in_background(cat: catalog.Catalog) -> None:
    """A Cookbook view refreshes an old catalog copy without waiting for it (the next view shows it)."""
    if cat.stale():
        threading.Thread(target=cat.refresh_if_stale, name="recipe-catalog", daemon=True).start()


@router.get("")
def index(ctrl: Controller = Depends(controller_dep)) -> dict:
    existing = {p.name: (p.working_draft().source or {}).get("recipe") if p.working_draft() else None
                for p in ctrl.list_profiles()}
    catalog.configure(ctrl.catalog)
    refresh_in_background(ctrl.catalog)
    recipes = list_recipes()
    for r in recipes:
        r["imported_as"] = sorted(n for n, src in existing.items() if src == r["name"])
    return {"recipes": recipes, "sources": ctrl.config.recipe_sources, "catalog": ctrl.catalog.status(),
            "community": {"enabled": bool(ctrl.config.cookbook.community_url)}}


@router.get("/catalog")
def catalog_status(ctrl: Controller = Depends(controller_dep)) -> dict:
    return ctrl.catalog.status()


@router.post("/catalog/refresh")
async def catalog_refresh(ctrl: Controller = Depends(controller_dep)) -> dict:
    """Fetch the recipe catalog now; answers what changed (added / updated / removed recipes)."""
    try:
        changes = await asyncio.to_thread(ctrl.catalog.refresh)
    except catalog.CatalogError as e:
        raise HTTPException(502, str(e)) from None
    ctrl._audit("user", "cookbook.catalog_refresh", "cookbook/catalog",
                {k: changes.get(k) for k in ("added", "updated", "removed")})
    return {"ok": True, **changes, "status": ctrl.catalog.status()}


@router.get("/community-stats")
async def stats(refresh: bool = False, ctrl: Controller = Depends(controller_dep)) -> dict:
    """Totals per recipe from the community server (``cookbook.community_url``; cached 30 min)."""
    return await asyncio.to_thread(reports.community_stats, ctrl.config.cookbook.community_url, None, refresh)


class ReportIn(BaseModel):
    report: dict[str, Any]


@router.get("/report/{profile}")
def report_preview(profile: str, outcome: Optional[str] = None, failure_stage: Optional[str] = None,
                   comment: str = "", anonymous: bool = False, ctrl: Controller = Depends(controller_dep)) -> dict:
    """The exact document ``POST /report`` would send for a profile — nothing is sent here."""
    reporter = None if anonymous else reports.reporter_id(Path(ctrl.config.db_path).parent)
    try:
        doc = reports.report_for_profile(ctrl, profile, outcome=outcome, failure_stage=failure_stage,
                                           comment=comment, reporter=reporter)
    except reports.ReportError as e:
        raise HTTPException(422, str(e)) from None
    return {"report": doc, "url": ctrl.config.cookbook.community_url,
            "enabled": bool(ctrl.config.cookbook.community_url)}


@router.post("/report")
async def report_send(req: ReportIn, ctrl: Controller = Depends(controller_dep)) -> dict:
    """Send a previewed report as it is (checked again: only the allowlisted fields can leave)."""
    url = ctrl.config.cookbook.community_url
    if not url:
        raise HTTPException(409, "community reports are off — set cookbook.community_url in controller.yaml")
    try:
        reports.check_report(req.report)
        answer = await asyncio.to_thread(reports.send_report, url, req.report)
    except reports.ReportError as e:
        raise HTTPException(422 if "not reachable" not in str(e) and "answered" not in str(e) else 502,
                            str(e)) from None
    ctrl._audit("user", "cookbook.report", f"recipe/{req.report.get('recipe')}",
                {"outcome": req.report.get("outcome"), "mode": req.report.get("mode")})
    return {"ok": True, "server": answer}


@router.get("/recipes/{name}")
def recipe(name: str, mode: Optional[str] = None):
    try:
        d = recipe_detail(name, mode)
    except RecipeImportError as e:
        raise HTTPException(422, str(e)) from None
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
def import_recipe(name: str, profile_name: Optional[str] = None, mode: Optional[str] = None,
                  ctrl: Controller = Depends(controller_dep)):
    try:
        draft = build_draft(name, profile_name, mode)
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


@router.post("/integrate", response_model=Job, status_code=202)
async def integrate_recipe(req: IntegrationRequest, ctrl: Controller = Depends(controller_dep)):
    """Import the reviewed snapshot, pin models/images, validate and stage both nodes."""
    try:
        return await integrate(ctrl, req)
    except (BusyError, DuplicateProfileError) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/integration/{job_id}/retry", response_model=Job, status_code=202)
async def retry_integration(job_id: str, req: RetryRequest, ctrl: Controller = Depends(controller_dep)):
    try:
        return await retry(ctrl, job_id, req)
    except (BusyError, DuplicateProfileError) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/updates")
async def recipe_updates(profile: Optional[str] = None, ctrl: Controller = Depends(controller_dep)):
    """Compare imported recipes with upstream; retain local edits and never overwrite a profile."""
    if profile:
        selected = ctrl.get_profile(profile)
        if selected is None:
            raise HTTPException(404, "profile not found")
        profiles = [selected]
    else:
        profiles = ctrl.list_profiles()
    return {"updates": await check_updates(ctrl, profiles)}
