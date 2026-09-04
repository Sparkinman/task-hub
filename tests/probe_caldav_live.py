"""Point the generic CalDAV connector at a real server and make it work.

Not part of the suite: it talks to a live CalDAV server. It exists because the
generic connector is the same code as Apple's with the address unpinned, and
"the same code" is exactly the claim that deserves checking rather than
asserting -- Apple's account is reached through discovery on a sharded host,
which is not the path a plain server takes.

The server used is Task Hub's own embedded Radicale, which is convenient and
also the point: it is a different CalDAV implementation from iCloud's, so
passing here and against Apple means the connector is not quietly shaped around
one vendor.

Everything it makes, it removes. The collection is named with a timestamp so a
half-finished run can never be mistaken for somebody's real list.
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid

from app.connectors.caldav_remote import RemoteCalDAVConnector
from app.crypto import decrypt_json
from app.db import settings_store
from app.db.models import CollectionKind
from app.db.session import session_scope
from app.services.ical_model import CanonicalRecord

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


with session_scope() as session:
    username = settings_store.get(session, settings_store.RADICALE_USERNAME) or ""
    password = decrypt_json(
        settings_store.get(session, "radicale_password_enc")
    ).get("password", "")

if not username or not password:
    print("No Radicale credentials saved; nothing to probe.")
    sys.exit(0)

BASE = "http://127.0.0.1:8080/radicale/"
STAMP = dt.datetime.now().strftime("%H%M%S")
NAME = f"caldav-probe-{STAMP}"

connector = RemoteCalDAVConnector(
    0, {"username": username, "password": password, "url": BASE},
    default_timezone="America/Denver",
)

created = None
try:
    print("\nSigning in to a server that is not iCloud")
    identity = connector.verify()
    check("it signs in", bool(identity), identity)
    check("and names the account by host as well as user",
          "127.0.0.1" in identity, identity)

    print("\nDiscovery finds what the account owns")
    lists = connector.list_remote_lists()
    check("at least one collection is found", len(lists) > 0, str(len(lists)))
    check("every one declares which kind it is",
          all(l.kind in (CollectionKind.TASKS, CollectionKind.CALENDAR) for l in lists))
    check("and every one has a usable name", all(l.name for l in lists))

    print("\nA to-do survives the round trip")
    targets = [l for l in lists if l.kind == CollectionKind.TASKS]
    if not targets:
        print("  SKIP  no task collection on this server to write into")
    else:
        target = targets[0]
        uid = f"probe-{uuid.uuid4()}"
        record = CanonicalRecord(
            uid=uid,
            kind=CollectionKind.TASKS,
            title=f"CalDAV probe {STAMP}",
            notes="Written by tests/probe_caldav_live.py. Safe to delete.",
            due_date=dt.date.today() + dt.timedelta(days=3),
            due_time=dt.time(14, 30),
            priority=1,
        )
        outcome = connector.create(target.remote_id, record, CollectionKind.TASKS)
        created = (target.remote_id, uid)
        check("it can be created", not outcome.error, outcome.error or "")

        pulled = connector.pull(target.remote_id, CollectionKind.TASKS)
        mine = [i for i in pulled.items if i.record.uid == uid]
        check("it comes back on the next read", len(mine) == 1, str(len(mine)))
        if mine:
            back = mine[0].record
            check("the title survived", back.title == record.title, back.title)
            check("the due date survived", back.due_date == record.due_date,
                  str(back.due_date))
            # The whole argument for CalDAV: every other service drops this.
            check("the time of day survived", back.due_time == record.due_time,
                  str(back.due_time))
            check("the priority survived", back.priority == record.priority,
                  str(back.priority))
            check("and so did the notes", (back.notes or "").startswith("Written by"))

        connector.delete(target.remote_id, uid, CollectionKind.TASKS)
        created = None
        after = connector.pull(target.remote_id, CollectionKind.TASKS)
        check("and deleting it really removes it",
              not [i for i in after.items if i.record.uid == uid])
finally:
    if created:
        try:
            connector.delete(created[0], created[1], CollectionKind.TASKS)
            print("  (cleaned up after a failure)")
        except Exception as exc:  # noqa: BLE001
            print(f"  LEFT BEHIND: {created} -- {exc}")
    connector.close()

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("Generic CalDAV connector verified against a live server.")
