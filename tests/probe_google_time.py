"""Does a timed Google Calendar event come back at the time it was written?

The live diagnosis showed events losing an hour on their first round trip --
``22:00 Europe/London`` written, ``21:00 Europe/London`` read back. That is a
different moment, not a different way of writing the same one, so it matters far
more than the redundant write it also causes.

This probe isolates it to one event in a calendar of its own, and prints three
things: what was sent, the raw JSON Google stored, and what the connector made
of that JSON on the way back. Whichever of the two boundaries is wrong shows up
immediately, and they need opposite fixes.

Depends on nothing but Google, so it still runs when another service is down.

    docker compose exec -e PYTHONPATH=/app -e TASKHUB_DATA_DIR=/tmp/gprobe \\
        -w /app taskhub python -m tests.probe_google_time
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

os.environ.setdefault("STRESS_TASKS", "1")
os.environ.setdefault("STRESS_EVENTS", "1")

import tests.stress_live as SL  # noqa: E402  (prepares the isolated data dir)

from app.db.models import CollectionKind, ServiceKind  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.services.ical_model import CanonicalRecord  # noqa: E402

ZONE = "Europe/London"

#: September, when London is on British Summer Time (UTC+1). A zone whose offset
#: is not zero is the whole point: an implementation that confuses wall time with
#: UTC looks perfectly correct in winter and loses an hour in summer.
CASES = [
    ("evening, crossing midnight", dt.date(2026, 9, 9), dt.time(22, 0),
     dt.date(2026, 9, 10), dt.time(6, 30)),
    ("mid-morning, same day", dt.date(2026, 9, 9), dt.time(9, 0),
     dt.date(2026, 9, 9), dt.time(10, 30)),
    ("winter, when London is UTC", dt.date(2026, 12, 9), dt.time(9, 0),
     dt.date(2026, 12, 9), dt.time(10, 30)),
]


def main() -> int:
    print("Google Calendar time round-trip")
    print("=" * 74)

    failures = 0
    try:
        with session_scope() as session:
            account, google = SL.connector_for(session, ServiceKind.GOOGLE)
            if google is None:
                print("No Google account connected.")
                return 1

            from app.connectors.google import CALENDAR_API

            name = f"TaskHub Probe {SL.RUN_ID}"
            created = google._request(
                "POST", f"{CALENDAR_API}/calendars", json={"summary": name}) or {}
            calendar_id = created.get("id", "")
            SL.registry_add({"service": "google", "kind": "calendar", "id": calendar_id})
            print(f"probe calendar {calendar_id}\n")

            for label, start_date, start_time, end_date, end_time in CASES:
                sent = CanonicalRecord(
                    uid=f"probe-{label}", kind=CollectionKind.CALENDAR,
                    title=f"Probe: {label}",
                    start_date=start_date, start_time=start_time, start_tz=ZONE,
                    end_date=end_date, end_time=end_time, end_tz=ZONE,
                )
                outcome = google.create(calendar_id, sent, CollectionKind.CALENDAR)
                raw = google._request(
                    "GET",
                    f"{CALENDAR_API}/calendars/{calendar_id}/events/{outcome.remote_id}")

                back = None
                for item in google.pull(calendar_id, CollectionKind.CALENDAR).items:
                    if item.remote_id == outcome.remote_id:
                        back = item.record

                print(f"{label}")
                print(f"  sent      start {start_date} {start_time} {ZONE}")
                print(f"            end   {end_date} {end_time} {ZONE}")
                print(f"  Google's  start {json.dumps(raw.get('start'))}")
                print(f"            end   {json.dumps(raw.get('end'))}")
                if back is None:
                    print("  READ BACK: the event did not come back at all")
                    failures += 1
                    continue
                print(f"  read back start {back.start_date} {back.start_time} "
                      f"{back.start_tz!r}")
                print(f"            end   {back.end_date} {back.end_time} "
                      f"{back.end_tz!r}")

                same = (back.start_date == start_date and back.start_time == start_time
                        and back.end_date == end_date and back.end_time == end_time)
                print(f"  VERDICT   {'unchanged' if same else 'MOVED'}\n")
                if not same:
                    failures += 1

    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    finally:
        print("=" * 74)
        print("Teardown")
        SL.teardown()

    print(f"\n{failures} case(s) came back at a different time.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
