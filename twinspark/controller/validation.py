"""A validation record: what was tested, with what, and what came out (master plan §1.4).

One JSON document per (profile revision, activation job) that a third party can use to reconstruct the
tested combination: the manager version and source commit, both agents, the model commit, the image
digest, the mods' hashes, topology, context, KV dtype and speculative settings, the procedure (stages,
timings, smoke test) and the results (KV pool, startup footprint, benchmarks, evaluations).

Evidence classes keep "the recipe works" apart from "this manager version ran it":

* ``manager-validated`` — activated, served and smoke-tested by TwinSpark on real hardware
* ``dry-run`` — walked through by TwinSpark with simulated containers: proves the plan, not the model
* ``external`` — the recipe's own notes say it ran elsewhere (e.g. its author's launcher); TwinSpark has not
* ``none`` — nothing has run yet
"""

from __future__ import annotations

import datetime as _dt
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .. import __version__

if TYPE_CHECKING:
    from .controller import Controller

FORMAT = "twinspark-validation/1"
EVIDENCE = {
    "manager-validated": "activated, served and smoke-tested by TwinSpark on real hardware",
    "dry-run": "walked through by TwinSpark with simulated containers: proves the plan, not the model",
    "external": "the recipe ran outside TwinSpark (its notes say so); this manager version has not run it",
    "none": "not run yet",
}
_NODE_FACTS = ("agent_version", "runtime_mode", "kernel", "arch", "driver_version", "cuda_version", "gpu_name",
               "mem_total_gib", "rdma_active")


def source_commit() -> Optional[str]:
    """The git commit the installer installed from (written next to the venv), if known."""
    for path in (Path(sys.prefix).parent / "source-commit", Path(__file__).resolve().parents[2] / ".git" / "HEAD"):
        try:
            text = path.read_text().strip()
        except OSError:
            continue
        if text.startswith("ref: "):
            ref = path.parent / text[5:]
            try:
                text = ref.read_text().strip()
            except OSError:
                continue
        if len(text.split()[0]) >= 7:
            return text.split()[0]
    return None


_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")
_HOME = re.compile(r"/home/[^/\s]+")


def _scrub(arg: str) -> str:
    """No addresses or user names in a record that is meant to be published."""
    return _HOME.sub("/home/<user>", _IPV6.sub("<address>", _IPV4.sub("<address>", str(arg))))


def _stages(job) -> list[dict[str, Any]]:
    out = []
    for st in job.steps:
        dur = None
        if st.started_at and st.finished_at:
            try:
                dur = round((_dt.datetime.fromisoformat(st.finished_at) - _dt.datetime.fromisoformat(st.started_at))
                            .total_seconds(), 1)
            except (TypeError, ValueError):
                dur = None
        out.append({"stage": st.stage, "status": st.status, "seconds": dur,
                    "message": _scrub((st.message or "")[:300])})
    return out


def _jobs_for(ctrl: "Controller", revision_id: str, kinds: tuple[str, ...], limit: int = 500) -> list:
    return [j for j in ctrl.store.list_jobs(limit=limit) if j.profile_revision == revision_id and j.kind in kinds]


