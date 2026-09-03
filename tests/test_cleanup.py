"""Tests for removing what a disconnected account leaves behind.

This deletes people's tasks, so the tests are mostly about what it *refuses* to
delete. Three rules have to hold, and each of them is the difference between
tidying up and losing work:

* an item another connected service still holds is never removed;
* an item created in Task Hub is never removed on some service's account;
* Radicale holding a copy does not count as "another service has it" -- being
  held only there is exactly what an orphan is.
"""

from __future__ import annotations

import datetime as dt
import sys

from app.db.models import (
    Account, AccountStatus, CollectionKind, Item, ItemLink, ItemStatus,
    RadicaleCollection, ServiceKind, SyncGroup,
)
from app.db.session import init_db, session_scope
from app.sync.cleanup import plan_for_accounts, remove_orphans

UTC = dt.timezone.utc
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


class FakeRadicale:
    """Records what it was asked to delete; never fails."""

    def __init__(self):
        self.deleted: list[tuple[str, str]] = []

    def delete_record(self, collection_id, uid, kind=None):
        self.deleted.append((collection_id, uid))
        return True


class BrokenRadicale:
    def delete_record(self, collection_id, uid, kind=None):
        from app.services.caldav_client import CalDAVError

        raise CalDAVError("the server is down")


init_db()

with session_scope() as s:
    collection = RadicaleCollection(
        collection_id="tasks", display_name="Tasks",
        kind=CollectionKind.TASKS, radicale_user="paul",
    )
    s.add(collection)
    s.flush()

    group = SyncGroup(name="Work", kind=CollectionKind.TASKS, enabled=True,
                      radicale_collection_id=collection.id)
    s.add(group)

    radicale = Account(service=ServiceKind.RADICALE, slot=1, label="Local",
                       enabled=True, status=AccountStatus.CONNECTED)
    google = Account(service=ServiceKind.GOOGLE, slot=1, label="Google",
                     enabled=True, status=AccountStatus.CONNECTED)
    todoist = Account(service=ServiceKind.TODOIST, slot=1, label="Todoist",
                      enabled=True, status=AccountStatus.CONNECTED)
    s.add_all([radicale, google, todoist])
    s.flush()

    def item(uid, origin, links):
        row = Item(uid=uid, kind=CollectionKind.TASKS, title=uid,
                   status=ItemStatus.NEEDS_ACTION, sync_group_id=group.id,
                   origin_service=origin)
        s.add(row)
        s.flush()
        for account in links:
            s.add(ItemLink(item_id=row.id, account_id=account.id,
                           sync_group_id=group.id, remote_id=f"{uid}-{account.id}"))
        s.flush()
        return row.id

    only_google = item("only-google", ServiceKind.GOOGLE, [radicale, google])
    shared = item("shared", ServiceKind.GOOGLE, [radicale, google, todoist])
    made_here = item("made-here", ServiceKind.LOCAL, [radicale, google])
    untouched = item("untouched", ServiceKind.TODOIST, [radicale, todoist])

    google_id, todoist_id = google.id, todoist.id

print("Planning what disconnecting Google would leave behind")

with session_scope() as s:
    plan = plan_for_accounts(s, [google_id])
    doomed = {uid for _, uid, _, _ in plan.doomed}

    check("an item only Google held is removed", "only-google" in doomed)
    check("an item Todoist still holds is kept", "shared" not in doomed)
    check("  ...and is counted as shared", plan.kept_shared == 1, str(plan.kept_shared))
    check("an item created in Task Hub is kept", "made-here" not in doomed)
    check("  ...and is counted as locally made", plan.kept_local == 1, str(plan.kept_local))
    check("an item Google never touched is not considered at all",
          "untouched" not in doomed)
    check("the Radicale copy does not count as another service holding it",
          "only-google" in doomed,
          "otherwise nothing would ever be cleanable")
    check("it describes itself readably", plan.describe() == "1 task", plan.describe())

print("\nDoing it")

with session_scope() as s:
    client = FakeRadicale()
    result = remove_orphans(s, [google_id], client)
    check("one item removed", result.removed == 1, str(result.removed))
    check("it was deleted from the collection too",
          client.deleted == [("tasks", "only-google")], str(client.deleted))
    check("no errors", not result.errors, str(result.errors))

with session_scope() as s:
    remaining = {i.uid for i in s.query(Item).all()}
    check("the orphan is gone from the database", "only-google" not in remaining)
    check("the shared item survived", "shared" in remaining)
    check("the locally made item survived", "made-here" in remaining)
    check("the untouched item survived", "untouched" in remaining)

print("\nWhen Radicale cannot be reached")

with session_scope() as s:
    result = remove_orphans(s, [todoist_id], None)
    check("nothing is removed without a working collection store",
          result.removed == 0)
    check("and it says why", bool(result.errors), str(result.errors))

with session_scope() as s:
    result = remove_orphans(s, [todoist_id], BrokenRadicale())
    check("a collection that refuses the delete leaves the row alone",
          result.removed == 0 and result.failed >= 1,
          f"removed={result.removed} failed={result.failed}")

with session_scope() as s:
    still = {i.uid for i in s.query(Item).all()}
    check("  ...so the item is still there to try again",
          "untouched" in still and "shared" in still)

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All cleanup tests passed.")
