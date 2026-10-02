"""tsm — the TwinSpark Manager CLI.

Talks to the same management API as the web GUI (never around it). The API
address comes from ``--api`` / ``$TSM_API`` (default ``http://127.0.0.1:8443``),
the key from ``--key`` / ``$TSM_KEY`` or, on the controller node, straight from
the local secrets vault.

Everyday flow::

    tsm cookbook list                      # built-in dual-Spark recipes
    tsm cookbook import-recipe ~/spark-vllm-docker/recipes/glm-5.3-flash.yaml
    tsm pin glm-5.3-flash                  # resolve commit sha + image digest
    tsm plan glm-5.3-flash                 # exact docker/vllm commands on A and B
    tsm activate glm-5.3-flash             # stage weights, switch, verify, route
    tsm models ls                          # what is on disk on both nodes
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

import httpx

from . import __version__

DEFAULT_API = "http://127.0.0.1:8443"
DEFAULT_SECRETS = "/etc/twinspark/secrets"


# ---- plumbing -------------------------------------------------------------------------------
class Api:
    def __init__(self, base: str, key: str, as_json: bool = False):
        self.base = base.rstrip("/")
        self.key = key
        self.as_json = as_json

    def __call__(self, method: str, path: str, *, ok: tuple[int, ...] = (), timeout: float = 120,
                 **kw) -> Any:
        headers = {"x-api-key": self.key} if self.key else {}
        try:
            r = httpx.request(method, f"{self.base}{path}", headers=headers, timeout=timeout, **kw)
        except httpx.HTTPError as exc:
            sys.exit(f"cannot reach the controller at {self.base} ({type(exc).__name__}) — "
                     f"is twinspark-controller running? (set --api / TSM_API)")
        if r.status_code >= 400 and r.status_code not in ok:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:500]
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail)
            elif isinstance(detail, list):         # pydantic validation errors
                detail = "; ".join(f"{'.'.join(str(x) for x in d.get('loc', []))}: {d.get('msg')}"
                                   for d in detail)
            sys.exit(f"error {r.status_code}: {detail}")
        try:
            return r.json()
        except ValueError:
            return r.text


def _key(args) -> str:
    if args.key:
        return args.key
    if os.environ.get("TSM_KEY"):
        return os.environ["TSM_KEY"]
    try:
        from .security import SecretsVault
        return SecretsVault(args.secrets_dir).get("management_api_key") or ""
    except Exception:  # noqa: BLE001 - vault unreadable for this user: fall back to no key
        return ""


def _out(api: Api, data: Any) -> bool:
    """Print raw JSON when --json was given. Returns True if it did."""
    if api.as_json:
        print(json.dumps(data, indent=2, default=str))
        return True
    return False


def _gib(b: Optional[float]) -> str:
    return "-" if b is None else f"{b / 1024**3:.1f} GiB"


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    d = max(0, time.time() - ts)
    for unit, sec in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= sec:
            return f"{int(d // sec)}{unit} ago"
    return f"{int(d)}s ago"


def _table(rows: list[list[Any]], head: list[str]) -> None:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(head)]
    print("  ".join(h.ljust(w) for h, w in zip(head, widths, strict=True)).rstrip())
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())


# ---- setup & services -----------------------------------------------------------------------
def cmd_init(args, api=None):
    from .security import SecretsVault

    vault = SecretsVault(args.secrets_dir)
    slots = ["agent_token"] if args.role == "agent" else [
        "agent_token", "management_api_key", "inference_api_key", "backend_api_key"]
    if args.role == "agent" and args.agent_token:
        vault.set("agent_token", args.agent_token)
        slots = ["backend_api_key"]
        if args.backend_api_key:
            vault.set("backend_api_key", args.backend_api_key)
            slots = []
    for s in slots:
        val, created = vault.ensure(s)
        print(f"{s:20} {'created' if created else 'exists '}  {val if args.show else '(hidden, use --show)'}")
    if args.role == "controller":
        print("\nOn node B run:  tsm init --role agent --agent-token <agent_token> "
              "--backend-api-key <backend_api_key>   (tsm init --show prints them)")
    if args.hf_token:
        vault.set("hf_token", args.hf_token)
        print("hf_token             stored (set it on both nodes: the controller pins, the agents download)")


def cmd_serve(args, api=None):
    from . import serve

    if args.what == "controller":
        asyncio.run(serve.run_controller(args.config))
    elif args.what == "agent":
        serve.run_agent(args.config)
    elif args.what == "termd":
        serve.run_termd(args.config)
    else:
        serve.run_privd(args.socket, args.group, args.remote_policy, not args.unsafe_policy_owner)


# ---- status ---------------------------------------------------------------------------------
def cmd_status(args, api: Api):
    st = api("GET", "/api/v1/status")
    if _out(api, st):
        return
    act, det = st.get("active"), st.get("active_detail") or {}
    if act:
        print(f"active    {act['profile']} {act.get('label', '')} as {', '.join(act.get('aliases') or [act['alias']])}"
              f"  (since {_ago(act.get('since'))})")
        print(f"model     {det.get('model')}@{(det.get('model_revision') or '')[:12]}  {det.get('topology')}")
        print(f"image     {det.get('image')}")
        obs = det.get("observed") or {}
        kv = obs.get("kv") or {}
        if kv.get("kv_cache_tokens"):
            conc = (f" (≈{kv['max_concurrency']:.1f}x at {kv['max_concurrency_at_tokens']:,} tokens)"
                    if kv.get("max_concurrency") else "")
            print(f"kv pool   {kv['kv_cache_tokens']:,} tokens{conc}")
        if obs.get("max_model_len"):
            print(f"context   {obs['max_model_len']:,} tokens")
    else:
        print("active    -")
    if st.get("current_job"):
        print(f"job       {st['current_job']} (running)")
    if st.get("staging"):
        print(f"staging   {', '.join(st['staging'])}")
    for r in st.get("routes", []):
        down = f"  DOWN: {r['down_reason']}" if r.get("down_reason") else ""
        print(f"route     {r['alias']:10} {r['status']:10} → {r['model']}  in-flight {r['inflight']}"
              f"  requests {r.get('requests', 0)}  errors {r.get('errors', 0)}{down}")
    for n, t in sorted((st.get("telemetry") or {}).items()):
        if "error" in t:
            print(f"node {n}    {t['error']}")
        else:
            print(f"node {n}    {t['mem_available_gib']:.1f} / {t['mem_total_gib']:.1f} GiB available "
                  f"({t.get('level', '')}), page cache {t.get('page_cache_gib', 0):.1f} GiB")
    m = st.get("metrics") or {}
    if m.get("ok"):
        print(f"serving   decode {m.get('decode_tok_s', '-')} tok/s, prefill {m.get('prefill_tok_s', '-')} tok/s, "
              f"running {m.get('requests-running', 0):.0f}, waiting {m.get('requests-waiting', 0):.0f}, "
              f"KV {m.get('kv_cache_usage_pct', '-')}%")
        if m.get("spec_mean_accept_len"):
            print(f"spec      mean acceptance length {m['spec_mean_accept_len']}")
    inc = (st.get("watchdog") or {}).get("last_incident")
    if inc:
        print(f"incident  {_ago(inc.get('at'))}: {inc['problem']} → {inc['action']}")


# ---- profiles -----------------------------------------------------------------------------
def cmd_profiles(args, api: Api):
    rows = api("GET", "/api/v1/profiles", params={"summary": "true"})
    if _out(api, rows):
        return
    if not rows:
        print("no profiles — `tsm cookbook list` / `tsm cookbook import <recipe>`")
        return
    out = []
    for p in rows:
        state = "ACTIVE" if p["active"] else ("pinned" if p["pinned"] else "draft")
        if p["pinned"] and p["draft_differs"]:
            state += "*"
        last = p.get("latest") or {}
        out.append([p["name"], p["model"], p["topology"], p["verification"], state,
                    last.get("label", "-"), "✓" if last.get("known_good") else ""])
    _table(out, ["PROFILE", "MODEL", "TOPOLOGY", "RECIPE", "STATE", "REV", "GOOD"])
    if any(p["pinned"] and p["draft_differs"] for p in rows):
        print("\n* draft has unpinned edits — `tsm pin <profile>` makes them activatable")


def cmd_show(args, api: Api):
    p = api("GET", f"/api/v1/profiles/{args.profile}")
    if _out(api, p):
        return
    d = p.get("draft") or (p["revisions"][-1]["draft"] if p["revisions"] else {})
    s, a, b = d.get("simple", {}), d.get("advanced", {}), d.get("behaviour", {})
    print(f"{p['name']} — {p.get('description') or ''}")
    print(f"  model        {s.get('model')}  ({s.get('quantization')}, {s.get('topology')})")
    print(f"  context      {s.get('context_length')}   concurrency {a.get('max_num_seqs') or s.get('concurrency')}")
    print(f"  aliases      {', '.join([s.get('api_alias', 'default')] + (s.get('extra_aliases') or []))}")
    if d.get("secondary"):
        second = d["secondary"]
        print(f"  node B model {second['simple']['model']} ({second['simple']['api_alias']})")
        print(f"  node B image {(second.get('identity') or {}).get('image') or second.get('image_hint')}")
    print(f"  parsers      reasoning={b.get('reasoning_parser')} tools={b.get('tool_call_parser')}")
    if a.get("speculative_config"):
        print(f"  speculative  {json.dumps(a['speculative_config'])}")
    if a.get("mods"):
        print(f"  mods         {', '.join(a['mods'])}")
    if a.get("extra_vllm_args"):
        print(f"  raw args     {' '.join(a['extra_vllm_args'])}")
    if d.get("image_hint"):
        print(f"  image hint   {d['image_hint']}")
    src = d.get("source") or {}
    if src.get("url") or src.get("ref"):
        print(f"  source       {src.get('url') or src.get('ref')}")
    for r in src.get("requirements") or []:
        print(f"  needs        {r}")
    print("  revisions:")
    for r in p["revisions"]:
        i = r["identity"]
        flags = " ".join(x for x in ("known-good" if r["known_good"] else "",
                                     "pinned" if r.get("pinned") else "") if x)
        print(f"    {r['label']:4} {r['created_at'][:16]}  {i['model_revision'][:10]}  "
              f"{i['image']}@{i['image_digest'][7:19]}  {flags}")
    if not p["revisions"]:
        print("    (none — `tsm pin " + p["name"] + "`)")
    fit = api("GET", f"/api/v1/profiles/{args.profile}/fit")
    if fit.get("nodes"):
        for node, detail in fit["nodes"].items():
            print(f"  node {node} memory: " + json.dumps(detail))
    elif fit.get("known"):
        print(f"  memory       weights {fit['weights_gib_per_node']} GiB/node, KV pool ≈ "
              f"{fit['kv_pool_gib_per_node']} GiB/node ≈ {fit['est_kv_tokens']:,} tokens "
              f"({fit['kv_bytes_per_token_source']} KV size){'' if fit['fits'] else '  — DOES NOT FIT'}")


def cmd_fit(args, api: Api):
    fit = api("GET", f"/api/v1/profiles/{args.profile}/fit")
    if _out(api, fit):
        return
    if fit.get("nodes"):
        for node, detail in fit["nodes"].items():
            print(f"node {node}: {detail['model']}")
            print(json.dumps(detail, indent=2))
        return
    if not fit.get("known"):
        print(fit.get("note"))
    else:
        print(f"fits                 {'yes' if fit['fits'] else 'NO'}"
              f"{'' if fit['full_concurrency_fits'] else ' (not every sequence at full context)'}")
        print(f"gpu mem utilization  {fit['gpu_memory_utilization']}")
        print(f"weights per node     {fit['weights_gib_per_node']} GiB")
        print(f"KV pool per node     {fit['kv_pool_gib_per_node']} GiB ({fit['kv_bytes_per_token_source']})")
        print(f"≈ KV tokens          {fit['est_kv_tokens']:,}  "
              f"(≈ {fit['est_full_context_seqs']} sequences at {fit['context_length']:,})")
        print(f"headroom             {fit['headroom_gib']} GiB ({fit['level']})")
    obs = fit.get("observed")
    if obs:
        kv = obs.get("kv") or {}
        print(f"observed last run    KV {kv.get('kv_cache_tokens', '-')} tokens, "
              f"max_model_len {obs.get('max_model_len', '-')}")


def cmd_pin(args, api: Api):
    body = {"model_ref": args.model_ref, "image": args.image, "local_image": args.local_image,
            "note": args.note}
    res = api("POST", f"/api/v1/profiles/{args.profile}/pin",
              json={k: v for k, v in body.items() if v}, timeout=300)
    if _out(api, res):
        return
    rev = res["revision"]
    print(f"pinned {args.profile} {rev['label']}")
    print(f"  model  {res['resolved']['model']}")
    print(f"  image  {res['resolved']['image']} ({res['resolved']['image_source']})")
    if res["resolved"].get("secondary"):
        second = res["resolved"]["secondary"]
        print(f"  node B model  {second['model']}")
        print(f"  node B image  {second['image']} ({second['image_source']})")
    for x in res["resolved"].get("extra_models") or []:
        print(f"  extra  {x}")
    for n in res.get("notes", []):
        print(f"  note   {n}")


def cmd_edit(args, api: Api):
    p = api("GET", f"/api/v1/profiles/{args.profile}")
    draft = p.get("draft") or (p["revisions"][-1]["draft"] if p["revisions"] else None)
    if draft is None:
        sys.exit("profile has no draft")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
        json.dump(draft, f, indent=2)
        path = f.name
    try:
        while True:
            subprocess.run([*editor.split(), path], check=False)
            try:
                new = json.loads(Path(path).read_text())
            except ValueError as exc:
                if input(f"invalid JSON ({exc}) — edit again? [Y/n] ").lower().startswith("n"):
                    return
                continue
            if new == draft:
                print("no changes")
                return
            api("PUT", f"/api/v1/profiles/{args.profile}/draft", json=new)
            print("draft saved — `tsm pin " + args.profile + "` to make it activatable")
            return
    finally:
        os.unlink(path)


def cmd_export(args, api: Api):
    p = api("GET", f"/api/v1/profiles/{args.profile}")
    draft = p.get("draft") or (p["revisions"][-1]["draft"] if p["revisions"] else None)
    if draft is not None and not args.with_identity:
        draft.pop("identity", None)
        if draft.get("secondary"):
            draft["secondary"].pop("identity", None)
    print(json.dumps(draft, indent=2))


def cmd_delete(args, api: Api):
    api("DELETE", f"/api/v1/profiles/{args.profile}")
    print(f"deleted profile {args.profile} (model files are kept — `tsm models rm`)")


def cmd_plan(args, api: Api):
    path = (f"/api/v1/profiles/{args.profile}/draft/launch-plan" if args.draft else
            f"/api/v1/profiles/{args.profile}/revisions/{args.revision}/launch-plan")
    data = api("GET", path)
    if _out(api, data):
        return
    plan = data["plan"]
    if data.get("unpinned"):
        print("# DRAFT — model sha and image digest are placeholders until `tsm pin`")
    print(f"# backend={plan['backend']}  gpu_memory_utilization={plan['gpu_memory_utilization']}")
    for note in plan["notes"]:
        print(f"# note: {note}")
    delays = plan.get("wave_delays_s") or []
    for wave, names in enumerate(plan["start_order"]):
        for n in names:
            node = next(c["node"] for c in plan["containers"] if c["name"] == n)
            print(f"\n# wave {wave + 1} — node {node}\n{data['commands'][n]}")
        if wave < len(delays) and delays[wave] and wave < len(plan["start_order"]) - 1:
            print(f"\n# … wait {delays[wave]:.0f}s")


def _follow(api: Api, job_id: str) -> dict:
    """Print step transitions and live progress until the job ends."""
    seen = 0
    live = sys.stdout.isatty()
    last = ""
    while True:
        job = api("GET", f"/api/v1/jobs/{job_id}")
        steps = job["steps"]
        while seen < len(steps) and steps[seen]["status"] != "running":
            s = steps[seen]
            if live and last:
                print("\r\033[K", end="")
            print(f"  [{s['status']:6}] {s['stage']:17} {s['message']}")
            seen += 1
            last = ""
        if seen < len(steps) and live:
            s = steps[seen]
            line = f"  [ .... ] {s['stage']:17} {s['message']}"[: shutil.get_terminal_size().columns - 1]
            if line != last:
                print("\r\033[K" + line, end="", flush=True)
                last = line
        if job["state"] in ("completed", "failed", "cancelled"):
            if live and last:
                print("\r\033[K", end="")
            return job
        time.sleep(1.5)


def _report(job: dict) -> None:
    if job["state"] == "completed":
        return
    print(f"\n{job['state'].upper()}: {job.get('error')}")
    for g in job.get("guidance", []):
        print("  hint:", g)
    if job.get("rollback"):
        print("  rollback:", job["rollback"])
    excerpt = next((s.get("log_excerpt") for s in reversed(job["steps"]) if s.get("log_excerpt")), None)
    if excerpt:
        print("\n--- log excerpt ---\n" + excerpt)
    sys.exit(1)


def cmd_activate(args, api: Api):
    job = api("POST", f"/api/v1/profiles/{args.profile}/activate", params={"revision": args.revision})
    print(f"job {job['job_id']}  (Ctrl-C stops watching, not the switch; `tsm cancel {job['job_id']}`)")
    if args.no_wait:
        return
    try:
        job = _follow(api, job["job_id"])
    except KeyboardInterrupt:
        print("\nstill running in the background — `tsm job " + job["job_id"] + "`")
        return
    _report(job)
    for w in job.get("payload", {}).get("warnings", []):
        print("  warning:", w)
    print("\nOK — serving.")


def cmd_prepare(args, api: Api):
    job = api("POST", f"/api/v1/profiles/{args.profile}/prepare", params={"revision": args.revision})
    print(f"preparation job {job['job_id']} — current deployment keeps serving")
    if not args.no_wait:
        job = _follow(api, job["job_id"])
        _report(job)
        print(f"prepared {job['profile_revision']} — activate this revision when ready")


def cmd_split(args, api: Api):
    p = api("POST", "/api/v1/profiles/compose/split", json={
        "name": args.name, "node_a": args.node_a, "node_b": args.node_b,
        "alias_a": args.alias_a, "alias_b": args.alias_b})
    print(f"created {p['name']} — A: {args.node_a} ({args.alias_a}), B: {args.node_b} ({args.alias_b})")
    print(f"review with `tsm show {p['name']}`, then pin and prepare")


def cmd_cancel(args, api: Api):
    r = api("POST", f"/api/v1/jobs/{args.job}/cancel")
    print(r["note"])


def cmd_stop(args, api: Api):
    print(api("POST", "/api/v1/stop", timeout=600)["steps"][-1]["message"])


def cmd_jobs(args, api: Api):
    jobs = api("GET", "/api/v1/jobs", params={"limit": args.limit, **({"kind": args.kind} if args.kind else {})})
    if _out(api, jobs):
        return
    _table([[j["job_id"], j["kind"], j["state"], (j.get("payload") or {}).get("profile")
             or (j.get("payload") or {}).get("repo") or "", j.get("stage") or "", j["created_at"][:19],
             (j.get("error") or "")[:60]] for j in jobs],
           ["JOB", "KIND", "STATE", "TARGET", "STAGE", "CREATED", "ERROR"])


def cmd_job(args, api: Api):
    job = api("GET", f"/api/v1/jobs/{args.job}")
    if _out(api, job):
        return
    if job["state"] in ("running", "pending") and not args.no_wait:
        job = _follow(api, args.job)
    else:
        for s in job["steps"]:
            print(f"  [{s['status']:6}] {s['stage']:17} {s['message']}")
    _report(job)


def cmd_logs(args, api: Api):
    if args.node and args.container:
        print(api("GET", f"/api/v1/logs/{args.node}/{args.container}", params={"tail": args.tail})["log"])
        return
    res = api("GET", "/api/v1/active/logs", params={"tail": args.tail})
    if not res["containers"]:
        print("nothing running")
    for c in res["containers"]:
        print(f"===== node {c['node']} · {c['name']} ({c['role']}) =====")
        print(c.get("log") or c.get("error", ""))


# ---- cookbook -------------------------------------------------------------------------------
def cmd_cookbook(args, api: Api):
    if args.action == "list":
        data = api("GET", "/api/v1/cookbook")
        if _out(api, data):
            return
        for r in data["recipes"]:
            if "error" in r:
                print(f"{r['name']:36} ERROR {r['error']}")
                continue
            imp = f"  → profile {', '.join(r['imported_as'])}" if r.get("imported_as") else ""
            print(f"{r['name']:36} {r['verification']:12} {r['topology']:7} {r['model']}{imp}")
            print(f"    {r['title']}")
        print("\ncommunity sources: " + ", ".join(data.get("sources", [])) + "  (`tsm cookbook community`)")
    elif args.action == "show":
        r = api("GET", f"/api/v1/cookbook/recipes/{args.name}")
        if _out(api, r):
            return
        print(f"{r['name']} — {r['title']}  [{r['verification']}]\n{r['description']}\n")
        for k in ("url", "author", "image_hint"):
            if r.get(k):
                print(f"{k:12} {r[k]}")
        for k, v in (r.get("measured") or {}).items():
            print(f"measured     {k}: {v}")
        for x in r.get("requirements") or []:
            print(f"needs        {x}")
        for x in r.get("notes") or []:
            print(f"note         {x}")
        rep = r.get("report") or {}
        if rep.get("raw"):
            print(f"raw args     {' '.join(rep['raw'])}")
        if rep.get("dropped"):
            print(f"dropped      {', '.join(rep['dropped'])} (TwinSpark wires these itself)")
    elif args.action == "import":
        if not args.name:
            sys.exit("usage: tsm cookbook import <recipe> [--as NAME]")
        res = api("POST", f"/api/v1/cookbook/import/{args.name}",
                  params={"profile_name": args.as_name} if args.as_name else None)
        print(f"imported profile {res['profile']['name']} — next: `tsm pin {res['profile']['name']}`")
    elif args.action == "community":
        data = api("GET", "/api/v1/cookbook/community", params={"refresh": str(args.refresh).lower()})
        if _out(api, data):
            return
        for src in data["sources"]:
            print(f"# {src['source']}" + (f"  — {src['error']}" if src.get("error") else ""))
            for f in src.get("files", []):
                print(f"  {f['name']:48} {f.get('url') or ''}")
        print("\nimport one: tsm cookbook import-recipe <url>")
    elif args.action == "import-recipe":
        if not args.name:
            sys.exit("usage: tsm cookbook import-recipe <file|url> [--as NAME] [--preview] [-e key=value]")
        body: dict[str, Any] = {"preview": args.preview, "overrides": {}}
        for kv in args.set or []:
            k, _, v = kv.partition("=")
            body["overrides"][k] = int(v) if v.isdigit() else v
        if args.as_name:
            body["profile_name"] = args.as_name
        if args.name.startswith(("http://", "https://")):
            body["url"] = args.name
        else:
            body["text"] = Path(args.name).expanduser().read_text()
        res = api("POST", "/api/v1/cookbook/import-recipe", json=body)
        if _out(api, res):
            return
        rep, d = res["report"], res["draft"]
        print(f"{'PREVIEW of' if res['preview'] else 'imported'} profile {d['name']}  "
              f"({d['simple']['model']}, {d['simple']['topology']}, format {rep.get('format')})")
        for k, v in (rep.get("mapped") or {}).items():
            print(f"  mapped   --{k} = {v}")
        if rep.get("raw"):
            print(f"  raw      {' '.join(rep['raw'])}")
        for x in rep.get("dropped") or []:
            print(f"  dropped  {x}")
        for x in rep.get("notes") or []:
            print(f"  note     {x}")
        for w in res.get("warnings") or []:
            print(f"  warning  {w}")
        if not res["preview"]:
            print(f"\nnext: tsm pin {d['name']}")


# ---- model files ----------------------------------------------------------------------------
def cmd_models(args, api: Api):
    if args.action == "ls":
        inv = api("GET", "/api/v1/models/files", timeout=300)
        if _out(api, inv):
            return
        nodes = sorted(inv["nodes"])
        rows = []
        for m in inv["models"]:
            for r in m["revisions"]:
                cells = []
                for n in nodes:
                    x = r["nodes"].get(n)
                    cells.append("-" if not x else ("✓" if x["complete"] else "partial") +
                                 ("+v" if x.get("verified") else ""))
                use = ("ACTIVE " if r["active"] else "") + ", ".join(r["profiles"])
                rows.append([m["repo"], r["revision"][:10], _gib(r["size_bytes"]), *cells,
                             ",".join(r["refs"]), use])
        _table(rows, ["MODEL", "REVISION", "SIZE", *[f"NODE {n}" for n in nodes], "REFS", "USED BY"])
        for n in nodes:
            d = inv["nodes"][n]
            if d.get("error"):
                print(f"node {n}: {d['error']}")
            else:
                print(f"node {n}: {_gib(d['free_bytes'])} free of {_gib(d['total_bytes'])} in {d['hf_home']}")
            for t in d.get("downloads") or []:
                print(f"  downloading: {t.get('detail')}")
        for x in inv.get("missing_for_profiles") or []:
            print(f"missing: {x['repo']}@{x['revision'][:10]} (profiles {', '.join(x['profiles'])}) — "
                  f"`tsm models stage {x['repo']}@{x['revision']}`")
    elif args.action == "stage":
        body = {"ref": args.target, "nodes": args.nodes.split(",") if args.nodes else None,
                "include": args.include or None}
        job = api("POST", "/api/v1/models/stage", json=body, timeout=120)
        print(f"job {job['job_id']}")
        if not args.no_wait:
            _report(_follow(api, job["job_id"]))
            print("staged.")
    elif args.action == "rm":
        repo, _, rev = args.target.partition("@")
        body = {"repo": repo, "revision": rev or None, "force": args.force,
                "nodes": args.nodes.split(",") if args.nodes else None, "preview": True}
        prev = api("POST", "/api/v1/models/files/delete", json=body, ok=(409,))
        if "detail" in prev:
            d = prev["detail"]
            sys.exit(d["message"] if isinstance(d, dict) else d)
        total = 0
        for n, r in prev["results"].items():
            if "error" in r:
                print(f"node {n}: {r['error']}")
                continue
            total += r.get("freed_bytes", 0)
            print(f"node {n}: frees {_gib(r.get('freed_bytes'))} ({len(r.get('revisions', []))} "
                  f"snapshot(s), keeps {r.get('kept_shared_blobs', 0)} shared file(s))")
        if prev.get("dependents"):
            print(f"used by profiles: {', '.join(prev['dependents'])}")
        if args.preview:
            return
        if not args.yes and input(f"delete {args.target} ({_gib(total)})? [y/N] ").lower() != "y":
            return
        body["preview"] = False
        res = api("POST", "/api/v1/models/files/delete", json=body)
        for n, r in res["results"].items():
            print(f"node {n}: " + (r.get("error") or ("dry-run, nothing deleted" if r.get("dry_run")
                                                    else f"freed {_gib(r.get('freed_bytes'))}")))


# ---- mods -----------------------------------------------------------------------------------
def _pack_dir(path: Path) -> str:
    if not (path / "run.sh").is_file():
        sys.exit(f"{path} has no run.sh — not a mod directory")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(path.rglob("*")):
            rel = f.relative_to(path)
            if any(part in (".git", "__pycache__") for part in rel.parts) or not f.is_file():
                continue
            info = zipfile.ZipInfo(rel.as_posix())
            info.external_attr = (f.stat().st_mode & 0o777) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    return base64.b64encode(buf.getvalue()).decode()


def _install_mod(api: Api, name: str, b64: str, nodes: Optional[str]) -> None:
    res = api("POST", "/api/v1/mods", timeout=300,
              json={"name": name, "archive_b64": b64, "nodes": nodes.split(",") if nodes else None})
    for n, r in res["results"].items():
        print(f"  {name:40} node {n}: " + (r.get("error") or f"ok {r['hash'][7:19]}"))
    if not res["consistent"]:
        print(f"  WARNING: {name} is not identical on every node")


def cmd_mods(args, api: Api):
    if args.action == "ls":
        data = api("GET", "/api/v1/mods")
        if _out(api, data):
            return
        for m in data["mods"]:
            nodes = " ".join(f"{n}:{'✓' if v['present'] else '-'}" for n, v in sorted(m["nodes"].items()))
            warn = "" if m["consistent"] else "  (differs between nodes!)"
            print(f"{m['name']:40} {nodes}  used by: {', '.join(m['profiles']) or '-'}{warn}")
            if m.get("summary"):
                print(f"    {m['summary']}")
        for m in data.get("missing", []):
            print(f"{m['name']:40} MISSING — needed by {', '.join(m['profiles'])}")
        if not data["mods"] and not data.get("missing"):
            print("no mods installed")
    elif args.action == "install":
        src = Path(args.path).expanduser()
        if src.is_dir():
            b64, name = _pack_dir(src), args.name or src.name
        else:
            b64 = base64.b64encode(src.read_bytes()).decode()
            name = args.name or src.name.split(".")[0]
        _install_mod(api, name, b64, args.nodes)
    elif args.action == "import-eugr":
        root = Path(args.path).expanduser()
        if (root / "mods").is_dir():
            root = root / "mods"
        only = set(args.only.split(",")) if args.only else None
        dirs = [d for d in sorted(root.iterdir()) if d.is_dir() and (d / "run.sh").is_file()
                and (only is None or d.name in only)]
        if not dirs:
            sys.exit(f"no mod directories with a run.sh under {root}")
        for d in dirs:
            _install_mod(api, d.name, _pack_dir(d), args.nodes)
    elif args.action == "rm":
        res = api("DELETE", f"/api/v1/mods/{args.path}")
        print(f"removed {args.path}" + (f" (still referenced by {', '.join(res['used_by'])})"
                                        if res.get("used_by") else ""))


# ---- diagnostics ----------------------------------------------------------------------------
_ICON = {"pass": "✓", "info": "·", "warn": "!", "fail": "✗"}


def cmd_doctor(args, api: Api):
    res = api("GET", "/api/v1/system/doctor", timeout=180)
    if _out(api, res):
        return
    for c in res["checks"]:
        print(f" {_ICON.get(c['status'], '?')} {c['node']:6} {c['check']:16} {c['detail']}")
        if c.get("fix") and c["status"] in ("warn", "fail"):
            for line in c["fix"].splitlines():
                print(f"{'':27}→ {line}")
    s = res["summary"]
    print(f"\n{s['pass']} ok, {s['warn']} warnings, {s['fail']} failures")
    if s["fail"]:
        sys.exit(1)


def cmd_rdma(args, api: Api):
    res = api("GET", "/api/v1/system/rdma")
    if _out(api, res):
        return
    for n, r in sorted(res.items()):
        print(f"node {n}")
        if "error" in r:
            print(f"  {r['error']}")
            continue
        for d in r["devices"]:
            ips = ", ".join(f"{g['ipv4']} (gid {g['index']})" for g in d["roce_v2_ipv4"]) or "-"
            print(f"  {d['hca']:14} {d['state']:8} {d['rate_gbps'] or '-':>5} Gb/s  {','.join(d['netdevs']):16} {ips}")
        print(f"  suggestion: {r['yaml'].replace(chr(10), '   ')}"
              f"{'   (matches controller.yaml)' if r['matches_config'] else '   ← put this under nodes.' + n}")
        if r["suggestion"].get("note"):
            print(f"  note: {r['suggestion']['note']}")
        if not r.get("perftest"):
            print("  (install perftest for `tsm link --mode rdma`)")
    if getattr(args, "apply", False):
        _rdma_apply(args, res)


def _rdma_apply(args, res: dict) -> None:
    from .provision import Layout, SetupError, set_node_fields
    path = Path(args.config) if args.config else Layout(Path(args.root)).controller_yaml
    if not path.exists():
        sys.exit(f"{path} not found — run this on the controller node (or pass --config)")
    changed = []
    for n, r in sorted(res.items()):
        sug = r.get("suggestion") if isinstance(r, dict) else None
        if not sug or not sug.get("hcas"):
            why = (r or {}).get("error") or (sug or {}).get("note") or "no RoCE device"
            print(f"node {n}: nothing to apply ({why})")
            continue
        try:
            if set_node_fields(path, n, sug["hcas"], sug["gid_index"]):
                changed.append(n)
        except SetupError as exc:
            sys.exit(str(exc))
    if changed:
        print(f"updated {path} for node(s) {', '.join(changed)}")
        print("restart to apply: sudo systemctl restart twinspark-controller")
    else:
        print("controller.yaml already matches the discovered values")


def cmd_link(args, api: Api):
    res = api("POST", "/api/v1/system/link-test", timeout=args.duration + 180,
              json={"mode": args.mode, "duration_s": args.duration, "port": args.port,
                    "streams": args.streams})
    if _out(api, res):
        return
    ini = res["initiator"]
    if ini.get("error"):
        sys.exit(f"link test failed: {ini['error']}")
    if ini.get("dry_run"):
        print("DRY-RUN link test (simulated numbers — real values need runtime_mode: docker)")
    for h in ini.get("per_hca") or []:
        print(f"  {h['hca']:14} {h.get('gbps') if h.get('gbps') is not None else '-'} Gb/s"
              + ("" if not h.get("rc") else f"  (rc {h['rc']}: {(h.get('tail') or '').strip()[-200:]})"))
    print(f"bandwidth : {ini.get('bandwidth_gbps', '-')} Gb/s")
    if ini.get("rtt_us_median") is not None:
        print(f"rtt       : {ini['rtt_us_median']} µs median, {ini.get('rtt_us_p99', '-')} µs p99")
    for n in ([ini["note"]] if ini.get("note") else []) + (ini.get("notes") or []):
        print(f"note      : {n}")


def cmd_metrics(args, api: Api):
    while True:
        r = api("GET", "/api/v1/system/metrics", params={"live": str(args.live).lower()})
        if _out(api, r):
            return
        if not r.get("ok"):
            print(f"metrics unavailable: {r.get('note') or r.get('error')}")
        else:
            print(f"decode {r.get('decode_tok_s', '-')} tok/s  prefill {r.get('prefill_tok_s', '-')} tok/s  "
                  f"running {r.get('requests-running', 0):.0f}  waiting {r.get('requests-waiting', 0):.0f}  "
                  f"KV {r.get('kv_cache_usage_pct', '-')}%  TTFT {r.get('avg_ttft_ms', '-')} ms  "
                  f"ITL {r.get('avg_itl_ms', '-')} ms"
                  + (f"  accept {r['spec_mean_accept_len']}" if r.get("spec_mean_accept_len") else "")
                  + (f"  prefix-hit {r['prefix_hit_rate_pct']}%" if r.get("prefix_hit_rate_pct") else ""))
        if not args.watch:
            return
        time.sleep(args.watch)


def cmd_headless(args, api: Api):
    if args.mode == "status":
        r = api("GET", "/api/v1/system/headless")
        if _out(api, r):
            return
        print(f"desired mode: {r.get('mode') or '-'}")
        for n, v in sorted(r["nodes"].items()):
            if "error" in v:
                print(f"  {n}: {v['error']}")
                continue
            d = v["desktop"]
            state = (f"desktop running ({d['desktop_rss_gib']:.1f} GiB)" if d["desktop_running"]
                     else "no desktop")
            print(f"  {n}: {state}, default target {d.get('default_target')}, "
                  f"privd {'ok' if v['privd_available'] else 'MISSING'}")
        return
    r = api("POST", "/api/v1/system/headless", json={"mode": args.mode, "now": args.now}, timeout=300)
    if _out(api, r):
        return
    print(f"mode {r['mode']}{' (applied now)' if args.now else ''}")
    for n, res in sorted(r["results"].items()):
        if "error" in res:
            print(f"  {n}: {res['error']}")
        elif res.get("dry_run"):
            print(f"  {n}: dry-run, would do: " + ", ".join(f"{s['op']}({json.dumps(s['params'])})"
                                                          for s in res["steps"]))
        else:
            print(f"  {n}: {res.get('effective')}, reclaimed {res.get('reclaimed_gib')} GiB")


def cmd_foreign(args, api: Api):
    if args.action == "ls":
        r = api("GET", "/api/v1/system/foreign")
        if _out(api, r):
            return
        found = False
        for n, lst in sorted(r.items()):
            if isinstance(lst, dict):
                print(f"node {n}: {lst.get('error')}")
                continue
            for c in lst:
                found = True
                print(f"node {n}: {c['name']:24} {c.get('image', '')}  {c.get('status', '')}")
        if not found:
            print("no inference containers outside TwinSpark")
    else:
        if not args.node or not args.name:
            sys.exit("usage: tsm foreign stop <node> <container>")
        if not args.yes and input(f"stop {args.name} on node {args.node}? [y/N] ").lower() != "y":
            return
        r = api("POST", "/api/v1/system/foreign/stop",
                json={"node": args.node, "name": args.name, "confirm": args.name}, timeout=180)
        print(f"stopped {r['stopped']}")


def cmd_resolve(args, api: Api):
    body = {"ref": args.ref, **({"image": args.image} if args.image else {})}
    r = api("POST", "/api/v1/system/resolve", json=body, timeout=120)
    if _out(api, r):
        return
    print(f"repo      : {r['repo']}")
    print(f"branch    : {r['branch']}")
    print(f"revision  : {r['revision']}")
    spec = r.get("spec")
    if spec:
        print(f"params    : {spec['num_params']:,}  layers={spec['layers']} MoE={spec['is_moe']}")
    print(f"weights   : {_gib(r.get('weight_bytes'))}")
    if r.get("image_ref"):
        print(f"image     : {r['image_ref']}")


def cmd_hardware(args, api: Api):
    r = api("GET", "/api/v1/system/hardware", params={"refresh": "true"}, timeout=120)
    if _out(api, r):
        return
    for n, f in sorted(r.items()):
        if not f or "error" in f:
            print(f"node {n}: {(f or {}).get('error', 'unknown')}")
            continue
        print(f"node {n}: {f.get('hostname')}  {f.get('gpu_name') or 'GPU ?'}  driver {f.get('driver_version')}  "
              f"kernel {f.get('kernel')}")
        print(f"         {f.get('mem_available_gib', 0):.1f}/{f.get('mem_total_gib', 0):.1f} GiB available, "
              f"disk {f.get('disk_free_gib', 0):.0f} GiB free, desktop "
              f"{'running' if f.get('desktop_running') else 'off'}, privd "
              f"{'ok' if f.get('privd_available') else 'missing'}, RDMA {', '.join(f.get('rdma_active') or []) or '-'}")


# ---- argument parsing -----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tsm", description="TwinSpark Manager — dual DGX Spark model switching")
    p.add_argument("--api", default=os.environ.get("TSM_API", DEFAULT_API), help="management API URL")
    p.add_argument("--key", default=None, help="management API key (default: $TSM_KEY or the local vault)")
    p.add_argument("--secrets-dir", default=os.environ.get("TSM_SECRETS", DEFAULT_SECRETS))
    p.add_argument("--json", action="store_true", help="print raw JSON")
    p.add_argument("--version", action="version", version=f"tsm {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    def cmd(name, fn, help_, aliases=()):
        s = sub.add_parser(name, help=help_, aliases=list(aliases))
        s.set_defaults(fn=fn)
        return s

    s = cmd("init", cmd_init, "create secrets in the local vault")
    s.add_argument("--role", choices=["controller", "agent"], default="controller")
    s.add_argument("--agent-token")
    s.add_argument("--backend-api-key")
    s.add_argument("--hf-token")
    s.add_argument("--show", action="store_true", help="print secret values")

    s = cmd("serve", cmd_serve, "run a service (used by systemd)")
    s.add_argument("what", choices=["controller", "agent", "privd", "termd"])
    s.add_argument("--config", help="controller.yaml / agent.yaml")
    s.add_argument("--socket", default="/run/twinspark/privd.sock")
    s.add_argument("--group", default="twinspark")
    s.add_argument("--remote-policy", help="privd: path of the remote-management policy "
                                           "(default /etc/twinspark/remote-policy.json)")
    s.add_argument("--unsafe-policy-owner", action="store_true",
                   help="privd: accept a policy file not owned by root (sandbox installs and tests only)")

    cmd("status", cmd_status, "active model, routes, memory, live serving metrics")
    cmd("profiles", cmd_profiles, "list profiles", aliases=["ls"])
    s = cmd("show", cmd_show, "one profile: settings, revisions, memory fit")
    s.add_argument("profile")
    s = cmd("fit", cmd_fit, "memory fit and estimated KV pool")
    s.add_argument("profile")
    s = cmd("pin", cmd_pin, "resolve the draft to a commit sha + image digest (activatable revision)")
    s.add_argument("profile")
    s.add_argument("--model-ref", help="branch, 40-char sha or org/repo@ref (default: keep / main)")
    s.add_argument("--image", help="registry image (pinned to its multi-arch digest) or local tag")
    s.add_argument("--local-image", help="force a local image tag/ID (must be identical on both nodes)")
    s.add_argument("--note")
    s = cmd("edit", cmd_edit, "edit a profile's draft as JSON in $EDITOR")
    s.add_argument("profile")
    s = cmd("export", cmd_export, "print a profile's draft as JSON (shareable recipe)")
    s.add_argument("profile")
    s.add_argument("--with-identity", action="store_true")
    s = cmd("delete", cmd_delete, "delete a profile (keeps model files)")
    s.add_argument("profile")
    s = cmd("plan", cmd_plan, "show the exact docker/vllm commands without running them")
    s.add_argument("profile")
    s.add_argument("revision", nargs="?", default="latest")
    s.add_argument("--draft", action="store_true", help="render the unpinned working draft")
    s = cmd("activate", cmd_activate, "switch to a profile (stage, stop old, start, verify, route)",
            aliases=["switch"])
    s.add_argument("profile")
    s.add_argument("revision", nargs="?", default="latest")
    s.add_argument("--no-wait", action="store_true")
    s = cmd("prepare", cmd_prepare, "check images/patches and stage a recipe without switching")
    s.add_argument("profile")
    s.add_argument("revision", nargs="?", default="latest")
    s.add_argument("--no-wait", action="store_true")
    s = cmd("split", cmd_split, "combine two recipes into one split profile")
    s.add_argument("name")
    s.add_argument("node_a")
    s.add_argument("node_b")
    s.add_argument("--alias-a", default="default")
    s.add_argument("--alias-b", default="secondary")
    s = cmd("cancel", cmd_cancel, "cancel a running activation/staging job")
    s.add_argument("job")
    cmd("stop", cmd_stop, "drain and stop the active model")
    s = cmd("jobs", cmd_jobs, "recent jobs")
    s.add_argument("--kind", choices=["activation", "rollback", "recovery", "stage", "prepare", "stop"])
    s.add_argument("--limit", type=int, default=30)
    s = cmd("job", cmd_job, "follow / show one job")
    s.add_argument("job")
    s.add_argument("--no-wait", action="store_true")
    s = cmd("logs", cmd_logs, "container logs (default: the active deployment on both nodes)")
    s.add_argument("node", nargs="?", choices=["A", "B"])
    s.add_argument("container", nargs="?")
    s.add_argument("--tail", type=int, default=200)

    s = cmd("cookbook", cmd_cookbook, "recipes: built-in, community (eugr), import")
    s.add_argument("action", choices=["list", "show", "import", "community", "import-recipe"])
    s.add_argument("name", nargs="?", help="recipe name, or a file/URL for import-recipe")
    s.add_argument("--as", dest="as_name", help="profile name to create")
    s.add_argument("--preview", action="store_true", help="import-recipe: show the mapping only")
    s.add_argument("-e", "--set", action="append", metavar="KEY=VALUE",
                   help="import-recipe: override an eugr template default (like run-recipe -e)")
    s.add_argument("--refresh", action="store_true", help="community: bypass the 10 min cache")

    s = cmd("models", cmd_models, "model files on both nodes: ls, stage, rm")
    s.add_argument("action", choices=["ls", "stage", "rm"])
    s.add_argument("target", nargs="?", help="org/repo[@revision]")
    s.add_argument("--nodes", help="comma-separated, default: all")
    s.add_argument("--include", action="append", help="stage: extra file globs (e.g. 'dflash/*')")
    s.add_argument("--force", action="store_true", help="rm: even if profiles reference it")
    s.add_argument("--preview", action="store_true", help="rm: only show what would be freed")
    s.add_argument("-y", "--yes", action="store_true")
    s.add_argument("--no-wait", action="store_true")

    s = cmd("mods", cmd_mods, "vLLM patch mods (eugr format: a directory with run.sh)")
    s.add_argument("action", choices=["ls", "install", "import-eugr", "rm"])
    s.add_argument("path", nargs="?", help="mod dir/zip/tar, spark-vllm-docker checkout, or mod name")
    s.add_argument("--name")
    s.add_argument("--only", help="import-eugr: comma-separated mod names")
    s.add_argument("--nodes")

    cmd("doctor", cmd_doctor, "check everything a fast, stable dual-Spark setup needs")
    s = cmd("rdma", cmd_rdma, "discover RoCE devices / GID index and compare with controller.yaml")
    s.add_argument("--apply", action="store_true", help="write the discovered values into controller.yaml")
    s.add_argument("--config", help="controller.yaml (default /etc/twinspark/controller.yaml)")
    s.add_argument("--root", default="/")
    s = cmd("link", cmd_link, "QSFP link test between the nodes")
    s.add_argument("--mode", choices=["tcp", "rdma"], default="tcp")
    s.add_argument("--duration", type=float, default=5.0)
    s.add_argument("--port", type=int, default=29511)
    s.add_argument("--streams", type=int, default=4)
    s = cmd("metrics", cmd_metrics, "serving metrics of the active model")
    s.add_argument("--live", action="store_true", help="scrape now instead of the last sample")
    s.add_argument("-w", "--watch", type=float, nargs="?", const=5.0, help="repeat every N seconds")
    s = cmd("headless", cmd_headless, "desktop/headless mode on both nodes (via tsm-privd)")
    s.add_argument("mode", choices=["status", "desktop", "headless-safe", "headless-max"])
    s.add_argument("--now", action="store_true", help="also stop/start the display manager right away")
    s = cmd("foreign", cmd_foreign, "vLLM containers started outside TwinSpark")
    s.add_argument("action", choices=["ls", "stop"])
    s.add_argument("node", nargs="?", choices=["A", "B"])
    s.add_argument("name", nargs="?")
    s.add_argument("-y", "--yes", action="store_true")
    s = cmd("resolve", cmd_resolve, "resolve org/model@branch to a commit sha (+ image digest)")
    s.add_argument("ref")
    s.add_argument("--image")
    cmd("hardware", cmd_hardware, "hardware facts of both nodes")

    from . import cli_qsfp, cli_remote, cli_setup
    cli_setup.add_parsers(sub, cmd)
    cli_remote.add_parsers(sub, cmd)
    cli_qsfp.add_parsers(sub, cmd)
    from . import demo
    demo.add_parsers(sub, cmd)
    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 0
    if args.fn is cmd_serve and args.what in ("controller", "agent", "termd") and not args.config:
        p.error("serve controller|agent|termd needs --config")
    api = None if getattr(args.fn, "local", False) or args.fn in (cmd_init, cmd_serve) \
        else Api(args.api, _key(args), args.json)
    args.fn(args, api)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
