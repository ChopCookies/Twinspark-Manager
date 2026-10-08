"""The "Get started" checklist: what is done, what is next, and the exact way to do it.

Cheap on purpose (one short ``hardware_facts`` call per node plus local state) so the GUI can
poll it. Heavier checks stay in ``tsm doctor``. Every step carries a ``fix`` that is either a
link into the GUI or a command to run, never prose alone.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .controller import Controller


def _step(sid: str, title: str, status: str, detail: str, *, href: str = "", command: str = "",
          required: bool = True, label: str = "") -> dict[str, Any]:
    return {"id": sid, "title": title, "status": status, "detail": detail, "required": required,
            "fix": {"href": href, "command": command, "label": label} if (href or command) else None}


async def _facts(ctrl: "Controller") -> dict[str, dict[str, Any]]:
    async def one(n: str, agent) -> tuple[str, dict[str, Any]]:
        try:
            f = await agent.call("hardware_facts", timeout=6)
            ctrl.store.kv_set(f"hardware:{n}", f)
            return n, f
        except Exception as exc:  # noqa: BLE001 - unreachable is a result, not an error
            return n, {"error": str(exc)}

    return dict(await asyncio.gather(*(one(n, a) for n, a in ctrl.agents.items())))


def _url_host(url: Any) -> str:
    from urllib.parse import urlsplit
    try:
        return urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""


def _first_error(result: dict[str, Any]) -> str:
    return str(next(iter(result.get("errors") or []), "no bandwidth reading"))[:200]


def _link_measured(ctrl: "Controller") -> dict[str, Any]:
    """Configured is not measured: show the last real link test, or say there is none."""
    import time
    last = {m: ctrl.store.kv_get(f"linktest:{m}") for m in ("tcp", "rdma")}
    real = {m: r for m, r in last.items() if r and not r.get("simulated")}
    good = {m: r for m, r in real.items() if r.get("ok")}

    def describe(m: str, r: dict[str, Any]) -> str:
        age = time.time() - (r.get("at") or 0)
        when = f"{int(age // 3600)} h ago" if age >= 3600 else f"{max(1, int(age // 60))} min ago"
        if m == "tcp":
            return f"TCP {r.get('bandwidth_gbps')} Gb/s ({r.get('streams')} streams, {when})"
        devs = ", ".join(f"{h['hca']} {h['gbps']}" for h in r.get("per_hca") or [] if h.get("gbps") is not None)
        return f"RDMA {r.get('bandwidth_gbps')} Gb/s ({devs}; {when})"
    if good:
        return _step("link-test", "The link is measured, not only configured", "done",
                     "; ".join(describe(m, r) for m, r in good.items()) +
                     ("" if "rdma" in good else
                      " — the last RDMA test (what NCCL uses) failed: " + _first_error(real["rdma"])
                      if "rdma" in real else " — RDMA (what NCCL uses) not measured yet"),
                     href="#/diagnostics/link", label="Test again", required=False)
    if real:
        err = next((_first_error(r) for r in real.values() if r.get("errors")), "no bandwidth reading")
        return _step("link-test", "The link is measured, not only configured", "warn",
                     f"the last test failed: {err}", href="#/diagnostics/link", label="Test again",
                     required=False)
    return _step("link-test", "The link is measured, not only configured", "todo",
                 "only simulated (dry-run) results so far" if any(last.values()) else "not tested yet",
                 href="#/diagnostics/link", label="Run a link test", required=False)


async def build(ctrl: "Controller") -> dict[str, Any]:
    facts = await _facts(ctrl)
    steps: list[dict[str, Any]] = []
    nodes = sorted(ctrl.config.nodes)

    down = [n for n in nodes if "error" in facts.get(n, {"error": "no agent"})]
    if down:
        other = "B" if "B" in down else down[0]
        steps.append(_step(
            "nodes", "Both Sparks are connected", "blocked",
            "; ".join(f"node {n}: {facts.get(n, {}).get('error', 'not configured')[:120]}" for n in down),
            command=("sudo tsm join-code   (on node A, prints the command for node B)"
                     if other == "B" else "sudo systemctl status twinspark-agent"),
            label=f"Fix node {other}"))
    else:
        versions = {facts[n].get("agent_version") for n in nodes}
        steps.append(_step("nodes", "Both Sparks are connected", "done" if len(versions) == 1 else "warn",
                           f"{' and '.join(nodes)} reachable" + (
                               "" if len(versions) == 1
                               else f" — different TwinSpark versions: {sorted(map(str, versions))}")))

    cfg = ctrl.config.nodes
    missing_rdma = [n for n in nodes if len(nodes) > 1 and not cfg[n].rdma_hcas]
    if len(nodes) < 2:
        pass
    elif missing_rdma:
        steps.append(_step(
            "link", "QSFP link is configured (addresses + RoCE devices)", "todo",
            f"nodes.{'/'.join(missing_rdma)}.rdma_hcas is empty — NCCL would fall back to TCP sockets",
            command="sudo tsm rdma --apply && sudo systemctl restart twinspark-controller", label="Discover RDMA"))
    else:
        b_over_link = "B" in cfg and bool(cfg["B"].qsfp_ip) and _url_host(cfg["B"].agent_url) == cfg["B"].qsfp_ip
        live = "B" not in down and b_over_link
        steps.append(_step("link", "QSFP link is configured (addresses + RoCE devices)", "done",
                           f"RoCE {', '.join(cfg['A'].rdma_hcas)} (GID {cfg['A'].ib_gid_index})"
                           + ("; node B's agent answers over the link" if live else ""),
                           href="#/diagnostics/link", label="Run a link test"))
    if len(nodes) >= 2:
        steps.append(_link_measured(ctrl))
        halves = {n: len(cfg[n].rdma_hcas) for n in nodes}
        if all(h >= 2 for h in halves.values()):
            steps.append(_step("halves", "Both PCIe halves of the port are used", "done",
                               "two RoCE devices per node — NCCL can approach ~200 Gb/s", required=False))
        elif any(h == 1 for h in halves.values()):
            one = sorted(n for n, h in halves.items() if h == 1)
            steps.append(_step(
                "halves", "Both PCIe halves of the port are used", "warn",
                f"one RoCE device on node {', '.join(one)}: the link works, but NCCL is limited to that half "
                f"(~100 Gb/s). The second interface needs its own subnet",
                command="tsm qsfp plan   (on each node; then sudo tsm qsfp apply, tsm rdma --apply)",
                required=False, label="Plan the second half"))

    ok = {n: f for n, f in facts.items() if "error" not in f}
    dry = [n for n, f in ok.items() if f.get("runtime_mode") == "dry-run"]
    if ok:
        steps.append(_step(
            "live", "Real containers are enabled", "todo" if dry else "done",
            ("dry-run on " + ", ".join(sorted(dry)) + " — activations are simulated") if dry
            else "docker mode on every node",
            command="sudo tsm go-live    (run on each node when `tsm plan` looks right)" if dry else "",
            required=False, label="Go live"))
        desk = [n for n, f in ok.items() if f.get("desktop_running")]
        steps.append(_step(
            "headless", "Desktop is off (memory goes to the model)", "todo" if desk else "done",
            "graphical session running on " + ", ".join(sorted(desk)) if desk else "headless",
            command="tsm headless status   (check the remote-access line first), then tsm headless headless-max"
            if desk else "", required=False, label="Go headless"))
        nopriv = [n for n, f in ok.items() if not f.get("privd_available")]
        steps.append(_step(
            "privd", "Privileged helper is running (page-cache drop, power, headless)", "warn" if nopriv else "done",
            "tsm-privd missing on " + ", ".join(sorted(nopriv)) if nopriv else "reachable on every node",
            command="sudo systemctl enable --now twinspark-privd" if nopriv else "", required=False,
            label="Start helper"))
        foreign = [n for n, f in ok.items() if f.get("foreign_inference")]
        if foreign:
            steps.append(_step(
                "foreign", "No hand-started vLLM is holding GPU memory", "todo",
                "inference containers outside TwinSpark on " + ", ".join(sorted(foreign)),
                href="#/diagnostics/doctor", label="Review", required=False))
    if not ctrl.hf_token:
        steps.append(_step("hf", "Hugging Face token (only for gated models)", "optional",
                           "not set", command="sudo tsm init --hf-token hf_…  (on both nodes)", required=False))

    profiles = ctrl.list_profiles()
    steps.append(_step("recipe", "Import a recipe", "done" if profiles else "todo",
                       f"{len(profiles)} profile(s)" if profiles else "start from a built-in or a community recipe",
                       href="#/cookbook", label="Open Cookbook"))
    pinned = [p for p in profiles if p.revisions]
    steps.append(_step("pin", "Pin it (exact model commit + image)", "done" if pinned else "todo",
                       f"{len(pinned)} pinned" if pinned else (
                           "pinning makes a revision activatable and reproducible" if profiles
                           else "after you import a recipe"),
                       href="#/profiles" if profiles else "", label="Open Profiles"))
    good = any(r.known_good for p in profiles for r in p.revisions)
    act = ctrl.active()
    steps.append(_step("activate", "Activate and send a test request",
                       "done" if (good or act) else "todo",
                       f"active: {act['profile']}" if act else (
                           "a previous activation succeeded" if good
                           else "switch to a pinned profile" if pinned else "after you pin a profile"),
                       command="tsm plan <profile> && tsm activate <profile>" if pinned else "",
                       href="#/profiles" if pinned else "", label="Open Profiles"))
    req = [s for s in steps if s["required"]]
    done = sum(1 for s in req if s["status"] == "done")
    nxt: Optional[dict[str, Any]] = next((s for s in steps if s["required"] and s["status"] != "done"), None)
    return {"steps": steps, "done": done, "total": len(req), "complete": done == len(req),
            "next": nxt["id"] if nxt else None,
            "dry_run": bool(dry) if ok else None, "demo": bool(getattr(ctrl.config, "demo", False))}
