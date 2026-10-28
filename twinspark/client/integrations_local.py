"""Agent apps on the workstation: the Hermes lifecycle and a "test connection" from this computer.

The adapter (``adapters/hermes.py``) does the work; this module resolves *where* the app runs and the
endpoint as seen from there:

* ``workstation`` (default) — this computer. The endpoint is the client's SSH forward of node A's
  gateway (``http://127.0.0.1:<port>/v1``). Hermes's memory then does not compete with the model.
* ``node-a`` — an existing Hermes installation on node A itself, driven over the same SSH connection.
  The endpoint is node A's gateway on its own loopback.

The deployed context limit comes from the gateway's ``/v1/models`` (``max_model_len`` of the alias),
never from a setting the user would like to have. Only the inference key is ever written into Hermes;
the management key is passed as a forbidden value so a wrong paste is refused.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException

from .adapters import AdapterError, Endpoint, HermesAdapter, LocalHost, SSHHost, validate_endpoint
from .app import AdapterIn, TestIn  # module level: FastAPI resolves the (string) annotations from here
from .session import ClientSession
from .ssh import SSHError

_ERR_STATUS = {"stale-preview": 409, "preview-mismatch": 409, "invalid-token": 409, "context-too-small": 422,
               "context-unknown": 422, "wrong-key": 422, "invalid-config": 422, "no-profile": 404,
               "no-backup": 404, "backup-damaged": 409, "launch-failed": 502, "not-owned": 409,
               "write-failed": 500}


def _ssh_host(session: ClientSession) -> SSHHost:
    conn = session.current
    if conn is None or not conn.uses_ssh:
        raise HTTPException(422, "running Hermes on node A needs an SSH connection to node A")

    async def run(argv: list[str], timeout: float, input: Optional[bytes]) -> tuple[int, bytes, bytes]:
        try:
            r = await session.run(conn, argv, home=session.home, timeout=timeout, input=input)
        except SSHError as exc:
            raise AdapterError(f"node A: {exc}", "launch-failed") from None
        return r.rc, r.stdout.encode("utf-8", "replace"), r.stderr.encode("utf-8", "replace")
    return SSHHost(run, label=f"node A ({conn.host})")


_APP_KEY = {"workstation": "hermes_key", "node-a": "hermes_node_key"}


async def app_key(session: ClientSession, host: str) -> Optional[str]:
    """Hermes's own key (master plan §4.1): created once on node A as "Hermes on <computer>" and kept in this
    client's credential store, so it can be revoked without touching any other app. Falls back to the
    shared inference key when node A predates named keys or cannot create one."""
    import platform

    conn = session.current
    slot = _APP_KEY.get(host)
    if conn is None or slot is None:
        return session.inference_key()
    stored = session.creds.get(conn.id, slot)
    try:
        r = await session.node_request("GET", "/api/v1/keys", timeout=15)
    except ConnectionError:
        return stored.partition(":")[2] if stored else session.inference_key()
    if r.status_code != 200:                      # an older node A: one shared key
        return session.inference_key()
    active = {k.get("id") for k in r.json() if isinstance(k, dict) and k.get("state") == "active"}
    if stored and stored.partition(":")[0] in active:
        return stored.partition(":")[2]
    where = "node A" if host == "node-a" else (re.sub(r"[^\w.-]", "", platform.node())[:40] or "this computer")
    made = await session.node_request("POST", "/api/v1/keys", json={"name": f"Hermes on {where}", "app": "hermes"},
                                      timeout=15)
    if made.status_code != 201:
        return session.inference_key()
    doc = made.json()
    session.creds.set(conn.id, slot, f"{doc['id']}:{doc['key']}")
    return doc["key"]


async def endpoint_for(session: ClientSession, host: str, alias: Optional[str], *,
                       for_app: bool = False) -> Endpoint:
    """The endpoint for ``alias`` as an app on ``host`` reaches it, with the deployed context limit."""
    try:
        models = await session.models()
    except ConnectionError as exc:
        raise HTTPException(503, str(exc), headers={"X-TwinSpark-Client": "transport"}) from None
    except Exception as exc:  # noqa: BLE001 - the gateway answered something unusable
        raise HTTPException(502, f"node A's gateway did not list its models ({type(exc).__name__})") from None
    served = {m["id"]: m for m in models if isinstance(m.get("id"), str)}      # an entry without id is no alias
    if not served:
        raise HTTPException(409, "node A serves no model right now — switch to a recipe first")
    if alias is None:
        alias = "default" if "default" in served else next(iter(served))
    m = served.get(alias)
    if m is None:
        raise HTTPException(404, f"node A does not serve '{alias}' (it serves: {', '.join(sorted(served))})")
    meta = m.get("twinspark") or {}
    ctx = m.get("max_model_len")
    if host == "node-a":
        # the gateway as node A itself reaches it: the same address and scheme the SSH forward targets there
        cfg = session.node_cfg
        gw_host = cfg.get("gateway_forward_host") or "127.0.0.1"
        gw_host = f"[{gw_host}]" if ":" in gw_host and not gw_host.startswith("[") else gw_host
        base = f"{'https' if cfg.get('gateway_tls') else 'http'}://{gw_host}:{cfg.get('gateway_port', 8000)}/v1"
        source = "spark-local"
    else:
        base = session.gateway_url() or ""
        source = "workstation-forward" if session.current and session.current.uses_ssh else "tailnet"
    key = await app_key(session, host) if for_app else session.inference_key()
    return Endpoint(base_url=base, alias=alias, api_key=key,
                    context_length=ctx if isinstance(ctx, int) else None,
                    revision_id=meta.get("revision_id"), served_model=m.get("root"), source=source)


def add_integration_routes(app: FastAPI, session: ClientSession, connected: Callable[[], None]) -> None:
    def adapter(req: AdapterIn) -> HermesAdapter:
        host = LocalHost() if req.host == "workstation" else _ssh_host(session)
        try:
            a = HermesAdapter(host, req.profile, forbidden_keys=(session.management_key(),))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if req.host == "node-a":
            # Hermes there uses node A's loopback; this computer reaches the same gateway via its forward
            a.probe_base_url = session.gateway_url()
        return a

    async def guarded(coro) -> Any:
        try:
            result = await coro
        except AdapterError as exc:
            raise HTTPException(_ERR_STATUS.get(exc.code, 422), str(exc)) from None
        if isinstance(result, list):
            return [r.to_dict() if hasattr(r, "to_dict") else r for r in result]
        return result.to_dict() if hasattr(result, "to_dict") else result

    @app.post("/api/v1/client/integrations/test")
    async def test_connection(req: TestIn):
        """Chat, streaming and a tool round trip *from this computer*, through the same address apps use."""
        connected()
        ep = await endpoint_for(session, "workstation", req.alias)
        checks = await validate_endpoint(ep.base_url, ep.api_key, ep.alias, tools=req.tools)
        return {"endpoint": ep.to_dict(), "checks": [c.to_dict() for c in checks]}

    @app.post("/api/v1/client/integrations/hermes/detect")
    async def hermes_detect(req: AdapterIn):
        return await guarded(adapter(req).detect(deep=True))

    @app.post("/api/v1/client/integrations/hermes/status")
    async def hermes_status(req: AdapterIn):
        ep = None
        if session.phase == "connected":
            try:
                ep = await endpoint_for(session, req.host, req.alias, for_app=True)
            except HTTPException:
                ep = None                       # status still shows what is installed and running
        st = await guarded(adapter(req).status(ep, log_lines=req.lines))
        return {"status": st, "endpoint": ep.to_dict() if ep else None}

    @app.post("/api/v1/client/integrations/hermes/inspect")
    async def hermes_inspect(req: AdapterIn):
        ep = await endpoint_for(session, req.host, req.alias, for_app=True) if session.phase == "connected" else None
        return await guarded(adapter(req).inspect(ep))

    @app.post("/api/v1/client/integrations/hermes/preview")
    async def hermes_preview(req: AdapterIn):
        connected()
        ep = await endpoint_for(session, req.host, req.alias, for_app=True)
        return {"preview": await guarded(adapter(req).preview(ep)), "endpoint": ep.to_dict()}

    @app.post("/api/v1/client/integrations/hermes/apply")
    async def hermes_apply(req: AdapterIn):
        connected()
        if not req.token:
            raise HTTPException(422, "apply needs the token of the preview you reviewed")
        ep = await endpoint_for(session, req.host, req.alias, for_app=True)
        return await guarded(adapter(req).apply(ep, req.token))

    @app.post("/api/v1/client/integrations/hermes/validate")
    async def hermes_validate(req: AdapterIn):
        connected()
        ep = await endpoint_for(session, req.host, req.alias, for_app=True)
        return {"checks": await guarded(adapter(req).validate(ep)), "endpoint": ep.to_dict()}

    @app.post("/api/v1/client/integrations/hermes/launch")
    async def hermes_launch(req: AdapterIn):
        return await guarded(adapter(req).launch(req.mode))

    @app.post("/api/v1/client/integrations/hermes/stop")
    async def hermes_stop(req: AdapterIn):
        return await guarded(adapter(req).stop())

    @app.post("/api/v1/client/integrations/hermes/restart")
    async def hermes_restart(req: AdapterIn):
        return await guarded(adapter(req).restart())

    @app.post("/api/v1/client/integrations/hermes/logs")
    async def hermes_logs(req: AdapterIn):
        return {"lines": await guarded(adapter(req).logs(req.lines))}

    @app.post("/api/v1/client/integrations/hermes/backups")
    async def hermes_backups(req: AdapterIn):
        return {"backups": await guarded(adapter(req).backups())}

    @app.post("/api/v1/client/integrations/hermes/restore")
    async def hermes_restore(req: AdapterIn):
        return await guarded(adapter(req).restore(req.backup_id))

    @app.post("/api/v1/client/integrations/hermes/disconnect/preview")
    async def hermes_disconnect_preview(req: AdapterIn):
        return await guarded(adapter(req).preview_disconnect(restore_previous=req.restore_previous))

    @app.post("/api/v1/client/integrations/hermes/disconnect")
    async def hermes_disconnect(req: AdapterIn):
        return await guarded(adapter(req).disconnect(req.token, restore_previous=req.restore_previous))
