"""The client's own routes for work done on the workstation: the Hermes lifecycle and recipe conversion.

Hermes is the fake install from test_client_hermes (a fake HOME, a script launcher), its model endpoint
the in-process FakeGateway. Recipe conversion runs against the in-process demo cluster as node A.
Nothing touches the real home directory, a real Hermes or the network.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from tests.test_client_hermes import MGMT_KEY, kill_fake_gateways, make_install
from tests.test_client_validate import BASE, KEY, FakeGateway
from twinspark.client import integrations_local
from twinspark.client.adapters import Endpoint, HermesAdapter
from twinspark.client.adapters import validate as validate_mod
from twinspark.client.app import create_client_app
from twinspark.client.connections import Connection
from twinspark.client.session import ClientSession
from twinspark.demo import DemoCluster

LOCAL = "local-session-key-0123456789"
H = {"x-api-key": LOCAL}


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18765",
                             headers=H, timeout=60)


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    """A fake Hermes in a fake home, the client's adapter wired to a fake gateway, a 'connected' session."""
    monkeypatch.setenv("TSM_CLIENT_KEYRING", "off")
    inst = make_install(tmp_path, stale_wrapper=False, wrapper_ok=True)
    # LocalHost() (what the route builds) reads this process's environment: point it at the fake home
    monkeypatch.setenv("HOME", str(inst.home))
    monkeypatch.setenv("PATH", f"{inst.wrapper.parent}:/usr/bin:/bin")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    gw = FakeGateway()

    class WiredAdapter(HermesAdapter):
        def __init__(self, host, profile=None, **kw):
            super().__init__(host, profile, http=gw.client(), start_wait=5.0, stop_grace=3.0, **kw)

    async def endpoint_for(session, host, alias, for_app=False):
        return Endpoint(base_url=BASE, alias=alias or "glm", api_key=KEY, context_length=131072,
                        revision_id="rev-1", served_model="org/glm-model", source="workstation-forward")

    async def validate_endpoint(base_url, api_key, alias, **kw):
        return await validate_mod.validate_endpoint(base_url, api_key, alias, http=gw.client(), **kw)

    monkeypatch.setattr(integrations_local, "HermesAdapter", WiredAdapter)
    monkeypatch.setattr(integrations_local, "endpoint_for", endpoint_for)
    monkeypatch.setattr(integrations_local, "validate_endpoint", validate_endpoint)
    session = ClientSession(home=tmp_path / "client")
    conn = session.connections.add(Connection(id="", name="lab", mode="direct", host="127.0.0.1",
                                              direct_url="http://127.0.0.1:9"))
    session.creds.set(conn.id, "management_key", MGMT_KEY)
    session.creds.set(conn.id, "inference_key", KEY)
    session.current, session.phase = conn, "connected"
    yield inst, session
    kill_fake_gateways(tmp_path)


