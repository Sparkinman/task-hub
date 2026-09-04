"""A timed task must not be rewritten just because a service renames its zone.

Most services do not store the timezone a time was written in. They store the
instant and report it back in a zone of their own choosing -- TickTick sends the
UTC instant with the account's zone named separately, Google Calendar answers in
the calendar's zone, and Todoist falls back to the account default. So a task
written as half past five in London comes back as half past ten in Denver: the
same moment, a different clock face.

Nothing is wrong with that value, and the live stress run confirmed the merge
handles it correctly -- no time was ever corrupted. What it does not handle is
the *bookkeeping*: the hash that decides whether a write is needed is taken over
the clock face, so an item that round-trips through a zone-shifting service
looks changed when it is not, and is written a second time.

That is what this test pins down. It cost 533 redundant writes on the second
pass of a 400-item live run -- a first sync roughly 1.7 times more expensive
than it needs to be, on exactly the operation every service rate-limits hardest.
"""

from __future__ import annotations

import datetime as dt
import sys

from app.connectors.base import (
    F_DUE_DATE, F_DUE_TIME, F_END, F_START, F_STATUS, F_TITLE,
    Capabilities, Connector, PullResult, PushOutcome, RemoteItem, RemoteList,
)
from app.db.models import (
    Account, AccountStatus, CollectionKind, ListMapping,
    RemoteList as RemoteListRow, ServiceKind, SyncGroup,
)
from app.db.session import init_db, session_scope
from app.services.ical_model import CanonicalRecord
from app.services.timezones import to_utc, wall_time
from app.sync import engine as engine_module
from app.sync.engine import SyncEngine

UTC = dt.timezone.utc
_failures: list[str] = []

#: The zone the fake service answers in, whatever zone it was written in. A real
#: account's zone, not the one the data was authored in, which is the whole
#: point.
#: Overridable so the same script can be run as a control: setting it to the
#: zone the data was authored in removes the shift without changing anything
#: else, which separates "the zone moved" from "the engine writes once anyway".
SERVICE_ZONE = __import__("os").environ.get("SERVICE_ZONE", "America/Denver")


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


class ZoneShiftingConnector(Connector):
    """A service that keeps the instant and forgets the zone it was given.

    This is not a contrived fake: it is what TickTick, Google Calendar and
    Todoist each do in their own way. The record it hands back is correct --
    it names the same moment -- but it names it in :data:`SERVICE_ZONE`.
    """

    def __init__(self, key: str, caps: Capabilities, service: ServiceKind):
        super().__init__(account_id=0, credentials={}, sync_state={})
        self.key = key
        self._caps = caps
        self.service = service
        self.name = key
        self.store: dict[str, CanonicalRecord] = {}
        self.stamps: dict[str, dt.datetime] = {}
        self.writes = 0

    def capabilities(self, kind):
        return self._caps

    def verify(self):
        return self.key

    def list_remote_lists(self):
        return [RemoteList(remote_id="list1", name="List", kind=CollectionKind.TASKS)]

    def _restate(self, record: CanonicalRecord) -> CanonicalRecord:
        """Re-express every timed field in this service's own zone."""
        from app.sync.merge import project

        shown = project(record, self._caps)
        shown.uid = "" if not self._caps.stores_uid else shown.uid

        if shown.due_time is not None:
            moment = to_utc(shown.due_date, shown.due_time, shown.due_tz, SERVICE_ZONE)
            if moment is not None:
                date, time_of_day, label = wall_time(
                    moment.isoformat(), SERVICE_ZONE, SERVICE_ZONE
                )
                shown.due_date, shown.due_time, shown.due_tz = date, time_of_day, label

        if shown.start_time is not None:
            moment = to_utc(shown.start_date, shown.start_time, shown.start_tz, SERVICE_ZONE)
            if moment is not None:
                date, time_of_day, label = wall_time(
                    moment.isoformat(), SERVICE_ZONE, SERVICE_ZONE
                )
                shown.start_date, shown.start_time, shown.start_tz = date, time_of_day, label

        return shown

    def pull(self, remote_list_id, kind, since=None, state=None):
        items = [
            RemoteItem(
                remote_id=remote_id,
                record=self._restate(record),
                fields_present=self._caps.present_fields(),
                remote_updated_at=self.stamps.get(remote_id),
            )
            for remote_id, record in self.store.items()
        ]
        return PullResult(items=items, incremental=False)

    def create(self, remote_list_id, record, kind):
        self.writes += 1
        remote_id = f"{self.key}-{len(self.store) + 1}"
        self.store[remote_id] = record
        self.stamps[remote_id] = dt.datetime.now(UTC)
        return PushOutcome(remote_id=remote_id, remote_updated_at=self.stamps[remote_id])

    def update(self, remote_list_id, remote_id, record, kind):
        self.writes += 1
        self.store[remote_id] = record
        self.stamps[remote_id] = dt.datetime.now(UTC)
        return PushOutcome(remote_id=remote_id, remote_updated_at=self.stamps[remote_id])

    def delete(self, remote_list_id, remote_id, kind):
        self.store.pop(remote_id, None)
        return PushOutcome(remote_id=remote_id)


