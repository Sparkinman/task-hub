"""Todoist and TickTick translation, without touching either service.

Every call is answered by a fake transport, so this runs offline and
deterministically. What it checks is the part that goes wrong quietly: the
conversions. A priority scale that drifts by one on each pass, or an all-day task
that grows a midnight, produces no error anywhere -- it just slowly corrupts
data, which is the hardest kind of bug to notice and the easiest to test for.

Run directly:  PYTHONPATH=. python3 tests/test_connectors.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys

import httpx

from app.connectors import ticktick as tt
from app.connectors import todoist as td
from app.db.models import CollectionKind, ItemStatus
from app.services.ical_model import CanonicalRecord

UTC = dt.timezone.utc
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


class FakeTransport(httpx.BaseTransport):
    """Answers from a routing table, and records what was asked."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: list[dict] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {}
        self.calls.append((key, str(request.url), body))
        self.headers.append(dict(request.headers))

        handler = self.routes.get(key)
        if handler is None:
            return httpx.Response(404, json={"error": "no route"})
        payload = handler(request) if callable(handler) else handler
        status = 200
        if isinstance(payload, tuple):
            status, payload = payload
        return httpx.Response(status, json=payload)


def make_todoist(routes) -> tuple[td.TodoistConnector, FakeTransport]:
    transport = FakeTransport(routes)
    connector = td.TodoistConnector(
        1, {"access_token": "tok"}, {}, client_id="id", client_secret="secret"
    )
    connector._client = httpx.Client(transport=transport)
    return connector, transport


def make_ticktick(routes, default_timezone: str | None = None
                  ) -> tuple[tt.TickTickConnector, FakeTransport]:
    transport = FakeTransport(routes)
    connector = tt.TickTickConnector(1, {"access_token": "tok"}, {},
                                     default_timezone=default_timezone)
    connector._client = httpx.Client(transport=transport)
    return connector, transport


# --- Priority -----------------------------------------------------------------

print("Priority scales round-trip without drifting")
for todoist_value in (1, 2, 3, 4):
    canonical = td.todoist_priority_to_canonical(todoist_value)
    back = td.canonical_priority_to_todoist(canonical)
    check(f"Todoist P{5 - todoist_value} survives a round trip",
          back == todoist_value, f"{todoist_value} -> {canonical} -> {back}")

for ticktick_value in (0, 1, 3, 5):
    canonical = tt.ticktick_priority_to_canonical(ticktick_value)
    back = tt.canonical_priority_to_ticktick(canonical)
    check(f"TickTick priority {ticktick_value} survives a round trip",
          back == ticktick_value, f"{ticktick_value} -> {canonical} -> {back}")

check("Todoist's most urgent is canonical 1",
      td.todoist_priority_to_canonical(4) == 1)
check("TickTick's most urgent is canonical 1",
      tt.ticktick_priority_to_canonical(5) == 1)
check("an unknown Todoist priority does not crash",
      td.todoist_priority_to_canonical("nonsense") == 0)


# --- Todoist reading ----------------------------------------------------------

print("\nTodoist: the unified v1 shapes are read correctly")

PAGE_ONE = {
    "results": [
        {
            "id": "1",
            "content": "Buy milk",
            "description": "semi-skimmed",
            "project_id": "p1",
            "priority": 4,
            "labels": ["shopping"],
            "due": {"date": "2026-09-04"},
            "added_at": "2026-09-01T10:00:00Z",
            "updated_at": "2026-09-01T11:00:00Z",
        }
    ],
    "next_cursor": "CURSOR",
}
PAGE_TWO = {
    "results": [
        {
            "id": "2",
            "content": "Call the dentist",
            "project_id": "p1",
            "priority": 1,
            "due": {"date": "2026-09-05T14:30:00", "timezone": "Europe/London"},
            "updated_at": "2026-09-01T11:30:00Z",
        }
    ],
    "next_cursor": None,
}

pages = [PAGE_ONE, PAGE_TWO]


def tasks_route(request):
    return pages.pop(0) if pages else {"results": [], "next_cursor": None}


connector, transport = make_todoist({
    "GET /api/v1/tasks": tasks_route,
    "GET /api/v1/tasks/completed/by_completion_date": {
        "results": [
            {
                "id": "3",
                "content": "Finished thing",
                "project_id": "p1",
                "priority": 1,
                "completed_at": "2026-09-01T09:00:00Z",
            }
        ],
        "next_cursor": None,
    },
})
result = connector.pull("p1", CollectionKind.TASKS)
by_id = {item.remote_id: item for item in result.items}

