"""TwinSpark releases on GitHub: which versions exist and whether a download is the published file.

A release is a ``vX.Y.Z`` tag on the repository. The release workflow (``.github/workflows/release.yml``)
attaches these assets to it:

* ``twinspark_manager-X.Y.Z-py3-none-any.whl`` — the program
* ``constraints-release.txt`` — the dependency versions that release was tested with
* ``SHA256SUMS`` — one ``sha256sum`` line per other asset

`tsm update` installs only files listed in ``SHA256SUMS`` whose hash matches. Where GitHub reports a digest
for an asset as well, it must agree. There is no separate signature: the trust anchor is this repository on
GitHub, reached over HTTPS. Every URL is built here from the fixed repository name, a validated tag and a
validated asset name. Nothing a caller passes can point the update at another server.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import __version__

REPO = "ChopCookies/Twinspark-Manager"
API = f"https://api.github.com/repos/{REPO}/releases"
DOWNLOAD = f"https://github.com/{REPO}/releases/download"
SUMS = "SHA256SUMS"
CONSTRAINTS = "constraints-release.txt"
PROJECT = "twinspark-manager"

_VERSION_RE = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:(a|b|rc)(\d{1,4}))?\Z")
TAG_RE = re.compile(r"^v\d{1,4}\.\d{1,4}\.\d{1,4}(?:(?:a|b|rc)\d{1,4})?\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
_PRE_RANK = {"a": 0, "b": 1, "rc": 2}


class ReleaseError(RuntimeError):
    """No usable release, or a download that is not the published file.

    ``kind`` tells the cases apart for the GUI and the CLI: ``none-published`` (GitHub answered, there is simply
    no release yet: nothing is wrong here), ``unreachable`` (network, DNS, TLS), ``rate-limited``, ``invalid``
    (a release or a download that is not what the release workflow publishes) and ``error``."""

    def __init__(self, message: str, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


def parse_version(text: str) -> Optional[tuple[int, int, int, int, int]]:
    """``0.8.0`` / ``v0.8.0`` / ``0.8.0rc1`` → a sortable key; anything else → None.

    A pre-release sorts before its final version (``0.8.0rc1 < 0.8.0``)."""
    m = _VERSION_RE.match(str(text).strip())
    if not m:
        return None
    return (int(m[1]), int(m[2]), int(m[3]), _PRE_RANK.get(m[4] or "", 3), int(m[5] or 0))


def is_prerelease(version: str) -> bool:
    key = parse_version(version)
    return key is not None and key[3] < 3


def newer(candidate: str, than: str) -> bool:
    a, b = parse_version(candidate), parse_version(than)
    return a is not None and (b is None or a > b)


def wheel_name(version: str) -> str:
    return f"twinspark_manager-{version}-py3-none-any.whl"


def asset_url(tag: str, name: str) -> str:
    if not TAG_RE.match(tag):
        raise ReleaseError(f"not a release tag: {tag!r}")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) or name.startswith("."):
        raise ReleaseError(f"not a release asset name: {name!r}")
    return f"{DOWNLOAD}/{tag}/{name}"


@dataclass
class Asset:
    name: str
    size: int = 0
    digest: Optional[str] = None          # GitHub's "sha256:…" when it reports one


@dataclass
class Release:
    tag: str
    version: str
    prerelease: bool = False
    published_at: Optional[str] = None
    notes: str = ""
    html_url: str = ""
    assets: dict[str, Asset] = field(default_factory=dict)

    @property
    def wheel(self) -> str:
        return wheel_name(self.version)


def parse_release(doc: dict[str, Any]) -> Optional[Release]:
    """One entry of GitHub's ``/releases`` list → a Release, or None for drafts and other tags."""
    tag = str(doc.get("tag_name") or "")
    if doc.get("draft") or not TAG_RE.match(tag):
        return None
    version = tag[1:]
    assets = {}
    for a in doc.get("assets") or []:
        name = str(a.get("name") or "")
        if name:
            digest = a.get("digest")
            assets[name] = Asset(name=name, size=int(a.get("size") or 0),
                                 digest=digest if isinstance(digest, str) else None)
    return Release(tag=tag, version=version, prerelease=bool(doc.get("prerelease")) or is_prerelease(version),
                   published_at=doc.get("published_at"), notes=str(doc.get("body") or "")[:20000],
                   html_url=str(doc.get("html_url") or ""), assets=assets)


def parse_releases(docs: list[dict[str, Any]]) -> list[Release]:
    out = [r for r in (parse_release(d) for d in docs if isinstance(d, dict)) if r is not None]
    return sorted(out, key=lambda r: parse_version(r.version) or (0, 0, 0, 0, 0), reverse=True)


