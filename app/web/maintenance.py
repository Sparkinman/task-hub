"""Finding and repairing genuine database inconsistencies.

Deliberately not a "clean up the database" button. The most valuable table here
is ``item_links``, which records that a particular canonical task *is* a
particular Google task; delete a link while both sides still exist and the next
sync sees an unlinked remote item, falls back to matching on title, and can
create a duplicate. A blanket sweep would do exactly that on a bad day, and the
damage would surface days later as tasks mysteriously appearing twice.

So every check below is narrow, names precisely what it found, and only ever
removes rows whose parent has genuinely gone. Nothing here touches a row that
still points at something real, and nothing runs on its own -- each repair is a
separate deliberate click.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Account,
    Item,
    ItemLink,
    ListMapping,
    RadicaleCollection,
    RemoteList,
    SyncGroup,
    SyncLogEntry,
    SyncRun,
)

logger = logging.getLogger(__name__)


@dataclass
class Finding:
    """One kind of inconsistency, with enough context to decide about it."""

    key: str
    title: str
    #: What it means and why it is safe to remove. Shown next to the button.
    detail: str
    count: int


def _ids(db: Session, model) -> set[int]:
    return set(db.execute(select(model.id)).scalars())


# Each check returns the rows it would delete. Keeping "find" and "fix" as one
# function means the count shown to the user and the rows actually removed can
# never drift apart.
def _links_without_item(db: Session) -> list:
    return list(
        db.execute(
            select(ItemLink).where(ItemLink.item_id.notin_(select(Item.id)))
        ).scalars()
    )


def _links_without_account(db: Session) -> list:
    return list(
        db.execute(
            select(ItemLink).where(ItemLink.account_id.notin_(select(Account.id)))
        ).scalars()
    )


def _links_without_group(db: Session) -> list:
    return list(
        db.execute(
            select(ItemLink).where(
                ItemLink.sync_group_id.isnot(None),
                ItemLink.sync_group_id.notin_(select(SyncGroup.id)),
            )
        ).scalars()
    )


def _mappings_without_list(db: Session) -> list:
    return list(
        db.execute(
            select(ListMapping).where(
                ListMapping.remote_list_id.notin_(select(RemoteList.id))
            )
        ).scalars()
    )


def _mappings_without_group(db: Session) -> list:
    return list(
        db.execute(
            select(ListMapping).where(
                ListMapping.sync_group_id.notin_(select(SyncGroup.id))
            )
        ).scalars()
    )


def _items_without_group(db: Session) -> list:
    return list(
        db.execute(
            select(Item).where(
                Item.sync_group_id.isnot(None),
                Item.sync_group_id.notin_(select(SyncGroup.id)),
            )
        ).scalars()
    )


def _lists_without_account(db: Session) -> list:
    return list(
        db.execute(
            select(RemoteList).where(RemoteList.account_id.notin_(select(Account.id)))
        ).scalars()
    )


def _log_without_run(db: Session) -> list:
    return list(
        db.execute(
            select(SyncLogEntry).where(SyncLogEntry.run_id.notin_(select(SyncRun.id)))
        ).scalars()
    )


def _groups_without_collection(db: Session) -> list:
    return list(
        db.execute(
            select(SyncGroup).where(
                SyncGroup.radicale_collection_id.isnot(None),
                SyncGroup.radicale_collection_id.notin_(
                    select(RadicaleCollection.id)
                ),
            )
        ).scalars()
    )


#: key -> (title, explanation, finder)
#:
#: Ordered so that removing a parent happens before the check that would notice
#: its newly orphaned children -- lists go before the settings that configure
#: them, groups before the tasks inside them. Applying the whole set in this
#: order settles in one pass. Applying one on its own can still reveal another,
#: which is correct and shown on the page rather than swept up invisibly.
CHECKS: dict[str, tuple[str, str, Callable[[Session], list]]] = {
    "lists_no_account": (
        "Service lists belonging to a disconnected account",
        "Discovered from an account that has since been removed. Reconnecting "
        "that account discovers its lists again.",
        _lists_without_account,
    ),
    "groups_no_collection": (
        "Sync groups whose Radicale collection is gone",
        "The collection was deleted but the group configuring it remained. "
        "Nothing syncs through it.",
        _groups_without_collection,
    ),
    "items_no_group": (
        "Tasks belonging to a deleted collection",
        "These can never sync, because the collection that held them no longer "
        "exists. They are not visible anywhere in the interface.",
        _items_without_group,
    ),
    "links_no_item": (
        "Links pointing at a task that no longer exists",
        "Each records where a task used to live in a service. The task itself "
        "is gone from Task Hub, so the link can never be followed. Removing "
        "them changes nothing in any connected service.",
        _links_without_item,
    ),
    "links_no_account": (
        "Links belonging to a disconnected account",
        "Left behind when an account was removed. They refer to a service "
        "Task Hub can no longer reach. Safe to remove; reconnecting the account "
        "rebuilds what it needs.",
        _links_without_account,
    ),
    "links_no_group": (
        "Links pointing at a deleted collection",
        "The collection they belonged to is gone, so nothing will ever consult "
        "them again.",
        _links_without_group,
    ),
    "mappings_no_list": (
        "Sync settings for a list that no longer exists",
        "The service list they configured has gone. Removing them tidies the "
        "mapping tables and changes nothing that still works.",
        _mappings_without_list,
    ),
    "mappings_no_group": (
        "Sync settings pointing at a deleted collection",
        "Same again, from the other direction: the Radicale collection is gone.",
        _mappings_without_group,
    ),
    "log_no_run": (
        "Log lines with no sync run",
        "Detail rows whose parent run has been cleared. Harmless, just clutter.",
        _log_without_run,
    ),
}


def scan(db: Session) -> list[Finding]:
    """Every inconsistency currently present. Read-only."""
    findings: list[Finding] = []
    for key, (title, detail, finder) in CHECKS.items():
        try:
            rows = finder(db)
        except Exception:  # noqa: BLE001 - one broken check must not hide the rest
            logger.exception("Maintenance check %s failed", key)
            continue
        if rows:
            findings.append(Finding(key=key, title=title, detail=detail, count=len(rows)))
    return findings


def repair(db: Session, key: str) -> int:
    """Apply one named repair. Returns how many rows were removed."""
    entry = CHECKS.get(key)
    if entry is None:
        return 0
    _title, _detail, finder = entry
    rows = finder(db)
    for row in rows:
        db.delete(row)
    db.commit()
    if rows:
        logger.info("Repair %s removed %d row(s)", key, len(rows))
    return len(rows)
