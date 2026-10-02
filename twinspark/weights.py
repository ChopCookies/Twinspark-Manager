"""Model weights in the Hugging Face cache: selection, completeness, sync scope,
inventory, hash verification and safe deletion.

The cache layout is the standard HF one (``<hf_home>/hub/models--org--name/``
with ``blobs/``, ``snapshots/<sha>/`` symlinks and ``refs/``), including the
HF 2.x variant where blobs are shared under ``<hf_home>/hub/blobs``. Only
revision-scoped operations exist: an older snapshot is never touched by a sync,
and deleting one revision keeps every blob another snapshot still references.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Optional

MANIFEST = ".twinspark-weights.json"
_ROOT_FILES = {"config.json", "generation_config.json", "model.safetensors.index.json",
               "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
               "added_tokens.json", "vocab.json", "merges.txt", "vocab.txt",
               "preprocessor_config.json", "processor_config.json", "chat_template.json",
               "video_preprocessor_config.json", "params.json"}


def inference_file(name: str, include: Iterable[str] = ()) -> bool:
    """Root checkpoint/tokenizer files, excluding alternate exports and training data.

    ``include`` adds glob patterns for files a recipe needs beyond that, e.g. a
    drafter bundled in a sub-directory (``dflash/*``).
    """
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or name in {".", ".."}:
        return False
    if any(fnmatch.fnmatch(name, pat) for pat in include):
        return True
    if len(p.parts) != 1:
        return False
    return (name.endswith((".safetensors", ".model", ".tiktoken", ".jinja", ".py"))
            or name in _ROOT_FILES)


def select_files(siblings, include: Iterable[str] = ()) -> dict[str, int]:
    include = list(include)
    files = {}
    for entry in siblings:
        name, size = entry.rfilename, entry.size
        if inference_file(name, include):
            if not isinstance(size, int) or size < 0:
                raise ValueError(f"cannot budget download: missing file size for {name}")
            files[name] = size
    if "config.json" not in files or not any(n.endswith(".safetensors") for n in files):
        raise ValueError("model needs config.json and a root safetensors checkpoint")
    for pattern in include:
        if not any(fnmatch.fnmatch(n, pattern) for n in files):
            raise ValueError(f"recipe download_include matches no files: {pattern}")
    return files


def expected_hashes(siblings) -> dict[str, dict[str, Any]]:
    """{name: {"size", "sha256"|None, "git_sha1"|None}} from HfApi.model_info(files_metadata=True)."""
    out = {}
    for e in siblings:
        lfs = getattr(e, "lfs", None)
        sha256 = None
        if lfs is not None:
            sha256 = getattr(lfs, "sha256", None) or (lfs.get("sha256") if isinstance(lfs, dict) else None)
        out[e.rfilename] = {"size": e.size, "sha256": sha256,
                            "git_sha1": getattr(e, "blob_id", None)}
    return out


def _manifest(snapshot: Path) -> Optional[dict]:
    try:
        return json.loads((snapshot / MANIFEST).read_text())
    except (OSError, ValueError):
        return None


def snapshot_complete(snapshot: Path, include: Iterable[str] = ()) -> bool:
    try:
        if not (snapshot / "config.json").is_file():
            return False
        doc = _manifest(snapshot)
        if include and not set(include).issubset((doc or {}).get("include", [])):
            return False
        if doc is not None:
            files = doc["files"]
            return (doc["revision"] == snapshot.name and "config.json" in files
                    and any(n.endswith(".safetensors") for n in files)
                    and all(isinstance(size, int) and size >= 0
                            and not PurePosixPath(n).is_absolute() and ".." not in PurePosixPath(n).parts
                            and (snapshot / n).is_file() and (snapshot / n).stat().st_size == size
                            for n, size in files.items()))
        # Existing HF caches may predate our manifest. All indexed shards must exist.
        index = snapshot / "model.safetensors.index.json"
        if index.exists():
            shards = set(json.loads(index.read_text())["weight_map"].values())
        else:
            shards = {"model.safetensors"}
        has_tokenizer = any((snapshot / name).is_file() for name in
                            ("tokenizer.json", "tokenizer.model", "vocab.json", "vocab.txt",
                             "tiktoken.model"))
        return (has_tokenizer and bool(shards)
                and all(inference_file(n) and (snapshot / n).is_file()
                        and (snapshot / n).stat().st_size > 0 for n in shards))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def write_manifest(snapshot: Path, files: dict[str, int], verified: Optional[dict] = None,
                   tag: str = "", include: Iterable[str] = ()) -> None:
    doc = {"revision": snapshot.name, "files": files, "include": list(include)}
    if verified:
        doc["verified"] = verified
    tmp = snapshot / f"{MANIFEST}.{tag or os.getpid()}.tmp"
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(snapshot / MANIFEST)


def _resolve_chain(path: Path, repo_root: Path, hub_root: Path) -> list[Path]:
    """The file itself plus every symlink hop, all confined to the repo or hub/blobs."""
    chain, seen = [], set()
    current = path
    while True:
        if not (current.is_relative_to(repo_root) or current.is_relative_to(hub_root / "blobs")):
            raise ValueError(f"snapshot file escapes the model cache: {path.name}")
        if current in seen:
            raise ValueError(f"cyclic cache symlink: {path.name}")
        seen.add(current)
        chain.append(current)
        if not current.is_symlink():
            return chain
        current = Path(os.path.abspath(current.parent / current.readlink()))


def _snapshot_entries(snap: Path, include: Iterable[str] = ()) -> list[Path]:
    include = list(include)
    manifest = _manifest(snap)
    listed = set((manifest or {}).get("files", {}))
    out = []
    for path in sorted(snap.rglob("*")):
        rel = path.relative_to(snap).as_posix()
        if path.is_dir() and not path.is_symlink():
            continue
        if rel == MANIFEST or rel in listed or inference_file(rel, include):
            out.append(path)
    return out


def sync_files(repo_dir: Path, revision: str, include: Iterable[str] = ()) -> list[str]:
    """Only this revision and the HF blobs its symlinks reference, never older snapshots.

    Paths are relative to ``<hf_home>/hub`` (rsync ``--files-from`` root).
    """
    repo_root = repo_dir.resolve()
    root = repo_root.parent  # HF 2.x also uses hub/blobs shared across repositories
    snap = repo_root / "snapshots" / revision
    if not snapshot_complete(snap):
        raise ValueError(f"snapshot is incomplete: {revision}")
    result = set()
    for path in _snapshot_entries(snap, include):
        if not (path.is_file() or path.is_symlink()):
            continue
        for hop in _resolve_chain(path, repo_root, root):
            result.add(str(hop.relative_to(root)))
    return sorted(result)


def sync_file_sizes(hub_root: Path, rel_paths: list[str]) -> dict[str, int]:
    """Apparent size of each regular file in a sync list (symlinks count 0)."""
    out = {}
    for rel in rel_paths:
        p = hub_root / rel
        try:
            out[rel] = 0 if p.is_symlink() else p.stat().st_size
        except OSError:
            out[rel] = 0
    return out


def partition_by_size(sizes: dict[str, int], buckets: int) -> list[list[str]]:
    """Greedy largest-first split so N rsync streams finish at about the same time."""
    bins: list[tuple[int, list[str]]] = [(0, []) for _ in range(max(1, buckets))]
    for rel, size in sorted(sizes.items(), key=lambda kv: -kv[1]):
        i = min(range(len(bins)), key=lambda k: bins[k][0])
        total, names = bins[i]
        names.append(rel)
        bins[i] = (total + size, names)
    return [sorted(names) for _, names in bins if names]


# ---- hashing ---------------------------------------------------------------------------
def _hash_file(path: Path, want_sha256: bool, want_sha1: bool, size: int) -> dict[str, str]:
    sha256, sha1 = hashlib.sha256(), hashlib.sha1()
    if want_sha1:
        sha1.update(f"blob {size}\0".encode())
    with path.open("rb") as fh:
        while chunk := fh.read(16 * 1024 ** 2):
            if want_sha256:
                sha256.update(chunk)
            if want_sha1:
                sha1.update(chunk)
    out = {}
    if want_sha256:
        out["sha256"] = sha256.hexdigest()
    if want_sha1:
        out["git_sha1"] = sha1.hexdigest()
    return out


def verify_snapshot(snapshot: Path, expected: dict[str, dict[str, Any]], files: dict[str, int],
                    progress: Optional[Callable[[int, int], None]] = None,
                    workers: int = 4) -> dict[str, dict[str, Any]]:
    """Check each selected file against the Hub's sha256 (LFS) or git blob sha1.

    Results are cached in the manifest keyed by (size, mtime) so a re-check after
    a restart only hashes files that changed.
    """
    previous = (_manifest(snapshot) or {}).get("verified", {})
    total = sum(files.values())
    done = 0
    verified: dict[str, dict[str, Any]] = {}
    todo = []
    for name, size in files.items():
        path = snapshot / name
        st = path.stat()
        if st.st_size != size:
            raise ValueError(f"wrong size for {name}: {st.st_size} != {size}")
        want = expected.get(name, {})
        rec = previous.get(name)
        if rec and rec.get("size") == size and rec.get("mtime_ns") == st.st_mtime_ns \
                and rec.get("sha256") == want.get("sha256") and rec.get("git_sha1") == want.get("git_sha1"):
            verified[name] = rec
            done += size
            continue
        todo.append((name, path, size, st.st_mtime_ns, want))
    if progress:
        progress(done, total)

    def work(item):
        name, path, size, mtime, want = item
        if not (want.get("sha256") or want.get("git_sha1")):
            return name, {"size": size, "mtime_ns": mtime, "unverified": True}
        got = _hash_file(path, bool(want.get("sha256")), not want.get("sha256"), size)
        if want.get("sha256") and got.get("sha256") != want["sha256"]:
            raise ValueError(f"SHA-256 mismatch: {name}")
        if not want.get("sha256") and want.get("git_sha1") and got.get("git_sha1") != want["git_sha1"]:
            raise ValueError(f"git blob hash mismatch: {name}")
        return name, {"size": size, "mtime_ns": mtime, "sha256": want.get("sha256"),
                      "git_sha1": want.get("git_sha1")}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for name, rec in ex.map(work, todo):
            verified[name] = rec
            done += rec["size"]
            if progress:
                progress(done, total)
    return verified


# ---- inventory ---------------------------------------------------------------------------
def repo_id_from_dir(name: str) -> Optional[str]:
    if not name.startswith("models--"):
        return None
    parts = name.split("--")[1:]
    return "/".join(parts) if len(parts) >= 2 else None


def hf_repo_dir(hf_home: str | Path, repo: str) -> Path:
    return Path(hf_home) / "hub" / f"models--{repo.replace('/', '--')}"


def _snapshot_size(snap: Path, repo_root: Path, hub_root: Path) -> tuple[int, set[Path]]:
    """(bytes of the distinct real files behind a snapshot, every path in its chains)."""
    seen: set[Path] = set()
    for path in snap.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            continue
        try:
            seen.update(_resolve_chain(path, repo_root, hub_root))
        except ValueError:
            continue
    total = 0
    for f in {c for c in seen if not c.is_symlink()}:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total, seen


def scan_cache(hf_home: str | Path) -> dict[str, Any]:
    """Every cached model: revisions, sizes, completeness, refs, partial downloads."""
    hub = Path(hf_home) / "hub"
    repos: list[dict[str, Any]] = []
    if hub.is_dir():
        hub_root = hub.resolve()
        for link in sorted(hub.glob("models--*")):
            repo = repo_id_from_dir(link.name)
            if not repo or not link.is_dir():
                continue
            repo_dir = link.resolve()
            refs: dict[str, str] = {}
            rdir = repo_dir / "refs"
            if rdir.is_dir():
                for rf in rdir.rglob("*"):
                    if rf.is_file():
                        try:
                            refs[rf.relative_to(rdir).as_posix()] = rf.read_text().strip()
                        except OSError:
                            pass
            revs = []
            sdir = repo_dir / "snapshots"
            for snap in sorted(sdir.iterdir()) if sdir.is_dir() else []:
                if not snap.is_dir():
                    continue
                size, _ = _snapshot_size(snap, repo_dir, hub_root)
                man = _manifest(snap)
                try:
                    mtime = max((p.lstat().st_mtime for p in snap.iterdir()), default=snap.stat().st_mtime)
                except OSError:
                    mtime = 0
                verified = (man or {}).get("verified") or {}
                files = (man or {}).get("files") or {}
                revs.append({
                    "revision": snap.name,
                    "complete": snapshot_complete(snap),
                    "managed": man is not None,
                    "verified": bool(files) and all(n in verified and not verified[n].get("unverified")
                                                    for n in files),
                    "size_bytes": size,
                    "refs": sorted(r for r, sha in refs.items() if sha == snap.name),
                    "modified": mtime,
                })
            partial = 0
            for blobs in (repo_dir / "blobs",):
                if blobs.is_dir():
                    partial += sum(p.stat().st_size for p in blobs.glob("*.incomplete") if p.is_file())
            repos.append({"repo": repo, "revisions": revs, "refs": refs,
                          "partial_bytes": partial, "path": str(repo_dir)})
    probe = hub if hub.exists() else Path(hf_home)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    du = shutil.disk_usage(probe)
    return {"hf_home": str(hf_home), "repos": repos,
            "disk_free_bytes": du.free, "disk_total_bytes": du.total, "scanned_at": time.time()}


def download_progress(hf_home: str | Path, repo: str, revision: str, files: dict[str, int]) -> int:
    """Bytes on disk for a running download: finished files + partial blobs."""
    repo_dir = hf_repo_dir(hf_home, repo)
    snap = repo_dir / "snapshots" / revision
    done = 0
    for name, size in files.items():
        p = snap / name
        try:
            if p.is_file() and p.stat().st_size == size:
                done += size
        except OSError:
            pass
    for d in (repo_dir / "blobs", Path(hf_home) / "hub" / "blobs"):
        if d.is_dir():
            for p in d.rglob("*.incomplete"):
                try:
                    done += p.stat().st_size
                except OSError:
                    pass
    return min(done, sum(files.values()))


# ---- deletion ------------------------------------------------------------------------------
def plan_delete(hf_home: str | Path, repo: str, revision: Optional[str]) -> dict[str, Any]:
    """Which paths go away and how many bytes that frees, keeping shared blobs."""
    hub = (Path(hf_home) / "hub").resolve()
    repo_dir = hf_repo_dir(hf_home, repo)
    if not repo_dir.is_dir():
        raise FileNotFoundError(f"{repo} is not in the cache")
    repo_root = repo_dir.resolve()
    if not repo_root.is_relative_to(hub):
        raise ValueError("repository directory escapes the cache")
    sdir = repo_root / "snapshots"
    snaps = sorted(p for p in sdir.iterdir() if p.is_dir()) if sdir.is_dir() else []
    targets = [s for s in snaps if revision is None or s.name == revision]
    if revision is not None and not targets:
        raise FileNotFoundError(f"{repo}@{revision[:12]} is not in the cache")
    doomed: set[Path] = set()
    for s in targets:
        doomed |= _snapshot_size(s, repo_root, hub)[1]
    keep: set[Path] = set()
    for s in snaps:
        if s not in targets:
            keep |= _snapshot_size(s, repo_root, hub)[1]
    shared = {p for p in doomed if p.is_relative_to(hub / "blobs")}
    if shared:
        # HF 2.x shared blob store: other repositories may point at the same blob
        for other in hub.glob("models--*"):
            other_root = other.resolve()
            if other_root == repo_root:
                continue
            osd = other_root / "snapshots"
            for s in (osd.iterdir() if osd.is_dir() else []):
                try:
                    keep |= _snapshot_size(s, other_root, hub)[1]
                except OSError:
                    pass
    blobs = sorted(p for p in doomed - keep if not p.is_relative_to(sdir) and not p.is_symlink())
    kept_shared = sorted(p for p in doomed & keep if not p.is_relative_to(sdir) and not p.is_symlink())
    freed = 0
    for b in blobs:
        try:
            freed += b.stat().st_size
        except OSError:
            pass
    whole = revision is None or len(targets) == len(snaps)
    return {"repo": repo, "revisions": [s.name for s in targets], "whole_repo": whole,
            "snapshot_dirs": [str(s) for s in targets], "blobs": [str(b) for b in blobs],
            "kept_shared_blobs": len(kept_shared),
            "freed_bytes": freed, "repo_dir": str(repo_root)}


def execute_delete(plan: dict[str, Any], hf_home: str | Path) -> dict[str, Any]:
    hub = (Path(hf_home) / "hub").resolve()
    repo_dir = Path(plan["repo_dir"]).resolve()
    if not repo_dir.is_relative_to(hub):
        raise ValueError("refusing to delete outside the cache")
    errors: list[str] = []
    # Blobs first: if one cannot be removed (root-owned files in a reused cache, a read-only
    # mount) stop BEFORE touching the snapshots, so the model still shows up in the inventory
    # and the user sees what is left instead of invisible orphaned blobs.
    for b in plan["blobs"]:
        p = Path(b)
        if p.resolve().is_relative_to(hub) and p.is_file():
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"{p.name}: {exc.strerror or exc}")
    if errors:
        raise ValueError(f"could not remove {len(errors)} file(s) ({'; '.join(errors[:3])}); the model "
                         f"was left in place — fix the permissions in the cache and retry")

    def note(_func, path, exc) -> None:
        errors.append(f"{Path(path).name}: {getattr(exc, 'strerror', None) or exc}")

    for s in plan["snapshot_dirs"]:
        p = Path(s)
        if p.resolve().is_relative_to(repo_dir):
            shutil.rmtree(p, onexc=note)
    if errors:
        raise ValueError(f"could not remove the snapshot ({'; '.join(errors[:3])}); retry after "
                         f"fixing the permissions")
    refs = repo_dir / "refs"
    if refs.is_dir():
        for rf in refs.rglob("*"):
            if rf.is_file() and rf.read_text().strip() in plan["revisions"]:
                rf.unlink(missing_ok=True)
    for rev in plan["revisions"]:
        shutil.rmtree(repo_dir / ".no_exist" / rev, ignore_errors=True)
    if plan["whole_repo"]:
        shutil.rmtree(repo_dir, ignore_errors=True)
        lock = hub / ".locks" / repo_dir.name
        shutil.rmtree(lock, ignore_errors=True)
    return {"deleted": plan["revisions"], "freed_bytes": plan["freed_bytes"],
            "whole_repo": plan["whole_repo"]}
