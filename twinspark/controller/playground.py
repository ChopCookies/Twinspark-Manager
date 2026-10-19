"""The chat playground's way to the models (master plan §4.1): ``/api/v1/playground/v1/...``.

The GUI's *Playground* page talks to the OpenAI-compatible API of this gateway with the management session
it already has, instead of asking for an inference key: this ASGI endpoint checks the management key (the
same rules as every ``/api`` route), replaces it with TwinSpark's own internal token and hands the request
to the gateway app in this process. Streams pass through as they come, so the page measures time to first
token and tokens per second as an app would see them.

* Only ``POST /v1/chat/completions``, ``POST /v1/completions`` and ``GET /v1/models``.
* The management key never reaches a backend, and no app key is used or counted: playground requests do not
  show up in the per-key usage.
* With ``gateway_mode: standalone`` the request is served by the controller's copy of the routing, not by
  the gateway process: a switch does not wait for a playground answer.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.routing import Route

from ..api.deps import _bucket, _too_many, check_key
from ..gateway.app import build_gateway_app

PREFIX = "/api/v1/playground"
_ALLOWED = {("POST", "/v1/chat/completions"), ("POST", "/v1/completions"), ("GET", "/v1/models")}
_DROP = {b"authorization", b"x-api-key", b"cookie", b"origin", b"referer"}


class Playground:
    def __init__(self, controller):
        self.controller = controller
        self._app = None

    def _gateway_app(self):
        if self._app is None:
            self._app = build_gateway_app(self.controller.gateway)
        return self._app

    async def __call__(self, scope, receive, send) -> None:
        request = Request(scope, receive)
        try:
            check_key(request)
            ok = _bucket(request, "ok")
            ok[1] += 1
            if ok[1] > self.controller.config.rate_limit_per_min:
                raise _too_many()
        except HTTPException as exc:
            await _json(send, exc.status_code, {"detail": exc.detail}, exc.headers)
            return
        path = scope["path"][len(PREFIX):] or "/"
        if (scope["method"], path) not in _ALLOWED:
            await _json(send, 404, {"detail": "the playground offers /v1/chat/completions, /v1/completions and "
                                              "/v1/models"})
            return
        token = self.controller.gateway.internal_token
        headers = [(k, v) for k, v in scope["headers"] if k.lower() not in _DROP]
        headers.append((b"authorization", f"Bearer {token}".encode()))
        inner = {**scope, "path": path, "raw_path": path.encode(), "root_path": "", "headers": headers}
        await self._gateway_app()(inner, receive, send)


async def _json(send, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    data = json.dumps(body).encode()
    raw = [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]
    raw += [(k.lower().encode(), str(v).encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": data})


def routes(controller) -> list[Route]:
    endpoint = Playground(controller)
    return [Route(PREFIX + "/v1/{rest:path}", endpoint, methods=["GET", "POST"])]
