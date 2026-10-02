"""eugr-style *mods*: directories with a ``run.sh`` applied inside the container.

Community dual-Spark recipes ship small patch directories (a ``run.sh`` plus
patch files) that must run inside the vLLM container before ``vllm serve``.
The agent stores them under ``runtime.mods_dir/<name>/``; the launch planner
mounts that directory read-only and the container wrapper applies them in order
(``controller.launch.mods_wrapper``).

Mods are installed from an archive (zip or tar.gz) the controller pushes to
every node, so both Sparks always run byte-identical patches (checked via
``tree_hash`` before each activation).
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import os
import re
import secrets
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

MOD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
MAX_ARCHIVE = 64 * 1024 ** 2
MAX_EXTRACTED = 512 * 1024 ** 2
MAX_FILES = 20000
MAX_MODS = 64
MAX_ZIP_DIRECTORY = 8 * 1024 ** 2           # bytes of zip central directory (20k entries need ~1-2 MiB)
STALE_AFTER_S = 3600
_swap_lock = threading.Lock()


class ModError(ValueError):
    pass


def tree_hash(root: Path) -> str:
    """Deterministic content hash of a directory (paths, exec bits, file bytes)."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            h.update(f"L {rel} -> {os.readlink(p)}\n".encode())
        elif p.is_file():
            with p.open("rb") as f:                     # streamed: a huge file must not be read into memory
                fh = hashlib.file_digest(f, "sha256").hexdigest()
            x = "x" if os.access(p, os.X_OK) else "-"
            h.update(f"F {rel} {x} {fh}\n".encode())
    return "sha256:" + h.hexdigest()


def list_mods(mods_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(mods_dir)
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or not MOD_RE.match(d.name):
            continue
        run = d / "run.sh"
        readme = next((d / n for n in ("README.md", "readme.md") if (d / n).is_file()), None)
        summary = ""
        if readme:
            summary = next((ln.strip("# ").strip() for ln in readme.read_text(errors="replace").splitlines()
                            if ln.strip()), "")[:200]
        elif run.is_file():
            for ln in run.read_text(errors="replace").splitlines()[1:12]:
                if ln.startswith("#") and len(ln.strip("# ").strip()) > 8:
                    summary = ln.strip("# ").strip()[:200]
                    break
        size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file() and not p.is_symlink())
        out.append({"name": d.name, "has_run_sh": run.is_file(), "hash": tree_hash(d),
                    "size_bytes": size, "summary": summary})
    return out


def mods_status(mods_dir: str | Path, names: list[str]) -> dict[str, Any]:
    installed = {m["name"]: m for m in list_mods(mods_dir)}
    return {n: {"present": n in installed and installed[n]["has_run_sh"],
                "hash": installed.get(n, {}).get("hash")} for n in names}


def install_mod(mods_dir: str | Path, name: str, archive_b64: str) -> dict[str, Any]:
    if not MOD_RE.match(name):
        raise ModError(f"invalid mod name {name!r}")
    try:
        raw = base64.b64decode(archive_b64, validate=True)
    except ValueError as exc:
        raise ModError("archive must be base64") from exc
    if len(raw) > MAX_ARCHIVE:
        raise ModError(f"mod archive larger than {MAX_ARCHIVE // 1024**2} MiB")
    root = Path(mods_dir)
    root.mkdir(parents=True, exist_ok=True)
    _sweep_stale(root)
    if not (root / name).is_dir() and sum(1 for d in root.iterdir() if d.is_dir()
                                           and not d.name.startswith(".")) >= MAX_MODS:
        raise ModError(f"at most {MAX_MODS} mods can be installed; remove one first")
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{name}-", dir=root))
    try:
        _extract(raw, tmp)
        if not (tmp / "run.sh").is_file():
            raise ModError("mod archive must contain run.sh at its top level")
        for p in tmp.rglob("*"):
            if p.is_file() and not p.is_symlink():
                mode = 0o755 if (p.suffix == ".sh" or os.access(p, os.X_OK)) else 0o644
                p.chmod(mode)
        final = root / name
        with _swap_lock:
            if final.exists():
                # Atomic exchange: readers (preflight, container start) never see the mod missing.
                if _exchange(tmp, final):
                    pass                                  # tmp now holds the old version; cleaned below
                else:
                    # A native exchange is unavailable on Windows. Retain the old
                    # tree until publication succeeds, and restore it if rename fails.
                    # Failed recovery backups are deliberately excluded from stale
                    # temp sweeping so a later install cannot erase the only old copy.
                    backup = root / f".backup-{name}-{secrets.token_hex(6)}"
                    final.rename(backup)
                    try:
                        tmp.rename(final)
                    except BaseException:
                        try:
                            backup.rename(final)
                        except OSError as exc:
                            raise ModError("mod replacement and rollback failed; previous version remains at "
                                           f"{backup}") from exc
                        raise
                    tmp = backup
            else:
                tmp.rename(final)
                tmp = None
        return {"name": name, "hash": tree_hash(final),
                "files": sum(1 for p in final.rglob("*") if p.is_file())}
    finally:
        if tmp is not None and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _exchange(a: Path, b: Path) -> bool:
    """renameat2(RENAME_EXCHANGE): swap two directories atomically (Linux). False if unsupported."""
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.renameat2(-100, os.fsencode(a), -100, os.fsencode(b), 2) == 0   # AT_FDCWD, EXCHANGE
    except (OSError, AttributeError):
        return False


