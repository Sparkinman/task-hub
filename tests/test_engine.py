"""End-to-end test of the sync engine using stub connectors.

Uses a throwaway database and two fake services rather than live accounts, so
the full pull -> merge -> plan -> suppress -> push loop can be exercised
deterministically. One stub deliberately mimics Google Tasks by refusing to
store a time of day; the other is lossless like Radicale.

This is the test that proves the user-facing requirement: a date changed in the
service that cannot hold a time must not destroy the time held elsewhere.
"""

from __future__ import annotations

import datetime as dt
import sys

from sqlalchemy import select

from app.connectors.base import (
    F_DUE_DATE, F_DUE_TIME, F_NOTES, F_PRIORITY, F_STATUS, F_TITLE,
    Capabilities, Connector, ConnectorGoneError, PullResult, PushOutcome,
    RemoteItem, RemoteList,
)
from app.db.models import (
    Account, AccountStatus, CollectionKind, ItemStatus, ListMapping,
    RemoteList as RemoteListRow, ServiceKind, SyncGroup,
)
from app.db.session import init_db, session_scope
from app.services.ical_model import CanonicalRecord
from app.sync import engine as engine_module
from app.sync.engine import SyncEngine

UTC = dt.timezone.utc
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


class StubConnector(Connector):
    """An in-memory fake service. Its store survives across sync passes."""

    STORES: dict[str, dict[str, CanonicalRecord]] = {}
    UPDATED: dict[str, dict[str, dt.datetime]] = {}

    def __init__(self, key: str, caps: Capabilities, service: ServiceKind):
        super().__init__(account_id=0, credentials={}, sync_state={})
        self.key = key
        self._caps = caps
        self.service = service
        self.name = key
        self.writes = 0
        #: Remote ids this service will deny the existence of on a write, while
        #: still listing them on a pull. That combination is not hypothetical:
        #: Todoist's completed-task archive kept returning a task minutes after
        #: it was deleted, so every pass recreated it and every push then failed.
        self.gone: set[str] = set()
        StubConnector.STORES.setdefault(key, {})
        StubConnector.UPDATED.setdefault(key, {})

    @property
    def store(self):
        return StubConnector.STORES[self.key]

    @property
    def stamps(self):
        return StubConnector.UPDATED[self.key]

    def capabilities(self, kind):
        return self._caps

    def verify(self):
        return self.key

    def list_remote_lists(self):
        return [RemoteList(remote_id="list1", name="List", kind=CollectionKind.TASKS)]

    def pull(self, remote_list_id, kind, since=None, state=None):
        from app.sync.merge import project

        items = []
        for remote_id, record in self.store.items():
            # A real service can only report what it can store, so the stub
            # projects through its own capabilities before reporting.
            reported = project(record, self._caps)
            if not self._caps.stores_uid:
                # A service with nowhere to keep Task Hub's UID reports none.
                reported.uid = ""
            items.append(
                RemoteItem(
                    remote_id=remote_id,
                    record=reported,
                    fields_present=self._caps.present_fields(),
                    remote_updated_at=self.stamps.get(remote_id),
                )
            )
        return PullResult(items=items, incremental=False)

    def create(self, remote_list_id, record, kind):
        self.writes += 1
        remote_id = f"{self.key}-{len(self.store) + 1}"
        self.store[remote_id] = record
        self.stamps[remote_id] = dt.datetime.now(UTC)
        return PushOutcome(remote_id=remote_id, remote_updated_at=self.stamps[remote_id])

    def update(self, remote_list_id, remote_id, record, kind):
        if remote_id in self.gone:
            raise ConnectorGoneError("That task no longer exists.")
        self.writes += 1
        self.store[remote_id] = record
        self.stamps[remote_id] = dt.datetime.now(UTC)
        return PushOutcome(remote_id=remote_id, remote_updated_at=self.stamps[remote_id])

    def delete(self, remote_list_id, remote_id, kind):
        self.store.pop(remote_id, None)
        return PushOutcome(remote_id=remote_id)


LOSSLESS = Capabilities(
    fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE, F_DUE_TIME, F_PRIORITY}),
    stores_uid=True,
)
# Mimics Google Tasks: keeps the date, cannot keep a time or a priority.
DATE_ONLY = Capabilities(
    fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE})
)

CONNECTORS: dict[int, StubConnector] = {}


def fake_build_connector(session, account):
    return CONNECTORS[account.id]