check("both pages were followed via next_cursor", len(by_id) == 3, str(sorted(by_id)))
check("an all-day due date has no time",
      by_id["1"].record.due_date == dt.date(2026, 9, 4)
      and by_id["1"].record.due_time is None,
      f"{by_id['1'].record.due_date} {by_id['1'].record.due_time}")
check("a timed due date keeps its time and timezone",
      by_id["2"].record.due_time == dt.time(14, 30)
      and by_id["2"].record.due_tz == "Europe/London",
      f"{by_id['2'].record.due_time} {by_id['2'].record.due_tz}")
check("labels become tags", by_id["1"].record.tags == ["shopping"],
      str(by_id["1"].record.tags))
check("COMPLETED TASKS COME FROM THEIR OWN ENDPOINT",
      by_id["3"].record.status == ItemStatus.COMPLETED,
      str(by_id["3"].record.status))
check("THE PULL IS INCREMENTAL, SO ABSENCE IS NEVER READ AS DELETION",
      result.incremental is True, str(result.incremental))
check("the ids seen are remembered for next time",
      set(result.sync_state["seen"]) == {"1", "2", "3"}, str(result.sync_state))

print("\nTodoist: a failed completed-tasks call never looks like mass deletion")
connector, _ = make_todoist({
    "GET /api/v1/tasks": {"results": [], "next_cursor": None},
    "GET /api/v1/tasks/completed/by_completion_date": (500, {"error": "boom"}),
})
result = connector.pull("p1", CollectionKind.TASKS)
check("the failure is reported as an error", bool(result.errors), str(result.errors))

print("\nTodoist: a vanished task is asked about, not assumed deleted")
# The bug this prevents: /tasks returns only open tasks, so a completed task is
# absent exactly as a deleted one is. Inferring deletion from absence deleted
# finished work out of every other service.
connector, transport = make_todoist({
    "GET /api/v1/tasks": {"results": [{"id": "1", "content": "Still open",
                                       "priority": 1}], "next_cursor": None},
    "GET /api/v1/tasks/completed/by_completion_date": {"results": [], "next_cursor": None},
    "GET /api/v1/tasks/7": {"id": "7", "content": "Ticked off", "priority": 1,
                            "checked": True, "completed_at": "2026-09-01T09:00:00Z"},
    "GET /api/v1/tasks/8": (404, {"error": "gone"}),
})
result = connector.pull("p1", CollectionKind.TASKS, state={"seen": ["1", "7", "8"]})
by_id = {item.remote_id: item for item in result.items}
check("A TASK MISSING FROM BOTH LISTINGS IS FOUND COMPLETED, NOT DELETED",
      by_id["7"].record.status == ItemStatus.COMPLETED and not by_id["7"].deleted,
      str(by_id.get("7")))
check("a genuinely deleted task is still reported as deleted",
      by_id["8"].deleted is True, str(by_id.get("8")))
check("the open one is untouched",
      by_id["1"].record.status == ItemStatus.NEEDS_ACTION)

print("\nTodoist: a mass disappearance is treated as a fault")
connector, transport = make_todoist({
    "GET /api/v1/tasks": {"results": [], "next_cursor": None},
    "GET /api/v1/tasks/completed/by_completion_date": {"results": [], "next_cursor": None},
})
many = [str(n) for n in range(td.MAX_PROBES + 5)]
result = connector.pull("p1", CollectionKind.TASKS, state={"seen": many})
check("nothing was probed",
      not any(call[0].startswith("GET /api/v1/tasks/") and "completed" not in call[0]
              for call in transport.calls), str(len(transport.calls)))
check("and it is reported", bool(result.errors), str(result.errors))

print("\nTodoist: writing")
connector, transport = make_todoist({
    "POST /api/v1/tasks": {"id": "9", "updated_at": "2026-09-01T12:00:00Z"},
    "POST /api/v1/tasks/9/close": {},
})
outcome = connector.create(
    "p1",
    CanonicalRecord(
        uid="x", title="Timed task", kind=CollectionKind.TASKS,
        due_date=dt.date(2026, 9, 7), due_time=dt.time(9, 15), priority=1,
        status=ItemStatus.COMPLETED,
    ),
    CollectionKind.TASKS,
)
body = transport.calls[0][2]
check("a create succeeds", outcome.ok and outcome.remote_id == "9", str(outcome.error))
check("a timed task sends due_datetime, never due_date",
      body.get("due_datetime") == "2026-09-07T09:15:00" and "due_date" not in body,
      str(body))
