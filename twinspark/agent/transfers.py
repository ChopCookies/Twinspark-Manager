"""Weight transfers run by the agent: Hub download, hash verification, QSFP sync.

* Downloads run ``huggingface_hub.snapshot_download`` in a child process (real
  cancel, no GIL contention with the agent), select inference files only, report
  bytes / rate / ETA, and verify every file against the Hub's sha256 (LFS) or
  git blob hash before the snapshot counts as complete.
* Sync copies exactly one revision (+ the blobs it references) with N parallel
  rsync streams over SSH on the direct link — resumable (``--partial
  --append-verify``), no compression, a fast AEAD cipher.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..weights import (
    _manifest,
    download_progress,
    expected_hashes,
    hf_repo_dir,
    partition_by_size,
    select_files,
    snapshot_complete,
    sync_file_sizes,
    sync_files,
    verify_snapshot,
    write_manifest,
)

if TYPE_CHECKING:
    from ..schemas.config import RuntimeSettings
    from .tasks import TaskRegistry

_PROGRESS_RE = re.compile(rb"^\s*([\d,]+)\s+(\d+)%")
_DOWNLOAD_SCRIPT = (
    "import json, os, sys\n"
    "from huggingface_hub import snapshot_download\n"
    "a = json.loads(sys.argv[1])\n"
    "snapshot_download(token=os.environ.get('HF_TOKEN') or None, **a)\n"
)


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


async def run_download(reg: "TaskRegistry", tid: str, rt: "RuntimeSettings", repo: str, rev: str,
                       include: list[str], token: Optional[str], verify: bool,
                       downloader: Optional[Callable[[dict], None]] = None) -> dict[str, Any]:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError("install the 'hf' extra on this node: pip install twinspark-manager[hf]") from exc
    reg.update(tid, phase="listing", detail=f"reading file list of {repo}@{rev[:8]}")
    info = await asyncio.to_thread(HfApi(token=token).model_info, repo, revision=rev,
                                   files_metadata=True)
    snap = hf_repo_dir(rt.hf_cache_dir, repo) / "snapshots" / rev
    include = sorted(set(include) | set((_manifest(snap) or {}).get("include", [])))
    files = select_files(info.siblings, include)
    total = sum(files.values())
    if rt.max_download_gib is not None and total > rt.max_download_gib * 1024 ** 3:
        raise RuntimeError(f"selected checkpoint is {total / 1024**3:.3f} GiB; "
                           f"download limit is {rt.max_download_gib:g} GiB")
    hub = Path(rt.hf_cache_dir) / "hub"
    snap = hf_repo_dir(rt.hf_cache_dir, repo) / "snapshots" / rev
    missing = sum(size for name, size in files.items()
                  if not (snap / name).is_file() or (snap / name).stat().st_size != size)
    if _free_bytes(hub) < missing + rt.min_disk_free_gib * 1024 ** 3:
        raise RuntimeError(f"not enough disk space: need {missing / 1024**3:.1f} GiB plus the "
                           f"{rt.min_disk_free_gib:g} GiB reserve, have {_free_bytes(hub) / 1024**3:.1f} GiB")
    started = time.time()
    base_done = total - missing
    reg.progress(tid, base_done, total, started, "downloading")
    if missing and downloader is not None:          # in-process hook (tests)
        hub.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(downloader, {"repo_id": repo, "revision": rev, "cache_dir": str(hub),
                                             "allow_patterns": sorted(files), "token": token,
                                             "max_workers": rt.download_workers})
    elif missing:
        hub.mkdir(parents=True, exist_ok=True)
        args = {"repo_id": repo, "revision": rev, "cache_dir": str(hub),
                "allow_patterns": sorted(files), "max_workers": rt.download_workers}
        env = {**os.environ, "HF_HUB_DISABLE_PROGRESS_BARS": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
               "HF_XET_HIGH_PERFORMANCE": os.environ.get("HF_XET_HIGH_PERFORMANCE", "1")}
        env.pop("HF_HUB_OFFLINE", None)
        if token:
            env["HF_TOKEN"] = token
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _DOWNLOAD_SCRIPT, json.dumps(args),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        reg.register_proc(tid, proc)
        tail = bytearray()

        async def pump():
            assert proc.stdout
            async for line in proc.stdout:
                tail.extend(line)
                del tail[:-4000]

        pumper = asyncio.create_task(pump())
        try:
            while proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                done = await asyncio.to_thread(download_progress, rt.hf_cache_dir, repo, rev, files)
                reg.progress(tid, done, total, started, "downloading")
        finally:
            await asyncio.gather(pumper, return_exceptions=True)
        if proc.returncode != 0:
            out = tail.decode(errors="replace")[-1500:]
            raise RuntimeError(f"download failed (rc={proc.returncode}): {out}")
    for name, size in files.items():
        if not (snap / name).is_file() or (snap / name).stat().st_size != size:
            raise RuntimeError(f"download is incomplete: {name}")
    verified = None
    if verify:
        vstart = time.time()
        verified = await asyncio.to_thread(
            verify_snapshot, snap, expected_hashes(info.siblings), files,
            lambda d, t: reg.progress(tid, d, t, vstart, "verifying", "verifying hashes"))
    write_manifest(snap, files, verified, tag=tid, include=include)
    return {"repo": repo, "revision": rev, "files": len(files), "bytes": total,
            "verified": bool(verified), "downloaded_bytes": missing}


async def run_verify(reg: "TaskRegistry", tid: str, rt: "RuntimeSettings", repo: str, rev: str) -> dict:
    """Re-hash a synced snapshot against the hashes recorded when it was downloaded."""
    snap = hf_repo_dir(rt.hf_cache_dir, repo) / "snapshots" / rev
    man = _manifest(snap)
    if not man or not snapshot_complete(snap):
        raise RuntimeError(f"{repo}@{rev[:8]} is not complete on this node")
    files = man["files"]
    recorded = man.get("verified") or {}
    expected = {n: {"sha256": r.get("sha256"), "git_sha1": r.get("git_sha1")}
                for n, r in recorded.items() if not r.get("unverified")}
    if not expected:
        return {"verified": False, "note": "no recorded hashes (download was not verified)"}
    # drop cached records: every byte is re-read on this node
    man.pop("verified", None)
    write_manifest(snap, files, None, tag=tid, include=man.get("include", []))
    vstart = time.time()
    verified = await asyncio.to_thread(
        verify_snapshot, snap, expected, files,
        lambda d, t: reg.progress(tid, d, t, vstart, "verifying", "verifying hashes"))
    write_manifest(snap, files, verified, tag=tid, include=man.get("include", []))
    return {"verified": True, "files": len(verified)}


SSH_HOST_KEY_DIR = Path("/etc/ssh")
_HOST_KEY_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|ssh-rsa) "
                          r"([A-Za-z0-9+/]{40,}={0,3})(?: .*)?\Z")
_PIN_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}\Z")


def own_host_keys(folder: Optional[Path] = None) -> list[str]:
    """This machine's SSH host public keys ("type base64", comments dropped) — for the other node to pin."""
    out = []
    for name in ("ssh_host_ed25519_key.pub", "ssh_host_ecdsa_key.pub", "ssh_host_rsa_key.pub"):
        try:
            text = ((folder or SSH_HOST_KEY_DIR) / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        m = _HOST_KEY_RE.match(text)
        if m:
            out.append(f"{m[1]} {m[2]}")
    return out


def known_hosts_path(rt: "RuntimeSettings") -> Optional[Path]:
    if rt.ssh_known_hosts:
        return Path(rt.ssh_known_hosts)
    return Path(rt.ssh_key).parent / "known_hosts" if rt.ssh_key else None


def pin_host_keys(rt: "RuntimeSettings", host: str, keys: list[str]) -> Path:
    """Make ``keys`` the only accepted host keys of ``host`` in the sync's own known_hosts file.

    The keys come from the other node's agent, over the agent connection the controller authenticates with
    that node's token. SSH then runs with StrictHostKeyChecking=yes, so not even the first connection is
    taken on trust."""
    if not _PIN_HOST_RE.match(host):
        raise ValueError("not a host name")
    lines = []
    for k in keys:
        m = _HOST_KEY_RE.match(str(k).strip())
        if not m:
            raise ValueError("host_keys must be SSH public keys (ssh-ed25519 / ecdsa-sha2-* / ssh-rsa)")
        lines.append(f"{host} {m[1]} {m[2]}")
    if not lines:
        raise ValueError("no host keys to pin")
    path = known_hosts_path(rt)
    if path is None:
        raise ValueError("runtime.ssh_known_hosts (or ssh_key) is not configured on this node")
    try:
        kept = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip() and ln.split(None, 1)[0] not in (host, f"[{host}]:22")]
    except FileNotFoundError:
        kept = []
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(kept + lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def ssh_command(rt: "RuntimeSettings", pinned: bool = False) -> list[str]:
    """``pinned``: the peer's host keys were written by :func:`pin_host_keys` — accept nothing else."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"StrictHostKeyChecking={'yes' if pinned else 'accept-new'}",
           "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
           "-o", "ServerAliveCountMax=6", "-o", "Compression=no", "-T"]
    if rt.ssh_cipher:
        cmd += ["-c", rt.ssh_cipher]
    if rt.ssh_key:
        cmd += ["-i", rt.ssh_key, "-o", "IdentitiesOnly=yes"]
    kh = known_hosts_path(rt) if pinned else (Path(rt.ssh_known_hosts) if rt.ssh_known_hosts else None)
    if kh:
        cmd += ["-o", f"UserKnownHostsFile={kh}"]
    return cmd


def rsync_argv(rt: "RuntimeSettings", src_hub: Path, user: str, host: str, dst_hub: str,
               pinned: bool = False) -> list[str]:
    return ["rsync", "-a", "--partial", "--append-verify", "--mkpath", "--from0",
            "--files-from=-", "--info=progress2", "--timeout=600",
            "-e", shlex.join(ssh_command(rt, pinned)),
            f"{src_hub}/", f"{user}@{host}:{dst_hub.rstrip('/')}/"]


async def run_sync(reg: "TaskRegistry", tid: str, rt: "RuntimeSettings", repo: str, rev: str,
                   include: list[str], host: str, user: str, dst_hf_home: str,
                   streams: int, dry_run: bool, host_keys: Optional[list[str]] = None) -> dict[str, Any]:
    src_hub = Path(rt.hf_cache_dir) / "hub"
    dst_hub = f"{dst_hf_home.rstrip('/')}/hub"
    repo_dir = hf_repo_dir(rt.hf_cache_dir, repo)
    pinned = bool(host_keys)
    if pinned and not dry_run:
        pin_host_keys(rt, host, host_keys or [])
    if dry_run:
        argv = rsync_argv(rt, src_hub, user, host, dst_hub, pinned)
        reg.update(tid, detail="[dry-run] " + shlex.join(argv))
        return {"dry_run": True, "argv": argv}
    rels = await asyncio.to_thread(sync_files, repo_dir, rev, include)
    sizes = sync_file_sizes(src_hub.resolve(), rels)
    total = sum(sizes.values())
    groups = partition_by_size(sizes, streams)
    started = time.time()
    counters = [0] * len(groups)
    tails: list[bytearray] = [bytearray() for _ in groups]

    async def one(i: int, names: list[str]) -> int:
        argv = rsync_argv(rt, src_hub, user, host, dst_hub, pinned)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        reg.register_proc(tid, proc)
        assert proc.stdin and proc.stdout
        proc.stdin.write("\0".join(names).encode() + b"\0")
        await proc.stdin.drain()
        proc.stdin.close()
        buf = b""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            tails[i].extend(chunk)
            del tails[i][:-3000]
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts[-1]
            for part in parts[:-1]:
                m = _PROGRESS_RE.match(part)
                if m:
                    counters[i] = int(m.group(1).replace(b",", b""))
            reg.progress(tid, min(sum(counters), total), total, started, "syncing",
                         f"copying over QSFP ({len(groups)} streams)")
        return await proc.wait()

    reg.progress(tid, 0, total, started, "syncing", f"copying over QSFP ({len(groups)} streams)")
    codes = await asyncio.gather(*(one(i, g) for i, g in enumerate(groups)))
    bad = [(i, c) for i, c in enumerate(codes) if c != 0]
    if bad:
        i, c = bad[0]
        raise RuntimeError(f"rsync stream {i} failed rc={c}: {tails[i].decode(errors='replace')[-1500:]}")
    reg.progress(tid, total, total, started, "syncing", "copy complete")
    return {"files": len(rels), "bytes": total, "streams": len(groups),
            "seconds": round(time.time() - started, 1)}
