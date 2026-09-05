"""Signing in to Supernote Cloud, which takes two steps rather than one.

Every other password service in Task Hub takes an address and a secret and is
done. Supernote emails a six-character code and will not issue a session
without it, so the form has to pause in the middle and come back.

The half-finished sign-in lives in the browser session, not the database: it is
worthless after a few minutes, it is tied to the person sitting at the form, and
writing it to disk would mean storing a credential that exists only to be spent
once. The password is held there too, for the same few minutes, because the
verification step needs it and asking somebody to type it twice to complete one
sign-in is the sort of thing that makes people give up.

What is stored afterwards is the session token and the address it belongs to.
The password is not kept: it cannot renew a session on its own -- Supernote
would email another code -- so keeping it would be holding a credential that
buys nothing.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorAuthError, ConnectorError
from app.connectors.supernote import begin_sign_in, finish_sign_in, token_expiry
from app.crypto import decrypt_json, encrypt_json
from app.db.models import Account, AccountStatus, ServiceKind
from app.db.session import get_db
from app.web import deps

logger = logging.getLogger(__name__)
router = APIRouter()

PAGE = "/services/supernote"
MAX_SLOTS = 10

#: Where the half-finished sign-in waits between the two steps.
PENDING_KEY = "supernote_pending"


def pending_for(request: Request, slot: int) -> dict | None:
    """The in-progress sign-in for this slot, if the code step is still open."""
    pending = request.session.get(PENDING_KEY)
    if isinstance(pending, dict) and int(pending.get("slot", 0)) == int(slot):
        return pending
    return None


def any_pending(request: Request) -> dict | None:
    pending = request.session.get(PENDING_KEY)
    return pending if isinstance(pending, dict) else None


@router.post("/services/supernote/{slot}/signin")
def sign_in(
    slot: int,
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """Step one: send the password, and ask Supernote to email a code."""
    if not 1 <= slot <= MAX_SLOTS:
        deps.flash(request, "That is not a valid account slot.", "error")
        return deps.redirect(PAGE)

    email = email.strip()
    if not email or not password:
        deps.flash(request, "Both the email address and the password are needed.", "error")
        return deps.redirect(PAGE)

    try:
        result = begin_sign_in(email, password)
    except ConnectorAuthError as exc:
        # A refused password is worth clearing any half-finished attempt for:
        # the code sitting in their inbox belongs to a sign-in that cannot now
        # be completed.
        request.session.pop(PENDING_KEY, None)
        deps.flash(request, str(exc), "error")
        return deps.redirect(PAGE)
    except ConnectorError as exc:
        logger.warning("Supernote sign-in failed: %s", exc)
        # Supernote rate-limits code requests -- press the button twice and the
        # second attempt is refused with "wait for the countdown to end". The
        # first one still sent a code, so throwing the pending sign-in away here
        # would strand somebody holding a perfectly good code with nowhere to
        # type it. Keep the form open and say so.
        existing = pending_for(request, slot)
        if existing is not None:
            deps.flash(
                request,
                f"{exc} A code was already sent to {existing['email']} — enter "
                "that one below rather than asking for another.",
                "warning",
            )
        else:
            deps.flash(request, str(exc), "error")
        return deps.redirect(PAGE)

    # Some accounts are not asked for a code at all, and finishing there and
    # then is better than showing a box for a code that will never arrive.
    if result.get("token"):
        return _store_token(request, db, slot, email, result["token"])

    request.session[PENDING_KEY] = {
        "slot": slot,
        "email": email,
        "password": password,
        "validCodeKey": result["validCodeKey"],
        "timestamp": result["timestamp"],
    }
    deps.flash(
        request,
        f"Supernote has emailed a six-character code to {email}. Enter it below "
        "to finish. The codes expire after a few minutes, so if it has been "
        "longer than that, start again and use the newest email.",
        "info",
    )
    return deps.redirect(PAGE)


@router.post("/services/supernote/{slot}/verify")
def verify_code(
    slot: int,
    request: Request,
    code: str = Form(""),
    db: Session = Depends(get_db),
):
    """Step two: exchange the emailed code for a thirty-day session."""
    pending = pending_for(request, slot)
    if pending is None:
        deps.flash(
            request,
            "That sign-in is no longer in progress. Enter your email address "
            "and password again to get a fresh code.",
            "error",
        )
        return deps.redirect(PAGE)

    if not code.strip():
        deps.flash(request, "Enter the six-character code from the email.", "error")
        return deps.redirect(PAGE)

    try:
        token = finish_sign_in(
            pending["email"], code, pending["validCodeKey"], pending["timestamp"]
        )
    except ConnectorAuthError as exc:
        # Deliberately kept pending: a mistyped code should not cost the user
        # another email and another wait.
        deps.flash(request, str(exc), "error")
        return deps.redirect(PAGE)
    except ConnectorError as exc:
        request.session.pop(PENDING_KEY, None)
        deps.flash(request, str(exc), "error")
        return deps.redirect(PAGE)

    request.session.pop(PENDING_KEY, None)
    return _store_token(request, db, slot, pending["email"], token)


@router.post("/services/supernote/{slot}/cancel")
def cancel(slot: int, request: Request):
    """Abandon a half-finished sign-in without leaving the form stuck."""
    request.session.pop(PENDING_KEY, None)
    deps.flash(request, "Sign-in cancelled.", "info")
    return deps.redirect(PAGE)


def _store_token(request: Request, db: Session, slot: int, email: str, token: str):
    """Save the session, then prove it works before saying it worked."""
    account = db.execute(
        select(Account).where(
            Account.service == ServiceKind.SUPERNOTE, Account.slot == slot
        )
    ).scalar_one_or_none()
    if account is None:
        account = Account(service=ServiceKind.SUPERNOTE, slot=slot)
        db.add(account)

    # The password is deliberately not among these: it cannot renew a session
    # without another emailed code, so storing it would buy nothing and risk
    # something.
    account.credentials = encrypt_json({"token": token, "email": email})
    account.enabled = True
    account.status = AccountStatus.NEW
    account.status_detail = None
    db.commit()
    db.refresh(account)

    from app.sync.engine import build_connector

    try:
        connector = build_connector(db, account)
        identity = connector.verify()
        connector.close()
    except ConnectorAuthError as exc:
        account.status = AccountStatus.NEEDS_AUTH
        account.status_detail = str(exc)
        db.commit()
        deps.flash(request, str(exc), "error")
        return deps.redirect(PAGE)
    except ConnectorError as exc:
        account.status = AccountStatus.ERROR
        account.status_detail = str(exc)
        db.commit()
        deps.flash(request, f"Signed in, but the first read failed: {exc}", "error")
        return deps.redirect(PAGE)

    account.remote_identity = identity
    account.status = AccountStatus.CONNECTED
    account.status_detail = None
    db.commit()

    expires = token_expiry(token)
    when = f" It runs out on {expires:%-d %B %Y}." if expires else ""
    deps.flash(request, f"Connected {identity} to slot {slot}.{when}", "success")
    return deps.redirect(f"{PAGE}?discover={slot}")


# --- Expiry warning -----------------------------------------------------------

def expiring_accounts(db: Session, within_days: int = 7) -> list[dict]:
    """Connected accounts whose session runs out soon, or already has.

    Worth surfacing because there is no way to renew one in the background.
    Sync simply stops, and the only cure is a person signing in again -- so
    saying so a week early is the difference between a chore and a surprise.
    """
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    soon: list[dict] = []
    accounts = db.execute(
        select(Account).where(Account.service == ServiceKind.SUPERNOTE)
    ).scalars()
    for account in accounts:
        token = (decrypt_json(account.credentials) or {}).get("token") or ""
        expires = token_expiry(token)
        if expires is None:
            continue
        days = (expires - now).total_seconds() / 86400
        if days <= within_days:
            soon.append({
                "slot": account.slot,
                "email": (decrypt_json(account.credentials) or {}).get("email", ""),
                "expires": expires,
                "days": int(days),
                "expired": days <= 0,
            })
    return soon
