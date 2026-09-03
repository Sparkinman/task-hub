"""Make Task Hub see the address the browser actually used.

Task Hub is distributed as one image that has to work unchanged behind a LAN
address, a Cloudflare tunnel, Tailscale, or somebody's own nginx. It cannot be
told its own address in advance, so it works it out per request: everything it
shows or registers -- OAuth redirect URIs, the CalDAV addresses copied into a
phone -- is built from the connection in front of it.

A reverse proxy terminates the real connection and forwards a new one, so what
the application sees is the proxy, not the browser. The original details survive
only in headers the proxy adds, and this middleware puts them back into the
request before anything else looks at it. That is the whole reason a single
image can serve every deployment shape without configuration.

Those headers are also trivially forgeable by whoever connects, so they are
honoured only when the connection is plausibly a proxy: from loopback (a
sidecar such as cloudflared, or a proxy sharing the host) or from a private
network (a proxy elsewhere on the LAN). A request arriving straight off the
public internet is taken at face value. ``TASKHUB_TRUST_PROXY`` overrides the
decision with ``always`` or ``never`` for the unusual cases -- a proxy reached
over a public address, or an instance that must ignore the headers entirely.
"""

from __future__ import annotations

import ipaddress
import os

#: How much to believe the forwarded headers: "auto" (private and loopback
#: peers only), "always", or "never".
TRUST_MODE = (os.environ.get("TASKHUB_TRUST_PROXY") or "auto").strip().lower()

_HOP_HEADERS = (b"forwarded", b"x-forwarded-proto", b"x-forwarded-host",
                b"x-forwarded-port", b"x-forwarded-for")


def _peer_is_proxy_like(client: tuple | None) -> bool:
    """Whether the peer looks like a proxy rather than the open internet."""
    if not client:
        # No peer address at all: a unix socket or a test client, both of which
        # are local by definition.
        return True
    try:
        address = ipaddress.ip_address(client[0])
    except (ValueError, IndexError, TypeError):
        return False
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
    )


def should_trust(client: tuple | None) -> bool:
    if TRUST_MODE == "always":
        return True
    if TRUST_MODE == "never":
        return False
    return _peer_is_proxy_like(client)


def _first(value: str) -> str:
    """The left-most entry of a comma-separated forwarded header.

    Proxies append, so the original client's value is the first one. Anything
    after it was added by intermediaries closer to us.
    """
    return value.split(",")[0].strip()


def _parse_forwarded(value: str) -> dict[str, str]:
    """The first element of an RFC 7239 ``Forwarded`` header.

    Only ``proto`` and ``host`` are of interest; quoting is stripped because
    the standard allows ``host="example.com:8443"``.
    """
    found: dict[str, str] = {}
    for pair in _first(value).split(";"):
        key, _, raw = pair.partition("=")
        key = key.strip().lower()
        if key in ("proto", "host", "for"):
            found[key] = raw.strip().strip('"')
    return found


def _headers_of(scope) -> dict[bytes, str]:
    out: dict[bytes, str] = {}
    for key, value in scope.get("headers") or ():
        if key in _HOP_HEADERS:
            out[key] = value.decode("latin-1")
    return out


def _set_host(scope, host: str) -> None:
    headers = [(k, v) for k, v in scope["headers"] if k != b"host"]
    headers.append((b"host", host.encode("latin-1")))
    scope["headers"] = headers


def apply_forwarded(scope) -> None:
    """Rewrite ``scope`` so the request describes the browser's own connection.

    Starlette builds ``request.base_url`` from the scheme and the Host header,
    so correcting those two here fixes every address the application derives
    without a single call site having to know a proxy exists.
    """
    if not should_trust(scope.get("client")):
        return

    headers = _headers_of(scope)
    if not headers:
        return

    raw_forwarded = headers.get(b"forwarded", "")
    forwarded = _parse_forwarded(raw_forwarded) if raw_forwarded else {}

    scheme = forwarded.get("proto") or _first(headers.get(b"x-forwarded-proto", ""))
    if scheme in ("http", "https"):
        scope["scheme"] = scheme

    host = forwarded.get("host") or _first(headers.get(b"x-forwarded-host", ""))
    if host:
        # A proxy that forwards the host but not the port leaves the address
        # incomplete, and an OAuth redirect URI missing its port matches
        # nothing. The default port for the scheme stays implicit, as in any
        # normal URL.
        if ":" not in host.rsplit("]", 1)[-1]:
            port = _first(headers.get(b"x-forwarded-port", ""))
            default = "443" if scope.get("scheme") == "https" else "80"
            if port and port.isdigit() and port != default:
                host = f"{host}:{port}"
        _set_host(scope, host)

    client = _first(headers.get(b"x-forwarded-for", ""))
    if client:
        # Restores the real caller for logging and for any check that cares who
        # is asking; without it every request appears to come from the proxy.
        scope["client"] = (client, 0)


class ForwardedHeadersMiddleware:
    """Applies :func:`apply_forwarded` to every HTTP request.

    Registered outermost so that every other middleware, every route and the
    embedded Radicale server all see the corrected request.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            apply_forwarded(scope)
        await self.app(scope, receive, send)
