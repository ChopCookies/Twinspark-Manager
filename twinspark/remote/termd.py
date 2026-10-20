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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

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
                    manager: Optional[TerminalManager] = None, idle_exit_s: float = 0.0) -> Starlette:
    """Plain Starlette (no FastAPI): a handful of endpoints, and the process should stay small.

    ``idle_exit_s`` > 0: once no shell has been open for that long, ask the server in
    ``app.state.server`` to finish. Used with socket activation, where systemd keeps the port and starts
    the service again on the next connection.
    """
    expected = token if token is not None else SecretsVault(config.secrets_dir).get("agent_token")
    if not expected:
        raise RuntimeError("agent_token is not set — run `tsm init --role agent` first")
    mgr = manager or TerminalManager(policy_source(config), config.remote_mgmt.record_dir,
                                     config.remote_mgmt.shell, config.node.node_id)
    idle = {"since": time.monotonic()}

    async def watch_idle(app: Starlette) -> None:
        while True:
            await asyncio.sleep(min(15.0, max(0.05, idle_exit_s / 4)))
            if mgr.sessions:
                idle["since"] = time.monotonic()
                continue
            server = getattr(app.state, "server", None)
            if server is not None and time.monotonic() - idle["since"] >= idle_exit_s:
                log.info("no terminal session for %.0f s: exiting until the next connection", idle_exit_s)
                server.should_exit = True
                return

    @asynccontextmanager
    async def lifespan(app: Starlette):
        mgr.start()
        watcher = asyncio.create_task(watch_idle(app)) if idle_exit_s > 0 else None
        yield
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await mgr.stop()

    def _bearer(value: Optional[str]) -> bool:
        scheme, _, tok = (value or "").partition(" ")
        return scheme.lower() == "bearer" and tokens_equal(tok.strip(), expected)

    def _denied(request: Request) -> Optional[JSONResponse]:
        if _bearer(request.headers.get("authorization")):
            return None
        return JSONResponse({"detail": "invalid agent token"}, status_code=401)

    async def status(request: Request):
        return _denied(request) or JSONResponse({**mgr.status(), "node": config.node.node_id,
                                                 "version": __version__})

    async def recordings(request: Request):
        return _denied(request) or JSONResponse({"recordings": mgr.recordings()})

    async def recording(request: Request):
        if (refused := _denied(request)) is not None:
            return refused
        name = request.path_params["name"]
        try:
            return JSONResponse({"name": name, "cast": mgr.read_recording(name)})
        except TerminalError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)

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
        idle["since"] = time.monotonic()
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
            idle["since"] = time.monotonic()
            with contextlib.suppress(Exception):
                await ws.close()

    app = Starlette(lifespan=lifespan, routes=[
        Route("/v1/terminal/status", status, methods=["GET"]),
        Route("/v1/terminal/recordings", recordings, methods=["GET"]),
        Route("/v1/terminal/recordings/{name}", recording, methods=["GET"]),
        WebSocketRoute("/v1/terminal", terminal),
    ])
    app.state.manager = mgr
    return app


async def _send_json(ws: WebSocket, obj: dict[str, Any]) -> None:
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await ws.send_text(json.dumps(obj))
