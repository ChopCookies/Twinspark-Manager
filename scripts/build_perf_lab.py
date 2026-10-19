#!/usr/bin/env python3
"""Write the results table of docs/perf-lab.md from the recipes' measured numbers.

    python scripts/build_perf_lab.py           # rewrite the generated part of docs/perf-lab.md
    python scripts/build_perf_lab.py --check   # exit 1 when it is out of date (tests/test_perf_lab.py)

Only the part between the two markers is generated; the rest of the page is written by hand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from twinspark import cookbook  # noqa: E402

PAGE = ROOT / "docs" / "perf-lab.md"
START, END = "<!-- results:start (scripts/build_perf_lab.py) -->", "<!-- results:end -->"
LABELS = {
    "single_stream_tok_s": "one request, tok/s", "aggregate_c4_tok_s": "4 at once, tok/s total",
    "aggregate_c6_tok_s": "6 at once, tok/s total", "aggregate_c8_tok_s": "8 at once, tok/s total",
    "structured_tok_s": "structured output, tok/s", "ttft_s": "time to first token, s", "kv_pool": "KV cache",
    "boot": "start-up", "context": "context", "speculation": "speculation",
    "gpu_memory_utilization": "gpu-memory-utilization", "note": "note",
}
ORDER = list(LABELS)


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def generate() -> str:
    rows = []
    for r in cookbook.list_recipes():
        if "error" in r:
            continue
        modes = r.get("modes") or [{"name": None, "title": None, "measured": r.get("measured") or {}}]
        for m in modes:
            measured = m.get("measured") or {}
            if not measured and m.get("name") is None:
                continue
            facts = sorted(measured.items(), key=lambda kv: ORDER.index(kv[0]) if kv[0] in ORDER else 99)
            numbers = "<br>".join(f"{LABELS.get(k, k.replace('_', ' '))}: {_cell(v)}" for k, v in facts) \
                or "not measured yet"
            author = _cell(r.get("author") or "")
            source = f"[{author or 'source'}]({r['url']})" if r.get("url") else author
            rows.append(f"| {_cell(r['title'])} | `{r['name']}` | {_cell(m.get('title') or '—')} | {r['topology']} "
                        f"| {r['verification']} | {numbers} | {source} |")
    head = ("| Recipe | Name | Mode | Nodes | Status | Measured | Measured by |\n"
            "|---|---|---|---|---|---|---|")
    return f"{START}\n{head}\n" + "\n".join(rows) + f"\n{END}"


def render(page: str) -> str:
    before, rest = page.split(START, 1)
    _old, after = rest.split(END, 1)
    return before + generate() + after


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    page = PAGE.read_text(encoding="utf-8")
    new = render(page)
    if args.check:
        if new != page:
            print("docs/perf-lab.md is out of date — run `python scripts/build_perf_lab.py`", file=sys.stderr)
            return 1
        return 0
    PAGE.write_text(new, encoding="utf-8")
    print(f"wrote {PAGE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
