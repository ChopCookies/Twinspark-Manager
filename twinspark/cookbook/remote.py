"""Community recipe sources on GitHub: list a recipe folder, fetch a recipe file.

Read-only, unauthenticated, cached for a few minutes (the GitHub API allows 60
anonymous requests an hour — one listing per source per refresh is plenty).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..netguard import UnsafeURL
from ..netguard import check_public_url as _check_public_url
from . import raw_url

_SOURCE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+):([A-Za-z0-9_./-]*?)(?:@([A-Za-z0-9_./-]+))?$")
MAX_RECIPE_BYTES = 512 * 1024
_TTL = 600.0
_cache: dict[str, tuple[float, Any]] = {}


class RemoteError(ValueError):
    pass


def parse_source(src: str) -> tuple[str, str, str, Optional[str]]:
    m = _SOURCE.match(src.strip())
    if not m:
        raise RemoteError(f"recipe source must look like owner/repo:path[@ref], got {src!r}")
    return m.group(1), m.group(2), m.group(3).strip("/"), m.group(4)


def _client(client: Optional[httpx.Client]) -> httpx.Client:
    return client or httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=True,
                                  headers={"user-agent": "twinspark-manager"})


def list_source(src: str, client: Optional[httpx.Client] = None, refresh: bool = False) -> dict[str, Any]:
    """Recipe files in a GitHub folder or the TwinSpark Community v1 registry."""
    src = src.strip()
    key = f"list:{src}"
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < _TTL:
        return deepcopy(hit[1])
    if src.startswith("https://"):
        out = _list_registry(src, client)
        _cache[key] = (time.time(), deepcopy(out))
        return out
    owner, repo, path, ref = parse_source(src)
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    c = _client(client)
    try:
        r = c.get(url, params={"ref": ref} if ref else None,
                  headers={"accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        raise RemoteError(f"GitHub not reachable ({type(exc).__name__})") from exc
    finally:
        if client is None:
            c.close()
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise RemoteError("GitHub API rate limit reached — try again in an hour or import by URL")
    if r.status_code == 404:
        raise RemoteError(f"{owner}/{repo}:{path} not found")
    if r.status_code != 200:
        raise RemoteError(f"GitHub returned HTTP {r.status_code}")
    try:
        items = r.json()
    except ValueError as exc:
        raise RemoteError("GitHub returned an invalid recipe listing") from exc
    if not isinstance(items, list):
        raise RemoteError("recipe source must point to a GitHub folder, not a file")
    if any(not isinstance(it, dict) or not isinstance(it.get("name"), str)
           or not isinstance(it.get("path"), str) for it in items):
        raise RemoteError("GitHub returned an invalid recipe listing")
    files = [{"name": it["name"], "path": it["path"], "size": it.get("size"),
              "url": it.get("html_url"), "download_url": it.get("download_url"),
              "sha": it.get("sha")}
             for it in items if it.get("type") == "file"
             and it["name"].lower().endswith((".yaml", ".yml", ".json"))
             and not it["name"].lower().endswith(".meta.json")]
    out = {"source": src, "repo": f"{owner}/{repo}", "path": path, "ref": ref, "files": files,
           "fetched_at": time.time()}
    files.sort(key=lambda item: item["name"].lower())
    _cache[key] = (time.time(), deepcopy(out))
    return out


MAX_REGISTRY_PAGES = 5
_REGISTRY_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,119}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _list_registry(src: str, client: Optional[httpx.Client]) -> dict[str, Any]:
    """Read bounded v1 pages and only offer immutable downloads from this registry."""
    check_public_url(src, resolve=False)
    parts = urlsplit(src)
    suffix = "/api/v1/recipes"
    if not parts.path.endswith(suffix) or parts.query or parts.fragment:
        raise RemoteError("Community registry source must end in /api/v1/recipes without a query or fragment")
    files: dict[str, dict[str, Any]] = {}
    last_page = 1
    for page in range(1, MAX_REGISTRY_PAGES + 1):
        url = urlunsplit(parts._replace(query=f"page={page}"))
        try:
            doc = json.loads(fetch_text(url, client, allow_redirects=False))
        except (ValueError, RecursionError) as exc:
            if isinstance(exc, RemoteError):
                raise
            raise RemoteError("Community registry returned invalid JSON") from exc
        if (not isinstance(doc, dict) or type(doc.get("schema_version")) is not int
                or doc["schema_version"] != 1 or type(doc.get("page")) is not int or doc["page"] != page
                or type(doc.get("last_page")) is not int or doc["last_page"] < 1
                or not isinstance(doc.get("data"), list) or len(doc["data"]) > 12):
            raise RemoteError("Community registry returned an unsupported or invalid v1 listing")
        last_page = doc["last_page"]
        for row in doc["data"]:
            if not isinstance(row, dict):
                raise RemoteError("Community registry returned an invalid recipe entry")
            slug, version, digest = row.get("slug"), row.get("version"), row.get("sha256")
            if (not isinstance(slug, str) or not _REGISTRY_SLUG.fullmatch(slug)
                    or type(version) is not int or not 0 < version <= 2**31 - 1
                    or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
                raise RemoteError("Community registry returned an invalid recipe version or checksum")
            immutable = f"{src}/{slug}/versions/{version}/recipe.json"
            if row.get("download_url") != immutable:
                raise RemoteError("Community registry returned a download outside its versioned endpoint")
            title = row.get("title")
            files.setdefault(slug, {"name": slug, "title": title[:120] if isinstance(title, str) else slug,
                                    "version": version, "download_url": immutable, "sha256": digest})
        if page >= last_page:
            break
    return {"source": src, "kind": "registry", "files": list(files.values()),
            "truncated": last_page > MAX_REGISTRY_PAGES, "fetched_at": time.time()}


def verify_recipe(text: str, expected_sha256: Optional[str]) -> None:
    if expected_sha256 is not None and hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_sha256:
        raise RemoteError("recipe download does not match the catalog SHA-256 — refresh Community and retry")


MAX_REDIRECTS = 3


def check_public_url(url: str, resolve: bool = True) -> str:
    try:
        return _check_public_url(url, resolve)
    except (UnsafeURL, ValueError) as exc:
        raise RemoteError(str(exc).replace("URLs", "recipe URLs", 1)) from exc


def fetch_text(url: str, client: Optional[httpx.Client] = None, *, allow_redirects: bool = True) -> str:
    url = raw_url(url)
    own = client is None
    # a caller-supplied client is a test seam: skip DNS there, keep every other rule
    check_public_url(url, resolve=own)
    c = client or httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
                               headers={"user-agent": "twinspark-manager"})
    buf = bytearray()
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            with c.stream("GET", url, follow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    if not allow_redirects:
                        raise RemoteError("Community registry must not redirect")
                    target = urljoin(url, r.headers["location"])
                    if urlsplit(target).scheme != "https":
                        raise RemoteError("recipe redirect must stay on https://")
                    url = check_public_url(target, resolve=own)
                    continue
                if r.status_code != 200:
                    raise RemoteError(f"{_host(url)} returned HTTP {r.status_code}")
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_RECIPE_BYTES:
                        raise RemoteError("recipe file larger than 512 KiB")
                break
        else:
            raise RemoteError("too many redirects")
    except httpx.HTTPError as exc:
        raise RemoteError(f"cannot fetch from {_host(url)} ({type(exc).__name__})") from exc
    finally:
        if own:
            c.close()
    try:
        return bytes(buf).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RemoteError("recipe file is not UTF-8 text") from exc


def _host(url: str) -> str:
    return urlsplit(url).hostname or "host"
