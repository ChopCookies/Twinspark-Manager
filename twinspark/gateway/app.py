"""ASGI app for the stable gateway (spec §15, §43)."""

from __future__ import annotations

import json

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .gateway import Gateway, RouteState

# Endpoints forwarded to vLLM. /v1/models is answered by the gateway itself.
# (audio/transcriptions is multipart, which this JSON-rewriting gateway cannot route by alias.)
_FORWARDED = {"chat/completions", "completions", "embeddings", "responses", "rerank",
              "score", "tokenize", "detokenize", "classify", "pooling"}
_HOP_BY_HOP = {"content-length", "transfer-encoding", "connection", "keep-alive",
               "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade"}
# Only failures before sending a request may move a POST to another replica.
# A read/write failure can mean the first backend already accepted an agent turn.
_RETRYABLE_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# An explicit unavailable reply rejects the turn. Gateway timeout/error replies
# (502/504) may follow an accepted turn and therefore must not be replayed.
_FAILOVER_STATUS = {503}


# A request body is parsed in the controller process (which also serves the GUI), so it is bounded.
# 32 MiB holds a prompt of about a million tokens or a few inline images.
MAX_REQUEST_BYTES = 32 * 1024 ** 2


async def _read_bounded(request: Request, limit: int) -> bytes | None:
    """The request body, or None when it is (or turns out to be) larger than ``limit``."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class _ResponseLimitExceeded(httpx.ReadError):
    """The in-process compatibility proxy exceeded its upstream byte budget."""


def _error(status: int, message: str, code: str, headers: dict | None = None, **extra) -> JSONResponse:
    body = {"error": {"message": message, "type": code, "code": code, **extra}}
    return JSONResponse(body, status_code=status, headers=headers)


def _model_entry(st: RouteState) -> dict:
    entry = {"id": st.alias, "object": "model", "owned_by": "twinspark",
             "root": st.served_model, "twinspark": st.to_payload()}
    if st.max_model_len:
        entry["max_model_len"] = st.max_model_len
    return entry


def build_gateway_app(gateway: Gateway, *, max_response_bytes: int | None = None,
                      max_request_bytes: int = MAX_REQUEST_BYTES) -> Starlette:
    # Probes use a cap before their in-process ASGI transport can buffer a body.
    # Ordinary inference traffic keeps its existing streaming behavior.
    if max_response_bytes is not None and (isinstance(max_response_bytes, bool)
                                           or not isinstance(max_response_bytes, int) or max_response_bytes < 1):
        raise ValueError("max_response_bytes must be a positive integer or None")
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
        if not gateway.check_key(request.headers.get("authorization")):
            return _error(401, "invalid api key", "invalid_api_key")
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
        raw = await _read_bounded(request, max_request_bytes)
        if raw is None:
            return _error(413, f"request body larger than {max_request_bytes // 1024 ** 2} MiB",
                          "invalid_request_error")
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return _error(400, "request body must be JSON", "invalid_request_error")
        if not isinstance(body, dict):
            return _error(400, "request body must be a JSON object", "invalid_request_error")
        model = body.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            return _error(400, "model must be a non-empty string", "invalid_request_error", param="model")

        st = gateway.resolve(model)
        if st is None:
            return _error(404, f"unknown model/alias: {str(model)[:120]!r}", "model_not_found")
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
        try:
            candidates = st.candidates()
            for i, backend in enumerate(candidates):
                backend_headers = gateway.backend_headers()
                if max_response_bytes is not None:
                    backend_headers["Accept-Encoding"] = "identity"
                req = gateway._client.build_request(
                    "POST", f"{backend}/v1/{path}", content=payload, headers=backend_headers)
                try:
                    resp = await gateway._client.send(req, stream=True)
                except _RETRYABLE_CONNECT_ERRORS as exc:
                    st.backend_failed(backend)
                    last_exc = exc
                except httpx.HTTPError as exc:
                    # Do not replay potentially accepted inference/tool requests.
                    last_exc = exc
                    break
                else:
                    if resp.status_code in _FAILOVER_STATUS and i + 1 < len(candidates):
                        st.backend_failed(backend)
                        try:
                            with anyio.CancelScope(shield=True):
                                await resp.aclose()
                        except httpx.HTTPError:
                            pass
                        continue
                    upstream = resp
                    break
        except BaseException:
            # Cancellation before upstream headers must not block future drains.
            st.release()
            raise
        if upstream is None:
            st.errors += 1
            st.last_error = f"backend request failed: {type(last_exc).__name__}"
            st.release()
            status_code = 504 if isinstance(last_exc, httpx.TimeoutException) else 502
            return _error(status_code, st.last_error, "backend_error")
        if upstream.status_code >= 500:
            st.errors += 1
            st.last_error = f"backend returned {upstream.status_code}"
            st.backend_failed(backend)

        if (max_response_bytes is not None
                and upstream.headers.get("content-encoding", "identity").lower() != "identity"):
            # A compressed wire-byte cap does not bound decompressed ASGI bodies.
            try:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
            except httpx.HTTPError:
                pass
            finally:
                st.release()
            return _error(502, "compatibility checks require uncompressed upstream responses", "backend_error")

        connection_headers = {h.strip().lower() for h in upstream.headers.get("connection", "").split(",")}
        headers = {k: v for k, v in upstream.headers.items()
                   if k.lower() not in _HOP_BY_HOP | connection_headers}
        released = False

        async def body_iter():
            # the in-flight counter only drops once the client got the last byte,
            # the client went away, or the backend stream broke — exactly once
            nonlocal released
            total = 0
            try:
                async for chunk in upstream.aiter_raw():
                    total += len(chunk)
                    if max_response_bytes is not None and total > max_response_bytes:
                        raise _ResponseLimitExceeded("backend response exceeded the compatibility check size limit")
                    yield chunk
            except httpx.CloseError as exc:
                # httpx also closes after the last byte; a cleanup error does
                # not invalidate a response that was already fully delivered.
                st.errors += 1
                st.last_error = f"stream close failed: {type(exc).__name__}"
            except httpx.HTTPError as exc:
                st.errors += 1
                st.last_error = f"stream interrupted: {type(exc).__name__}"
                # Headers have already been sent; abort the response instead of
                # presenting incomplete tool arguments as a successful stream.
                raise
            finally:
                try:
                    # Starlette cancels its stream task when the caller disconnects.
                    # Shield cleanup so the backend connection is released as well.
                    with anyio.CancelScope(shield=True):
                        await upstream.aclose()
                except httpx.HTTPError as exc:
                    st.errors += 1
                    st.last_error = f"stream close failed: {type(exc).__name__}"
                finally:
                    if not released:
                        released = True
                        st.release()

        is_sse = headers.get("content-type", "").lower().startswith("text/event-stream")
        if max_response_bytes is not None and not is_sse:
            # JSON/errors must be bounded before sending response headers. Reading
            # incrementally avoids the unbounded Response.aread() path.
            data = bytearray()
            try:
                async for chunk in body_iter():
                    data.extend(chunk)
            except _ResponseLimitExceeded:
                return _error(502, "backend response exceeded the compatibility check size limit",
                              "backend_response_too_large")
            except httpx.HTTPError:
                return _error(502, "backend response transfer failed", "backend_error")
            return Response(bytes(data), status_code=upstream.status_code, headers=headers)

        return StreamingResponse(body_iter(), status_code=upstream.status_code, headers=headers)

    return Starlette(routes=[
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/v1/models/{model:path}", get_model, methods=["GET"]),
        Route("/v1/{path:path}", forward, methods=["POST"]),
        Route("/twinspark/status", status, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
    ])
