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

from app.connectors.base import F_DUE_DATE, F_DUE_TIME, F_NOTES, F_STATUS, F_TITLE
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
check("tasks only, not calendars",
      connector().supports_kind(CollectionKind.TASKS)
      and not connector().supports_kind(CollectionKind.CALENDAR))

print("\nWrites use the right verb, which is not obvious for any of the three")
# Each verb on /file/schedule/task behaves in a way the obvious implementation
# gets wrong, and the worst of them fails silently. These were established
# against a live account, so the checks are about not regressing them.
from app.connectors.base import ConnectorGoneError  # noqa: E402
from app.connectors.supernote import (  # noqa: E402
    LIST_GROUPS, LIST_TASKS, TASK, UNFILED_LIST_ID,
)
from app.services.ical_model import CanonicalRecord  # noqa: E402

#: The lists the recorder reports, kept beside it rather than shared with the
#: unfiled-list fixtures further down: those are built for a different question
#: and a write test that quietly depended on them would be hard to follow.
RECORDER_GROUPS = {"scheduleTaskGroup": [
    {"taskListId": "1", "title": "Tasks", "isDeleted": "N"},
]}

LIVE_ROW = dict(live_row, taskId="abc123", taskListId="1", sort=7, planerSort=3,
                isReminderOn="Y", recurrence="FREQ=WEEKLY")


class Recorder(SupernoteConnector):
    """Records the calls a write makes instead of making them."""

    def __init__(self, rows=None):
        super().__init__(1, {"token": "x.y.z"}, {})
        self.calls = []
        self._rows = rows if rows is not None else [LIVE_ROW]

    def _request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == LIST_TASKS:
            return {"scheduleTask": self._rows}
        if path == LIST_GROUPS:
            return RECORDER_GROUPS
        return {"success": True, "taskId": "new999"}


fresh = CanonicalRecord(uid="u", kind=CollectionKind.TASKS, title="Write me",
                        notes="notes", due_date=dt.date(2026, 12, 25))

r = Recorder()
outcome = r.create("1", fresh, CollectionKind.TASKS)
method, path, payload = r.calls[-1]
check("create posts", (method, path) == ("POST", TASK), f"{method} {path}")
check("create returns the new id", outcome.remote_id == "new999", str(outcome.remote_id))
check("create names the list", payload.get("taskListId") == "1", str(payload))
check("a bare date is written as an instant", isinstance(payload.get("dueTime"), int))
check("and it round-trips to the same date",
      from_epoch_ms(payload["dueTime"]).date() == dt.date(2026, 12, 25),
      str(payload.get("dueTime")))

r = Recorder()
r.update("1", "abc123", fresh, CollectionKind.TASKS)
method, path, payload = r.calls[-1]
check("update PUTs, never POSTs", method == "PUT", method)
# POST inserts even with a taskId in the body, so an update that fell back to it
# would silently make a second copy of the task instead of failing.
check("update never falls back to POST",
      not any(m == "POST" and p == TASK for m, p, _ in r.calls),
      str([(m, p) for m, p, _ in r.calls]))
# PUT is refused outright without this, and the error blames the list.
check("update supplies a fresh lastModified",
      isinstance(payload.get("lastModified"), int) and payload["lastModified"] > 0,
      str(payload.get("lastModified")))
# Fields Supernote maintains for itself must survive an edit rather than being
# blanked by a body that only carries what Task Hub knows about.
check("the server's own fields are preserved", payload.get("sort") == 7
      and payload.get("planerSort") == 3 and payload.get("recurrence") == "FREQ=WEEKLY",
      str({k: payload.get(k) for k in ("sort", "planerSort", "recurrence")}))
check("and the edit is applied over them", payload.get("title") == "Write me")

done_record = CanonicalRecord(uid="u", kind=CollectionKind.TASKS, title="Done",
                              status=ItemStatus.COMPLETED,
                              completed_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))
r = Recorder()
r.update("1", "abc123", done_record, CollectionKind.TASKS)
_, _, payload = r.calls[-1]
check("completing sends their word for it", payload.get("status") == "completed")
check("with the moment it happened",
      payload.get("completedTime") == to_epoch_ms(dt.datetime(2026, 9, 1, tzinfo=dt.UTC)))

r = Recorder()
r.delete("1", "abc123", CollectionKind.TASKS)
method, path, payload = r.calls[-1]
# As a body or a query parameter this answers 500; the id has to be in the path.
check("delete puts the id in the path",
      (method, path) == ("DELETE", f"{TASK}/abc123"), f"{method} {path}")
