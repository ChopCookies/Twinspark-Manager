"""Remote management routes: reachability, logs, support bundle, terminal, power, Wake-on-LAN, smart plug.

Every HTTP route sits behind the management key like the rest of the API. The terminal WebSocket
cannot send custom headers from a browser, so it is authenticated by a one-time ticket that only an
authenticated ``POST /terminal/ticket`` can obtain (30 s, single use, bound to one node), plus an
Origin check. Nothing here runs a command: the controller asks an agent for a typed action or relays
the stream to the node's own terminal service, and the node's root-owned policy decides.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query, Response, WebSocket
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from ..controller.controller import Controller
from ..controller.remote import MAX_BRIDGES, RemoteError, RemoteService
from ..remote.logs import SOURCES
from .deps import controller_dep, require_auth

log = logging.getLogger("twinspark.remote")

router = APIRouter(prefix="/api/v1/remote", tags=["remote"], dependencies=[Depends(require_auth)])
ws_router = APIRouter(tags=["remote"])

_IFACE = r"^[A-Za-z0-9_.:-]{1,15}$"


def svc(ctrl: Controller = Depends(controller_dep)) -> RemoteService:
    return ctrl.remote


class TicketRequest(BaseModel):
    node: str = Field(max_length=8)
    cols: int = Field(80, ge=10, le=500)
    rows: int = Field(24, ge=2, le=200)


class PowerRequest(BaseModel):
    action: Literal["reboot", "poweroff"]
    confirm: str = Field("", max_length=40)
    delay_s: int = Field(5, ge=2, le=600)
    force: bool = False


class BootNextRequest(BaseModel):
    target: str = Field("network", pattern=r"^(network|[0-9A-Fa-f]{4})$")


class WolSetRequest(BaseModel):
    iface: str = Field(pattern=_IFACE)
    mode: Literal["g", "d"] = "g"


class PlugRequest(BaseModel):
    action: Literal["on", "off", "cycle"]
    confirm: str = Field("", max_length=40)
    force: bool = False


# ---- read-only -------------------------------------------------------------------------------
@router.get("/overview")
async def overview(s: RemoteService = Depends(svc)):
    return await s.overview()


@router.get("/{node}/status")
async def status(node: str, s: RemoteService = Depends(svc)):
    s.node(node)
    return await s.status(node)


@router.get("/{node}/reach")
async def reach(node: str, s: RemoteService = Depends(svc)):
    return await s.reach(node)


@router.get("/{node}/logs")
async def logs(node: str, source: str = Query("agent", max_length=30),
               lines: int = Query(200, ge=1, le=2000),
               since_s: Optional[int] = Query(None, ge=1, le=30 * 86400),
               grep: Optional[str] = Query(None, max_length=100),
               s: RemoteService = Depends(svc)):
    if source not in SOURCES:
        raise RemoteError(f"unknown log source '{source}' (known: {', '.join(SOURCES)})")
    s.node(node)
    return {"sources": SOURCES, **await s.logs(node, source, lines, since_s, grep)}


@router.get("/{node}/bundle")
async def bundle(node: str, s: RemoteService = Depends(svc)):
    """A redacted tar.gz with everything needed to diagnose the node; no secret values inside."""
    name, data, problems = await s.bundle(node)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "bundle.tar.gz"
    return Response(data, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{safe}"', "Content-Encoding": "identity",
        "X-TSM-Problems": str(len(problems)), "Cache-Control": "no-store"})


@router.get("/{node}/recordings")
async def recordings(node: str, s: RemoteService = Depends(svc)):
    s.node(node)
    return {"recordings": await s.recordings(node)}


@router.get("/{node}/recordings/{name}")
async def recording(node: str, name: str, s: RemoteService = Depends(svc)):
    s.node(node)
    return await s.recording(node, name)


@router.get("/{node}/boot")
async def boot(node: str, s: RemoteService = Depends(svc)):
    return await s.boot_status(node)


# ---- actions ---------------------------------------------------------------------------------
@router.post("/terminal/ticket")
async def terminal_ticket(req: TicketRequest, s: RemoteService = Depends(svc)):
    return await s.issue_ticket(req.node, req.cols, req.rows)


@router.post("/{node}/power")
async def power(node: str, req: PowerRequest, s: RemoteService = Depends(svc)):
    s.node(node)
    return await s.power(node, req.action, req.confirm, req.delay_s, req.force)


@router.post("/{node}/power/cancel")
async def power_cancel(node: str, s: RemoteService = Depends(svc)):
    s.node(node)
    return await s.power_cancel(node)


@router.post("/{node}/boot/next")
async def boot_next(node: str, req: BootNextRequest, s: RemoteService = Depends(svc)):
    s.node(node)
    return await s.boot_next(node, req.target)


@router.post("/{node}/boot/clear")
async def boot_clear(node: str, s: RemoteService = Depends(svc)):
    s.node(node)
    return await s.boot_clear(node)


@router.post("/{node}/wol/set")
async def wol_set(node: str, req: WolSetRequest, s: RemoteService = Depends(svc)):
    s.node(node)
    return await s.wol_set(node, req.iface, req.mode)


@router.post("/{node}/wake")
async def wake(node: str, s: RemoteService = Depends(svc)):
    return await s.wake(node)


@router.post("/{node}/plug")
async def plug(node: str, req: PlugRequest, s: RemoteService = Depends(svc)):
    return await s.plug(node, req.action, req.confirm, req.force)


# ---- terminal WebSocket ----------------------------------------------------------------------
def origin_allowed(ws: WebSocket, csrf_protection: bool) -> bool:
    """A browser always sends Origin on a WebSocket; it must be this server. No Origin = not a browser."""
    origin = ws.headers.get("origin")
    if origin is None or not csrf_protection:
        return True
    return origin.split("://", 1)[-1].rstrip("/") == ws.headers.get("host", "")


def _resize_frame(text: str) -> Optional[str]:
    """The only browser control message that is forwarded; the numbers are re-validated downstream."""
    try:
        ctl: Any = json.loads(text)
    except ValueError:
        return None
    if not isinstance(ctl, dict) or ctl.get("type") != "resize":
        return None
    cols, rows = ctl.get("cols"), ctl.get("rows")
    if (isinstance(cols, bool) or isinstance(rows, bool)
            or not isinstance(cols, int) or not isinstance(rows, int)):
        return None
    return json.dumps({"type": "resize", "cols": cols, "rows": rows})


async def _send_error(ws: WebSocket, message: str) -> None:
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await ws.send_text(json.dumps({"type": "error", "error": message}))


@ws_router.websocket("/api/v1/remote/terminal/ws")
async def terminal_ws(ws: WebSocket):
    ctrl: Controller = ws.app.state.controller
    s: RemoteService = ctrl.remote
    # Everything that can be refused is refused before the upgrade completes. The ticket is spent
    # first, so a request from the wrong origin cannot be retried with the same ticket.
    ticket = s.redeem(ws.query_params.get("ticket", ""))
    if ticket is None or not origin_allowed(ws, ctrl.config.csrf_protection):
        await ws.close(code=1008)
        return
    if s.bridges >= MAX_BRIDGES:
        await ws.close(code=1013)
        return
    await ws.accept()
    s.bridges += 1
    started = time.monotonic()
    stats: dict[str, Any] = {"in": 0, "out": 0, "session": None, "reason": None}
    up = None
    try:
        try:
            up = await s.open_upstream(ticket.node, ticket.cols, ticket.rows, ticket.actor)
        except RemoteError as exc:
            await _send_error(ws, str(exc))
            await ws.close(code=1011)
            return
        s._audit("remote.terminal_open", ticket.node, {"actor": ticket.actor})

        async def node_to_browser() -> None:
            async for msg in up:
                if isinstance(msg, bytes):
                    stats["out"] += len(msg)
                    await ws.send_bytes(msg)
                    continue
                with contextlib.suppress(ValueError, AttributeError):
                    frame = json.loads(msg)
                    if frame.get("type") == "hello":
                        stats["session"] = frame.get("session")
                    elif frame.get("type") == "exit":
                        stats["reason"] = frame.get("reason")
                await ws.send_text(msg)

        async def browser_to_node() -> None:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    stats["browser_left"] = True
                    return
                if msg.get("bytes") is not None:
                    stats["in"] += len(msg["bytes"])
                    await up.send(msg["bytes"])
                elif msg.get("text") is not None:
                    frame = _resize_frame(msg["text"])
                    if frame:
                        await up.send(frame)

        tasks = [asyncio.ensure_future(node_to_browser()), asyncio.ensure_future(browser_to_node())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if stats["reason"] is None:
            stats["reason"] = "browser closed" if stats.get("browser_left") else "node closed the session"
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:  # noqa: BLE001 - a relay failure must end the session, not the server
        stats["reason"] = stats["reason"] or f"relay error ({type(exc).__name__})"
        log.warning("terminal relay to node %s ended: %s", ticket.node, type(exc).__name__)
    finally:
        s.bridges -= 1
        if up is not None:
            with contextlib.suppress(Exception):
                await up.close()
            s._audit("remote.terminal_close", ticket.node, {
                "actor": ticket.actor, "session": stats["session"], "reason": stats["reason"],
                "bytes_in": stats["in"], "bytes_out": stats["out"],
                "seconds": round(time.monotonic() - started, 1)})
        with contextlib.suppress(Exception):
            await ws.close()
