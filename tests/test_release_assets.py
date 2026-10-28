"""scripts/release_assets.py: what the release workflow publishes."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
from pathlib import Path

import pytest

from twinspark import __version__, releases

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_assets", ROOT / "scripts" / "release_assets.py")
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)


def test_the_tag_must_name_the_project_version():
    assert ra.project_version() == __version__
    ra.check_tag(f"v{__version__}", __version__)
    with pytest.raises(SystemExit, match="does not match"):
        ra.check_tag("v0.0.1", __version__)
    with pytest.raises(SystemExit, match="not a release tag"):
        ra.check_tag("latest", __version__)


def test_release_notes_are_the_changelog_section():
    text = "# Changelog\n\n## 1.2.0 — 2026-11-01 — title\n\n- a\n- b\n\n## 1.1.0 — older\n\n- c\n"
    out = ra.notes(text, "1.2.0")
    assert out.startswith("**1.2.0 — 2026-11-01 — title**") and "- b" in out and "- c" not in out
    with pytest.raises(SystemExit, match="no section"):
        ra.notes(text, "9.9.9")
    with pytest.raises(SystemExit, match="empty"):
        ra.notes("## 1.0.0\n\n## 0.9.0\n- x\n", "1.0.0")
    assert ra.notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), __version__)   # this version has one


def test_prepare_writes_checksums_that_the_updater_accepts(tmp_path):
    root = tmp_path / "repo"
    (root / "constraints").mkdir(parents=True)
    shutil.copy(ROOT / "pyproject.toml", root / "pyproject.toml")
    shutil.copy(ROOT / "CHANGELOG.md", root / "CHANGELOG.md")
    (root / "constraints" / "release.txt").write_text("fastapi==1\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / releases.wheel_name(__version__)
    wheel.write_bytes(b"wheel bytes")
    (dist / f"twinspark_manager-{__version__}.tar.gz").write_bytes(b"sdist")
    out = ra.prepare(f"v{__version__}", dist, tmp_path / "notes.md", root=root)
    sums = releases.parse_sums((dist / releases.SUMS).read_text())
    assert set(sums) == {wheel.name, releases.CONSTRAINTS, f"twinspark_manager-{__version__}.tar.gz"}
    assert sums[wheel.name] == hashlib.sha256(b"wheel bytes").hexdigest()
    assert (dist / releases.CONSTRAINTS).read_text() == "fastapi==1\n"
    assert out["prerelease"] == releases.is_prerelease(__version__)
    assert (tmp_path / "notes.md").read_text(encoding="utf-8").startswith(f"**{__version__}")
    (dist / "twinspark_manager-0.0.1-py3-none-any.whl").write_bytes(b"old")
    with pytest.raises(SystemExit, match="other wheels"):
        ra.prepare(f"v{__version__}", dist, tmp_path / "notes.md", root=root)


def test_prepare_needs_the_wheel(tmp_path):
    with pytest.raises(SystemExit, match="missing"):
        ra.prepare(f"v{__version__}", tmp_path, tmp_path / "n.md")


def test_the_release_workflow_runs_the_script_and_publishes_all_files():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "scripts/release_assets.py check-tag" in wf and "scripts/release_assets.py prepare" in wf
    assert 'tags: ["v*"]' in wf and "gh release create" in wf and "dist/*" in wf
    assert "vars.PUBLISH_PYPI == 'true'" in wf          # PyPI only when the owner switched it on
