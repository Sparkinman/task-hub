"""Reading a service's answer back without changing what it means.

Two faults found by running against real accounts, pinned here so they cannot
return quietly. Neither needed a network to reproduce once the real payloads
were known, and both were invisible to every existing test.

**Google Calendar was losing an hour.** It stores the instant and names the zone
separately, so ten o'clock in London during British Summer Time comes back as
``2026-09-09T09:00:00Z`` with ``timeZone: Europe/London``. The connector read
the clock face off that string and paired it with the zone label, producing nine
o'clock -- a different moment, which was then written to every other service as
though somebody had moved the event. In winter, when London is UTC, the
unconverted value is accidentally correct, which is how it survived.

**Todoist and TickTick were provoking pointless writes.** Todoist stores a due
time as a floating clock face and returns no zone at all; both services carry
fewer priority levels than iCalendar's nine. Nothing is lost that either service
claimed to keep, but the value that comes back differs from the one sent, and
the echo check compares against what was sent. ``echo_of`` closes that gap by
recording what the service will actually say.
"""

from __future__ import annotations

import datetime as dt
import sys

from app.connectors.google import GoogleConnector
from app.connectors.ticktick import (
    TickTickConnector, canonical_priority_to_ticktick, ticktick_priority_to_canonical,
)
from app.connectors.todoist import (
    TodoistConnector, canonical_priority_to_todoist, todoist_priority_to_canonical,
)
from app.db.models import CollectionKind
from app.services.ical_model import CanonicalRecord

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


google = GoogleConnector(
    account_id=0,
    credentials={"access_token": "x", "refresh_token": "y"},
    client_id="c", client_secret="s",
    default_timezone="America/Denver",
)
todoist = TodoistConnector(account_id=0, credentials={"api_token": "x"})
ticktick = TickTickConnector(account_id=0, credentials={"access_token": "x"})


print("\nGoogle Calendar endpoints, using payloads the real API returned")

# Recorded from a live probe: sent 22:00 Europe/London, Google stored this.
date, time_of_day, zone = google._split_endpoint(
    {"dateTime": "2026-09-09T21:00:00Z", "timeZone": "Europe/London"}
)
check("a summer evening reads back at the time it was written",
      (date, time_of_day) == (dt.date(2026, 9, 9), dt.time(22, 0)),
      f"got {date} {time_of_day} {zone!r}, wanted 2026-09-09 22:00:00")
check("and keeps the zone it names", zone == "Europe/London", repr(zone))

date, time_of_day, zone = google._split_endpoint(
    {"dateTime": "2026-09-09T08:00:00Z", "timeZone": "Europe/London"}
)
check("a summer morning too",
      (date, time_of_day) == (dt.date(2026, 9, 9), dt.time(9, 0)),
      f"got {date} {time_of_day}")

# Winter: London is UTC, so the value is the same either way. This is the case
# that used to pass by accident, and it must still pass on purpose.
date, time_of_day, zone = google._split_endpoint(
    {"dateTime": "2026-12-09T09:00:00Z", "timeZone": "Europe/London"}
)
check("a winter morning, when the offset is zero",
      (date, time_of_day) == (dt.date(2026, 12, 9), dt.time(9, 0)),
      f"got {date} {time_of_day}")

# A zone west of UTC, where an unconverted read lands on the wrong day.
date, time_of_day, zone = google._split_endpoint(
    {"dateTime": "2026-09-10T02:00:00Z", "timeZone": "America/Denver"}
)
check("an evening in Denver stays on its own day",
      (date, time_of_day) == (dt.date(2026, 9, 9), dt.time(20, 0)),
      f"got {date} {time_of_day}")

# All-day endpoints carry no time and must never be converted: shifting one by
# a zone offset moves the date for anyone west of UTC.
date, time_of_day, zone = google._split_endpoint({"date": "2026-09-09"})
check("an all-day endpoint keeps its date and gains no time",
      (date, time_of_day, zone) == (dt.date(2026, 9, 9), None, None),
      f"got {date} {time_of_day} {zone!r}")

# No zone named at all: the instant must survive, whatever it is labelled.
date, time_of_day, zone = google._split_endpoint({"dateTime": "2026-09-09T21:00:00Z"})
check("a timestamp with no named zone still lands on a real moment",
      date == dt.date(2026, 9, 9) and time_of_day is not None,
      f"got {date} {time_of_day} {zone!r}")


print("\nWhat each service will say when asked again")

record = CanonicalRecord(
    uid="u", kind=CollectionKind.TASKS, title="Task",
    due_date=dt.date(2026, 9, 9), due_time=dt.time(17, 30), due_tz="Europe/London",
    start_date=dt.date(2026, 9, 9), start_time=dt.time(9, 0), start_tz="Europe/London",
    priority=2,
)

echoed = todoist.echo_of(record, CollectionKind.TASKS)
check("Todoist reports no timezone, as the live API does",
      echoed.due_tz is None and echoed.start_tz is None,
      f"due_tz={echoed.due_tz!r} start_tz={echoed.start_tz!r}")
check("Todoist reports the clock face unchanged",
      (echoed.due_date, echoed.due_time) == (dt.date(2026, 9, 9), dt.time(17, 30)),
      f"{echoed.due_date} {echoed.due_time}")
check("Todoist reports priority 2 as 1, which is what it returned live",
      echoed.priority == 1, str(echoed.priority))
check("the original record is not modified in place",
      record.due_tz == "Europe/London" and record.priority == 2,
      f"{record.due_tz!r} {record.priority}")

echoed = ticktick.echo_of(record, CollectionKind.TASKS)
check("TickTick keeps the zone, which it does return",
      echoed.due_tz == "Europe/London", repr(echoed.due_tz))
check("TickTick reports priority 2 as 1", echoed.priority == 1, str(echoed.priority))


print("\nAn echo of an echo must be the same echo")
# If it were not, the baseline would keep moving and the write would come back
# on the pass after the one that suppressed it.
for connector, name in ((todoist, "Todoist"), (ticktick, "TickTick")):
    stable = True
    for value in range(0, 10):
        probe = CanonicalRecord(uid="u", kind=CollectionKind.TASKS, title="t",
                                priority=value)
        once = connector.echo_of(probe, CollectionKind.TASKS)
        twice = connector.echo_of(once, CollectionKind.TASKS)
        if once.priority != twice.priority:
            stable = False
            print(f"    {name}: priority {value} -> {once.priority} -> {twice.priority}")
    check(f"{name} priority settles after one round trip", stable)

print("\nPriority mappings are many-to-one, and that is expected")
for value in range(0, 10):
    there = canonical_priority_to_todoist(value)
    check_value = todoist_priority_to_canonical(there)
    if check_value != value:
        print(f"    todoist  canonical {value} -> {there} -> {check_value}")
for value in range(0, 10):
    there = canonical_priority_to_ticktick(value)
    check_value = ticktick_priority_to_canonical(there)
    if check_value != value:
        print(f"    ticktick canonical {value} -> {there} -> {check_value}")
print("  (listed rather than asserted: four and five levels cannot carry nine)")

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All service echo tests passed.")
