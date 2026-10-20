"""The documentation site (mkdocs.yml): every page in docs/ is in the navigation, and the generated pages are
current."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _nav_files(nav) -> set[str]:
    out: set[str] = set()
    for item in nav:
        for value in (item.values() if isinstance(item, dict) else [item]):
            if isinstance(value, list):
                out |= _nav_files(value)
            elif isinstance(value, str):
                out.add(value)
    return out


def test_every_page_is_in_the_navigation():
    cfg = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    listed = _nav_files(cfg["nav"])
    pages = {p.relative_to(ROOT / "docs").as_posix() for p in (ROOT / "docs").rglob("*.md")
             if "notes/examples" not in p.as_posix()}
    assert pages - listed == set(), "add these pages to mkdocs.yml nav"
    assert listed - pages == set(), "mkdocs.yml names pages that do not exist"


def test_the_performance_lab_table_matches_the_recipes():
    r = subprocess.run([sys.executable, str(ROOT / "scripts/build_perf_lab.py"), "--check"], capture_output=True,
                       text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr


def test_links_out_of_docs_point_at_github():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import mkdocs_hooks
    finally:
        sys.path.pop(0)

    class Page:
        class file:
            src_uri = "integrations/n8n.md"
    md = "[a](../gateway.md#x) [b](../../README.md) [c](../../catalog/README.md#format) [d](https://x.example)"
    out = mkdocs_hooks.on_page_markdown(md, Page, None, None)
    assert out == ("[a](../gateway.md#x) [b](https://github.com/ChopCookies/Twinspark-Manager/blob/main/README.md) "
                   "[c](https://github.com/ChopCookies/Twinspark-Manager/blob/main/catalog/README.md#format) "
                   "[d](https://x.example)")