def build_record(ctrl: "Controller", name: str, ref: Optional[str] = None, job_id: Optional[str] = None) -> dict:
    p = ctrl.get_profile(name)
    if p is None:
        raise ValueError(f"unknown profile: {name}")
    if job_id:
        job = ctrl.store.load_job(job_id)
        if job is None or job.kind not in ("activation", "recovery", "rollback"):
            raise ValueError(f"no activation job {job_id}")
        rev = next((r for r in p.revisions if r.revision_id == job.profile_revision), None)
        if rev is None:
            raise ValueError(f"job {job_id} did not activate a revision of {name}")
    else:
        rev = p.get_revision(ref) if ref else (p.get_revision(p.pinned_revision) if p.pinned_revision else p.latest())
        if rev is None:
            raise ValueError(f"profile {name} has no pinned revision" if not ref else f"revision not found: {ref}")
        acts = _jobs_for(ctrl, rev.revision_id, ("activation", "recovery", "rollback"))
        done = [j for j in acts if j.state.value == "completed"]
        job = (done or acts or [None])[0]
    d, ident = rev.draft, rev.identity
    payload = job.payload if job is not None else {}
    hardware = payload.get("hardware") or {}
    dry = bool(payload.get("dry_run"))
    if job is not None and job.state.value == "completed":
        evidence = "dry-run" if dry else "manager-validated"
    elif str(getattr(d.verification, "value", d.verification)) in ("verified", "community") \
            or (d.source or {}).get("validated_externally"):
        evidence = "external"
    else:
        evidence = "none"
    parts = []
    for part in rev.parts():
        s, a = part.draft.simple, part.draft.advanced
        parts.append({
            "profile": part.profile_name, "topology": s.topology.value, "nodes": part.required_nodes(),
            "alias": s.api_alias, "model_repo": part.identity.model_repo,
            "model_revision": part.identity.model_revision,
            "quantization": part.identity.quantization.value, "image": part.identity.image,
            "image_digest": part.identity.image_digest, "image_source": part.identity.image_source,
            "vllm_version": part.identity.vllm_version, "context_length": s.planning_context(),
            "concurrency": a.max_num_seqs or s.concurrency, "kv_dtype": a.kv_dtype or "auto",
            "max_num_batched_tokens": a.max_num_batched_tokens, "speculative_config": a.speculative_config,
            "extra_models": a.extra_models, "distributed_backend": getattr(part.draft.distributed_backend, "value",
                                                                           part.draft.distributed_backend),
            "gpu_memory_utilization": a.gpu_memory_utilization,
        })
    plan = payload.get("launch_plan") or {}
    commands = [{"node": c.get("node"), "role": c.get("role"), "command": [_scrub(x) for x in c.get("command") or []]}
                for c in plan.get("containers") or [] if isinstance(c, dict)]
    benches = [{"job_id": j.job_id, "at": j.created_at, **{k: j.payload.get(k) for k in ("preset", "summary", "valid")}}
               for j in _jobs_for(ctrl, rev.revision_id, ("benchmark",))]
    evals = [{"job_id": j.job_id, "evidence": j.payload.get("evidence"), "metrics": j.payload.get("metrics"),
              "source": j.payload.get("source")} for j in _jobs_for(ctrl, rev.revision_id, ("evaluation",))]
    startup = (ctrl.store.kv_get(f"observed_startup:{rev.revision_id}") or {}).get("nodes")
    return {
        "format": FORMAT,
        "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "evidence": {"class": evidence, "meaning": EVIDENCE[evidence],
                     "recipe_verification": str(getattr(d.verification, "value", d.verification))},
        "manager": {"version": __version__, "source_commit": source_commit(),
                    "api": 2},
        "nodes": {n: {k: f.get(k) for k in _NODE_FACTS if k in f} for n, f in hardware.items() if isinstance(f, dict)},
        "revision": {"profile": rev.profile_name, "revision_id": rev.revision_id, "label": rev.label,
                     "created_at": rev.created_at, "known_good": rev.known_good,
                     "recipe": (d.source or {}).get("recipe"), "recipe_ref": ((d.source or {}).get("receipt") or {})
                     .get("ref"), "recipe_sha256": ((d.source or {}).get("receipt") or {}).get("sha256")},
        "deployment": {"parts": parts, "mods": payload.get("mods") or {m: None for m in d.advanced.mods},
                       "commands": commands, "model_identity": ident.model_dump(mode="json")},
        "procedure": None if job is None else {
            "job_id": job.job_id, "kind": job.kind, "state": job.state.value, "dry_run": dry,
            "created_at": job.created_at, "stages": _stages(job), "error": job.error,
            "rollback": job.rollback, "warnings": payload.get("warnings") or [],
        },
        # benchmarks and evaluations are evidence of their own: they show even before an activation job
        "results": None if job is None and not benches and not evals else {
            "kv": payload.get("kv"), "models": payload.get("models"), "kv_sources": payload.get("kv_sources"),
            "startup": startup, "startup_classification": (payload.get("startup_evidence") or {}).get("classification"),
            "benchmarks": benches, "evaluations": evals,
        },
    }


def to_markdown(rec: dict) -> str:
    """The same record as a short Markdown block for docs/validation.md or an issue."""
    ev, rv, mg = rec["evidence"], rec["revision"], rec["manager"]
    lines = [f"### {rv['profile']} {rv['label']} — {ev['class']}", "",
             f"*{ev['meaning']}.* Exported {rec['exported_at']} from TwinSpark {mg['version']}"
             + (f" (commit `{mg['source_commit'][:12]}`)" if mg.get("source_commit") else "") + ".", "",
             "| | |", "|---|---|"]
    for part in rec["deployment"]["parts"]:
        lines += [f"| Model | `{part['model_repo']}@{part['model_revision'][:12]}` ({part['quantization']}) |",
                  f"| Image | `{part['image']}@{part['image_digest'][:19]}…` (vLLM {part['vllm_version']}) |",
                  f"| Topology | {part['topology']} on {', '.join(part['nodes'])} |",
                  f"| Context / concurrency | {part['context_length']:,} / {part['concurrency']} |",
                  f"| KV dtype | {part['kv_dtype']} |"]
        if part.get("speculative_config"):
            sc = part["speculative_config"]
            lines.append(f"| Speculative | {sc.get('method') or sc.get('model')} "
                         f"({sc.get('num_speculative_tokens')}) |")
    for m, hashes in (rec["deployment"].get("mods") or {}).items():
        shown = ", ".join(f"{n}: `{(h or '?')[:19]}`" for n, h in (hashes or {}).items())
        lines.append(f"| Mod {m} | {shown or 'hash unknown'} |")
    for n, f in rec["nodes"].items():
        lines.append(f"| Node {n} | agent {f.get('agent_version')}, {f.get('runtime_mode')}, driver "
                     f"{f.get('driver_version') or '?'}, kernel {f.get('kernel') or '?'} |")
    proc = rec.get("procedure")
    if proc:
        took = sum(s["seconds"] or 0 for s in proc["stages"])
        lines += ["", f"Procedure: job `{proc['job_id']}` ({proc['kind']}) {proc['state']} in {took:.0f} s"
                  + (f"; error: {proc['error']}" if proc.get("error") else "") + "."]
    res = rec.get("results") or {}
    kv = res.get("kv") or {}
    if kv.get("kv_cache_tokens"):
        lines.append(f"KV pool: {kv['kv_cache_tokens']:,} tokens"
                     + (f" in {kv['kv_memory_gib']} GiB" if kv.get("kv_memory_gib") else "") + ".")
    for b in res.get("benchmarks") or []:
        summary = b.get("summary")
        text = summary.get("text") if isinstance(summary, dict) else summary
        lines.append(f"Benchmark `{b['job_id']}` ({b.get('preset')}{'' if b.get('valid', True) else ', invalid'}, "
                     f"measured by the client): {text}")
    return "\n".join(lines) + "\n"
