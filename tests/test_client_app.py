"""The workstation client end to end: GUI auth, the connection flow, the proxy to node A, local recipe work.

Node A is the in-process demo cluster (dry-run). The SSH test uses the fake ssh from
test_client_ssh_fake.py, which really forwards TCP, so requests travel client app -> fake ssh -L ->
the demo's management API and gateway, like they would to a Spark.
"""

from __future__ import annotations

import httpx
import pytest

from twinspark.client import ssh
from twinspark.client.app import create_client_app
from twinspark.client.connections import Connection
from twinspark.client.session import ClientSession, compatibility
from twinspark.demo import DemoCluster
from twinspark.security import SecretsVault

from .test_client_ssh_fake import HOSTKEY, HOSTKEY_FP, FakeSSH, needs_posix

KEY = "local-session-key-0123456789"


def client_for(app, host="127.0.0.1:18765", **headers) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}",
                             headers={"x-api-key": KEY, **headers}, timeout=30)


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("TSM_CLIENT_KEYRING", "off")
    return ClientSession(home=tmp_path / "client")


async def test_the_local_gui_only_answers_its_own_key_host_and_origin(session):
    app = create_client_app(session, KEY)
    async with client_for(app) as c:
        assert (await c.get("/api/v1/client/info")).status_code == 200
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:1") as anon:
            r = await anon.get("/api/v1/client/info")
            assert r.status_code == 401 and "session key" in r.json()["detail"]
        # DNS rebinding: a page whose own name resolves to 127.0.0.1 sends that name in Host
        async with client_for(app, host="evil.example:18765") as rebound:
            assert (await rebound.get("/api/v1/client/info")).status_code == 403
        r = await c.post("/api/v1/client/disconnect", headers={"origin": "https://evil.example"})
        assert r.status_code == 403
        r = await c.post("/api/v1/client/disconnect", headers={"origin": "http://127.0.0.1:18765"})
        assert r.status_code == 200
        # not connected: node routes answer 503 marked as a transport problem, not a node error
        r = await c.get("/api/v1/status")
        assert r.status_code == 503 and r.headers["x-twinspark-client"] == "transport"
        assert "not connected" in r.json()["detail"]


def test_version_compatibility_is_explained():
    assert compatibility("0.6.0", "0.6.3", [])["state"] == "ok"
    old = compatibility("0.6.0", "0.5.1", [])
    assert old["state"] == "older-node" and "update them" in old["note"]
    assert compatibility("0.6.0", "0.7.0", [])["state"] == "newer-node"
    assert compatibility("0.6.0", None, [])["state"] == "unknown"


