"""The workstation client's web app: the GUI, the connection pages, and a proxy to node A.

It listens on loopback only and accepts a per-launch session key (handed to the browser in the URL
fragment, so it never reaches a server log or a Referer). Requests are checked like the node checks
them — the key, a loopback ``Host`` (DNS rebinding) and the ``Origin`` of anything that changes state —
because a local proxy that adds node A's real management key would otherwise be a confused deputy for
any web page the user visits.

What runs here, on the workstation:

* ``/api/v1/client/*`` — saved connections, host keys, the one-time key install, connect/disconnect,
  setup checks, the three status states, the local inference endpoint, and the agent-app adapters
  (Hermes: detect, configure, validate, launch, restore) with a "test connection" from this computer.
* ``/api/v1/cookbook`` browsing, community recipe fetching and recipe conversion — the Spark only gets
  the finished draft (``POST /api/v1/profiles``).

Everything else under ``/api/`` is passed to node A with the stored management key: node A owns the
profiles, the jobs, the deployment and the agents.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import secrets
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlencode

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from .. import __version__
from ..security import tokens_equal
from . import ssh
from .connections import Connection, suggest_mode, tailscale_peers
from .discovery import (
    CredentialAccessError,
    DiscoveryError,
    fetch_credentials,
    probe_node,
    read_controller_nodes,
    setup_checks,
)
from .session import ClientSession

log = logging.getLogger("twinspark.client.app")
WEB_ASSETS = Path(__file__).resolve().parent.parent / "web" / "dist"
MAX_PROXY_BODY = 128 * 1024 ** 2          # a mod upload is at most 100 MiB of base64 JSON
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
        "transfer-encoding", "upgrade", "content-length", "content-encoding", "host"}
# never sent upstream: the browser's own credentials and the headers the client already checked
_DROP_UP = _HOP | {"x-api-key", "authorization", "cookie", "origin", "referer", "sec-fetch-site",
                   "sec-fetch-mode", "sec-fetch-dest", "accept-encoding"}


def _loopback_host(host_header: str) -> bool:
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _clean(value: object, limit: int) -> str:
    """Printable, JSON/UTF-8 safe, bounded text for error payloads."""
    return str(value)[:limit].encode("utf-8", "replace").decode("utf-8", "replace")


def _same_origin(origin: Optional[str], host: str) -> bool:
    return origin is not None and origin.split("://", 1)[-1].rstrip("/") == host


# ---- request bodies ---------------------------------------------------------------------------------
class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1, max_length=253)
    ssh_user: str = Field("", max_length=32)
    ssh_port: int = Field(22, ge=1, le=65535)
    mode: Optional[Literal["ssh", "tailscale", "direct"]] = None
    identity_file: Optional[str] = Field(None, max_length=1024)
    direct_url: Optional[str] = Field(None, max_length=512)


class ConnectionPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    ssh_user: Optional[str] = Field(None, max_length=32)
    ssh_port: Optional[int] = Field(None, ge=1, le=65535)
    identity_file: Optional[str] = Field(None, max_length=1024)
    # the local ends of the forwards; null = pick again at the next connect
    local_api_port: Optional[int] = Field(None, ge=1024, le=65535)
    local_gateway_port: Optional[int] = Field(None, ge=1024, le=65535)


class TrustIn(BaseModel):
    fingerprints: list[str] = Field(min_length=1, max_length=8)
    replace: bool = False


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class CredentialsIn(BaseModel):
    management_key: Optional[str] = Field(None, max_length=512)
    inference_key: Optional[str] = Field(None, max_length=512)


class FetchIn(BaseModel):
    sudo: bool = False


class AdapterIn(BaseModel):
    host: Literal["workstation", "node-a"] = "workstation"
    profile: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    alias: Optional[str] = Field(None, max_length=63)
    token: Optional[str] = Field(None, max_length=512)
    mode: Literal["cli", "gateway"] = "gateway"
    backup_id: Optional[str] = Field(None, max_length=128)
    restore_previous: bool = True
    lines: int = Field(50, ge=1, le=500)


class TestIn(BaseModel):
    alias: str = Field(min_length=1, max_length=63)
    tools: bool = True


class ImportIn(BaseModel):
    text: Optional[str] = Field(None, max_length=512 * 1024)
    url: Optional[str] = None
    profile_name: Optional[str] = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    preview: bool = False


def create_client_app(session: ClientSession, local_key: str, *, web_dir: Optional[Path] = None,
                      autoconnect: bool = False, launch_code: Optional[str] = None) -> FastAPI:
    web = Path(web_dir) if web_dir else WEB_ASSETS

    async def lifespan(app: FastAPI):
        if autoconnect and (last := session.last_connection()) is not None:
            with contextlib.suppress(Exception):
                await session.connect(last.id, wait=False)
        yield
        await session.aclose()

    app = FastAPI(title="TwinSpark client", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=contextlib.asynccontextmanager(lifespan))
    app.state.session = session

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # FastAPI's default handler echoes the submitted value: a too-long password or key would come
        # back in the response. Report where and why, never the value.
        errors = [{"loc": [_clean(x, 60) for x in e.get("loc", ())], "msg": _clean(e.get("msg", ""), 300),
                   "type": _clean(e.get("type", ""), 60)} for e in exc.errors()[:20]]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": f"internal error ({type(exc).__name__})"})

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # the static GUI is public (it holds no data); every API call needs the session key
        if not _loopback_host(request.headers.get("host", "")):
            return JSONResponse({"detail": "the TwinSpark client only answers http://127.0.0.1"}, status_code=403)
        if request.url.path.startswith("/api/"):
            if request.method in _MUTATING:
                origin = request.headers.get("origin")
                if origin is not None and not _same_origin(origin, request.headers.get("host", "")):
                    return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
                if origin is None and request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                    return JSONResponse({"detail": "cross-site request rejected"}, status_code=403)
            if not tokens_equal(request.headers.get("x-api-key"), local_key):
                return JSONResponse({"detail": "invalid client session key — open the address `tsm client` "
                                               "printed (it carries the key)"}, status_code=401,
                                    headers={"X-TwinSpark-Client": "session"})
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    # ---- the browser's first visit ----------------------------------------------------------------
    launch = {"code": launch_code}

    @app.get("/launch/{code}", include_in_schema=False)
    def open_gui(code: str):
        """The address `tsm client` gives the browser it starts: a code that works once and turns into the
        page with the session key in its fragment. A program's command line can be read by other users
        of this computer; the key itself never appears in one."""
        if launch["code"] is not None and tokens_equal(code, launch["code"]):
            launch["code"] = None
            target = f"/#k={local_key}"
        else:
            target = "/"
        return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})

    def conn_or_404(conn_id: str) -> Connection:
        c = session.connections.get(conn_id)
        if c is None:
            raise HTTPException(404, "no such connection")
        return c

    def connected() -> None:
        if session.phase != "connected" or not session.node_url():
            raise HTTPException(503, session.transport_note(), headers={"X-TwinSpark-Client": "transport"})

    def bad(exc: Exception, status: int = 422) -> HTTPException:
        return HTTPException(status, str(exc))

    # ---- client: connections ------------------------------------------------------------------------
    @app.get("/api/v1/client/info")
    async def info():
        try:
            ssh_path, ssh_error = ssh.find_ssh(), None
        except ssh.SSHError as exc:
            ssh_path, ssh_error = None, str(exc)
        return {"client": True, "version": __version__, "home": str(session.home),
                "credential_backend": session.creds.backend, "ssh": {"path": ssh_path, "error": ssh_error},
                "connections": [c.to_dict() for c in session.connections.list()],
                "current": session.current.id if session.current else None}

    @app.get("/api/v1/client/status")
    async def status():
        return await session.status()

    @app.get("/api/v1/client/connections")
    def list_connections():
        return {"connections": [c.to_dict() for c in session.connections.list()]}

    @app.post("/api/v1/client/connections", status_code=201)
    def add_connection(req: ConnectionIn):
        mode = req.mode or ("direct" if req.direct_url else suggest_mode(req.host))
        try:
            c = session.connections.add(Connection(id="", name=req.name, mode=mode, host=req.host.strip(),
                                                   ssh_user=req.ssh_user.strip(), ssh_port=req.ssh_port,
                                                   identity_file=req.identity_file or None,
                                                   direct_url=req.direct_url or None))
        except ValueError as exc:
            raise bad(exc) from None
        return c.to_dict()

    @app.patch("/api/v1/client/connections/{conn_id}")
    def update_connection(conn_id: str, req: ConnectionPatch):
        conn_or_404(conn_id)
        try:
            return session.connections.update(conn_id, **req.model_dump(exclude_unset=True)).to_dict()
        except ValueError as exc:
            raise bad(exc) from None

    @app.delete("/api/v1/client/connections/{conn_id}")
    async def remove_connection(conn_id: str):
        conn_or_404(conn_id)
        if session.current and session.current.id == conn_id:
            await session.disconnect()
        session.connections.remove(conn_id, credentials=session.creds)
        return {"ok": True}

    @app.post("/api/v1/client/connections/{conn_id}/hostkey/scan")
    async def scan_hostkey(conn_id: str):
        c = conn_or_404(conn_id)
        try:
            keys = await asyncio.to_thread(ssh.scan_host_keys, c.host, c.ssh_port)
        except ssh.SSHError as exc:
            raise bad(exc, 502) from None
        return {"status": ssh.host_key_status(session.home, c.host, c.ssh_port, keys),
                "keys": [{"type": k.type, "fingerprint": k.fingerprint} for k in keys],
                "known": [{"type": k.type, "fingerprint": k.fingerprint}
                          for k in ssh.known_keys(session.home, c.host, c.ssh_port)]}

    @app.post("/api/v1/client/connections/{conn_id}/hostkey/trust")
    async def trust_hostkey(conn_id: str, req: TrustIn):
        """Trust what the user confirmed: the keys are scanned again and only the confirmed fingerprints
        are stored, so a key that changed between showing and confirming is not trusted."""
        c = conn_or_404(conn_id)
        try:
            keys = await asyncio.to_thread(ssh.scan_host_keys, c.host, c.ssh_port)
        except ssh.SSHError as exc:
            raise bad(exc, 502) from None
        chosen = [k for k in keys if k.fingerprint in set(req.fingerprints)]
        if not chosen:
            raise HTTPException(409, "the host now presents different keys than the ones you confirmed — "
                                     "scan again and compare the fingerprints")
        try:
            added = ssh.trust_host_keys(session.home, c.host, c.ssh_port, chosen, replace=req.replace)
        except ValueError as exc:
            raise bad(exc, 409) from None
        return {"trusted": added}

    @app.post("/api/v1/client/connections/{conn_id}/key/install")
    async def install_key(conn_id: str, req: PasswordIn):
        """One password login that adds this client's own SSH key; the password is used once, never stored."""
        c = conn_or_404(conn_id)
        _, public = ssh.ensure_client_key(session.home)
        try:
            added = await ssh.install_key_with_password(c, req.password, public, home=session.home)
        except ssh.SSHError as exc:
            raise bad(exc, 401 if exc.kind == "auth" else 502) from None
        except ValueError as exc:            # e.g. a line break in the password; the message never holds it
            raise bad(exc) from None
        return {"installed": added, "public_key": public}

    @app.post("/api/v1/client/connections/{conn_id}/connect")
    async def connect(conn_id: str, wait: bool = False):
        conn_or_404(conn_id)
        return await session.connect(conn_id, wait=wait)

    @app.post("/api/v1/client/disconnect")
    async def disconnect():
        await session.disconnect()
        return await session.status()

    @app.post("/api/v1/client/connections/{conn_id}/probe")
    async def probe(conn_id: str):
        """What is installed on node A, and the setup checks (reachable, version, role, runtime, peer)."""
        c = conn_or_404(conn_id)
        if not c.uses_ssh:
            raise HTTPException(422, "a direct connection has no SSH access to inspect the node")
        facts, nodes, err = None, None, None
        try:
            facts = await probe_node(c, home=session.home, run=session.run)
            if facts.get("role") == "controller":
                nodes = await read_controller_nodes(c, home=session.home, run=session.run)
        except ssh.SSHError as exc:
            err = str(exc)
        except DiscoveryError as exc:
            err = str(exc)
        creds_ok = None if facts is None else bool(session.creds.get(c.id, "management_key"))
        return {"facts": facts, "nodes": nodes, "error": err,
                "checks": setup_checks(facts, (nodes or {}).get("nodes") if nodes else None, creds_ok)}

    @app.put("/api/v1/client/connections/{conn_id}/credentials")
    def put_credentials(conn_id: str, req: CredentialsIn):
        conn_or_404(conn_id)
        for name in ("management_key", "inference_key"):
            value = getattr(req, name)
            if value:
                session.creds.set(conn_id, name, value.strip())
        return {"management_key": bool(session.creds.get(conn_id, "management_key")),
                "inference_key": bool(session.creds.get(conn_id, "inference_key")),
                "backend": session.creds.backend}

    @app.post("/api/v1/client/connections/{conn_id}/credentials/fetch")
    async def fetch_keys(conn_id: str, req: FetchIn = Body(default_factory=FetchIn)):
        c = conn_or_404(conn_id)
        try:
            keys = await fetch_credentials(c, home=session.home, run=session.run, sudo=req.sudo)
        except CredentialAccessError as exc:
            raise bad(exc, 403) from None
        except ssh.SSHError as exc:
            raise bad(exc, 502) from None
        for name in ("management_key", "inference_key"):
            if keys.get(name):
                session.creds.set(c.id, name, keys[name])
        return {"management_key": bool(keys.get("management_key")), "inference_key": bool(keys.get("inference_key"))}

    @app.delete("/api/v1/client/connections/{conn_id}/credentials")
    def forget_credentials(conn_id: str):
        conn_or_404(conn_id)
        session.creds.delete_all(conn_id)
        return {"ok": True}

    @app.get("/api/v1/client/endpoint")
    async def endpoint(reveal: bool = False):
        """The OpenAI-compatible endpoint for apps on this computer. ``reveal=true`` includes the
        inference key (for pasting into an app); the management key is never handed out."""
        out: dict[str, Any] = {"base_url": session.gateway_url(), "has_key": bool(session.inference_key()),
                               "note": "keep `tsm client` running: the address is an SSH forward"
                               if session.current and session.current.uses_ssh else None}
        if reveal:
            out["api_key"] = session.inference_key()
        return out

    @app.get("/api/v1/client/tailscale/peers")
    async def peers():
        return {"peers": await asyncio.to_thread(tailscale_peers)}

    # ---- client: agent apps ---------------------------------------------------------------------
    from .integrations_local import add_integration_routes
    add_integration_routes(app, session, connected)

    # ---- client: recipe browsing and conversion ---------------------------------------------------
    from .cookbook_local import add_cookbook_routes
    add_cookbook_routes(app, session, connected)

    # ---- everything else: node A --------------------------------------------------------------------
    @app.websocket("/api/v1/remote/terminal/ws")
    async def terminal_ws(ws: WebSocket):
        await _proxy_ws(ws, session, local_key)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def to_node(path: str, request: Request):
        return await _proxy(request, session)

    if web.exists():
        app.mount("/", StaticFiles(directory=str(web), html=True), name="web")
    return app