check("the highest priority is sent as Todoist P1", body.get("priority") == 4, str(body))
check("an already-completed task is closed after creation",
      any(call[0].endswith("/close") for call in transport.calls),
      str([c[0] for c in transport.calls]))

connector, transport = make_todoist({"POST /api/v1/tasks": {"id": "10"}})
connector.create(
    "p1",
    CanonicalRecord(uid="y", title="All day", kind=CollectionKind.TASKS,
                    due_date=dt.date(2026, 9, 7)),
    CollectionKind.TASKS,
)
body = transport.calls[0][2]
check("AN ALL-DAY TASK SENDS due_date AND NEVER INVENTS A MIDNIGHT",
      body.get("due_date") == "2026-09-07" and "due_datetime" not in body, str(body))

print("\nTodoist: capabilities are declared honestly")
caps = connector.capabilities(CollectionKind.TASKS)
check("due_time is claimed, unlike Google", caps.supports(td.F_DUE_TIME))
check("recurrence is NOT claimed", not caps.supports("rrule"))
check("location is NOT claimed", not caps.supports("location"))
check("calendars are refused", not connector.supports_kind(CollectionKind.CALENDAR))


# --- TickTick -----------------------------------------------------------------

print("\nTickTick: reading a project")
connector, transport = make_ticktick({
    "GET /open/v1/project/p1/data": {
        "project": {"id": "p1", "name": "Work"},
        "tasks": [
            {
                "id": "t1", "projectId": "p1", "title": "Write report",
                "content": "for Friday", "priority": 5, "status": 0,
                "dueDate": "2026-09-04T16:00:00+0000", "timeZone": "Europe/London",
                "isAllDay": False, "modifiedTime": "2026-09-01T10:00:00+0000",
            },
            {
                "id": "t2", "projectId": "p1", "title": "All day thing",
                "priority": 0, "status": 0, "isAllDay": True,
                "dueDate": "2026-09-05T00:00:00+0000",
            },
        ],
    },
})
result = connector.pull("p1", CollectionKind.TASKS)
by_id = {item.remote_id: item for item in result.items}

check("tasks are read from the project data endpoint", len(by_id) == 2, str(sorted(by_id)))
# 16:00+0000 with timeZone Europe/London is five o'clock there, not four.
# This check previously asserted 16:00 and so encoded the very bug it was
# supposed to protect against.
check("a timed task keeps its time, converted into its own zone",
      by_id["t1"].record.due_time == dt.time(17, 0),
      str(by_id["t1"].record.due_time))
check("AN ALL-DAY TASK GETS NO PHANTOM MIDNIGHT",
      by_id["t2"].record.due_date == dt.date(2026, 9, 5)
      and by_id["t2"].record.due_time is None,
      f"{by_id['t2'].record.due_date} {by_id['t2'].record.due_time}")
check("THE PULL IS MARKED INCREMENTAL SO ABSENCE IS NEVER DELETION",
      result.incremental is True)
check("the ids seen are remembered for next time",
      set(result.sync_state["seen"]) == {"t1", "t2"}, str(result.sync_state))

print("\nTickTick: a task that vanished is probed rather than assumed deleted")
connector, transport = make_ticktick({
    "GET /open/v1/project/p1/data": {"tasks": [{"id": "t1", "title": "Still here",
                                               "status": 0, "priority": 0}]},
    "GET /open/v1/project/p1/task/t2": {"id": "t2", "title": "Done now",
                                        "status": 2, "priority": 0,
                                        "completedTime": "2026-09-01T12:00:00+0000"},
    "GET /open/v1/project/p1/task/t3": (404, {"error": "gone"}),
})
result = connector.pull("p1", CollectionKind.TASKS, state={"seen": ["t1", "t2", "t3"]})
by_id = {item.remote_id: item for item in result.items}

check("A COMPLETED TASK IS FOUND, NOT LOST",
      by_id["t2"].record.status == ItemStatus.COMPLETED, str(by_id.get("t2")))
check("a genuinely deleted task is reported as deleted",
      by_id["t3"].deleted is True, str(by_id.get("t3")))