def choose(releases: list[Release], version: Optional[str] = None, allow_prerelease: bool = False) -> Release:
    """The newest installable release, or exactly ``version`` (which may be older: that is how to go back)."""
    if version:
        want = version[1:] if version.startswith("v") else version
        if parse_version(want) is None:
            raise ReleaseError(f"not a version: {version!r} (expected e.g. 0.8.0)")
        for r in releases:
            if r.version == want:
                _require_assets(r)
                return r
        raise ReleaseError(f"there is no release {want} on GitHub ({REPO})", "none-published")
    for r in releases:
        if r.prerelease and not allow_prerelease:
            continue
        if r.wheel in r.assets and SUMS in r.assets:
            return r
    if not releases:
        raise ReleaseError(f"no TwinSpark release is published on GitHub ({REPO}) yet — GitHub answered, so this is "
                           f"not a connection problem; releases appear when a vX.Y.Z tag is pushed", "none-published")
    raise ReleaseError(f"none of the {len(releases)} release(s) on GitHub ({REPO}) has the files `tsm update` "
                       f"installs (made by the release workflow)" + ("" if allow_prerelease else
                                                                     " — or only pre-releases do"), "none-published")


def _require_assets(r: Release) -> None:
    missing = [n for n in (r.wheel, CONSTRAINTS, SUMS) if n not in r.assets]
    if missing:
        raise ReleaseError(f"release {r.tag} lacks {', '.join(missing)} — it was not made by the release "
                           f"workflow and cannot be installed by `tsm update`")


def parse_sums(text: str) -> dict[str, str]:
    """``sha256sum`` output (``<hex>  <name>`` or ``<hex> *<name>``) → {name: hex}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S+)", line)
        if not m:
            raise ReleaseError(f"{SUMS}: unreadable line {line[:80]!r}")
        name, digest = m[2], m[1].lower()
        if out.get(name, digest) != digest:
            raise ReleaseError(f"{SUMS} lists {name} twice with different hashes")
        out[name] = digest
    return out


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plan(release: Release, sums_text: str) -> dict[str, Any]:
    """What an update installs: the wheel and the constraints, each with the hash it must have."""
    _require_assets(release)
    sums = parse_sums(sums_text)
    files = {}
    for key, name in (("wheel", release.wheel), ("constraints", CONSTRAINTS)):
        digest = sums.get(name)
        if not digest:
            raise ReleaseError(f"{SUMS} of {release.tag} does not list {name}")
        gh = release.assets[name].digest
        if gh and gh.lower() != f"sha256:{digest}":
            raise ReleaseError(f"{name}: GitHub reports {gh}, {SUMS} says sha256:{digest} — not installing")
        files[key] = {"name": name, "sha256": digest, "size": release.assets[name].size,
                      "url": asset_url(release.tag, name)}
    return {"tag": release.tag, "version": release.version, "prerelease": release.prerelease,
            "published_at": release.published_at, "notes": release.notes, "html_url": release.html_url,
            **files}


def check_download(name: str, data: bytes, expected_sha256: str) -> None:
    got = sha256(data)
    if got != expected_sha256:
        raise ReleaseError(f"{name}: downloaded sha256 {got} does not match the published {expected_sha256}")


def _headers() -> dict[str, str]:
    return {"accept": "application/vnd.github+json", "user-agent": f"twinspark-manager/{__version__}",
            "x-github-api-version": "2022-11-28"}


def _sums_text(data: bytes) -> str:
    if len(data) > 64 * 1024:
        raise ReleaseError(f"{SUMS} is larger than expected")
    return data.decode("utf-8")


def latest(client, *, version: Optional[str] = None, allow_prerelease: bool = False) -> dict[str, Any]:
    """Look the release up with a synchronous ``httpx.Client`` (the CLI)."""
    r = client.get(API, params={"per_page": 30}, headers=_headers())
    _raise_for(r)
    rel = choose(parse_releases(r.json()), version, allow_prerelease)
    s = client.get(asset_url(rel.tag, SUMS), headers={"user-agent": _headers()["user-agent"]})
    _raise_for(s)
    return plan(rel, _sums_text(s.content))


async def latest_async(client, *, version: Optional[str] = None, allow_prerelease: bool = False) -> dict[str, Any]:
    """The same with an ``httpx.AsyncClient`` (node A's controller)."""
    r = await client.get(API, params={"per_page": 30}, headers=_headers())
    _raise_for(r)
    rel = choose(parse_releases(r.json()), version, allow_prerelease)
    s = await client.get(asset_url(rel.tag, SUMS), headers={"user-agent": _headers()["user-agent"]})
    _raise_for(s)
    return plan(rel, _sums_text(s.content))


def _raise_for(r) -> None:
    if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
        raise ReleaseError("GitHub's rate limit for unauthenticated requests is used up; try again in an hour",
                           "rate-limited")
    if r.status_code >= 400:
        raise ReleaseError(f"GitHub answered {r.status_code} for {r.request.url}", "error")


def summary(current: str, target: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Current vs. published, for the GUI and `tsm update`."""
    if not target:
        return {"current": current, "latest": None, "available": False}
    return {"current": current, "latest": target["version"], "tag": target["tag"],
            "available": newer(target["version"], current), "prerelease": target.get("prerelease", False),
            "published_at": target.get("published_at"), "html_url": target.get("html_url"),
            "notes": target.get("notes", "")}
