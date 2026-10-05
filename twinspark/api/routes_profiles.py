"""Profile & revision management routes (spec §17, §18)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..controller.controller import BusyError, Controller, DuplicateProfileError
from ..controller.launch import LaunchError
from ..controller.pinning import PinError
from ..schemas.job import Job
from ..schemas.profile import ImmutableIdentity, Profile, ProfileDraft, ProfileRevision
from .deps import controller_dep, require_auth

router = APIRouter(prefix="/api/v1/profiles", tags=["profiles"],
                   dependencies=[Depends(require_auth)])

_PLACEHOLDER_SHA = "0" * 40
_PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


def _profile(ctrl: Controller, name: str) -> Profile:
    p = ctrl.get_profile(name)
    if not p:
        raise HTTPException(404, "profile not found")
    return p


def _revision(ctrl: Controller, name: str, ref: str) -> ProfileRevision:
    p = _profile(ctrl, name)
    rev = p.get_revision(ref)
    if not rev:
        if not p.revisions:
            raise HTTPException(404, f"'{name}' has no pinned revision yet: pin it first (`tsm pin {name}`), "
                                     f"or look at the unpinned draft (`tsm plan {name} --draft`)")
        raise HTTPException(404, "revision not found")
    return rev


def _summary(p: Profile, ctrl: Controller) -> dict:
    d = p.working_draft()
    last = p.latest()
    act = ctrl.active()
    return {
        "name": p.name, "description": p.description,
        "model": d.simple.model if d else None,
        "secondary_model": d.secondary.simple.model if d and d.secondary else None,
        "topology": d.simple.topology.value if d else None,
        "quantization": d.simple.quantization.value if d else None,
        "alias": d.simple.api_alias if d else None,
        "verification": d.verification.value if d else None,
        "image_hint": d.image_hint if d else None,
        "mods": list(dict.fromkeys(m for part in d.parts() for m in part.advanced.mods)) if d else [],
        "source": (d.source or {}) if d else {},
        "revisions": len(p.revisions),
        "latest": None if last is None else {
            "label": last.label, "revision_id": last.revision_id, "known_good": last.known_good,
            "model_revision": last.identity.model_revision, "image": last.identity.image_ref,
            "created_at": last.created_at},
        "pinned": last is not None,
        "draft_differs": bool(last and p.draft and p.draft.model_dump(exclude={"identity"}) !=
                              last.draft.model_dump(exclude={"identity"})),
        "active": bool(act and act.get("profile") == p.name),
        "warnings": d.warnings() if d else [],
        "updated_at": p.updated_at,
    }


@router.get("")
def list_profiles(summary: bool = False, ctrl: Controller = Depends(controller_dep)):
    ps = ctrl.list_profiles()
    if summary:
        return [_summary(p, ctrl) for p in ps]
    return [p.model_dump(mode="json") for p in ps]


@router.post("", response_model=Profile, status_code=201)
def create_profile(draft: ProfileDraft, ctrl: Controller = Depends(controller_dep)):
    try:
        return ctrl.create_profile(draft)
    except DuplicateProfileError as e:
        raise HTTPException(409, str(e))


@router.get("/{name}", response_model=Profile)
def get_profile(name: str, ctrl: Controller = Depends(controller_dep)):
    return _profile(ctrl, name)


@router.delete("/{name}")
def delete_profile(name: str, ctrl: Controller = Depends(controller_dep)):
    try:
        if not ctrl.delete_profile(name):
            raise HTTPException(404, "profile not found")
    except BusyError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@router.put("/{name}/draft", response_model=Profile)
def save_draft(name: str, draft: ProfileDraft, ctrl: Controller = Depends(controller_dep)):
    try:
        return ctrl.save_draft(name, draft)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/{name}/revisions", response_model=ProfileRevision, status_code=201)
def save_revision(name: str, draft: ProfileDraft, ctrl: Controller = Depends(controller_dep)):
    if draft.name != name:
        raise HTTPException(422, "draft name must match profile")
    try:
        return ctrl.save_revision(draft)
    except ValueError as e:
        raise HTTPException(422, str(e))


class PinRequest(BaseModel):
    model_ref: Optional[str] = None     # "main", a 40-char sha, or "org/repo@branch"
    image: Optional[str] = None         # registry ref (pinned to digest) or local tag
    local_image: Optional[str] = None   # force a local image tag / sha256 ID
    note: Optional[str] = None


class SplitRequest(BaseModel):
    name: str
    node_a: str
    node_b: str
    alias_a: str = "default"
    alias_b: str = "secondary"


@router.post("/compose/split", response_model=Profile, status_code=201)
def compose_split(req: SplitRequest, ctrl: Controller = Depends(controller_dep)):
    try:
        return ctrl.compose_split(**req.model_dump())
    except DuplicateProfileError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/{name}/pin")
async def pin(name: str, req: PinRequest, ctrl: Controller = Depends(controller_dep)):
    try:
        return await ctrl.pin_profile(name, model_ref=req.model_ref, image=req.image,
                                      local_image=req.local_image, label_note=req.note)
    except (PinError, ValueError) as e:
        raise HTTPException(422, str(e))


@router.get("/{name}/compare")
def compare(name: str, a: str, b: str, ctrl: Controller = Depends(controller_dep)):
    result = _profile(ctrl, name).diff(a, b)
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(404, str(result["error"]))
    return result


@router.post("/{name}/duplicate", response_model=Profile, status_code=201)
def duplicate(name: str, new_name: str, ctrl: Controller = Depends(controller_dep)):
    try:
        return ctrl.duplicate_profile(name, new_name)
    except DuplicateProfileError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/{name}/revisions/{ref}/pin", response_model=ProfileRevision)
def pin_revision(name: str, ref: str, ctrl: Controller = Depends(controller_dep)):
    try:
        return ctrl.set_pinned(name, ref)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/{name}/known-good", response_model=ProfileRevision)
def known_good(name: str, ctrl: Controller = Depends(controller_dep)):
    rev = ctrl.restore_last_known_good(name)
    if not rev:
        raise HTTPException(404, "no known-good revision")
    return rev


@router.get("/{name}/fit")
def fit(name: str, revision: Optional[str] = None, ctrl: Controller = Depends(controller_dep)):
    """Memory fit + estimated KV pool for the draft (or a revision), next to observed facts."""
    _profile(ctrl, name)
    try:
        return ctrl.profile_fit(name, revision)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---- transparency (spec §2.4) ------------------------------------------------
@router.get("/{name}/revisions/{ref}/effective")
def effective_config(name: str, ref: str, ctrl: Controller = Depends(controller_dep)):
    return _revision(ctrl, name, ref).show_effective_config()


def _plan_payload(plan) -> dict:
    return {"plan": plan.model_dump(mode="json"), "commands": plan.rendered_commands()}


@router.get("/{name}/revisions/{ref}/launch-plan")
def launch_plan(name: str, ref: str, ctrl: Controller = Depends(controller_dep)):
    try:
        plan = ctrl.launch_plan(_revision(ctrl, name, ref))
    except LaunchError as e:
        raise HTTPException(422, str(e))
    return _plan_payload(plan)


@router.get("/{name}/draft/launch-plan")
def draft_launch_plan(name: str, ctrl: Controller = Depends(controller_dep)):
    """Render the working draft with placeholder pins — see the commands before pinning."""
    p = _profile(ctrl, name)
    d = p.working_draft()
    if d is None:
        raise HTTPException(404, "profile has no draft")
    ident = d.identity or ImmutableIdentity(
        model_repo=d.simple.model, model_revision=_PLACEHOLDER_SHA,
        quantization=d.simple.quantization, image="unpinned", image_digest=_PLACEHOLDER_DIGEST,
        image_source="local")
    preview = d.model_copy(update={"identity": ident}, deep=True)
    if preview.secondary and not preview.secondary.identity:
        secondary = preview.secondary
        secondary.identity = ImmutableIdentity(
            model_repo=secondary.simple.model, model_revision=_PLACEHOLDER_SHA,
            quantization=secondary.simple.quantization, image="unpinned",
            image_digest=_PLACEHOLDER_DIGEST, image_source="local")
    rev = ProfileRevision(revision_id=f"{name}-draft-00000000", profile_name=name, label="draft",
                          draft=preview, identity=ident)
    try:
        plan = ctrl.launch_plan(rev)
    except LaunchError as e:
        raise HTTPException(422, str(e))
    out = _plan_payload(plan)
    out["unpinned"] = not d.fully_pinned()
    return out


# ---- activation ------------------------------------------------------------------
@router.post("/{name}/prepare", response_model=Job, status_code=202)
async def prepare(name: str, revision: str = "latest", ctrl: Controller = Depends(controller_dep)):
    from ..controller.preparation import prepare as prepare_recipe
    try:
        return await prepare_recipe(ctrl, name, revision)
    except BusyError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/{name}/activate", response_model=Job, status_code=202)
async def activate(name: str, revision: str = "latest", ctrl: Controller = Depends(controller_dep)):
    try:
        return await ctrl.activate(name, revision)
    except BusyError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e) else 422, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
