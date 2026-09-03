"""Removing what an account left behind, when it is disconnected.

Disconnecting an account unlinks it, but the tasks and events it put into your
collections stay exactly where they are. That is the right default -- a
disconnect should never quietly destroy data -- but it leaves orphans: items
nothing upstream owns any more, which no sync will ever update again and which
accumulate every time a service is tried and dropped.

This offers the other choice, and the whole design is about being conservative
in what it will delete.

**Nothing is ever deleted from the service itself.** Disconnecting Google and
tidying up locally must not remove a single task from Google. Everything here
works on Task Hub's own database and its Radicale collections.

**An item another service still holds is kept.** A task in Google, Todoist and
Radicale is not orphaned by Google leaving -- Todoist still owns it. Only items
that would be left with nothing but the local copy are candidates.

**An item you created here is kept.** A task written in Task Hub and pushed out
to Google is still your task after Google goes. It is not "left behind by
Google", so it is never removed on Google's account.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Account, CollectionKind, Item, ItemLink, RadicaleCollection, ServiceKind,
    SyncGroup,
)
from app.services.caldav_client import CalDAVError

logger = logging.getLogger(__name__)


@dataclass
class CleanupPlan:
    """What tidying up after these accounts would actually do."""

    #: Items that would be removed, as (item id, uid, kind, collection_id).
    doomed: list[tuple[int, str, CollectionKind, str | None]] = field(default_factory=list)
    #: Kept because another connected service still holds them.
    kept_shared: int = 0
    #: Kept because they were created in Task Hub rather than by this service.
    kept_local: int = 0

    @property
    def total(self) -> int:
        return len(self.doomed)

    @property
    def tasks(self) -> int:
        return sum(1 for _, _, kind, _ in self.doomed if kind == CollectionKind.TASKS)

    @property
    def events(self) -> int:
        return sum(1 for _, _, kind, _ in self.doomed if kind == CollectionKind.CALENDAR)

    def describe(self) -> str:
        """One line a person can read before agreeing to it."""
        if not self.doomed:
            return "Nothing would be removed from your collections."
        parts = []
        if self.tasks:
            parts.append(f"{self.tasks} task{'' if self.tasks == 1 else 's'}")
        if self.events:
            parts.append(f"{self.events} event{'' if self.events == 1 else 's'}")
        return " and ".join(parts)


def plan_for_accounts(db: Session, account_ids: list[int]) -> CleanupPlan:
    """Work out what removing these accounts would leave orphaned.

    Reads only. Call it to show someone the number before they commit to it,
    and again inside the deletion so the two can never disagree.
    """
    plan = CleanupPlan()
    if not account_ids:
        return plan

    # Which items these accounts touch at all.
    item_ids = set(
        db.execute(
            select(ItemLink.item_id).where(ItemLink.account_id.in_(account_ids))
        ).scalars()
    )
    if not item_ids:
        return plan

    # Radicale is Task Hub's own copy, not an upstream owner: an item held only
    # there is precisely what "orphaned" means, so it does not count as another
    # service keeping the item alive.
    local_account_ids = set(
        db.execute(
            select(Account.id).where(Account.service == ServiceKind.RADICALE)
        ).scalars()
    )
    ignored = set(account_ids) | local_account_ids

    # Where each group writes, so the Radicale copy can be found.
    collection_of: dict[int, str | None] = {}
    for group in db.execute(select(SyncGroup)).scalars():
        collection = (
            db.get(RadicaleCollection, group.radicale_collection_id)
            if group.radicale_collection_id else None
        )
        collection_of[group.id] = collection.collection_id if collection else None

    for item_id in item_ids:
        item = db.get(Item, item_id)
        if item is None:
            continue

        survivors = db.execute(
            select(ItemLink.account_id).where(
                ItemLink.item_id == item_id,
                ItemLink.account_id.notin_(ignored),
            )
        ).scalars().first()
        if survivors is not None:
            plan.kept_shared += 1
            continue

        if item.origin_service == ServiceKind.LOCAL:
            plan.kept_local += 1
            continue

        plan.doomed.append(
            (item.id, item.uid, item.kind, collection_of.get(item.sync_group_id))
        )

    return plan


@dataclass
class CleanupResult:
    removed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    plan: CleanupPlan = field(default_factory=CleanupPlan)


def remove_orphans(db: Session, account_ids: list[int], client) -> CleanupResult:
    """Delete the items these accounts would leave behind.

    ``client`` is the Radicale client, or None when Radicale is unreachable --
    in which case nothing is deleted at all rather than half of it, because a
    database row removed without its collection entry is a worse orphan than
    the one being cleaned up.
    """
    plan = plan_for_accounts(db, account_ids)
    result = CleanupResult(plan=plan)
    if not plan.doomed:
        return result

    if client is None:
        result.errors.append(
            "Radicale is not reachable, so nothing was removed. The accounts "
            "were left connected; try again once it is back."
        )
        result.failed = len(plan.doomed)
        return result

    for item_id, uid, kind, collection_id in plan.doomed:
        if collection_id:
            try:
                client.delete_record(collection_id, uid, kind)
            except CalDAVError as exc:
                # Leave the database row alone so the item stays consistent and
                # can be tried again, rather than losing track of a file that
                # is still sitting in the collection.
                result.failed += 1
                result.errors.append(f"Could not remove {uid} from {collection_id}: {exc}")
                continue

        item = db.get(Item, item_id)
        if item is not None:
            # No tombstone. A tombstone exists to stop a deletion being undone
            # by a service that still has the item -- and by definition none
            # does, so one here would only be a note to nobody.
            db.delete(item)
        result.removed += 1

    db.flush()
    logger.info(
        "Cleaned up %s orphaned item(s) from accounts %s", result.removed, account_ids
    )
    return result


def plan_for_service(db: Session, service: ServiceKind) -> tuple[list[int], CleanupPlan]:
    """The same, for every account of one service at once."""
    account_ids = list(
        db.execute(select(Account.id).where(Account.service == service)).scalars()
    )
    return account_ids, plan_for_accounts(db, account_ids)