check("and sends no body", payload is None, str(payload))

print("\nA task written on a page of a notebook keeps its way back")
# The tablet shows a notebook icon on these that jumps to the page. The field
# is base64 around JSON, and it must survive the round trip -- an update sends
# the server's row with only Task Hub's fields laid over it, so it does.
from app.connectors.supernote import note_link  # noqa: E402

REAL = ("eyJhcHBOYW1lIjoibm90ZSIsImZpbGVJZCI6IkYyMDI2MDgyMTA0MTAxNjM4OTA0MmNIVVdr"
        "UUZuVVBNQiIsImZpbGVQYXRoIjoiL3N0b3JhZ2UvZW11bGF0ZWQvMC9Ob3RlLzIwMjYwODIx"
        "XzA0MTAxMy5ub3RlIiwicGFnZSI6MiwicGFnZUlkIjoiUDIwMjYwOTA0MTMwNTIwMjAyNTg0"
        "ZU9sOHlWd3hBV2NTIn0=")
link = note_link(REAL)
check("the notebook is named", link["name"] == "20260821_041013.note", str(link))
check("and the page", link["page"] == 2, str(link))
for junk in ("", None, "not base64", "eyJhIjoxfQ==", 12345):
    check(f"{str(junk)[:14]!r} yields nothing rather than raising",
          note_link(junk) is None)

linked = connector()._record_from(dict(live_row, links=REAL), "abc123")
check("the reference reaches the task",
      "20260821_041013.note" in (linked.notes or ""), repr(linked.notes))
check("with the page number", "page 2" in (linked.notes or ""), repr(linked.notes))
# A task with its own notes keeps them; the reference is added, not substituted.
both = connector()._record_from(
    dict(live_row, links=REAL, detail="ring Jason"), "abc123")
check("existing notes are not replaced", "ring Jason" in (both.notes or ""),
      repr(both.notes))
check("and the reference is there too", "page 2" in (both.notes or ""), repr(both.notes))
# An update must send the server's own row, or the link is blanked by omission.
r = Recorder(rows=[dict(LIVE_ROW, links=REAL)])
r.update("1", "abc123", fresh, CollectionKind.TASKS)
_, _, payload = r.calls[-1]
check("an edit preserves the link", payload.get("links") == REAL, str(payload.get("links"))[:40])

print("\nA task read from Supernote is never written back into Supernote")
# Reachable through the unfiled view: a task belonging to no list is read from
# it, and any real list of the same account that is a write-back target has no
# link for that task -- so the engine creates one, leaving the original outside
# every list and a copy inside one. It happened to a live account.
already_there = CanonicalRecord(uid="supernote-abc123", kind=CollectionKind.TASKS,
                                title="WPRD3- Place Order Today")
r = Recorder()
outcome = r.create("1", already_there, CollectionKind.TASKS)
check("creating it again is declined", outcome.skipped is True, str(outcome))
# Declined, not failed. It happens on every pass for every task read from
# another Supernote list in the same collection -- as an error that made a
# perfectly healthy sync report dozens of failures a minute.
check("and it is not an error", outcome.ok is True and outcome.error is None,
      str(outcome.error))
check("nothing was sent", not any(m == "POST" and p == TASK for m, p, _ in r.calls),
      str([(m, p) for m, p, _ in r.calls]))
# A task that genuinely originated elsewhere still writes normally.
check("a task from anywhere else still writes",
      Recorder().create("1", fresh, CollectionKind.TASKS).ok is True)

print("\nUpdating a task that has gone refuses rather than duplicating it")
r = Recorder(rows=[])
try:
    r.update("1", "vanished", fresh, CollectionKind.TASKS)
    check("a vanished task raises", False, "no error raised")
except ConnectorGoneError:
    check("a vanished task raises", True)
check("and nothing was written",
      not any(m in ("POST", "PUT") and p == TASK for m, p, _ in r.calls),
      str([(m, p) for m, p, _ in r.calls]))

print("\nThe unfiled list is a view, so nothing can be created in it")
outcome = Recorder().create(UNFILED_LIST_ID, fresh, CollectionKind.TASKS)
check("creating there is refused", outcome.ok is False)
check("with a reason that says what to do instead",
      "Map a real Supernote list" in (outcome.error or ""), str(outcome.error))

