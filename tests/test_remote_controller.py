"""Controller side of remote management: API, tickets, power confirmations, Wake-on-LAN, smart plug,
reachability triage — and the whole browser -> controller -> node terminal path over real sockets."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import tarfile
import time

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from twinspark.controller import remote as remote_mod
from twinspark.controller.agent_client import AgentActionError
from twinspark.controller.app import create_app
from twinspark.controller.remote import RemoteError, _triage, magic_packet
from twinspark.demo import DemoCluster
from twinspark.schemas.config import PlugRequest, PlugSettings, WakeSettings
from twinspark.security import SecretsVault

KEY = "mgmt-key"
UNREACHABLE = AgentActionError("remote_status", "B", "agent unreachable (ConnectError)")
MAC = "aa:bb:cc:dd:ee:ff"
PLUG_TOKEN = "plug-token-" + "Zx81QwErTy"


class FakeAgent:
    """Records the typed actions the controller asks for; answers from a table."""

    def __init__(self, node: str, answers: dict | None = None):
        self.node = node
        self.calls: list[tuple[str, dict]] = []
        self.answers = answers or {}
        self._headers = {"authorization": "Bearer fake"}

    async def call(self, action, /, timeout=60, **params):
        self.calls.append((action, params))
        ans = self.answers.get(action, {})
        if isinstance(ans, Exception):
            raise ans
        return ans(**params) if callable(ans) else ans

    async def aclose(self):
        pass

    def actions(self) -> list[str]:
        return [a for a, _ in self.calls]


def status_answer(terminal=True, service=True, **extra):
    return {"node": "B", "policy": {"terminal": terminal, "error": None}, "enabled": ["terminal"] if terminal else [],
            "terminal_service": service, "privd": True, "interfaces": [], "power_pending": [], **extra}


@pytest.fixture
def ctl(cluster, controller_config):
    """In-process controller whose nodes are FakeAgents; B has Wake-on-LAN and a smart plug configured."""
    cfg = cluster.controller.config
    cfg.nodes["B"].wake = WakeSettings(mac=MAC, broadcast="192.168.100.255")
    auth = {"Authorization": "Bearer ${secret:plug_token}"}
    cfg.nodes["B"].plug = PlugSettings(
        on=PlugRequest(url="http://plug.lan/cm?cmnd=Power%20On", headers=auth),
        off=PlugRequest(url="http://plug.lan/cm?cmnd=Power%20Off", headers=auth), settle_s=3)
    SecretsVault(cfg.secrets_dir).set("plug_token", PLUG_TOKEN)
    c = cluster.controller
    c.agents["A"] = FakeAgent("A", {"remote_status": status_answer(node="A")})
    c.agents["B"] = FakeAgent("B", {"remote_status": status_answer(),
                                    "remote_power": lambda **p: {"scheduled": p["action"], "in_s": p["delay_s"]},
                                    "remote_power_cancel": {"cancelled": ["reboot"]},
                                    "remote_boot_status": {"current": "0001", "entries": []},
                                    "remote_boot_next": lambda **p: {"next": p["target"]},
                                    "remote_boot_next_clear": {"cleared": True},
                                    "remote_wol_set": lambda **p: {"interface": p["iface"], "mode": p["mode"]}})
    return c


def api(controller, **headers) -> httpx.AsyncClient:
    app = create_app(controller, KEY, run_startup=False, background=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                             base_url="http://m", headers={"x-api-key": KEY, **headers})


def audits(controller, action: str) -> list:
    return [e for e in controller.store.audit_log(limit=200) if e.action == action]


# ---- auth and validation ---------------------------------------------------------------------
@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/v1/remote/overview", None), ("GET", "/api/v1/remote/B/status", None),
    ("GET", "/api/v1/remote/B/reach", None), ("GET", "/api/v1/remote/B/logs", None),
    ("GET", "/api/v1/remote/B/bundle", None), ("GET", "/api/v1/remote/B/recordings", None),
    ("GET", "/api/v1/remote/B/boot", None),
    ("POST", "/api/v1/remote/terminal/ticket", {"node": "B"}),
    ("POST", "/api/v1/remote/B/power", {"action": "reboot", "confirm": "REBOOT B"}),
    ("POST", "/api/v1/remote/B/power/cancel", {}), ("POST", "/api/v1/remote/B/boot/next", {}),
    ("POST", "/api/v1/remote/B/boot/clear", {}), ("POST", "/api/v1/remote/B/wol/set", {"iface": "eth0"}),
    ("POST", "/api/v1/remote/B/wake", {}), ("POST", "/api/v1/remote/B/plug", {"action": "on"}),
])
async def test_every_remote_route_needs_the_management_key(ctl, method, path, body):
    async with api(ctl) as c:
        c.headers.pop("x-api-key")
        r = await c.request(method, path, json=body)
        assert r.status_code == 401, path
    assert not [a for a in ctl.agents.values() if a.calls], "nothing may reach a node without the key"


async def test_cross_origin_posts_are_refused(ctl):
    async with api(ctl, origin="http://evil.example") as c:
        r = await c.post("/api/v1/remote/B/power", json={"action": "reboot", "confirm": "REBOOT B"})
        assert r.status_code == 403
    assert ctl.agents["B"].calls == []


@pytest.mark.parametrize("method,path,body,status", [
    ("GET", "/api/v1/remote/Z/status", None, 404),
    ("GET", "/api/v1/remote/B/logs?lines=0", None, 422),
    ("GET", "/api/v1/remote/B/logs?lines=99999", None, 422),
    ("GET", "/api/v1/remote/B/logs?source=shadow", None, 422),
    ("GET", "/api/v1/remote/B/logs?grep=" + "x" * 101, None, 422),
    ("POST", "/api/v1/remote/terminal/ticket", {"node": "B", "cols": 100000}, 422),
    ("POST", "/api/v1/remote/terminal/ticket", {"node": "Z"}, 404),
    ("POST", "/api/v1/remote/B/power", {"action": "halt", "confirm": "x"}, 422),
    ("POST", "/api/v1/remote/B/power", {"action": "reboot", "delay_s": 0}, 422),
    ("POST", "/api/v1/remote/B/power", {"action": "reboot", "confirm": "x" * 500}, 422),
    ("POST", "/api/v1/remote/B/boot/next", {"target": "../../etc"}, 422),
    ("POST", "/api/v1/remote/B/wol/set", {"iface": "eth0; reboot"}, 422),
    ("POST", "/api/v1/remote/B/wol/set", {"iface": "eth0", "mode": "x"}, 422),
    ("POST", "/api/v1/remote/B/plug", {"action": "explode"}, 422),
])
async def test_bad_requests_are_refused_before_a_node_is_asked(ctl, method, path, body, status):
    async with api(ctl) as c:
        r = await c.request(method, path, json=body)
    assert r.status_code == status, r.text
    assert ctl.agents["B"].calls == []


# ---- overview, logs, bundle ------------------------------------------------------------------
async def test_overview_survives_a_dead_node(ctl):
    ctl.agents["B"].answers["remote_status"] = UNREACHABLE
    async with api(ctl) as c:
        r = (await c.get("/api/v1/remote/overview")).json()
    assert r["nodes"]["A"]["reachable"] is True and r["controller_node"] == "A"
    b = r["nodes"]["B"]
    assert b["reachable"] is False and "unreachable" in b["error"]
    assert b["wake_configured"] and b["plug_configured"] and b["mac"] == MAC
    assert b["plug_actions"] == ["off", "on"]


async def test_logs_are_passed_through_with_the_source_list(ctl):
    ctl.agents["B"].answers["remote_logs"] = {"source": "kernel", "lines": ["a", "b"], "count": 2, "via": "direct",
                                              "note": None}
    async with api(ctl) as c:
        r = (await c.get("/api/v1/remote/B/logs?source=kernel&lines=50&since_s=600&grep=nvme")).json()
    assert r["lines"] == ["a", "b"] and "agent" in r["sources"]
    assert ctl.agents["B"].calls[-1] == ("remote_logs", {"source": "kernel", "lines": 50, "since_s": 600,
                                                         "grep": "nvme"})


async def test_bundle_downloads_as_a_safe_gzip_file(ctl):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"hello"
        info = tarfile.TarInfo("tsm-bundle-B/README.txt")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    ctl.agents["B"].answers["remote_bundle"] = {"name": 'x/../"bad name".tar.gz', "size": 1, "problems": ["a", "b"],
                                                "b64": base64.b64encode(buf.getvalue()).decode()}
    async with api(ctl) as c:
        r = await c.get("/api/v1/remote/B/bundle")
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    assert r.headers["x-tsm-problems"] == "2" and r.headers["cache-control"] == "no-store"
    disp = r.headers["content-disposition"]
    assert "/" not in disp.split("filename=")[1] and disp.count('"') == 2
    assert r.content == buf.getvalue(), "the archive must arrive byte for byte, not compressed twice"
    assert audits(ctl, "remote.bundle")


# ---- terminal tickets ------------------------------------------------------------------------
async def test_ticket_is_single_use_and_expires(ctl):
    s = ctl.remote
    t = await s.issue_ticket("B", 100, 30, actor="user")
    assert s.redeem("nonsense") is None
    got = s.redeem(t["ticket"])
    assert got and got.node == "B" and (got.cols, got.rows) == (100, 30)
    assert s.redeem(t["ticket"]) is None, "a ticket works once"
    t2 = await s.issue_ticket("B")
    s.tickets[t2["ticket"]].expires = time.monotonic() - 1
    assert s.redeem(t2["ticket"]) is None, "an old ticket is worthless"
    assert s.redeem(None) is None and s.redeem(["x"]) is None


async def test_ticket_cap(ctl):
    for _ in range(remote_mod.MAX_TICKETS):
        await ctl.remote.issue_ticket("B")
    with pytest.raises(RemoteError) as err:
        await ctl.remote.issue_ticket("B")
    assert err.value.status == 429


async def test_ticket_explains_how_to_turn_the_terminal_on(ctl):
    ctl.agents["B"].answers["remote_status"] = status_answer(terminal=False)
    async with api(ctl) as c:
        r = await c.post("/api/v1/remote/terminal/ticket", json={"node": "B"})
    assert r.status_code == 409 and "sudo tsm remote enable terminal" in r.json()["detail"]
    assert ctl.remote.tickets == {}


async def test_ticket_explains_a_missing_terminal_service_and_an_unsafe_policy(ctl):
    ctl.agents["B"].answers["remote_status"] = status_answer(service=False)
    with pytest.raises(RemoteError, match="systemctl enable --now twinspark-terminal"):
        await ctl.remote.issue_ticket("B")
    st = status_answer(terminal=False)
    st["policy"]["error"] = "policy file is writable by group/other"
    ctl.agents["B"].answers["remote_status"] = st
    with pytest.raises(RemoteError, match="writable by group/other"):
        await ctl.remote.issue_ticket("B")


async def test_ticket_for_an_unreachable_node_says_so(ctl):
    ctl.agents["B"].answers["remote_status"] = UNREACHABLE
    with pytest.raises(RemoteError) as err:
        await ctl.remote.issue_ticket("B")
    assert err.value.status == 502 and "reachability" in str(err.value)


# ---- power -----------------------------------------------------------------------------------
async def test_power_needs_the_exact_typed_confirmation(ctl):
    async with api(ctl) as c:
        for confirm in ("", "reboot b", "REBOOT A", "REBOOT B ", "POWEROFF B"):
            r = await c.post("/api/v1/remote/B/power", json={"action": "reboot", "confirm": confirm})
            assert r.status_code == 422 and "REBOOT B" in r.json()["detail"], confirm
    assert "remote_power" not in ctl.agents["B"].actions()
    assert not audits(ctl, "remote.reboot")


async def test_power_reports_what_the_operator_loses_and_is_audited(ctl):
    async with api(ctl) as c:
        r = await c.post("/api/v1/remote/B/power", json={"action": "reboot", "confirm": "REBOOT B", "delay_s": 7})
        assert r.status_code == 200
        body = r.json()
        assert body["scheduled"] == "reboot" and body["in_s"] == 7
        assert body["controller_goes_down"] is False and "autostart" in body
        r = await c.post("/api/v1/remote/A/power", json={"action": "poweroff", "confirm": "POWEROFF A"})
        assert r.json()["controller_goes_down"] is True, "A runs the controller: the GUI will go away"
    assert ctl.agents["B"].calls[-1] == ("remote_power", {"action": "reboot", "delay_s": 7})
    assert audits(ctl, "remote.reboot") and audits(ctl, "remote.poweroff")


async def test_power_refuses_a_busy_cluster_unless_forced(ctl):
    ctl.store.kv_set("maintenance", {"state": "running"})
    async with api(ctl) as c:
        r = await c.post("/api/v1/remote/B/power", json={"action": "reboot", "confirm": "REBOOT B"})
        assert r.status_code == 409 and "busy" in r.json()["detail"]
        assert "remote_power" not in ctl.agents["B"].actions()
        r = await c.post("/api/v1/remote/B/power", json={"action": "reboot", "confirm": "REBOOT B", "force": True})
        assert r.status_code == 200


@pytest.mark.parametrize("operation", ["power", "plug_cycle", "plug_on"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_remote_power_reserves_cluster_until_finished(ctl, monkeypatch, operation, outcome):
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise RemoteError("mock node unavailable", 502)
        return {} if operation == "power" else 200

    monkeypatch.setattr(ctl.remote, "_call", pending)
    monkeypatch.setattr(ctl.remote, "_plug_call", pending)
    monkeypatch.setattr(ctl.remote, "sleep", pending)
    request = (ctl.remote.power("B", "reboot", "REBOOT B") if operation == "power" else
               ctl.remote.plug("B", "cycle" if operation == "plug_cycle" else "on", "CUT POWER B"))
    task = asyncio.create_task(request)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert ctl.busy()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ctl._lock.acquire(), 0.02)
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if outcome == "failure":
                with pytest.raises(RemoteError, match="mock node unavailable"):
                    await task
            else:
                await task
        assert not ctl.busy(), "success, failure and cancellation must all release the reservation"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_forced_power_and_recovery_on_preserve_an_existing_reservation(ctl, monkeypatch):
    async def plug_ok(*args):
        return 200

    monkeypatch.setattr(ctl.remote, "_plug_call", plug_ok)
    await ctl._lock.acquire()
    try:
        await ctl.remote.power("B", "reboot", "REBOOT B", force=True)
        await ctl.remote.plug("B", "off", "CUT POWER B", force=True)
        await ctl.remote.plug("B", "on", "")
        assert ctl._lock.locked(), "recovery requests cannot release another operation's reservation"
    finally:
        ctl._lock.release()


async def test_remote_management_works_while_maintenance_has_the_cluster_reserved(ctl):
    """A failed update is exactly when you need a terminal; other changes stay blocked."""
    ctl.store.kv_set("maintenance", {"state": "failed"})
    async with api(ctl) as c:
        assert (await c.post("/api/v1/remote/terminal/ticket", json={"node": "B"})).status_code == 200
        assert (await c.post("/api/v1/remote/B/boot/next", json={"target": "network"})).status_code == 200
        blocked = await c.post("/api/v1/stop")
        assert blocked.status_code == 409 and "maintenance" in blocked.json()["detail"]


async def test_cancel_boot_and_wol_settings_are_audited(ctl):
    async with api(ctl) as c:
        assert (await c.post("/api/v1/remote/B/power/cancel")).json() == {"cancelled": ["reboot"]}
        assert (await c.post("/api/v1/remote/B/boot/next", json={"target": "0003"})).json() == {"next": "0003"}
        assert (await c.post("/api/v1/remote/B/boot/clear")).json() == {"cleared": True}
        r = await c.post("/api/v1/remote/B/wol/set", json={"iface": "enP7s7", "mode": "g"})
        assert r.json() == {"interface": "enP7s7", "mode": "g"}
        assert (await c.get("/api/v1/remote/B/boot")).json()["current"] == "0001"
    for a in ("remote.power_cancel", "remote.boot_next", "remote.boot_next_clear", "remote.wol_set"):
        assert audits(ctl, a), a


async def test_a_node_refusing_an_action_comes_back_as_a_readable_409(ctl):
    ctl.agents["B"].answers["remote_power"] = AgentActionError(
        "remote_power", "B", "reboot is not enabled on this node. On that node run: sudo tsm remote enable reboot")
    async with api(ctl) as c:
        r = await c.post("/api/v1/remote/B/power", json={"action": "reboot", "confirm": "REBOOT B"})
    assert r.status_code == 409 and "sudo tsm remote enable reboot" in r.json()["detail"]
    assert not audits(ctl, "remote.reboot"), "a refused action is not an audited power event"


# ---- Wake-on-LAN -----------------------------------------------------------------------------
def test_magic_packet_layout():
    pkt = magic_packet("AA:BB:CC:DD:EE:FF")
    assert len(pkt) == 102 and pkt[:6] == b"\xff" * 6 and pkt[6:] == bytes.fromhex("aabbccddeeff") * 16
    for bad in ("aa:bb", "zz:zz:zz:zz:zz:zz"):
        with pytest.raises(ValueError):
            magic_packet(bad)


async def test_wake_sends_the_packet_for_the_configured_mac(ctl):
    sent = []
    ctl.remote.send_wol = lambda *a: sent.append(a)
    async with api(ctl) as c:
        r = await c.post("/api/v1/remote/B/wake")
    assert r.status_code == 200 and r.json()["sent"] is True
    assert sent == [(MAC, "192.168.100.255", 9, None)]
    assert audits(ctl, "remote.wake")


async def test_wake_explains_what_is_missing(ctl):
    ctl.config.nodes["B"].wake = None
    with pytest.raises(RemoteError, match="no Wake-on-LAN settings"):
        await ctl.remote.wake("B")
    with pytest.raises(RemoteError, match="cannot wake itself"):
        await ctl.remote.wake("A")


async def test_wake_reports_a_send_failure_and_a_missing_interface(ctl):
    def boom(*a):
        raise OSError(101, "Network is unreachable")
    ctl.remote.send_wol = boom
    with pytest.raises(RemoteError, match="Network is unreachable") as err:
        await ctl.remote.wake("B")
    assert err.value.status == 502
    ctl.config.nodes["B"].wake = WakeSettings(mac=MAC, iface="doesnotexist0")
    with pytest.raises(RemoteError, match="no IPv4 address"):
        await ctl.remote.wake("B")


# ---- smart plug ------------------------------------------------------------------------------
@pytest.fixture
def plug_http(ctl):
    seen: list[httpx.Request] = []
    state = {"status": 200, "error": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if state["error"]:
            raise state["error"]
        return httpx.Response(state["status"], json={"POWER": "ON"})

    ctl.remote.http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pauses: list[float] = []

    async def fake_sleep(s):
        pauses.append(s)
    ctl.remote.sleep = fake_sleep
    return seen, state, pauses


async def test_plug_on_needs_no_confirmation_and_sends_the_secret_only_to_the_plug(ctl, plug_http):
    seen, _, _ = plug_http
    async with api(ctl) as c:
        r = await c.post("/api/v1/remote/B/plug", json={"action": "on"})
    assert r.status_code == 200 and r.json() == {"ok": True, "action": "on", "http": [200]}
    assert seen[0].headers["authorization"] == f"Bearer {PLUG_TOKEN}"
    entry = audits(ctl, "remote.plug_on")[0]
    assert PLUG_TOKEN not in entry.model_dump_json() and PLUG_TOKEN not in r.text


async def test_plug_off_and_cycle_need_the_typed_confirmation(ctl, plug_http):
    seen, _, pauses = plug_http
    async with api(ctl) as c:
        for action in ("off", "cycle"):
            r = await c.post("/api/v1/remote/B/plug", json={"action": action, "confirm": "yes"})
            assert r.status_code == 422 and "CUT POWER B" in r.json()["detail"]
        assert seen == []
        r = await c.post("/api/v1/remote/B/plug", json={"action": "cycle", "confirm": "CUT POWER B"})
    assert r.status_code == 200 and r.json()["http"] == [200, 200]
    assert [str(q.url).split("Power%20")[1] for q in seen] == ["Off", "On"]
    assert pauses == [3], "off and on are separated by the settle time so the PSU really drops"


async def test_plug_uses_a_single_cycle_request_when_the_plug_has_one(ctl, plug_http):
    seen, _, pauses = plug_http
    ctl.config.nodes["B"].plug.cycle = PlugRequest(url="http://plug.lan/cycle", method="POST", body="{}")
    r = await ctl.remote.plug("B", "cycle", "CUT POWER B")
    assert r["http"] == [200] and len(seen) == 1 and seen[0].method == "POST" and pauses == []


async def test_plug_refuses_off_and_cycle_on_a_busy_cluster_unless_forced(ctl, plug_http):
    seen, _, _ = plug_http
    ctl.store.kv_set("maintenance", {"state": "running"})
    with pytest.raises(RemoteError, match="busy"):
        await ctl.remote.plug("B", "off", "CUT POWER B")
    assert seen == []
    await ctl.remote.plug("B", "on", "")                       # switching on is never dangerous
    await ctl.remote.plug("B", "off", "CUT POWER B", force=True)


async def test_plug_failures_are_readable_and_do_not_leak_the_url_or_token(ctl, plug_http):
    _, state, _ = plug_http
    state["status"] = 500
    with pytest.raises(RemoteError, match="HTTP 500") as err:
        await ctl.remote.plug("B", "on", "")
    assert err.value.status == 502
    state["error"] = httpx.ConnectError("boom " + PLUG_TOKEN)
    with pytest.raises(RemoteError, match="did not answer") as err:
        await ctl.remote.plug("B", "on", "")
    assert PLUG_TOKEN not in str(err.value) and "plug.lan" not in str(err.value)


async def test_plug_without_config_or_token_says_what_to_do(ctl, plug_http):
    with pytest.raises(RemoteError, match="no smart plug configured"):
        await ctl.remote.plug("A", "on", "")
    SecretsVault(ctl.config.secrets_dir).dir.joinpath("plug_token.enc").unlink()
    with pytest.raises(RemoteError, match="tsm remote plug-token"):
        await ctl.remote.plug("B", "on", "")


# ---- reachability triage ---------------------------------------------------------------------
@pytest.fixture
def ports(monkeypatch):
    """Which TCP ports 'answer' in a reachability test."""
    open_ports: set[int] = set()

    async def fake(host, port, timeout=2.5):
        return port in open_ports

    monkeypatch.setattr(remote_mod, "tcp_open", fake)
    return open_ports


async def test_reach_ok_when_the_agent_answers(ctl, ports):
    ports.update({80, 22})
    r = await ctl.remote.reach("B")
    assert r["verdict"] == "ok" and r["agent_answers"] and r["ssh_port_open"] and not r["terminal_port_open"]


async def test_reach_blames_the_token_when_the_port_is_open_but_the_agent_refuses(ctl, ports):
    ports.update({80})
    ctl.agents["B"].answers["remote_status"] = AgentActionError("remote_status", "B", "invalid agent token", status=401)
    r = await ctl.remote.reach("B")
    assert r["verdict"] == "agent_error" and any("token" in s for s in r["steps"])


async def test_reach_tells_a_stopped_agent_from_a_dead_machine(ctl, ports):
    ports.update({22})
    r = await ctl.remote.reach("B")
    assert r["verdict"] == "agent_down"
    assert any("systemctl restart twinspark-agent" in s for s in r["steps"])
    ports.clear()
    r = await ctl.remote.reach("B")
    assert r["verdict"] == "host_down" and any("Wake-on-LAN" in s for s in r["steps"])
    assert any("smart plug" in s for s in r["steps"])


async def test_reach_notices_a_dead_qsfp_link(ctl, ports):
    ctl.agents["A"].answers["remote_status"] = status_answer(
        interfaces=[{"name": "enp1s0f1np1", "state": "down"}])
    r = await ctl.remote.reach("B")
    assert r["verdict"] == "link_down" and "enp1s0f1np1" in r["summary"]


def test_triage_is_honest_when_no_remote_power_is_configured():
    ep = type("Ep", (), {"wake": None, "plug": None})()
    verdict, summary, steps = _triage("B", False, False, False, None, ep, "", "A")
    assert verdict == "host_down" and any("No remote power is configured" in s for s in steps)
    verdict, _, steps = _triage("A", False, False, False, None, ep, "", "A")
    assert "runs the controller" in steps[0], "node A cannot really be down while it answers us"


# ---- the real path: browser -> controller -> node terminal -----------------------------------
pytestmark_linux = pytest.mark.skipif(sys.platform != "linux", reason="needs Linux PTYs")


@pytest.fixture
async def demo():
    async with DemoCluster(remote={"terminal": True}) as d:
        yield d


async def ticket(demo, node="B", **body):
    async with httpx.AsyncClient(base_url=demo.url, headers={"x-api-key": demo.key}, timeout=20) as c:
        r = await c.post("/api/v1/remote/terminal/ticket", json={"node": node, "cols": 100, "rows": 30, **body})
    assert r.status_code == 200, r.text
    return r.json()


def ws_url(demo, t) -> str:
    return f"ws://127.0.0.1:{demo.mgmt_port}{t['ws_path']}?ticket={t['ticket']}"


async def read_until(ws, needle: bytes, timeout=10.0) -> bytes:
    got = b""
    end = time.monotonic() + timeout
    while needle not in got:
        msg = await asyncio.wait_for(ws.recv(), max(0.1, end - time.monotonic()))
        if isinstance(msg, bytes):
            got += msg
    return got


@pytestmark_linux
async def test_terminal_session_end_to_end_with_audit_and_recording(demo):
    t = await ticket(demo, "B")
    async with connect(ws_url(demo, t), open_timeout=10) as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello" and hello["recorded"] is True and hello["cols"] == 100
        await ws.send(b"echo tsm$((6*7))end; stty size\n")
        out = await read_until(ws, b"tsm42end")
        assert b"tsm42end" in out
        await ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        await ws.send(json.dumps({"type": "evil", "cmd": "reboot"}))          # not forwarded, not fatal
        await ws.send(b"stty size\n")
        assert b"40 120" in await read_until(ws, b"40 120")
    async with httpx.AsyncClient(base_url=demo.url, headers={"x-api-key": demo.key}, timeout=20) as c:
        for _ in range(100):
            if audits(demo.controller, "remote.terminal_close"):
                break
            await asyncio.sleep(0.1)
        closed = audits(demo.controller, "remote.terminal_close")[0].detail
        assert closed["bytes_in"] > 20 and closed["bytes_out"] > 20 and closed["session"]
        assert closed["reason"] == "browser closed"
        assert audits(demo.controller, "remote.terminal_open")
        recs = []
        for _ in range(100):                       # the node finishes the recording after the socket closed
            recs = (await c.get("/api/v1/remote/B/recordings")).json()["recordings"]
            if recs:
                break
            await asyncio.sleep(0.1)
        assert recs, "a recording must exist for the session"
        cast = (await c.get(f"/api/v1/remote/B/recordings/{recs[0]['name']}")).json()["cast"]
        assert '"version": 2' in cast.splitlines()[0] and "tsm42end" in cast
        assert (await c.get("/api/v1/remote/B/recordings/..%2F..%2Fetc%2Fpasswd")).status_code in (404, 422)
    assert demo.controller.remote.bridges == 0
    assert demo.controller.remote.tickets == {}


@pytestmark_linux
async def test_terminal_on_the_controller_node_itself(demo):
    t = await ticket(demo, "A")
    async with connect(ws_url(demo, t), open_timeout=10) as ws:
        assert json.loads(await ws.recv())["type"] == "hello"
        await ws.send(b"echo a$((2+3))b\n")
        assert b"a5b" in await read_until(ws, b"a5b")


@pytestmark_linux
async def test_terminal_ws_refuses_everything_without_a_valid_one_time_ticket(demo):
    for url in (f"ws://127.0.0.1:{demo.mgmt_port}/api/v1/remote/terminal/ws",
                f"ws://127.0.0.1:{demo.mgmt_port}/api/v1/remote/terminal/ws?ticket=guess",
                f"ws://127.0.0.1:{demo.mgmt_port}/api/v1/remote/terminal/ws?ticket={demo.key}"):
        with pytest.raises(InvalidStatus) as err:
            async with connect(url, open_timeout=5):
                pass
        assert err.value.response.status_code == 403
    t = await ticket(demo, "B")
    async with connect(ws_url(demo, t), open_timeout=10) as ws:
        assert json.loads(await ws.recv())["type"] == "hello"
    with pytest.raises(InvalidStatus):                  # replay
        async with connect(ws_url(demo, t), open_timeout=5):
            pass


@pytestmark_linux
async def test_terminal_ws_checks_the_origin_and_burns_the_ticket(demo):
    t = await ticket(demo, "B")
    with pytest.raises(InvalidStatus) as err:
        async with connect(ws_url(demo, t), open_timeout=5, additional_headers={"Origin": "http://evil.example"}):
            pass
    assert err.value.response.status_code == 403
    with pytest.raises(InvalidStatus):                  # the same ticket does not work for the right origin now
        async with connect(ws_url(demo, t), open_timeout=5,
                           additional_headers={"Origin": f"http://127.0.0.1:{demo.mgmt_port}"}):
            pass
    t = await ticket(demo, "B")
    async with connect(ws_url(demo, t), open_timeout=10,
                       additional_headers={"Origin": f"http://127.0.0.1:{demo.mgmt_port}"}) as ws:
        assert json.loads(await ws.recv())["type"] == "hello"


@pytestmark_linux
async def test_terminal_bridge_cap(demo):
    t = await ticket(demo, "B")
    demo.controller.remote.bridges = remote_mod.MAX_BRIDGES
    with pytest.raises(InvalidStatus):
        async with connect(ws_url(demo, t), open_timeout=5):
            pass
    demo.controller.remote.bridges = 0


@pytestmark_linux
async def test_node_ending_the_shell_ends_the_bridge_with_the_reason(demo):
    t = await ticket(demo, "B")
    async with connect(ws_url(demo, t), open_timeout=10) as ws:
        await ws.recv()
        await ws.send(b"exit\n")
        frames = []
        with pytest.raises(ConnectionClosed):
            while True:
                frames.append(await asyncio.wait_for(ws.recv(), 10))
    assert any(isinstance(f, str) and '"exit"' in f for f in frames)
    for _ in range(100):
        if audits(demo.controller, "remote.terminal_close"):
            break
        await asyncio.sleep(0.1)
    assert demo.controller.remote.bridges == 0


@pytestmark_linux
async def test_stopping_the_policy_stops_new_sessions_immediately(demo):
    from twinspark.remote.policy import write_policy
    write_policy(demo.b_layout.etc / "remote-policy.json", {"terminal": False}, chown_root=False)
    async with httpx.AsyncClient(base_url=demo.url, headers={"x-api-key": demo.key}, timeout=20) as c:
        r = await c.post("/api/v1/remote/terminal/ticket", json={"node": "B"})
    assert r.status_code == 409 and "sudo tsm remote enable terminal" in r.json()["detail"]


async def test_cluster_without_remote_features_has_everything_off_and_says_how_to_turn_it_on():
    async with DemoCluster() as d:
        async with httpx.AsyncClient(base_url=d.url, headers={"x-api-key": d.key}, timeout=20) as c:
            ov = (await c.get("/api/v1/remote/overview")).json()
            assert ov["nodes"]["B"]["enabled"] == [] and ov["nodes"]["B"]["reachable"]
            r = await c.post("/api/v1/remote/terminal/ticket", json={"node": "B"})
            assert r.status_code == 409 and "sudo tsm remote enable terminal" in r.json()["detail"]
            r = await c.post("/api/v1/remote/B/power", json={"action": "reboot", "confirm": "REBOOT B"})
            assert r.status_code == 409           # the node (no helper, nothing enabled) refuses, readably
            assert (await c.get("/api/v1/remote/B/reach")).json()["verdict"] == "ok"