async def test_a_direct_connection_proxies_node_a_and_keeps_recipe_work_local(session):
    async with DemoCluster() as demo:
        inference = SecretsVault(demo.a_layout.secrets).get("inference_api_key")
        conn = session.connections.add(Connection(id="", name="lab", mode="direct", host="127.0.0.1",
                                                  ssh_user="", direct_url=demo.url,
                                                  gateway_port=demo.gateway_port))
        app = create_client_app(session, KEY)
        async with client_for(app) as c:
            st = (await c.post(f"/api/v1/client/connections/{conn.id}/connect", params={"wait": "true"})).json()
            assert st["phase"] == "needs-action" and st["need"] == "keys"          # direct mode: paste keys
            r = await c.put(f"/api/v1/client/connections/{conn.id}/credentials",
                            json={"management_key": "wrong-key-123456", "inference_key": inference})
            assert r.json()["management_key"] is True
            st = (await c.post(f"/api/v1/client/connections/{conn.id}/connect", params={"wait": "true"})).json()
            assert st["need"] == "keys" and "refused the stored management key" in st["message"]
            await c.put(f"/api/v1/client/connections/{conn.id}/credentials", json={"management_key": demo.key})
            st = (await c.post(f"/api/v1/client/connections/{conn.id}/connect", params={"wait": "true"})).json()
            assert st["phase"] == "connected" and st["states"]["client_connected"] is True
            assert st["states"]["agents_compatible"] is True and st["states"]["model_ready"] is False
            assert st["node"]["compat"]["state"] == "ok" and "startup-timeline" in st["node"]["capabilities"]
            assert set(st["node"]["agents"]) == {"A", "B"}
            # node A's API through the client, with node A's key added by the client
            r = await c.get("/api/v1/status")
            assert r.status_code == 200 and "nodes" in r.json()
            # recipe browsing is answered here, from the package's recipes and node A's profiles
            idx = (await c.get("/api/v1/cookbook")).json()
            assert idx["where"] == "client" and idx["recipes"]
            name = idx["recipes"][0]["name"]
            r = await c.post(f"/api/v1/cookbook/import/{name}", params={"profile_name": "from-client"})
            assert r.status_code == 200 and r.json()["profile"]["name"] == "from-client"
            assert (await c.get("/api/v1/profiles/from-client")).status_code == 200      # stored on node A
            assert "from-client" in [p["name"] for p in (await c.get("/api/v1/profiles?summary=true")).json()]
            again = await c.post(f"/api/v1/cookbook/import/{name}", params={"profile_name": "from-client"})
            assert again.status_code == 409
            ep = (await c.get("/api/v1/client/endpoint", params={"reveal": "true"})).json()
            assert ep["base_url"] == f"http://127.0.0.1:{demo.gateway_port}/v1" and ep["api_key"] == inference
            await c.post("/api/v1/client/disconnect")
            r = await c.get("/api/v1/status")
            assert r.status_code == 503 and r.headers["x-twinspark-client"] == "transport"


@needs_posix
async def test_ssh_connection_end_to_end_through_a_supervised_tunnel(session, tmp_path, monkeypatch):
    fake = FakeSSH(tmp_path, monkeypatch)
    async with DemoCluster() as demo:
        inference = SecretsVault(demo.a_layout.secrets).get("inference_api_key")
        controller_yaml = (f"node: {{node_id: A, role: controller}}\n"
                           f"listener: {{bind: 127.0.0.1, port: {demo.mgmt_port}}}\n"
                           f"gateway_listener: {{bind: 127.0.0.1, port: {demo.gateway_port}}}\n"
                           "nodes:\n  A: {agent_url: 'http://127.0.0.1:9443'}\n"
                           "  B: {agent_url: 'http://192.168.100.2:9443', qsfp_ip: 192.168.100.2}\n"
                           "recipe_sources: ['example/recipes:dir']\n")
        fake.configure(auth="any", canned=[
            {"match": "/etc/twinspark/controller.yaml", "rc": 0, "stdout": controller_yaml},
            {"match": "tsm-show-keys", "rc": 0,
             "stdout": f"management_api_key   {demo.key}\ninference_api_key    {inference}\n"},
        ])
        app = create_client_app(session, KEY)
        async with client_for(app) as c:
            conn = (await c.post("/api/v1/client/connections",
                                 json={"name": "home", "host": "spark-a.lan", "ssh_user": "spark"})).json()
            assert conn["mode"] == "ssh"
            st = (await c.post(f"/api/v1/client/connections/{conn['id']}/connect", params={"wait": "true"})).json()
            assert st["need"] == "host-key"                       # never connects to an unconfirmed host
            scan = (await c.post(f"/api/v1/client/connections/{conn['id']}/hostkey/scan")).json()
            assert scan["status"] == "unknown" and scan["keys"][0]["fingerprint"] == HOSTKEY_FP
            r = await c.post(f"/api/v1/client/connections/{conn['id']}/hostkey/trust",
                             json={"fingerprints": ["SHA256:somethingelse"]})
            assert r.status_code == 409                           # only what the user confirmed is trusted
            r = await c.post(f"/api/v1/client/connections/{conn['id']}/hostkey/trust",
                             json={"fingerprints": [HOSTKEY_FP]})
            assert r.json()["trusted"] == 1
            st = (await c.post(f"/api/v1/client/connections/{conn['id']}/connect", params={"wait": "true"})).json()
            assert st["phase"] == "connected", st
            assert st["transport"]["state"] == "up" and st["credentials"]["management_key"] is True
            fwd = {f["label"]: f for f in st["transport"]["forwards"]}
            assert fwd["api"]["remote_port"] == demo.mgmt_port and fwd["gateway"]["remote_port"] == demo.gateway_port
            assert st["endpoint"]["base_url"] == f"http://127.0.0.1:{fwd['gateway']['local_port']}/v1"
            saved = session.connections.get(conn["id"])
            assert saved.local_gateway_port == fwd["gateway"]["local_port"]       # stable for apps' configs
            r = await c.get("/api/v1/jobs")                       # through the client and the tunnel
            assert r.status_code == 200
            assert (await c.get("/api/v1/cookbook")).json()["sources"] == ["example/recipes:dir"]
            models = await session.models()                       # the gateway forward, as Hermes would use it
            assert isinstance(models, list)
            assert any("-L" in call["argv"] for call in fake.calls())
            await c.post("/api/v1/client/disconnect")
            assert session.tunnel is None
        assert ssh.known_keys(session.home, "spark-a.lan", 22)[0].fingerprint == HOSTKEY_FP


