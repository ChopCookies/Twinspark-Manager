"""Every relative link in the Markdown files points at a file or directory that exists."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)\s]+)\)")
SKIP_DIRS = {".git", ".venv", "build", "node_modules", ".pytest_cache"}


def markdown_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.md")
                  if not SKIP_DIRS & set(p.relative_to(ROOT).parts) and "egg-info" not in str(p))


@pytest.mark.parametrize("md", markdown_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(md):
    broken = []
    for target in LINK.findall(md.read_text(encoding="utf-8")):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue                                            # http(s):, mailto:, in-page anchors
        path = target.split("#", 1)[0]
        if path and not (md.parent / path).exists():
            broken.append(target)
    assert not broken, f"{md.relative_to(ROOT)}: {broken}"


def test_docs_use_placeholders_not_someones_home_directory():
    for md in markdown_files():
        found = re.findall(r"/home/[a-z_][a-z0-9_-]*/", md.read_text(encoding="utf-8"))
        assert not found, f"{md.relative_to(ROOT)}: {found} — use ~/ or <user>"
