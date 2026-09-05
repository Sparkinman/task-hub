"""Subscribing a device to notifications, and sending them.

Task Hub sends very few of these on purpose. A notification that arrives often
is one people turn off, and then the one that mattered never arrives either. So
there are exactly two: a sync that has started failing, and a Supernote session
about to expire -- both things you can do something about, and both things you
would otherwise discover only by visiting a page you had no reason to visit.

Everything here is best-effort. A push that cannot be sent is logged and
forgotten; nothing in Task Hub waits on one, and no failure to notify is
allowed to affect a sync.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import decrypt_json, encrypt_json
from app.db import settings_store
from app.db.models import PushSubscription
from app.db.session import get_db, session_scope
from app.services import webpush
from app.web import deps

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/push")


# --- The server's keypair -----------------------------------------------------

def keypair(session: Session) -> tuple[str, str]:
    """This server's VAPID keys, generated once on first use.

    Never regenerated automatically. A new key silently invalidates every
    subscription, because a browser records which key it agreed to accept
    messages signed by -- so a well-meaning "reset" would quietly stop
    notifications everywhere with nothing to show why.
    """
    public = settings_store.get(session, settings_store.PUSH_PUBLIC_KEY) or ""
    stored = decrypt_json(
        settings_store.get(session, settings_store.PUSH_PRIVATE_KEY)
    ) or {}
    private = stored.get("key") or ""

    if not private or not public:
        private, public = webpush.generate_keypair()
        settings_store.set_value(
            session, settings_store.PUSH_PRIVATE_KEY, encrypt_json({"key": private})
        )
        settings_store.set_value(session, settings_store.PUSH_PUBLIC_KEY, public)
        session.commit()
        logger.info("Generated a VAPID keypair for push notifications")
    return private, public


@router.get("/key")
def public_key(db: Session = Depends(get_db)):
    """The public key a browser needs before it can subscribe."""
    from fastapi.responses import JSONResponse

    _, public = keypair(db)
    return JSONResponse({"key": public})


# --- Subscribing --------------------------------------------------------------

@router.post("/subscribe")
async def subscribe(request: Request, db: Session = Depends(get_db)):
    """Record a browser's subscription, or refresh one already known."""
    from fastapi.responses import JSONResponse

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "expected JSON"}, status_code=400)

    endpoint = (body.get("endpoint") or "").strip()
    keys = body.get("keys") or {}
    p256dh, auth = (keys.get("p256dh") or "").strip(), (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        return JSONResponse({"error": "incomplete subscription"}, status_code=400)

    row = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    ).scalar_one_or_none()
    if row is None:
        row = PushSubscription(endpoint=endpoint)
        db.add(row)
    row.p256dh = p256dh
    row.auth = auth
    # Truncated hard: it is whatever the browser chose to say about itself.
    row.label = (request.headers.get("user-agent") or "")[:120]
    row.last_error = None
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/unsubscribe")
async def unsubscribe(request: Request, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "expected JSON"}, status_code=400)
    endpoint = (body.get("endpoint") or "").strip()
    row = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    ).scalar_one_or_none()
    if row is not None:
        db.delete(row)
        db.commit()
    return JSONResponse({"ok": True})


@router.post("/test")
def send_test(request: Request, db: Session = Depends(get_db)):
    """Send a notification to every subscribed device, to prove it works."""
    sent, failed = broadcast(
        title="Task Hub",
        body="Notifications are working. This is the only one you asked for.",
        url="/",
        tag="taskhub-test",
    )
    if not sent and not failed:
        deps.flash(
            request,
            "No devices are subscribed yet. Turn notifications on in this "
            "browser first — and on a phone, install Task Hub to the home "
            "screen before doing so.",
            "warning",
        )
    elif failed:
        deps.flash(
            request,
            f"Sent to {sent} device(s); {failed} could not be reached.",
            "warning",
        )
    else:
        deps.flash(request, f"Test notification sent to {sent} device(s).", "success")
    return deps.redirect("/settings")


@router.post("/categories")
def save_categories(
    request: Request,
    on_tasks: str = Form(""),
    on_sync: str = Form(""),
    on_expiring: str = Form(""),
    db: Session = Depends(get_db),
):
    """Choose which of the three kinds of notification to receive."""
    settings_store.set_bool(db, settings_store.PUSH_ON_TASKS, bool(on_tasks))
    settings_store.set_bool(db, settings_store.PUSH_ON_SYNC_FAILURE, bool(on_sync))
    settings_store.set_bool(db, settings_store.PUSH_ON_EXPIRING, bool(on_expiring))
    db.commit()
    chosen = sum(bool(x) for x in (on_tasks, on_sync, on_expiring))
    if chosen:
        deps.flash(request, f"Saved. {chosen} kind(s) of notification are on.", "success")
    else:
        deps.flash(
            request,
            "Saved. Nothing will be sent — the test button still works, so you "
            "can check the connection without turning any of these on.",
            "info",
        )
    return deps.redirect("/settings#notifications")


# --- Sending ------------------------------------------------------------------

#: Which setting governs each kind of notification, and what it is called on
#: the page. Kept together so adding a fourth means touching one place.
CATEGORIES: dict[str, tuple[str, str]] = {
    "tasks": (settings_store.PUSH_ON_TASKS, "Tasks due"),
    "sync": (settings_store.PUSH_ON_SYNC_FAILURE, "Sync failures"),
    "expiring": (settings_store.PUSH_ON_EXPIRING, "Expiring sign-ins"),
}


def category_enabled(session: Session, category: str) -> bool:
    """Whether this kind of notification is wanted at all."""
    if not settings_store.get_bool(session, settings_store.PUSH_ENABLED):
        return False
    key = CATEGORIES.get(category, (None, ""))[0]
    # An unknown category is always allowed: it is only ever the test message,
    # which the user asked for by pressing a button.
    return True if key is None else settings_store.get_bool(session, key)


def broadcast(
    title: str,
    body: str,
    url: str = "/",
    tag: str = "taskhub",
    category: str | None = None,
) -> tuple[int, int]:
    """Send one notification to every subscribed device.

    Returns how many succeeded and how many did not. Subscriptions the push
    service reports as dead are deleted rather than retried: a browser that has
    been reinstalled is never coming back, and keeping the row means a failure
    on every notification from now on.

    Never raises. Nothing in Task Hub should fail because a notification could
    not be delivered.
    """
    sent = failed = 0
    with session_scope() as session:
        if category is not None and not category_enabled(session, category):
            return 0, 0
        if not settings_store.get_bool(session, settings_store.PUSH_ENABLED):
            return 0, 0
        private, public = keypair(session)
        rows = list(session.execute(select(PushSubscription)).scalars())
        if not rows:
            return 0, 0

        message = {"title": title, "body": body, "url": url, "tag": tag}
        for row in rows:
            try:
                webpush.send(row.endpoint, row.p256dh, row.auth, message, private, public)
            except webpush.PushGone:
                logger.info("Forgetting a push subscription the service says is gone")
                session.delete(row)
                failed += 1
            except Exception as exc:  # noqa: BLE001 - never fatal
                logger.warning("Push failed: %s", exc)
                row.last_error = str(exc)[:400]
                failed += 1
            else:
                from app.db.models import utcnow

                row.last_sent_at = utcnow()
                row.last_error = None
                sent += 1
        session.commit()
    return sent, failed


def subscriptions(session: Session) -> list[PushSubscription]:
    return list(session.execute(select(PushSubscription)).scalars())
