"""Recipe research on the workstation: the cookbook index, community recipe fetching and conversion.

Same paths and answers as node A's ``/api/v1/cookbook`` routes, so the GUI does not care where they run.
Browsing GitHub, parsing YAML and mapping flags happen here; node A only receives the finished draft
(``POST /api/v1/profiles``) and keeps the profiles. Integrating a recipe (pin, pull, stage) and the
update comparison stay node A's job: they need its agents and its profile history, so those paths are
not defined here and the client passes them on.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException

from ..cookbook import RecipeImportError, build_draft, import_text, list_recipes, recipe_detail
from ..cookbook.remote import RemoteError, fetch_text, list_source
from .app import ImportIn  # module level: FastAPI resolves the (string) annotation from here
from .session import ClientSession


async def _node_json(session: ClientSession, method: str, path: str, **kw: Any) -> tuple[int, Any]:
    try:
        r = await session.node_request(method, path, **kw)
    except ConnectionError as exc:
        raise HTTPException(503, str(exc), headers={"X-TwinSpark-Client": "transport"}) from None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"detail": r.text[:500]}


async def _create(session: ClientSession, draft) -> dict:
    status, body = await _node_json(session, "POST", "/api/v1/profiles", json=draft.model_dump(mode="json"))
    if status == 201:
        return body
    detail = body.get("detail") if isinstance(body, dict) else body
    raise HTTPException(status if status in (409, 422) else 502, detail or f"node A answered {status}")


def add_cookbook_routes(app: FastAPI, session: ClientSession, connected: Callable[[], None]) -> None:
    @app.get("/api/v1/cookbook")
    async def index() -> dict:
        connected()
        status, profiles = await _node_json(session, "GET", "/api/v1/profiles?summary=true")
        if status != 200:
            raise HTTPException(502, f"node A did not list its profiles ({status})")
        existing = {p.get("name"): (p.get("source") or {}).get("recipe") for p in profiles if isinstance(p, dict)}
        recipes = list_recipes()
        for r in recipes:
            r["imported_as"] = sorted(n for n, src in existing.items() if src == r["name"])
        return {"recipes": recipes, "sources": session.node_cfg.get("recipe_sources")
                or ["eugr/spark-vllm-docker:recipes"], "where": "client"}

    @app.get("/api/v1/cookbook/recipes/{name}")
    def recipe(name: str):
        d = recipe_detail(name)
        if d is None:
            raise HTTPException(404, f"no such recipe: {name}")
        return d

    @app.post("/api/v1/cookbook/import/{name}")
    async def import_recipe(name: str, profile_name: Optional[str] = None):
        connected()
        try:
            draft = build_draft(name, profile_name)
        except (RecipeImportError, ValueError) as e:
            raise HTTPException(422, str(e)) from None
        if draft is None:
            raise HTTPException(404, f"no such recipe: {name}")
        return {"ok": True, "profile": await _create(session, draft)}

    @app.get("/api/v1/cookbook/community")
    async def community(source: Optional[str] = None, refresh: bool = False):
        """Recipe files in the configured GitHub sources, fetched from this computer (cached ~10 min)."""
        sources = [source] if source else (session.node_cfg.get("recipe_sources") or ["eugr/spark-vllm-docker:recipes"])
        out: list[dict[str, Any]] = []
        for src in sources:
            try:
                out.append(await asyncio.to_thread(list_source, src, None, refresh))
            except RemoteError as e:
                out.append({"source": src, "error": str(e), "files": []})
        return {"sources": out}

    @app.post("/api/v1/cookbook/import-recipe")
    async def import_any(req: ImportIn):
        if bool(req.text) == bool(req.url):
            raise HTTPException(422, "send exactly one of text or url")
        text, ref = req.text, "pasted"
        if req.url:
            try:
                text = await asyncio.to_thread(fetch_text, req.url)
            except RemoteError as e:
                raise HTTPException(422, str(e)) from None
            ref = req.url
        try:
            draft, report = await asyncio.to_thread(
                import_text, text or "", req.profile_name, source_ref=ref, overrides=req.overrides or None)
        except (RecipeImportError, ValueError) as e:
            raise HTTPException(422, str(e)) from None
        exists = False
        if session.phase == "connected":
            status, _ = await _node_json(session, "GET", f"/api/v1/profiles/{quote(draft.name, safe='')}")
            exists = status == 200
        out: dict[str, Any] = {"draft": draft.model_dump(mode="json"), "report": report,
                               "warnings": draft.warnings(), "exists": exists}
        if req.preview:
            return {"ok": True, "preview": True, **out}
        connected()
        return {"ok": True, "preview": False, "profile": await _create(session, draft), **out}