async def _proxy(request: Request, session: ClientSession) -> Response:
    base, key = session.node_url(), session.management_key()
    if session.phase != "connected" or not base:
        return JSONResponse({"detail": session.transport_note()}, status_code=503,
                            headers={"X-TwinSpark-Client": "transport"})
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_PROXY_BODY:
        return JSONResponse({"detail": "request body too large"}, status_code=413)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_PROXY_BODY:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_UP}
    if key:
        headers["x-api-key"] = key
    url = base + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    try:
        upstream = await session.http.send(
            session.http.build_request(request.method, url, headers=headers, content=bytes(body)), stream=True)
    except httpx.TransportError as exc:
        return JSONResponse({"detail": f"node A did not answer through the connection ({type(exc).__name__}): "
                                       f"{session.transport_note()}"}, status_code=503,
                            headers={"X-TwinSpark-Client": "transport"})
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP}
    if upstream.status_code == 401:
        # node A refused *our stored key*; the browser's own session is fine — say so instead of locking it
        await upstream.aclose()
        return JSONResponse({"detail": "node A refused the stored management key — open Connection and paste it "
                                       "again (`sudo tsm init --show` on node A)"}, status_code=502,
                            headers={"X-TwinSpark-Client": "credentials"})

    async def relay():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
    return StreamingResponse(relay(), status_code=upstream.status_code, headers=out_headers)