check("the surviving task is unaffected",
      by_id["t1"].record.status == ItemStatus.NEEDS_ACTION)
check("the remembered set no longer includes the deleted one",
      "t3" not in result.sync_state["seen"], str(result.sync_state))

print("\nTickTick: a mass disappearance is treated as a fault, not as work done")
many = [f"t{n}" for n in range(tt.MAX_PROBES + 5)]
connector, transport = make_ticktick({"GET /open/v1/project/p1/data": {"tasks": []}})
result = connector.pull("p1", CollectionKind.TASKS, state={"seen": many})
check("nothing was probed", not any("/task/" in call[0] for call in transport.calls),
      str(len(transport.calls)))
check("and it is reported rather than silently ignored", bool(result.errors),
      str(result.errors))

print("\nTickTick: writing")
connector, transport = make_ticktick({
    "POST /open/v1/task": {"id": "n1", "modifiedTime": "2026-09-01T12:00:00+0000"},
    "POST /open/v1/project/p1/task/n1/complete": {},
})
outcome = connector.create(
    "p1",
    CanonicalRecord(uid="z", title="New", kind=CollectionKind.TASKS,
                    due_date=dt.date(2026, 9, 9), priority=1),
    CollectionKind.TASKS,
)
body = transport.calls[0][2]
check("a create succeeds", outcome.ok and outcome.remote_id == "n1", str(outcome.error))
check("an all-day due date is flagged as such", body.get("isAllDay") is True, str(body))
check("the highest priority is sent as TickTick 5", body.get("priority") == 5, str(body))

print("\nTickTick: capabilities are declared honestly")
caps = connector.capabilities(CollectionKind.TASKS)
check("recurrence IS claimed, because repeatFlag is an RRULE", caps.supports(tt.F_RRULE))
check("TAGS ARE NOT CLAIMED, SO THEY CANNOT BE ERASED", not caps.supports("tags"))
check("calendars are refused", not connector.supports_kind(CollectionKind.CALENDAR))

print("\nTodoist: a multi-day span uses the deadline field")
# Todoist has one scheduling date. A task that spans time puts the START there,
# so it surfaces in Today when work should begin, and the real deadline in the
# separate deadline field. Writing the start into the date field ALONE was
# tested against the live API and destroyed the deadline on the next pull:
# nothing distinguished "this is a start" from "this is a deadline".
connector, transport = make_todoist(
    {"POST /api/v1/tasks": {"id": "s1", "updated_at": "2026-09-01T12:00:00Z"}})
connector.create("p1", CanonicalRecord(
    uid="s", title="Span", kind=CollectionKind.TASKS,
    start_date=dt.date(2026, 9, 10), start_time=dt.time(9, 0),
    due_date=dt.date(2026, 9, 12), due_time=dt.time(17, 0),
), CollectionKind.TASKS)
body = transport.calls[0][2]
check("THE START GOES IN TODOIST'S SCHEDULING DATE",
      body.get("due_datetime") == "2026-09-10T09:00:00", str(body.get("due_datetime")))
check("and the real deadline is kept in the deadline field",
      body.get("deadline_date") == "2026-09-12", str(body.get("deadline_date")))

print("\nTodoist: a single-moment task is unchanged")
connector, transport = make_todoist(
    {"POST /api/v1/tasks": {"id": "s2", "updated_at": "2026-09-01T12:00:00Z"}})
connector.create("p1", CanonicalRecord(
    uid="p", title="Point", kind=CollectionKind.TASKS,
    due_date=dt.date(2026, 9, 12), due_time=dt.time(17, 0),
), CollectionKind.TASKS)
body = transport.calls[0][2]
check("the due time still goes in the scheduling date",
      body.get("due_datetime") == "2026-09-12T17:00:00", str(body.get("due_datetime")))
check("and no deadline is invented", body.get("deadline_date") is None,
      str(body.get("deadline_date")))

print("\nTodoist: reading a span back undoes the swap exactly")
connector, _ = make_todoist({})
caps = connector.capabilities(CollectionKind.TASKS)
entry = {
    "id": "s1", "content": "Span", "priority": 1,
    "due": {"date": "2026-09-10T09:00:00", "timezone": None},
    "deadline": {"date": "2026-09-12", "lang": "en"},
}
rec = connector._task_to_record(entry)
check("the deadline comes back as the due date",
      rec.due_date == dt.date(2026, 9, 12), str(rec.due_date))
