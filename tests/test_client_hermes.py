"""HermesAdapter end to end on fake Hermes installs in tmp dirs.

Nothing here runs a real Hermes, touches the real home directory or uses the network: the launcher is a small
Python script (``#!`` = this interpreter) that understands --version, doctor, config check, chat and
gateway run/start/stop/status; the gateway is the in-process FakeGateway from test_client_validate.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests.test_client_validate import BASE, KEY, FakeGateway
from twinspark.client.adapters import AdapterError, Endpoint, HermesAdapter, LocalHost
from twinspark.client.adapters.hermes import (
    command_line,
    env_set,
    hermes_home_candidates,
    launcher_candidates,
    mask_env,
    profile_home,
    render_config,
    scan_routes,
    service_candidates,
)

DEEPSEEK_KEY = "sk-deepseekSecretValue0123456789abcd"
OPENROUTER_KEY = "sk-or-v1-routerSecretValue0123456789"
MGMT_KEY = "management-key-Kp4sW9xL2mQ7"

FAKE_HERMES = r'''
import json, os, pathlib, signal, subprocess, sys, time

args = sys.argv[1:]
root = pathlib.Path(os.environ.get("HERMES_HOME") or pathlib.Path(os.environ["HOME"]) / ".hermes")
home = root
if args[:1] == ["-p"]:
    home = root / "profiles" / args[1]
    args = args[2:]
with open(root / "fake-calls.log", "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "pid": os.getpid()}) + "\n")
pidfile = home / "gateway.pid"


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return fh.read().rsplit(b")", 1)[1].split()[0] not in (b"Z", b"X")
    except OSError:
        return True


def current():
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if alive(pid) else None


def config():
    import yaml
    try:
        return yaml.safe_load((home / "config.yaml").read_text()) or {}
    except OSError:
        return {}


def env():
    values = {}
    try:
        for line in (home / ".env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return values


if args == ["--version"]:
    print("Hermes Agent v0.9.1 (fake)")
    sys.exit(0)
if args == ["doctor"]:
    print("All components present")
    sys.exit(0)
if args == ["chat"]:
    print("chat")
    sys.exit(0)
if args == ["config", "check"]:
    ctx = (config().get("model") or {}).get("context_length") or 0
    if ctx < 64000:
        print(f"model.context_length {ctx} is below 64000", file=sys.stderr)
        sys.exit(1)
    print("config ok")
    sys.exit(0)
if args[:1] == ["gateway"]:
    sub = args[1]
    if sub == "status":
        pid = current()
        print(f"Gateway is running (pid {pid})" if pid else "Gateway is not running")
        sys.exit(0 if pid else 3)
    if sub == "run":
        if current():
            print("a gateway is already running", file=sys.stderr)
            sys.exit(1)
        model = config().get("model") or {}
        if model.get("fail_start"):
            print(f"fatal: cannot start, key {env().get('TWINSPARK_API_KEY', '')} rejected", file=sys.stderr,
                  flush=True)
            sys.exit(1)
        pidfile.write_text(str(os.getpid()))

        def bye(*_):
            try:
                pidfile.unlink()
            except OSError:
                pass
            sys.exit(0)

        signal.signal(signal.SIGTERM, bye)
        print(f"gateway up: model {model.get('default')} at {model.get('base_url')}", flush=True)
        print(f"Authorization: Bearer {env().get('TWINSPARK_API_KEY', '')}", flush=True)   # a careless log line
        print(f"loaded key={env().get('TWINSPARK_API_KEY', '')}", file=sys.stderr, flush=True)
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(0.05)
        bye()
    if sub == "start":                       # what the installed service would do
        logs = home / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "gateway.log", "ab") as out, open(logs / "gateway.error.log", "ab") as err:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), *sys.argv[1:-1], "run"], stdout=out,
                             stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(100):
            if current():
                break
            time.sleep(0.05)
        print("started")
        sys.exit(0)
    if sub == "stop":
        pid = current()
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(100):
                if not alive(pid):
                    break
                time.sleep(0.05)
        print("stopped")
        sys.exit(0)
print("unknown command", args, file=sys.stderr)
sys.exit(2)
'''

DEEPSEEK_CONFIG = """\
# Hermes settings (hand-written)
model:
  provider: deepseek
  default: deepseek-chat
  base_url: https://api.deepseek.com/v1
  max_tokens: 8192

# what the agent may do
agent:
  max_turns: 60      # keep runs short
  reasoning_effort: medium

terminal:
  backend: local
  cwd: ~/projects

auxiliary:
  vision:
    provider: openrouter
    model: google/gemini-2.5-flash
    api_key: """ + OPENROUTER_KEY + """
  web_extract:
    provider: auto

compression:
  enabled: true
  threshold: 0.5
  summary_provider: main

fallback_model:
  provider: openrouter
  model: anthropic/claude-sonnet-4