print("\nIt now declares itself writable")
capabilities = connector().capabilities(CollectionKind.TASKS)
check("it can create", capabilities.can_create is True)
check("it can delete", capabilities.can_delete is True)
check("and writes the four fields it can hold",
      capabilities.push_fields() == frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE}),
      str(sorted(capabilities.push_fields())))

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
# Real lists accept write-back; the unfiled view cannot, because a task written
# there would have no list to go in. Marking it read-only stops the engine
# offering it as a target and then failing on every single pass.
by_id = {e.remote_id: e for e in lists}
check("real lists are writable", by_id["1"].read_only is False)
check("the unfiled view is not", by_id[UNFILED_LIST_ID].read_only is True)

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
# The unfiled list is a view, so a task leaving it has usually been filed rather
# than deleted. Reporting the pull as complete would let the engine read that
# absence as a deletion and propagate it to every other service -- deleting a
# task because the user tidied it up.
check("a real list reports completely, so absence means deletion",
      FakeApi().pull("1", CollectionKind.TASKS).incremental is False)
check("but the unfiled view never lets absence imply deletion",
      FakeApi().pull(UNFILED_LIST_ID, CollectionKind.TASKS).incremental is True)

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

print("\nThe expiry warning arrives before syncing stops, not after")
# There is no way to renew a Supernote session in the background, so the only
# cure is a person signing in again. A warning that arrived when it broke would
# be no use at all; the whole point is to say so while it still works.
from app.crypto import encrypt_json  # noqa: E402
from app.db.models import Account, AccountStatus  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402
from app.web.supernote_setup import expiring_accounts  # noqa: E402

init_db()


def account_expiring_in(session, slot: int, days: float | None):
    token = "" if days is None else jwt_with({
        "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(days=days)).timestamp()),
        "userId": 1,
    })
    account = Account(
        service=ServiceKind.SUPERNOTE, slot=slot, status=AccountStatus.CONNECTED,
        credentials=encrypt_json({"token": token, "email": f"slot{slot}@example.com"}),
    )
    session.add(account)
    session.flush()
    return account


with session_scope() as session:
    account_expiring_in(session, 71, 29)      # fresh
    account_expiring_in(session, 72, 3)       # due soon
    account_expiring_in(session, 73, -2)      # already gone
    account_expiring_in(session, 74, None)    # unreadable token
    session.flush()

    warned = {row["slot"]: row for row in expiring_accounts(session, within_days=7)}
    check("a fresh session is not nagged about", 71 not in warned, str(sorted(warned)))
    check("one running out soon is", 72 in warned, str(sorted(warned)))
    check("and it says how many days are left", warned.get(72, {}).get("days") == 3,
          str(warned.get(72, {}).get("days")))
    check("an expired one is reported as expired",
          73 in warned and warned[73]["expired"] is True, str(warned.get(73)))
    check("one still in date is not called expired",
          warned.get(72, {}).get("expired") is False, str(warned.get(72)))
    # A token that cannot be read means "unknown", never "expired". Guessing the
    # other way would nag forever about an account that works perfectly.
    check("an unreadable token is left alone", 74 not in warned, str(sorted(warned)))
    check("the address is carried so the warning can name it",
          warned.get(72, {}).get("email") == "slot72@example.com",
          str(warned.get(72, {}).get("email")))
    session.rollback()

print("\nA notebook link is only offered when the notebook is actually here")
# The rule that matters: a link that goes nowhere is worse than no link. It
# invites a tap, spends a moment loading and answers with an error for
# something that was never wrong.
from app.web.note_links import reference_in  # noqa: E402

for text, expected in [
    ("From 20260821_041013.note, page 2", ("20260821_041013.note", 2)),
    ("From Recipes.note", ("Recipes.note", None)),
    ("ring Jason\n\nFrom Can Am.note, page 7", ("Can Am.note", 7)),
    ("From Bathroom To Do's.note, page 1", ("Bathroom To Do's.note", 1)),
    # Prose somebody typed must not be mistaken for a reference.
    ("a note about the note I wrote", None),
    ("From the meeting", None),
    ("", None),
    (None, None),
]:
    check(f"{str(text)[:30]!r} parses to {expected}",
          reference_in(text) == expected, str(reference_in(text)))

if _failures:
    print(f"\n{len(_failures)} check(s) failed.")
    sys.exit(1)
print("\nAll Supernote connector tests passed.")
