"""Connecting services that sign in with a username and password.

Apple, a generic CalDAV server and Things 3 have no OAuth to delegate to, so
Task Hub holds the credentials itself. All are encrypted at rest with the same
key as every OAuth token, and none is ever rendered back into the page.

Apple must be given an *app-specific* password rather than the Apple ID
password; the setup guide explains where to make one, and the connector says so
plainly when Apple refuses a sign-in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorAuthError, ConnectorError
from app.crypto import decrypt_json, encrypt_json
from app.db.models import Account, AccountStatus, RemoteList, ServiceKind
from app.db.session import get_db
from app.web import deps
from app.web.disconnect import disconnect_accounts, wants_cleanup

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_SLOTS = 10


@dataclass(frozen=True)
class PasswordService:
    kind: ServiceKind
    name: str
    #: What the two fields are called to the person filling them in.
    user_label: str
    secret_label: str
    user_hint: str
    secret_hint: str
    #: A generic CalDAV server needs a third field, because unlike Apple and
    #: Things there is no address to assume. Left off every other service rather
    #: than shown empty, so nobody wonders what to put in it.
    needs_url: bool = False
    url_label: str = "Server address"
    url_hint: str = ""
    url_placeholder: str = ""


SERVICES: dict[str, PasswordService] = {
    ServiceKind.APPLE.value: PasswordService(
        kind=ServiceKind.APPLE,
        name="Apple",
        user_label="Apple ID",
        secret_label="App-specific password",
        user_hint="The full email address of the Apple ID holding these calendars.",
        secret_hint=(
            "Not your normal Apple ID password — Apple always refuses that here. "
            "Make an app-specific password at account.apple.com and paste it in, "
            "dashes included."
        ),
    ),
    ServiceKind.CALDAV.value: PasswordService(
        kind=ServiceKind.CALDAV,
        name="CalDAV",
        user_label="Username",
        secret_label="Password",
        user_hint=(
            "Whatever you sign in to that server with. Nextcloud wants the "
            "short username, Fastmail and Zoho want the full email address."
        ),
        secret_hint=(
            "Most servers want an app password made in their own settings "
            "rather than the one you log in to the website with — Nextcloud, "
            "Fastmail and Zoho all do. It is encrypted at rest here."
        ),
        needs_url=True,
        url_label="Server address",
        url_hint=(
            "The address of the server itself, not of one calendar. Task Hub "
            "asks it what the account owns and finds the rest — so "
            "https://cloud.example.com is usually enough, and the long "
            "/remote.php/dav/... path is not needed."
        ),
        url_placeholder="https://cloud.example.com",
    ),
    ServiceKind.THINGS3.value: PasswordService(
        kind=ServiceKind.THINGS3,
        name="Things 3",
        user_label="Things Cloud email",
        secret_label="Things Cloud password",
        user_hint="The email address you use for Things Cloud.",
        secret_hint=(
            "Cultured Code publishes no API, so there is no way to connect "
            "without storing this. It is encrypted at rest and never shown again."
        ),
    ),
}


def normalise_server_url(value: str) -> str:
    """Make a typed-in server address into something a client can use.

    People paste what their provider's help page shows them, and that is rarely
    a tidy URL: a bare hostname, a copied address with a trailing space, or the
    long path to one calendar. The first two are fixed here rather than refused,
    because a form that rejects "cloud.example.com" for missing a scheme is a
    form that teaches nothing and helps nobody.

    A missing scheme becomes https. Plain http is left alone -- it is a
    deliberate choice for a server on your own network, and the connector
    relaxes its TLS requirement for exactly that case.
    """
    url = (value or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = f"https://{url}"
    return url.rstrip()


def service_for(key: str) -> PasswordService | None:
    """The definition for a service that signs in with a username and password.

    Returns None for anything else, which is how a route distinguishes a
    password-based service from an OAuth one without a second table of names.
    """
    return SERVICES.get(key)


def _build(db: Session, account: Account):
    """Construct the live connector for an account, to test its credentials.

    Used at the moment credentials are saved rather than at the next sync, so
    that a wrong password is reported while the person is still looking at the
    form that caused it.
    """
    from app.sync.engine import build_connector

    return build_connector(db, account)


@router.post("/services/{service_key}/{slot}/credentials")
def save_credentials(
    service_key: str,
    slot: int,
    request: Request,
    username: str = Form(""),
    secret: str = Form(""),
    server_url: str = Form(""),
    label: str = Form(""),
    db: Session = Depends(get_db),
):
    service = service_for(service_key)
    if service is None:
        deps.flash(request, "Unknown service.", "error")
        return deps.redirect("/services")
    if not 1 <= slot <= MAX_SLOTS:
        deps.flash(request, "That is not a valid account slot.", "error")
        return deps.redirect(f"/services/{service_key}")

    username = username.strip()
    account = db.execute(
        select(Account).where(Account.service == service.kind, Account.slot == slot)
    ).scalar_one_or_none()

    stored = decrypt_json(account.credentials) if account else {}
    # An empty secret box means "keep the one already saved", so the field can
    # stay blank rather than echoing a password back into the HTML.
    secret = secret.strip() or stored.get("password") or ""

    if not username or not secret:
        deps.flash(
            request,
            f"Both the {service.user_label.lower()} and the "
            f"{service.secret_label.lower()} are needed.",
            "error",
        )
        return deps.redirect(f"/services/{service_key}")

    payload = {"username": username, "password": secret}
    if service.kind == ServiceKind.THINGS3:
        payload = {"email": username, "password": secret}

    if service.needs_url:
        # Blank means "keep the address already saved", the same rule the
        # password field follows, so an account can be re-authenticated without
        # retyping its server.
        url = normalise_server_url(server_url) or stored.get("url", "")
        if not url:
            deps.flash(request, f"The {service.url_label.lower()} is needed.", "error")
            return deps.redirect(f"/services/{service_key}")
        payload["url"] = url

    if account is None:
        account = Account(service=service.kind, slot=slot)
        db.add(account)
    account.credentials = encrypt_json(payload)
    account.enabled = True
    account.status = AccountStatus.NEW
    account.status_detail = None
    if label.strip():
        account.label = label.strip()[:120]
    db.commit()
    db.refresh(account)

    # Prove it works now, while the user is still here to fix it.
    try:
        connector = _build(db, account)
        identity = connector.verify()
        connector.close()
    except ConnectorAuthError as exc:
        account.status = AccountStatus.NEEDS_AUTH
        account.status_detail = str(exc)
        db.commit()
        deps.flash(request, str(exc), "error")
        return deps.redirect(f"/services/{service_key}")
    except ConnectorError as exc:
        account.status = AccountStatus.ERROR
        account.status_detail = str(exc)
        db.commit()
        deps.flash(request, f"Saved, but the first call failed: {exc}", "error")
        return deps.redirect(f"/services/{service_key}")

    account.remote_identity = identity
    account.status = AccountStatus.CONNECTED
    account.status_detail = None
    db.commit()

    deps.flash(request, f"Connected {identity} to slot {slot}.", "success")
    return deps.redirect(f"/services/{service_key}?discover={slot}")


@router.post("/services/{service_key}/{slot}/forget")
def forget(service_key: str, slot: int, request: Request,
           remove_items: str = Form(""),
           db: Session = Depends(get_db)):
    service = service_for(service_key)
    if service is None:
        return deps.redirect("/services")
    account = db.execute(
        select(Account).where(Account.service == service.kind, Account.slot == slot)
    ).scalar_one_or_none()
    if account is not None:
        disconnect_accounts(
            request, db, [account], wants_cleanup(remove_items),
            note="Its saved password was deleted.",
        )
    return deps.redirect(f"/services/{service_key}")
