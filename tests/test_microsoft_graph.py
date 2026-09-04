"""Microsoft Graph's date handling, pinned against its documented shapes.

The Microsoft connector has never run against a live account, and the label
saying so stays until it has. What can be done meanwhile is to check the part
that went wrong for a connector that *had* run for months: Google Calendar
returned every timed event an hour early for its whole life, and no test caught
it because every test was written from the same misunderstanding as the code.

The misunderstanding was about a shape both APIs use and mean differently:

- **Google** returns ``dateTime`` carrying an *offset* -- ``2026-09-09T21:00:00Z``
  -- next to a ``timeZone`` naming the zone to *display* it in. Two different
  things, so the instant has to be converted into the named zone before its
  clock face is read. Reading the clock face straight off the string loses the
  offset, which is where the hour went.

- **Graph** returns ``dateTime`` as a naive local time *already expressed in*
  the ``timeZone`` beside it -- ``2026-09-09T21:00:00.0000000`` with
  ``timeZone: "UTC"`` means twenty-one hundred UTC. Pairing them is correct, and
  converting would be the bug.

So the two connectors should differ here, and the Microsoft one is right to do
what would be wrong in the Google one. That is worth asserting, because it looks
like an inconsistency and the obvious "fix" would break it.

Everything below uses response shapes as Graph documents them, so this is only
as good as that documentation -- which is exactly why the untested label stays.
"""

from __future__ import annotations

import datetime as dt
import sys

from app.connectors.microsoft import (
    _CANONICAL_TO_IMPORTANCE, _IMPORTANCE_TO_CANONICAL,
    _graph_datetime, _parse_graph_datetime,
)


def canonical_priority_to_importance(value):
    return _CANONICAL_TO_IMPORTANCE.get(value or 5, "normal")


def importance_to_canonical_priority(word):
    return _IMPORTANCE_TO_CANONICAL.get(word or "normal", 5)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


print("\nA Graph dateTime is a local time in the zone beside it, not an instant")
date, time_of_day, zone = _parse_graph_datetime(
    {"dateTime": "2026-09-09T21:00:00.0000000", "timeZone": "UTC"}
)
check("21:00 UTC reads as 21:00 UTC",
      (date, time_of_day, zone) == (dt.date(2026, 9, 9), dt.time(21, 0), "UTC"),
      f"{date} {time_of_day} {zone!r}")

# The case that would have caught the Google fault had Google worked this way.
# Nothing may be shifted by the offset of the named zone: the value is already
# in it.
date, time_of_day, zone = _parse_graph_datetime(
    {"dateTime": "2026-09-09T10:00:00.0000000", "timeZone": "Europe/London"}
)
check("10:00 named as Europe/London stays 10:00, not converted",
      (date, time_of_day) == (dt.date(2026, 9, 9), dt.time(10, 0)),
      f"got {date} {time_of_day} -- an hour's shift here is the Google bug")
check("and keeps the zone it was given", zone == "Europe/London", repr(zone))

print("\nGraph's fractional seconds vary in length and must not break parsing")
for raw in ("2026-09-09T21:00:00.0000000",
            "2026-09-09T21:00:00.000",
            "2026-09-09T21:00:00",
            "2026-09-09T21:00:00Z"):
    date, time_of_day, _ = _parse_graph_datetime({"dateTime": raw, "timeZone": "UTC"})
    check(f"{raw} parses to 21:00",
          (date, time_of_day) == (dt.date(2026, 9, 9), dt.time(21, 0)),
          f"got {date} {time_of_day}")

print("\nEtc/GMT is normalised, because two names for UTC compare as a change")
_, _, zone = _parse_graph_datetime(
    {"dateTime": "2026-09-09T21:00:00", "timeZone": "Etc/GMT"})
check("Etc/GMT reads as UTC", zone == "UTC", repr(zone))

print("\nA value Graph cannot be parsed from still yields its date")
date, time_of_day, zone = _parse_graph_datetime(
    {"dateTime": "2026-09-09 not a timestamp", "timeZone": "UTC"})
check("the date survives an unparseable time", date == dt.date(2026, 9, 9), str(date))
check("and no invented time comes with it", time_of_day is None, str(time_of_day))

print("\nNothing at all is not a date")
for value in (None, {}, {"timeZone": "UTC"}):
    check(f"{value!r} yields nothing",
          _parse_graph_datetime(value) == (None, None, None))

print("\nWhat is written out comes back the same")
for date_part, time_part, zone_name in (
    (dt.date(2026, 9, 9), dt.time(21, 0), "Europe/London"),
    (dt.date(2026, 12, 9), dt.time(9, 30), "America/Denver"),
    (dt.date(2026, 3, 5), None, None),
):
    sent = _graph_datetime(date_part, time_part, zone_name)
    back_date, back_time, back_zone = _parse_graph_datetime(sent)
    check(f"{date_part} {time_part} {zone_name} round-trips",
          back_date == date_part and back_time == (time_part or dt.time(0, 0)),
          f"sent {sent} got {back_date} {back_time} {back_zone!r}")

print("\nImportance is a word in Graph, not a number")
# To Do has three levels against iCalendar's nine, so this cannot be exact. What
# must hold is that it settles: a value that has been through once must not keep
# moving, or every sync pass would rewrite the task.
for value in range(0, 10):
    there = canonical_priority_to_importance(value)
    back = importance_to_canonical_priority(there)
    again = importance_to_canonical_priority(canonical_priority_to_importance(back))
    check(f"priority {value} settles after one round trip ({there})",
          back == again, f"{value} -> {there} -> {back} -> {again}")

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All Microsoft Graph date tests passed.")
