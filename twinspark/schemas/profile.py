"""Profile and immutable revision model (spec §2.3, §17, §18).

Every save produces an immutable revision. Revisions pin exact, immutable
identifiers; mutable references (``latest``, ``main``) are rejected when a
revision is created (``ImmutableIdentity`` validators).

A profile also keeps a mutable *working draft* (``Profile.draft``). A recipe
imported from the cookbook lands there first — with every setting intact — and
becomes an activatable revision once it is pinned to a model commit sha and an
image digest (``Controller.pin_profile``).

Flag handling: all vLLM flags are stored in *canonical* form — lower-case,
hyphen-separated, without leading dashes (``max-model-len``). Anything the
user types (``--max_model_len``, ``max_model_len``, ``-tp``) is normalised
before the manager-owned check, so owned flags can never sneak through a
different spelling. Dotted JSON sub-keys (``default-chat-template-kwargs.thinking``)
keep their key path verbatim; only the flag part is canonicalised.
"""

from __future__ import annotations

import copy
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from .enums import (
    DistributedBackend,
    MemoryStrategy,
    Quantization,
    RuntimeAdapter,
    Topology,
    VerificationStatus,
)

# vLLM's short aliases for parallelism flags
_SHORT_ALIASES = {"tp": "tensor-parallel-size", "pp": "pipeline-parallel-size",
                  "dp": "data-parallel-size"}


def canonical_flag(name: str) -> str:
    """``--Max_Model_Len`` -> ``max-model-len``; ``-tp`` -> ``tensor-parallel-size``;
    ``--default-chat-template-kwargs.enable_thinking`` keeps the key path."""
    raw = name.strip().lstrip("-")
    base, dot, rest = raw.partition(".")
    base = base.lower().replace("_", "-")
    base = _SHORT_ALIASES.get(base, base)
    return f"{base}.{rest}" if dot else base


def flag_base(name: str) -> str:
    """Canonical flag without JSON sub-key or ``=value`` part."""
    return canonical_flag(name.split("=", 1)[0]).split(".", 1)[0]


# Manager-owned parameters that raw vLLM arguments can NEVER override (spec §18).
MANAGER_OWNED: frozenset[str] = frozenset(
    canonical_flag(f)
    for f in (
        "model", "revision", "served-model-name", "tensor-parallel-size",
        "pipeline-parallel-size", "data-parallel-size", "distributed-executor-backend",
        "port", "host", "api-key", "nnodes", "node-rank", "master-addr", "master-port",
        "headless", "gpu-memory-utilization", "download-dir", "config",
        "data-parallel-address", "data-parallel-rpc-port", "data-parallel-size-local",
        "data-parallel-start-rank",
    )
)

_MUTABLE_REFS = {"", "latest", "main", "master", "head", "nightly"}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")  # full HF commit sha (snapshot dir name)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_MOD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FLAG_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[A-Za-z0-9_.-]+)?$")
_NUMBER_RE = re.compile(r"^-?[0-9.]+([eE][-+]?[0-9]+)?$")
_GLOB_RE = re.compile(r"^[A-Za-z0-9._*?/\[\]-]{1,128}$")
_MODEL_REF_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9._/-]{1,128})?$")
MAX_CONTEXT = 4 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_raw_args(args: list[str]) -> list[str]:
    """Validate raw vLLM argv tokens (``--flag``, ``--flag=v``, ``-cc.x=y``, values).

    Rejects any manager-owned flag in any spelling. Values are passed as argv
    items (never through a shell), so no quoting concerns beyond sanity limits.
    """
    if len(args) > 400:
        raise ValueError("too many raw vLLM arguments (max 400)")
    for tok in args:
        if not isinstance(tok, str):
            raise ValueError("raw vLLM arguments must be strings")
        if len(tok) > 16384 or "\x00" in tok:
            raise ValueError("raw vLLM argument too long or contains NUL")
        if tok.startswith("-") and not _NUMBER_RE.match(tok):
            base = flag_base(tok)
            if base in MANAGER_OWNED:
                raise ValueError(f"flag is manager-owned and cannot be overridden: {tok.split('=')[0]}")
    return args


