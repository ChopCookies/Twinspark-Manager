"""Bound API request bodies as they are read, including chunked transfers."""
from __future__ import annotations

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

JSON_LIMIT = 4 * 1024 ** 2
UPLOAD_LIMIT = 100 * 1024 ** 2
UPLOAD_PATHS = ("/api/v1/mods",)
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class BodyLimitMiddleware:
    """Count received bytes without pre-reading or buffering the request body.

    Authentication can therefore reject a request before consuming any data.
    HTTPException is raised from receive inside the endpoint's normal exception
    handling, allowing FastAPI to return its ordinary, safe 413 response.
    Register before any BaseHTTPMiddleware so the limiter wraps its receive;
    otherwise that middleware can turn receive failures into ExceptionGroups.
    """

    def __init__(self, app: ASGIApp, json_limit: int = JSON_LIMIT,
                 upload_limit: int = UPLOAD_LIMIT, upload_paths: tuple[str, ...] = UPLOAD_PATHS):
        self.app = app
        self.json_limit = json_limit
        self.upload_limit = upload_limit
        self.upload_paths = upload_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope.get("method") not in MUTATING_METHODS
                or not scope.get("path", "").startswith("/api/")):
            await self.app(scope, receive, send)
            return
        limit = self.upload_limit if scope["path"].startswith(self.upload_paths) else self.json_limit
        size = 0

        async def bounded_receive() -> Message:
            nonlocal size
            if size > limit:
                raise HTTPException(413, f"request body exceeds the limit of {limit} bytes")
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > limit:
                    raise HTTPException(413, f"request body exceeds the limit of {limit} bytes")
            return message

        await self.app(scope, bounded_receive, send)
