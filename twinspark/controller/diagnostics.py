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
    """Responder on B, initiator on A, over the QSFP IPs.

    Both agents must run in the same mode: a dry-run responder opens no socket, so a live initiator
    would only measure "connection refused" (and a dry-run initiator would invent numbers).
    """
    import time

    a, b = ctrl.agents.get("A"), ctrl.agents.get("B")
    if not a or not b:
        raise ValueError("link test needs both node A and node B configured")
    if mode not in ("tcp", "rdma"):
        raise ValueError("mode must be tcp or rdma")
    ep_a, ep_b = ctrl.config.nodes["A"], ctrl.config.nodes["B"]
    peer_ip = ep_b.qsfp_ip
    if not peer_ip:
        raise ValueError("nodes.B.qsfp_ip is not configured")
    modes = {}
    for n, agent in (("A", a), ("B", b)):
        try:
            modes[n] = (await agent.call("hardware_facts", timeout=15)).get("runtime_mode")
        except AgentActionError as exc:
            raise ValueError(f"node {n} is not reachable: {exc.detail}") from exc
    if len(set(modes.values())) > 1:
        dry = [n for n, m in modes.items() if m == "dry-run"]
        raise ValueError(f"node {', '.join(dry)} runs in dry-run while the other node runs real containers: a "
                         f"simulated side opens no socket, so the test cannot measure the link. Run "
                         f"`sudo tsm go-live` on node {', '.join(dry)} (or keep both in dry-run to see a simulation)")
    simulated = all(m == "dry-run" for m in modes.values())
    common = dict(mode=mode, port=port, duration_s=duration_s, streams=streams)
    resp = await b.call("link_test", role="responder", bind=peer_ip if mode == "tcp" else None,
                        hcas=ep_b.rdma_hcas, gid_index=ep_b.ib_gid_index, **common)

    async def cancel_responder() -> None:
        try:
            await b.call("task_cancel", task_id=resp["task_id"], timeout=10)
        except AgentActionError:
            pass

    await asyncio.sleep(1.0 if mode == "rdma" else 0.5)
    try:
        init = await a.call("link_test", role="initiator", peer_ip=peer_ip,
                            hcas=ep_a.rdma_hcas, gid_index=ep_a.ib_gid_index, **common)
    except BaseException:
        await cancel_responder()                       # nothing will connect; do not leave it listening
        raise
    timeout = duration_s + 90
    init_t: dict[str, Any] = {}
    try:
        init_t = await a.wait_task(init["task_id"], timeout=timeout, poll=0.5)
    except AgentActionError as exc:
        init_t = {"result": {"error": exc.detail}}
        await cancel_responder()
    try:
        resp_t = await b.wait_task(resp["task_id"], timeout=timeout if mode == "rdma" else 30, poll=0.5)
    except AgentActionError as exc:
        resp_t = {"result": {"error": exc.detail}}
    init_r, resp_r = init_t.get("result") or {}, resp_t.get("result") or {}
    errors = [e for e in ([init_r.get("error")] + list(init_r.get("errors") or [])) if e]
    if resp_r.get("error"):
        errors.append(f"responder on node B: {resp_r['error']}")
    out = {"mode": mode, "nodes": {"initiator": "A", "responder": "B"}, "simulated": simulated,
           "runtime_modes": modes, "duration_s": duration_s,
           "streams": streams if mode == "tcp" else None,
           "hcas": {"A": list(ep_a.rdma_hcas), "B": list(ep_b.rdma_hcas)} if mode == "rdma" else None,
           "initiator": init_r, "responder": resp_r,
           "ok": init_r.get("bandwidth_gbps") is not None and not init_r.get("error"),
           "errors": errors}
    ctrl.store.kv_set(f"linktest:{mode}", {
        "at": time.time(), "ok": out["ok"], "simulated": simulated, "bandwidth_gbps": init_r.get("bandwidth_gbps"),
        "measured_gbps": init_r.get("measured_gbps"), "per_hca": [
            {k: r.get(k) for k in ("hca", "gbps", "error")} for r in init_r.get("per_hca") or []],
        "streams": out["streams"], "duration_s": duration_s, "errors": errors[:5]})
    ctrl._audit("user", "link.test", "system", {"mode": mode, "duration_s": duration_s, "ok": out["ok"],
                                                "simulated": simulated})
    return out


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
                   f"GB10 recipes run with vm.swappiness=0 (unified memory; keep swap on). On node {n}, "
                   "persistently: echo 'vm.swappiness = 0' | sudo tee /etc/sysctl.d/90-twinspark-swappiness.conf "
                   "&& sudo sysctl --system")
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
    me = ctrl.config.node.node_id
    if ctrl.config.management_auth == "none" and me in facts:
        # without a key only the Host header is checked; a tailnet forward makes that a header anyone can send
        try:
            acc = (await ctrl.agents[me].call("headless_status", timeout=30,
                                              manager_port=ctrl.config.listener.port)).get("access") or {}
        except AgentActionError:
            acc = {}
        ts = acc.get("tailscale") or {}
        forwarded = (ts.get("serve") or {}).get("manager_forwarded")
        fix = ("`sudo tsm remote tailscale-serve --remove`, or set management_auth: apikey in controller.yaml "
               "and restart the controller")
        if forwarded:
            _check(res, "management auth", me, "fail",
                   f"management_auth is 'none' and port {ctrl.config.listener.port} is forwarded on the tailnet: "
                   "every tailnet device can use the management API without a key", fix)
        elif forwarded is None and ts.get("running"):
            _check(res, "management auth", me, "warn",
                   "management_auth is 'none' and Tailscale is up, but whether the manager port is forwarded "
                   "could not be read (`tailscale serve status` needs root). A forward would expose the API "
                   "without a key", "check with `sudo tailscale serve status`; if it is forwarded: " + fix)
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
    from ..headless import acts_now
    from .controller import BusyError
    if acts_now(mode, now) and (ctrl.busy() or ctrl.maintenance.blocking()):
        # headless-max stops the display manager even without --now: that frees (or, for desktop,
        # takes) gigabytes of unified memory in the middle of a model start
        raise BusyError(f"{mode}{' --now' if now else ''} starts or stops the desktop immediately; an "
                        f"activation or maintenance run is in progress — wait for it to finish")
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
        # the agent on the controller's machine also checks that the manager port is forwarded
        extra = {"manager_port": ctrl.config.listener.port} if n == ctrl.config.node.node_id else {}
        try:
            out[n] = await a.call("headless_status", timeout=30, **extra)
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
