"""End-to-end scenarios across every connected service at once.

Each service was added and proved largely on its own. This is the suite that
runs them *together*, which is where the interesting failures live: a task that
settles correctly between two services can still be rewritten for ever by a
third, and a field one service cannot hold can be wiped by a fourth that can.

It is deliberately built on stub connectors and a throwaway database rather
than live accounts, for two reasons. It has to be runnable at any moment --
including while somebody is testing against the real accounts from another
device -- without putting test tasks into their Google or their Todoist. And it
has to be deterministic: a live suite that fails once a fortnight because a
service was slow teaches nobody anything.

The stubs are not toys. Each declares the same capabilities as the real
connector it stands for, and reports back only what it could really store, so
the merge engine is exercised against the same lossiness it meets in
production.
"""

from __future__ import annotations

import datetime as dt
import sys

from sqlalchemy import select

from app.connectors.base import (
    F_DUE_DATE, F_DUE_TIME, F_END, F_LOCATION, F_NOTES, F_PRIORITY, F_START,
    F_STATUS, F_TAGS, F_TITLE, Capabilities, Connector, PullResult, PushOutcome,
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


# --- The services, as they actually behave -----------------------------------

ALL = frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE, F_DUE_TIME,
                 F_PRIORITY, F_TAGS, F_START, F_END, F_LOCATION})

#: Task Hub's own store loses nothing.
RADICALE = Capabilities(fields=ALL, stores_uid=True, carries_origin=True)

#: Google Tasks: keeps a date, discards the time, has no priority or tags.
GOOGLE = Capabilities(fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE}))

#: Todoist: times and priorities, and a span through its deadline field.
TODOIST = Capabilities(fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE,
                                         F_DUE_TIME, F_PRIORITY, F_TAGS, F_START}))

#: TickTick: like Todoist, but deliberately never reports a start date.
TICKTICK = Capabilities(fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE,
                                          F_DUE_TIME, F_PRIORITY, F_TAGS}))

#: Obsidian: reads a lot, writes only whether a task is ticked.
OBSIDIAN_READONLY = Capabilities(
    fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE, F_TAGS, F_START}),
    can_create=False, can_delete=False, writable_fields=frozenset(),
)
OBSIDIAN_WRITING = Capabilities(
    fields=OBSIDIAN_READONLY.fields,
    can_create=False, can_delete=False, writable_fields=frozenset({F_STATUS}),
)


class Stub(Connector):
    """An in-memory service that reports only what it could really store."""

    def __init__(self, key, caps, service):
        super().__init__(account_id=0, credentials={}, sync_state={})
        self.key, self._caps, self.service = key, caps, service
        self.name = key
        self.store: dict[str, CanonicalRecord] = {}
        self.stamps: dict[str, dt.datetime] = {}
        self.writes = 0
        self.refused = 0

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
            reported = project(record, self._caps)
            if not self._caps.stores_uid:
                reported.uid = ""
            items.append(RemoteItem(
                remote_id=remote_id, record=reported,
                fields_present=self._caps.present_fields(),
                remote_updated_at=self.stamps.get(remote_id),
            ))
        return PullResult(items=items, incremental=False)

    def create(self, remote_list_id, record, kind):
        if not self._caps.can_create:
            self.refused += 1
            return PushOutcome(remote_id=None, error=f"{self.key} cannot create.")
        self.writes += 1
        remote_id = f"{self.key}-{len(self.store) + 1}"
        self.store[remote_id] = record
        self.stamps[remote_id] = dt.datetime.now(UTC)
        return PushOutcome(remote_id=remote_id, remote_updated_at=self.stamps[remote_id])

    def update(self, remote_list_id, remote_id, record, kind):
        if not self._caps.push_fields():
            self.refused += 1
            return PushOutcome(remote_id=remote_id, error=f"{self.key} is read-only.")
        self.writes += 1
        if self._caps.push_fields() == frozenset({F_STATUS}):
            # Writes only the tick, exactly as the Obsidian connector does.
            held = self.store.get(remote_id)
            if held is not None:
                held.status = record.status
                held.completed_at = record.completed_at
                self.stamps[remote_id] = dt.datetime.now(UTC)
                return PushOutcome(remote_id=remote_id,
                                   remote_updated_at=self.stamps[remote_id])
        self.store[remote_id] = record
        self.stamps[remote_id] = dt.datetime.now(UTC)
        return PushOutcome(remote_id=remote_id, remote_updated_at=self.stamps[remote_id])

    def delete(self, remote_list_id, remote_id, kind):
        if not self._caps.can_delete:
            self.refused += 1
            return PushOutcome(remote_id=remote_id, error=f"{self.key} cannot delete.")
        self.store.pop(remote_id, None)
        return PushOutcome(remote_id=remote_id)


