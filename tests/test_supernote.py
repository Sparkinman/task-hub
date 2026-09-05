"""Reading the Supernote To-Do app, and the one mistake that would have hurt.

Every payload here is the real shape returned by a live account, field names
and value encodings included -- their booleans really are the strings "Y" and
"N", their timestamps really are epoch milliseconds, and `importance` really
does come back null.

The test that matters most is the completedTime one. On the account this
connector was built against, all six tasks carried a completedTime while every
one of them reported status "needsAction". A connector that read completion
from that field would have marked the lot done and pushed it out to every other
connected service on the first sync -- silent, immediate, and hard to trace
back. It is checked here so nobody later "fixes" the apparent oversight of
ignoring a field that is sitting right there.
"""

from __future__ import annotations

import datetime as dt
import sys

from app.connectors.base import F_DUE_TIME, F_TITLE
from app.connectors.supernote import (
    STATUS_FROM_REMOTE,
    SupernoteConnector,
    from_epoch_ms,
    to_epoch_ms,
    token_expiry,
    yes,
)
from app.db.models import CollectionKind, ItemStatus, ServiceKind

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def connector() -> SupernoteConnector:
    return SupernoteConnector(1, {"token": "x.y.z", "email": "someone@example.com"}, {})


print("\nTheir booleans are the strings Y and N")
for value, expected in [("Y", True), ("y", True), ("N", False), ("n", False),
                        ("", False), (None, False), ("Yes", False)]:
    check(f"yes({value!r}) is {expected}", yes(value) is expected)

print("\nTheir timestamps are epoch milliseconds")
check(
    "a real lastModified converts",
    from_epoch_ms(1787698909090) == dt.datetime(2026, 8, 25, 23, 1, 49, 90000, tzinfo=dt.UTC),
    str(from_epoch_ms(1787698909090)),
)
# Zero means "not set" in this API, not the first instant of 1970. Showing
# somebody a task due in 1970 would be worse than showing no date at all.
for empty in (0, -1, None, "", "1787698909090"):
    check(f"{empty!r} reads as no time at all", from_epoch_ms(empty) is None)
check(
    "and the conversion round-trips",
    to_epoch_ms(from_epoch_ms(1787698909090)) == 1787698909090,
)
check("a naive datetime is assumed to be UTC",
      to_epoch_ms(dt.datetime(2026, 8, 25, 23, 1, 49, 90000)) == 1787698909090)

print("\nThe session token declares its own expiry")
# A real token's payload: {"createTime":..., "equipmentNo":..., "exp":..., "userId":...}
import base64  # noqa: E402
import json  # noqa: E402


def jwt_with(payload: dict) -> str:
    encode = lambda raw: base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")  # noqa: E731
    return f"{encode({'typ': 'JWT', 'alg': 'HS256'})}.{encode(payload)}.signature"


expiry = dt.datetime(2026, 10, 5, 12, 41, 42, tzinfo=dt.UTC)
check(
    "expiry is read out of the token",
    token_expiry(jwt_with({"exp": int(expiry.timestamp()), "userId": 1})) == expiry,
)
# Unknown must never be reported as expired: that would nag forever about an
# account that works perfectly.
for bad in ("", "not-a-jwt", "a.b", "a.!!!.c", jwt_with({"userId": 1}),
            jwt_with({"exp": 0}), jwt_with({"exp": "soon"})):
    check(f"{bad[:18]!r} gives no expiry rather than a wrong one",
          token_expiry(bad) is None)

print("\ncompletedTime does NOT mean completed")
# Exactly what the live account returned: a completedTime on every task, and
# needsAction on every task. Believing the timestamp marks them all done.
live_row = {
    "taskId": "abc123", "taskListId": "list-1", "title": "BMW R1200c To Do",
    "detail": None, "lastModified": 1787698909090, "recurrence": None,
    "isReminderOn": "N", "status": "needsAction", "importance": None,
    "dueTime": 0, "completedTime": 1784054191329, "links": "", "isDeleted": "N",
}
record = connector()._record_from(live_row, "abc123")
check("status is read from `status`, not from completedTime",
      record.status == ItemStatus.NEEDS_ACTION, record.status.value)
check("and no completion date is invented for an open task",
      record.completed_at is None, str(record.completed_at))

done_row = dict(live_row, status="completed")
done = connector()._record_from(done_row, "abc123")
check("a genuinely completed task is completed",
      done.status == ItemStatus.COMPLETED, done.status.value)
check("and only then is completedTime used to date it",
      done.completed_at == from_epoch_ms(1784054191329), str(done.completed_at))

print("\nA task converts into Task Hub's own shape")
check("the title carries over", record.title == "BMW R1200c To Do", record.title)
check("an empty detail becomes no notes, not an empty string",
      record.notes is None, repr(record.notes))
check("notes carry when present",
      connector()._record_from(dict(live_row, detail="  ring Jason  "), "x").notes
      == "ring Jason")
check("the origin is stamped as Supernote",
      record.origin_service == ServiceKind.SUPERNOTE)
check("dueTime of 0 is no due date", record.due_date is None, str(record.due_date))

dated = connector()._record_from(dict(live_row, dueTime=1787698909090), "x")
check("a real dueTime becomes a date", dated.due_date == dt.date(2026, 8, 25),
      str(dated.due_date))
