"""The recipe catalog (master plan §4.1): new and corrected recipes without a TwinSpark update.

The recipes in ``twinspark/cookbook/recipes`` ship with each release. The same folder on the repository's
``main`` branch is published as a catalog: ``catalog/index.json`` lists every recipe file with its
SHA-256 and size, and is generated from the folder by ``scripts/build_catalog.py`` (CI refuses an index
that does not match). Node A and the workstation client read it from raw.githubusercontent.com:

* only over https, with size limits (index 1 MiB, a recipe 512 KiB, 300 recipes);
* every file is checked against the SHA-256 in the index before it is used, and again when it is read
  from the cache; a file that does not match is not used;
* a recipe that needs a newer TwinSpark (``min_twinspark``) is listed, not offered for import;
* the catalog only adds recipes and newer versions of them to the Cookbook. Profiles imported from a
  recipe are never changed: a newer recipe shows up as an update to review (``Recipe updates``), which
  creates a separate profile.

Nothing is fetched while nobody looks: the Cookbook refreshes the catalog in the background when its
copy is older than ``cookbook.catalog_refresh_hours`` (24 h), or on ``tsm cookbook refresh``.
The SHA-256 protects against a damaged or partial download and a changed cache; the index itself is
trusted as far as https to github.com is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from .. import __version__

FORMAT = "twinspark-catalog/1"
REPO = "ChopCookies/Twinspark-Manager"
DEFAULT_URL = f"https://raw.githubusercontent.com/{REPO}/main/catalog/index.json"
RECIPES_PATH = "twinspark/cookbook/recipes"
MAX_INDEX_BYTES = 1024 * 1024
MAX_FILE_BYTES = 512 * 1024
MAX_RECIPES = 300
MAX_REDIRECTS = 3
BASELINE = "0.7.0"           # the first TwinSpark that reads the catalog: a recipe's default min_twinspark

log = logging.getLogger("twinspark.cookbook.catalog")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^\d{1,4}(?:\.\d{1,4}){0,3}$")
RECIPE_SUFFIXES = (".json", ".yaml", ".yml")


class CatalogError(ValueError):
    pass


def version_tuple(v: str) -> tuple[int, ...]:
    """``0.7.0`` → (0, 7, 0); a suffix (``0.7.0rc1``, ``0.7.0+dev``) is ignored."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", str(v or ""))
    if not m:
        raise CatalogError(f"not a version: {v!r}")
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def recipe_files(folder: Path) -> dict[str, list[Path]]:
    """Recipe name → its files (the recipe, then a YAML recipe's ``.meta.json``)."""
    out: dict[str, list[Path]] = {}
    for p in sorted(folder.glob("*")):
        if p.suffix in RECIPE_SUFFIXES and not p.name.endswith(".meta.json"):
            files = [p]
            meta = p.with_suffix(".meta.json")
            if p.suffix != ".json" and meta.exists():
                files.append(meta)
            out[p.stem] = files
    return out


# --------------------------------------------------------------------------------------------- the index


def _min_version(files: list[Path]) -> str:
    """``min_twinspark`` of a recipe: in a JSON recipe's ``source``, or a YAML recipe's ``.meta.json``."""
    value = None
    for f in files:
        if f.suffix != ".json":
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        value = doc.get("min_twinspark") if f.name.endswith(".meta.json") else (doc.get("source") or {}).get(
            "min_twinspark")
        if value:
            break
    if not value:
        return BASELINE
    if not _VERSION.match(str(value)):
        raise CatalogError(f"{files[0].name}: min_twinspark must look like 0.7.0, got {value!r}")
    return str(value)


