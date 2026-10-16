"""Hermes Agent on the workstation: find it, point it at TwinSpark, check it, run it and undo it.

Why this exists: exporting a YAML fragment was not enough. On a real machine Hermes was installed and its
gateway running, yet the shell could not find ``hermes`` (``~/.local/bin`` not on PATH and a wrapper pointing
at an old install), ``config.yaml`` still named an old DeepSeek model and the TwinSpark key was missing.
Every step that took manual edits there is a method here:

* ``detect`` probes the documented install paths, not just PATH, and names each broken launcher with the
  reason and the working fallback it found instead.
* ``inspect`` shows the current model section, stale or conflicting entries and every auxiliary or fallback
  route that still sends requests to a cloud provider (those are reported, never changed).
* ``preview``/``apply`` make the additive change — the ``model`` section and ``TWINSPARK_API_KEY`` in
  ``.env`` — after a byte-exact, timestamped backup, with atomic writes, and only if the files still match
  the preview. ``restore`` puts the backup back; ``disconnect`` removes only TwinSpark's settings.
* ``validate``, ``launch``, ``stop``, ``status`` and ``logs`` cover running it.

Files TwinSpark keeps for itself live in ``<profile home>/.twinspark/`` (backups and a small state file);
the inference key is written only to Hermes's ``.env``, never to config.yaml, a backup manifest or a result.
"""

from __future__ import annotations

import asyncio
import copy
import difflib
import hashlib
import ipaddress
import json
import re
import secrets
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import httpx
import yaml

from ...controller.integrations import normalize_url
from ...remote.bundle import Redactor
from ...remote.logs import clean
from ...security import tokens_equal
from .base import (
    LAUNCH_MODES,
    AdapterError,
    ApplyResult,
    Backup,
    Check,
    ClientAdapter,
    Detection,
    Endpoint,
    FileChange,
    LaunchResult,
    Preview,
    RestoreResult,
    State,
    Status,
    StopResult,
    checks_passed,
)
from .hosts import HostOps, LocalHost, PathInfo
from .validate import REQUEST_TIMEOUT, validate_endpoint

KEY_ENV = "TWINSPARK_API_KEY"
MIN_CONTEXT = 64_000                 # Hermes rejects smaller windows at startup
OUR_MODEL_KEYS = ("provider", "default", "base_url", "key_env", "context_length")
PROFILE_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")
STATE_DIR = ".twinspark"
_STATE_KEYS = ("connected", "previous_model", "previous_model_present")   # what a backup snapshot restores
LOG_TAIL_BYTES = 256 * 1024
LAUNCHER_TIMEOUT = 20.0
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_BACKUP_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
_TOKEN_RE = re.compile(r"^v1\.([0-9a-f]{24})\.([0-9a-f]{24})$")
_ENV_NAME_RE = re.compile(r"^(?P<lead>\s*)(?P<export>export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$")
_SAFE_ENV_VALUE = re.compile(r"^[A-Za-z0-9._~+/=:@%,-]+$")
_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|passw(or)?d)$")
_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.]+)?)")
_EXEC_LINE_RE = re.compile(r"""^\s*(?:exec\s+)?["']?(/[^\s"';|&<>()`$]+)""")
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
_LOCAL_PROVIDERS = {"local", "ollama", "lmstudio", "llamacpp", "llama.cpp", "vllm", "sglang"}
_OFF_PROVIDERS = {"none", "off", "disabled", "false"}


# ---- where things are (pure, so Windows layouts are testable anywhere) ---------------------------------------
def _flavor(platform: str):
    return PureWindowsPath if platform == "windows" else PurePosixPath


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        key = item[0] if isinstance(item, tuple) else item
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _expand(value: str, user_home: str, platform: str) -> str:
    P = _flavor(platform)
    if value == "~" or value.startswith(("~/", "~\\")):
        return str(P(user_home) / value[2:]) if len(value) > 1 else str(P(user_home))
    return str(P(value))


def hermes_home_candidates(platform: str, env: Mapping[str, Optional[str]], user_home: str) -> list[str]:
    """Hermes's data directory, most specific first: ``$HERMES_HOME``, then (Windows source installs)
    ``%LOCALAPPDATA%\\hermes``, then ``~/.hermes``."""
    P = _flavor(platform)
    out = []
    if env.get("HERMES_HOME"):
        out.append(_expand(env["HERMES_HOME"], user_home, platform))
    if platform == "windows" and env.get("LOCALAPPDATA"):
        out.append(str(P(env["LOCALAPPDATA"]) / "hermes"))
    out.append(str(P(user_home) / ".hermes"))
    return _dedupe(out)


def profile_home(platform: str, hermes_home: str, profile: Optional[str]) -> str:
    """The default profile is the Hermes home itself; named profiles live in ``<home>/profiles/<name>``."""
    return str(_flavor(platform)(hermes_home) / "profiles" / profile) if profile else hermes_home


def launcher_candidates(platform: str, env: Mapping[str, Optional[str]], user_home: str,
                        hermes_home: str) -> list[tuple[str, str]]:
    """Documented launcher locations as ``(path, role)``. ``entry`` launchers are what a user's terminal runs;
    ``internal`` ones are the installed program itself, a working fallback when an entry wrapper is stale."""
    P = _flavor(platform)
    out: list[tuple[str, str]] = []
    if platform == "windows":
        roots = [P(env["LOCALAPPDATA"]) / "hermes"] if env.get("LOCALAPPDATA") else []
        roots = _dedupe([*roots, P(hermes_home)])
        for root in roots:
            out += [(str(root / "bin" / "hermes.exe"), "entry"), (str(root / "bin" / "hermes.cmd"), "entry")]
        for root in roots:
            out += [(str(root / "hermes-agent" / venv / "Scripts" / "hermes.exe"), "internal")
                    for venv in (".venv", "venv")]
        return _dedupe(out)
    home = P(user_home)
    out.append((str(home / ".local" / "bin" / "hermes"), "entry"))
    if env.get("PREFIX"):                                   # Termux
        out.append((str(P(env["PREFIX"]) / "bin" / "hermes"), "entry"))
    for root in _dedupe([P(hermes_home), home / ".hermes"]):
        out += [(str(root / "hermes-agent" / venv / "bin" / "hermes"), "internal") for venv in (".venv", "venv")]
    out += [(f"/opt/hermes/{venv}/bin/hermes", "internal") for venv in (".venv", "venv")]   # Docker image
    out.append(("/opt/hermes/bin/hermes", "entry"))
    return _dedupe(out)


def code_dirs(platform: str, user_home: str, hermes_home: str) -> list[str]:
    P = _flavor(platform)
    return _dedupe([str(P(hermes_home) / "hermes-agent"), str(P(user_home) / ".hermes" / "hermes-agent")])