def setup_world():
    """Two accounts, one sync group, both lists reading and writing."""
    with session_scope() as s:
        local = Account(service=ServiceKind.RADICALE, slot=1, label="Local",
                        status=AccountStatus.CONNECTED)
        google = Account(service=ServiceKind.GOOGLE, slot=1, label="Google",
                         status=AccountStatus.CONNECTED)
        s.add_all([local, google])
        s.commit()

        group = SyncGroup(name="Work", kind=CollectionKind.TASKS, enabled=True)
        s.add(group)
        s.commit()

        for account in (local, google):
            row = RemoteListRow(
                account_id=account.id, remote_id="list1", name="List",
                kind=CollectionKind.TASKS,
            )
            s.add(row)
            s.flush()
            # Participation now comes from the mapping table, which is what
            # lets one list feed several collections.
            s.add(ListMapping(
                remote_list_id=row.id, sync_group_id=group.id,
                read_enabled=True, write_enabled=True,
            ))
        s.commit()

        CONNECTORS[local.id] = StubConnector("local", LOSSLESS, ServiceKind.RADICALE)
        CONNECTORS[google.id] = StubConnector("google", DATE_ONLY, ServiceKind.GOOGLE)
        return local.id, google.id


def run_sync():
    with session_scope() as s:
        return SyncEngine(s).run_sync(trigger="test")


init_db()
engine_module.build_connector = fake_build_connector
engine_module.ensure_radicale_account = lambda session: None

local_id, google_id = setup_world()
local = CONNECTORS[local_id]
google = CONNECTORS[google_id]

# A task created locally, due 5 March at 14:30.
local.store["local-1"] = CanonicalRecord(
    uid="uid-1", kind=CollectionKind.TASKS, title="Call the dentist",
    notes="Ask about the 3pm slot", due_date=dt.date(2026, 3, 5),
    due_time=dt.time(14, 30), due_tz="America/New_York", priority=1,
)
local.stamps["local-1"] = dt.datetime(2026, 3, 1, tzinfo=UTC)

print("\nFirst sync: the task propagates to the date-only service")
run = run_sync()
check("sync completed", run.outcome.value in ("success", "partial"), run.outcome.value)
check("task reached the date-only service", len(google.store) == 1, str(google.store))

pushed = next(iter(google.store.values()), None)
check("its date arrived", pushed and pushed.due_date == dt.date(2026, 3, 5))
check("its time was correctly dropped there", pushed and pushed.due_time is None)
check("the source service was NOT written back to", local.writes == 0,
      f"local was written {local.writes} time(s)")

print("\nA service's own items are not immediately pushed back to it")
# Seed an item that exists only in the date-only service, as a first sync of an
# account that already has content would.
google.store["google-existing"] = CanonicalRecord(
    uid="uid-2", kind=CollectionKind.TASKS, title="Already in Google",
    due_date=dt.date(2026, 4, 1),
)
google.stamps["google-existing"] = dt.datetime(2026, 3, 20, tzinfo=UTC)
local.writes = google.writes = 0
run_sync()
check("it was copied into the lossless service", len(local.store) == 2, str(len(local.store)))
check("GOOGLE WAS NOT REWRITTEN WITH ITS OWN DATA", google.writes == 0,
      f"google was written {google.writes} time(s)")

print("\nSecond sync with nothing changed: no writes at all")
local.writes = google.writes = 0
run = run_sync()
check("nothing was rewritten", local.writes == 0 and google.writes == 0,
      f"local={local.writes} google={google.writes}")
check("unchanged items were counted as skipped", run.items_skipped > 0)

print("\nThe date is changed in the service that cannot store a time")
remote_id = next(iter(google.store))
edited = google.store[remote_id]
edited.due_date = dt.date(2026, 3, 9)
google.stamps[remote_id] = dt.datetime.now(UTC)
run_sync()

survivor = local.store["local-1"]
check("the new date propagated back", survivor.due_date == dt.date(2026, 3, 9),
      str(survivor.due_date))
check("THE TIME OF DAY SURVIVED", survivor.due_time == dt.time(14, 30),
      f"got {survivor.due_time}")
check("the timezone survived", survivor.due_tz == "America/New_York")
check("the priority survived", survivor.priority == 1)

print("\nTwo services changing the SAME field in one pass")
# Both edit the title before the next sync. Provenance is recorded per field
# per item, and the row added while merging the first service was still pending
# and invisible to the second's lookup -- which then added a second row for the
# same (item, field) and hit the unique constraint at commit. That rolled back
# every merge in the group, so the edits vanished and the push wrote the old
# values straight back over both services. It read as "nothing synced".
local_key = "local-1"
g_key = next(iter(google.store))
local.store[local_key].title = "Renamed in the lossless service"
local.stamps[local_key] = dt.datetime(2026, 3, 12, 10, tzinfo=UTC)
google.store[g_key].title = "Renamed in Google"
google.stamps[g_key] = dt.datetime(2026, 3, 12, 11, tzinfo=UTC)

run = run_sync()
check("THE PASS COMPLETED WITHOUT ERROR", run.errors == 0, f"{run.errors} error(s)")
check("the newer of the two edits won",
      local.store[local_key].title == "Renamed in Google",
      local.store[local_key].title)
with session_scope() as _s:
    from app.db.models import FieldProvenance as _FP
    rows = _s.execute(select(_FP).where(_FP.field == "title")).scalars().all()
    per_item = {}
    for r in rows:
        per_item[r.item_id] = per_item.get(r.item_id, 0) + 1
