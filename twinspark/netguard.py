"""Outbound URL policy for fetches the management API can trigger (recipe URLs, registry lookups).

Those endpoints take a URL from the caller. Without a policy they could be pointed at loopback or
LAN services (the node agent, Docker, a router UI) and used to probe or read them. The rule is:
``https`` only, no embedded credentials, and every address the host name resolves to must be
globally routable.

The connection resolves the name once more when it opens, so a hostile DNS server could still
rebind between check and connect; the other rules and the terse error messages limit what that
could expose.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


class UnsafeURL(ValueError):
    pass


def check_public_url(url: str, resolve: bool = True) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise UnsafeURL("only https:// URLs are accepted")
    if parts.username or parts.password:
        raise UnsafeURL("URLs must not contain credentials")
    if resolve:
        try:
            infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError) as exc:
            raise UnsafeURL(f"cannot resolve {parts.hostname}") from exc
        for info in infos:
            addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
            if not addr.is_global:
                raise UnsafeURL(f"{parts.hostname} resolves to a non-public address; refusing to connect")
    return url


def guard_request(request: httpx.Request) -> None:
    """httpx ``request`` event hook: applies the policy to the first request and to every redirect."""
    try:
        check_public_url(str(request.url))
    except UnsafeURL as exc:
        raise httpx.RequestError(str(exc), request=request) from exc
