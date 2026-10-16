"""The contract every workstation-client integration follows.

An adapter connects one local agent app (Hermes first; others later) to the TwinSpark inference gateway.
The client web app drives every adapter through the same steps, so the Integrations page can show one
flow for all of them:

``detect`` → ``inspect`` → ``preview`` → ``apply`` → ``validate`` → ``launch`` / ``stop`` → ``status`` /
``logs`` → ``restore`` (or ``disconnect``).

Two rules hold for every adapter:

* Changes to the app's files are previewed first, applied only when the files still match the preview, and
  always backed up, so ``restore`` can put back the exact bytes.
* Only the *inference* key is ever written, and only to the app's secret store. Results, previews, logs and
  exceptions never carry key values; ``to_dict()`` of every result is safe to send to the browser.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from ...controller.integrations import normalize_url

SOURCES = ("workstation-forward", "tailnet", "spark-local")
LAYERS = ("transport", "auth", "model", "parser", "client")
CHECK_STATUSES = ("pass", "fail", "warn", "skip")
DETECTION_STATES = ("not-installed", "launcher-broken", "installed")
LAUNCH_MODES = ("cli", "gateway")


class AdapterError(Exception):
    """A refusal or failure the user can act on.

    ``str(exc)`` is a plain sentence that says what to do next and never contains a secret, so the web app
    can show it as it is. ``code`` is stable for programs (``stale-preview``, ``context-too-small``, …).
    """

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value.to_dict() if hasattr(value, "to_dict") else dataclasses.asdict(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class _Plain:
    """``to_dict()`` for result dataclasses: nested results are converted with their own ``to_dict``."""

    def to_dict(self) -> dict:
        return {f.name: _plain(getattr(self, f.name)) for f in dataclasses.fields(self)}  # type: ignore[arg-type]


@dataclass(frozen=True)
class Endpoint:
    """The gateway address as seen from the machine where the app runs, and the model alias to use.

    ``base_url`` is normalised to end in ``/v1``. ``context_length`` is the *deployed* limit of the alias
    (``max_model_len`` of the serving route), never a wished-for number. ``api_key`` is the inference key;
    it is left out of ``repr``, equality and ``to_dict`` so it cannot leak through logging or JSON.
    """

    base_url: str
    alias: str
    api_key: Optional[str] = field(repr=False, compare=False)
    context_length: Optional[int]
    revision_id: Optional[str] = None
    served_model: Optional[str] = None
    source: str = "workstation-forward"

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_url(self.base_url))
        if (not isinstance(self.alias, str) or not self.alias or len(self.alias) > 128
                or any(c.isspace() or ord(c) < 32 for c in self.alias)):
            raise ValueError("use a model alias without spaces, as TwinSpark lists it (for example default)")
        if self.context_length is not None and (isinstance(self.context_length, bool)
                                                or not isinstance(self.context_length, int)
                                                or self.context_length < 1):
            raise ValueError("context_length must be a positive number of tokens or None when unknown")
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {', '.join(SOURCES)}")
        if self.api_key is not None and (not isinstance(self.api_key, str) or not self.api_key
                                         or len(self.api_key) > 512
                                         or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in self.api_key)):
            # The message never repeats the value: it might be a pasted management key.
            raise ValueError("the inference key must be one line without spaces; copy it from TwinSpark again")

    def to_dict(self) -> dict:
        return {"base_url": self.base_url, "alias": self.alias, "context_length": self.context_length,
                "revision_id": self.revision_id, "served_model": self.served_model, "source": self.source,
                "has_api_key": self.api_key is not None}


@dataclass
class Check(_Plain):
    """One validation result. ``layer`` says where a failure is, so the page can say what to fix:

    transport (forward/port/network) · auth (key) · model (alias, recipe, runtime) · parser (tool calls,
    chat template) · client (the app's own files and launcher).
    """

    id: str
    title: str
    layer: str
    status: str
    detail: str
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.layer not in LAYERS:
            raise ValueError(f"unknown check layer {self.layer!r}")
        if self.status not in CHECK_STATUSES:
            raise ValueError(f"unknown check status {self.status!r}")


def checks_passed(checks: list[Check], layers: tuple[str, ...] = LAYERS) -> bool:
    """True when no check in ``layers`` failed and at least one of them passed."""
    relevant = [c for c in checks if c.layer in layers]
    return bool(relevant) and all(c.status != "fail" for c in relevant) and any(c.status == "pass" for c in relevant)


@dataclass
class Detection(_Plain):
    """What is installed where. ``launcher`` is the command TwinSpark will use (it works); entries in
    ``broken_launchers`` exist but cannot start the app, each with the exact path and the reason."""

    state: str
    launcher: Optional[str]
    home: str
    profiles: list[str]
    version: Optional[str]
    running: bool
    gateway_pid: Optional[int]
    notes: list[str] = field(default_factory=list)
    profile: Optional[str] = None
    profile_home: Optional[str] = None
    platform: Optional[str] = None
    host: str = "this computer"
    on_path: Optional[str] = None
    broken_launchers: list[dict] = field(default_factory=list)
    service: Optional[dict] = None
    config_path: Optional[str] = None
    env_path: Optional[str] = None
    started_by_twinspark: bool = False
    gateway_status: Optional[str] = None
    doctor: Optional[list[str]] = None
    model: Optional[dict] = None              # the app's current main-model settings, without secrets
    other_homes: list[str] = field(default_factory=list)   # further data directories that exist but are not used

    def __post_init__(self) -> None:
        if self.state not in DETECTION_STATES:
            raise ValueError(f"unknown detection state {self.state!r}")


@dataclass
class FileChange(_Plain):
    """One file in a preview. ``diff`` is unified and masked; ``before_hash`` is the SHA-256 of the current
    bytes (None when the file does not exist yet)."""

    path: str
    before_hash: Optional[str]
    diff: str
    changed: bool
    label: str = ""


@dataclass
class Preview(_Plain):
    """A proposed change. Apply it by passing ``token`` back; ``token`` is None when ``blocked`` explains why
    the change must not be made."""

    files: list[FileChange]
    warnings: list[str]
    token: Optional[str]
    blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(f.changed for f in self.files)


@dataclass
class Backup(_Plain):
    id: str
    kind: str                     # apply · disconnect · restore (taken just before a restore)
    created: float
    location: str
    files: list[dict] = field(default_factory=list)   # {name, path, existed, sha256, mode, backup_path}


@dataclass
class ApplyResult(_Plain):
    changed: bool
    backup: Optional[Backup]
    files: list[str]
    notes: list[str] = field(default_factory=list)


@dataclass
class RestoreResult(_Plain):
    backup: Backup
    restored: list[str]
    removed: list[str]
    safety_backup: Optional[Backup] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class LaunchResult(_Plain):
    """``action``: command (shown for the user to run) · attached (already running, nothing started) ·
    started (TwinSpark started it) · service-started (the installed service was asked to start)."""

    mode: str
    action: str
    command: list[str]
    command_line: str
    pid: Optional[int] = None
    log_path: Optional[str] = None
    detail: str = ""
    host: str = "this computer"


@dataclass
class StopResult(_Plain):
    stopped: bool
    method: Optional[str]          # terminated (TwinSpark's own process) · service (gateway stop) · None
    pid: Optional[int]
    detail: str


@dataclass
class State(_Plain):
    """Separate facts, because each needs a different fix:

    installed – the app is on this host · configured – its files point at this endpoint and alias with the
    key in place · reachable – the endpoint answers from this host · compatible – checks passed for this
    revision · running – its gateway process is alive. ``None`` means "not determined" (no endpoint given).
    """

    installed: bool
    configured: Optional[bool]
    reachable: Optional[bool]
    compatible: Optional[bool]
    running: bool
    launcher_ok: bool = False

    @property
    def phase(self) -> str:
        """One word for a badge; the booleans stay the source of truth."""
        if not self.installed:
            return "not-installed"
        if not self.launcher_ok:
            return "launcher-broken"
        if self.configured is False:
            return "not-configured"
        if self.reachable is False:
            return "unreachable"
        if self.running:
            return "running"
        return "ready" if self.compatible else "unverified"

    def to_dict(self) -> dict:
        return {**super().to_dict(), "phase": self.phase}


@dataclass
class Status(_Plain):
    state: State
    detection: Detection
    config: dict
    logs: list[str]
    error_logs: list[str]
    notes: list[str] = field(default_factory=list)


class ClientAdapter(ABC):
    """Lifecycle of one local agent app. All methods are async because the host may be reached over SSH."""

    id: ClassVar[str]
    title: ClassVar[str]

    @abstractmethod
    async def detect(self) -> Detection:
        """Find the app, its data directory, profiles, launcher and running processes. Changes nothing."""

    @abstractmethod
    async def inspect(self, endpoint: Optional[Endpoint] = None) -> dict:
        """Report the app's current model settings and anything stale, conflicting or leaving the Sparks."""

    @abstractmethod
    async def preview(self, endpoint: Endpoint) -> Preview:
        """The additive change that points the app at ``endpoint``, as masked diffs plus an apply token."""

    @abstractmethod
    async def apply(self, endpoint: Endpoint, token: str) -> ApplyResult:
        """Back up and write the previewed change; refuses when the files changed since the preview."""

    @abstractmethod
    async def validate(self, endpoint: Endpoint) -> list[Check]:
        """Endpoint checks (transport, auth, model, parser) plus the app's own client-layer checks."""

    @abstractmethod
    async def launch(self, mode: str = "cli") -> LaunchResult:
        """``cli``: the exact command for a terminal. ``gateway``: attach to or start the background service."""

    @abstractmethod
    async def stop(self) -> StopResult:
        """Stop only what TwinSpark started, or the selected installed service."""

    async def restart(self) -> LaunchResult:
        await self.stop()
        return await self.launch("gateway")

    @abstractmethod
    async def status(self, endpoint: Optional[Endpoint] = None) -> Status:
        """Detection, the ``State`` booleans and recent redacted log lines."""

    @abstractmethod
    async def logs(self, n: int = 50) -> list[str]:
        """The last ``n`` log lines, cleaned of control characters and with secrets masked."""

    @abstractmethod
    async def backups(self) -> list[Backup]:
        """Backups TwinSpark took, newest last."""

    @abstractmethod
    async def restore(self, backup_id: Optional[str] = None) -> RestoreResult:
        """Put back the files from the latest (or the given) backup, byte for byte."""

    @abstractmethod
    async def preview_disconnect(self) -> Preview:
        """The change that removes only TwinSpark's settings again."""

    @abstractmethod
    async def disconnect(self, token: Optional[str] = None) -> ApplyResult:
        """Remove only TwinSpark's settings and key, preserving everything else."""
