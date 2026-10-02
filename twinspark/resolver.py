"""Resolve mutable references to immutable identities (spec §2.3).

The profile system refuses ``main`` / ``latest`` at activation time because the
manager's whole transparency model is built on *immutable* model revision (40-char
commit sha) and *immutable* container image (digest). This module turns a
human-friendly ``org/model@branch`` + ``registry/repo:tag`` into the exact pins a
profile needs.

Two resolution paths, both pure outbound HTTP — they never touch the local vLLM:

* :func:`resolve_hf_revision` — branch/tag → commit sha (40 hex) + the model's
  ``config.json`` + safetensors ``total_size``, so a profile can be built with an
  exact, planner-ready ``ModelSpec`` in one round trip.
* :func:`resolve_image_digest` — ``registry/repo:tag`` → ``registry/repo@sha256:…``
  via the Docker Registry HTTP API v2.

Both degrade gracefully offline / on failure so `tsm plan` and the GUI can run
against a model the controller has never downloaded.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .controller.planner import ModelSpec, make_spec_from_hf
from .netguard import guard_request

_SHA40 = re.compile(r"^[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_TAG = re.compile(r"^[\w][\w.-]{0,127}\Z")
_HF_API = "https://huggingface.co/api"
_SHA_HEADER = "x-repo-commit"

# Total bytes carried by a model's safetensors shards. Read from the index file's
# ``metadata.total_size`` (authors that publish it) — the planner favours this over
# a param-count estimate. Absent that, we fall back to summing shard sizes.
_INDEX_META = ("metadata", "total_size")


def _strip_ref(ref: str) -> tuple[str, str]:
    """Split 'org/name@branch' into (repo, branch)."""
    if len(ref) > 400:
        raise ValueError("model reference is too long")
    if "@" in ref:
        repo, _, branch = ref.partition("@")
    else:
        repo, branch = ref, "main"
    if not _REPO.match(repo) or any(p in ("", ".", "..") for p in repo.split("/")):
        raise ValueError(f"malformed model repo: {repo[:120]!r}")
    if not _TAG.match(branch):
        raise ValueError(f"malformed branch/tag: {branch!r}")
    return repo, branch


def resolve_hf_revision(ref: str, token: Optional[str] = None,
                        client: Optional[httpx.Client] = None,
                        want_config: bool = True) -> dict[str, Any]:
    """Resolve ``org/model@branch`` → commit sha + config.json + weight size.

    Returns a dict suitable for building a :class:`ModelSpec` and pinning a profile:
    ``{repo, branch, revision, config, weight_bytes}``. Raises ``ValueError`` when
    the repo/branch cannot be resolved.
    """
    repo, branch = _strip_ref(ref)
    close = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=True,
                                    headers={"User-Agent": "twinspark-manager"})
    token = token or None
    try:
        # 1) resolve the branch to a commit sha
        h = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            info = client.get(f"{_HF_API}/models/{repo}/revision/{quote(branch, safe='')}",
                              params={"blobs": "true"}, headers=h).raise_for_status().json()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            hint = " (gated/private repo? store an hf_token with `tsm init --hf-token`)" \
                if code in (401, 403) else ""
            raise ValueError(f"Hugging Face returned {code} for {repo}@{branch}{hint}") from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ValueError(f"cannot reach Hugging Face ({type(exc).__name__})") from exc
        sha = info.get("sha", "")
        if not isinstance(sha, str) or not _SHA40.fullmatch(sha):
            raise ValueError(f"could not resolve branch {branch!r} of {repo!r} to a commit sha")
        if not want_config:
            return {"repo": repo, "branch": branch, "revision": sha, "config": None,
                    "weight_bytes": None}

        # 2) pull config.json at that revision
        base = f"https://huggingface.co/{repo}/resolve/{sha}"
        cfg_resp = client.get(f"{base}/config.json", headers=h, follow_redirects=True)
        cfg_resp.raise_for_status()
        try:
            config: dict[str, Any] = cfg_resp.json()
        except json.JSONDecodeError:
            raise ValueError(f"{repo}@{branch} has no valid config.json") from None

        # 3) best-effort weight size from the safetensors index
        weight_bytes: Optional[int] = None
        try:
            idx = client.get(f"{base}/model.safetensors.index.json",
                             headers=h, follow_redirects=True).raise_for_status().json()
            total = idx.get(_INDEX_META[0], {}).get(_INDEX_META[1])
            if isinstance(total, int) and total > 0:
                weight_bytes = total
        except (httpx.HTTPError, json.JSONDecodeError, AttributeError):
            weight_bytes = None

        if weight_bytes is None:
            shards = [s for s in info.get("siblings", [])
                      if "/" not in s.get("rfilename", "")
                      and s.get("rfilename", "").endswith(".safetensors")]
            if shards and all(isinstance(s.get("size"), int) and s["size"] > 0 for s in shards):
                weight_bytes = sum(s["size"] for s in shards)

        return {"repo": repo, "branch": branch, "revision": sha,
                "config": config, "weight_bytes": weight_bytes}
    finally:
        if close:
            client.close()


def spec_from_resolved(resolved: dict[str, Any]) -> ModelSpec:
    """Build a planner ModelSpec from :func:`resolve_hf_revision` output."""
    return make_spec_from_hf(resolved["config"], resolved.get("weight_bytes"))


_MANIFEST_ACCEPT = ", ".join([
    # multi-arch indexes first: pinning a single-platform manifest picked by the
    # registry's default (amd64!) would break an arm64 Spark
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def split_image_ref(image: str) -> tuple[str, Optional[str], Optional[str]]:
    """``registry:5000/org/repo:tag@sha256:..`` -> (name, tag, digest)."""
    name, _, digest = image.partition("@")
    tag = None
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name.rsplit(":", 1)
    return name, tag, (digest or None)


def looks_like_registry_ref(image: str) -> bool:
    """``vllm-node-b12x`` (local build) vs ``eugr/spark-vllm`` / ``ghcr.io/x/y:tag``."""
    name, _, _ = split_image_ref(image)
    return "/" in name


def resolve_image_digest(image: str, client: Optional[httpx.Client] = None) -> str:
    """Resolve ``registry/org/repo:tag`` → ``registry/org/repo@sha256:…``.

    If ``image`` already is a digest reference (``@sha256:``) it is returned
    unchanged. Uses the registry v2 manifest endpoint; multi-arch *index*
    digests are preferred so the pin stays valid for arm64.
    """
    if "@sha256:" in image:
        return image
    name, tag, _ = split_image_ref(image)
    tag = tag or "latest"
    parts = name.split("/")
    if len(parts) < 2:
        raise ValueError(f"image needs at least org/repo (local images are pinned via the agents): {image!r}")
    if not all(_REPO.match(p) for p in parts[1:]) or not _TAG.match(tag):
        raise ValueError(f"malformed image reference: {image!r}")

    first = parts[0]
    is_host = "." in first or ":" in first or first == "localhost"
    registry = first if is_host else "registry-1.docker.io"
    repo_path = "/".join(parts[1:]) if is_host else "/".join(parts)
    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"
    url = f"https://{registry}/v2/{repo_path}/manifests/{tag}"
    close = client is None
    # image references come from the caller: never let them reach loopback / LAN addresses
    client = client or httpx.Client(timeout=20, follow_redirects=True,
                                    event_hooks={"request": [guard_request]})
    headers = {"Accept": _MANIFEST_ACCEPT}
    try:
        r = client.head(url, headers=headers)
        if r.status_code in (401, 403):
            token = _registry_token(client, r.headers.get("www-authenticate", ""), repo_path)
            if token:
                headers["Authorization"] = f"Bearer {token}"
                r = client.head(url, headers=headers)
        if r.status_code == 405:           # some registries do not implement HEAD
            r = client.get(url, headers=headers)
        if r.status_code != 200:
            raise ValueError(f"registry {registry} returned {r.status_code} for {image!r}")
        digest = r.headers.get("docker-content-digest", "")
        if not _DIGEST.match(digest):
            raise ValueError(f"no valid digest in response for {image!r}")
        return f"{name}@{digest}"
    except httpx.HTTPError as exc:
        raise ValueError(f"registry lookup failed for {image!r}: {type(exc).__name__}") from exc
    finally:
        if close:
            client.close()


def _registry_token(client: httpx.Client, challenge: str, repo_path: str) -> str:
    """Anonymous bearer token dance (Docker Hub, ghcr.io, nvcr.io public repos)."""
    fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = fields.get("realm")
    if not realm:
        return ""
    params = {"scope": fields.get("scope") or f"repository:{repo_path}:pull"}
    if fields.get("service"):
        params["service"] = fields["service"]
    try:
        tok = client.get(realm, params=params)
    except httpx.HTTPError:
        return ""
    if tok.status_code != 200:
        return ""
    data = tok.json()
    return data.get("token") or data.get("access_token") or ""


def resolve_model_ref(ref: str, token: Optional[str] = None,
                      client: Optional[httpx.Client] = None) -> tuple[str, str]:
    """``org/repo`` / ``org/repo@branch`` / ``org/repo@<sha>`` -> (repo, sha)."""
    repo, _, branch = ref.partition("@")
    if branch and _SHA40.fullmatch(branch.lower()):
        return repo, branch.lower()
    resolved = resolve_hf_revision(f"{repo}@{branch or 'main'}", token=token, client=client,
                                   want_config=False)
    return resolved["repo"], resolved["revision"]