def build_index(folder: Path, repo_root: Path, *, validate: Callable[[Path, Optional[str]], Any],
                modes_of: Callable[[Path], tuple[dict, Optional[str]]]) -> dict[str, Any]:
    """The index of a recipe folder. Deterministic: the same files give the same bytes (no timestamps).

    ``validate(path, mode)`` must raise when a recipe (in a mode) does not build; ``modes_of`` gives its
    modes. Both come from :mod:`twinspark.cookbook` (passed in so this module does not import it).
    """
    entries = []
    for name, files in recipe_files(folder).items():
        if not _NAME.match(name):
            raise CatalogError(f"recipe file name {files[0].name!r}: use lowercase letters, digits, . _ -")
        modes, default = modes_of(files[0])
        draft = validate(files[0], None)
        for mode in modes:
            validate(files[0], mode)
        minimum = _min_version(files)
        if version_tuple(minimum) > version_tuple(__version__):
            raise CatalogError(f"{name} needs TwinSpark {minimum}, this repository is {__version__}")
        rows = []
        for f in files:
            data = f.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise CatalogError(f"{f.name} is larger than {MAX_FILE_BYTES // 1024} KiB")
            rows.append({"path": f.relative_to(repo_root).as_posix(), "sha256": sha256(data), "size": len(data)})
        src = draft.source or {}
        entries.append({
            "name": name, "title": src.get("title") or draft.description.split(".")[0][:80] or name,
            "model": draft.simple.model, "topology": draft.simple.topology.value,
            "engine": str(draft.engine), "verification": draft.verification.value,
            "modes": list(modes), "default_mode": default, "min_twinspark": minimum, "files": rows,
        })
    if len(entries) > MAX_RECIPES:
        raise CatalogError(f"more than {MAX_RECIPES} recipes")
    return {"format": FORMAT, "twinspark_version": __version__, "recipes": entries}


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check_index(doc: Any) -> dict[str, Any]:
    """Validate a fetched index; returns it. Paths stay relative, inside the repository, one recipe each."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise CatalogError(f"not a TwinSpark recipe catalog (format {FORMAT})")
    recipes = doc.get("recipes")
    if not isinstance(recipes, list) or len(recipes) > MAX_RECIPES:
        raise CatalogError("the catalog's recipe list is missing or too long")
    version_tuple(str(doc.get("twinspark_version") or "0"))
    seen, basenames = set(), set()
    for r in recipes:
        if not isinstance(r, dict) or not isinstance(r.get("name"), str) or not _NAME.match(r["name"]):
            raise CatalogError("a catalog entry has no valid name")
        name = r["name"]
        if name in seen:
            raise CatalogError(f"{name} is listed twice")
        seen.add(name)
        if not _VERSION.match(str(r.get("min_twinspark") or "")):
            raise CatalogError(f"{name}: invalid min_twinspark")
        files = r.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= 2:
            raise CatalogError(f"{name}: a recipe has one file and maybe its .meta.json")
        for i, f in enumerate(files):
            if not isinstance(f, dict):
                raise CatalogError(f"{name}: invalid file entry")
            path, digest, size = f.get("path"), f.get("sha256"), f.get("size")
            segments = path.split("/") if isinstance(path, str) else []
            if not segments or any(s in (".", "..") or not _SEGMENT.match(s) for s in segments):
                raise CatalogError(f"{name}: invalid path {path!r}")
            base = segments[-1]
            allowed = {f"{name}{s}" for s in RECIPE_SUFFIXES} if i == 0 else {f"{name}.meta.json"}
            if base not in allowed or base in basenames:
                raise CatalogError(f"{name}: unexpected file {base!r}")
            basenames.add(base)
            if not isinstance(digest, str) or not _SHA.match(digest):
                raise CatalogError(f"{name}: invalid sha256")
            if not isinstance(size, int) or not 0 < size <= MAX_FILE_BYTES:
                raise CatalogError(f"{name}: {base} is larger than {MAX_FILE_BYTES // 1024} KiB")
        if len(files) == 2 and files[0]["path"].endswith(".json"):
            raise CatalogError(f"{name}: only a YAML recipe has a .meta.json")
    return doc


def base_url(index_url: str) -> str:
    """Where the index's paths start: the repository root for ``…/catalog/index.json``."""
    if index_url.endswith("/catalog/index.json"):
        return index_url[: -len("catalog/index.json")]
    return index_url.rsplit("/", 1)[0] + "/"


# ----------------------------------------------------------------------------------------------- fetching


def _get(client: httpx.Client, url: str, limit: int) -> bytes:
    for _ in range(MAX_REDIRECTS + 1):
        if urlsplit(url).scheme != "https":
            raise CatalogError("the catalog is only read over https://")
        try:
            with client.stream("GET", url, follow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200:
                    raise CatalogError(f"{urlsplit(url).hostname} answered HTTP {r.status_code} for {url}")
                declared = r.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise CatalogError(f"{url} is larger than {limit // 1024} KiB")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > limit:
                        raise CatalogError(f"{url} is larger than {limit // 1024} KiB")
                return bytes(buf)
        except httpx.HTTPError as exc:
            raise CatalogError(f"cannot reach {urlsplit(url).hostname} ({type(exc).__name__})") from exc
    raise CatalogError("too many redirects")


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20, connect=5),
                        headers={"user-agent": f"twinspark-manager/{__version__}"})


# -------------------------------------------------------------------------------------------- the cache