class ImmutableIdentity(BaseModel):
    """Exact, immutable identifiers (spec §2.3)."""

    model_repo: str
    model_revision: str                     # HF commit sha
    quantization: Quantization
    image: str                              # repository without tag, e.g. nvcr.io/nvidia/vllm
    image_digest: str                       # sha256:<64 hex>
    image_source: Literal["registry", "local"] = "registry"
    vllm_version: str = "unknown"
    cuda_version: str = "unknown"
    pytorch_version: str = "unknown"

    @field_validator("model_revision")
    @classmethod
    def _pinned_revision(cls, v: str) -> str:
        if v.lower() in _MUTABLE_REFS or not _SHA_RE.match(v.lower()):
            raise ValueError("model_revision must be the full 40-char commit sha, "
                             "not a branch/tag like 'main'")
        return v.lower()

    @field_validator("image_digest")
    @classmethod
    def _pinned_digest(cls, v: str) -> str:
        if not _DIGEST_RE.match(v):
            raise ValueError("image_digest must look like 'sha256:<64 hex chars>'")
        return v

    @field_validator("image")
    @classmethod
    def _no_tag(cls, v: str) -> str:
        # "registry:5000/repo" has a colon in the host part; only a colon after the
        # last slash is a tag.
        if ":" in v.rsplit("/", 1)[-1] or "@" in v:
            raise ValueError("image must be given without tag/digest; put the digest in image_digest")
        return v

    @property
    def image_ref(self) -> str:
        if self.image_source == "local":
            return self.image_digest  # Docker's immutable local image ID; never pull it
        return f"{self.image}@{self.image_digest}"


class SimpleSettings(BaseModel):
    """Simple Mode fields shown to non-power users (spec §18)."""

    model: str
    quantization: Quantization
    topology: Topology
    # "auto" hands --max-model-len auto to vLLM (largest length the KV pool fits)
    context_length: Union[int, Literal["auto"]] = 32768
    memory_strategy: MemoryStrategy = MemoryStrategy.BALANCED
    concurrency: int = Field(default=1, ge=1, le=512)
    thinking: bool = False
    tool_calling: bool = False
    api_alias: str = "default"
    extra_aliases: list[str] = Field(default_factory=list)

    @field_validator("context_length")
    @classmethod
    def _ctx(cls, v: Union[int, str]) -> Union[int, str]:
        if isinstance(v, str):
            if v != "auto":
                raise ValueError("context_length must be an integer or 'auto'")
            return v
        if not 1024 <= v <= MAX_CONTEXT:
            raise ValueError(f"context_length must be between 1024 and {MAX_CONTEXT}")
        return v

    @field_validator("api_alias")
    @classmethod
    def _alias_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("api_alias must be lower-case letters, digits, '.', '_' or '-'")
        return v

    @field_validator("extra_aliases")
    @classmethod
    def _extra_aliases(cls, v: list[str]) -> list[str]:
        for a in v:
            if not _NAME_RE.match(a):
                raise ValueError(f"invalid alias {a!r}")
        return list(dict.fromkeys(v))

    @property
    def aliases(self) -> list[str]:
        return [self.api_alias] + [a for a in self.extra_aliases if a != self.api_alias]

    def planning_context(self, fallback: int = 131072) -> int:
        """A concrete number for memory estimates when the profile says 'auto'."""
        return self.context_length if isinstance(self.context_length, int) else fallback