check("the scheduling date comes back as the start",
      (rec.start_date, rec.start_time) == (dt.date(2026, 9, 10), dt.time(9, 0)),
      f"{rec.start_date} {rec.start_time}")

fields = td.fields_for(entry, caps)
check("IT SAYS NOTHING ABOUT THE DUE TIME, WHICH IT CANNOT HOLD",
      "due_time" not in fields, str(sorted(fields)))
check("but it does speak for the start", "start" in fields, str(sorted(fields)))

plain = {"id": "s2", "content": "Point", "priority": 1,
         "due": {"date": "2026-09-12T17:00:00", "timezone": None}, "deadline": None}
rec = connector._task_to_record(plain)
check("a task with no deadline reports no start", rec.start_date is None, str(rec.start_date))
fields = td.fields_for(plain, caps)
check("and does not claim the start field, so it cannot erase one",
      "start" not in fields, str(sorted(fields)))
check("while still speaking for the due time", "due_time" in fields, str(sorted(fields)))

print("\nTickTick: a floating time is written in the user's own zone")
# Todoist reports "timezone": null for a task set at 10am, meaning that clock
# time wherever you are. TickTick cannot express floating -- every task carries
# a zone -- so the time has to be materialised in the user's zone. Sending it as
# UTC instead is what showed a 10am task at 4am in Denver.
connector, transport = make_ticktick(
    {"POST /open/v1/task": {"id": "n2", "modifiedTime": "2026-09-01T12:00:00+0000"}},
    default_timezone="America/Denver",
)
connector.create("p1", CanonicalRecord(
    uid="f", title="Floating", kind=CollectionKind.TASKS,
    due_date=dt.date(2026, 9, 5), due_time=dt.time(10, 0), due_tz=None,
), CollectionKind.TASKS)
body = transport.calls[0][2]
check("A FLOATING 10AM IS SENT AS 16:00 UTC, WHICH IS 10AM IN DENVER",
      body.get("dueDate", "").startswith("2026-09-05T16:00:00"), str(body.get("dueDate")))
check("and the zone sent is the user's, not UTC",
      body.get("timeZone") == "America/Denver", str(body.get("timeZone")))

print("\nTickTick: one field's zone is never borrowed for another")
# A stray zone left on the start field used to be picked up by a due time that
# had none of its own, which is how UTC crept in and shifted everything.
connector, transport = make_ticktick(
    {"POST /open/v1/task": {"id": "n3", "modifiedTime": "2026-09-01T12:00:00+0000"}},
    default_timezone="America/Denver",
)
connector.create("p1", CanonicalRecord(
    uid="g", title="Mixed", kind=CollectionKind.TASKS,
    due_date=dt.date(2026, 9, 5), due_time=dt.time(10, 0), due_tz=None,
    start_date=dt.date(2026, 9, 5), start_time=dt.time(9, 0), start_tz="UTC",
), CollectionKind.TASKS)
body = transport.calls[0][2]
check("the due time still uses the user's zone, not the start field's UTC",
      body.get("dueDate", "").startswith("2026-09-05T16:00:00"), str(body.get("dueDate")))
check("the start keeps its own explicit UTC",
      body.get("startDate", "").startswith("2026-09-05T09:00:00"), str(body.get("startDate")))

print("\nTickTick: a materialised floating time reads back as floating")
# Otherwise the round trip pins it: Task Hub would report a change nobody made
# on every pass, flipping between "10am floating" and "10am in Denver" forever.
connector, _ = make_ticktick({}, default_timezone="America/Denver")
record = connector._task_to_record({
    "id": "t1", "title": "Round trip", "status": 0,
    "dueDate": "2026-09-05T16:00:00.000+0000", "timeZone": "America/Denver",
    "isAllDay": False,
})
check("the clock time comes back unchanged", record.due_time == dt.time(10, 0),
      str(record.due_time))
check("AND IT IS STILL FLOATING, NOT PINNED", record.due_tz is None, repr(record.due_tz))

record = connector._task_to_record({
    "id": "t2", "title": "Elsewhere", "status": 0,
    "dueDate": "2026-09-05T16:00:00.000+0000", "timeZone": "Europe/London",
    "isAllDay": False,
})
check("a genuinely different zone is kept", record.due_tz == "Europe/London",
      repr(record.due_tz))

print("\niCalendar keeps all three forms of a due time apart")
from app.services.ical_model import parse_single, record_to_ics