async def test_hermes_on_this_computer_detect_preview_apply_validate_restore(hermes):
    inst, session = hermes
    original = (inst.hermes / "config.yaml").read_bytes()
    async with client_for(create_client_app(session, LOCAL)) as c:
        det = (await c.post("/api/v1/client/integrations/hermes/detect", json={})).json()
        assert det["state"] == "installed" and det["host"] == "this computer"
        st = (await c.post("/api/v1/client/integrations/hermes/status", json={})).json()
        assert st["status"]["state"]["phase"] == "not-configured" and st["endpoint"]["alias"] == "glm"

        pv = (await c.post("/api/v1/client/integrations/hermes/preview", json={})).json()["preview"]
        assert pv["token"] and any(f["changed"] for f in pv["files"])
        r = await c.post("/api/v1/client/integrations/hermes/apply", json={})
        assert r.status_code == 422 and "token" in r.json()["detail"]          # never without a reviewed preview
        r = await c.post("/api/v1/client/integrations/hermes/apply", json={"token": pv["token"]})
        assert r.status_code == 200 and r.json()["changed"] is True
        again = await c.post("/api/v1/client/integrations/hermes/apply", json={"token": pv["token"]})
        assert again.status_code == 409                                        # the reviewed preview is spent

        cfg = yaml.safe_load((inst.hermes / "config.yaml").read_text())
        assert cfg["model"]["base_url"] == BASE and cfg["model"]["default"] == "glm"
        assert cfg["model"]["context_length"] == 131072
        written = (inst.hermes / "config.yaml").read_text() + (inst.hermes / ".env").read_text()
        assert KEY in written and MGMT_KEY not in written                      # only the inference key

        checks = (await c.post("/api/v1/client/integrations/hermes/validate", json={})).json()["checks"]
        assert checks and all(ch["status"] != "fail" for ch in checks), checks
        st = (await c.post("/api/v1/client/integrations/hermes/status", json={})).json()["status"]
        assert st["state"]["configured"] and st["state"]["compatible"] and st["state"]["phase"] == "ready"

        cli = (await c.post("/api/v1/client/integrations/hermes/launch", json={"mode": "cli"})).json()
        assert cli["action"] == "command" and cli["command"][-1] == "chat"

        backups = (await c.post("/api/v1/client/integrations/hermes/backups", json={})).json()["backups"]
        assert backups and backups[0]["kind"] == "apply"
        r = await c.post("/api/v1/client/integrations/hermes/restore", json={"backup_id": backups[0]["id"]})
        assert r.status_code == 200
        assert (inst.hermes / "config.yaml").read_bytes() == original

        test = (await c.post("/api/v1/client/integrations/test", json={"alias": "glm"})).json()
        assert test["endpoint"]["base_url"] == BASE and all(ch["status"] != "fail" for ch in test["checks"])


async def test_hermes_route_inputs_are_checked(hermes):
    _, session = hermes
    async with client_for(create_client_app(session, LOCAL)) as c:
        r = await c.post("/api/v1/client/integrations/hermes/detect", json={"host": "node-a"})
        assert r.status_code == 422 and "SSH connection" in r.json()["detail"]    # a direct connection has no SSH
        r = await c.post("/api/v1/client/integrations/hermes/detect", json={"profile": "../etc"})
        assert r.status_code == 422
        r = await c.post("/api/v1/client/integrations/hermes/launch", json={"mode": "daemon"})
        assert r.status_code == 422
        r = await c.post("/api/v1/client/integrations/hermes/restore", json={"backup_id": "nope"})
        assert r.status_code == 404
        session.phase = "idle"
        r = await c.post("/api/v1/client/integrations/hermes/preview", json={})
        assert r.status_code == 503 and r.headers["x-twinspark-client"] == "transport"


async def test_recipe_conversion_runs_here_and_only_the_draft_reaches_node_a(tmp_path, monkeypatch):
    monkeypatch.setenv("TSM_CLIENT_KEYRING", "off")
    text = (Path(__file__).resolve().parent.parent / "twinspark/cookbook/recipes/deepseek-v4-flash-0731-b12x.yaml"
            ).read_text()
    async with DemoCluster() as demo:
        session = ClientSession(home=tmp_path / "client")
        conn = session.connections.add(Connection(id="", name="lab", mode="direct", host="127.0.0.1",
                                                  direct_url=demo.url, gateway_port=demo.gateway_port))
        session.creds.set(conn.id, "management_key", demo.key)
        async with client_for(create_client_app(session, LOCAL)) as c:
            assert (await c.post(f"/api/v1/client/connections/{conn.id}/connect",
                                 params={"wait": "true"})).json()["phase"] == "connected"
            preview = {"text": text, "preview": True, "profile_name": "ds"}
            r = await c.post("/api/v1/cookbook/import-recipe", json=preview)
            assert r.status_code == 200 and r.json()["preview"] and r.json()["exists"] is False
            assert (await c.get("/api/v1/profiles/ds")).status_code == 404            # a preview stores nothing
            r = await c.post("/api/v1/cookbook/import-recipe", json={"text": text, "profile_name": "ds"})
            assert r.status_code == 200 and r.json()["profile"]["name"] == "ds"
            assert (await c.get("/api/v1/profiles/ds")).status_code == 200             # stored on node A
            r = await c.post("/api/v1/cookbook/import-recipe", json=preview)
            assert r.json()["exists"] is True
            r = await c.post("/api/v1/cookbook/import-recipe", json={"text": "a: ["})
            assert r.status_code == 422
            r = await c.post("/api/v1/cookbook/import-recipe", json={"text": text, "url": "https://x/y.yaml"})
            assert r.status_code == 422 and "exactly one" in r.json()["detail"]
            r = await c.post("/api/v1/cookbook/import-recipe", json={"url": "http://x/y.yaml"})
            assert r.status_code == 422 and "https" in r.json()["detail"]
        await session.aclose()