class BehaviourSettings(BaseModel):
    """Model behaviour (original spec §5.4): parsers, template, sampling defaults."""

    reasoning_parser: Optional[str] = None      # e.g. qwen3, deepseek_r1, deepseek_v4
    tool_call_parser: Optional[str] = None      # e.g. hermes, glm47, deepseek_v4
    chat_template: Optional[str] = None         # path inside the container
    chat_template_kwargs: dict[str, Any] = Field(default_factory=dict)
    # set False when the recipe passes thinking via extra args itself
    manage_thinking_kwarg: bool = True
    temperature: Optional[float] = Field(default=None, ge=0, le=5)
    top_p: Optional[float] = Field(default=None, gt=0, le=1)
    top_k: Optional[int] = Field(default=None, ge=-1)
    min_p: Optional[float] = Field(default=None, ge=0, le=1)
    repetition_penalty: Optional[float] = Field(default=None, gt=0, le=3)
    max_tokens: Optional[int] = Field(default=None, ge=1)

    def generation_overrides(self) -> dict[str, Any]:
        keys = ("temperature", "top_p", "top_k", "min_p", "repetition_penalty")
        out = {k: getattr(self, k) for k in keys if getattr(self, k) is not None}
        if self.max_tokens is not None:
            out["max_new_tokens"] = self.max_tokens
        return out