def service_candidates(platform: str, env: Mapping[str, Optional[str]], user_home: str,
                       profile: Optional[str]) -> list[dict]:
    """The gateway's per-user service file for this profile (systemd on Linux, launchd on macOS)."""
    P = _flavor(platform)
    suffix = f"-{profile}" if profile else ""
    if platform == "linux":
        base = P(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else P(user_home) / ".config"
        name = f"hermes-gateway{suffix}.service"
        return [{"kind": "systemd", "name": name, "path": str(base / "systemd" / "user" / name)}]
    if platform == "darwin":
        name = f"ai.hermes.gateway{suffix}"
        return [{"kind": "launchd", "name": name,
                 "path": str(P(user_home) / "Library" / "LaunchAgents" / f"{name}.plist")}]
    return []


def command_line(argv: list[str], platform: str) -> str:
    return subprocess.list2cmdline(argv) if platform == "windows" else shlex.join(argv)


# ---- .env, config.yaml and routing (pure text logic) ----------------------------------------------------------
def _env_value(raw: str) -> str:
    v = raw.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        return v[1:end] if end > 0 else v[1:]
    cut = v.find(" #")
    return v[:cut].rstrip() if cut >= 0 else v


def env_entries(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        m = _ENV_NAME_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            out.append((m.group("name"), _env_value(m.group("value"))))
    return out


def env_get(text: str, name: str) -> Optional[str]:
    values = [v for n, v in env_entries(text) if n == name]
    return values[-1] if values else None       # dotenv semantics: the last assignment wins


def _quote_env(value: str) -> str:
    if _SAFE_ENV_VALUE.match(value):
        return value
    if "'" in value or "\n" in value or "\r" in value:
        raise AdapterError("The inference key contains characters a .env file cannot hold safely; copy the "
                           "inference key from TwinSpark again.", "invalid-key")
    return f"'{value}'"


def _split_eol(line: str) -> tuple[str, str]:
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def env_set(text: str, name: str, value: str) -> str:
    """Set ``name`` in place (first occurrence, keeping ``export``), drop later duplicates, else append.
    Every other line — comments, blank lines, other secrets — stays byte for byte."""
    nl = "\r\n" if "\r\n" in text else "\n"
    out, done = [], False
    for line in text.splitlines(keepends=True):
        body, eol = _split_eol(line)
        m = _ENV_NAME_RE.match(body)
        if m and m.group("name") == name and not body.lstrip().startswith("#"):
            if not done:
                out.append(f"{m.group('lead')}{m.group('export') or ''}{name}={_quote_env(value)}{eol or nl}")
                done = True
            continue
        out.append(line)
    if not done:
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += nl
        out.append(f"{name}={_quote_env(value)}{nl}")
    return "".join(out)


def env_remove(text: str, name: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        m = _ENV_NAME_RE.match(_split_eol(line)[0])
        if m and m.group("name") == name and not line.lstrip().startswith("#"):
            continue
        out.append(line)
    return "".join(out)


def mask_env(text: str, changed: Optional[str] = None) -> str:
    """Every value in .env is a secret: show names only. ``changed`` marks the variable whose value is new,
    so a diff still shows that the key changes without showing either value."""
    out = []
    for line in text.splitlines(keepends=True):
        body, eol = _split_eol(line)
        m = _ENV_NAME_RE.match(body)
        if m and not body.lstrip().startswith("#"):
            marker = "[REDACTED] (new value)" if m.group("name") == changed else "[REDACTED]"
            body = f"{m.group('lead')}{m.group('export') or ''}{m.group('name')}={marker}"
        out.append(body + eol)
    return "".join(out)


def parse_config(data: Optional[bytes]) -> tuple[dict, Optional[str]]:
    if not data:
        return {}, None
    try:
        loaded = yaml.safe_load(data.decode("utf-8"))
    except UnicodeDecodeError:
        return {}, "config.yaml is not UTF-8 text. Fix or remove the file, then preview again."
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        return {}, f"config.yaml is not valid YAML{where}. Fix it (hermes config check helps), then preview again."
    if loaded is None:
        return {}, None
    if not isinstance(loaded, dict):
        return {}, "config.yaml does not contain settings (a mapping) at the top level. Fix it, then preview again."
    return loaded, None


_DELETE = object()


def _top_level_block(lines: list[str], key: str) -> Optional[tuple[int, int]]:
    """Line span of an unquoted top-level ``key:`` and its indented body; None when absent.
    A column-0 comment ends the block (it usually introduces the next section)."""
    pattern = re.compile(rf"^{re.escape(key)}\s*:(\s|$)")
    starts = [i for i, line in enumerate(lines) if pattern.match(line)]
    if len(starts) != 1:
        if starts:
            raise ValueError("ambiguous")
        return None
    start = end = starts[0]
    end += 1
    while end < len(lines):
        line = lines[end]
        if line.strip() == "" or line[:1] in (" ", "\t"):
            end += 1
            continue
        break
    while end > start + 1 and lines[end - 1].strip() == "":
        end -= 1                                   # blank lines before the next section stay where they are
    return start, end


def render_config(text: str, cfg: dict, key: str, value: Any) -> tuple[str, bool]:
    """config.yaml with top-level ``key`` set to ``value`` (or removed for ``_DELETE``).

    Only that block is rewritten, so comments and formatting elsewhere survive; the result is accepted only if
    it parses to exactly the intended settings. Otherwise the whole file is re-serialised and the second value
    is False (PyYAML drops comments — the caller says so in the preview; the backup keeps the original bytes).
    """
    expected = copy.deepcopy(cfg)
    if value is _DELETE:
        expected.pop(key, None)
    else:
        expected[key] = value
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    try:
        span = _top_level_block(lines, key)
        block = "" if value is _DELETE else \
            yaml.safe_dump({key: value}, sort_keys=False, default_flow_style=False, allow_unicode=True,
                           width=4096).replace("\n", nl)
        if span is None:
            if key in cfg:                          # present but written in a form this edit cannot locate
                raise ValueError("not located")
            candidate = text if value is _DELETE else \
                (text if not text or text.endswith(("\n", "\r")) else text + nl) + block
        else:
            candidate = "".join(lines[:span[0]]) + block + "".join(lines[span[1]:])
        if (yaml.safe_load(candidate) or {}) == expected:
            return candidate, True
    except (ValueError, yaml.YAMLError):
        pass
    return yaml.safe_dump(expected, sort_keys=False, default_flow_style=False, allow_unicode=True,
                          width=4096), False


def _has_comments(text: str) -> bool:
    return any(line.lstrip().startswith("#") or " #" in line for line in text.splitlines())


def same_endpoint(a: Optional[str], b: Optional[str]) -> bool:
    """Same gateway, treating localhost, 127.0.0.1 and ::1 as one host and ignoring a trailing slash."""
    if not a or not b:
        return False
    try:
        x, y = urlsplit(normalize_url(str(a))), urlsplit(normalize_url(str(b)))
    except ValueError:
        return str(a).rstrip("/") == str(b).rstrip("/")

    def host(u):
        name = (u.hostname or "").lower()
        return "loopback" if name in _LOOPBACK_NAMES else name

    def port(u):
        return u.port or (443 if u.scheme == "https" else 80)

    return (x.scheme, host(x), port(x), x.path) == (y.scheme, host(y), port(y), y.path)


def _is_local_url(url: str) -> bool:
    try:
        name = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if name in _LOOPBACK_NAMES or name.endswith((".local", ".ts.net", ".lan", ".internal")) or "." not in name:
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr in ipaddress.ip_network("100.64.0.0/10")


def _provider_url(entry: Any) -> Optional[str]:
    if isinstance(entry, dict):
        for k in ("base_url", "baseUrl", "api_base", "url"):
            if isinstance(entry.get(k), str) and entry[k].strip():
                return entry[k].strip()
    return None


def named_providers(cfg: dict) -> dict[str, dict]:
    raw = cfg.get("providers")
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    if isinstance(raw, list):
        return {str(v.get("name") or v.get("id") or i): v for i, v in enumerate(raw) if isinstance(v, dict)}
    return {}


def classify_route(provider: Optional[str], base_url: Optional[str], providers: dict[str, dict],
                   endpoint_url: Optional[str]) -> str:
    """twinspark · main (follows the main model) · local (another machine on this network) · auto (Hermes
    picks the first provider with credentials, which may be a cloud service) · cloud."""
    if base_url:
        if endpoint_url and same_endpoint(base_url, endpoint_url):
            return "twinspark"
        return "local" if _is_local_url(base_url) else "cloud"
    name = (provider or "").strip()
    if name.lower() == "main":
        return "main"
    if name.lower() in _OFF_PROVIDERS:
        return "disabled"
    if name.lower() in _LOCAL_PROVIDERS:
        return "local"
    if name.lower() in ("", "auto", "custom"):
        return "auto"
    if name in providers:
        url = _provider_url(providers[name])
        return classify_route(None, url, {}, endpoint_url) if url else "auto"
    return "cloud"


def scan_routes(cfg: dict, endpoint_url: Optional[str]) -> list[dict]:
    """Every place outside ``model`` that names a model route: ``auxiliary.<task>``, ``compression``'s summary
    model, fallback models/chains, delegation and the like. Unknown layouts are tolerated: any mapping with a
    provider or base_url counts, and plain strings in a ``fallback…`` list are providers."""
    providers = named_providers(cfg)
    routes: list[dict] = []

    def add(path, provider, model, base_url, cls=None):
        routes.append({"task": ".".join(path), "provider": provider, "model": model, "base_url": base_url,
                       "class": cls or classify_route(provider, base_url, providers, endpoint_url)})

    def visit(node, path, depth):
        if depth > 6:
            return
        if isinstance(node, dict):
            if node.get("enabled") is False:
                return
            provider = node.get("provider", node.get("summary_provider"))
            base_url = node.get("base_url", node.get("summary_base_url"))
            model = node.get("model", node.get("summary_model", node.get("default")))
            if path and (isinstance(provider, str) or isinstance(base_url, str) and base_url.strip()):
                add(path, provider if isinstance(provider, str) else None,
                    model if isinstance(model, str) else None,
                    base_url.strip() if isinstance(base_url, str) and base_url.strip() else None)
            for k, v in node.items():
                if isinstance(k, str) and isinstance(v, (dict, list)):
                    visit(v, (*path, k), depth + 1)
                elif isinstance(k, str) and "fallback" in k.lower() and isinstance(v, str) and v.strip():
                    # a bare model name as fallback: not a TwinSpark alias route, so it leaves the Sparks
                    add((*path, k), None, v.strip(), None, "cloud")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str) and path and "fallback" in path[-1].lower() and v.strip():
                    add((*path, str(i)), v.strip(), None, None)
                elif isinstance(v, (dict, list)):
                    visit(v, (*path, str(i)), depth + 1)

    visit({k: v for k, v in cfg.items() if k not in ("model", "providers")}, (), 0)
    return routes


def route_message(route: dict) -> str:
    what = route["provider"] or route["model"] or route["base_url"] or "an unnamed provider"
    model = f" (model {route['model']})" if route["model"] and route["provider"] else ""
    if route["provider"] is None and route["base_url"] is None:
        return f"{route['task']} names {what}, which is not served by the Sparks: these requests leave them."
    if route["class"] == "auto":
        return (f"{route['task']} uses provider {what}: Hermes picks the first provider with credentials, which "
                "can be a cloud service.")
    return f"{route['task']} uses {what}{model}, a cloud service: these requests leave the Sparks."


def _env_decode(data: Optional[bytes]) -> str:
    # surrogateescape: bytes that are not UTF-8 come back out unchanged when the file is written again
    return data.decode("utf-8", "surrogateescape") if data else ""


def _env_encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _printable(text: str) -> str:
    return text.encode("utf-8", "replace").decode("utf-8")


def _sha(data: Optional[bytes]) -> Optional[str]:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _first_line(text: str, limit: int = 160) -> str:
    for line in clean(text).splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


# ---- the adapter ----------------------------------------------------------------------------------------------
@dataclass
class _Paths:
    platform: str
    user_home: str
    env: dict
    home: str
    profile_home: str
    config: str
    env_file: str
    pid: str
    log: str
    err_log: str
    state_dir: str
    state_file: str
    backups: str
    other_homes: list = field(default_factory=list)

    def join(self, *parts: str) -> str:
        return str(_flavor(self.platform)(parts[0]).joinpath(*parts[1:]))


@dataclass
class _FilePlan:
    label: str
    path: str
    before: Optional[bytes]
    after: Optional[bytes]
    mode: Optional[int] = None
    masked_before: str = ""
    masked_after: str = ""

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass
class _Plan:
    paths: _Paths
    files: list[_FilePlan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    state_after: Optional[dict] = None
    state_before: Optional[dict] = None
    identity: str = ""
    block_code: str = "blocked"

    def block(self, code: str, message: str) -> None:
        if not self.blocked:
            self.block_code = code
        self.blocked.append(message)


class HermesAdapter(ClientAdapter):
    """Hermes Agent on ``host`` (default: this computer), default profile or ``profile``.

    ``forbidden_keys`` are keys that must never be written into Hermes's files — pass the management key and
    any cluster-internal key the client holds, so a pasted wrong key is refused instead of stored. ``http``
    is the client used for endpoint checks (tests pass an in-process transport). ``hermes_home`` overrides
    the data-directory lookup for unusual installs.
    """

    id = "hermes"
    title = "Hermes Agent"

    def __init__(self, host: Optional[HostOps] = None, profile: Optional[str] = None, *,
                 http: Optional[httpx.AsyncClient] = None, forbidden_keys=(), hermes_home: Optional[str] = None,
                 request_timeout: float = REQUEST_TIMEOUT, start_wait: float = 10.0, stop_grace: float = 10.0):
        if profile in ("", "default"):
            profile = None
        if profile is not None and (not _PROFILE_RE.match(profile) or ".." in profile):
            raise ValueError("use a Hermes profile name made of letters, digits, '-', '_' or '.'")
        self.host = host or LocalHost()
        self.profile = profile
        self._http = http
        self._forbidden = [k for k in forbidden_keys if k]
        self._hermes_home = hermes_home
        self.request_timeout = request_timeout
        self.start_wait = start_wait
        self.stop_grace = stop_grace
        self._secret_values: set[str] = set(self._forbidden)
        self._launcher_cache: dict[tuple, tuple[float, Optional[str], Optional[str]]] = {}

    # ---- helpers ----
    def _pflag(self) -> list[str]:
        return ["-p", self.profile] if self.profile else []

    def _remember(self, *values: Optional[str]) -> None:
        self._secret_values.update(v for v in values if isinstance(v, str) and len(v) >= 6)

    async def _paths(self) -> _Paths:
        host = self.host
        platform = await host.platform()
        user_home = await host.home()
        env = {name: await host.env(name) for name in ("HERMES_HOME", "LOCALAPPDATA", "PREFIX", "XDG_CONFIG_HOME")}
        candidates = hermes_home_candidates(platform, env, user_home)
        existing = [c for c in candidates if (await host.stat(c)).kind == "dir"]
        if self._hermes_home:
            home = _expand(self._hermes_home, user_home, platform)
        elif env.get("HERMES_HOME"):                 # an explicit HERMES_HOME wins even before Hermes creates it
            home = candidates[0]
        else:
            home = existing[0] if existing else candidates[0]
        phome = profile_home(platform, home, self.profile)
        P = _flavor(platform)
        root = P(phome)
        return _Paths(platform, user_home, env, home, phome, str(root / "config.yaml"), str(root / ".env"),
                      str(root / "gateway.pid"), str(root / "logs" / "gateway.log"),
                      str(root / "logs" / "gateway.error.log"), str(root / STATE_DIR),
                      str(root / STATE_DIR / "state.json"), str(root / STATE_DIR / "backups"),
                      [c for c in existing if c != home])

    async def _read_optional(self, path: str) -> Optional[bytes]:
        try:
            return await self.host.read_bytes(path)
        except FileNotFoundError:
            return None
        except IsADirectoryError:
            raise AdapterError(f"{path} is a directory, not a file. Move it away, then try again.",
                               "unexpected-file") from None

    async def _env_text(self, paths: _Paths) -> str:
        data = await self._read_optional(paths.env_file)
        text = _env_decode(data)
        self._remember(*(v for _, v in env_entries(text)))
        return text

    def _remember_config_secrets(self, node: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and isinstance(k, str) and _SECRET_KEY_RE.search(k) and not v.startswith("${"):
                    self._remember(v)
                elif isinstance(v, (dict, list)):
                    self._remember_config_secrets(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                self._remember_config_secrets(v, depth + 1)

    async def _redactor(self, paths: Optional[_Paths] = None) -> Redactor:
        try:
            await self._env_text(paths or await self._paths())
        except (OSError, ValueError, AdapterError):
            pass
        return Redactor(self._secret_values)

    def _redact_now(self, text: str) -> str:
        return Redactor(self._secret_values)(clean(text))

    async def _load_state(self, paths: _Paths) -> dict:
        try:
            data = json.loads(await self.host.read_text(paths.state_file))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    async def _save_state(self, paths: _Paths, state: dict) -> None:
        await self.host.make_dirs(paths.state_dir, 0o700)
        await self.host.write_text_atomic(paths.state_file, json.dumps(state, indent=2, sort_keys=True) + "\n",
                                          0o600)

    async def _read_pid(self, path: str) -> Optional[int]:
        try:
            raw = (await self.host.read_bytes(path, 4096)).decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            return None
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                raw = str(data.get("pid", ""))
            elif isinstance(data, int):
                raw = str(data)
        except ValueError:
            pass
        m = re.search(r"\d+", raw)
        pid = int(m.group(0)) if m else 0
        return pid if pid > 1 else None

    async def _gateway_pid(self, paths: _Paths) -> tuple[Optional[int], Optional[str]]:
        """(live Hermes gateway PID from the PID file, note about a stale file)."""
        pid = await self._read_pid(paths.pid)
        if pid is None:
            return None, None
        if not await self.host.pid_alive(pid):
            return None, f"{paths.pid} names process {pid}, which is not running (a stale PID file)."
        args = await self.host.process_args(pid)
        if args is not None and "hermes" not in args.lower():
            return None, f"{paths.pid} names process {pid}, which is not Hermes (a stale PID file)."
        return pid, None

    # ---- detect ----
    async def _check_launcher(self, path: str, info: PathInfo, platform: str) -> tuple[Optional[str], Optional[str],
                                                                                         Optional[str]]:
        """(problem, version, missing target). ``problem`` None means the launcher starts Hermes."""
        if info.kind == "broken-link":
            return f"it is a link to {info.target}, which no longer exists", None, info.target
        if info.kind != "file":
            return "it is not a regular file", None, None
        if platform != "windows" and not info.executable:
            return "it is not marked executable", None, None
        cache_key = (path, info.mtime, info.target)
        cached = self._launcher_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1], cached[2], None
        missing = None
        try:
            head = await self.host.read_bytes(path, 4096, head=True)
        except (OSError, ValueError) as exc:
            return f"it cannot be read ({exc})", None, None
        if head.startswith(b"#!"):
            first = head.split(b"\n", 1)[0][2:].decode("utf-8", "replace").strip().split()
            if first and first[0].startswith("/") and not await self.host.exists(first[0]):
                return f"its first line runs {first[0]}, which no longer exists", None, first[0]
        if b"\0" not in head:
            for line in head.decode("utf-8", "replace").splitlines()[1:]:
                m = _EXEC_LINE_RE.match(line)
                if m and "hermes" in m.group(1).lower() and not await self.host.exists(m.group(1)):
                    missing = m.group(1)
                    break
        if missing:
            problem = f"it runs {missing}, which no longer exists"
            self._launcher_cache[cache_key] = (time.monotonic(), problem, None)
            return problem, None, missing
        problem, version = await self._probe_launcher(path)
        self._launcher_cache[cache_key] = (time.monotonic(), problem, version)
        return problem, version, None

    async def _probe_launcher(self, path: str) -> tuple[Optional[str], Optional[str]]:
        result = await self.host.run([path, "--version"], LAUNCHER_TIMEOUT)
        if result.rc == 0:
            return None, self._version(result.stdout + "\n" + result.stderr)
        if result.rc == 124:
            return f"it did not answer --version within {LAUNCHER_TIMEOUT:.0f} s", None
        text = (result.stdout + result.stderr).lower()
        if result.rc == 2 or any(w in text for w in ("unrecognized", "no such option", "invalid choice")):
            # A CLI without --version: its version subcommand or doctor proves it starts just as well.
            for alt, limit in ((["version"], LAUNCHER_TIMEOUT), (["doctor"], 90.0)):
                retry = await self.host.run([path, *alt], limit)
                if retry.rc == 0:
                    return None, self._version(retry.stdout) if alt == ["version"] else None
        said = self._redact_now(_first_line(result.stderr) or _first_line(result.stdout))
        return f"running it failed (exit {result.rc}{': ' + said if said else ''})", None

    @staticmethod
    def _version(text: str) -> Optional[str]:
        for line in clean(text).splitlines()[:5]:
            m = _VERSION_RE.search(line)
            if m:
                return m.group(1)
        return None

    async def _profiles(self, paths: _Paths) -> list[str]:
        root = paths.join(paths.home, "profiles")
        names = []
        for name in await self.host.list_dir(root):
            if not _PROFILE_RE.match(name):
                continue
            for marker in PROFILE_MARKERS:
                if await self.host.exists(paths.join(root, name, marker)):
                    names.append(name)
                    break
        return names

    async def _service(self, paths: _Paths) -> Optional[dict]:
        for candidate in service_candidates(paths.platform, paths.env, paths.user_home, self.profile):
            if not await self.host.exists(candidate["path"]):
                continue
            service = {**candidate, "active": None, "pid": None}
            if candidate["kind"] == "systemd" and await self.host.which("systemctl"):
                r = await self.host.run(["systemctl", "--user", "is-active", candidate["name"]], 10)
                service["active"] = r.stdout.strip() == "active"
                if service["active"]:
                    r = await self.host.run(["systemctl", "--user", "show", "-p", "MainPID", "--value",
                                             candidate["name"]], 10)
                    service["pid"] = int(r.stdout.strip()) if r.stdout.strip().isdigit() and \
                        int(r.stdout.strip()) > 1 else None
            elif candidate["kind"] == "launchd" and await self.host.which("launchctl"):
                r = await self.host.run(["launchctl", "list", candidate["name"]], 10)
                m = re.search(r'"PID"\s*=\s*(\d+)', r.stdout)
                service["active"] = bool(r.rc == 0 and m)
                service["pid"] = int(m.group(1)) if m else None
            return service
        return None

    @staticmethod
    def _interpret_status(rc: int, text: str) -> tuple[Optional[bool], Optional[int]]:
        low = text.lower()
        if any(w in low for w in ("not running", "stopped", "inactive", "no gateway", "not started")):
            return False, None
        m = re.search(r"\bpid\D{0,4}(\d+)", low)
        if rc == 0 and ("running" in low or "active" in low or m):
            return True, int(m.group(1)) if m else None
        return None, None

    async def detect(self, *, deep: bool = False) -> Detection:
        """``deep`` also runs ``hermes doctor`` (slower; it may check network services)."""
        host, p = self.host, await self._paths()
        notes: list[str] = []
        profiles = await self._profiles(p)
        if self.profile and not await host.exists(p.profile_home):
            notes.append(f"The profile {self.profile} does not exist in {p.join(p.home, 'profiles')} yet. "
                         "Create it in Hermes first, or choose the default profile.")
        path_hit = await host.which("hermes")
        candidates = ([(path_hit, "entry")] if path_hit else []) + \
            launcher_candidates(p.platform, p.env, p.user_home, p.home)
        broken: list[dict] = []
        working = version = None
        for path, role in _dedupe(candidates):
            info = await host.stat(path)
            if info.kind == "missing":
                continue
            problem, ver, target = await self._check_launcher(path, info, p.platform)
            if problem:
                broken.append({"path": path, "problem": problem, "target": target, "role": role,
                               "on_path": path == path_hit})
                continue
            working, version = path, ver
            break
        code_present = False
        for d in code_dirs(p.platform, p.user_home, p.home):
            code_present = code_present or (await host.stat(d)).kind == "dir"
        config_present = await host.exists(p.config)

        for b in broken:
            notes.append(f"The hermes command at {b['path']} does not work: {b['problem']}.")
        if working:
            state = "launcher-broken" if any(b["role"] == "entry" for b in broken) else "installed"
            if broken:
                entry = next((b["path"] for b in broken if b["role"] == "entry"), broken[0]["path"])
                notes.append(f"TwinSpark uses {working} instead. To repair the hermes command in your terminal, "
                             f"re-run the Hermes installer or point {entry} at {working}.")
            if not path_hit and p.platform != "windows":
                bindir = str(_flavor(p.platform)(working).parent)
                notes.append(f"hermes is not on the PATH that TwinSpark sees, so a terminal may not find it either. "
                             f"Add {bindir} to PATH in your shell profile, or use the full path {working}.")
        elif broken or code_present or config_present:
            state = "launcher-broken"
            where = p.home if (code_present or config_present) else broken[0]["path"]
            notes.append(f"Hermes is partly installed ({where}) but no working hermes command was found. Re-run the "
                         "Hermes installer, then detect again.")
        else:
            state = "not-installed"
            notes.append(f"Hermes Agent was not found on {host.label}: no hermes command on PATH and nothing in "
                         f"{p.home}. Install Hermes Agent, then detect again.")
        if self.profile and working:
            alias = str(_flavor(p.platform)(p.user_home) / ".local" / "bin" / self.profile)
            info = await host.stat(alias)
            if info.kind == "broken-link":
                notes.append(f"The profile shortcut {alias} links to {info.target}, which no longer exists. "
                             f"Use {working} -p {self.profile} instead.")

        pid, stale = await self._gateway_pid(p)
        if stale:
            notes.append(stale)
        service = await self._service(p)
        running = pid is not None
        if not running and service and service.get("active"):
            running, pid = True, service.get("pid")
        status_text = None
        if working and (deep or not running):
            r = await host.run([working, *self._pflag(), "gateway", "status"], LAUNCHER_TIMEOUT)
            status_text = self._redact_now(" ".join((r.stdout + " " + r.stderr).split()))[:300] or None
            alive, reported = self._interpret_status(r.rc, r.stdout + "\n" + r.stderr)
            if alive and not running:
                if reported and not await host.pid_alive(reported):
                    reported = None
                running, pid = True, reported
        doctor = None
        if deep and working:
            r = await host.run([working, *self._pflag(), "doctor"], 90.0)
            redact = await self._redactor(p)
            doctor = [redact(clean(line))[:300] for line in (r.stdout + r.stderr).splitlines() if line.strip()][-40:]
            if r.rc != 0:
                notes.append("hermes doctor reported problems; its output is listed under doctor.")
        state_file = await self._load_state(p)
        mark = state_file.get("gateway") or {}
        started = bool(running and pid and mark.get("pid") == pid)
        for other in p.other_homes:
            notes.append(f"Another Hermes data directory exists at {other}; TwinSpark uses {p.home}"
                         + (" because HERMES_HOME points there." if p.env.get("HERMES_HOME") else
                            ", which Hermes finds first."))
        model = None
        try:
            cfg, _ = parse_config(await self._read_optional(p.config))
            raw = cfg.get("model")
            model = ({k: raw.get(k) for k in OUR_MODEL_KEYS} if isinstance(raw, dict)
                     else {"default": raw} if isinstance(raw, str) else None)
        except (OSError, ValueError, AdapterError):
            pass
        return Detection(state=state, launcher=working, home=p.home, profiles=profiles, version=version,
                         running=running, gateway_pid=pid, notes=notes, profile=self.profile,
                         profile_home=p.profile_home, platform=p.platform, host=host.label, on_path=path_hit,
                         broken_launchers=broken, service=service, config_path=p.config, env_path=p.env_file,
                         started_by_twinspark=started, gateway_status=status_text, doctor=doctor, model=model,
                         other_homes=list(p.other_homes))

    # ---- inspect ----
    async def inspect(self, endpoint: Optional[Endpoint] = None, *,
                      served_aliases: Optional[list[str]] = None) -> dict:
        """The current settings as Hermes will read them, without any secret value. ``served_aliases`` (from
        validation) lets the report flag a model id the gateway no longer serves."""
        p = await self._paths()
        if endpoint is not None:
            self._remember(endpoint.api_key)
        cfg_bytes = await self._read_optional(p.config)
        env_text = await self._env_text(p)
        cfg, error = parse_config(cfg_bytes)
        self._remember_config_secrets(cfg)
        model = cfg.get("model")
        mdict = model if isinstance(model, dict) else {}
        key_var = mdict.get("key_env") if isinstance(mdict.get("key_env"), str) else None
        key_value = env_get(env_text, key_var) if key_var else None
        inline = mdict.get("api_key")
        summary = {k: mdict.get(k) for k in OUR_MODEL_KEYS}
        if isinstance(model, str):
            summary["default"] = model
        summary["has_inline_api_key"] = bool(inline)
        summary["other_keys"] = sorted(str(k) for k in mdict if k not in (*OUR_MODEL_KEYS, "api_key"))
        url = endpoint.base_url if endpoint else None
        issues: list[dict] = []

        def issue(issue_id, severity, message):
            issues.append({"id": issue_id, "severity": severity, "message": message})

        if error:
            issue("parse-error", "error", error)
        provider = summary["provider"]
        if isinstance(model, str):
            issue("model-string", "info", f"model is a single value ({model}); connecting turns it into a section "
                                          "with provider, default and base_url.")
        if provider not in (None, "custom"):
            issue("other-provider", "warn", f"Hermes uses provider {provider} (model {summary['default']}) for its "
                                            "main model. Connecting switches it to the TwinSpark endpoint; the "
                                            "current values stay in the backup.")
        if endpoint and summary["base_url"] and not same_endpoint(summary["base_url"], url):
            issue("other-base-url", "warn", f"model.base_url is {summary['base_url']}, not {url}.")
        points = None
        if endpoint:
            points = (provider == "custom" and summary["default"] == endpoint.alias
                      and same_endpoint(summary["base_url"], url))
            if points is False and provider == "custom" and same_endpoint(summary["base_url"], url):
                issue("other-alias", "warn", f"Hermes uses the alias {summary['default']} on this gateway, not "
                                             f"{endpoint.alias}.")
        if served_aliases is not None and summary["default"] and same_endpoint(summary["base_url"], url) \
                and summary["default"] not in served_aliases:
            issue("stale-model", "warn", f"Hermes asks this gateway for {summary['default']}, which it no longer "
                                         "serves. Connect again to use a served alias.")
        if inline:
            if isinstance(inline, str) and endpoint and endpoint.api_key and tokens_equal(inline, endpoint.api_key):
                issue("inline-key", "warn", "config.yaml holds the inference key inline as model.api_key. Connecting "
                                            "removes it, because .env already provides it.")
            else:
                issue("inline-key", "warn", "config.yaml holds an inline model.api_key that is not the TwinSpark "
                                            f"inference key; Hermes may send it instead of {KEY_ENV}. Remove it "
                                            "from config.yaml if requests are rejected.")
        if key_var and not key_value:
            issue("key-missing", "warn", f"model.key_env names {key_var}, but {p.env_file} does not set it.")
        key_matches = None
        if key_value and endpoint and endpoint.api_key:
            key_matches = tokens_equal(key_value, endpoint.api_key)
            if not key_matches:
                issue("key-differs", "warn", f"{key_var} in .env differs from the current inference key. Connect "
                                             "again to update it.")
        ctx = summary["context_length"]
        if isinstance(ctx, int) and ctx < MIN_CONTEXT:
            issue("context-small", "warn", f"model.context_length is {ctx:,}; Hermes needs at least {MIN_CONTEXT:,}.")
        if isinstance(ctx, int) and endpoint and endpoint.context_length and ctx > endpoint.context_length \
                and points:
            issue("context-large", "warn", f"model.context_length ({ctx:,}) is larger than the deployed limit "
                                           f"({endpoint.context_length:,}); longer prompts would be rejected.")
        providers = named_providers(cfg)
        for name, entry in providers.items():
            purl = _provider_url(entry)
            if url and purl and same_endpoint(purl, url):
                issue("duplicate-provider", "warn", f"providers.{name} also points at this gateway. Hermes uses "
                                                    "model.provider custom after connecting; remove the duplicate "
                                                    "entry if nothing else uses it.")
        routes = scan_routes(cfg, url)
        cloud = [r for r in routes if r["class"] in ("cloud", "auto")]
        for r in cloud:
            r["message"] = route_message(r)
        configured = None
        if endpoint:
            key_ok = (not endpoint.api_key) or bool(key_value and key_matches)
            configured = bool(points and key_var and key_ok)
        return {"config_path": p.config, "env_path": p.env_file, "config_exists": cfg_bytes is not None,
                "env_exists": await self.host.exists(p.env_file), "parse_error": error, "model": summary,
                "key_env": key_var, "key_env_set": bool(key_value), "key_matches": key_matches,
                "points_at_endpoint": points, "configured": configured, "providers": sorted(providers),
                "routes": routes, "cloud_routes": cloud, "issues": issues}

    # ---- preview / apply ----
    def _identity(self, endpoint: Optional[Endpoint], action: str) -> str:
        return json.dumps({"adapter": self.id, "profile": self.profile, "action": action,
                           "endpoint": endpoint.to_dict() if endpoint else None}, sort_keys=True)

    async def _plan_connect(self, endpoint: Endpoint) -> _Plan:
        self._remember(endpoint.api_key)
        p = await self._paths()
        plan = _Plan(paths=p, identity=self._identity(endpoint, "connect"))
        if endpoint.context_length is None:
            plan.block("context-unknown", f"TwinSpark does not know the deployed context limit of {endpoint.alias}. "
                                          "Activate its recipe and wait until it serves, then preview again.")
        elif endpoint.context_length < MIN_CONTEXT:
            plan.block("context-too-small",
                       f"Hermes needs at least {MIN_CONTEXT:,} tokens of context, but {endpoint.alias} is deployed "
                       f"with {endpoint.context_length:,}. Choose a recipe that serves at least {MIN_CONTEXT:,} "
                       "tokens; TwinSpark does not raise the number in Hermes, because the model would still reject "
                       "longer prompts.")
        key = endpoint.api_key
        if key and (any(tokens_equal(key, k) for k in self._forbidden) or key.startswith("tsm1.")):
            plan.block("wrong-key", "This is not the inference key (it matches the management key, a cluster-internal "
                                "key or a join code). Use the inference key; TwinSpark never writes other keys into "
                                "an app's files.")
        cfg_bytes = await self._read_optional(p.config)
        env_bytes = await self._read_optional(p.env_file)
        cfg, error = parse_config(cfg_bytes)
        if error:
            plan.block("invalid-config", error)
        if not await self.host.exists(p.profile_home) and self.profile:
            plan.block("no-profile", f"The profile {self.profile} does not exist yet ({p.profile_home}). Create it in "
                                "Hermes first, then preview again.")
        if plan.blocked:
            return plan
        self._remember_config_secrets(cfg)
        cfg_text = cfg_bytes.decode("utf-8") if cfg_bytes else ""
        env_text = _env_decode(env_bytes)
        self._remember(*(v for _, v in env_entries(env_text)))

        model = cfg.get("model")
        new_model = dict(model) if isinstance(model, dict) else {}
        if isinstance(model, str):
            plan.notes.append(f"model was the single value {model}; it becomes a section.")
        old_ctx = new_model.get("context_length")
        new_model.update({"provider": "custom", "default": endpoint.alias, "base_url": endpoint.base_url,
                          "key_env": KEY_ENV, "context_length": endpoint.context_length})
        if isinstance(old_ctx, int) and old_ctx != endpoint.context_length:
            plan.notes.append(f"model.context_length changes from {old_ctx:,} to {endpoint.context_length:,}, the "
                              "deployed limit.")
        inline = new_model.get("api_key")
        if inline:
            duplicate = isinstance(inline, str) and (
                (key and tokens_equal(inline, key)) or inline.strip() in (f"${{{KEY_ENV}}}", f"${KEY_ENV}"))
            if duplicate:
                del new_model["api_key"]
                plan.notes.append(f"The inline model.api_key duplicated {KEY_ENV} and is removed.")
            else:
                plan.warnings.append("config.yaml has an inline model.api_key that is not the TwinSpark inference "
                                     f"key. TwinSpark leaves it, but Hermes may send it instead of {KEY_ENV}; remove "
                                     "it if requests are rejected.")
        cfg_after_text, preserved = render_config(cfg_text, cfg, "model", new_model)
        if not preserved and _has_comments(cfg_text):
            plan.warnings.append("config.yaml is rewritten by a YAML parser, so its comments and formatting are not "
                                 "kept. The backup holds the exact original and Restore puts it back.")
        for route in scan_routes({**cfg, "model": new_model}, endpoint.base_url):
            if route["class"] in ("cloud", "auto"):
                plan.warnings.append(route_message(route) + " TwinSpark leaves it unchanged.")
        if key:
            env_after_text = env_set(env_text, KEY_ENV, key)
            changed_key = None if env_get(env_text, KEY_ENV) == key else KEY_ENV
        else:
            env_after_text, changed_key = env_text, None
            plan.warnings.append(f"No inference key was given, so .env is left as it is. If the gateway requires a "
                                 f"key, set {KEY_ENV} and preview again.")
        env_info = await self.host.stat(p.env_file)
        if env_info.exists and env_info.mode is not None and env_info.mode & 0o077 and p.platform != "windows":
            plan.warnings.append(f"{p.env_file} can be read by other users (mode {env_info.mode:o}). TwinSpark keeps "
                                 "its mode; chmod 600 would protect the key.")
        redact = Redactor(self._secret_values)
        plan.files = [
            _FilePlan("config.yaml", p.config, cfg_bytes, cfg_after_text.encode("utf-8"),
                      None if cfg_bytes is not None else 0o600, redact(cfg_text), redact(cfg_after_text)),
            _FilePlan(".env", p.env_file, env_bytes,
                      _env_encode(env_after_text) if (env_bytes is not None or key) else None,
                      None if env_bytes is not None else 0o600, redact(_printable(mask_env(env_text))),
                      redact(_printable(mask_env(env_after_text, changed_key)))),
        ]
        state = await self._load_state(p)
        plan.state_before = {k: state.get(k) for k in _STATE_KEYS}
        after = dict(state)
        if not state.get("connected"):
            prev = model if isinstance(model, dict) else ({"default": model} if isinstance(model, str) else {})
            after["previous_model"] = {k: prev[k] for k in OUR_MODEL_KEYS if k in prev}
            after["previous_model_present"] = model is not None
        after["connected"] = {"base_url": endpoint.base_url, "alias": endpoint.alias,
                              "context_length": endpoint.context_length, "revision_id": endpoint.revision_id,
                              "source": endpoint.source, "applied_at": time.time()}
        plan.state_after = after
        pid, _ = await self._gateway_pid(p)
        if pid:
            plan.notes.append(f"The Hermes gateway (pid {pid}) keeps its current settings until it restarts. "
                              "Restart it after applying.")
        return plan

    def _token(self, plan: _Plan) -> str:
        files = hashlib.sha256("|".join(f"{f.label}={_sha(f.before) or 'absent'}" for f in plan.files)
                               .encode()).hexdigest()
        body = hashlib.sha256((plan.identity + "".join(f"\0{f.label}\0{f.masked_after}\0{f.after is None}"
                                                       for f in plan.files)).encode()).hexdigest()
        return f"v1.{files[:24]}.{body[:24]}"

    def _preview(self, plan: _Plan) -> Preview:
        changes = []
        for f in plan.files:
            diff = "\n".join(difflib.unified_diff(
                f.masked_before.splitlines(), f.masked_after.splitlines() if f.after is not None else [],
                fromfile=f"{f.path} (current)" if f.before is not None else f"{f.path} (new file)",
                tofile=f"{f.path} (after)" if f.after is not None else f"{f.path} (removed)", lineterm=""))
            changes.append(FileChange(f.path, _sha(f.before), self._redact_now(diff), f.changed, f.label))
        token = None if plan.blocked else self._token(plan)
        notes = list(plan.notes)
        if not plan.blocked and not any(f.changed for f in plan.files):
            notes.append("Hermes already uses these settings; applying changes nothing.")
        return Preview(files=changes, warnings=list(plan.warnings), token=token, blocked=list(plan.blocked),
                       notes=notes)

    async def preview(self, endpoint: Endpoint) -> Preview:
        return self._preview(await self._plan_connect(endpoint))

    def _check_token(self, plan: _Plan, token: Optional[str]) -> None:
        if plan.blocked:
            raise AdapterError(plan.blocked[0], plan.block_code)
        m = _TOKEN_RE.match(token) if isinstance(token, str) else None
        if not m:
            raise AdapterError("This change has no valid preview token. Preview it again, then apply.", "invalid-token")
        expected = _TOKEN_RE.match(self._token(plan))
        assert expected is not None
        if m.group(1) != expected.group(1):
            raise AdapterError("config.yaml or .env changed after the preview. Preview again to see the current "
                               "files, then apply.", "stale-preview")
        if m.group(2) != expected.group(2):
            raise AdapterError("The settings differ from the preview (another endpoint, alias or profile). Preview "
                               "again, then apply.", "preview-mismatch")

    async def apply(self, endpoint: Endpoint, token: str) -> ApplyResult:
        plan = await self._plan_connect(endpoint)
        self._check_token(plan, token)
        return await self._commit(plan, "apply")

    async def _take_backup(self, paths: _Paths, kind: str, state_snapshot: Optional[dict]) -> Backup:
        now = time.time()
        backup_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + secrets.token_hex(3)
        location = paths.join(paths.backups, backup_id)
        await self.host.make_dirs(paths.state_dir, 0o700)
        await self.host.make_dirs(paths.backups, 0o700)
        await self.host.make_dirs(location, 0o700)
        files = []
        for name, path in (("config.yaml", paths.config), (".env", paths.env_file)):
            data = await self._read_optional(path)
            entry = {"name": name, "path": path, "existed": data is not None, "sha256": _sha(data),
                     "mode": None, "backup_path": None}
            if data is not None:
                entry["mode"] = (await self.host.stat(path)).mode
                dest = paths.join(location, name)
                await self.host.backup(path, dest)
                if _sha(await self.host.read_bytes(dest)) != entry["sha256"]:
                    raise AdapterError(f"The backup copy of {path} does not match the original, so nothing was "
                                       "changed. Check free disk space and try again.", "backup-failed")
                entry["backup_path"] = dest
            files.append(entry)
        manifest = {"id": backup_id, "kind": kind, "created": now, "adapter": self.id, "profile": self.profile,
                    "files": files, "state": state_snapshot or {}}
        await self.host.write_text_atomic(paths.join(location, "manifest.json"),
                                          json.dumps(manifest, indent=2) + "\n", 0o600)
        return Backup(backup_id, kind, now, location, files)

    async def _commit(self, plan: _Plan, kind: str) -> ApplyResult:
        p = plan.paths
        changed = [f for f in plan.files if f.changed]
        if not changed:
            if plan.state_after is not None:
                await self._save_state(p, plan.state_after)
            return ApplyResult(False, None, [], ["Hermes already uses these settings; nothing was written."])
        backup = await self._take_backup(p, kind, plan.state_before)
        written: list[_FilePlan] = []
        # .env first: if config.yaml then fails, Hermes merely has an unused variable rather than a key_env
        # that points at nothing.
        for f in sorted(changed, key=lambda f: f.label != ".env"):
            if _sha(await self._read_optional(f.path)) != _sha(f.before):
                await self._roll_back(written, backup)
                raise AdapterError(f"{f.path} changed while applying. Nothing was kept; preview again, then apply.",
                                   "stale-preview")
            try:
                if f.after is None:
                    await self.host.remove(f.path)
                else:
                    await self.host.write_bytes_atomic(f.path, f.after, f.mode)
            except (OSError, ValueError) as exc:
                await self._roll_back(written, backup)
                raise AdapterError(f"Writing {f.path} failed ({self._redact_now(str(exc))[:200]}). The previous files "
                                   "were put back.", "write-failed") from None
            written.append(f)
        if plan.state_after is not None:
            await self._save_state(p, plan.state_after)
        notes = [f"The previous files are in backup {backup.id}; Restore puts them back."] + list(plan.notes)
        return ApplyResult(True, backup, [f.path for f in written], notes)

    async def _roll_back(self, written: list[_FilePlan], backup: Backup) -> None:
        by_path = {f["path"]: f for f in backup.files}
        for f in reversed(written):
            entry = by_path.get(f.path)
            try:
                if entry and entry["existed"]:
                    await self.host.write_bytes_atomic(f.path, await self.host.read_bytes(entry["backup_path"]),
                                                       entry["mode"])
                else:
                    await self.host.remove(f.path)
            except (OSError, ValueError):
                pass                             # the backup remains for restore()

    # ---- disconnect ----
    async def _plan_disconnect(self, restore_previous: bool = True) -> _Plan:
        p = await self._paths()
        plan = _Plan(paths=p, identity=self._identity(None, f"disconnect:{restore_previous}"))
        cfg_bytes = await self._read_optional(p.config)
        env_bytes = await self._read_optional(p.env_file)
        cfg, error = parse_config(cfg_bytes)
        if error:
            plan.block("invalid-config", error)
            return plan
        self._remember_config_secrets(cfg)
        cfg_text = cfg_bytes.decode("utf-8") if cfg_bytes else ""
        env_text = _env_decode(env_bytes)
        self._remember(*(v for _, v in env_entries(env_text)))
        state = await self._load_state(p)
        ours = state.get("connected") or {}
        model = cfg.get("model")
        new_cfg_text = cfg_text
        if isinstance(model, dict):
            expected = {"provider": "custom", "default": ours.get("alias"), "base_url": ours.get("base_url"),
                        "key_env": KEY_ENV, "context_length": ours.get("context_length")}
            managed = model.get("key_env") == KEY_ENV and model.get("provider") == "custom"
            prev = (state.get("previous_model") or {}) if restore_previous else {}
            new_model = dict(model)
            kept = []
            for k in OUR_MODEL_KEYS:
                if k not in new_model:
                    continue
                if ours and new_model[k] != expected[k]:
                    kept.append(k)                    # changed by someone since TwinSpark wrote it
                    continue
                if not ours and not managed:
                    continue
                if k in prev:
                    new_model[k] = prev[k]
                else:
                    del new_model[k]
            if kept:
                plan.notes.append("These model settings were changed after TwinSpark wrote them and stay as they "
                                  f"are: {', '.join('model.' + k for k in kept)}.")
            if new_model != model:
                if new_model or state.get("previous_model_present"):
                    new_cfg_text, preserved = render_config(cfg_text, cfg, "model", new_model)
                else:
                    new_cfg_text, preserved = render_config(cfg_text, cfg, "model", _DELETE)
                if not preserved and _has_comments(cfg_text):
                    plan.warnings.append("config.yaml is rewritten by a YAML parser, so its comments and formatting "
                                         "are not kept. The backup holds the exact original.")
                if prev and restore_previous:
                    plan.notes.append("Hermes uses its previous model settings again ("
                                      + ", ".join(f"{k} {prev[k]}" for k in OUR_MODEL_KEYS if k in prev) + ").")
        env_after = env_remove(env_text, KEY_ENV)
        redact = Redactor(self._secret_values)
        plan.files = [
            _FilePlan("config.yaml", p.config, cfg_bytes,
                      new_cfg_text.encode("utf-8") if cfg_bytes is not None else None, None,
                      redact(cfg_text), redact(new_cfg_text)),
            _FilePlan(".env", p.env_file, env_bytes, _env_encode(env_after) if env_bytes is not None else None,
                      None, redact(_printable(mask_env(env_text))), redact(_printable(mask_env(env_after)))),
        ]
        plan.state_before = {k: state.get(k) for k in _STATE_KEYS}
        after = {k: v for k, v in state.items() if k not in (*_STATE_KEYS, "validation")}
        plan.state_after = after
        if not any(f.changed for f in plan.files):
            plan.notes.append("Hermes has no TwinSpark settings to remove.")
        return plan

    async def preview_disconnect(self, *, restore_previous: bool = True) -> Preview:
        return self._preview(await self._plan_disconnect(restore_previous))

    async def disconnect(self, token: Optional[str] = None, *, restore_previous: bool = True) -> ApplyResult:
        """Remove TwinSpark's model settings and ``TWINSPARK_API_KEY``, preserving everything else. Model
        settings TwinSpark replaced are put back when it recorded them (``restore_previous``); settings changed
        by someone after TwinSpark wrote them stay. With ``token`` the files must still match the preview."""
        plan = await self._plan_disconnect(restore_previous)
        if token is not None:
            self._check_token(plan, token)
        elif plan.blocked:
            raise AdapterError(plan.blocked[0], plan.block_code)
        result = await self._commit(plan, "disconnect")
        result.notes += [n for n in plan.notes if n not in result.notes]
        return result

    # ---- backups / restore ----
    async def backups(self) -> list[Backup]:
        p = await self._paths()
        out = []
        for name in await self.host.list_dir(p.backups):
            if not _BACKUP_ID_RE.match(name):
                continue
            manifest = await self._manifest(p, name)
            if manifest:
                out.append(Backup(name, str(manifest.get("kind", "apply")), float(manifest.get("created", 0)),
                                  p.join(p.backups, name), list(manifest.get("files", []))))
        return sorted(out, key=lambda b: (b.created, b.id))

    async def _manifest(self, p: _Paths, backup_id: str) -> Optional[dict]:
        try:
            data = json.loads(await self.host.read_text(p.join(p.backups, backup_id, "manifest.json")))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("id") == backup_id else None

    async def restore(self, backup_id: Optional[str] = None) -> RestoreResult:
        """Put back config.yaml and .env exactly as they were before the chosen TwinSpark change (latest by
        default). Files that did not exist then are removed. The current files are backed up first."""
        p = await self._paths()
        if backup_id is not None and not _BACKUP_ID_RE.match(str(backup_id)):
            raise AdapterError("That is not a TwinSpark backup id; choose one from the backup list.", "no-backup")
        items = await self.backups()
        if backup_id:
            chosen = next((b for b in items if b.id == backup_id), None)
            if chosen is None:
                raise AdapterError(f"There is no backup {backup_id} for this Hermes profile.", "no-backup")
        else:
            chosen = next((b for b in reversed(items) if b.kind != "restore"), None)
            if chosen is None:
                raise AdapterError("TwinSpark has not changed Hermes's files for this profile, so there is nothing "
                                   "to restore.", "no-backup")
        manifest = await self._manifest(p, chosen.id) or {}
        targets = {"config.yaml": p.config, ".env": p.env_file}
        contents: dict[str, Optional[bytes]] = {}
        for entry in manifest.get("files", []):
            name = entry.get("name")
            if name not in targets:
                continue
            if entry.get("existed"):
                try:
                    data = await self.host.read_bytes(p.join(chosen.location, name))
                except (OSError, ValueError):
                    data = None
                if data is None or _sha(data) != entry.get("sha256"):
                    raise AdapterError(f"Backup {chosen.id} is damaged ({name} does not match its checksum), so "
                                       "nothing was restored. Choose an older backup.", "backup-damaged")
                contents[name] = data
            else:
                contents[name] = None
        if not contents:
            raise AdapterError(f"Backup {chosen.id} lists no files, so nothing was restored.", "backup-damaged")
        current_state = await self._load_state(p)
        safety = await self._take_backup(p, "restore", {k: current_state.get(k) for k in _STATE_KEYS})
        modes = {e.get("name"): e.get("mode") for e in manifest.get("files", [])}
        restored, removed = [], []
        for name, data in contents.items():
            if data is None:
                if await self.host.remove(targets[name]):
                    removed.append(targets[name])
            else:
                await self.host.write_bytes_atomic(targets[name], data, modes.get(name))
                restored.append(targets[name])
        snapshot = manifest.get("state") or {}
        state = {k: v for k, v in current_state.items() if k not in (*_STATE_KEYS, "validation")}
        state.update({k: v for k, v in snapshot.items() if v is not None})
        await self._save_state(p, state)
        notes = [f"The files from before the restore are in backup {safety.id}."]
        pid, _ = await self._gateway_pid(p)
        if pid:
            notes.append(f"The Hermes gateway (pid {pid}) still runs with the previous settings. Restart it to use "
                         "the restored files.")
        return RestoreResult(chosen, restored, removed, safety, notes)

    # ---- validate ----
    async def validate(self, endpoint: Endpoint) -> list[Check]:
        self._remember(endpoint.api_key)
        checks = await validate_endpoint(endpoint.base_url, endpoint.api_key, endpoint.alias, http=self._http,
                                         min_context=MIN_CONTEXT, timeout=self.request_timeout)
        if not self.host.is_local:
            checks.insert(0, Check("vantage", "Checked from the workstation", "client", "warn",
                                   f"These requests went from the workstation, not from {self.host.label} where "
                                   f"Hermes runs. Hermes there must reach {endpoint.base_url} itself, and 127.0.0.1 "
                                   f"on {self.host.label} is that machine, not this one. After launching, watch the "
                                   "gateway log on that host for connection errors."))
        alias_check = next((c for c in checks if c.id == "alias"), None)
        served = alias_check.data.get("served_aliases") if alias_check else None
        deployed = alias_check.data.get("max_model_len") if alias_check else None
        det = await self.detect()
        report = await self.inspect(endpoint, served_aliases=served)
        checks += await self._client_checks(det, report, endpoint, deployed)
        await self._record_validation(endpoint, checks)
        return checks

    async def _client_checks(self, det: Detection, report: dict, endpoint: Endpoint,
                             deployed: Optional[int]) -> list[Check]:
        out = []

        def add(check_id, title, status, detail, **data):
            out.append(Check(check_id, title, "client", status, self._redact_now(detail), data))

        if det.launcher is None:
            add("hermes.launcher", "Hermes launcher", "fail", det.notes[0] if det.notes else
                "No working hermes command was found. Re-run the Hermes installer, then detect again.")
        elif det.state == "launcher-broken":
            b = next((b for b in det.broken_launchers if b["role"] == "entry"), det.broken_launchers[0])
            add("hermes.launcher", "Hermes launcher", "warn",
                f"The hermes command at {b['path']} does not work ({b['problem']}). TwinSpark uses {det.launcher} "
                "instead; re-run the Hermes installer to repair the command for your terminal.",
                launcher=det.launcher, broken=b["path"])
        else:
            add("hermes.launcher", "Hermes launcher", "pass", f"Hermes starts with {det.launcher}"
                + (f" (version {det.version})." if det.version else "."), launcher=det.launcher)
        model = report["model"]
        if report["parse_error"]:
            add("hermes.config", "Hermes model settings", "fail", report["parse_error"])
        elif report["points_at_endpoint"]:
            add("hermes.config", "Hermes model settings", "pass",
                f"config.yaml uses the custom provider at {endpoint.base_url} with model {endpoint.alias}.")
        else:
            add("hermes.config", "Hermes model settings", "fail",
                f"config.yaml uses provider {model.get('provider') or 'none'} and model "
                f"{model.get('default') or 'none'}, not {endpoint.alias} at {endpoint.base_url}. Preview and apply "
                "the connection.")
        if not endpoint.api_key:
            add("hermes.key", "Inference key for Hermes", "skip", "No inference key is in use for this endpoint.")
        elif report["key_env"] and report["key_env_set"] and report["key_matches"]:
            add("hermes.key", "Inference key for Hermes", "pass",
                f"{report['key_env']} is set in {report['env_path']} and matches the inference key.")
        elif report["key_env"] and report["key_env_set"]:
            add("hermes.key", "Inference key for Hermes", "fail",
                f"{report['key_env']} in {report['env_path']} differs from the current inference key. Apply the "
                "connection again to update it.")
        else:
            add("hermes.key", "Inference key for Hermes", "fail",
                f"{KEY_ENV} is missing from {report['env_path']}. Apply the connection to add it.")
        ctx = model.get("context_length")
        if not report["points_at_endpoint"]:
            add("hermes.context", "Hermes context setting", "skip", "Checked once Hermes points at TwinSpark.")
        elif not isinstance(ctx, int):
            add("hermes.context", "Hermes context setting", "warn",
                "model.context_length is not set; Hermes may assume a window the model does not have. Apply the "
                "connection to set the deployed limit.")
        elif ctx < MIN_CONTEXT:
            add("hermes.context", "Hermes context setting", "fail",
                f"model.context_length is {ctx:,}; Hermes needs at least {MIN_CONTEXT:,}. Choose a recipe with a "
                "larger deployed context.")
        elif deployed and ctx > deployed:
            add("hermes.context", "Hermes context setting", "fail",
                f"model.context_length ({ctx:,}) is larger than the deployed limit ({deployed:,}); longer prompts "
                "would be rejected. Apply the connection again to use the deployed limit.")
        else:
            add("hermes.context", "Hermes context setting", "pass", f"model.context_length is {ctx:,} tokens.")
        if det.launcher and not report["parse_error"]:
            r = await self.host.run([det.launcher, *self._pflag(), "config", "check"], 60.0)
            said = self._redact_now(_first_line(r.stdout + "\n" + r.stderr, 240))
            if r.rc == 0:
                add("hermes.config_check", "hermes config check", "pass",
                    "hermes config check found no problems." + (f" It said: {said}" if said else ""))
            else:
                add("hermes.config_check", "hermes config check", "warn",
                    f"hermes config check reported a problem (exit {r.rc})" + (f": {said}" if said else ".")
                    + " Fix what it names, then validate again.")
        cloud = report["cloud_routes"]
        if cloud:
            add("hermes.routing", "Tasks that leave the Sparks", "warn",
                " ".join(r["message"] for r in cloud) + " TwinSpark does not change these; edit config.yaml to keep "
                "them local.", routes=[{k: r[k] for k in ("task", "provider", "model", "class")} for r in cloud])
        else:
            add("hermes.routing", "Tasks that leave the Sparks", "pass",
                "No auxiliary or fallback route sends requests to a cloud provider.")
        conflicts = [i["message"] for i in report["issues"]
                     if i["id"] in ("duplicate-provider", "stale-model", "inline-key", "other-alias")]
        if conflicts:
            add("hermes.conflicts", "Stale or conflicting entries", "warn", " ".join(conflicts))
        return out

    async def _record_validation(self, endpoint: Endpoint, checks: list[Check]) -> None:
        p = await self._paths()
        if not await self.host.exists(p.profile_home):
            return
        state = await self._load_state(p)
        state["validation"] = {
            "base_url": endpoint.base_url, "alias": endpoint.alias, "revision_id": endpoint.revision_id,
            "at": time.time(), "passed": checks_passed(checks, ("transport", "auth", "model", "parser")),
            "client_passed": checks_passed(checks, ("client",)),
            "checks": [{"id": c.id, "layer": c.layer, "status": c.status} for c in checks]}
        try:
            await self._save_state(p, state)
        except OSError:
            pass                                      # evidence is a convenience; the checks were still returned

    # ---- launch / stop ----
    async def launch(self, mode: str = "cli") -> LaunchResult:
        if mode not in LAUNCH_MODES:
            raise ValueError(f"mode must be one of {', '.join(LAUNCH_MODES)}")
        det = await self.detect()
        if det.launcher is None:
            raise AdapterError(det.notes[0] if det.notes else "No working hermes command was found.", det.state)
        p = await self._paths()
        base = [det.launcher, *self._pflag()]
        if mode == "cli":
            argv = [*base, "chat"]
            detail = (f"Run this in a terminal on {self.host.label}. Hermes reads {KEY_ENV} from {p.env_file}.")
            if det.state == "launcher-broken":
                detail += " It uses the working launcher directly, because the hermes command itself is broken."
            return LaunchResult("cli", "command", argv, command_line(argv, p.platform), detail=detail,
                                host=self.host.label)
        if det.running:
            return LaunchResult("gateway", "attached", [*base, "gateway", "run"],
                                command_line([*base, "gateway", "run"], p.platform), det.gateway_pid, p.log,
                                f"The Hermes gateway is already running (pid {det.gateway_pid}); TwinSpark attached to "
                                "it instead of starting another. Restart it to apply changed settings.",
                                self.host.label)
        if det.service:
            argv = [*base, "gateway", "start"]
            r = await self.host.run(argv, 60.0)
            if r.rc != 0:
                said = self._redact_now(_first_line(r.stderr) or _first_line(r.stdout))
                raise AdapterError(f"hermes gateway start failed (exit {r.rc}{': ' + said if said else ''}). Check "
                                   f"the {det.service['name']} service, then try again.", "launch-failed")
            pid = await self._wait_for_gateway(p, None)
            return LaunchResult("gateway", "service-started", argv, command_line(argv, p.platform), pid, p.log,
                                f"Asked the {det.service['name']} service to start the gateway"
                                + (f"; it runs as pid {pid}." if pid else "; it has not reported a PID yet."),
                                self.host.label)
        argv = [*base, "gateway", "run"]
        child = await self.host.start_detached(argv, None, p.log, p.err_log)
        state = await self._load_state(p)
        state["gateway"] = {"pid": child, "argv": argv, "started_at": time.time(), "log": p.log}
        await self._save_state(p, state)
        pid = await self._wait_for_gateway(p, child)
        if pid is None:
            await self.host.terminate(child, 1.0)
            state.pop("gateway", None)
            await self._save_state(p, state)
            tail = (await self.logs(5, errors=True)) or (await self.logs(5))
            raise AdapterError("The Hermes gateway stopped right after starting."
                               + (f" Last log lines: {' | '.join(tail)}" if tail else "")
                               + " Fix the problem shown, then start it again.", "launch-failed")
        if pid != child:
            state["gateway"]["pid"] = pid
            await self._save_state(p, state)
        return LaunchResult("gateway", "started", argv, command_line(argv, p.platform), pid, p.log,
                            f"TwinSpark started the Hermes gateway (pid {pid}); its output goes to {p.log}.",
                            self.host.label)

    async def _wait_for_gateway(self, p: _Paths, child: Optional[int]) -> Optional[int]:
        """The gateway's PID once its PID file names a live process; None if the started process exits first."""
        deadline = time.monotonic() + self.start_wait
        while True:
            pid, _ = await self._gateway_pid(p)
            if pid:
                return pid
            if child is not None and not await self.host.pid_alive(child):
                return None
            if time.monotonic() >= deadline:
                return child if child is not None and await self.host.pid_alive(child) else None
            await asyncio.sleep(0.1)

    async def stop(self) -> StopResult:
        p = await self._paths()
        state = await self._load_state(p)
        mark = state.get("gateway") or {}
        pid = mark.get("pid") if isinstance(mark.get("pid"), int) else None
        if pid:
            args = await self.host.process_args(pid) if await self.host.pid_alive(pid) else None
            state.pop("gateway", None)
            if args is not None and "hermes" in args.lower():
                ok = await self.host.terminate(pid, self.stop_grace)
                if not ok:
                    state["gateway"] = mark
                await self._save_state(p, state)
                return StopResult(ok, "terminated", pid,
                                  f"TwinSpark stopped the Hermes gateway it started (pid {pid})." if ok else
                                  f"The Hermes gateway (pid {pid}) did not stop. Stop it from a terminal on "
                                  f"{self.host.label}.")
            await self._save_state(p, state)
        det = await self.detect()
        if det.service and det.launcher:
            argv = [det.launcher, *self._pflag(), "gateway", "stop"]
            r = await self.host.run(argv, 60.0)
            if r.rc != 0:
                said = self._redact_now(_first_line(r.stderr) or _first_line(r.stdout))
                raise AdapterError(f"hermes gateway stop failed (exit {r.rc}{': ' + said if said else ''}).",
                                   "stop-failed")
            if det.gateway_pid:
                await self._wait_gone(det.gateway_pid)
            return StopResult(True, "service", det.gateway_pid,
                              f"Asked the {det.service['name']} service to stop the gateway.")
        if det.running:
            hint = command_line([det.launcher, *self._pflag(), "gateway", "stop"], p.platform) if det.launcher \
                else "hermes gateway stop"
            return StopResult(False, None, det.gateway_pid,
                              f"The Hermes gateway (pid {det.gateway_pid}) was not started by TwinSpark and has no "
                              f"service here, so TwinSpark leaves it running. Stop it where it was started, or run: "
                              f"{hint}")
        return StopResult(False, None, None, "No Hermes gateway is running.")

    async def _wait_gone(self, pid: int, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and await self.host.pid_alive(pid):
            await asyncio.sleep(0.1)

    async def restart(self) -> LaunchResult:
        """Stop and start the selected gateway only; refuses to touch a gateway TwinSpark did not start
        unless it is this profile's installed service."""
        result = await self.stop()
        if not result.stopped and (await self.detect()).running:
            raise AdapterError(result.detail, "not-owned")
        return await self.launch("gateway")

    # ---- status / logs ----
    async def logs(self, n: int = 50, *, errors: bool = False) -> list[str]:
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 2000:
            raise ValueError("n must be between 1 and 2000 lines")
        p = await self._paths()
        raw = await self.host.read_tail(p.err_log if errors else p.log, LOG_TAIL_BYTES)
        lines = raw.decode("utf-8", "replace").splitlines()
        if len(raw) >= LOG_TAIL_BYTES and lines:
            lines = lines[1:]                         # the first line was cut in the middle
        redact = await self._redactor(p)
        return [redact(clean(line))[:2000] for line in lines[-n:]]

    async def _probe_models(self, endpoint: Endpoint) -> tuple[bool, Optional[list[str]]]:
        """(reachable, served aliases). Any HTTP answer counts as reachable; aliases only from a valid list."""
        own = self._http is None
        client = self._http or httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(5.0))
        headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
        try:
            async with asyncio.timeout(6.0):
                response = await client.get(endpoint.base_url + "/models", headers=headers)
        except (httpx.HTTPError, TimeoutError):
            return False, None
        finally:
            if own:
                await client.aclose()
        served = None
        try:
            if response.status_code == 200 and len(response.content) < 1 << 20:
                data = response.json().get("data")
                if isinstance(data, list):
                    served = [e["id"] for e in data if isinstance(e, dict) and isinstance(e.get("id"), str)]
        except (ValueError, AttributeError):
            served = None
        return True, served

    async def status(self, endpoint: Optional[Endpoint] = None, *, log_lines: int = 50) -> Status:
        det = await self.detect()
        p = await self._paths()
        configured = reachable = compatible = served = None
        if endpoint is not None:
            reachable, served = await self._probe_models(endpoint)
        report = await self.inspect(endpoint, served_aliases=served)
        if endpoint is not None:
            configured = bool(report["configured"])
            v = (await self._load_state(p)).get("validation") or {}
            compatible = bool(v.get("passed") and v.get("alias") == endpoint.alias
                              and same_endpoint(v.get("base_url"), endpoint.base_url)
                              and v.get("revision_id") == endpoint.revision_id)
        state = State(installed=det.state != "not-installed", configured=configured, reachable=reachable,
                      compatible=compatible, running=det.running, launcher_ok=det.launcher is not None)
        notes = list(det.notes)
        phase = state.phase
        if phase == "not-configured":
            notes.append("Hermes does not point at this endpoint yet. Preview and apply the connection.")
        elif phase == "unreachable":
            notes.append(f"The endpoint {endpoint.base_url} does not answer from {self.host.label}. Start the "
                         "TwinSpark connection (SSH forward or tailnet), then check again.")
        elif phase == "unverified":
            notes.append("Validate the connection to confirm chat, streaming and tool calls for this revision.")
        return Status(state, det, report, await self.logs(log_lines), await self.logs(log_lines, errors=True), notes)
