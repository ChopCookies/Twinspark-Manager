"""Pinned prerequisites of a recipe: patches TwinSpark installs itself, instead of instructions in prose.

A recipe names the mods it needs in ``advanced.mods``. For a mod that replaces one file of the image with a file
from an upstream repository (most community patches for the Sparks), the recipe also carries the patch itself,
pinned, under ``source.mod_artifacts``::

    "mod_artifacts": {
      "glm53-sm121-sparse-attn": {
        "kind": "file-patch",
        "repo": "owner/name", "commit": "<40 hex>", "path": "docker/file.py",
        "sha256": "<64 hex: the file at that commit>",
        "target": "/usr/local/lib/python3.12/dist-packages/.../file.py",
        "license": "Apache-2.0",
        "image": "ghcr.io/...@sha256:...",      # the image it was written for (shown; the patch checks its target)
        "why": "what breaks without it"
      }
    }

TwinSpark fetches exactly that file at that commit (never a branch) and checks its SHA-256. It then builds the
mod exactly as ``tsm mods file-patch`` does, installs identical content on every node and checks the nodes
afterwards. The descriptor alone determines the mod's ``run.sh`` and its patch file. So a revision that pins the
descriptor also pins what "the right patch" means. A mod installed by hand under the same name with other
content is reported as not matching, not silently used.

States, as the Mods page and ``tsm mods required`` show them: missing on a node, installed but different
between the nodes, installed and identical, and matching the recipe's pinned patch or not. Whether the patch
works on the GPU shows only in a real activation (its validation record).
"""

from __future__ import annotations

import base64
import hashlib
import io
import posixpath
import re
import shlex
import zipfile
from typing import Any, Optional

MAX_FILE = 8 * 1024 * 1024
KINDS = ("file-patch",)
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_COMMIT = re.compile(r"^[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"^[0-9a-f]{64}\Z")
_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,255}\Z")
_TARGET = re.compile(r"^/[A-Za-z0-9._/+-]{1,255}\Z")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

# `tsm mods file-patch` writes the same run.sh: a mod made by hand from the same file is the same mod.
FILE_PATCH_RUN = """#!/usr/bin/env bash
# TwinSpark mod {name}: replaces one file inside the vLLM image before vLLM starts.
# Made with `tsm mods file-patch`. target: {target}   sha256: {digest}
set -euo pipefail
target={q_target}
if [ ! -f "$target" ]; then
  echo "[mod {name}] $target does not exist in this image - is this the image the patch was made for?" >&2
  exit 1
fi
install -m 0644 ./{q_file} "$target"
if command -v sha256sum >/dev/null 2>&1; then
  got="$(sha256sum "$target" | cut -d' ' -f1)"
  if [ "$got" != "{digest}" ]; then
    echo "[mod {name}] $target has sha256 $got after the copy, expected {digest}" >&2
    exit 1
  fi
fi
echo "[mod {name}] replaced $target (sha256 {short})"
"""


class ArtifactError(ValueError):
    """A descriptor that is not complete or not pinned, or a download that is not the pinned file."""


def file_patch_run_sh(name: str, target: str, digest: str, filename: str) -> str:
    return FILE_PATCH_RUN.format(name=name, target=target, digest=digest, short=digest[:12],
                                 q_target=shlex.quote(target), q_file=shlex.quote(filename))