class AdvancedSettings(BaseModel):
    """Advanced Mode exposes additional flags (spec §18).

    Parallelism (tp/pp/ep) is *derived from the topology* by the launch planner;
    it is intentionally not a free field here, so the two can never disagree.
    """

    dtype: Optional[str] = None
    kv_dtype: Optional[str] = None               # auto | fp8 | fp8_e4m3 | nvfp4 ...
    block_size: Optional[int] = Field(default=None, ge=1)
    attention_backend: Optional[str] = None
    weight_loader: Optional[str] = None          # --load-format
    tokenizer_mode: Optional[str] = None
    eager_mode: bool = False
    chunked_prefill: Optional[bool] = None
    prefix_cache: bool = True
    max_num_seqs: Optional[int] = Field(default=None, ge=1)
    max_num_batched_tokens: Optional[int] = Field(default=None, ge=1)
    gpu_memory_utilization: Optional[float] = Field(default=None, gt=0, le=0.95)
    trust_remote_code: bool = False
    speculative_config: Optional[dict[str, Any]] = None
    compilation_config: Optional[dict[str, Any]] = None
    env: dict[str, str] = Field(default_factory=dict)
    extra_vllm_flags: dict[str, Any] = Field(default_factory=dict)
    # Raw argv tail, appended verbatim (after manager-owned validation). This is
    # what keeps community recipes lossless: --default-chat-template-kwargs.x=y,
    # -cc.cudagraph_mode=..., repeated flags, order-sensitive flags.
    extra_vllm_args: list[str] = Field(default_factory=list)
    # eugr-style mods (directories with run.sh) applied inside the container
    # before vLLM starts, in order. Must exist in runtime.mods_dir on every node.
    mods: list[str] = Field(default_factory=list)
    # Some two-node recipes start the rank-1 worker first and the head after a
    # short delay (e.g. GLM-5.3 on 2x Spark: ~25 s).
    head_start_delay_s: float = Field(default=0.0, ge=0, le=600)
    # Extra files to download beyond the root checkpoint, e.g. a drafter bundled
    # in a sub-directory ("dflash/*").
    download_include: list[str] = Field(default_factory=list)
    # Additional HF repos the recipe needs on every node (separate drafter
    # models). "org/repo" or "org/repo@branch" in a draft; pinning turns them
    # into "org/repo@<40-char sha>".
    extra_models: list[str] = Field(default_factory=list)

    @field_validator("extra_vllm_flags")
    @classmethod
    def _normalise_and_reject_owned(cls, v: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for flag, value in v.items():
            name = canonical_flag(flag)
            if name.split(".", 1)[0] in MANAGER_OWNED:
                raise ValueError(f"flag is manager-owned and cannot be overridden: {flag}")
            if not _FLAG_NAME_RE.match(name):
                raise ValueError(f"invalid flag name: {flag}")
            out[name] = value
        return out

    @field_validator("extra_vllm_args")
    @classmethod
    def _raw_args(cls, v: list[str]) -> list[str]:
        return check_raw_args(v)

    @field_validator("mods")
    @classmethod
    def _mods(cls, v: list[str]) -> list[str]:
        for m in v:
            if not _MOD_RE.match(m):
                raise ValueError(f"invalid mod name {m!r} (letters, digits, '.', '_', '-')")
        if len(set(v)) != len(v):
            raise ValueError("duplicate mod in list")
        return v

    @field_validator("download_include")
    @classmethod
    def _includes(cls, v: list[str]) -> list[str]:
        for g in v:
            if not _GLOB_RE.match(g) or ".." in g or g.startswith("/"):
                raise ValueError(f"invalid download_include pattern {g!r}")
        return v

    @field_validator("extra_models")
    @classmethod
    def _extra_models(cls, v: list[str]) -> list[str]:
        for ref in v:
            if not _MODEL_REF_RE.match(ref):
                raise ValueError(f"extra model must look like 'org/repo' or 'org/repo@ref': {ref!r}")
        return v

    @field_validator("env")
    @classmethod
    def _env_names(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                raise ValueError(f"invalid environment variable name: {k}")
        return {k: str(val) for k, val in v.items()}


class ProfileDraft(BaseModel):
    """The editable, mutable form. Saving creates a revision."""

    name: str
    description: str = ""
    simple: SimpleSettings
    behaviour: BehaviourSettings = Field(default_factory=BehaviourSettings)
    advanced: AdvancedSettings = Field(default_factory=AdvancedSettings)
    runtime_adapter: RuntimeAdapter = RuntimeAdapter.EUGR
    distributed_backend: DistributedBackend = DistributedBackend.AUTO
    verification: VerificationStatus = VerificationStatus.EXPERIMENTAL
    tags: list[str] = Field(default_factory=list)
    # Where the container image comes from before it is pinned: a registry
    # reference ("ghcr.io/org/img:tag") or a local tag ("vllm-node-b12x").
    image_hint: Optional[str] = None
    # Provenance for recipes: {"recipe": ..., "url": ..., "requirements": [...], ...}
    source: dict[str, Any] = Field(default_factory=dict)
    identity: Optional[ImmutableIdentity] = None

    @field_validator("name")
    @classmethod
    def _profile_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("profile name must be lower-case letters, digits, '.', '_' or '-'")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "ProfileDraft":
        if self.identity is not None:
            if self.identity.quantization != self.simple.quantization:
                raise ValueError("identity.quantization and simple.quantization disagree")
            if self.identity.model_repo != self.simple.model:
                raise ValueError("identity.model_repo and simple.model disagree")
        if self.simple.topology == Topology.SPLIT:
            raise ValueError(
                "topology 'split' needs two models; create two single-a/single-b "
                "profiles with different aliases instead (split profiles: not supported yet)"
            )
        return self

    def warnings(self) -> list[str]:
        """Non-fatal problems worth surfacing in the GUI/CLI."""
        out = []
        if self.simple.thinking and not self.behaviour.reasoning_parser:
            out.append("thinking is on but no reasoning_parser is set — reasoning ends up in content")
        if self.simple.tool_calling and not self.behaviour.tool_call_parser:
            out.append("tool calling is on but no tool_call_parser is set")
        if self.identity is None:
            out.append("not pinned yet — pin a model commit and image digest to activate")
        return out


class ProfileRevision(BaseModel):
    """An immutable revision of a profile (spec §17)."""

    revision_id: str
    profile_name: str
    label: str = "r1"
    created_at: str = Field(default_factory=_now)
    draft: ProfileDraft
    identity: ImmutableIdentity
    pinned: bool = False
    known_good: bool = False

    def required_nodes(self) -> list[str]:
        t = self.draft.simple.topology
        if t == Topology.SINGLE_A:
            return ["A"]
        if t == Topology.SINGLE_B:
            return ["B"]
        return ["A", "B"]

    def show_effective_config(self) -> dict[str, Any]:
        """Full transparency view (spec §2.4). The concrete per-node commands come
        from ``LaunchPlanner`` (see ``/launch-plan``)."""
        return {
            "revision": self.revision_id,
            "identity": self.identity.model_dump(mode="json"),
            "simple": self.draft.simple.model_dump(mode="json"),
            "behaviour": self.draft.behaviour.model_dump(mode="json"),
            "advanced": self.draft.advanced.model_dump(mode="json"),
            "runtime_adapter": self.draft.runtime_adapter.value,
            "distributed_backend": self.draft.distributed_backend.value,
        }


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(d, dict):
        out: dict[str, Any] = {}
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: d}


class Profile(BaseModel):
    """A named profile with its historical revision chain (spec §17)."""

    name: str
    description: str = ""
    revisions: list[ProfileRevision] = Field(default_factory=list)
    # mutable working copy; imported recipes live here until they are pinned
    draft: Optional[ProfileDraft] = None
    pinned_revision: Optional[str] = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    def latest(self) -> Optional[ProfileRevision]:
        return self.revisions[-1] if self.revisions else None

    def working_draft(self) -> Optional[ProfileDraft]:
        if self.draft is not None:
            return self.draft
        last = self.latest()
        return last.draft if last else None

    def add_revision(self, draft: ProfileDraft, identity: ImmutableIdentity) -> ProfileRevision:
        n = len(self.revisions) + 1
        pinned = draft.model_copy(update={"identity": identity}, deep=True)
        rev = ProfileRevision(
            # short, URL-safe and unique: "qwen-flash-r3-9f2c1a7e"
            revision_id=f"{self.name}-r{n}-{secrets.token_hex(4)}",
            profile_name=self.name,
            label=f"r{n}",
            draft=pinned,
            identity=identity,
        )
        self.revisions.append(rev)
        self.draft = pinned.model_copy(deep=True)
        self.description = draft.description or self.description
        self.updated_at = _now()
        return rev

    def get_revision(self, ref: str) -> Optional[ProfileRevision]:
        """Accepts a full revision id, a label (``r3``), ``latest`` or ``pinned``."""
        if ref == "latest":
            return self.latest()
        if ref == "pinned":
            return self.get_revision(self.pinned_revision) if self.pinned_revision else self.latest()
        return next((r for r in self.revisions if ref in (r.revision_id, r.label)), None)

    def diff(self, a: str, b: str) -> dict[str, Any]:
        """Structured diff over *all* settings, not a hand-picked subset (spec §17)."""
        ra, rb = self.get_revision(a), self.get_revision(b)
        if not ra or not rb:
            return {"error": "unknown revision"}
        fa = _flatten(ra.show_effective_config())
        fb = _flatten(rb.show_effective_config())
        fa.pop("revision", None)
        fb.pop("revision", None)
        changed = {
            k: {"from": fa.get(k), "to": fb.get(k)}
            for k in sorted(set(fa) | set(fb))
            if fa.get(k) != fb.get(k)
        }
        return {"a": ra.revision_id, "b": rb.revision_id, "changed": changed}

    def duplicate(self, new_name: str) -> "Profile":
        """Deep-copy a profile under a new name (spec §17)."""
        cloned = copy.deepcopy(self)
        cloned.name = new_name
        cloned.pinned_revision = None
        cloned.created_at = cloned.updated_at = _now()
        for rev in cloned.revisions:
            rev.profile_name = new_name
            rev.revision_id = f"{new_name}-{rev.label}-{secrets.token_hex(4)}"
            rev.draft = rev.draft.model_copy(update={"name": new_name})
            rev.known_good = False
            rev.pinned = False
        if cloned.draft is not None:
            cloned.draft = cloned.draft.model_copy(update={"name": new_name})
        return cloned