TIMED = Capabilities(
    fields=frozenset({F_TITLE, F_STATUS, F_DUE_DATE, F_DUE_TIME, F_START, F_END}),
    stores_uid=True,
    carries_origin=True,
)
#: Same fields, but nowhere to keep Task Hub's UID or origin marker -- which is
#: the ordinary case, and the one where an echo is hardest to recognise.
TIMED_ANONYMOUS = Capabilities(
    fields=frozenset({F_TITLE, F_STATUS, F_DUE_DATE, F_DUE_TIME, F_START, F_END}),
)

CONNECTORS: dict[int, ZoneShiftingConnector] = {}


def fake_build_connector(session, account):
    return CONNECTORS[account.id]


def setup_world():
    with session_scope() as s:
        source = Account(service=ServiceKind.RADICALE, slot=1, label="Source",
                         status=AccountStatus.CONNECTED)
        remote = Account(service=ServiceKind.TICKTICK, slot=1, label="Zoned",
                         status=AccountStatus.CONNECTED)
        s.add_all([source, remote])
        s.commit()

        group = SyncGroup(name="Work", kind=CollectionKind.TASKS, enabled=True)
        s.add(group)
        s.commit()

        for account in (source, remote):
            row = RemoteListRow(account_id=account.id, remote_id="list1",
                                name="List", kind=CollectionKind.TASKS)
            s.add(row)
            s.flush()
            s.add(ListMapping(remote_list_id=row.id, sync_group_id=group.id,
                              read_enabled=True, write_enabled=True))
        s.commit()

        CONNECTORS[source.id] = ZoneShiftingConnector("source", TIMED, ServiceKind.RADICALE)
        CONNECTORS[remote.id] = ZoneShiftingConnector(
            "zoned", TIMED_ANONYMOUS, ServiceKind.TICKTICK)
        return source.id, remote.id


def run_sync():
    with session_scope() as s:
        return SyncEngine(s).run_sync(trigger="test")


init_db()
engine_module.build_connector = fake_build_connector
engine_module.ensure_radicale_account = lambda session: None

source_id, remote_id = setup_world()
source = CONNECTORS[source_id]
zoned = CONNECTORS[remote_id]

# The source is lossless and does not shift anything: it reports what it holds.
source._restate = lambda record: record  # type: ignore[method-assign]

source.store["source-1"] = CanonicalRecord(
    uid="uid-1", kind=CollectionKind.TASKS, title="Board the ferry",
    due_date=dt.date(2026, 3, 5), due_time=dt.time(17, 30), due_tz="Europe/London",
    start_date=dt.date(2026, 3, 5), start_time=dt.time(9, 0), start_tz="Europe/London",
)
source.stamps["source-1"] = dt.datetime(2026, 3, 1, tzinfo=UTC)

print("\nPass 1: the task propagates to the zone-shifting service")
zoned.writes = 0
run_sync()
check("task reached the zoned service", len(zoned.store) == 1, str(zoned.store))
check("pass 1 wrote it once", zoned.writes == 1, f"{zoned.writes} writes")

print("\nPass 2: nothing has changed, so nothing should be written")
zoned.writes = 0
source.writes = 0
run_sync()
check("pass 2 wrote nothing to the zoned service", zoned.writes == 0,
      f"{zoned.writes} write(s) — the zone was renamed, not the moment")
check("pass 2 wrote nothing back to the source", source.writes == 0,
      f"{source.writes} write(s)")

print("\nPass 3: still quiet")
zoned.writes = 0
source.writes = 0
run_sync()
check("pass 3 wrote nothing", zoned.writes == 0 and source.writes == 0,
      f"zoned {zoned.writes}, source {source.writes}")

print("\nThe moment itself must be unchanged after all the round trips")
held = next(iter(zoned.store.values()))
instant = to_utc(held.due_date, held.due_time, held.due_tz, SERVICE_ZONE)
expected = dt.datetime(2026, 3, 5, 17, 30, tzinfo=UTC) - dt.timedelta(0)
expected = to_utc(dt.date(2026, 3, 5), dt.time(17, 30), "Europe/London", SERVICE_ZONE)
check("the due moment survived unchanged", instant == expected,
      f"{instant} != {expected}")

with session_scope() as s:
    from app.db.models import Item

    item = s.query(Item).filter(Item.uid == "uid-1").one()
    canonical_instant = to_utc(item.due_date, item.due_time, item.due_tz, SERVICE_ZONE)
    check("the canonical due moment is unchanged too", canonical_instant == expected,
          f"{canonical_instant} != {expected}")

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All timezone-rewrite checks passed.")