"""

DEEPSEEK_ENV = f"# Hermes secrets\nDEEPSEEK_API_KEY={DEEPSEEK_KEY}\nOPENROUTER_API_KEY={OPENROUTER_KEY}\n"


@dataclass
class FakeInstall:
    root: Path              # tmp dir
    home: Path              # the fake user's home
    hermes: Path            # ~/.hermes
    launcher: Path          # the working program in the venv
    wrapper: Path           # ~/.local/bin/hermes
    stale_target: str
    host: LocalHost

    def calls(self) -> list[list[str]]:
        log = self.hermes / "fake-calls.log"
        return [json.loads(line)["argv"] for line in log.read_text().splitlines()] if log.exists() else []

    def adapter(self, gw: FakeGateway | None = None, profile=None, **kw) -> HermesAdapter:
        kw.setdefault("start_wait", 5.0)
        kw.setdefault("stop_grace", 3.0)
        return HermesAdapter(self.host, profile, http=gw.client() if gw else None, **kw)


def write_launcher(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + FAKE_HERMES)
    path.chmod(0o755)


def make_install(tmp_path: Path, *, stale_wrapper=True, wrapper_ok=False, config=DEEPSEEK_CONFIG,
                 env=DEEPSEEK_ENV, path_hermes=False) -> FakeInstall:
    home = tmp_path / "home"
    hermes = home / ".hermes"
    launcher = hermes / "hermes-agent" / ".venv" / "bin" / "hermes"
    write_launcher(launcher)
    wrapper = home / ".local" / "bin" / "hermes"
    wrapper.parent.mkdir(parents=True)
    stale = str(hermes / "hermes-agent-0.7" / "venv" / "bin" / "hermes")
    if stale_wrapper:
        wrapper.write_text(f'#!/bin/sh\n# installed by hermes 0.7\nexec "{stale}" "$@"\n')
        wrapper.chmod(0o755)
    elif wrapper_ok:
        wrapper.write_text(f'#!/bin/sh\nexec "{launcher}" "$@"\n')
        wrapper.chmod(0o755)
    if config is not None:
        (hermes / "config.yaml").write_text(config)
    if env is not None:
        (hermes / ".env").write_text(env)
        (hermes / ".env").chmod(0o600)
    pathbin = tmp_path / "pathbin"
    pathbin.mkdir()
    if path_hermes:
        os.symlink(launcher, pathbin / "hermes")
    host = LocalHost(environ={"HOME": str(home), "PATH": str(pathbin), "LANG": "C.UTF-8"}, home=str(home))
    return FakeInstall(tmp_path, home, hermes, launcher, wrapper, stale, host)


@pytest.fixture
def install(tmp_path):
    inst = make_install(tmp_path)
    yield inst
    kill_fake_gateways(tmp_path)


def kill_fake_gateways(root: Path) -> None:
    """Never leave a fake gateway behind, whatever a test did."""
    for pidfile in root.rglob("gateway.pid"):
        try:
            os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def everything_text(*objects) -> str:
    def plain(o):
        return o.to_dict() if hasattr(o, "to_dict") else o
    return json.dumps([plain(o) if not isinstance(o, list) else [plain(x) for x in o] for o in objects], default=str)


SECRETS = (KEY, DEEPSEEK_KEY, OPENROUTER_KEY, MGMT_KEY)


def assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# ---- the incident, end to end ---------------------------------------------------------------------------------
async def test_incident_resolved_end_to_end(install):
    gw = FakeGateway()
    adapter = install.adapter(gw, forbidden_keys=[MGMT_KEY])
    original_config = (install.hermes / "config.yaml").read_bytes()
    original_env = (install.hermes / ".env").read_bytes()

    det = await adapter.detect()
    assert det.state == "launcher-broken"
    assert det.on_path is None                                        # the shell could not find hermes
    broken = det.broken_launchers[0]
    assert broken["path"] == str(install.wrapper) and broken["target"] == install.stale_target
    assert install.stale_target in broken["problem"]
    assert det.launcher == str(install.launcher)                      # the working fallback in the venv
    assert det.version == "0.9.1" and det.running is False
    assert det.model["provider"] == "deepseek" and det.model["default"] == "deepseek-chat"
    assert any("PATH" in n for n in det.notes)
    assert any(str(install.launcher) in n and "re-run the Hermes installer" in n for n in det.notes)

    endpoint = gw.endpoint()
    report = await adapter.inspect(endpoint)
    assert report["model"]["provider"] == "deepseek" and report["model"]["default"] == "deepseek-chat"
    assert report["key_env"] is None and report["configured"] is False
    ids = {i["id"] for i in report["issues"]}
    assert {"other-provider", "other-base-url"} <= ids
    tasks = {r["task"]: r["class"] for r in report["cloud_routes"]}
    assert tasks == {"auxiliary.vision": "cloud", "auxiliary.web_extract": "auto", "fallback_model": "cloud"}
    assert "compression" not in tasks                                 # summary_provider: main follows the model

    preview = await adapter.preview(endpoint)
    assert preview.token and not preview.blocked
    config_diff = next(f for f in preview.files if f.label == "config.yaml").diff
    env_change = next(f for f in preview.files if f.label == ".env")
    assert "+  key_env: TWINSPARK_API_KEY" in config_diff and "+  provider: custom" in config_diff
    assert "-  provider: deepseek" in config_diff and "+  context_length: 131072" in config_diff
    assert "+TWINSPARK_API_KEY=[REDACTED] (new value)" in env_change.diff
    assert " DEEPSEEK_API_KEY=[REDACTED]" in env_change.diff
    assert env_change.before_hash is not None
    assert any("auxiliary.vision" in w and "leave the Sparks" in w for w in preview.warnings)
    assert_no_secrets(everything_text(det, report, preview))

    result = await adapter.apply(endpoint, preview.token)
    assert result.changed and result.backup is not None
    cfg = yaml.safe_load((install.hermes / "config.yaml").read_text())
    assert cfg["model"] == {"provider": "custom", "default": "glm", "base_url": BASE, "max_tokens": 8192,
                            "key_env": "TWINSPARK_API_KEY", "context_length": 131072}
    text = (install.hermes / "config.yaml").read_text()
    assert "max_turns: 60      # keep runs short" in text and "# what the agent may do" in text
    assert cfg["auxiliary"]["vision"]["provider"] == "openrouter"     # reported, never changed
    env_text = (install.hermes / ".env").read_text()
    assert env_text == DEEPSEEK_ENV + f"TWINSPARK_API_KEY={KEY}\n"
    assert stat.S_IMODE((install.hermes / ".env").stat().st_mode) == 0o600
    assert KEY not in text

    checks = await adapter.validate(endpoint)
    by = {c.id: c for c in checks}
    for check_id in ("transport", "auth", "alias", "context", "chat", "streaming", "tools"):
        assert by[check_id].status == "pass", (check_id, by[check_id].detail)
    assert by["hermes.config"].status == "pass" and by["hermes.key"].status == "pass"
    assert by["hermes.context"].status == "pass" and by["hermes.config_check"].status == "pass"
    assert by["hermes.launcher"].status == "warn" and str(install.wrapper) in by["hermes.launcher"].detail
    assert by["hermes.routing"].status == "warn" and "auxiliary.vision" in by["hermes.routing"].detail

    launched = await adapter.launch("gateway")
    assert launched.action == "started" and launched.pid
    assert int((install.hermes / "gateway.pid").read_text()) == launched.pid
    assert launched.command[:1] == [str(install.launcher)] and launched.command[-2:] == ["gateway", "run"]

    status = await adapter.status(endpoint)
    assert status.state.to_dict() == {"installed": True, "configured": True, "reachable": True,
                                      "compatible": True, "running": True, "launcher_ok": True,
                                      "phase": "running"}
    assert status.detection.started_by_twinspark and status.detection.gateway_pid == launched.pid
    deadline = time.monotonic() + 5
    while not any("loaded key=" in line for line in await adapter.logs(20, errors=True)):
        assert time.monotonic() < deadline, await adapter.logs(20, errors=True)
        await asyncio.sleep(0.05)
    logs, errors = await adapter.logs(20), await adapter.logs(20, errors=True)
    assert any("gateway up: model glm" in line for line in logs)
    assert any(line.startswith("Authorization: [REDACTED]") for line in logs)
    assert any("key=[REDACTED]" in line for line in errors)
    assert_no_secrets(everything_text(checks, launched, status, logs, errors, result))

    stopped = await adapter.stop()
    assert stopped.stopped and stopped.method == "terminated" and stopped.pid == launched.pid
    assert not (await adapter.detect()).running

    restored = await adapter.restore()
    assert restored.backup.id == result.backup.id
    assert (install.hermes / "config.yaml").read_bytes() == original_config
    assert (install.hermes / ".env").read_bytes() == original_env
    assert (await adapter.inspect(endpoint))["configured"] is False
    assert_no_secrets(everything_text(restored))


async def test_backups_are_byte_exact_and_private(install):
    gw = FakeGateway()
    adapter = install.adapter(gw)
    (install.hermes / "config.yaml").write_bytes(DEEPSEEK_CONFIG.replace("\n", "\r\n").encode())
    preview = await adapter.preview(gw.endpoint())
    result = await adapter.apply(gw.endpoint(), preview.token)
    location = Path(result.backup.location)
    assert stat.S_IMODE(location.stat().st_mode) == 0o700
    assert (location / "config.yaml").read_bytes() == DEEPSEEK_CONFIG.replace("\n", "\r\n").encode()
    assert (location / ".env").read_bytes() == DEEPSEEK_ENV.encode()
    for name in (".env", "config.yaml", "manifest.json"):
        assert stat.S_IMODE((location / name).stat().st_mode) == 0o600
    manifest = (location / "manifest.json").read_text()
    assert_no_secrets(manifest)
    # CRLF files keep CRLF outside the rewritten block.
    assert b"agent:\r\n  max_turns: 60" in (install.hermes / "config.yaml").read_bytes()
    assert [b.id for b in await adapter.backups()] == [result.backup.id]


# ---- refusals -------------------------------------------------------------------------------------------------
async def test_context_below_64k_is_refused(install):
    gw = FakeGateway(aliases={"small": {"max_model_len": 32768}})
    adapter = install.adapter(gw)
    before = (install.hermes / "config.yaml").read_bytes()
    preview = await adapter.preview(gw.endpoint("small"))
    assert preview.token is None and preview.files == []
    assert "32,768" in preview.blocked[0] and "64,000" in preview.blocked[0]
    assert "does not raise the number" in preview.blocked[0]
    with pytest.raises(AdapterError) as exc:
        await adapter.apply(gw.endpoint("small"), "v1." + "0" * 24 + "." + "0" * 24)
    assert exc.value.code == "context-too-small"
    unknown = await adapter.preview(Endpoint(BASE, "glm", KEY, None))
    assert unknown.token is None and "context limit" in unknown.blocked[0]
    assert (install.hermes / "config.yaml").read_bytes() == before
    assert not (install.hermes / ".twinspark").exists()


async def test_apply_refused_when_files_changed_since_preview(install):
    gw = FakeGateway()
    adapter = install.adapter(gw)
    preview = await adapter.preview(gw.endpoint())
    edited = (install.hermes / ".env").read_text() + "EXTRA=1\n"
    (install.hermes / ".env").write_text(edited)
    with pytest.raises(AdapterError) as exc:
        await adapter.apply(gw.endpoint(), preview.token)
    assert exc.value.code == "stale-preview" and "Preview again" in str(exc.value)
    assert (install.hermes / ".env").read_text() == edited
    assert (install.hermes / "config.yaml").read_text() == DEEPSEEK_CONFIG
    assert await adapter.backups() == []

    fresh = await adapter.preview(gw.endpoint())
    other = Endpoint("http://127.0.0.1:9999/v1", "glm", KEY, 131072)
    with pytest.raises(AdapterError) as exc:
        await adapter.apply(other, fresh.token)
    assert exc.value.code == "preview-mismatch"
    with pytest.raises(AdapterError) as exc:
        await adapter.apply(gw.endpoint(), "not-a-token")
    assert exc.value.code == "invalid-token"
    assert (await adapter.apply(gw.endpoint(), fresh.token)).changed


async def test_management_key_is_never_written(install):
    gw = FakeGateway()
    adapter = install.adapter(gw, forbidden_keys=[MGMT_KEY])
    wrong = Endpoint(BASE, "glm", MGMT_KEY, 131072)
    preview = await adapter.preview(wrong)
    assert preview.token is None and "not the inference key" in preview.blocked[0]
    with pytest.raises(AdapterError) as exc:
        await adapter.apply(wrong, "v1." + "0" * 24 + "." + "0" * 24)
    assert exc.value.code == "wrong-key"
    assert_no_secrets(everything_text(preview) + str(exc.value))
    assert MGMT_KEY not in (install.hermes / ".env").read_text()


async def test_invalid_yaml_is_refused_without_writing(install):
    (install.hermes / "config.yaml").write_text("model:\n  provider: [unclosed\n")
    adapter = install.adapter(FakeGateway())
    preview = await adapter.preview(FakeGateway().endpoint())
    assert preview.token is None and "not valid YAML" in preview.blocked[0]
    assert (install.hermes / "config.yaml").read_text() == "model:\n  provider: [unclosed\n"


# ---- additive edits -------------------------------------------------------------------------------------------
async def test_unrelated_settings_and_secrets_preserved_and_reapply_is_a_no_op(install):
    gw = FakeGateway()
    adapter = install.adapter(gw)
    preview = await adapter.preview(gw.endpoint())
    await adapter.apply(gw.endpoint(), preview.token)
    before = yaml.safe_load(DEEPSEEK_CONFIG)
    after = yaml.safe_load((install.hermes / "config.yaml").read_text())
    assert {k: v for k, v in after.items() if k != "model"} == {k: v for k, v in before.items() if k != "model"}
    text = (install.hermes / "config.yaml").read_text()
    marker = "\n# what the agent may do\n"
    assert text.split(marker)[1] == DEEPSEEK_CONFIG.split(marker)[1]   # everything after the model block, verbatim
    assert text.startswith("# Hermes settings (hand-written)\nmodel:\n")
    again = await adapter.preview(gw.endpoint())
    assert not again.changed and any("already uses" in n for n in again.notes)
    result = await adapter.apply(gw.endpoint(), again.token)
    assert not result.changed and len(await adapter.backups()) == 1


async def test_key_rotation_replaces_only_our_line(install):
    gw = FakeGateway()
    adapter = install.adapter(gw)
    await adapter.apply(gw.endpoint(), (await adapter.preview(gw.endpoint())).token)
    rotated = "inference-key-rotated-0002"
    ep = Endpoint(BASE, "glm", rotated, 131072)
    preview = await adapter.preview(ep)
    env_diff = next(f for f in preview.files if f.label == ".env").diff
    assert "-TWINSPARK_API_KEY=[REDACTED]" in env_diff and "+TWINSPARK_API_KEY=[REDACTED] (new value)" in env_diff
    assert rotated not in env_diff and KEY not in env_diff
    await adapter.apply(ep, preview.token)
    assert (install.hermes / ".env").read_text() == DEEPSEEK_ENV + f"TWINSPARK_API_KEY={rotated}\n"


async def test_inline_duplicate_key_is_removed_and_other_inline_key_kept(tmp_path):
    cfg = f"model:\n  provider: custom\n  api_key: {KEY}\n  base_url: http://old:1/v1\n"
    inst = make_install(tmp_path, config=cfg)
    try:
        adapter = inst.adapter(FakeGateway())
        ep = FakeGateway().endpoint()
        preview = await adapter.preview(ep)
        assert any("duplicated" in n for n in preview.notes)
        assert_no_secrets(everything_text(preview))
        await adapter.apply(ep, preview.token)
        assert "api_key" not in yaml.safe_load((inst.hermes / "config.yaml").read_text())["model"]
        other = "sk-otherInlineKeyValue0123456789"
        (inst.hermes / "config.yaml").write_text(f"model:\n  provider: custom\n  api_key: {other}\n")
        preview = await adapter.preview(ep)
        assert any("inline model.api_key" in w for w in preview.warnings)
        assert other not in everything_text(preview)
        await adapter.apply(ep, preview.token)
        assert yaml.safe_load((inst.hermes / "config.yaml").read_text())["model"]["api_key"] == other
    finally:
        kill_fake_gateways(tmp_path)


async def test_comment_loss_is_announced_when_the_block_cannot_be_edited_in_place(tmp_path):
    cfg = "# my settings\n{model: {provider: deepseek}, agent: {max_turns: 5}}\n"
    inst = make_install(tmp_path, config=cfg)
    adapter = inst.adapter(FakeGateway())
    ep = FakeGateway().endpoint()
    preview = await adapter.preview(ep)
    assert any("comments and formatting are not kept" in w for w in preview.warnings)
    result = await adapter.apply(ep, preview.token)
    assert yaml.safe_load((inst.hermes / "config.yaml").read_text())["agent"] == {"max_turns": 5}
    await adapter.restore(result.backup.id)
    assert (inst.hermes / "config.yaml").read_text() == cfg


async def test_fresh_install_without_files_gets_new_private_files(tmp_path):
    inst = make_install(tmp_path, config=None, env=None, stale_wrapper=False, wrapper_ok=True)
    adapter = inst.adapter(FakeGateway())
    ep = FakeGateway().endpoint()
    det = await adapter.detect()
    assert det.state == "installed" and det.launcher == str(inst.wrapper)
    preview = await adapter.preview(ep)
    assert all(f.before_hash is None for f in preview.files)
    await adapter.apply(ep, preview.token)
    assert stat.S_IMODE((inst.hermes / ".env").stat().st_mode) == 0o600
    assert (inst.hermes / ".env").read_text() == f"TWINSPARK_API_KEY={KEY}\n"
    restored = await adapter.restore()
    assert sorted(restored.removed) == sorted([str(inst.hermes / "config.yaml"), str(inst.hermes / ".env")])
    assert not (inst.hermes / "config.yaml").exists() and not (inst.hermes / ".env").exists()


async def test_disconnect_removes_only_twinspark_settings(install):
    gw = FakeGateway()
    adapter = install.adapter(gw)
    await adapter.apply(gw.endpoint(), (await adapter.preview(gw.endpoint())).token)
    (install.hermes / ".env").write_text((install.hermes / ".env").read_text() + "NEW_TOOL_KEY=abcdef123456\n")
    preview = await adapter.preview_disconnect()
    assert preview.token and preview.changed
    assert_no_secrets(everything_text(preview))
    result = await adapter.disconnect(preview.token)
    assert result.changed and any("previous model settings" in n for n in result.notes)
    cfg = yaml.safe_load((install.hermes / "config.yaml").read_text())
    assert cfg["model"] == {"provider": "deepseek", "default": "deepseek-chat",
                            "base_url": "https://api.deepseek.com/v1", "max_tokens": 8192}
    assert cfg["auxiliary"] == yaml.safe_load(DEEPSEEK_CONFIG)["auxiliary"]
    env_text = (install.hermes / ".env").read_text()
    assert "TWINSPARK_API_KEY" not in env_text
    assert env_text == DEEPSEEK_ENV + "NEW_TOOL_KEY=abcdef123456\n"
    assert not (await adapter.preview_disconnect()).changed
    # disconnect is itself backed up and reversible
    await adapter.restore()
    assert "TWINSPARK_API_KEY" in (install.hermes / ".env").read_text()


async def test_disconnect_without_previous_values_removes_the_keys(install):
    gw = FakeGateway()
    adapter = install.adapter(gw)
    await adapter.apply(gw.endpoint(), (await adapter.preview(gw.endpoint())).token)
    result = await adapter.disconnect(restore_previous=False)
    assert result.changed
    assert yaml.safe_load((install.hermes / "config.yaml").read_text())["model"] == {"max_tokens": 8192}


async def test_disconnect_keeps_settings_changed_after_apply(install):
    gw = FakeGateway()
    adapter = install.adapter(gw)
    await adapter.apply(gw.endpoint(), (await adapter.preview(gw.endpoint())).token)
    path = install.hermes / "config.yaml"
    path.write_text(path.read_text().replace("context_length: 131072", "context_length: 100000"))
    result = await adapter.disconnect()
    assert any("model.context_length" in n for n in result.notes)
    assert yaml.safe_load(path.read_text())["model"]["context_length"] == 100000


async def test_restore_specific_backup_and_damaged_backup(install):
    gw = FakeGateway()
    adapter = install.adapter(gw)
    first = await adapter.apply(gw.endpoint(), (await adapter.preview(gw.endpoint())).token)
    ep2 = Endpoint(BASE, "glm", "inference-key-rotated-0002", 131072)
    await adapter.apply(ep2, (await adapter.preview(ep2)).token)
    with pytest.raises(AdapterError) as exc:
        await adapter.restore("../../etc")
    assert exc.value.code == "no-backup"
    Path(first.backup.location, ".env").write_text("tampered\n")
    with pytest.raises(AdapterError) as exc:
        await adapter.restore(first.backup.id)
    assert exc.value.code == "backup-damaged"
    restored = await adapter.restore()                # the latest: before the key rotation
    assert f"TWINSPARK_API_KEY={KEY}" in (install.hermes / ".env").read_text()
    assert restored.safety_backup is not None and restored.safety_backup.kind == "restore"


async def test_restore_without_backup_says_so(install):
    with pytest.raises(AdapterError) as exc:
        await install.adapter().restore()
    assert exc.value.code == "no-backup" and "nothing to restore" in str(exc.value)


# ---- detection ------------------------------------------------------------------------------------------------
async def test_not_installed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "pathbin").mkdir()
    host = LocalHost(environ={"HOME": str(home), "PATH": str(tmp_path / "pathbin")}, home=str(home))
    adapter = HermesAdapter(host)
    det = await adapter.detect()
    assert det.state == "not-installed" and det.launcher is None and not det.running
    assert "Install Hermes Agent" in det.notes[0]
    status = await adapter.status()
    assert status.state.phase == "not-installed" and status.logs == []
    with pytest.raises(AdapterError):
        await adapter.launch("cli")


async def test_launcher_on_path_works(tmp_path):
    inst = make_install(tmp_path, stale_wrapper=False, path_hermes=True)
    det = await inst.adapter().detect()
    assert det.state == "installed" and det.on_path == str(tmp_path / "pathbin" / "hermes")
    assert det.launcher == det.on_path and not det.broken_launchers


async def test_broken_symlink_and_missing_interpreter(tmp_path):
    inst = make_install(tmp_path, stale_wrapper=False)
    os.symlink(tmp_path / "gone" / "hermes", inst.wrapper)
    det = await inst.adapter().detect()
    assert det.state == "launcher-broken" and det.broken_launchers[0]["problem"].startswith("it is a link to")
    assert det.launcher == str(inst.launcher)
    inst.launcher.write_text("#!/nonexistent/venv/bin/python\nprint('x')\n")
    det = await inst.adapter().detect()
    assert det.launcher is None and det.state == "launcher-broken"
    assert any("/nonexistent/venv/bin/python" in b["problem"] for b in det.broken_launchers)
    assert any("Re-run the Hermes installer" in n for n in det.notes)


async def test_launcher_that_fails_to_run_is_broken(tmp_path):
    inst = make_install(tmp_path, stale_wrapper=False)
    inst.wrapper.write_text("#!/bin/sh\necho 'ModuleNotFoundError: No module named hermes_cli' >&2\nexit 1\n")
    inst.wrapper.chmod(0o755)
    det = await inst.adapter().detect()
    assert det.state == "launcher-broken"
    assert "exit 1" in det.broken_launchers[0]["problem"] and "hermes_cli" in det.broken_launchers[0]["problem"]


async def test_stale_pid_file_is_not_running(install):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    (install.hermes / "gateway.pid").write_text(str(proc.pid))
    det = await install.adapter().detect()
    assert not det.running and det.gateway_pid is None
    assert any("stale PID file" in n for n in det.notes)


async def test_deep_detect_runs_doctor(install):
    det = await install.adapter().detect(deep=True)
    assert det.doctor == ["All components present"]


# ---- launch / stop --------------------------------------------------------------------------------------------
async def test_cli_launch_returns_the_exact_command(install):
    result = await install.adapter().launch("cli")
    assert result.action == "command" and result.command == [str(install.launcher), "chat"]
    assert result.command_line == command_line([str(install.launcher), "chat"], "linux")
    assert "working launcher directly" in result.detail
    assert ["chat"] not in install.calls()                           # nothing was started
    with pytest.raises(ValueError):
        await install.adapter().launch("terminal")


async def test_running_gateway_is_attached_not_duplicated(install):
    env = {"HOME": str(install.home), "PATH": os.environ.get("PATH", "")}
    outside = subprocess.Popen([str(install.launcher), "gateway", "run"], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(100):
            if (install.hermes / "gateway.pid").exists():
                break
            time.sleep(0.05)
        adapter = install.adapter()
        result = await adapter.launch("gateway")
        assert result.action == "attached" and result.pid == outside.pid
        assert sum(1 for argv in install.calls() if argv[-2:] == ["gateway", "run"]) == 1
        stopped = await adapter.stop()
        assert not stopped.stopped and "not started by TwinSpark" in stopped.detail
        assert outside.poll() is None                                 # left running
        with pytest.raises(AdapterError) as exc:
            await adapter.restart()
        assert exc.value.code == "not-owned" and outside.poll() is None
    finally:
        outside.kill()
        outside.wait()


async def test_service_unit_is_used_for_start_and_stop(install):
    unit = install.home / ".config" / "systemd" / "user" / "hermes-gateway.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=hermes gateway run\n")
    adapter = install.adapter()
    det = await adapter.detect()
    assert det.service["name"] == "hermes-gateway.service" and det.service["active"] is None
    result = await adapter.launch("gateway")
    assert result.action == "service-started" and result.pid
    assert result.command[-2:] == ["gateway", "start"]
    restarted = await adapter.restart()
    assert restarted.action == "service-started" and restarted.pid != result.pid
    stopped = await adapter.stop()
    assert stopped.stopped and stopped.method == "service"
    assert not (await adapter.detect()).running
    assert ["gateway", "stop"] in install.calls()


async def test_gateway_that_exits_at_once_is_reported_with_redacted_log(install):
    gw = FakeGateway()
    adapter = install.adapter(gw)
    await adapter.apply(gw.endpoint(), (await adapter.preview(gw.endpoint())).token)
    path = install.hermes / "config.yaml"
    path.write_text(path.read_text().replace("max_tokens: 8192", "max_tokens: 8192\n  fail_start: true"))
    with pytest.raises(AdapterError) as exc:
        await adapter.launch("gateway")
    assert exc.value.code == "launch-failed" and "rejected" in str(exc.value)
    assert KEY not in str(exc.value)
    assert not (await adapter.detect()).running
    assert (await adapter.stop()).stopped is False


async def test_stop_when_nothing_runs(install):
    result = await install.adapter().stop()
    assert not result.stopped and "No Hermes gateway is running" in result.detail


# ---- named profile --------------------------------------------------------------------------------------------
async def test_named_profile_uses_its_own_files_and_flag(install):
    work = install.hermes / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("model:\n  provider: openrouter\n  default: x/y\n")
    (work / ".env").write_text("OPENROUTER_API_KEY=sk-or-workProfileSecret0001\n")
    (install.hermes / "profiles" / "empty-dir").mkdir()
    root_config = (install.hermes / "config.yaml").read_bytes()
    gw = FakeGateway()
    adapter = install.adapter(gw, profile="work")
    det = await adapter.detect()
    assert det.profiles == ["work"] and det.profile_home == str(work) and det.config_path == str(work / "config.yaml")
    preview = await adapter.preview(gw.endpoint())
    assert all(f.path.startswith(str(work)) for f in preview.files)
    await adapter.apply(gw.endpoint(), preview.token)
    assert yaml.safe_load((work / "config.yaml").read_text())["model"]["default"] == "glm"
    assert (work / ".env").read_text().endswith(f"TWINSPARK_API_KEY={KEY}\n")
    assert (install.hermes / "config.yaml").read_bytes() == root_config
    assert "TWINSPARK_API_KEY" not in (install.hermes / ".env").read_text()
    assert (work / ".twinspark" / "backups").is_dir() and not (install.hermes / ".twinspark").exists()
    cli = await adapter.launch("cli")
    assert cli.command == [str(install.launcher), "-p", "work", "chat"]
    launched = await adapter.launch("gateway")
    assert launched.action == "started" and int((work / "gateway.pid").read_text()) == launched.pid
    assert not (install.hermes / "gateway.pid").exists()
    assert (await install.adapter().detect()).running is False       # the default profile's gateway is separate
    assert (await adapter.stop()).stopped
    checks = {c.id: c for c in await adapter.validate(gw.endpoint())}
    assert checks["hermes.config"].status == "pass"
    await adapter.restore()
    assert yaml.safe_load((work / "config.yaml").read_text())["model"] == {"provider": "openrouter", "default": "x/y"}
    assert (install.hermes / "config.yaml").read_bytes() == root_config


async def test_other_data_directory_is_named(install, tmp_path):
    alt = tmp_path / "alt-hermes"
    alt.mkdir()
    host = LocalHost(environ={"HOME": str(install.home), "PATH": str(tmp_path / "pathbin"), "HERMES_HOME": str(alt)},
                     home=str(install.home))
    det = await HermesAdapter(host).detect()
    assert det.home == str(alt) and det.other_homes == [str(install.hermes)]
    assert any(str(install.hermes) in n and "HERMES_HOME" in n for n in det.notes)


async def test_missing_named_profile_is_refused(install):
    adapter = install.adapter(FakeGateway(), profile="ghost")
    det = await adapter.detect()
    assert any("profile ghost does not exist" in n for n in det.notes)
    preview = await adapter.preview(FakeGateway().endpoint())
    assert preview.token is None and "Create it in Hermes first" in preview.blocked[0]
    with pytest.raises(ValueError):
        HermesAdapter(install.host, "../etc")


# ---- status phases --------------------------------------------------------------------------------------------
async def test_status_unreachable_and_unverified(install):
    gw = FakeGateway()
    adapter = install.adapter(gw)
    await adapter.apply(gw.endpoint(), (await adapter.preview(gw.endpoint())).token)
    status = await adapter.status(gw.endpoint())
    assert status.state.phase == "unverified" and status.state.reachable and status.state.configured
    from tests.test_client_validate import free_port
    offline = install.adapter()                                       # real client, nothing listening
    ep = Endpoint(f"http://127.0.0.1:{free_port()}/v1", "glm", KEY, 131072)
    await offline.apply(ep, (await offline.preview(ep)).token)
    status = await offline.status(ep)
    assert status.state.reachable is False and status.state.phase == "unreachable"
    assert any("does not answer" in n for n in status.notes)
    # compatibility is evidence about the endpoint and revision; configuration is a separate fact
    await adapter.validate(gw.endpoint())
    state = (await adapter.status(gw.endpoint())).state
    assert state.compatible is True and state.configured is False and state.phase == "not-configured"
    await adapter.apply(gw.endpoint(), (await adapter.preview(gw.endpoint())).token)
    assert (await adapter.status(gw.endpoint())).state.phase == "ready"
    newer = Endpoint(BASE, "glm", KEY, 131072, revision_id="rev-2")   # a new revision needs new evidence
    assert (await adapter.status(newer)).state.compatible is False


async def test_validate_reports_stale_model_and_key_mismatch(install):
    gw = FakeGateway(aliases={"default": {"max_model_len": 131072, "revision": "r"}})
    cfg = (f"model:\n  provider: custom\n  default: glm-old\n  base_url: {BASE}\n  key_env: TWINSPARK_API_KEY\n"
           "  context_length: 131072\nproviders:\n  twinspark:\n    base_url: http://localhost/v1\n"
           f"  spark2:\n    base_url: {BASE}\n")
    (install.hermes / "config.yaml").write_text(cfg)
    (install.hermes / ".env").write_text("TWINSPARK_API_KEY=old-inference-key-0001\n")
    adapter = install.adapter(gw)
    checks = {c.id: c for c in await adapter.validate(gw.endpoint("default"))}
    assert checks["hermes.config"].status == "fail" and "Preview and apply" in checks["hermes.config"].detail
    assert checks["hermes.key"].status == "fail" and "differs" in checks["hermes.key"].detail
    assert "glm-old" in checks["hermes.conflicts"].detail and "providers.spark2" in checks["hermes.conflicts"].detail
    assert "old-inference-key-0001" not in everything_text(list(checks.values()))
    status = await adapter.status(gw.endpoint("default"))
    assert {"stale-model", "key-differs", "duplicate-provider"} <= {i["id"] for i in status.config["issues"]}
    assert status.state.configured is False and status.state.phase == "not-configured"
    assert "old-inference-key-0001" not in everything_text(status)


# ---- pure logic: paths, env, YAML, routing --------------------------------------------------------------------
def test_windows_paths():
    env = {"LOCALAPPDATA": r"C:\Users\Ana\AppData\Local"}
    homes = hermes_home_candidates("windows", env, r"C:\Users\Ana")
    assert homes == [r"C:\Users\Ana\AppData\Local\hermes", r"C:\Users\Ana\.hermes"]
    assert hermes_home_candidates("windows", {**env, "HERMES_HOME": r"D:\hermes"}, r"C:\Users\Ana")[0] == r"D:\hermes"
    assert profile_home("windows", homes[0], "work") == r"C:\Users\Ana\AppData\Local\hermes\profiles\work"
    launchers = launcher_candidates("windows", env, r"C:\Users\Ana", homes[0])
    assert launchers[:2] == [(r"C:\Users\Ana\AppData\Local\hermes\bin\hermes.exe", "entry"),
                             (r"C:\Users\Ana\AppData\Local\hermes\bin\hermes.cmd", "entry")]
    assert (r"C:\Users\Ana\AppData\Local\hermes\hermes-agent\.venv\Scripts\hermes.exe", "internal") in launchers
    assert service_candidates("windows", env, r"C:\Users\Ana", None) == []
    assert command_line([r"C:\Program Files\hermes\hermes.exe", "-p", "work", "chat"], "windows") == \
        r'"C:\Program Files\hermes\hermes.exe" -p work chat'


def test_posix_paths():
    assert hermes_home_candidates("linux", {}, "/home/ana") == ["/home/ana/.hermes"]
    assert hermes_home_candidates("linux", {"HERMES_HOME": "~/alt"}, "/home/ana")[0] == "/home/ana/alt"
    launchers = [p for p, _ in launcher_candidates("linux", {"PREFIX": "/data/data/com.termux/files/usr"},
                                                   "/home/ana", "/home/ana/.hermes")]
    assert launchers[:2] == ["/home/ana/.local/bin/hermes", "/data/data/com.termux/files/usr/bin/hermes"]
    assert "/home/ana/.hermes/hermes-agent/.venv/bin/hermes" in launchers
    assert "/home/ana/.hermes/hermes-agent/venv/bin/hermes" in launchers
    assert "/opt/hermes/.venv/bin/hermes" in launchers
    assert service_candidates("linux", {}, "/home/ana", "work")[0]["path"] == \
        "/home/ana/.config/systemd/user/hermes-gateway-work.service"
    assert service_candidates("darwin", {}, "/Users/ana", None)[0]["path"] == \
        "/Users/ana/Library/LaunchAgents/ai.hermes.gateway.plist"
    assert command_line(["/home/a b/hermes", "chat"], "linux") == "'/home/a b/hermes' chat"


def test_env_editing_is_line_exact():
    text = "# keys\r\nexport TWINSPARK_API_KEY=old\r\nOTHER=1\r\nTWINSPARK_API_KEY=dup\r\n"
    assert env_set(text, "TWINSPARK_API_KEY", "new-key") == "# keys\r\nexport TWINSPARK_API_KEY=new-key\r\nOTHER=1\r\n"
    assert env_set("A=1", "B", "x#y") == "A=1\nB='x#y'\n"
    assert mask_env("A=secret-value # c\n# A=commented\n", "A") == "A=[REDACTED] (new value)\n# A=commented\n"
    with pytest.raises(AdapterError):
        env_set("", "B", "it's")


def test_render_config_rewrites_only_the_model_block():
    text = "a: 1  # one\nmodel:\n  provider: x\n\n  extra: 2\n# next\nb:\n  - c\n"
    cfg = yaml.safe_load(text)
    out, preserved = render_config(text, cfg, "model", {"provider": "custom", "extra": 2})
    assert preserved and out == "a: 1  # one\nmodel:\n  provider: custom\n  extra: 2\n# next\nb:\n  - c\n"
    out, preserved = render_config("a: 1\n", {"a": 1}, "model", {"provider": "custom"})
    assert preserved and out == "a: 1\nmodel:\n  provider: custom\n"
    out, preserved = render_config("model: deepseek-chat # old\nz: 1\n", {"model": "deepseek-chat", "z": 1},
                                   "model", {"default": "glm"})
    assert preserved and out == "model:\n  default: glm\nz: 1\n"


def test_route_classification():
    cfg = {"auxiliary": {"vision": {"provider": "openrouter", "model": "m"},
                         "local_vision": {"provider": "custom", "base_url": "http://192.168.1.20:8000/v1"},
                         "spark": {"provider": "custom", "base_url": "http://localhost:8000/v1"},
                         "same": {"provider": "main"}, "off": {"provider": "none"}},
           "compression": {"enabled": False, "summary_provider": "openrouter"},
           "fallback_providers": ["nous", "openrouter"],
           "delegation": {"provider": "named"}, "providers": {"named": {"base_url": "https://api.example.com/v1"}}}
    routes = {r["task"]: r["class"] for r in scan_routes(cfg, "http://127.0.0.1:8000/v1")}
    assert routes == {"auxiliary.vision": "cloud", "auxiliary.local_vision": "local", "auxiliary.spark": "twinspark",
                      "auxiliary.same": "main", "auxiliary.off": "disabled", "fallback_providers.0": "cloud",
                      "fallback_providers.1": "cloud", "delegation": "cloud"}
