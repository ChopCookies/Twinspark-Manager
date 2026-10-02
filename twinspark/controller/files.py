"""Model files and mods across both Sparks: inventory, staging, deletion, mods."""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING, Any, Optional

from ..resolver import resolve_model_ref
from ..schemas.enums import JobState
from ..schemas.job import Job
from .agent_client import AgentActionError
from .staging import StageError, WeightsStager

if TYPE_CHECKING:
    from .controller import Controller


class FilesError(ValueError):
    pass


class DependencyError(FilesError):
    def __init__(self, msg: str, dependents: list[str]):
        super().__init__(msg)
        self.dependents = dependents


def _profile_refs(ctrl: "Controller") -> dict[tuple[str, str], list[str]]:
    """(repo, sha) -> profile names whose revisions/drafts use it (incl. drafters)."""
    out: dict[tuple[str, str], list[str]] = {}
    for p in ctrl.list_profiles():
        seen: set[tuple[str, str]] = set()
        drafts = [r.draft for r in p.revisions] + ([p.draft] if p.draft else [])
        for d in [part for draft in drafts for part in draft.parts()]:
            if d.identity:
                seen.add((d.identity.model_repo, d.identity.model_revision))
            for x in d.advanced.extra_models:
                repo, _, sha = x.partition("@")
                if sha:
                    seen.add((repo, sha))
        for key in seen:
            out.setdefault(key, []).append(p.name)
    return out


async def inventory(ctrl: "Controller") -> dict[str, Any]:
    per_node: dict[str, Any] = {}
    for n, agent in ctrl.agents.items():
        try:
            per_node[n] = await agent.call("weights_inventory", timeout=120)
        except AgentActionError as exc:
            per_node[n] = {"error": exc.detail}
    refs = _profile_refs(ctrl)
    active = ctrl.active_revision()
    active_models = set()
    if active:
        for part in active.parts():
            active_models.add((part.identity.model_repo, part.identity.model_revision))
            for x in part.draft.advanced.extra_models:
                repo, _, sha = x.partition("@")
                active_models.add((repo, sha))
    models: dict[str, dict[str, Any]] = {}
    for n, inv in per_node.items():
        for repo in inv.get("repos", []) if isinstance(inv, dict) else []:
            m = models.setdefault(repo["repo"], {"repo": repo["repo"], "revisions": {},
                                                 "partial_bytes": {}})
            if repo.get("partial_bytes"):
                m["partial_bytes"][n] = repo["partial_bytes"]
            for rev in repo["revisions"]:
                r = m["revisions"].setdefault(rev["revision"], {
                    "revision": rev["revision"], "nodes": {}, "refs": set(),
                    "profiles": refs.get((repo["repo"], rev["revision"]), []),
                    "active": (repo["repo"], rev["revision"]) in active_models})
                r["nodes"][n] = {k: rev[k] for k in ("complete", "verified", "managed",
                                                      "size_bytes", "modified")}
                r["refs"].update(rev.get("refs", []))
    # profiles whose pinned model is not on disk anywhere yet
    missing = []
    for (repo, sha), names in refs.items():
        if repo not in models or sha not in models[repo]["revisions"]:
            missing.append({"repo": repo, "revision": sha, "profiles": names})
    out_models = []
    for m in sorted(models.values(), key=lambda x: x["repo"].lower()):
        revs = []
        for r in m["revisions"].values():
            r["refs"] = sorted(r["refs"])
            r["size_bytes"] = max((v["size_bytes"] for v in r["nodes"].values()), default=0)
            revs.append(r)
        m["revisions"] = sorted(revs, key=lambda r: -max((v["modified"] for v in r["nodes"].values()),
                                                          default=0))
        out_models.append(m)
    disks = {n: {"free_bytes": inv.get("disk_free_bytes"), "total_bytes": inv.get("disk_total_bytes"),
                 "hf_home": inv.get("hf_home"), "downloads": inv.get("downloads", []),
                 "error": inv.get("error")}
             for n, inv in per_node.items() if isinstance(inv, dict)}
    return {"models": out_models, "nodes": disks, "missing_for_profiles": missing,
            "staging": [f"{r}@{s[:8]}" for (r, s) in ctrl._staging]}


