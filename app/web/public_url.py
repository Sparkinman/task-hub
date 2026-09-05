"""What address is Task Hub reachable at? Answered per request, not per install.

There is deliberately no configured answer to this question. The same image is
meant to run on a Raspberry Pi reached by LAN address, behind a Cloudflare
tunnel, on a Tailscale network and behind somebody's nginx, and in several of
those the address is not knowable until a request arrives. So the address in
use *is* the answer, and :mod:`app.web.forwarded` has already corrected the
request for any proxy in front of it.

The override exists for the one case the request cannot answer: an address that
must be handed to something which is not the browser -- a phone set up from a
different network, say -- where what the user is looking at now is not what
they need to type in later.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import RUNTIME
from app.db import settings_store


def detected_base_url(request: Request) -> str:
    """The address this request came in on, without a trailing slash."""
    return str(request.base_url).rstrip("/")


def configured_override(db: Session) -> str:
    """A hand-set address, if there is one. Normally there is not.

    The setting in the interface wins over the environment variable, so that an
    address set from a browser is not silently overruled by something in a
    compose file the user may not be able to edit.
    """
    saved = (settings_store.get(db, settings_store.BASE_URL_OVERRIDE) or "").rstrip("/")
    return saved or RUNTIME.base_url_override


def public_base_url(request: Request, db: Session) -> str:
    """The address to hand out to CalDAV clients.

    Not used for OAuth redirect URIs, which are built from the live request in
    :mod:`app.web.oauth_setup` and :mod:`app.web.google_setup` even when an
    override is set: a redirect address that disagrees with the browser by one
    character fails at the last step of sign-in, so the address that delivered
    the page is the only one worth sending.
    """
    return configured_override(db) or detected_base_url(request)


def override_conflict(request: Request, db: Session) -> str | None:
    """The address in use, when an override is set and disagrees with it.

    Worth surfacing: an override left over from an earlier deployment silently
    sends CalDAV clients somewhere that no longer works, and every symptom of
    that appears far away from the setting that caused it.
    """
    override = configured_override(db)
    if not override:
        return None
    detected = detected_base_url(request)
    return None if override == detected else detected