for zone, expect_line, expect_tz in [
    (None, "DUE:20260905T100000", None),
    ("America/Denver", "DUE;TZID=America/Denver:20260905T100000", "America/Denver"),
    ("UTC", "DUE:20260905T100000Z", "UTC"),
]:
    ics = record_to_ics(CanonicalRecord(
        uid="i", title="t", kind=CollectionKind.TASKS,
        due_date=dt.date(2026, 9, 5), due_time=dt.time(10, 0), due_tz=zone))
    line = next(l for l in ics.splitlines() if l.startswith("DUE"))
    back = parse_single(ics, CollectionKind.TASKS)
    check(f"{zone or 'floating'} is written as {expect_line}", line == expect_line, line)
    check(f"{zone or 'floating'} survives the round trip",
          back.due_time == dt.time(10, 0) and back.due_tz == expect_tz,
          f"{back.due_time} {back.due_tz!r}")

ics = record_to_ics(CanonicalRecord(uid="j", title="t", kind=CollectionKind.TASKS,
                                    due_date=dt.date(2026, 9, 5)))
check("a date with no time stays a bare DATE",
      "DUE;VALUE=DATE:20260905" in ics, ics)

print("\nTodoist: a personal API token needs no client id or secret")
transport = FakeTransport({"GET /api/v1/user": {"email": "paul@example.com"}})
token_connector = td.TodoistConnector(1, {"api_token": "PERSONAL"}, {})
token_connector._client = httpx.Client(transport=transport)
check("it builds with no OAuth application at all", token_connector.auth is None)
check("verify() works", token_connector.verify() == "paul@example.com")
check("THE TOKEN IS SENT AS A BEARER TOKEN",
      transport.headers[0].get("authorization") == "Bearer PERSONAL",
      str(transport.headers[0].get("authorization")))
check("nothing needs writing back", token_connector.credentials_changed is False)

try:
    td.TodoistConnector(1, {}, {})
    built = True
except Exception:
    built = False
check("with neither a token nor an application it refuses clearly", not built)

print("\nTodoist: a rejected token says so in the token's own terms")
transport = FakeTransport({"GET /api/v1/user": (401, {"error": "bad"})})
token_connector = td.TodoistConnector(1, {"api_token": "WRONG"}, {})
token_connector._client = httpx.Client(transport=transport)
try:
    token_connector.verify()
    message = ""
except Exception as exc:
    message = str(exc)
check("the message mentions the token, not 'reconnect'",
      "API token" in message, message)

print("\nPasted redirect addresses are read in every shape people paste them")
from app.web.oauth_setup import _code_from_pasted
cases = [
    ("http://h:8080/oauth/ticktick/callback?code=ABC123&state=xyz", "ABC123"),
    ("https://example.com/cb?state=xyz&code=ABC123", "ABC123"),
    ("?code=ABC123&state=x", "ABC123"),
    ("ABC123", "ABC123"),
    ("  ABC123  ", "ABC123"),
    ("", None),
    ("http://h/cb?state=only", None),
]
for text, expected in cases:
    got = _code_from_pasted(text)
    check(f"{text[:38]!r} -> {expected}", got == expected, f"got {got}")

print("\nTimezones: a fixed instant is converted, a floating time is not")
# The failure this guards against is silent and uniform: read a UTC instant as
# though its clock face were local and every timed task shifts by the local
# offset, in one direction, for ever.
from app.connectors.ticktick import _format_moment, _parse_moment
from app.connectors.todoist import _parse_due

check("Todoist floating 14:30 stays 14:30",
      _parse_due({"date": "2026-09-05T14:30:00", "timezone": "Europe/London"})[1]
      == dt.time(14, 30))
check("TODOIST 14:30Z IN LONDON IS 15:30, NOT 14:30",
      _parse_due({"date": "2026-09-05T14:30:00Z", "timezone": "Europe/London"})[1]
      == dt.time(15, 30),
      str(_parse_due({"date": "2026-09-05T14:30:00Z", "timezone": "Europe/London"})))
check("with no zone named, the hub's own zone is used",
      _parse_due({"date": "2026-09-05T14:30:00Z"}, "America/New_York")[1]
      == dt.time(10, 30),
      str(_parse_due({"date": "2026-09-05T14:30:00Z"}, "America/New_York")))