async def delete_model(ctrl: "Controller", repo: str, revision: Optional[str], nodes: list[str],
                       force: bool = False, preview: bool = False) -> dict[str, Any]:
    if ctrl.busy() and not preview:
        raise FilesError("cluster is busy preparing, switching or maintaining a deployment")
    active = ctrl.active_revision()
    if active is not None:
        used = {(part.identity.model_repo, part.identity.model_revision) for part in active.parts()} | {
            tuple(x.split("@", 1)) for part in active.parts()
            for x in part.draft.advanced.extra_models if "@" in x}
        if any(r == repo and (revision is None or s == revision) for r, s in used):
            raise FilesError(f"{repo} is used by the active deployment — stop or switch first")
    if (repo, revision) in {(r, s) for (r, s) in ctrl._staging} or \
            any(r == repo for (r, _) in ctrl._staging):
        raise FilesError(f"{repo} is being staged right now")
    refs = _profile_refs(ctrl)
    dependents = sorted({n for (r, s), names in refs.items()
                         if r == repo and (revision is None or s == revision) for n in names})
    if dependents and not force and not preview:
        raise DependencyError(f"profiles depend on {repo}"
                              f"{'@' + revision[:8] if revision else ''}: {', '.join(dependents)} "
                              f"(pass force to delete anyway; they will re-download on activation)",
                              dependents)
    async def run_deletes() -> dict[str, Any]:
        out: dict[str, Any] = {}
        for n in nodes:
            if n not in ctrl.agents:
                out[n] = {"error": "node not configured"}
                continue
            try:
                out[n] = await ctrl.agents[n].call("weights_delete", repo=repo, revision=revision,
                                                   preview=preview, timeout=600)
            except AgentActionError as exc:
                out[n] = {"error": exc.detail}
        return out

    if preview:
        results = await run_deletes()
    else:
        # hold the operation lock: an activation or a prepare must not start (and read these
        # files) while they are being removed
        if ctrl._lock.locked():
            raise FilesError("cluster is busy preparing, switching or maintaining a deployment")
        async with ctrl._lock:
            results = await run_deletes()
    if not preview:
        ctrl._audit("user", "model.delete", f"model/{repo}",
                    {"revision": revision, "nodes": nodes, "force": force,
                     "freed": {n: r.get("freed_bytes") for n, r in results.items()}})
    return {"repo": repo, "revision": revision, "dependents": dependents, "results": results,
            "preview": preview}


async def stage(ctrl: "Controller", ref: str, nodes: Optional[list[str]] = None,
                include: Optional[list[str]] = None, download_node: Optional[str] = None,
                verify: Optional[bool] = None) -> Job:
    """Download (+verify) and copy a model to the nodes without touching the deployment."""
    nodes = [n for n in (nodes or sorted(ctrl.agents)) if n in ctrl.agents]
    if not nodes:
        raise FilesError("no configured nodes to stage to")
    try:
        repo, sha = await asyncio.to_thread(resolve_model_ref, ref, ctrl.hf_token)
    except ValueError as exc:
        raise FilesError(str(exc)) from exc
    key = (repo, sha)
    if ctrl.maintenance.blocking():
        raise FilesError("cluster is reserved for maintenance")
    if key in ctrl._staging:
        raise FilesError(f"{repo}@{sha[:8]} is already being staged")
    job = Job(job_id=f"stage-{secrets.token_hex(5)}", kind="stage",
              payload={"repo": repo, "revision": sha, "nodes": nodes, "ref": ref,
                       "include": include or [], "download_node": download_node})
    ctrl.store.save_job(job)
    fut = asyncio.get_running_loop().create_future()
    ctrl._staging[key] = fut

    async def run():
        step = job.begin_step("staging")
        ctrl.store.save_job(job)
        try:
            stager = WeightsStager(ctrl.config, ctrl.agents, ctrl.store.save_job,
                                   poll=min(ctrl.poll_interval, 2.0))
            msg = await stager.ensure(job, step, repo, sha, nodes, include=include,
                                      download_node=download_node or ctrl.config.download_node,
                                      verify=verify,
                                      cancel_check=lambda: job.job_id in ctrl._cancel)
            job.finish_step(step, msg)
            job.state = JobState.COMPLETED
        except (StageError, AgentActionError, Exception) as exc:  # noqa: BLE001
            job.fail_step(step, str(exc))
            job.guidance = ["check disk space and SSH between the nodes (`tsm doctor`)"] \
                if "ssh" in str(exc).lower() or "space" in str(exc).lower() else []
        finally:
            ctrl.store.save_job(job)
            ctrl._staging.pop(key, None)
            ctrl._cancel.discard(job.job_id)
            if not fut.done():
                fut.set_result(job.state.value)
        ctrl._audit("user", "model.stage", f"model/{repo}",
                    {"revision": sha, "nodes": nodes, "state": job.state.value})

    ctrl._spawn(run())
    return job