def parse(name: str, doc: Any) -> dict[str, Any]:
    """A validated descriptor, or ArtifactError naming the field."""
    if not _NAME.match(str(name)):
        raise ArtifactError(f"not a mod name: {name!r}")
    if not isinstance(doc, dict):
        raise ArtifactError(f"{name}: the descriptor must be an object")
    kind = doc.get("kind", "file-patch")
    if kind not in KINDS:
        raise ArtifactError(f"{name}: kind {kind!r} is not supported (only {', '.join(KINDS)})")
    checks = (("repo", _REPO, "owner/name of a GitHub repository"),
              ("commit", _COMMIT, "a full 40-character commit (a branch or tag can change)"),
              ("path", _PATH, "the file's path in the repository"),
              ("sha256", _SHA256, "the file's SHA-256 (64 lowercase hex characters)"),
              ("target", _TARGET, "the absolute path of the file in the image"))
    out: dict[str, Any] = {"name": name, "kind": kind}
    for key, rx, what in checks:
        value = doc.get(key)
        if not isinstance(value, str) or not rx.match(value):
            raise ArtifactError(f"{name}: {key} must be {what}")
        out[key] = value
    if ".." in out["path"].split("/") or ".." in out["target"].split("/"):
        raise ArtifactError(f"{name}: paths must not contain '..'")
    for key in ("license", "image", "why"):
        if doc.get(key) is not None:
            if not isinstance(doc[key], str) or len(doc[key]) > 500:
                raise ArtifactError(f"{name}: {key} must be a short text")
            out[key] = doc[key]
    if not out.get("license"):
        raise ArtifactError(f"{name}: license is required (the recipe redistributes someone's code)")
    out["filename"] = posixpath.basename(out["path"])
    if out["filename"] in ("run.sh", "") or out["filename"].startswith("."):
        raise ArtifactError(f"{name}: the patched file cannot be called {out['filename']!r}")
    return out


def of_source(source: Any) -> dict[str, dict[str, Any]]:
    """The valid descriptors of a recipe's or profile's ``source`` (an invalid one raises)."""
    docs = (source or {}).get("mod_artifacts") if isinstance(source, dict) else None
    if not docs:
        return {}
    if not isinstance(docs, dict):
        raise ArtifactError("source.mod_artifacts must map mod names to descriptors")
    return {name: parse(name, doc) for name, doc in docs.items()}


def raw_url(art: dict[str, Any]) -> str:
    return f"https://raw.githubusercontent.com/{art['repo']}/{art['commit']}/{art['path']}"


def review_url(art: dict[str, Any]) -> str:
    return f"https://github.com/{art['repo']}/blob/{art['commit']}/{art['path']}"


def files(art: dict[str, Any], data: bytes) -> dict[str, bytes]:
    """The mod's files, from the pinned file's bytes (checked here once more)."""
    got = hashlib.sha256(data).hexdigest()
    if got != art["sha256"]:
        raise ArtifactError(f"{art['name']}: {art['path']} at {art['commit'][:12]} has sha256 {got}, "
                            f"the recipe pins {art['sha256']} — not installing")
    run = file_patch_run_sh(art["name"], art["target"], art["sha256"], art["filename"])
    return {"run.sh": run.encode("utf-8"), art["filename"]: data}


def expected(art: dict[str, Any]) -> dict[str, str]:
    """{file: sha256} the installed mod must contain. The descriptor alone decides it, no download needed."""
    run = file_patch_run_sh(art["name"], art["target"], art["sha256"], art["filename"]).encode("utf-8")
    return {"run.sh": hashlib.sha256(run).hexdigest(), art["filename"]: art["sha256"]}


def matches(art: dict[str, Any], installed: Optional[dict[str, str]]) -> Optional[bool]:
    """True if an installed mod has the pinned patch (other files, a README for instance, are ignored: run.sh
    never reads them); None if the node did not list its files."""
    if installed is None:
        return None
    return all(installed.get(path) == digest for path, digest in expected(art).items())


def pack(contents: dict[str, bytes]) -> str:
    """The mod as the base64 zip the agents install (run.sh executable)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(contents):
            info = zipfile.ZipInfo(path)
            info.external_attr = (0o755 if path.endswith(".sh") else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, contents[path])
    return base64.b64encode(buf.getvalue()).decode()


async def fetch(art: dict[str, Any], client=None) -> bytes:
    """The pinned file from GitHub, checked against its SHA-256."""
    import httpx
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=60)
    try:
        r = await client.get(raw_url(art))
        if r.status_code != 200:
            raise ArtifactError(f"{art['name']}: GitHub answered {r.status_code} for {raw_url(art)}")
        data = r.content
    except httpx.HTTPError as exc:
        raise ArtifactError(f"{art['name']}: GitHub is not reachable ({type(exc).__name__})") from exc
    finally:
        if own:
            await client.aclose()
    if len(data) > MAX_FILE:
        raise ArtifactError(f"{art['name']}: the file is larger than {MAX_FILE // 2**20} MiB")
    files(art, data)                         # raises on a hash mismatch
    return data
