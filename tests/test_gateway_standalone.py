"""The standalone gateway (master plan §3.2): its own process, fed by a state file, answering on a Unix
socket. Here both sides run in the test's event loop; the backend is a real loopback server so streams
behave as over the network."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from twinspark.client.ssh import free_local_port
from twinspark.controller.keys import digest
from twinspark.gateway.app import build_gateway_app
from twinspark.gateway.gateway import Gateway
from twinspark.gateway.standalone import control_app, read_config
from twinspark.gateway.state import GatewayLink, StateFollower, apply_state, export_state, read_state, write_atomic

KEY = "tsk_test-key"


@pytest.fixture
def backend():
    """Streams 20 chunks, 50 ms apart, then a usage chunk."""
    async def chat(request):
        async def gen():
            for i in range(20):
                yield f'data: {{"choices":[{{"delta":{{"content":"{i}"}}}}]}}\n\n'.encode()
                await asyncio.sleep(0.05)
            yield b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":20}}\n\n'
            yield b"data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    port = free_local_port(19700)
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"])]),
                                           host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(10)


class Process:
    """The gateway process: its Gateway, the state follower and the control socket."""

    def __init__(self, state: Path, sock: Path):
        self.gw = Gateway(None, "backend-secret")
        self.follower = StateFollower(self.gw, state, interval=0.05)
        self.sock = sock
        self.server = uvicorn.Server(uvicorn.Config(control_app(self.gw, self.follower), uds=str(sock),
                                                    log_level="warning"))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(self.gw)),
                                        base_url="http://gw", headers={"authorization": f"Bearer {KEY}"}, timeout=30)

    async def __aenter__(self):
        self.tasks = [asyncio.create_task(self.follower.run()), asyncio.create_task(self.server.serve())]
        while not self.server.started:
            await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        self.tasks[0].cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()
        await self.gw.aclose()


@pytest.fixture
def paths():
    d = Path(tempfile.mkdtemp(prefix="tsg"))          # a short path: Unix socket names are limited
    return d / "gateway-state.json", d / "control.sock"


def controller_side(paths) -> tuple[Gateway, GatewayLink]:
    gw = Gateway("legacy", "backend-secret")
    gw.set_keys([{"id": "key-1", "sha256": digest(KEY), "aliases": []}])
    link = GatewayLink(gw, *paths, interval=0.05)
    link.attach()
    return gw, link


async def until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_a_controller_restart_does_not_cut_a_running_stream(paths, backend):
    gw, link = controller_side(paths)
    gw.set_route("default", [backend], "m", "rev-1")
    async with Process(*paths) as proc:
        await until(lambda: "default" in proc.gw.routes)
        stream = asyncio.create_task(proc.client.post("/v1/chat/completions",
                                                      json={"model": "default", "stream": True}))
        await until(lambda: proc.gw.routes["default"].inflight == 1)
        # the controller stops and starts again: a new process with an empty gateway object
        await link.stop()
        del gw, link
        gw2, link2 = controller_side(paths)
        assert gw2.routes["default"].backends == [backend]          # it took over what is being served
        r = await stream
        assert r.status_code == 200 and r.text.count("data: ") == 22 and r.text.endswith("[DONE]\n\n")
        assert await link2.poll() and gw2.remote_version == gw2.published_version
        (row,) = [r for r in gw2.usage.values()]
        assert row["completion_tokens"] == 20 and row["prompt_tokens"] == 4      # counted, and handed over
        assert gw2.last_used.get("key-1")


async def test_a_drain_waits_until_the_gateway_applied_it_and_its_requests_ended(paths, backend):
    gw, link = controller_side(paths)
    gw.set_route("default", [backend], "m", "rev-1")
    async with Process(*paths) as proc:
        await until(lambda: "default" in proc.gw.routes)
        await link.poll()
        stream = asyncio.create_task(proc.client.post("/v1/chat/completions",
                                                      json={"model": "default", "stream": True}))
        await until(lambda: proc.gw.routes["default"].inflight == 1)
        gw.begin_drain("default")
        # published, not yet applied by the gateway: counted as busy even though nothing is known yet
        assert gw.inflight(["default"])[0] >= 1
        await until(lambda: proc.gw.routes["default"].draining)
        await link.poll()
        busy, oldest = gw.inflight(["default"])
        assert busy == 1 and oldest is not None and gw.routes["default"].status == "switching"
        r = await proc.client.post("/v1/chat/completions", json={"model": "default"})
        assert r.status_code == 503                                    # new requests wait for the switch
        assert (await stream).status_code == 200
        await link.poll()
        assert gw.inflight(["default"]) == (0, None)
        assert await gw.wait_idle("default", 1)


async def test_without_an_answer_from_the_gateway_nothing_is_in_flight(paths):
    gw, link = controller_side(paths)
    gw.set_route("default", ["http://127.0.0.1:9"], "m", "rev-1")
    assert not await link.poll()                                       # no process listening
    assert gw.inflight(["default"]) == (0, None) and link.status()["reachable"] is False
    assert "does not answer" in link.status()["error"]


def test_the_state_is_written_atomically_and_applied_in_one_step(paths, tmp_path):
    state, _ = paths
    gw = Gateway(None, None)
    gw.set_route("a", ["http://x"], "ma", "r1")
    gw.set_route("b", ["http://y"], "mb", "r1")
    gw.begin_drain("b")
    write_atomic(state, export_state(gw, 7))
    assert [p.name for p in state.parent.iterdir()] == [state.name]   # no temporary file left behind
    assert oct(state.stat().st_mode & 0o777) == "0o640"
    proc = Gateway(None, None)
    keep = proc.routes.setdefault("a", type(gw.routes["a"])("a"))
    keep.requests = 41
    follower = StateFollower(proc, state)
    assert follower.check() and follower.version == 7
    assert proc.routes["a"] is keep and keep.requests == 41 and proc.routes["b"].draining
    assert not follower.check()                                        # unchanged: nothing to do
    state.write_text('{"format": "twinspark-gateway/1", "version": 8, "routes": {')   # torn by hand
    assert not follower.check() and follower.version == 7 and set(proc.routes) == {"a", "b"}
    doc = export_state(gw, 9)
    del doc["routes"]["a"]
    write_atomic(state, doc)
    assert follower.check() and set(proc.routes) == {"b"}
    assert read_state(tmp_path / "missing.json") is None


def test_the_process_reads_only_what_it_needs_from_controller_yaml(tmp_path):
    cfg = tmp_path / "controller.yaml"
    cfg.write_text("node: {node_id: A, role: controller}\ngateway_listener: {bind: 127.0.0.1, port: 18000}\n"
                   "gateway_state_path: /x/state.json\nsecrets_dir: /x/secrets\n")
    c = read_config(str(cfg))
    assert (c["bind"], c["port"], c["state_path"], c["secrets_dir"]) == ("127.0.0.1", 18000, "/x/state.json",
                                                                         "/x/secrets")
    assert c["control_socket"] == "/run/twinspark-gateway/control.sock"


def test_keys_and_settings_travel_in_the_state_but_never_a_secret(paths):
    state, _ = paths
    gw = Gateway("legacy-secret", "backend-secret")
    gw.set_keys([{"id": "key-1", "sha256": digest(KEY), "aliases": ["coder"]}])
    gw.configure({"weights": {"A": 2}, "fallbacks": {"default": {"alias": "coder"}}}, fallback_api_key="ext-secret")
    write_atomic(state, export_state(gw, 1))
    text = state.read_text()
    for secret in ("legacy-secret", "backend-secret", "ext-secret", KEY):
        assert secret not in text
    proc = Gateway(None, None)
    apply_state(proc, json.loads(text))
    assert proc.authenticate(f"Bearer {KEY}")["aliases"] == ["coder"]
    assert proc.settings["weights"] == {"A": 2} and proc.settings["fallbacks"]["default"] == {"alias": "coder"}


def test_a_standalone_install_publishes_instead_of_serving_the_gateway(tmp_path):
    from twinspark import serve
    from twinspark.provision import Layout, Provisioner

    from .test_setup_flow import answers_a
    lay = Layout(tmp_path / "root")
    Provisioner(answers_a(tmp_path), lay, systemd=False).apply()
    controller, mgmt, gw_server = serve.build_servers(str(lay.controller_yaml))
    try:
        assert gw_server is None and controller.gateway_link is not None
        doc = read_state(Path(controller.config.gateway_state_path))
        assert doc["version"] >= 1 and [k["id"] for k in doc["keys"]] == ["default"]   # the migrated key
        assert controller.status()["gateway_process"]["mode"] == "standalone"
    finally:
        asyncio.run(controller.aclose())
