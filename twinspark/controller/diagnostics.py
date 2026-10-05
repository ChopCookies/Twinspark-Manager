"""Diagnostics across both Sparks: doctor, link tests, RDMA discovery, headless, foreign vLLM."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .. import __version__
from .agent_client import AgentActionError

if TYPE_CHECKING:
    from .controller import Controller


async def run_link_test(ctrl: "Controller", mode: str = "tcp", duration_s: float = 5.0,
                        port: int = 29511, streams: int = 4) -> dict[str, Any]:
    """Responder on B, initiator on A, over the QSFP IPs."""
    a, b = ctrl.agents.get("A"), ctrl.agents.get("B")
    if not a or not b:
        raise ValueError("link test needs both node A and node B configured")
    if mode not in ("tcp", "rdma"):
        raise ValueError("mode must be tcp or rdma")
    ep_a, ep_b = ctrl.config.nodes["A"], ctrl.config.nodes["B"]
    peer_ip = ep_b.qsfp_ip
    if not peer_ip:
        raise ValueError("nodes.B.qsfp_ip is not configured")
    common = dict(mode=mode, port=port, duration_s=duration_s, streams=streams)
    resp = await b.call("link_test", role="responder", bind=peer_ip if mode == "tcp" else None,
                        hcas=ep_b.rdma_hcas, gid_index=ep_b.ib_gid_index, **common)
    await asyncio.sleep(1.0 if mode == "rdma" else 0.5)
    init = await a.call("link_test", role="initiator", peer_ip=peer_ip,
                        hcas=ep_a.rdma_hcas, gid_index=ep_a.ib_gid_index, **common)
    timeout = duration_s + 90
    try:
        init_t = await a.wait_task(init["task_id"], timeout=timeout, poll=0.5)
    finally:
        try:
            resp_t = await b.wait_task(resp["task_id"], timeout=timeout if mode == "rdma" else 30,
                                       poll=0.5)
        except AgentActionError as exc:
            resp_t = {"result": {"error": exc.detail}}
    ctrl._audit("user", "link.test", "system", {"mode": mode, "duration_s": duration_s})
    return {"mode": mode, "nodes": {"initiator": "A", "responder": "B"},
            "initiator": init_t.get("result") or {}, "responder": resp_t.get("result") or {}}


async def rdma_report(ctrl: "Controller") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for n, agent in ctrl.agents.items():
        ep = ctrl.config.nodes[n]
        try:
            facts = await agent.call("rdma_facts", qsfp_ip=ep.qsfp_ip, timeout=30)
        except AgentActionError as exc:
            out[n] = {"error": exc.detail}
            continue
        sug = facts["suggestion"]
        cfg = {"rdma_hcas": ep.rdma_hcas, "ib_gid_index": ep.ib_gid_index}
        matches = sorted(sug["hcas"]) == sorted(ep.rdma_hcas) and sug["gid_index"] == ep.ib_gid_index
        out[n] = {**facts, "configured": cfg, "matches_config": matches,
                  "yaml": (f"rdma_hcas: [{', '.join(sug['hcas'])}]\n"
                           f"ib_gid_index: {sug['gid_index'] if sug['gid_index'] is not None else 'null'}")}
    return out


def _check(results: list, name: str, node: str, status: str, detail: str, fix: str = "") -> None:
    results.append({"check": name, "node": node, "status": status, "detail": detail, "fix": fix})


async def doctor(ctrl: "Controller") -> dict[str, Any]:
    """Every precondition for a fast, stable dual-Spark deployment, with the fix."""
    res: list[dict[str, Any]] = []
    facts: dict[str, dict] = {}
    for n, agent in ctrl.agents.items():
        try:
            facts[n] = await agent.call("hardware_facts", timeout=30)
            ctrl.store.kv_set(f"hardware:{n}", facts[n])
            v = facts[n].get("agent_version")
            _check(res, "agent", n, "pass" if v == __version__ else "warn",
                   f"reachable, version {v}", "" if v == __version__ else
                   f"controller is {__version__} — update the agent on node {n}")
        except AgentActionError as exc:
            _check(res, "agent", n, "fail", exc.detail,
                   f"start twinspark-agent on node {n}; check nodes.{n}.agent_url and the agent token")
    for n, f in facts.items():
        if f.get("runtime_mode") == "dry-run":
            _check(res, "runtime mode", n, "warn", "dry-run — activations simulate everything",
                   f"once `tsm plan` matches what you run by hand: `sudo tsm go-live` on node {n}")
        d = f.get("docker")
        if d is not None:
            _check(res, "docker", n, "pass" if d.get("ok") else "fail",
                   f"server {d.get('server_version')}" if d.get("ok") else str(d.get("error")),
                   "" if d.get("ok") else "add the agent user to the docker group")
        if f.get("desktop_running"):
            _check(res, "headless", n, "warn",
                   f"desktop running ({f.get('desktop_rss_gib', 0):.1f} GiB: "
                   f"{', '.join(f.get('desktop_processes', [])[:4])})",
                   "`tsm headless headless-max` (or systemctl set-default multi-user.target + reboot)")
        else:
            _check(res, "headless", n, "pass", f"no desktop session (default target "
                   f"{f.get('default_target') or 'unknown'})")
        _check(res, "privd", n, "pass" if f.get("privd_available") else "warn",
               "reachable" if f.get("privd_available") else "not reachable — page cache cannot be "
               "dropped before launch, headless mode cannot be switched",
               "" if f.get("privd_available") else f"on node {n}: `sudo systemctl enable --now twinspark-privd` "
               "(installed by `tsm setup`; re-run `sudo tsm setup` if the unit is missing)")
        free = f.get("disk_free_gib") or 0
        _check(res, "disk", n, "pass" if free > 250 else ("warn" if free > 50 else "fail"),
               f"{free:.0f} GiB free in {f.get('hf_cache_dir')}",
               "" if free > 250 else "delete unused models: `tsm models ls` / `tsm models rm`")
        sw = f.get("swappiness")
        if sw is not None and sw > 10:
            _check(res, "swappiness", n, "warn", f"vm.swappiness={sw}",
                   "community GB10 recipes run with vm.swappiness=0 (unified memory)")
        pc = f.get("page_cache_gib") or 0
        if pc > 20:
            _check(res, "page cache", n, "info", f"{pc:.0f} GiB page cache",
                   "dropped automatically before each launch when tsm-privd runs")
        foreign = f.get("foreign_inference") or []
        if foreign:
            _check(res, "foreign vLLM", n, "warn",
                   "running outside TwinSpark: " + ", ".join(c["name"] for c in foreign),
                   "stop it before the first activation (Diagnostics → foreign containers)")
        if not f.get("infiniband_dev") and f.get("runtime_mode") == "docker":
            _check(res, "rdma device", n, "warn", "/dev/infiniband missing",
                   "RoCE is unavailable to containers; TP2 would fall back to TCP")
    if len(facts) == 2:
        dv = {n: f.get("driver_version") for n, f in facts.items()}
        if all(dv.values()):
            same = len(set(dv.values())) == 1
            _check(res, "driver parity", "A+B", "pass" if same else "warn",
                   f"{dv}", "" if same else "install the same NVIDIA driver on both nodes — "
                   "mismatched drivers cost dual-Spark setups 2x+ throughput")
        kv = {n: f.get("kernel") for n, f in facts.items()}
        if len(set(kv.values())) > 1:
            _check(res, "kernel parity", "A+B", "info", f"{kv}")
        try:
            rd = await rdma_report(ctrl)
            for n, r in rd.items():
                if "error" in r:
                    _check(res, "rdma", n, "warn", r["error"])
                    continue
                sug = r["suggestion"]
                if not r["configured"]["rdma_hcas"]:
                    _check(res, "rdma config", n, "fail",
                           "nodes.%s.rdma_hcas is empty — NCCL uses TCP sockets" % n,
                           (f"`sudo tsm rdma --apply && sudo systemctl restart twinspark-controller` "
                            f"(sets in controller.yaml under nodes.{n}:\n{r['yaml']})") if sug.get("hcas") else
                           "no RoCE device was found: check the QSFP link with `tsm qsfp status` on that node")
                elif not r["matches_config"]:
                    _check(res, "rdma config", n, "warn",
                           f"configured {r['configured']} but discovered {sug['hcas']} "
                           f"gid {sug['gid_index']}", r["yaml"])
                else:
                    _check(res, "rdma config", n, "pass",
                           f"{', '.join(sug['hcas'])} (gid {sug['gid_index']})")
                if sug.get("note"):
                    _check(res, "rdma bandwidth", n, "warn", sug["note"])
        except Exception as exc:  # noqa: BLE001
            _check(res, "rdma", "A+B", "warn", str(exc))
        for src, dst in (("A", "B"), ("B", "A")):
            ep = ctrl.config.nodes.get(dst)
            if not ep or not ep.ssh_user or not ep.sync_host:
                if dst == "B":
                    _check(res, "ssh", f"{src}→{dst}", "warn", f"nodes.{dst}.ssh_user not set",
                           "weights cannot be copied to this node over QSFP")
                continue
            try:
                r = await ctrl.agents[src].call("ssh_check", target_host=ep.sync_host,
                                                ssh_user=ep.ssh_user, timeout=40)
                _check(res, "ssh", f"{src}→{dst}", "pass" if r.get("ok") else "fail",
                       f"{ep.ssh_user}@{ep.sync_host}" + ("" if r.get("ok") else f": {r.get('error')}"),
                       "" if r.get("ok") else f"copy the agent user's SSH key from node {src} to "
                       f"{ep.ssh_user}@{ep.sync_host} (ssh-copy-id)")
            except AgentActionError as exc:
                _check(res, "ssh", f"{src}→{dst}", "fail", exc.detail)
    if not ctrl.hf_token:
        _check(res, "hf token", "controller", "info", "no Hugging Face token stored",
               "gated models need one: `tsm init --hf-token hf_...` on node A and B")
    summary = {s: sum(1 for r in res if r["status"] == s) for s in ("pass", "info", "warn", "fail")}
    return {"checks": res, "summary": summary, "version": __version__}


async def headless_apply(ctrl: "Controller", mode: str, now: bool = False) -> dict[str, Any]:
    if mode not in ("desktop", "headless-safe", "headless-max"):
        raise ValueError("mode must be desktop, headless-safe or headless-max")
    results = {}
    for n, a in ctrl.agents.items():
        try:
            results[n] = await a.call("headless_apply", mode=mode, now=now, timeout=180)
        except AgentActionError as exc:
            results[n] = {"error": exc.detail}
    ctrl.store.kv_set("headless_mode", mode)
    ctrl._audit("user", "headless.apply", "system", {"mode": mode, "now": now,
                                                      "nodes": list(results)})
    return {"mode": mode, "now": now, "results": results}


async def headless_status(ctrl: "Controller") -> dict[str, Any]:
    out = {}
    for n, a in ctrl.agents.items():
        try:
            out[n] = await a.call("headless_status", timeout=20)
        except AgentActionError as exc:
            out[n] = {"error": exc.detail}
    return {"mode": ctrl.store.kv_get("headless_mode"), "nodes": out}


async def foreign_containers(ctrl: "Controller") -> dict[str, Any]:
    out = {}
    for n, a in ctrl.agents.items():
        try:
            out[n] = (await a.call("foreign_list", timeout=30))["foreign"]
        except AgentActionError as exc:
            out[n] = {"error": exc.detail}
    return out


async def stop_foreign(ctrl: "Controller", node: str, name: str, confirm: str) -> dict[str, Any]:
    if node not in ctrl.agents:
        raise ValueError("unknown node")
    if ctrl.busy():
        raise RuntimeError("an activation is running")
    res = await ctrl.agents[node].call("foreign_stop", name=name, confirm=confirm, timeout=120)
    ctrl._audit("user", "foreign.stop", f"container/{node}/{name}", {})
    return res
