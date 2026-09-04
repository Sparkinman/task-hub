"""CalDAV and CardDAV service discovery, as RFC 6764 defines it.

A CalDAV client is not given a full URL. It is given a server -- somebody types
``192.168.1.232:8080`` into their iPhone -- and is expected to find the rest by
asking for ``/.well-known/caldav`` and following the redirect to wherever the
service actually lives. Task Hub mounts Radicale at ``/radicale``, which no
client could guess, so without this the discovery step fails.

What made this worth finding was how it failed. The well-known path is not a
route, so it fell through to the session gate and was answered with a redirect
to Task Hub's HTML login page. iOS, given an HTML page where it expected a
redirect to a DAV collection, reports the account as unusable in a way that
reads exactly like a rejected password -- so the person adding it retypes their
password, resets it, retypes it again, and never learns that the password was
right the whole time.

The redirect is deliberately unauthenticated. It reveals only where the CalDAV
service is mounted, which is not a secret, and requiring a session to find that
out would defeat the entire point: the client has no session and never will,
because it authenticates against Radicale's own htpasswd with HTTP Basic.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.config import RADICALE_MOUNT_PATH

router = APIRouter()

#: 301 rather than 302, which RFC 6764 recommends: the mount point does not
#: move between requests, and a permanent answer lets a client stop asking.
_STATUS = 301


@router.get("/.well-known/caldav", include_in_schema=False)
@router.api_route(
    "/.well-known/caldav", methods=["PROPFIND", "OPTIONS"], include_in_schema=False
)
def caldav_discovery() -> RedirectResponse:
    """Point a calendar client at the collections."""
    return RedirectResponse(f"{RADICALE_MOUNT_PATH}/", status_code=_STATUS)


@router.get("/.well-known/carddav", include_in_schema=False)
@router.api_route(
    "/.well-known/carddav", methods=["PROPFIND", "OPTIONS"], include_in_schema=False
)
def carddav_discovery() -> RedirectResponse:
    """Contacts are not served, but answering honestly beats a login page.

    Radicale can hold address books and Task Hub does not use them. A client
    that asks is redirected to the same place, where it will find no address
    books and conclude that correctly, rather than being handed a login form and
    concluding something wrong about the credentials.
    """
    return RedirectResponse(f"{RADICALE_MOUNT_PATH}/", status_code=_STATUS)