# ---- mods --------------------------------------------------------------------------------
async def mods_overview(ctrl: "Controller") -> dict[str, Any]:
    per_node = {}
    for n, agent in ctrl.agents.items():
        try:
            per_node[n] = (await agent.call("mods_list"))["mods"]
        except AgentActionError as exc:
            per_node[n] = {"error": exc.detail}
    names = sorted({m["name"] for v in per_node.values() if isinstance(v, list) for m in v})
    used: dict[str, list[str]] = {}
    for p in ctrl.list_profiles():
        d = p.working_draft()
        for m in {m for part in d.parts() for m in part.advanced.mods} if d else []:
            used.setdefault(m, []).append(p.name)
    rows = []
    for name in names:
        nodes = {}
        for n, lst in per_node.items():
            if isinstance(lst, list):
                hit = next((m for m in lst if m["name"] == name), None)
                nodes[n] = {"present": bool(hit), "hash": hit["hash"] if hit else None,
                            "summary": hit["summary"] if hit else ""}
        hashes = {v["hash"] for v in nodes.values() if v["present"]}
        rows.append({"name": name, "nodes": nodes, "consistent": len(hashes) <= 1 and all(
            v["present"] for v in nodes.values()), "profiles": used.get(name, []),
            "summary": next((v["summary"] for v in nodes.values() if v.get("summary")), "")})
    wanted = sorted(set(used) - set(names))
    return {"mods": rows, "missing": [{"name": m, "profiles": used[m]} for m in wanted],
            "errors": {n: v["error"] for n, v in per_node.items() if isinstance(v, dict)}}


async def install_mod(ctrl: "Controller", name: str, archive_b64: str,
                      nodes: Optional[list[str]] = None) -> dict[str, Any]:
    if ctrl.busy():
        raise FilesError("cluster is busy — wait before changing recipe patches")
    results = {}
    for n in nodes or sorted(ctrl.agents):
        try:
            results[n] = await ctrl.agents[n].call("mods_install", name=name,
                                                   archive_b64=archive_b64, timeout=120)
        except AgentActionError as exc:
            results[n] = {"error": exc.detail}
    hashes = {r.get("hash") for r in results.values() if "hash" in r}
    ctrl._audit("user", "mod.install", f"mod/{name}", {"nodes": list(results),
                                                       "hash": next(iter(hashes), None)})
    return {"name": name, "results": results, "consistent": len(hashes) == 1 and all(
        "hash" in r for r in results.values())}


async def remove_mod(ctrl: "Controller", name: str) -> dict[str, Any]:
    if ctrl.busy():
        raise FilesError("cluster is busy — wait before changing recipe patches")
    users = [p.name for p in ctrl.list_profiles()
             if p.working_draft() and any(name in part.advanced.mods
                                          for part in p.working_draft().parts())]
    act = ctrl.active_revision()
    if act and any(name in part.draft.advanced.mods for part in act.parts()):
        raise FilesError(f"mod {name} is used by the active deployment")
    results = {}
    for n, agent in ctrl.agents.items():
        try:
            results[n] = await agent.call("mods_remove", name=name)
        except AgentActionError as exc:
            results[n] = {"error": exc.detail}
    ctrl._audit("user", "mod.remove", f"mod/{name}", {})
    return {"name": name, "results": results, "used_by": users}
