"""One-click pinning: working draft -> immutable, activatable revision.

* Model: ``org/model`` (+ optional branch/sha) -> 40-char commit sha via the Hub.
  The Hub's ``config.json`` + safetensors size are registered as the planner's
  ModelSpec on the way, so the memory check before every switch is real.
* Image: a registry reference is pinned to its (multi-arch) digest; a local
  image (e.g. ``vllm-node-b12x`` built by eugr's build-and-copy.sh) is pinned
  to its Docker image ID, and that ID must be identical on every node the
  profile runs on.
* Extra models (separate drafter repos) are pinned to their commit shas too.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..resolver import (
    looks_like_registry_ref,
    resolve_hf_revision,
    resolve_image_digest,
    resolve_model_ref,
    spec_from_resolved,
    split_image_ref,
)
from ..schemas.profile import ImmutableIdentity, ProfileDraft
from .agent_client import AgentActionError

if TYPE_CHECKING:
    from .controller import Controller

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_LOCAL_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class PinError(ValueError):
    pass


async def _local_image(ctrl: "Controller", ref: str, nodes: list[str]) -> dict[str, Any]:
    """Docker image ID of a local tag/ID, identical on every node."""
    ids, info = {}, None
    for n in nodes:
        if n not in ctrl.agents:
            raise PinError(f"node {n} is not configured")
        try:
            res = await ctrl.agents[n].call("image_inspect", ref=ref, timeout=30)
        except AgentActionError as exc:
            raise PinError(f"cannot inspect image {ref!r} on node {n}: {exc.detail}") from exc
        if not res.get("present"):
            raise PinError(f"image {ref!r} is not present on node {n} — build/copy it there first "
                           f"(eugr: ./build-and-copy.sh copies to the other node)")
        ids[n] = res["id"]
        info = info or res
    if len(set(ids.values())) > 1:
        raise PinError(f"image {ref!r} differs between nodes: {ids} — copy the same build to both")
    name = split_image_ref(ref)[0] if not _LOCAL_ID.match(ref) else (
        (info.get("repo_tags") or ["local"])[0].split(":")[0])
    return {"image": name or "local", "digest": next(iter(ids.values())), "source": "local",
            "versions": info.get("versions") or {}, "labels": info.get("labels") or {}}


async def _registry_image(ctrl: "Controller", ref: str, nodes: list[str]) -> dict[str, Any]:
    pinned = await asyncio.to_thread(resolve_image_digest, ref)
    name, _, digest = pinned.partition("@")
    versions: dict = {}
    # if the image is already on node A, read its versions for the revision record
    first = next((n for n in nodes if n in ctrl.agents), None)
    if first:
        try:
            res = await ctrl.agents[first].call("image_inspect", ref=pinned, timeout=30)
            if res.get("present"):
                versions = res.get("versions") or {}
        except AgentActionError:
            pass
    return {"image": name, "digest": digest, "source": "registry", "versions": versions}


async def _pin_draft(ctrl: "Controller", draft: ProfileDraft, model_ref=None,
                     image=None, local_image=None, nodes=None) -> tuple[ProfileDraft, dict, list[str]]:
    repo = draft.simple.model
    notes: list[str] = []
    # ---- model -----------------------------------------------------------------
    token = ctrl.hf_token
    if model_ref:
        ref = model_ref if "/" in model_ref.split("@")[0] else f"{repo}@{model_ref}"
    elif draft.identity and draft.identity.model_repo == repo:
        ref = f"{repo}@{draft.identity.model_revision}"
    else:
        ref = f"{repo}@main"
    r_repo, _, branch = ref.partition("@")
    if r_repo != repo:
        raise PinError(f"model_ref {r_repo!r} does not match the profile's model {repo!r} — "
                       f"edit the draft to change models")
    spec_registered = ctrl.model_spec(repo) is not None
    if _SHA40.match(branch or "") and spec_registered:
        sha = branch
    else:
        try:
            resolved = await asyncio.to_thread(resolve_hf_revision, ref, token)
            sha = resolved["revision"]
            try:
                spec = spec_from_resolved(resolved)
                kvb = draft.source.get("kv_bytes_per_token") if draft.source else None
                if kvb:
                    spec.kv_bytes_per_token = float(kvb)
                ctrl.set_model_spec(repo, spec)
                notes.append(f"memory spec registered ({(spec.weight_bytes or 0) / 1024**3:.1f} GiB weights)")
            except ValueError as exc:
                notes.append(f"memory spec not registered: {exc}")
        except ValueError as exc:
            if _SHA40.match(branch or ""):
                sha = branch
                notes.append(f"offline pin (Hub not reachable: {exc}); memory check skipped")
            else:
                raise PinError(str(exc)) from exc
    # ---- image -----------------------------------------------------------------
    nodes = nodes or (["A", "B"] if draft.simple.topology.value not in ("single-a", "single-b") else \
        (["A"] if draft.simple.topology.value == "single-a" else ["B"]))
    nodes = [n for n in nodes if n in ctrl.config.nodes] or nodes
    img: Optional[dict[str, Any]] = None
    if local_image:
        img = await _local_image(ctrl, local_image, nodes)
    elif image:
        img = await (_registry_image(ctrl, image, nodes) if looks_like_registry_ref(image)
                     else _local_image(ctrl, image, nodes))
    elif draft.image_hint:
        hint = draft.image_hint
        if looks_like_registry_ref(hint):
            try:
                img = await _registry_image(ctrl, hint, nodes)
            except ValueError as exc:
                notes.append(f"registry lookup of {hint} failed ({exc}); trying the local image")
                img = await _local_image(ctrl, hint, nodes)
        else:
            img = await _local_image(ctrl, hint, nodes)
    elif draft.identity is not None:
        img = {"image": draft.identity.image, "digest": draft.identity.image_digest,
               "source": draft.identity.image_source,
               "versions": {"VLLM_VERSION": draft.identity.vllm_version}}
        notes.append("kept the previously pinned image")
    else:
        raise PinError("no image to pin — pass an image (registry ref) or a local image tag")
    versions = img.get("versions") or {}
    # ---- extra models ------------------------------------------------------------
    extras = []
    for ref_x in draft.advanced.extra_models:
        x_repo, x_sha = await asyncio.to_thread(resolve_model_ref, ref_x, token)
        extras.append(f"{x_repo}@{x_sha}")
    adv_update: dict[str, Any] = {}
    if extras != draft.advanced.extra_models:
        adv_update["extra_models"] = extras
    spec_cfg = dict(draft.advanced.speculative_config or {})
    for x in extras:
        x_repo, _, x_sha = x.partition("@")
        if spec_cfg.get("model") == x_repo and spec_cfg.get("revision") != x_sha:
            # weights are staged offline: the drafter must be loaded at exactly this commit
            spec_cfg["revision"] = x_sha
            adv_update["speculative_config"] = spec_cfg
            notes.append(f"speculative drafter pinned to {x_repo}@{x_sha[:8]}")
    if adv_update:
        draft = draft.model_copy(update={"advanced": draft.advanced.model_copy(update=adv_update)})
    identity = ImmutableIdentity(
        model_repo=repo, model_revision=sha, quantization=draft.simple.quantization,
        image=img["image"], image_digest=img["digest"], image_source=img["source"],
        vllm_version=str(versions.get("VLLM_VERSION") or img.get("labels", {}).get(
            "org.opencontainers.image.version") or "unknown"),
        cuda_version=str(versions.get("CUDA_VERSION") or "unknown"),
        pytorch_version=str(versions.get("PYTORCH_VERSION") or "unknown"),
    )
    pinned = draft.model_copy(update={"identity": identity}, deep=True)
    return pinned, {"model": f"{repo}@{sha}", "image": identity.image_ref,
                    "image_source": identity.image_source, "extra_models": extras}, notes


async def pin_profile(ctrl: "Controller", name: str, model_ref: Optional[str] = None,
                      image: Optional[str] = None, local_image: Optional[str] = None,
                      label_note: Optional[str] = None, secondary: Optional[dict] = None,
                      cancel_check: Callable[[], bool] = lambda: False,
                      progress: Callable[[str], None] = lambda message: None) -> dict[str, Any]:
    p = ctrl.get_profile(name)
    if p is None:
        raise PinError(f"unknown profile: {name}")
    draft = p.working_draft()
    if draft is None:
        raise PinError(f"profile {name} has no draft to pin")
    original = draft.model_dump(mode="json")
    if cancel_check():
        raise PinError("cancelled by user")
    progress(f"Resolving model and image for {'node A' if draft.secondary else draft.simple.topology.value}")
    pinned, resolved, notes = await _pin_draft(
        ctrl, draft, model_ref, image, local_image, nodes=["A"] if draft.secondary else None)
    if cancel_check():
        raise PinError("cancelled by user")
    if draft.secondary:
        progress("Resolving model and image for node B")
        pinned_b, result_b, notes_b = await _pin_draft(ctrl, draft.secondary, nodes=["B"], **(secondary or {}))
        pinned = pinned.model_copy(update={"secondary": pinned_b})
        resolved["secondary"] = result_b
        notes.extend(f"node B: {note}" for note in notes_b)
    elif secondary:
        raise PinError("node B pin overrides require a split profile")
    if cancel_check():
        raise PinError("cancelled by user")
    # Hub/image lookups may take time; do not overwrite edits made during them.
    current = ctrl.get_profile(name)
    if current is None or current.working_draft().model_dump(mode="json") != original:
        raise PinError("profile changed while pinning — retry with the current draft")
    p = current
    if p.latest() and p.latest().draft == pinned:
        rev = p.latest()
        notes.append("nothing changed — latest revision already has these pins")
    else:
        rev = p.add_revision(pinned, pinned.identity)
        ctrl.store.save_profile(p)
        ctrl._audit("user", "profile.pin", f"profile/{name}",
                    {"revision": rev.label, "resolved": resolved, "note": label_note})
    return {"revision": rev.model_dump(mode="json"), "notes": notes, "resolved": resolved}