CONNECTORS: dict[int, Stub] = {}
engine_module.build_connector = lambda session, account: CONNECTORS[account.id]
engine_module.ensure_radicale_account = lambda session: None

SERVICES = [
    ("local",    ServiceKind.RADICALE, RADICALE),
    ("google",   ServiceKind.GOOGLE,   GOOGLE),
    ("todoist",  ServiceKind.TODOIST,  TODOIST),
    ("ticktick", ServiceKind.TICKTICK, TICKTICK),
    ("obsidian", ServiceKind.OBSIDIAN, OBSIDIAN_READONLY),
]


def build_world() -> dict[str, Stub]:
    with session_scope() as s:
        group = SyncGroup(name="All", kind=CollectionKind.TASKS, enabled=True)
        s.add(group)
        s.commit()
        for key, kind, caps in SERVICES:
            account = Account(service=kind, slot=1, label=key,
                              status=AccountStatus.CONNECTED)
            s.add(account)
            s.commit()
            row = RemoteListRow(account_id=account.id, remote_id="list1",
                                name="List", kind=CollectionKind.TASKS)
            s.add(row)
            s.flush()
            s.add(ListMapping(remote_list_id=row.id, sync_group_id=group.id,
                              read_enabled=True, write_enabled=True))
            s.commit()
            CONNECTORS[account.id] = Stub(key, caps, kind)
    return {c.key: c for c in CONNECTORS.values()}


def sync():
    with session_scope() as s:
        return SyncEngine(s).run_sync(trigger="test")


def writes_reset(svc):
    for c in svc.values():
        c.writes = 0


init_db()
svc = build_world()
print("Five services: Radicale, Google, Todoist, TickTick, Obsidian\n")

# --- 1. Propagation from every direction --------------------------------------

print("1. A task made in each service reaches all the others")

for origin in ("local", "google", "todoist", "ticktick"):
    svc[origin].store[f"{origin}-seed"] = CanonicalRecord(
        uid=f"uid-{origin}", kind=CollectionKind.TASKS,
        title=f"Made in {origin}", due_date=dt.date(2026, 3, 5),
    )
    svc[origin].stamps[f"{origin}-seed"] = dt.datetime.now(UTC)

sync()
for origin in ("local", "google", "todoist", "ticktick"):
    titles = {r.title for c in svc.values() for r in c.store.values()}
    check(f"a task made in {origin} reached the others",
          all(any(r.title == f"Made in {origin}" for r in svc[k].store.values())
              for k in ("local", "google", "todoist", "ticktick")),
          f"titles seen: {sorted(titles)}")

check("read-only Obsidian was not written to", svc["obsidian"].writes == 0)

# --- 2. It settles ------------------------------------------------------------

print("\n2. A second pass writes nothing")
sync()
writes_reset(svc)
run = sync()
total = sum(c.writes for c in svc.values())
check("a settled set produces no writes at all", total == 0, f"{total} write(s)")
check("the run is clean", run.outcome.value == "success", run.outcome.value)

# --- 3. The lossy-field case --------------------------------------------------

print("\n3. A date edited in Google does not destroy a time set elsewhere")

with_time = CanonicalRecord(
    uid="uid-timed", kind=CollectionKind.TASKS, title="Dentist",
    due_date=dt.date(2026, 3, 5), due_time=dt.time(14, 30), due_tz="America/Denver",
)
svc["todoist"].store["todoist-timed"] = with_time
svc["todoist"].stamps["todoist-timed"] = dt.datetime.now(UTC)
sync()

google_copy = next((k for k, r in svc["google"].store.items() if r.title == "Dentist"), None)
check("it reached Google", google_copy is not None)
check("Google holds the date only", svc["google"].store[google_copy].due_time is None)

# Move the date in Google, as a person would.
moved = svc["google"].store[google_copy]
moved.due_date = dt.date(2026, 3, 9)
svc["google"].stamps[google_copy] = dt.datetime.now(UTC) + dt.timedelta(seconds=5)
sync()

for name in ("todoist", "ticktick", "local"):
    held = next((r for r in svc[name].store.values() if r.title == "Dentist"), None)
    check(f"{name}: the new date arrived",
          held is not None and held.due_date == dt.date(2026, 3, 9),
          str(held.due_date if held else None))
    check(f"{name}: the 2:30pm survived",
          held is not None and held.due_time == dt.time(14, 30),
          str(held.due_time if held else None))