async def _proxy_ws(ws: WebSocket, session: ClientSession, local_key: str) -> None:
    """The browser terminal: check the browser like the node would, then bridge to node A's socket."""
    host = ws.headers.get("host", "")
    origin = ws.headers.get("origin")
    if (not _loopback_host(host) or (origin is not None and not _same_origin(origin, host))
            or not tokens_equal(ws.query_params.get("client_key"), local_key)):
        await ws.close(code=1008)
        return
    base = session.node_url()
    if session.phase != "connected" or not base:
        await ws.close(code=1013)
        return
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    # the session key is this client's, not node A's: it is not passed on
    query = urlencode([(k, v) for k, v in ws.query_params.multi_items() if k != "client_key"])
    target = "ws" + base[4:] + "/api/v1/remote/terminal/ws" + (f"?{query}" if query else "")
    try:
        # no Origin upstream: the ticket in the query authenticates, and node A treats a socket without
        # an Origin as "not a browser" (the browser's Origin was checked above)
        upstream = await connect(target, max_size=2 * 1024 ** 2, open_timeout=15, proxy=None)
    except Exception:  # noqa: BLE001 - refused ticket, transport down: the browser sees a closed socket
        await ws.close(code=1011)
        return
    await ws.accept()

    async def down() -> None:
        async for msg in upstream:
            if isinstance(msg, bytes):
                await ws.send_bytes(msg)
            else:
                await ws.send_text(msg)

    async def up() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])
            elif msg.get("text") is not None:
                await upstream.send(msg["text"])

    tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (ConnectionClosed, WebSocketDisconnect):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await ws.close()


def new_session_key() -> str:
    return secrets.token_urlsafe(24)
