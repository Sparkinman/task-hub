"""Which stored events are sitting at the wrong time, and by how much.

The Google Calendar connector spent its whole life reading the clock face off a
UTC timestamp and pairing it with the zone label beside it, so every timed event
read from Google arrived at Task Hub shifted by that zone's offset. Google
itself kept the right instant throughout -- it stores the moment, not the clock
face -- so the damage is one-sided: the canonical record, and everything Task
Hub then wrote from it, holds a time that Google does not agree with.

That asymmetry is what makes this checkable. For every event Task Hub has linked
to Google, ask Google what it holds now, read it with the corrected connector,
and compare. Anything differing by a whole number of hours was moved by the
fault rather than by a person.

Strictly read-only, twice over: it works from a copy of the database rather than
the live one, and it issues no write to any service. It reports; it does not
repair.

    docker compose cp tests taskhub:/app/
    docker compose exec -e PYTHONPATH=/app -e TASKHUB_DATA_DIR=/tmp/audit \\
        -w /app taskhub python -m tests.audit_event_times
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# The copy is made here, before anything imports the application, because every
# path in it derives from TASKHUB_DATA_DIR at import time.
#
# Note this deliberately does NOT reuse tests.stress_live's copy: that one
# strips every item, link and mapping, which is right for a stress run starting
# from an empty world and precisely wrong here, where those rows are the subject.
LIVE_DATA = Path("/data")
DATA_DIR = Path(os.environ.get("TASKHUB_DATA_DIR", ""))

if not DATA_DIR or DATA_DIR.resolve() == LIVE_DATA.resolve():
    sys.exit(
        "Refusing to run: TASKHUB_DATA_DIR must be a scratch directory, not /data. "
        "This audit reads a copy so that a mistake here cannot reach the real one."
    )

DATA_DIR.mkdir(parents=True, exist_ok=True)
_source = sqlite3.connect(f"file:{LIVE_DATA / 'taskhub.db'}?mode=ro", uri=True)
_target_path = DATA_DIR / "taskhub.db"
_target_path.unlink(missing_ok=True)
_target = sqlite3.connect(_target_path)
with _target:
    # Through the backup API rather than a file copy: the write-ahead log
    # routinely holds more of this database than the file does.
    _source.backup(_target)
_source.close()
_target.close()
shutil.copy2(LIVE_DATA / "secret.key", DATA_DIR / "secret.key")

from app.connectors.base import ConnectorError  # noqa: E402
from app.db.models import (  # noqa: E402
    Account, CollectionKind, Item, ItemLink, RemoteList, ServiceKind,
)
from app.db.session import session_scope  # noqa: E402
from app.sync.engine import build_connector  # noqa: E402


def describe(date, time_of_day, zone) -> str:
    if date is None:
        return "—"
    if time_of_day is None:
        return f"{date} all day"
    return f"{date} {time_of_day.strftime('%H:%M')} {zone or 'floating'}"


def main() -> int:
    print("Stored event times, checked against Google")
    print("=" * 78)
    print("Read-only. Works from a copy of the database; writes nothing anywhere.\n")

    checked = 0
    unchecked = 0
    agreed = 0
    moved: list[tuple[str, str, str, float]] = []
    offsets: Counter[float] = Counter()

    with session_scope() as session:
        google_accounts = (
            session.query(Account)
            .filter(Account.service == ServiceKind.GOOGLE)
            .all()
        )
        if not google_accounts:
            print("No Google account is connected; nothing to check.")
            return 0

        for account in google_accounts:
            try:
                connector = build_connector(session, account)
            except ConnectorError as exc:
                print(f"Could not reach Google ({exc}).")
                return 1

            calendars = (
                session.query(RemoteList)
                .filter(
                    RemoteList.account_id == account.id,
                    RemoteList.kind == CollectionKind.CALENDAR,
                )
                .all()
            )

            for remote_list in calendars:
                links = (
                    session.query(ItemLink)
                    .filter(
                        ItemLink.account_id == account.id,
                        ItemLink.remote_list_id == remote_list.id,
                    )
                    .all()
                )
                if not links:
                    continue

                print(f"\n{remote_list.name}  ({len(links)} linked events)")

                # One pull, then look up by remote id: far kinder to the API
                # than one request per event, and this may be a large calendar.
                try:
                    pulled = {
                        item.remote_id: item.record
                        for item in connector.pull(
                            remote_list.remote_id, CollectionKind.CALENDAR
                        ).items
                    }
                except Exception as exc:  # noqa: BLE001
                    print(f"  could not read this calendar: {exc}")
                    continue

                for link in links:
                    item = session.get(Item, link.item_id)
                    if item is None:
                        continue
                    truth = pulled.get(link.remote_id)
                    if truth is None or truth.start_time is None:
                        # Gone from Google, or an all-day event, which the fault
                        # never touched: no time means no conversion.
                        unchecked += 1
                        continue

                    checked += 1
                    stored = describe(item.start_date, item.start_time, item.start_tz)
                    actual = describe(truth.start_date, truth.start_time, truth.start_tz)

                    if (item.start_date == truth.start_date
                            and item.start_time == truth.start_time):
                        agreed += 1
                        continue

                    stored_at = dt.datetime.combine(
                        item.start_date or truth.start_date,
                        item.start_time or dt.time(0, 0),
                    )
                    actual_at = dt.datetime.combine(truth.start_date, truth.start_time)
                    gap_hours = (stored_at - actual_at).total_seconds() / 3600
                    offsets[gap_hours] += 1
                    moved.append((item.title or "(untitled)", stored, actual, gap_hours))

    print("\n" + "=" * 78)
    print(f"Timed events checked:            {checked}")
    print(f"  agreeing with Google:          {agreed}")
    print(f"  at a different time:           {len(moved)}")
    print(f"All-day or no longer in Google:  {unchecked} (unaffected by the fault)")

    if offsets:
        print("\nHow far out, and how many:")
        for gap, count in sorted(offsets.items()):
            whole = abs(gap - round(gap)) < 0.001
            note = "  <- a whole-hour shift, the signature of the fault" if whole else ""
            print(f"  {gap:+.2f} hours   {count} event(s){note}")

    if moved:
        print(f"\nThe {min(len(moved), 25)} of them, stored -> actually:")
        for title, stored, actual, gap in moved[:25]:
            print(f"  {title[:38]:40} {stored:34} -> {actual}  ({gap:+.0f}h)")
        print(
            "\nGoogle holds the right moment in every case; it is Task Hub's copy,"
            "\nand anything written from it, that is out. Re-reading these events"
            "\nwith the corrected connector is what puts them back -- which the next"
            "\nsync does by itself, now that the connector converts properly."
        )
    elif checked:
        print("\nNothing is out. Every timed event agrees with Google.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
