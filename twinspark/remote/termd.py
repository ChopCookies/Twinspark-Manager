"""``tsm-termd``: the terminal service, one small daemon per node.

It is a separate process (and systemd unit) on purpose. The agent runs under strict sandboxing —
read-only filesystem, no privilege escalation — which is right for the agent and useless for a
troubleshooting shell. The terminal unit runs as the same service user but with an ordinary login
environment, so ``sudo``, ``docker`` and editing files work as they do over SSH, while a bug in the
agent still cannot hand out a shell.

Endpoints (all need the shared agent bearer token; the daemon listens on the same address as the
agent: loopback on node A, the QSFP address on node B):

* ``WS  /v1/terminal?cols=&rows=&actor=``  — binary frames are the terminal stream, text frames are JSON control
* ``GET /v1/terminal/status``               — policy, open sessions, shell, user
* ``GET /v1/terminal/recordings[/name]``    — asciicast files of past sessions
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect

from .. import __version__
from ..schemas.config import AgentConfig
from ..security import SecretsVault, tokens_equal
from .policy import PolicySource
from .terminal import MAX_INPUT, TerminalError, TerminalManager

log = logging.getLogger("twinspark.termd")
WS_MAX_MESSAGE = MAX_INPUT + 1024


def policy_source(config: AgentConfig) -> PolicySource:
    r = config.remote_mgmt
    return PolicySource(path=Path(r.policy_path), require_root=r.require_root_owned)


def build_termd_app(config: AgentConfig, token: Optional[str] = None,
                    manager: Optional[TerminalManager] = None) -> FastAPI:
    expected = token if token is not None else SecretsVault(config.secrets_dir).get("agent_token")
    if not expected:
        raise RuntimeError("agent_token is not set — run `tsm init --role agent` first")
    mgr = manager or TerminalManager(policy_source(config), config.remote_mgmt.record_dir,
                                     config.remote_mgmt.shell, config.node.node_id)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mgr.start()
        yield
        await mgr.stop()

    app = FastAPI(title="tsm-termd", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.manager = mgr

    def _bearer(value: Optional[str]) -> bool:
        scheme, _, tok = (value or "").partition(" ")
        return scheme.lower() == "bearer" and tokens_equal(tok.strip(), expected)

    def _auth(authorization: Optional[str]) -> None:
        if not _bearer(authorization):
            raise HTTPException(401, "invalid agent token")

    @app.get("/v1/terminal/status")
    def status(authorization: Optional[str] = Header(default=None)):
        _auth(authorization)
        return {**mgr.status(), "node": config.node.node_id, "version": __version__}

    @app.get("/v1/terminal/recordings")
    def recordings(authorization: Optional[str] = Header(default=None)):
        _auth(authorization)
        return {"recordings": mgr.recordings()}

    @app.get("/v1/terminal/recordings/{name}")
    def recording(name: str, authorization: Optional[str] = Header(default=None)):
        _auth(authorization)
        try:
            return {"name": name, "cast": mgr.read_recording(name)}
        except TerminalError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.websocket("/v1/terminal")
    async def terminal(ws: WebSocket):
        # Refuse before the upgrade completes: an unauthenticated caller never gets a socket.
        if not _bearer(ws.headers.get("authorization")):
            await ws.close(code=1008)
            return
        q = ws.query_params
        cols = int(q["cols"]) if q.get("cols", "").isdigit() else 80
        rows = int(q["rows"]) if q.get("rows", "").isdigit() else 24
        actor = (q.get("actor") or "user")[:40]
        await ws.accept()
        try:
            sess = await mgr.open(cols, rows, actor)
        except TerminalError as exc:
            await _send_json(ws, {"type": "error", "error": str(exc)})
            await ws.close(code=1011)
            return
        log.info("terminal session %s opened for %s", sess.id, actor)
        await _send_json(ws, {"type": "hello", "session": sess.id, "recorded": sess.recorder is not None,
                              "cols": sess.cols, "rows": sess.rows,
                              "idle_s": mgr.policy.current().terminal_idle_s})

        async def pump_out() -> None:
            while True:
                kind, payload = await mgr.next(sess)
                if kind == "out":
                    await ws.send_bytes(payload)
                elif kind == "notice":
                    await _send_json(ws, {"type": "notice", "text": payload})
                else:                                   # exit
                    await _send_json(ws, {"type": "exit", **payload})
                    return

        async def pump_in() -> None:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    await mgr.write(sess, msg["bytes"])
                elif msg.get("text") is not None:
                    try:
                        ctl: Any = json.loads(msg["text"])
                    except ValueError:
                        continue
                    if isinstance(ctl, dict) and ctl.get("type") == "resize":
                        mgr.resize(sess, ctl.get("cols"), ctl.get("rows"))
                    # {"type": "ping"} and anything else: only counts as a sign of life for the socket

        tasks = [asyncio.ensure_future(pump_out()), asyncio.ensure_future(pump_in())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except Exception:  # noqa: BLE001
            log.exception("terminal pump failed")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await mgr.close(sess, sess.reason or "client disconnected")
            log.info("terminal session %s closed: %s (in %d B, out %d B)", sess.id, sess.reason,
                     sess.bytes_in, sess.bytes_out)
            with contextlib.suppress(Exception):
                await ws.close()

    return app


async def _send_json(ws: WebSocket, obj: dict[str, Any]) -> None:
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await ws.send_text(json.dumps(obj))
