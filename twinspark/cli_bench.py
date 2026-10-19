"""``tsm bench``: measure the served model through the gateway apps use, and keep the result on node A.

``tsm bench run`` runs one of the presets (``max-throughput``, ``latency-slo``, ``context``, ``quality``,
``fixed``) against an alias of the gateway and stores the result document against the revision it measured;
``tsm bench list`` and ``tsm bench show`` read the stored results. On a workstation the client GUI's
Benchmarks page does the same through its SSH forward.

The gateway address defaults to node A's own (``TSM_GATEWAY``, else the port in controller.yaml on
loopback); the inference key comes from ``--inference-key``, ``TSM_INFERENCE_KEY`` or this node's vault. The
management key is only used for node A's API (status, storage), never sent to the gateway.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from .client.bench import PRESETS, Options, Run, Target


def _gateway(args) -> str:
    if args.gateway:
        return args.gateway.rstrip("/") + ("" if args.gateway.rstrip("/").endswith("/v1") else "/v1")
    if os.environ.get("TSM_GATEWAY"):
        return os.environ["TSM_GATEWAY"].rstrip("/")
    port, tls = 8000, False
    cfg = Path(args.secrets_dir).parent / "controller.yaml"
    try:
        from .client.discovery import parse_controller_yaml
        doc = parse_controller_yaml(cfg.read_text(encoding="utf-8"))
        port, tls = doc["gateway_port"], doc["gateway_tls"]
    except Exception:  # noqa: BLE001 - not node A, or not readable for this user: the default port
        pass
    return f"{'https' if tls else 'http'}://127.0.0.1:{port}/v1"


def _inference_key(args) -> Optional[str]:
    if args.inference_key:
        return args.inference_key
    if os.environ.get("TSM_INFERENCE_KEY"):
        return os.environ["TSM_INFERENCE_KEY"]
    try:
        from .security import SecretsVault
        return SecretsVault(args.secrets_dir).get("inference_api_key")
    except Exception:  # noqa: BLE001 - vault unreadable for this user
        return None


def options_from(args) -> Options:
    kw: dict[str, Any] = {"preset": args.preset}
    for name in ("input_tokens", "output_tokens", "cache", "min_concurrency", "max_concurrency",
                 "requests_per_level", "slo_ttft_ms", "slo_tpot_ms", "needles", "seed"):
        value = getattr(args, name, None)
        if value is not None:
            kw[name] = value
    for name in ("levels", "contexts", "context_concurrency"):
        value = getattr(args, name, None)
        if value:
            kw[name] = tuple(int(x) for x in str(value).split(","))
    return Options(**kw)


def _status_reader(api):
    async def status() -> dict:
        try:
            st = await asyncio.to_thread(api, "GET", "/api/v1/status", ok=tuple(range(400, 600)), timeout=10)
        except SystemExit:                 # Api exits when node A is unreachable: no reading, not the end of the run
            return {}
        return st if isinstance(st, dict) else {}

    async def sample() -> dict[str, dict[str, Any]]:
        tel = (await status()).get("telemetry") or {}
        return {n: {"mem_available_gib": t.get("mem_available_gib"), "power_w": (t.get("gpu") or {}).get("power_w")}
                for n, t in tel.items() if isinstance(t, dict)}

    async def busy() -> bool:
        st = await status()
        return bool(st.get("busy") or st.get("current_job"))
    return sample, busy


def _print_points(points: list[dict]) -> None:
    from .cli import _table
    rows = []
    for p in points:
        ttft, tpot = p.get("ttft_ms") or {}, p.get("tpot_ms") or {}
        rows.append([p.get("context", "") or "", p["concurrency"], p["requests"], f"{p['output_tok_s']:,}",
                     ttft.get("p50"), ttft.get("p95"), tpot.get("p50"), tpot.get("p95"),
                     f"{(p.get('error_rate') or 0):.0%}"])
    if any(r[0] for r in rows):
        _table(rows, ["context", "conc", "reqs", "out tok/s", "TTFT p50", "TTFT p95", "TPOT p50", "TPOT p95", "errors"])
    else:
        _table([r[1:] for r in rows], ["conc", "reqs", "out tok/s", "TTFT p50", "TTFT p95", "TPOT p50", "TPOT p95",
                                       "errors"])


async def _run(args, api) -> dict[str, Any]:
    base = _gateway(args)
    host = urlsplit(base).hostname or ""
    opts = options_from(args)
    top = max([opts.max_concurrency, *opts.levels, *opts.context_concurrency])
    # node A's own certificate names the node, not 127.0.0.1; on loopback the traffic never leaves the machine
    loopback = host in ("127.0.0.1", "::1", "localhost")
    http = httpx.AsyncClient(timeout=httpx.Timeout(900, connect=15), trust_env=False, verify=not loopback,
                             limits=httpx.Limits(max_connections=top + 8))
    sample, busy = _status_reader(api) if api is not None else (None, None)
    run = Run(Target(base, _inference_key(args), args.alias, http=http), opts, sampler=sample, busy=busy)
    task = asyncio.create_task(run.execute())
    shown = 0
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=2)
            if not args.json:
                for p in run.points[shown:]:
                    print(f"  concurrency {p['concurrency']:>4}: {p['output_tok_s']:>10,} output tok/s, "
                          f"TTFT p95 {((p.get('ttft_ms') or {}).get('p95') or 0):,.0f} ms", file=sys.stderr)
                shown = len(run.points)
        return task.result()
    except (KeyboardInterrupt, asyncio.CancelledError):
        run.cancel.set()
        return await task
    finally:
        await http.aclose()


def cmd_bench_run(args, api) -> None:
    if not args.json:
        print(f"benchmark {args.preset} on {args.alias} through {_gateway(args)} …", file=sys.stderr)
    started = time.monotonic()
    result = asyncio.run(_run(args, api))
    stored = None
    if args.store and result.get("points") and result.get("revision_id"):
        stored = api("POST", "/api/v1/benchmarks", json=result, ok=(404, 413, 422), timeout=60)
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps({"result": result, "stored": stored}, indent=2, default=str))
    else:
        print(f"\n{result['summary'].get('text') or result.get('invalid_reason')}"
              f"  ({time.monotonic() - started:.0f} s, revision {result.get('revision_id') or '?'})")
        if result["points"]:
            print()
            _print_points(result["points"])
        for n in result.get("needles") or []:
            print(f"retrieval at {n['context_tokens']:,} tokens: {n['found']}/{n['needles']}")
        for c in result.get("checks") or []:
            print(f"{c.get('title') or c.get('id')}: {c.get('status')}  {c.get('detail') or ''}".rstrip())
        if not result.get("valid"):
            print(f"NOT VALID: {result.get('invalid_reason')}")
        if isinstance(stored, dict) and stored.get("id"):
            print(f"stored on node A as {stored['id']} (tsm bench show {stored['id']})")
        elif isinstance(stored, dict):
            print(f"node A did not store it: {stored.get('detail')}")
        if args.output:
            print(f"wrote {args.output}")
    if not result.get("valid"):
        sys.exit(1)


def cmd_bench_list(args, api) -> None:
    from .cli import _ago, _out, _table
    params = {k: v for k, v in (("profile", args.profile), ("revision", args.revision)) if v}
    rows = api("GET", "/api/v1/benchmarks", params={**params, "limit": args.limit})
    if _out(api, rows):
        return
    if not rows:
        print("no benchmark results yet — tsm bench run")
        return
    _table([[r["id"], r.get("profile"), r.get("label"), r.get("preset"), "yes" if r.get("valid") else "NO",
             _ago(r.get("created_at")), ((r.get("summary") or {}).get("text") or r.get("invalid_reason") or "")[:70]]
            for r in rows], ["id", "profile", "revision", "preset", "valid", "when", "result"])


def cmd_bench_show(args, api) -> None:
    from .cli import _out
    row = api("GET", f"/api/v1/benchmarks/{args.id}")
    if _out(api, row):
        return
    res = row.get("result") or {}
    print(f"{row['id']}: {row.get('profile')} {row.get('label')} ({row.get('revision_id')}), "
          f"preset {row.get('preset')}, alias {row.get('alias')}")
    print(f"evidence: {row.get('evidence')}"
          + ("" if row.get("valid") else f" — NOT VALID: {row.get('invalid_reason')}"))
    print((row.get("summary") or {}).get("text") or "")
    if res.get("points"):
        print()
        _print_points(res["ladder"] if res.get("ladder") else res["points"])
    for node, t in (res.get("telemetry") or {}).items():
        print(f"node {node}: lowest free memory {t.get('min_mem_available_gib')} GiB, mean GPU power "
              f"{t.get('mean_power_w')} W")


def add_parsers(sub, cmd) -> None:
    b = sub.add_parser("bench", help="benchmark the served model through the gateway; results stay on node A")
    bs = b.add_subparsers(dest="bench_cmd", metavar="<action>", required=True)
    s = bs.add_parser("run", help="run a preset against an alias", formatter_class=argparse.RawDescriptionHelpFormatter,
                      description="Presets:\n  max-throughput  double the load until throughput stops growing\n"
                                  "  latency-slo     the highest concurrency that meets TTFT/TPOT targets\n"
                                  "  context         the context ladder up to the deployed limit, with retrieval\n"
                                  "  quality         retrieval at long context and a tool call round trip\n"
                                  "  fixed           the same levels every time (to compare revisions)")
    s.set_defaults(fn=cmd_bench_run)
    s.add_argument("preset", nargs="?", default="max-throughput", choices=PRESETS)
    s.add_argument("--alias", default="default")
    s.add_argument("--gateway", help="gateway base URL (default: TSM_GATEWAY, else this node's gateway port)")
    s.add_argument("--inference-key", help="the gateway key (default: TSM_INFERENCE_KEY or this node's vault)")
    s.add_argument("--input-tokens", type=int)
    s.add_argument("--output-tokens", type=int)
    s.add_argument("--cache", choices=["cold", "warm"], help="cold: a fresh prompt per request (default)")
    s.add_argument("--min-concurrency", type=int)
    s.add_argument("--max-concurrency", type=int)
    s.add_argument("--requests-per-level", type=int, help="default 4 x the concurrency, at least 8")
    s.add_argument("--levels", help="fixed preset: concurrency levels, e.g. 1,4,16")
    s.add_argument("--slo-ttft-ms", type=float)
    s.add_argument("--slo-tpot-ms", type=float)
    s.add_argument("--contexts", help="context preset: lengths, e.g. 8192,32768,131072")
    s.add_argument("--context-concurrency", help="context preset: concurrency levels, e.g. 1,2,4")
    s.add_argument("--needles", type=int, help="facts hidden in the long prompt for the retrieval check")
    s.add_argument("--seed", type=int, help="repeat a run's prompts exactly")
    s.add_argument("--no-store", dest="store", action="store_false", help="do not keep the result on node A")
    s.add_argument("-o", "--output", help="also write the result document to this file")
    s = bs.add_parser("list", help="stored results")
    s.set_defaults(fn=cmd_bench_list)
    s.add_argument("--profile")
    s.add_argument("--revision", help="revision label or id")
    s.add_argument("--limit", type=int, default=30)
    s = bs.add_parser("show", help="one stored result with its points")
    s.set_defaults(fn=cmd_bench_show)
    s.add_argument("id")