@needs_posix
async def test_a_refused_login_offers_the_one_time_key_install(session, tmp_path, monkeypatch):
    fake = FakeSSH(tmp_path, monkeypatch)
    fake.configure(auth="deny")
    conn = session.connections.add(Connection(id="", name="home", mode="ssh", host="spark-a.lan", ssh_user="spark"))
    ssh.trust_host_keys(session.home, "spark-a.lan", 22, [ssh.HostKey.parse(*HOSTKEY.split()[:2])])
    st = await session.connect(conn.id)
    assert st["phase"] == "needs-action" and st["need"] == "auth"


async def test_the_terminal_socket_checks_the_browser_then_bridges_to_node_a(session):
    """The browser terminal's WebSocket: a foreign Origin or Host is refused, no connection closes it, and a
    connected client relays both ways to node A's socket without passing the browser's Origin on."""
    import asyncio

    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    from twinspark.client.ssh import free_local_port

    seen: dict = {}

    async def node_terminal(ws):
        seen["origin"], seen["query"] = ws.headers.get("origin"), ws.url.query
        await ws.accept()
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            await ws.send_text("echo:" + (msg.get("text") or msg.get("bytes", b"").decode()))

    node = Starlette(routes=[WebSocketRoute("/api/v1/remote/terminal/ws", node_terminal)])
    node_port, client_port = free_local_port(19100), free_local_port(19200)
    servers = [uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=p, log_level="warning", loop="none"))
               for a, p in ((node, node_port), (create_client_app(session, KEY), client_port))]
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    try:
        while not all(s.started for s in servers):
            await asyncio.sleep(0.05)
        url = f"ws://127.0.0.1:{client_port}/api/v1/remote/terminal/ws?ticket=t-123"
        own = f"http://127.0.0.1:{client_port}"
        # refused before the handshake completes (the browser sees a socket that never opened)
        for kw in ({"origin": "https://evil.example"},                  # another web page
                   {"additional_headers": {"Host": "evil.example"}, "origin": own},   # DNS rebinding
                   {"origin": own}):                                    # fine, but no Spark connected yet
            with pytest.raises(InvalidStatus) as refused:
                async with connect(url, **kw):
                    pass
            assert refused.value.response.status_code == 403
        conn = session.connections.add(Connection(id="", name="lab", mode="direct", host="127.0.0.1",
                                                  direct_url=f"http://127.0.0.1:{node_port}"))
        session.current, session.phase = conn, "connected"
        async with connect(url, origin=own) as ws:
            await ws.send("ls")
            assert await asyncio.wait_for(ws.recv(), 5) == "echo:ls"
        assert seen == {"origin": None, "query": "ticket=t-123"}
    finally:
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
