"""Shared behaviour for disconnecting an account.

Five different pages disconnect an account -- OAuth services, Google, the
password services, the slot editor and Obsidian -- and every one of them should
offer the same choice about what happens to the items that account brought in,
and say the same thing about it afterwards. Doing that in five places would
guarantee they drifted, and a disconnect that deletes data on one page and not
on another is exactly the sort of inconsistency nobody notices until something
is gone.
"""

from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy.orm import Session

from app.db.models import Account
from app.sync.cleanup import plan_for_accounts, remove_orphans
from app.web import deps

logger = logging.getLogger(__name__)


def wants_cleanup(value: str) -> bool:
    """Whether the form asked for the items to go too."""
    return value == "1"


def disconnect_accounts(
    request: Request,
    db: Session,
    accounts: list[Account],
    remove_items: bool,
    note: str = "",
) -> None:
    """Delete accounts, optionally removing what they leave behind.

    Flashes the outcome. The caller is left to redirect, because where you go
    back to differs from page to page.

    The order here matters. ``item_links`` cascade away with the account, so
    working out what is orphaned has to happen *before* the account row goes --
    afterwards there is nothing left to say which items were ever its.
    """
    if not accounts:
        return

    account_ids = [a.id for a in accounts]
    labels = ", ".join(a.label or f"slot {a.slot}" for a in accounts)
    removed_note = ""

    if remove_items:
        from app.web.radicale_admin import get_radicale_client

        result = remove_orphans(db, account_ids, get_radicale_client(db))
        if result.errors and not result.removed:
            # Nothing was removed and something is wrong; leave the account in
            # place so the user can try again rather than losing the link that
            # says which items were its.
            for message in result.errors[:3]:
                deps.flash(request, message, "error")
            db.rollback()
            return

        bits = []
        if result.removed:
            bits.append(f"removed {result.removed} item{'' if result.removed == 1 else 's'}")
        if result.plan.kept_shared:
            bits.append(f"kept {result.plan.kept_shared} still held by another service")
        if result.plan.kept_local:
            bits.append(f"kept {result.plan.kept_local} you created here")
        if result.failed:
            bits.append(f"{result.failed} could not be removed")
        if bits:
            removed_note = " — " + ", ".join(bits) + "."
        for message in result.errors[:3]:
            deps.flash(request, message, "warning")

    for account in accounts:
        db.delete(account)
    db.commit()

    tail = removed_note or (
        " Nothing was removed from your collections."
        if not remove_items else " Nothing needed removing."
    )
    deps.flash(request, f"Disconnected {labels}.{tail}{(' ' + note) if note else ''}",
               "success")


def cleanup_preview(db: Session, accounts: list[Account]) -> dict:
    """What the page should say next to the "also remove" option."""
    if not accounts:
        return {"total": 0, "summary": "", "kept_shared": 0, "kept_local": 0}
    plan = plan_for_accounts(db, [a.id for a in accounts])
    return {
        "total": plan.total,
        "summary": plan.describe(),
        "tasks": plan.tasks,
        "events": plan.events,
        "kept_shared": plan.kept_shared,
        "kept_local": plan.kept_local,
    }
