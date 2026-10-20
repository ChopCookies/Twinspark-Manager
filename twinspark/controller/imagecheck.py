"""Can the pinned image serve this profile? Asked before the old model is stopped (master plan §3.1).

The agent runs TwinSpark's fixed probe script in the image (agent.probe) and returns what the engine's own
registries contain. Here that answer is compared with what the launch plan uses: every engine flag, the
tool-call and reasoning parsers, the speculative method and the model's architecture. A pull that worked is
not a compatibility check; a missing parser found here costs nothing, found after the old model was
stopped it costs the deployment.

``runtime.image_check``: ``enforce`` refuses the switch, ``warn`` notes it, ``off`` skips the probe.
Profiles with mods only get warnings: a mod may add what the plain image lacks. Answers are kept per
image digest and engine (images pinned by digest do not change).
"""

from __future__ import annotations

import shlex
from typing import Any, Optional

from ..schemas.profile import flag_base

LABEL = {"vllm": "vLLM", "sglang": "SGLang"}
PROBE_MIN_FREE_GIB = 10        # the probe container is capped at 6 GiB; keep room for the serving model


def engine_argv(command: list[str]) -> list[str]:
    """The engine's own argv inside a container command (the ``exec`` line of a ``bash -c`` wrapper)."""
    if len(command) >= 3 and command[0] == "bash" and command[1] == "-c":
        lines = [ln.strip() for ln in command[2].splitlines() if ln.strip().startswith("exec ")]
        try:
            return shlex.split(lines[-1][5:]) if lines else []
        except ValueError:
            return []
    return list(command)


def used_flags(argv: list[str]) -> list[str]:
    return sorted({flag_base(t) for t in argv if t.startswith("--") and len(t) > 2})


def requirements(part: Any, argv: list[str], spec: Any) -> dict[str, Any]:
    d = part.draft
    s, b, a = d.simple, d.behaviour, d.advanced
    method = None
    if a.speculative_config:
        method = str(a.speculative_config.get("method") or "") or None
        if d.engine == "sglang" and method:
            from .launch import LaunchPlanner
            method = LaunchPlanner._SGLANG_SPEC.get(method.lower(), method)
    return {"flags": used_flags(argv),
            "tool_parser": b.tool_call_parser if s.tool_calling else None,
            "reasoning_parser": b.reasoning_parser,
            "speculative_method": method,
            "architectures": list(getattr(spec, "architectures", ()) or ())}


def compare(caps: dict[str, Any], req: dict[str, Any]) -> list[str]:
    """What the profile needs and the image lacks; only things the probe could answer are judged."""
    problems: list[str] = []
    flags = set(caps.get("flags") or [])
    if flags:
        missing = [f for f in req["flags"] if f not in flags and not (f.startswith("no-") and f[3:] in flags)]
        if missing:
            problems.append("it does not know " + ", ".join(f"--{f}" for f in missing[:8])
                            + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""))
    for key, what in (("tool_parser", "tool-call parser"), ("reasoning_parser", "reasoning parser")):
        have = caps.get(key + "s")
        if req.get(key) and have and req[key] not in have:
            problems.append(f"it has no {what} {req[key]!r} (it has {', '.join(have[:12])}"
                            + (", …" if len(have) > 12 else "") + ")")
    methods = caps.get("speculative_methods")
    if req.get("speculative_method") and methods and \
            req["speculative_method"].lower() not in {m.lower() for m in methods}:
        problems.append(f"it does not support the speculative method {req['speculative_method']!r} "
                        f"(it has {', '.join(methods[:12])})")
    archs = set(caps.get("architectures") or [])
    if archs and req.get("architectures") and not set(req["architectures"]) & archs:
        problems.append(f"it cannot load the architecture {', '.join(req['architectures'])}")
    return problems


async def check_plan(coord: Any, job: Any, rev: Any, step: Any) -> list[str]:
    """Probe every (node, image, engine) of the plan and compare. Raises when the policy is enforce and the
    image cannot serve the profile; returns the warnings for the job otherwise."""
    from .halves import halves
    policy = coord.config.runtime.image_check
    if policy == "off" or coord.dry_run:
        return []
    parts = halves(rev) or {n: rev for n in rev.required_nodes()}
    warnings: list[str] = []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for c in coord.plan.containers:
        part = parts.get(c.node, rev)
        engine = part.draft.engine
        if (c.image_ref, engine) in seen:
            continue
        seen.add((c.image_ref, engine))
        caps = await _caps(coord, c.node, c.image_ref, engine, step, job)
        name = f"{c.image_ref.rsplit('@', 1)[0]} ({LABEL.get(engine, engine)} {caps.get('version') or '?'})"
        if caps.get("error") or caps.get("simulated"):
            if caps.get("error"):
                warnings.append(f"could not check {name} before switching: {caps['error'][:200]}")
            continue
        spec = coord.model_specs.get(part.identity.model_repo) or (None if rev.draft.secondary else coord.model_spec)
        found = compare(caps, requirements(part, engine_argv(c.command), spec))
        if not found:
            continue
        text = f"image {name} cannot serve {part.profile_name}: " + "; ".join(found)
        if part.draft.advanced.mods or policy == "warn":
            warnings.append("WARNING: " + text + (" (its mods may add this)" if part.draft.advanced.mods else ""))
        else:
            problems.append(text)
    if problems:
        raise RuntimeError(" | ".join(problems) + " — nothing was stopped. Pin an image that has it, change the "
                           "profile, or set runtime.image_check: warn to switch anyway.")
    return warnings


async def _caps(coord: Any, node: str, image_ref: str, engine: str, step: Any, job: Any) -> dict[str, Any]:
    key = f"image_caps:{engine}:{image_ref}"
    cached: Optional[dict] = coord.store.kv_get(key) if coord.store is not None else None
    if cached:
        return cached
    try:
        # the old model is still serving: importing the engine takes a few GiB of the shared memory
        free = (await coord.agent(node).call("memory_telemetry")).get("mem_available_gib")
    except Exception:  # noqa: BLE001 - unknown: try anyway, the probe container is memory-capped
        free = None
    if isinstance(free, (int, float)) and free < PROBE_MIN_FREE_GIB:
        return {"error": f"skipped: only {free:.1f} GiB free on node {node} next to the running model "
                         f"(the probe needs {PROBE_MIN_FREE_GIB} GiB)"}
    step.message = f"node {node}: asking the image what it can serve ({LABEL.get(engine, engine)})"
    coord.persist(job)
    try:
        caps = await coord.agent(node).call("image_probe", image_ref=image_ref, engine=engine, timeout=300)
    except Exception as exc:  # noqa: BLE001 - an older agent or a broken probe: noted, never fatal
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    if coord.store is not None and not caps.get("error") and not caps.get("simulated"):
        coord.store.kv_set(key, caps)
    return caps
