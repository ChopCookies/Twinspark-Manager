"""headless-max is guarded like an immediate change, and remote access is checked before a desktop goes."""

from __future__ import annotations

import json

import httpx
import pytest

from twinspark import cli, cli_remote
from twinspark.controller.app import create_app
from twinspark.headless import acts_now
from twinspark.remote import access, tailscale


def test_which_modes_change_the_desktop_right_now():
    assert acts_now("headless-max", False) and acts_now("headless-max", True)      # with or without --now
    assert acts_now("headless-safe", True) and acts_now("desktop", True)
    assert not acts_now("headless-safe", False) and not acts_now("desktop", False)


async def test_headless_max_is_refused_during_an_activation_even_without_now(cluster):
    c = cluster.controller
    app = create_app(c, management_key="mk", run_startup=False, background=False)
    await c._lock.acquire()                                         # an activation holds the lock
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m",
                                     headers={"x-api-key": "mk"}) as api:
            r = await api.post("/api/v1/system/headless", json={"mode": "headless-max", "now": False})
            assert r.status_code == 409 and "immediately" in r.json()["detail"]
            r = await api.post("/api/v1/system/headless", json={"mode": "desktop", "now": True})
            assert r.status_code == 409
            ok = await api.post("/api/v1/system/headless", json={"mode": "headless-safe", "now": False})
            assert ok.status_code == 200                            # only the next boot changes
    finally:
        c._lock.release()


def fake_run(table: dict[tuple[str, ...], tuple[int, str]]):
    calls = []

    def run(argv, timeout=5.0):
        calls.append(tuple(argv))
        return table.get(tuple(argv), (1, "unknown"))
    run.calls = calls
    return run


TS_STATUS = json.dumps({"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.7", "fd7a::1"]}})
SERVE = json.dumps({"TCP": {"8443": {"TCPForward": "127.0.0.1:8443"}}})


def test_access_facts_are_separate_and_never_guessed():
    run = fake_run({
        ("systemctl", "is-active", "ssh.service"): (3, "inactive"),
        ("systemctl", "is-enabled", "ssh.service"): (1, "disabled"),
        ("systemctl", "is-active", "ssh.socket"): (0, "active"),
        ("systemctl", "is-enabled", "ssh.socket"): (0, "enabled"),
        ("tailscale", "status", "--json"): (0, TS_STATUS),
        ("systemctl", "is-active", "tailscaled.service"): (0, "active"),
        ("systemctl", "is-enabled", "tailscaled.service"): (0, "enabled"),
        ("tailscale", "serve", "status", "--json"): (0, SERVE),
        ("systemctl", "is-enabled", "twinspark-agent.service"): (0, "enabled"),
        ("systemctl", "is-enabled", "twinspark-controller.service"): (0, "enabled"),
        ("systemctl", "get-default"): (0, "multi-user.target"),
    })
    f = access.access_facts(manager_port=8443, controller=True, run=run, which=lambda n: "/usr/bin/" + n)
    assert f["ssh"]["running"] and f["ssh"]["starts_at_boot"]            # socket-activated SSH counts
    assert f["tailscale"]["ip"] == "100.64.0.7" and f["tailscale"]["serve"]["manager_forwarded"]
    assert f["services"] == {"twinspark-agent.service": "enabled", "twinspark-controller.service": "enabled"}
    assert f["remote_paths"] == ["ssh", "tailscale"] and f["default_target"] == "multi-user.target"
    line = access.summary(f)
    assert "starts at boot" in line and "100.64.0.7" in line and "forwarded" in line

    blind = fake_run({("tailscale", "status", "--json"): (0, TS_STATUS),
                      ("tailscale", "serve", "status", "--json"): (1, "Access denied: serve config denied")})
    g = access.access_facts(manager_port=8443, controller=True, run=blind, which=lambda n: "/usr/bin/" + n)
    assert g["tailscale"]["serve"]["manager_forwarded"] is None             # unknown, not "no"
    assert not g["ssh"]["starts_at_boot"] and g["remote_paths"] == ["tailscale"]
    gone_after_reboot = fake_run({("tailscale", "status", "--json"): (0, TS_STATUS),
                                  ("systemctl", "is-enabled", "tailscaled.service"): (1, "disabled")})
    h = access.access_facts(run=gone_after_reboot, which=lambda n: "/usr/bin/" + n)
    assert h["tailscale"]["running"] and h["remote_paths"] == []
    none = access.access_facts(run=fake_run({}), which=lambda n: None)
    assert none["tailscale"]["installed"] is False and none["remote_paths"] == []
    assert "Tailscale not installed" in access.summary(none)


