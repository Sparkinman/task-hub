"""What Todoist and TickTick hand back after storing a timed, prioritised task.

The live diagnosis showed two things changing on the way back: a start time
losing its timezone label, and a priority arriving one step from where it was
sent. Neither is a moved value -- the clock face and the ordering survive -- but
both look like edits to the merge, which then rewrites the canonical record and
pushes the "change" everywhere else.

This prints what was sent, the raw JSON, and what the connector made of it, for
one task in a project of its own.

    docker compose exec -e PYTHONPATH=/app -e TASKHUB_DATA_DIR=/tmp/tprobe \\
        -w /app taskhub python -m tests.probe_todoist
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


def probe(session, service: ServiceKind, make_container, raw_of, label: str) -> None:
    account, connector = SL.connector_for(session, service)
    if connector is None:
        print(f"\n{label}: not connected")
        return

    try:
        container = make_container(connector)
    except Exception as exc:  # noqa: BLE001
        print(f"\n{label}: could not make a project ({exc})")
        return

    sent = CanonicalRecord(
        uid="probe-task", kind=CollectionKind.TASKS, title="Probe task",
        due_date=dt.date(2026, 9, 9), due_time=dt.time(17, 30), due_tz=ZONE,
        start_date=dt.date(2026, 9, 9), start_time=dt.time(9, 0), start_tz=ZONE,
        priority=2,
    )
    print(f"\n{label}")
    print(f"  sent      due   {sent.due_date} {sent.due_time} {sent.due_tz!r}")
    print(f"            start {sent.start_date} {sent.start_time} {sent.start_tz!r}")
    print(f"            priority {sent.priority}")

    outcome = connector.create(container, sent, CollectionKind.TASKS)
    try:
        raw = raw_of(connector, container, outcome.remote_id)
        print(f"  raw       {json.dumps({k: v for k, v in raw.items() if k in ('due', 'deadline', 'duration', 'priority', 'dueDate', 'startDate', 'timeZone', 'isAllDay')})}")
    except Exception as exc:  # noqa: BLE001
        print(f"  raw       unavailable ({exc})")

    for item in connector.pull(container, CollectionKind.TASKS).items:
        if item.remote_id == outcome.remote_id:
            back = item.record
            print(f"  read back due   {back.due_date} {back.due_time} {back.due_tz!r}")
            print(f"            start {back.start_date} {back.start_time} {back.start_tz!r}")
            print(f"            priority {back.priority}")
            for name, before, after in (
                ("due", (sent.due_date, sent.due_time, sent.due_tz),
                 (back.due_date, back.due_time, back.due_tz)),
                ("start", (sent.start_date, sent.start_time, sent.start_tz),
                 (back.start_date, back.start_time, back.start_tz)),
                ("priority", sent.priority, back.priority),
            ):
                if before != after:
                    print(f"  DIFFERS   {name}: {before!r} -> {after!r}")


def main() -> int:
    print("Todoist and TickTick round-trip")
    print("=" * 74)

    try:
        with session_scope() as session:
            name = f"TaskHub Probe {SL.RUN_ID}"

            def todoist_container(connector):
                created = connector._request(
                    "POST", "/projects", json={"name": name}) or {}
                SL.registry_add({"service": "todoist", "kind": "project",
                                 "id": created.get("id", "")})
                return created["id"]

            def ticktick_container(connector):
                created = connector._request(
                    "POST", "/project", json={"name": name}) or {}
                SL.registry_add({"service": "ticktick", "kind": "project",
                                 "id": created.get("id", "")})
                return created["id"]

            probe(session, ServiceKind.TODOIST, todoist_container,
                  lambda c, container, rid: c._request("GET", f"/tasks/{rid}"),
                  "TODOIST")
            probe(session, ServiceKind.TICKTICK, ticktick_container,
                  lambda c, container, rid: c._request(
                      "GET", f"/project/{container}/task/{rid}"),
                  "TICKTICK")

    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    finally:
        print("\n" + "=" * 74)
        print("Teardown")
        SL.teardown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