async def test_hermes_on_node_a_uses_the_gateway_as_node_a_reaches_it(tmp_path, monkeypatch):
    monkeypatch.setenv("TSM_CLIENT_KEYRING", "off")
    session = ClientSession(home=tmp_path / "client")

    async def models():
        return [{"id": "glm", "max_model_len": 131072, "twinspark": {"revision_id": "r1"}}]
    session.models = models
    session.node_cfg = {"gateway_port": 8000}
    ep = await integrations_local.endpoint_for(session, "node-a", None)
    assert ep.base_url == "http://127.0.0.1:8000/v1" and ep.alias == "glm" and ep.context_length == 131072
    session.node_cfg = {"gateway_port": 8443, "gateway_tls": True, "gateway_forward_host": "10.0.0.5"}
    assert (await integrations_local.endpoint_for(session, "node-a", "glm")).base_url == "https://10.0.0.5:8443/v1"
    session.node_cfg = {"gateway_port": 8000, "gateway_forward_host": "fd00::5"}
    assert (await integrations_local.endpoint_for(session, "node-a", "glm")).base_url == "http://[fd00::5]:8000/v1"


async def test_hermes_gets_a_key_of_its_own_and_a_new_one_after_a_revoke(tmp_path, monkeypatch):
    """The client asks node A for "Hermes on <computer>" once, keeps it, and replaces it when revoked."""
    from twinspark.client import integrations_local
    keys, created = [], []

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    async def node_request(method, path, **kw):
        if method == "GET":
            return Resp(200, [{"id": k["id"], "state": k["state"]} for k in keys])
        created.append(kw["json"])
        k = {"id": f"key-{len(created)}", "state": "active", "key": f"tsk_secret{len(created)}"}
        keys.append(k)
        return Resp(201, {"id": k["id"], "key": k["key"], "name": kw["json"]["name"]})
    session = ClientSession(home=tmp_path / "client")
    conn = session.connections.add(Connection(id="", name="lab", mode="direct", host="127.0.0.1",
                                              direct_url="http://127.0.0.1:9"))
    session.creds.set(conn.id, "inference_key", "shared-key")
    session.current = conn
    monkeypatch.setattr(session, "node_request", node_request)
    first = await integrations_local.app_key(session, "workstation")
    again = await integrations_local.app_key(session, "workstation")
    assert first == again == "tsk_secret1" and len(created) == 1
    assert created[0]["app"] == "hermes" and created[0]["name"].startswith("Hermes on ")
    keys[0]["state"] = "revoked"
    assert await integrations_local.app_key(session, "workstation") == "tsk_secret2"
    node = await integrations_local.app_key(session, "node-a")
    assert node == "tsk_secret3" and created[-1]["name"] == "Hermes on node A"

    async def old_node(method, path, **kw):
        return Resp(404, {"detail": "Not Found"})
    monkeypatch.setattr(session, "node_request", old_node)
    assert await integrations_local.app_key(session, "workstation") == "shared-key"