check("but never a time of day", dated.due_time is None, str(dated.due_time))

print("\nThe connector is honest about what it cannot do")
capabilities = connector().capabilities(CollectionKind.TASKS)
check("it can hold a title", capabilities.supports(F_TITLE))
# The API stores an instant, but the To-Do app only ever offers a date. A time
# read back is an artefact of the encoding, and must not overwrite a real time
# set in Todoist or Google.
check("it declares no due time", not capabilities.supports(F_DUE_TIME))
check("it cannot create", capabilities.can_create is False)
check("it cannot delete", capabilities.can_delete is False)
check("and nothing is writable", capabilities.push_fields() == frozenset())
check("tasks only, not calendars",
      connector().supports_kind(CollectionKind.TASKS)
      and not connector().supports_kind(CollectionKind.CALENDAR))

print("\nWrites refuse clearly rather than failing silently")
for outcome in (
    connector().create("list-1", record, CollectionKind.TASKS),
    connector().update("list-1", "abc123", record, CollectionKind.TASKS),
    connector().delete("list-1", "abc123", CollectionKind.TASKS),
):
    check("a write reports an error", outcome.ok is False)
    check("and explains why", "never been exercised" in (outcome.error or ""))

print("\nTheir status vocabulary matches Google's, which is why it maps cleanly")
check("needsAction", STATUS_FROM_REMOTE["needsAction"] == ItemStatus.NEEDS_ACTION)
check("completed", STATUS_FROM_REMOTE["completed"] == ItemStatus.COMPLETED)
check("an unknown status falls back to needs-action rather than raising",
      connector()._record_from(dict(live_row, status="wat"), "x").status
      == ItemStatus.NEEDS_ACTION)

print("\nA task belonging to no list is offered rather than dropped")
# The live account had exactly this: one task with taskListId null, sitting in
# the app's "All" view and in none of its four lists. Filtering tasks by list --
# the obvious implementation, and the one written first -- lost it in silence.
from app.connectors.supernote import (  # noqa: E402
    LIST_GROUPS, LIST_TASKS, UNFILED_LIST_ID,
)

GROUPS = {"scheduleTaskGroup": [
    {"taskListId": "1", "title": "Tasks", "isDeleted": "N"},
    {"taskListId": "9bc99c7d", "title": "Work", "isDeleted": "N"},
    {"taskListId": "deadlist", "title": "Old", "isDeleted": "Y"},
]}
TASKS = {"scheduleTask": [
    dict(live_row, taskId="t1", taskListId="1", title="Filed"),
    dict(live_row, taskId="t2", taskListId=None, title="WPRD3- Place Order Today"),
    dict(live_row, taskId="t3", taskListId="deadlist", title="In a deleted list"),
    dict(live_row, taskId="t4", taskListId="1", title="Also filed", isDeleted="Y"),
]}


class FakeApi(SupernoteConnector):
    """The connector with its one network call replaced, nothing else."""

    def __init__(self):
        super().__init__(1, {"token": "x.y.z"}, {})
        self.calls = []

    def _get(self, path, payload=None):
        self.calls.append(path)
        return {LIST_GROUPS: GROUPS, LIST_TASKS: TASKS}[path]


lists = FakeApi().list_remote_lists()
names = [entry.name for entry in lists]
check("deleted lists are not offered", "Old" not in names, str(names))
check("real lists are offered", {"Tasks", "Work"} <= set(names), str(names))
check("and an unfiled list appears because something is in it",
      UNFILED_LIST_ID in [entry.remote_id for entry in lists], str(names))

filed = FakeApi().pull("1", CollectionKind.TASKS)
check("a normal list returns only its own live tasks",
      [i.record.title for i in filed.items if not i.deleted] == ["Filed"],
      str([i.record.title for i in filed.items]))

unfiled = FakeApi().pull(UNFILED_LIST_ID, CollectionKind.TASKS)
titles = sorted(i.record.title for i in unfiled.items)
# Both kinds of orphan: no list at all, and a list that has been deleted.
# Neither is reachable from any other list, so both belong here.
check("the listless task is reachable",
      "WPRD3- Place Order Today" in titles, str(titles))
check("so is a task whose list was deleted",
      "In a deleted list" in titles, str(titles))
check("and filed tasks are not duplicated into it",
      "Filed" not in titles, str(titles))

# The whole point: nothing an account holds may be invisible to every list.
every = set()
for entry in FakeApi().list_remote_lists():
    every |= {i.record.title for i in FakeApi().pull(entry.remote_id, CollectionKind.TASKS).items}
check("every task on the account is reachable from some list",
      every == {row["title"] for row in TASKS["scheduleTask"]},
      str(sorted(every)))


class NoOrphans(FakeApi):
    def _get(self, path, payload=None):
        if path == LIST_TASKS:
            return {"scheduleTask": [dict(live_row, taskId="t1", taskListId="1")]}
        return GROUPS


check("a tidy account is not shown a puzzling empty unfiled list",
      UNFILED_LIST_ID not in [e.remote_id for e in NoOrphans().list_remote_lists()])

if _failures:
    print(f"\n{len(_failures)} check(s) failed.")
    sys.exit(1)
print("\nAll Supernote connector tests passed.")