def test_tailscale_serve_shows_the_command_and_changes_nothing_by_default(tmp_path, monkeypatch, capsys):
    run = fake_run({("tailscale", "status", "--json"): (0, TS_STATUS),
                    ("tailscale", "serve", "status", "--json"): (0, json.dumps({}))})
    monkeypatch.setattr(access, "RUN", run)
    monkeypatch.setattr(tailscale, "RUN", run)
    etc = tmp_path / "etc/twinspark"
    etc.mkdir(parents=True)
    (etc / "controller.yaml").write_text("listener:\n  bind: 127.0.0.1\n  port: 8555\n")
    assert cli.main(["--key=k", "remote", "tailscale-serve", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "sudo tailscale serve --bg --tcp=8555 tcp://127.0.0.1:8555" in out and "http://100.64.0.7:8555/" in out
    assert not any(c[:3] == ("tailscale", "serve", "--bg") for c in run.calls)


def test_tailscale_serve_apply_forwards_and_checks_the_health_page(tmp_path, monkeypatch, capsys):
    run = fake_run({("tailscale", "status", "--json"): (0, TS_STATUS),
                    ("tailscale", "serve", "status", "--json"): (0, "{}"),
                    ("tailscale", "serve", "--bg", "--tcp=8443", "tcp://127.0.0.1:8443"): (0, "")})
    monkeypatch.setattr(tailscale, "RUN", run)
    monkeypatch.setattr(cli_remote.os, "geteuid", lambda: 0)
    seen = []
    monkeypatch.setattr(tailscale, "verify",
                        lambda ip, port, scheme="http": (seen.append((scheme, ip, port)) or True, f"{ip}:{port} ok"))
    assert cli.main(["--key=k", "remote", "tailscale-serve", "--apply", "--port", "8443",
                     "--root", str(tmp_path)]) == 0
    assert ("tailscale", "serve", "--bg", "--tcp=8443", "tcp://127.0.0.1:8443") in run.calls
    assert seen == [("http", "100.64.0.7", 8443)] and "persistent" in capsys.readouterr().out
    etc = tmp_path / "etc/twinspark"                                      # a TLS listener is checked over https
    etc.mkdir(parents=True)
    (etc / "controller.yaml").write_text("listener:\n  port: 8443\n  tls_cert: /etc/twinspark/tls.crt\n")
    assert cli.main(["--key=k", "remote", "tailscale-serve", "--apply", "--root", str(tmp_path)]) == 0
    assert seen[-1] == ("https", "100.64.0.7", 8443)


def test_tailscale_serve_refuses_a_controller_without_a_management_key(tmp_path, monkeypatch):
    """With management_auth: none the API only checks Host — which a tailnet peer can set to localhost."""
    run = fake_run({})
    monkeypatch.setattr(tailscale, "RUN", run)
    monkeypatch.setattr(cli_remote.os, "geteuid", lambda: 0)
    etc = tmp_path / "etc/twinspark"
    etc.mkdir(parents=True)
    (etc / "controller.yaml").write_text("management_auth: none\nlistener:\n  bind: 127.0.0.1\n  port: 8443\n")
    with pytest.raises(SystemExit, match="management_auth is 'none'.*management_auth: apikey"):
        cli.main(["--key=k", "remote", "tailscale-serve", "--apply", "--root", str(tmp_path)])
    (etc / "controller.yaml").write_text("listener: [unclosed\n")          # cannot check it: no forward either
    with pytest.raises(SystemExit, match="not readable"):
        cli.main(["--key=k", "remote", "tailscale-serve", "--apply", "--port", "8443", "--root", str(tmp_path)])
    assert run.calls == []


def test_tailscale_serve_apply_needs_root(tmp_path, monkeypatch):
    run = fake_run({("tailscale", "status", "--json"): (0, TS_STATUS),
                    ("tailscale", "serve", "status", "--json"): (0, "{}")})
    monkeypatch.setattr(tailscale, "RUN", run)
    monkeypatch.setattr(cli_remote.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit, match="needs root"):
        cli.main(["--key=k", "remote", "tailscale-serve", "--apply", "--port", "8443", "--root", str(tmp_path)])


def test_verify_reports_what_answered():
    class R:
        status_code = 200
    assert tailscale.verify("100.64.0.7", 8443, get=lambda url, timeout: R()) == (
        True, "http://100.64.0.7:8443/api/v1/health answered 200")

    def boom(url, timeout):
        raise httpx.ConnectError("refused")
    ok, detail = tailscale.verify("100.64.0.7", 8443, get=boom)
    assert not ok and "did not answer (ConnectError)" in detail


def test_headless_max_asks_first_and_shows_the_way_in(monkeypatch, capsys):
    posted = []

    class FakeApi:
        def __init__(self, *a, **k):
            self.as_json = False

        def __call__(self, method, path, **kw):
            if method == "GET":
                return {"nodes": {"A": {"access": {"ssh": {"running": True, "starts_at_boot": True},
                                                   "tailscale": {"installed": False}, "remote_paths": ["ssh"]}},
                                  "B": {"access": {"ssh": {"running": False}, "tailscale": {"installed": False},
                                                   "remote_paths": []}}}}
            posted.append(kw.get("json"))
            return {"mode": "headless-max", "results": {}}

    monkeypatch.setattr(cli, "Api", FakeApi)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    with pytest.raises(SystemExit, match="cancelled"):
        cli.main(["--key=k", "headless", "headless-max"])
    out = capsys.readouterr().out
    assert "closes the desktop session on every node NOW" in out and "WARNING: no SSH-at-boot" in out and " B" in out
    assert posted == []

    def no_tty(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", no_tty)                         # a script without -y: no traceback
    with pytest.raises(SystemExit, match="Pass -y"):
        cli.main(["--key=k", "headless", "headless-max"])
    assert posted == []
    cli.main(["--key=k", "headless", "headless-max", "--yes"])
    assert posted == [{"mode": "headless-max", "now": False}]


async def test_doctor_fails_a_keyless_api_that_is_forwarded_on_the_tailnet(cluster, monkeypatch):
    from twinspark.controller import diagnostics
    c = cluster.controller
    a = c.agents[c.config.node.node_id]
    real = a.call

    async def call(action, /, timeout=60, **params):
        if action == "headless_status":
            assert params == {"manager_port": c.config.listener.port}
            return {"access": {"tailscale": {"serve": {"manager_forwarded": True}}}}
        return await real(action, timeout=timeout, **params)
    monkeypatch.setattr(a, "call", call)
    assert "management auth" not in {x["check"] for x in (await diagnostics.doctor(c))["checks"]}   # key required
    monkeypatch.setattr(c.config, "management_auth", "none")
    bad = [x for x in (await diagnostics.doctor(c))["checks"] if x["check"] == "management auth"]
    assert bad and bad[0]["status"] == "fail" and "tailscale-serve --remove" in bad[0]["fix"]
    unknown = {"access": {"tailscale": {"running": True, "serve": {"manager_forwarded": None}}}}

    async def blind(action, /, timeout=60, **params):                  # serve status needs root: unknown
        return unknown if action == "headless_status" else await real(action, timeout=timeout, **params)
    monkeypatch.setattr(a, "call", blind)
    warn = [x for x in (await diagnostics.doctor(c))["checks"] if x["check"] == "management auth"]
    assert warn and warn[0]["status"] == "warn" and "could not be read" in warn[0]["detail"]
