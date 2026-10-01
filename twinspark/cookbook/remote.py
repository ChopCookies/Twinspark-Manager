"""Community recipe sources on GitHub: list a recipe folder, fetch a recipe file.

Read-only, unauthenticated, cached for a few minutes (the GitHub API allows 60
anonymous requests an hour — one listing per source per refresh is plenty).
"""

from __future__ import annotations

import re
import time
from copy import deepcopy
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

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
    """Recipe files (*.yaml/*.yml/*.json) in a GitHub folder."""
    key = f"list:{src}"
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < _TTL:
        return deepcopy(hit[1])
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


def _recipe_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise RemoteError("invalid recipe URL") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise RemoteError("only https:// recipe URLs are accepted")
    if parsed.username or parsed.password:
        raise RemoteError("recipe URLs must not contain credentials")
    return url


def fetch_text(url: str, client: Optional[httpx.Client] = None) -> str:
    url = _recipe_url(raw_url(url))
    c = _client(client)
    try:
        for _ in range(6):
            with c.stream("GET", url, follow_redirects=False) as r:
                if r.is_redirect:
                    if not r.headers.get("location"):
                        raise RemoteError("recipe redirect has no destination")
                    target = str(r.url.join(r.headers["location"]))
                    if urlsplit(target).scheme != "https":
                        raise RemoteError("recipe redirect must stay on https://")
                    url = _recipe_url(target)
                    continue
                if r.status_code != 200:
                    raise RemoteError(f"{url} returned HTTP {r.status_code}")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_RECIPE_BYTES:
                        raise RemoteError("recipe file larger than 512 KiB")
                break
        else:
            raise RemoteError("too many recipe redirects")
    except httpx.HTTPError as exc:
        raise RemoteError(f"cannot fetch {url} ({type(exc).__name__})") from exc
    finally:
        if client is None:
            c.close()
    try:
        return bytes(buf).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RemoteError("recipe file is not UTF-8 text") from exc