writes_reset(svc)
sync()
check("and it settles again afterwards", sum(c.writes for c in svc.values()) == 0,
      f"{sum(c.writes for c in svc.values())} write(s)")

# --- 4. Completion ------------------------------------------------------------

print("\n4. Completing in one service completes everywhere")

tick = next(k for k, r in svc["ticktick"].store.items() if r.title == "Dentist")
svc["ticktick"].store[tick].status = ItemStatus.COMPLETED
svc["ticktick"].store[tick].completed_at = dt.datetime.now(UTC)
svc["ticktick"].stamps[tick] = dt.datetime.now(UTC) + dt.timedelta(seconds=10)
sync()

for name in ("local", "google", "todoist"):
    held = next((r for r in svc[name].store.values() if r.title == "Dentist"), None)
    check(f"{name}: marked complete",
          held is not None and held.status == ItemStatus.COMPLETED,
          str(held.status if held else None))

writes_reset(svc)
sync()
check("a completed task is not rewritten on the next pass",
      sum(c.writes for c in svc.values()) == 0,
      f"{sum(c.writes for c in svc.values())} write(s)")

# --- 5. Obsidian's write-back -------------------------------------------------

print("\n5. Obsidian writes the tick and nothing else")

obs = next(a for a, c in CONNECTORS.items() if c.key == "obsidian")
CONNECTORS[obs]._caps = OBSIDIAN_WRITING
svc["obsidian"].store["obs-1"] = CanonicalRecord(
    uid="uid-obs", kind=CollectionKind.TASKS, title="From the vault",
    due_date=dt.date(2026, 4, 1),
)
svc["obsidian"].stamps["obs-1"] = dt.datetime.now(UTC)
sync()
sync()

held = next((r for r in svc["todoist"].store.values() if r.title == "From the vault"), None)
check("a vault task reached the other services", held is not None)

# Rename it elsewhere: the title must not travel back into the vault.
if held is not None:
    held.title = "Renamed in Todoist"
    key = next(k for k, r in svc["todoist"].store.items() if r is held)
    svc["todoist"].stamps[key] = dt.datetime.now(UTC) + dt.timedelta(seconds=20)
    sync()
    check("the vault's copy keeps its own wording",
          svc["obsidian"].store["obs-1"].title == "From the vault",
          svc["obsidian"].store["obs-1"].title)

# Tick it off elsewhere: that much should reach the vault.
    held.status = ItemStatus.COMPLETED
    held.completed_at = dt.datetime.now(UTC)
    svc["todoist"].stamps[key] = dt.datetime.now(UTC) + dt.timedelta(seconds=30)
    sync()
    check("but a completion does reach the vault",
          svc["obsidian"].store["obs-1"].status == ItemStatus.COMPLETED,
          str(svc["obsidian"].store["obs-1"].status))

# --- 6. Multi-day spans -------------------------------------------------------

print("\n6. A task spanning several days keeps its span")

span = CanonicalRecord(
    uid="uid-span", kind=CollectionKind.TASKS, title="Write the report",
    start_date=dt.date(2026, 5, 4), due_date=dt.date(2026, 5, 8),
)
svc["local"].store["local-span"] = span
svc["local"].stamps["local-span"] = dt.datetime.now(UTC)
sync()
sync()

held = next((r for r in svc["todoist"].store.values() if r.title == "Write the report"), None)
check("a span reached the service that can hold one",
      held is not None and held.start_date == dt.date(2026, 5, 4)
      and held.due_date == dt.date(2026, 5, 8),
      f"{held.start_date if held else None} -> {held.due_date if held else None}")

tt = next((r for r in svc["ticktick"].store.values() if r.title == "Write the report"), None)
check("the service that cannot hold a start took the due date only",
      tt is not None and tt.due_date == dt.date(2026, 5, 8),
      str(tt.due_date if tt else None))

kept = next((r for r in svc["local"].store.values() if r.title == "Write the report"), None)
check("and the span survived in Task Hub's own store",
      kept is not None and kept.start_date == dt.date(2026, 5, 4),
      str(kept.start_date if kept else None))

writes_reset(svc)
sync()
check("a spanning task settles too", sum(c.writes for c in svc.values()) == 0,
      f"{sum(c.writes for c in svc.values())} write(s)")

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All scenario tests passed.")
