"""Connecting Google accounts: client credentials, OAuth and list discovery.

The Client ID and Secret are stored once for the whole service rather than per
account, because one Google Cloud project can authorise all ten slots. The user
does the Cloud Console work once and then connects as many Google accounts as
they like.

The redirect URI is derived from the address the browser is actually using,
rather than from a stored setting. Google matches it exactly, and deriving it
from the live request means it is always right for whichever address the user
opened the page with -- and it lets the page show them the precise string to
paste into the console.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorAuthError, ConnectorError
from app.connectors.google import (
    GoogleConnector,
    authorization_url,
    exchange_code,
)
from app.crypto import decrypt_json, encrypt_json
from app.db import settings_store
from app.db.models import (
    Account,
    AccountStatus,
    CollectionKind,
    RemoteList,
    ServiceKind,
)
from app.db.session import get_db
from app.web import deps
from app.web.forwarded import LOOPBACK_HOSTS, is_bare_ip
from app.web.disconnect import disconnect_accounts, wants_cleanup

logger = logging.getLogger(__name__)

router = APIRouter()

GOOGLE_CLIENT_KEY = "google_client_credentials"
OAUTH_STATE_KEY = "_oauth_state"

#: Hosts Google accepts over plain HTTP. Everything else must be HTTPS, and raw
#: private IP addresses are rejected outright whatever the scheme.


# --- Client credentials -------------------------------------------------------


def get_google_client_credentials(session: Session) -> tuple[str, str]:
    payload = decrypt_json(settings_store.get(session, GOOGLE_CLIENT_KEY))
    return payload.get("client_id", ""), payload.get("client_secret", "")


def has_google_credentials(session: Session) -> bool:
    client_id, client_secret = get_google_client_credentials(session)
    return bool(client_id and client_secret)


# --- Redirect URI -------------------------------------------------------------


def redirect_uri_for(request: Request) -> str:
    """The callback address, taken from the address this page was loaded with."""
    base = str(request.base_url).rstrip("/")
    return f"{base}/oauth/google/callback"


def redirect_uri_problem(uri: str) -> str | None:
    """Explain why Google would reject this redirect URI, if it would.

    Worth checking before the user goes to the console: a rejected URI produces
    an opaque ``redirect_uri_mismatch`` at the very end of the flow, long after
    the mistake was made.
    """
    parsed = urlparse(uri)
    host = (parsed.hostname or "").lower()

    if host in LOOPBACK_HOSTS:
        return None  # Loopback is exempt from the HTTPS requirement.

    if parsed.scheme != "https":
        return (
            f"Google will reject this address because it is not HTTPS. Only "
            f"localhost and 127.0.0.1 may use plain http. Open Task Hub at "
            f"http://localhost:8080 to connect Google, or put it behind HTTPS."
        )

    # A bare IP address is refused even over HTTPS.
    if host.replace(".", "").isdigit() or ":" in host:
        return (
            "Google will reject this address because it is a raw IP address. "
            "Use localhost, or a real domain name with HTTPS."
        )
    return None


# --- Saving client credentials ------------------------------------------------


@router.post("/services/google/credentials")
def save_credentials(
    request: Request,
    client_id: str = Form(...),
    client_secret: str = Form(...),
    db: Session = Depends(get_db),
):
    client_id = client_id.strip()
    client_secret = client_secret.strip()

    if not client_id or not client_secret:
        deps.flash(request, "Both the Client ID and the Client Secret are required.", "error")
        return deps.redirect("/services/google")

    if not client_id.endswith(".apps.googleusercontent.com"):
        deps.flash(
            request,
            "That does not look like a Google Client ID — they end in "
            "'.apps.googleusercontent.com'. Check you copied the Client ID and "
            "not the Client Secret or the project number.",
            "error",
        )
        return deps.redirect("/services/google")

    settings_store.set_value(
        db,
        GOOGLE_CLIENT_KEY,
        encrypt_json({"client_id": client_id, "client_secret": client_secret}),
    )
    db.commit()
    deps.flash(request, "Google credentials saved. You can now connect an account.", "success")
    return deps.redirect("/services/google")


@router.post("/services/google/credentials/clear")
def clear_credentials(request: Request, db: Session = Depends(get_db)):
    settings_store.set_value(db, GOOGLE_CLIENT_KEY, None)
    db.commit()
    deps.flash(request, "Google credentials removed.", "success")
    return deps.redirect("/services/google")


# --- OAuth --------------------------------------------------------------------


@router.get("/services/google/connect/{slot}")
def start_oauth(slot: int, request: Request, db: Session = Depends(get_db)):
    client_id, client_secret = get_google_client_credentials(db)
    if not client_id or not client_secret:
        deps.flash(request, "Add your Google Client ID and Secret first.", "error")
        return deps.redirect("/services/google")

    if not 1 <= slot <= 10:
        deps.flash(request, "That is not a valid account slot.", "error")
        return deps.redirect("/services/google")

    redirect_uri = redirect_uri_for(request)
    problem = redirect_uri_problem(redirect_uri)
    if problem:
        deps.flash(request, problem, "error")
        return deps.redirect("/services/google")

    # The state ties the callback back to this browser session and this slot,
    # so a stray or forged callback cannot attach someone else's Google account.
    state = secrets.token_urlsafe(32)
    request.session[OAUTH_STATE_KEY] = {
        "state": state,
        "slot": slot,
        # Stored because the token exchange must present the identical URI.
        "redirect_uri": redirect_uri,
    }

    return deps.redirect(authorization_url(client_id, redirect_uri, state), 302)


@router.get("/oauth/google/callback")
def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    pending = request.session.pop(OAUTH_STATE_KEY, None)

    if error:
        message = {
            "access_denied": "You declined the permission request, so nothing was connected.",
        }.get(error, f"Google reported an error: {error}")
        deps.flash(request, message, "error")
        return deps.redirect("/services/google")

    if not pending or not state or state != pending.get("state"):
        deps.flash(
            request,
            "That sign-in link has expired or did not match this browser "
            "session. Please start the connection again.",
            "error",
        )
        return deps.redirect("/services/google")

    if not code:
        deps.flash(request, "Google did not return an authorisation code.", "error")
        return deps.redirect("/services/google")

    client_id, client_secret = get_google_client_credentials(db)
    slot = int(pending.get("slot", 1))
    redirect_uri = pending.get("redirect_uri") or redirect_uri_for(request)

    try:
        tokens = exchange_code(client_id, client_secret, code, redirect_uri)
    except ConnectorAuthError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/services/google")

    credentials = {
        "refresh_token": tokens.get("refresh_token"),
        "access_token": tokens.get("access_token"),
        "expires_at": 0,  # Forces a refresh on first use, proving it works.
    }

    account = db.execute(
        select(Account).where(
            Account.service == ServiceKind.GOOGLE, Account.slot == slot
        )
    ).scalar_one_or_none()
    if account is None:
        account = Account(service=ServiceKind.GOOGLE, slot=slot)
        db.add(account)

    account.credentials = encrypt_json(credentials)
    account.status = AccountStatus.CONNECTED
    account.status_detail = None
    account.enabled = True
    db.commit()
    db.refresh(account)

    # Confirm it actually works now, while the user is still here to fix it.
    try:
        connector = GoogleConnector(
            account.id, credentials, {}, client_id=client_id, client_secret=client_secret
        )
        identity = connector.verify()
        account.remote_identity = identity
        account.credentials = encrypt_json(connector.current_credentials())
        db.commit()
        connector.close()
    except ConnectorError as exc:
        account.status = AccountStatus.ERROR
        account.status_detail = str(exc)
        db.commit()
        deps.flash(request, f"Connected, but the first call to Google failed: {exc}", "error")
        return deps.redirect("/services/google")

    deps.flash(request, f"Connected {identity} to slot {slot}.", "success")
    return deps.redirect(f"/services/google?discover={slot}")


@router.post("/services/google/{slot}/disconnect")
def disconnect(slot: int, request: Request,
               remove_items: str = Form(""),
               db: Session = Depends(get_db)):
    account = db.execute(
        select(Account).where(
            Account.service == ServiceKind.GOOGLE, Account.slot == slot
        )
    ).scalar_one_or_none()
    if account is not None:
        disconnect_accounts(
            request, db, [account], wants_cleanup(remove_items),
            note="To revoke Task Hub's access entirely, also remove it at "
                 "myaccount.google.com/permissions.",
        )
    return deps.redirect("/services/google")


# --- List discovery -----------------------------------------------------------


@router.post("/services/google/{slot}/discover")
def discover_lists(slot: int, request: Request, db: Session = Depends(get_db)):
    """Ask Google what task lists and calendars this account has."""
    account = db.execute(
        select(Account).where(
            Account.service == ServiceKind.GOOGLE, Account.slot == slot
        )
    ).scalar_one_or_none()
    if account is None:
        deps.flash(request, "That slot is not connected.", "error")
        return deps.redirect("/services/google")

    client_id, client_secret = get_google_client_credentials(db)
    try:
        connector = GoogleConnector(
            account.id, decrypt_json(account.credentials), account.sync_state,
            client_id=client_id, client_secret=client_secret,
        )
        remote_lists = connector.list_remote_lists()
        if connector.credentials_changed:
            account.credentials = encrypt_json(connector.current_credentials())
        connector.close()
    except ConnectorAuthError as exc:
        account.status = AccountStatus.NEEDS_AUTH
        account.status_detail = str(exc)
        db.commit()
        deps.flash(request, f"{exc}", "error")
        return deps.redirect("/services/google")
    except ConnectorError as exc:
        deps.flash(request, f"Could not read your Google lists: {exc}", "error")
        return deps.redirect("/services/google")

    known = {
        row.remote_id: row
        for row in db.execute(
            select(RemoteList).where(RemoteList.account_id == account.id)
        ).scalars()
    }
    seen: set[str] = set()

    for entry in remote_lists:
        seen.add(entry.remote_id)
        row = known.get(entry.remote_id)
        if row is None:
            db.add(
                RemoteList(
                    account_id=account.id,
                    remote_id=entry.remote_id,
                    name=entry.name,
                    kind=entry.kind,
                    colour=entry.colour,
                    is_default=entry.is_default,
                    # Everything starts switched off. Enabling sync for a list
                    # the user did not choose would push their tasks somewhere
                    # they never asked for.
                    read_enabled=False,
                    write_enabled=False,
                )
            )
        else:
            row.name = entry.name
            row.colour = entry.colour
            row.is_default = entry.is_default
            row.unavailable = False

    for remote_id, row in known.items():
        if remote_id not in seen:
            # Kept rather than deleted: the user's read/write choices and group
            # assignment survive a list temporarily disappearing.
            row.unavailable = True

    account.status = AccountStatus.CONNECTED
    account.status_detail = None
    db.commit()

    deps.flash(request, f"Found {len(remote_lists)} lists and calendars.", "success")
    return deps.redirect("/services/google")
