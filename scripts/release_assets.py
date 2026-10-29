"""Prepare the files of a GitHub release (used by .github/workflows/release.yml).

    python scripts/release_assets.py prepare --tag v0.8.0 --dist dist --notes release-notes.md

1. The tag must name the version in pyproject.toml (``v`` + version).
2. ``dist/`` must hold that version's wheel. ``constraints/release.txt`` is copied next to it as
   ``constraints-release.txt``.
3. ``dist/SHA256SUMS`` lists every file in ``dist/``. `tsm update` installs nothing that is not listed there.
4. The release notes are that version's section of CHANGELOG.md.

Nothing is uploaded here; the workflow does that with ``gh release create``.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twinspark.releases import CONSTRAINTS, SUMS, TAG_RE, is_prerelease, wheel_name  # noqa: E402


def project_version(root: Path = ROOT) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def check_tag(tag: str, version: str) -> None:
    if not TAG_RE.match(tag):
        raise SystemExit(f"{tag!r} is not a release tag (vX.Y.Z, optionally aN/bN/rcN)")
    if tag != f"v{version}":
        raise SystemExit(f"tag {tag} does not match pyproject.toml's version {version}: bump the version first")


def notes(changelog: str, version: str) -> str:
    """The body of ``## <version> — …`` up to the next ``## `` heading."""
    m = re.search(rf"(?m)^## {re.escape(version)}(?: .*)?$", changelog)
    if not m:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    rest = changelog[m.end():]
    nxt = re.search(r"(?m)^## ", rest)
    body = (rest[: nxt.start()] if nxt else rest).strip()
    if not body:
        raise SystemExit(f"the CHANGELOG.md section for {version} is empty")
    title = m.group(0)[3:].strip()
    return f"**{title}**\n\n{body}\n"


def write_sums(dist: Path) -> str:
    lines = []
    for f in sorted(p for p in dist.iterdir() if p.is_file() and p.name != SUMS):
        lines.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}")
    text = "\n".join(lines) + "\n"
    (dist / SUMS).write_text(text, encoding="utf-8", newline="\n")
    return text


def prepare(tag: str, dist: Path, notes_out: Path, root: Path = ROOT) -> dict:
    version = project_version(root)
    check_tag(tag, version)
    wheel = dist / wheel_name(version)
    if not wheel.is_file():
        raise SystemExit(f"{wheel} is missing — build it first (python -m build)")
    others = [p.name for p in dist.glob("twinspark_manager-*.whl") if p.name != wheel.name]
    if others:
        raise SystemExit(f"dist/ holds other wheels too: {', '.join(others)}")
    shutil.copyfile(root / "constraints" / "release.txt", dist / CONSTRAINTS)
    sums = write_sums(dist)
    notes_out.write_text(notes((root / "CHANGELOG.md").read_text(encoding="utf-8"), version),
                         encoding="utf-8", newline="\n")
    return {"version": version, "prerelease": is_prerelease(version), "sums": sums}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prepare")
    s.add_argument("--tag", required=True)
    s.add_argument("--dist", type=Path, default=ROOT / "dist")
    s.add_argument("--notes", type=Path, default=ROOT / "release-notes.md")
    s = sub.add_parser("check-tag", help="only check that the tag matches pyproject.toml")
    s.add_argument("--tag", required=True)
    args = p.parse_args(argv)
    if args.cmd == "check-tag":
        check_tag(args.tag, project_version())
        print(f"{args.tag} matches pyproject.toml")
        return 0
    out = prepare(args.tag, args.dist, args.notes)
    print(out["sums"], end="")
    print(f"prerelease={'true' if out['prerelease'] else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
