"""Named inference keys and token usage (master plan §4.1). Loaded lazily."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..controller.controller import Controller
from ..controller.keys import public, to_csv
from .deps import controller_dep, require_auth

router = APIRouter(tags=["keys"], dependencies=[Depends(require_auth)])


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    aliases: list[str] = Field(default_factory=list, max_length=32)
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650)
    app: Optional[str] = Field(None, max_length=40)


@router.get("/api/v1/keys")
def keys(ctrl: Controller = Depends(controller_dep)):
    return [public(r) for r in ctrl.keys.records()]


@router.post("/api/v1/keys", status_code=201)
def create(req: KeyIn, ctrl: Controller = Depends(controller_dep)):
    """The new key, shown this once (only its hash is kept)."""
    try:
        token, rec = ctrl.create_key(req.name, aliases=req.aliases, expires_in_days=req.expires_in_days, app=req.app)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"key": token, **public(rec)}


@router.post("/api/v1/keys/{key_id}/revoke")
def revoke(key_id: str, ctrl: Controller = Depends(controller_dep)):
    try:
        return public(ctrl.revoke_key(key_id))
    except KeyError:
        raise HTTPException(404, "no such key") from None


@router.delete("/api/v1/keys/{key_id}")
def delete(key_id: str, ctrl: Controller = Depends(controller_dep)):
    try:
        ctrl.delete_key(key_id)
    except KeyError:
        raise HTTPException(404, "no such key") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return {"deleted": key_id}


@router.get("/api/v1/usage")
def usage(days: int = 30, format: Literal["json", "csv"] = "json", ctrl: Controller = Depends(controller_dep)):
    """Requests and tokens per day, key and alias (the gateway counts them; up to 90 days)."""
    ctrl.flush_usage()
    rows = ctrl.usage.rows(days=max(1, min(days, 90)))
    names = {r["id"]: r["name"] for r in ctrl.keys.records()}
    if format == "csv":
        return PlainTextResponse(to_csv(rows, names), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="twinspark-usage-{days}d.csv"'})
    totals: dict[str, dict[str, int]] = {}
    for r in rows:
        t = totals.setdefault(r["key_id"], {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
        for k in t:
            t[k] += r[k]
    return {"days": days, "rows": rows, "names": names, "totals": totals}