check("TICKTICK 16:00+0000 IN LONDON IS 17:00, NOT 16:00",
      _parse_moment("2026-09-04T16:00:00+0000", False, "Europe/London")[1]
      == dt.time(17, 0),
      str(_parse_moment("2026-09-04T16:00:00+0000", False, "Europe/London")))
check("a TickTick all-day task still gets no time",
      _parse_moment("2026-09-05T00:00:00+0000", True, "Europe/London")[1] is None)
check("an unparseable zone falls back rather than raising",
      _parse_moment("2026-09-04T16:00:00+0000", False, "Not/AZone")[1] == dt.time(16, 0))

print("\nTimezones: TickTick round-trips a wall time exactly")
for zone, clock in (("Europe/London", dt.time(17, 0)),
                    ("America/New_York", dt.time(9, 30)),
                    ("Asia/Tokyo", dt.time(23, 45)),
                    ("UTC", dt.time(6, 5))):
    written = _format_moment(dt.date(2026, 9, 4), clock, zone)
    back_date, back_time, _ = _parse_moment(written, False, zone)
    check(f"{clock} in {zone} survives write then read",
          back_time == clock and back_date == dt.date(2026, 9, 4),
          f"{written} -> {back_date} {back_time}")

print("\nGoogle cannot strip a time set in Todoist or TickTick")
# The protection is in the capability declarations, not in any one code path:
# the merge engine only ever considers fields a connector claims. If Google Tasks
# ever claimed due_time, its inability to store one would start erasing times
# everywhere -- so the absence is asserted here rather than assumed.
from app.connectors.base import F_DUE_DATE, F_DUE_TIME
from app.connectors.google import GoogleConnector

google_caps = GoogleConnector.capabilities(
    GoogleConnector.__new__(GoogleConnector), CollectionKind.TASKS
)
check("Google Tasks keeps the date", google_caps.supports(F_DUE_DATE))
check("GOOGLE TASKS DOES NOT CLAIM THE TIME, SO IT CANNOT ERASE ONE",
      not google_caps.supports(F_DUE_TIME))
check("Todoist does claim the time",
      td.TodoistConnector.capabilities(
          td.TodoistConnector.__new__(td.TodoistConnector), CollectionKind.TASKS
      ).supports(F_DUE_TIME))
check("TickTick does claim the time",
      tt.TickTickConnector.capabilities(
          tt.TickTickConnector.__new__(tt.TickTickConnector), CollectionKind.TASKS
      ).supports(F_DUE_TIME))

print("\nGoogle's recurrence rule is normalised to the form everything else uses")
# Google says "RRULE:FREQ=YEARLY"; iCalendar, and so Radicale, says
# "FREQ=YEARLY". Two spellings of one rule made every sync find a difference,
# resolve it, write it back and find it again -- for ever.
from app.connectors.google import GoogleConnector

blank = GoogleConnector.__new__(GoogleConnector)
for raw, expected in (
    (["RRULE:FREQ=YEARLY"], "FREQ=YEARLY"),
    (["rrule:FREQ=WEEKLY;BYDAY=MO"], "FREQ=WEEKLY;BYDAY=MO"),
    (["FREQ=DAILY"], "FREQ=DAILY"),
    (["EXDATE;VALUE=DATE:20260101", "RRULE:FREQ=MONTHLY"], "FREQ=MONTHLY"),
    ([], None),
):
    record = GoogleConnector._event_to_record(blank, {"summary": "x", "recurrence": raw})
    check(f"{raw} -> {expected!r}", record.rrule == expected, repr(record.rrule))

# And the write side puts the prefix back, so the round trip is stable.
payload = GoogleConnector._record_to_event(
    blank, CanonicalRecord(uid="u", kind=CollectionKind.CALENDAR, title="x",
                           rrule="FREQ=YEARLY")
)
check("writing back re-attaches the prefix Google expects",
      payload.get("recurrence") == ["RRULE:FREQ=YEARLY"], str(payload.get("recurrence")))
back = GoogleConnector._event_to_record(
    blank, {"summary": "x", "recurrence": payload["recurrence"]}
)
check("SO A RECURRING EVENT NO LONGER CONFLICTS WITH ITSELF",
      back.rrule == "FREQ=YEARLY", repr(back.rrule))

if _failures:
    print(f"\n{len(_failures)} check(s) failed: {', '.join(_failures)}")
    sys.exit(1)
print("\nAll connector tests passed.")
