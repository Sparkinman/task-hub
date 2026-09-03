"""Multi-day events and tasks must appear on every day they cover.

A three-day trip drawn only on the day it starts is a calendar that lies about
the other two, and a week opened on the Wednesday of that trip showed nothing at
all. These tests pin the placement rules directly, without a server or a
database, because they are pure date arithmetic and deserve to be checked as
such.
"""

from __future__ import annotations

import datetime as dt
import sys

from app.db.models import CollectionKind
from app.services.ical_model import CanonicalRecord
from app.web.calendar_view import MAX_SPAN_DAYS, _placements, _span_days

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def days(record, first, last, kind="event"):
    return [d for d, _offset, _span in _placements(record, first, last, kind)]


def event(**kwargs) -> CanonicalRecord:
    return CanonicalRecord(uid="e", kind=CollectionKind.CALENDAR, title="E", **kwargs)


def task(**kwargs) -> CanonicalRecord:
    return CanonicalRecord(uid="t", kind=CollectionKind.TASKS, title="T", **kwargs)


D = dt.date
MONTH_FIRST, MONTH_LAST = D(2026, 9, 1), D(2026, 9, 30)

print("\nA timed event running over three days appears on all three")
trip = event(
    start_date=D(2026, 9, 10), start_time=dt.time(9, 0),
    end_date=D(2026, 9, 12), end_time=dt.time(17, 0),
)
check("it covers three days", _span_days(trip, "event") == 3,
      str(_span_days(trip, "event")))
check("and is drawn on each of them",
      days(trip, MONTH_FIRST, MONTH_LAST) == [D(2026, 9, 10), D(2026, 9, 11), D(2026, 9, 12)],
      str(days(trip, MONTH_FIRST, MONTH_LAST)))

print("\nAn all-day event's exclusive end does not add a phantom day")
# DTEND on a date value is the day AFTER the event finishes, so a 10th-to-11th
# holiday is stored as ending on the 12th. Treating that literally showed every
# all-day event running a day longer than it does.
holiday = event(start_date=D(2026, 9, 10), end_date=D(2026, 9, 12))
check("two days, not three", _span_days(holiday, "event") == 2,
      str(_span_days(holiday, "event")))
check("drawn on the 10th and 11th only",
      days(holiday, MONTH_FIRST, MONTH_LAST) == [D(2026, 9, 10), D(2026, 9, 11)],
      str(days(holiday, MONTH_FIRST, MONTH_LAST)))

print("\nA single all-day event is still one day")
one = event(start_date=D(2026, 9, 10), end_date=D(2026, 9, 11))
check("one day", days(one, MONTH_FIRST, MONTH_LAST) == [D(2026, 9, 10)],
      str(days(one, MONTH_FIRST, MONTH_LAST)))

print("\nA run that began before the window is still drawn inside it")
# The failure this catches: opening the week view on the Wednesday of a
# Monday-to-Friday trip and seeing an empty calendar.
mid = days(trip, D(2026, 9, 11), D(2026, 9, 11))
check("THE MIDDLE DAY OF A TRIP SHOWS IT", mid == [D(2026, 9, 11)], str(mid))

print("\nThe ends of a run are marked, and the clock times stay at the ends")
places = _placements(trip, MONTH_FIRST, MONTH_LAST, "event")
offsets = [offset for _d, offset, _s in places]
check("the positions run 0, 1, 2", offsets == [0, 1, 2], str(offsets))
check("every placement knows the run is three long",
      all(span == 3 for _d, _o, span in places))

print("\nA recurring event that lasts two days spans on every occurrence")
weekly = event(
    start_date=D(2026, 9, 7), start_time=dt.time(9, 0),
    end_date=D(2026, 9, 8), end_time=dt.time(17, 0),
    rrule="FREQ=WEEKLY",
)
got = days(weekly, MONTH_FIRST, MONTH_LAST)
check("each week contributes two consecutive days",
      got == [D(2026, 9, 7), D(2026, 9, 8), D(2026, 9, 14), D(2026, 9, 15),
              D(2026, 9, 21), D(2026, 9, 22), D(2026, 9, 28), D(2026, 9, 29)],
      str(got))

print("\nA task with a start earlier than its due date spans the gap")
spanning = task(
    start_date=D(2026, 9, 10), start_time=dt.time(9, 0),
    due_date=D(2026, 9, 12), due_time=dt.time(17, 0),
)
check("three days", days(spanning, MONTH_FIRST, MONTH_LAST, "task")
      == [D(2026, 9, 10), D(2026, 9, 11), D(2026, 9, 12)],
      str(days(spanning, MONTH_FIRST, MONTH_LAST, "task")))

print("\nAn ordinary task stays on the day it is due")
plain = task(due_date=D(2026, 9, 15))
check("one day, the due date", days(plain, MONTH_FIRST, MONTH_LAST, "task")
      == [D(2026, 9, 15)], str(days(plain, MONTH_FIRST, MONTH_LAST, "task")))

same = task(start_date=D(2026, 9, 15), due_date=D(2026, 9, 15))
check("a start equal to the due date does not make it span",
      days(same, MONTH_FIRST, MONTH_LAST, "task") == [D(2026, 9, 15)],
      str(days(same, MONTH_FIRST, MONTH_LAST, "task")))

print("\nA task whose start is long past is filed under its due date, not its start")
# A task carrying a start date from months ago would otherwise be painted across
# every cell of every month between, burying everything else on the page.
stale = task(start_date=D(2025, 1, 1), due_date=D(2026, 9, 20))
check("the run is refused as too long",
      _span_days(stale, "task") > MAX_SPAN_DAYS, str(_span_days(stale, "task")))
check("AND IT APPEARS ON ITS DUE DATE ALONE",
      days(stale, MONTH_FIRST, MONTH_LAST, "task") == [D(2026, 9, 20)],
      str(days(stale, MONTH_FIRST, MONTH_LAST, "task")))

print("\nA task with a start AFTER its due date is not spun into nonsense")
backwards = task(start_date=D(2026, 9, 20), due_date=D(2026, 9, 10))
check("shown once, on the due date",
      days(backwards, MONTH_FIRST, MONTH_LAST, "task") == [D(2026, 9, 10)],
      str(days(backwards, MONTH_FIRST, MONTH_LAST, "task")))

print("\nA run reaching past the window is clipped to it")
long_trip = event(
    start_date=D(2026, 8, 28), start_time=dt.time(9, 0),
    end_date=D(2026, 9, 3), end_time=dt.time(17, 0),
)
got = days(long_trip, MONTH_FIRST, MONTH_LAST)
check("only the days inside September are returned",
      got == [D(2026, 9, 1), D(2026, 9, 2), D(2026, 9, 3)], str(got))

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All calendar span tests passed.")
