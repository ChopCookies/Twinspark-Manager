"""ASGI app for the stable gateway (spec §15, §43)."""

from __future__ import annotations

import json

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .gateway import Gateway, RouteState

# Endpoints forwarded to vLLM. /v1/models is answered by the gateway itself.
_FORWARDED = {"chat/completions", "completions", "embeddings", "responses", "rerank",
              "score", "tokenize", "detokenize", "audio/transcriptions", "classify",
              "pooling"}
_HOP_BY_HOP = {"content-length", "transfer-encoding", "connection", "keep-alive"}


def _error(status: int, message: str, code: str, headers: dict | None = None, **extra) -> JSONResponse:
    body = {"error": {"message": message, "type": code, "code": code, **extra}}
    return JSONResponse(body, status_code=status, headers=headers)


def _model_entry(st: RouteState) -> dict:
    entry = {"id": st.alias, "object": "model", "owned_by": "twinspark",
             "root": st.served_model, "twinspark": st.to_payload()}
    if st.max_model_len:
        entry["max_model_len"] = st.max_model_len
    return entry


def build_gateway_app(gateway: Gateway) -> Starlette:
    async def list_models(request: Request):
        if not gateway.check_key(request.headers.get("authorization")):
            return _error(401, "invalid api key", "invalid_api_key")
        return JSONResponse({
            "object": "list",
            "data": [_model_entry(st) for st in gateway.routes.values()
                     if st.backends or st.draining],
        })

    async def get_model(request: Request):
        if not gateway.check_key(request.headers.get("authorization")):
            return _error(401, "invalid api key", "invalid_api_key")
        st = gateway.resolve(request.path_params["model"])
        if st is None or not (st.backends or st.draining):
            return _error(404, "unknown model", "model_not_found")
        return JSONResponse(_model_entry(st))

    async def status(request: Request):
        return JSONResponse({"routes": [
            {k: v for k, v in st.to_payload().items() if k not in ("last_error",)}
            for st in gateway.routes.values()]})

    async def health(request: Request):
        serving = any(st.status == "serving" for st in gateway.routes.values())
        return JSONResponse({"status": "ok" if serving else "unavailable"},
                            status_code=200 if serving else 503)

    async def forward(request: Request):
        path = request.path_params["path"]
        if path not in _FORWARDED:
            return _error(404, f"unsupported endpoint /v1/{path}", "not_found")
        if not gateway.check_key(request.headers.get("authorization")):
            return _error(401, "invalid api key", "invalid_api_key")
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _error(400, "request body must be JSON", "invalid_request_error")
        if not isinstance(body, dict):
            return _error(400, "request body must be a JSON object", "invalid_request_error")

        st = gateway.resolve(body.get("model"))
        if st is None:
            return _error(404, f"unknown model/alias: {body.get('model')!r}", "model_not_found")
        if st.draining or not st.backends or st.down_reason:
            msg = ("model is down: " + st.down_reason) if st.down_reason else \
                "model is switching or not loaded; retry shortly"
            return _error(503, msg, "twinspark_unavailable",
                          headers={"Retry-After": str(st.retry_after)}, state=st.to_payload())

        body["model"] = st.served_model
        payload = json.dumps(body).encode()
        st.acquire()
        upstream = None
        last_exc: Exception | None = None
        for backend in st.candidates():
            req = gateway._client.build_request(
                "POST", f"{backend}/v1/{path}", content=payload, headers=gateway.backend_headers())
            try:
                upstream = await gateway._client.send(req, stream=True)
                break
            except httpx.HTTPError as exc:
                st.backend_failed(backend)
                last_exc = exc
        if upstream is None:
            st.errors += 1
            st.last_error = f"backend unreachable: {type(last_exc).__name__}"
            st.release()
            return _error(502, f"backend unreachable: {type(last_exc).__name__}", "backend_error")
        if upstream.status_code >= 500:
            st.errors += 1
            st.last_error = f"backend returned {upstream.status_code}"

        headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
        released = False

        async def body_iter():
            # the in-flight counter only drops once the client got the last byte,
            # the client went away, or the backend stream broke — exactly once
            nonlocal released
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            except httpx.HTTPError as exc:
                st.errors += 1
                st.last_error = f"stream interrupted: {type(exc).__name__}"
            finally:
                await upstream.aclose()
                if not released:
                    released = True
                    st.release()

        return StreamingResponse(body_iter(), status_code=upstream.status_code, headers=headers)

    return Starlette(routes=[
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/v1/models/{model:path}", get_model, methods=["GET"]),
        Route("/v1/{path:path}", forward, methods=["POST"]),
        Route("/twinspark/status", status, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
    ])