def _sweep_stale(root: Path) -> None:
    """Remove temp/trash directories left behind by an install that was interrupted."""
    now = time.time()
    for d in root.glob(".*"):
        if d.is_dir() and d.name.startswith((".tmp-", ".trash-")):
            try:
                if now - d.stat().st_mtime > STALE_AFTER_S:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def _zip_limits(raw: bytes) -> None:
    """Refuse oversized zip directories *before* zipfile builds one object per entry."""
    i = raw.rfind(b"PK\x05\x06", max(0, len(raw) - 65557))
    if i < 0 or len(raw) < i + 22:
        raise ModError("mod archive is not a valid zip")
    entries = int.from_bytes(raw[i + 10:i + 12], "little")
    directory = int.from_bytes(raw[i + 12:i + 16], "little")
    if entries > MAX_FILES or directory > MAX_ZIP_DIRECTORY:
        raise ModError("too many files in mod archive")


def _extract(raw: bytes, dest: Path) -> None:
    total = 0
    if raw[:4] == b"PK\x03\x04":
        _zip_limits(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            infos = [i for i in zf.infolist()]
            if len(infos) > MAX_FILES:
                raise ModError("too many files in mod archive")
            strip = _strip_prefix([i.filename for i in infos])
            for info in infos:
                rel = _rel(info.filename, strip)
                if rel is None:
                    continue
                mode = (info.external_attr >> 16) & 0o777777
                if stat.S_ISLNK(mode):
                    raise ModError(f"symlinks are not allowed in mods: {info.filename}")
                target = dest / rel
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += info.file_size
                if total > MAX_EXTRACTED:
                    raise ModError("mod archive expands beyond the size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                if mode & 0o111:
                    target.chmod(0o755)
        return
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
    except tarfile.TarError as exc:
        raise ModError("mod archive must be a zip or a tar(.gz)") from exc
    with tf:
        members = []
        for m in tf:                          # stop at the limit instead of listing a million entries
            members.append(m)
            if len(members) > MAX_FILES:
                raise ModError("too many files in mod archive")
        strip = _strip_prefix([m.name for m in members])
        for m in members:
            rel = _rel(m.name, strip)
            if rel is None:
                continue
            if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                raise ModError(f"links/devices are not allowed in mods: {m.name}")
            target = dest / rel
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += m.size
            if total > MAX_EXTRACTED:
                raise ModError("mod archive expands beyond the size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            if m.mode & 0o111:
                target.chmod(0o755)


def _strip_prefix(names: list[str]) -> str:
    clean = []
    for n in names:
        p = PurePosixPath(n)
        if p.is_absolute() or ".." in p.parts:
            raise ModError(f"unsafe path in archive: {n!r}")
        parts = [x for x in p.parts if x not in (".", "")]
        if parts:
            clean.append(parts)
    if not clean:
        raise ModError("empty mod archive")
    if any(parts == ["run.sh"] for parts in clean):
        return ""
    first = clean[0][0]
    if all(parts[0] == first for parts in clean) and any(parts[1:] == ["run.sh"] for parts in clean):
        return first
    return ""


def _rel(name: str, strip: str) -> PurePosixPath | None:
    parts = [x for x in PurePosixPath(name).parts if x not in (".", "")]
    if strip and parts and parts[0] == strip:
        parts = parts[1:]
    if not parts:
        return None
    return PurePosixPath(*parts)


def remove_mod(mods_dir: str | Path, name: str) -> bool:
    if not MOD_RE.match(name):
        raise ModError(f"invalid mod name {name!r}")
    d = Path(mods_dir) / name
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True