check("only one provenance row per item and field",
      all(n == 1 for n in per_item.values()), str(per_item))

print("\nProvenance survives a round trip through the database")
# The previous sync recorded which service last changed the due date. This pass
# reloads that row and compares it against a live timestamp -- the exact path
# where a timestamp stripped of its timezone by SQLite would abort the group.
with session_scope() as _s:
    from app.db.models import FieldProvenance
    saved = _s.execute(select(FieldProvenance)).scalars().all()
    check("provenance rows were written", len(saved) > 0, f"{len(saved)} rows")
    aware = [p for p in saved if p.changed_at.tzinfo is not None]
    check("stored timestamps come back timezone-aware", len(aware) == len(saved),
          f"{len(saved) - len(aware)} naive of {len(saved)}")

google_id_key2 = next(iter(google.store))
google.store[google_id_key2].title = "Call the dentist urgently"
google.stamps[google_id_key2] = dt.datetime.now(UTC)
run = run_sync()
check("a sync using stored provenance does not error", run.errors == 0,
      f"{run.errors} error(s)")
check("the newer title was applied", local.store["local-1"].title == "Call the dentist urgently",
      local.store["local-1"].title)

print("\nCompleting in one service completes it everywhere, then stops writing")
google_id_key = next(iter(google.store))
google.store[google_id_key].status = ItemStatus.COMPLETED
google.store[google_id_key].completed_at = dt.datetime.now(UTC)
google.stamps[google_id_key] = dt.datetime.now(UTC)
run_sync()
check("completion propagated", local.store["local-1"].status == ItemStatus.COMPLETED,
      str(local.store["local-1"].status))

local.writes = google.writes = 0
run_sync()
check("a completed task is NOT rewritten on the next pass",
      local.writes == 0 and google.writes == 0,
      f"local={local.writes} google={google.writes}")

print("\nA transient empty response must not be read as mass deletion")
saved = dict(google.store)
google.store.clear()
run_sync()
check("nothing was deleted locally", "local-1" in local.store)
google.store.update(saved)

print("\nA write refused because the task is gone settles instead of repeating")
# Reproduces the loop seen in the live install: a task deleted at the service
# whose archive still listed it. Every pass recreated it from the listing and
# every push then failed with "no longer exists", forever.
local.store["local-doomed"] = CanonicalRecord(
    uid="uid-doomed", kind=CollectionKind.TASKS, title="Deleted in Google",
    due_date=dt.date(2026, 5, 1),
)
local.stamps["local-doomed"] = dt.datetime(2026, 4, 1, tzinfo=UTC)
run_sync()
doomed_remote = next(
    (rid for rid, rec in google.store.items() if rec.title == "Deleted in Google"),
    None,
)
check("the task reached the other service", doomed_remote is not None)

# It is deleted there, but the service goes on listing it -- so the pull still
# reports it and only the write finds out.
google.gone.add(doomed_remote)
local.store["local-doomed"].title = "Deleted in Google, edited here"
local.stamps["local-doomed"] = dt.datetime.now(UTC)

run = run_sync()
check("the refusal was not logged as an error", run.errors == 0, f"{run.errors} error(s)")
check("AND REMOVED FROM THE OTHER SERVICES", "local-doomed" not in local.store,
      "it is still in the lossless service")

print("\nAnd the service still listing it does not bring it back")
# The tombstone has to match on the remote id here. This service cannot store a
# UID, so the task it reports has an empty one and no UID comparison can catch
# it -- which is exactly how a deleted task used to reappear on the next pass.
run = run_sync()
check("the deleted task was NOT recreated", "local-doomed" not in local.store,
      "it came back")
check("no error was raised doing so", run.errors == 0, f"{run.errors} error(s)")

print("\nA whole list refusing every write deletes nothing")
# The same refusal for every task means the list itself is gone, or the account
# lost access to it. Acting on that would empty the user's other services.
for n in range(1, 8):
    local.store[f"local-bulk{n}"] = CanonicalRecord(
        uid=f"uid-bulk{n}", kind=CollectionKind.TASKS, title=f"Bulk {n}",
        due_date=dt.date(2026, 6, 1),
    )
    local.stamps[f"local-bulk{n}"] = dt.datetime(2026, 5, 1, tzinfo=UTC)
run_sync()
bulk_remotes = [rid for rid, rec in google.store.items()
                if (rec.title or "").startswith("Bulk ")]
check("all of them reached the other service", len(bulk_remotes) == 7,
      f"{len(bulk_remotes)} of 7")

google.gone.update(bulk_remotes)
for n in range(1, 8):
    local.store[f"local-bulk{n}"].title = f"Bulk {n} edited"
    local.stamps[f"local-bulk{n}"] = dt.datetime.now(UTC)

run = run_sync()
survivors = [k for k in local.store if k.startswith("local-bulk")]
check("NOTHING WAS DELETED", len(survivors) == 7, f"{len(survivors)} of 7 left")
check("and the run reported the problem", run.errors > 0, "no error was raised")

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All engine tests passed.")