class Catalog:
    """A local copy of the catalog: ``<cache>/current.json`` names the set in use, ``<cache>/sets/<id>/``
    holds its index and files. A refresh writes a new set and then replaces ``current.json`` (atomic), so
    a reader sees the old or the new catalog, never a mix."""

    def __init__(self, cache_dir: Path | str, url: Optional[str] = DEFAULT_URL, *,
                 refresh_hours: float = 24.0, version: str = __version__,
                 package_files: Optional[Callable[[], dict[str, list[Path]]]] = None):
        self.cache = Path(cache_dir)
        self.url = url or None
        self.refresh_hours = refresh_hours
        self.version = version
        self._package_files = package_files or (lambda: {})
        self._lock = threading.Lock()
        self._refreshing = threading.Lock()
        self._loaded: Optional[tuple[Any, dict]] = None
        self.last_error: Optional[str] = None
        if self.url and urlsplit(self.url).scheme != "https":
            raise CatalogError("cookbook.catalog_url must be an https:// URL")

    # ---- reading

    def _pointer(self) -> Optional[dict]:
        try:
            doc = json.loads((self.cache / "current.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) and isinstance(doc.get("set"), str) else None

    def snapshot(self) -> dict[str, Any]:
        """The catalog in use: ``{"index", "fetched_at", "url", "dir", "files": {name: [Path]}, "bad": {...}}``,
        or an empty dict when there is none. Files are re-checked against the index once per change."""
        pointer_path = self.cache / "current.json"
        try:
            stamp: Any = pointer_path.stat().st_mtime_ns
        except OSError:
            return {}
        with self._lock:
            if self._loaded and self._loaded[0] == stamp:
                return self._loaded[1]
            pointer = self._pointer()
            snap: dict[str, Any] = {}
            if pointer and _SEGMENT.match(pointer["set"]):
                folder = self.cache / "sets" / pointer["set"]
                try:
                    index = check_index(json.loads((folder / "index.json").read_text(encoding="utf-8")))
                except (OSError, ValueError) as exc:
                    log.warning("the cached recipe catalog is unreadable: %s", exc)
                    index = None
                if index is not None:
                    files, bad = {}, {}
                    for r in index["recipes"]:
                        if version_tuple(r["min_twinspark"]) > version_tuple(self.version):
                            continue
                        paths = [folder / "recipes" / f["path"].rsplit("/", 1)[-1] for f in r["files"]]
                        try:
                            ok = all(sha256(p.read_bytes()) == f["sha256"]
                                     for p, f in zip(paths, r["files"], strict=True))
                        except OSError:
                            ok = False
                        if ok:
                            files[r["name"]] = paths
                        else:
                            bad[r["name"]] = "the cached file does not match the catalog (it is refreshed next time)"
                    snap = {"index": index, "fetched_at": pointer.get("fetched_at"), "url": pointer.get("url"),
                            "dir": folder, "files": files, "bad": bad, "changes": pointer.get("changes") or {}}
            self._loaded = (stamp, snap)
            return snap

    def overlay(self) -> tuple[dict[str, Path], dict[str, dict]]:
        """``(recipe paths the Cookbook uses instead of or besides the package's, notes per recipe)``.

        A catalog recipe is used when the package has none of that name (``new``) or a different one
        (``updated``) — unless this TwinSpark is newer than the catalog (an unreleased build): then its own
        recipes stay. A recipe that needs a newer TwinSpark is noted, not used."""
        snap = self.snapshot()
        if not snap:
            return {}, {}
        package = self._package_files()
        newer_here = version_tuple(self.version) > version_tuple(snap["index"].get("twinspark_version") or "0")
        paths: dict[str, Path] = {}
        notes: dict[str, dict] = {}
        for r in snap["index"]["recipes"]:
            name = r["name"]
            if version_tuple(r["min_twinspark"]) > version_tuple(self.version):
                notes[name] = {"needs": r["min_twinspark"], "title": r.get("title") or name,
                               "model": r.get("model"), "in_package": name in package}
                continue
            if name in snap["bad"] or name not in snap["files"]:
                continue
            mine = package.get(name)
            if mine is not None:
                same = [sha256(p.read_bytes()) for p in mine] == [f["sha256"] for f in r["files"]]
                if same:
                    notes[name] = {"origin": "package", "catalog": "same"}
                    continue
                if newer_here:
                    notes[name] = {"origin": "package", "catalog": "older"}
                    continue
            paths[name] = snap["files"][name][0]
            notes[name] = {"origin": "catalog", "catalog": "updated" if mine is not None else "new"}
        return paths, notes

    # ---- refreshing

    def stale(self) -> bool:
        if not self.url:
            return False
        pointer = self._pointer()
        if not pointer or pointer.get("url") != self.url:
            return True
        age = time.time() - float(pointer.get("fetched_at") or 0)
        return self.refresh_hours > 0 and age > self.refresh_hours * 3600

    def refresh(self, client: Optional[httpx.Client] = None) -> dict[str, Any]:
        """Fetch the catalog; returns what changed since the copy before (``added``/``updated``/``removed``)."""
        if not self.url:
            raise CatalogError("the recipe catalog is switched off (cookbook.catalog_url)")
        if not self._refreshing.acquire(blocking=False):
            raise CatalogError("the catalog is being refreshed already")
        own = client is None
        c = client or _client()
        try:
            raw = _get(c, self.url, MAX_INDEX_BYTES)
            try:
                index = check_index(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, ValueError) as exc:
                raise CatalogError(f"the catalog index is invalid: {exc}") from None
            before = self.snapshot()
            reuse = {}
            if before:
                for r in before["index"]["recipes"]:
                    for f in r["files"]:
                        reuse[f["sha256"]] = before["dir"] / "recipes" / f["path"].rsplit("/", 1)[-1]
            package = {sha256(p.read_bytes()): p for ps in self._package_files().values() for p in ps}
            set_id = f"{int(time.time())}-{sha256(raw)[:12]}-{os.urandom(3).hex()}"
            sets = self.cache / "sets"
            sets.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=".new-", dir=sets))
            try:
                (tmp / "recipes").mkdir()
                base = base_url(self.url)
                for r in index["recipes"]:
                    if version_tuple(r["min_twinspark"]) > version_tuple(self.version):
                        continue                                   # listed, not downloaded
                    for f in r["files"]:
                        target = tmp / "recipes" / f["path"].rsplit("/", 1)[-1]
                        known = reuse.get(f["sha256"]) or package.get(f["sha256"])
                        data = known.read_bytes() if known is not None and known.exists() else None
                        if data is None or sha256(data) != f["sha256"]:
                            data = _get(c, urljoin(base, f["path"]), MAX_FILE_BYTES)
                        if sha256(data) != f["sha256"] or len(data) != f["size"]:
                            raise CatalogError(f"{f['path']} does not match the catalog's SHA-256 — not used")
                        target.write_bytes(data)
                (tmp / "index.json").write_bytes(raw)
                final = sets / set_id
                os.replace(tmp, final)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            # the first time, "changed" means against the recipes this TwinSpark brought along
            delta = diff(before["index"] if before else self._package_index(), index)
            now = time.time()
            # what the Cookbook shows as "changed": the last refresh that changed something
            last = {**delta, "at": now} if not before or any(delta.values()) else (before.get("changes") or {})
            _write_json(self.cache / "current.json", {"set": set_id, "url": self.url, "fetched_at": now,
                                                      "changes": last})
            self._prune(keep={set_id, before["dir"].name} if before else {set_id})
            self.last_error = None
            return {**delta, "recipes": len(index["recipes"]), "fetched_at": now}
        except CatalogError as exc:
            self.last_error = str(exc)
            raise
        finally:
            if own:
                c.close()
            self._refreshing.release()

    def _package_index(self) -> dict[str, Any]:
        return {"recipes": [{"name": n, "files": [{"sha256": sha256(p.read_bytes())} for p in ps]}
                            for n, ps in self._package_files().items()]}

    def refresh_if_stale(self) -> None:
        """For a background thread: refresh when old, log instead of raising."""
        if not self.stale():
            return
        try:
            self.refresh()
        except CatalogError as exc:
            if "already" not in str(exc):
                log.info("recipe catalog not refreshed: %s", exc)
        except (OSError, ValueError) as exc:          # a full disk, a broken cache: the Cookbook keeps working
            self.last_error = f"the catalog cache could not be written: {exc}"
            log.warning("%s", self.last_error)

    def _prune(self, keep: set[str]) -> None:
        for p in (self.cache / "sets").iterdir():
            if p.name not in keep:
                shutil.rmtree(p, ignore_errors=True)

    def status(self) -> dict[str, Any]:
        snap = self.snapshot()
        _paths, notes = self.overlay()
        return {"enabled": bool(self.url), "url": self.url, "refresh_hours": self.refresh_hours,
                "fetched_at": snap.get("fetched_at"), "recipes": len(snap["index"]["recipes"]) if snap else 0,
                "catalog_version": snap["index"].get("twinspark_version") if snap else None,
                "changes": snap.get("changes") or {}, "last_error": self.last_error,
                "new": sorted(n for n, x in notes.items() if x.get("catalog") == "new"),
                "updated": sorted(n for n, x in notes.items() if x.get("catalog") == "updated"),
                "needs_newer": {n: x["needs"] for n, x in notes.items() if x.get("needs")},
                "unusable": dict(snap.get("bad") or {})}


def diff(old: Optional[dict], new: dict) -> dict[str, list[str]]:
    def shas(doc: Optional[dict]) -> dict[str, list[str]]:
        return {r["name"]: [f["sha256"] for f in r["files"]] for r in (doc or {}).get("recipes") or []}
    a, b = shas(old), shas(new)
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "updated": sorted(n for n in set(a) & set(b) if a[n] != b[n])}


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".current-", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------------- the one in use here

_active: Optional[Catalog] = None


def configure(catalog: Optional[Catalog]) -> None:
    """Set the catalog this process's Cookbook overlays (node A's controller, or the workstation client)."""
    global _active
    _active = catalog


def active() -> Optional[Catalog]:
    return _active

