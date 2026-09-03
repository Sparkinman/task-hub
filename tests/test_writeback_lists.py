"""Write-back into a different list from the one that was read.

The arrangement this proves is the one a shared list needs: a Google list is
read into a collection, other people change things there, and the merged result
is pushed into a *second* Google list rather than back into the original. Both
lists live in the same account, which is what makes it delicate -- an item link
found by account alone would belong to the list the item came from, and the push
would then update the wrong task.

Run directly:  PYTHONPATH=. python3 tests/test_writeback_lists.py
"""

from __future__ import annotations

import datetime as dt
import sys

from sqlalchemy import select

from app.connectors.base import (
    F_DUE_DATE, F_NOTES, F_STATUS, F_TITLE,
    Capabilities, Connector, PullResult, PushOutcome, RemoteItem, RemoteList,
)
from app.db.models import (
    Account, AccountStatus, CollectionKind, ItemLink, ItemStatus, ListMapping,
    RemoteList as RemoteListRow, ServiceKind, SyncGroup,
)
from app.db.session import init_db, session_scope
from app.services.ical_model import CanonicalRecord
from app.sync import engine as engine_module
from app.sync.engine import SyncEngine

UTC = dt.timezone.utc
_failures: list[str] = []

SOURCE_LIST = "grocery-shopping"
TARGET_LIST = "shared-grocery-list"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


class ListAwareStub(Connector):
    """A fake service that keeps each of its lists separate.

    The stub in test_engine keeps one flat store, which is fine when an account
    has a single list. Here the whole point is that two lists in one account
    must not be confused, so the store is keyed by list as well as by item.
    """

    def __init__(self, key: str, caps: Capabilities, service: ServiceKind,
                 lists: list[tuple[str, str]]):
        super().__init__(account_id=0, credentials={}, sync_state={})
        self.key = key
        self._caps = caps
        self.service = service
        self.name = key
        self.writes = 0
        self.lists = lists
        self.store: dict[str, dict[str, CanonicalRecord]] = {
            list_id: {} for list_id, _ in lists
        }
        self.stamps: dict[str, dict[str, dt.datetime]] = {
            list_id: {} for list_id, _ in lists
        }

    def capabilities(self, kind):
        return self._caps

    def verify(self):
        return self.key

    def list_remote_lists(self):
        return [
            RemoteList(remote_id=list_id, name=name, kind=CollectionKind.TASKS)
            for list_id, name in self.lists
        ]

    def pull(self, remote_list_id, kind, since=None, state=None):
        from app.sync.merge import project

        items = [
            RemoteItem(
                remote_id=remote_id,
                record=project(record, self._caps),
                fields_present=self._caps.present_fields(),
                remote_updated_at=self.stamps[remote_list_id].get(remote_id),
            )
            for remote_id, record in self.store[remote_list_id].items()
        ]
        return PullResult(items=items, incremental=False)

    def create(self, remote_list_id, record, kind):
        self.writes += 1
        remote_id = f"{self.key}-{remote_list_id}-{len(self.store[remote_list_id]) + 1}"
        self.store[remote_list_id][remote_id] = record
        self.stamps[remote_list_id][remote_id] = dt.datetime.now(UTC)
        return PushOutcome(
            remote_id=remote_id,
            remote_updated_at=self.stamps[remote_list_id][remote_id],
        )

    def update(self, remote_list_id, remote_id, record, kind):
        self.writes += 1
        if remote_id not in self.store[remote_list_id]:
            # The failure this whole file exists to catch: an update aimed at a
            # list that has never held that item. A real service answers 404;
            # recording it is what turns a silent misroute into a failed check.
            raise AssertionError(
                f"update() aimed {remote_id!r} at {remote_list_id!r}, "
                f"which holds {sorted(self.store[remote_list_id])}"
            )
        self.store[remote_list_id][remote_id] = record
        self.stamps[remote_list_id][remote_id] = dt.datetime.now(UTC)
        return PushOutcome(
            remote_id=remote_id,
            remote_updated_at=self.stamps[remote_list_id][remote_id],
        )

    def delete(self, remote_list_id, remote_id, kind):
        self.store[remote_list_id].pop(remote_id, None)
        return PushOutcome(remote_id=remote_id)


LOSSLESS = Capabilities(
    fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE}), stores_uid=True
)
DATE_ONLY = Capabilities(fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE}))

CONNECTORS: dict[int, Connector] = {}


def fake_build_connector(session, account):
    return CONNECTORS[account.id]


def setup_world():
    """One collection, one Google account, two lists pointing opposite ways."""
    with session_scope() as s:
        local = Account(service=ServiceKind.RADICALE, slot=1, label="Local",
                        status=AccountStatus.CONNECTED)
        google = Account(service=ServiceKind.GOOGLE, slot=1, label="Google",
                         status=AccountStatus.CONNECTED)
        s.add_all([local, google])
        s.commit()

        group = SyncGroup(name="Shared", kind=CollectionKind.TASKS, enabled=True)
        s.add(group)
        s.commit()

        local_row = RemoteListRow(account_id=local.id, remote_id="mom",
                                  name="Mom", kind=CollectionKind.TASKS)
        s.add(local_row)
        s.flush()
        s.add(ListMapping(remote_list_id=local_row.id, sync_group_id=group.id,
                          read_enabled=True, write_enabled=True))

        # Read from here, never write back to it.
        source = RemoteListRow(account_id=google.id, remote_id=SOURCE_LIST,
                               name="Grocery Shopping", kind=CollectionKind.TASKS)
        s.add(source)
        s.flush()
        s.add(ListMapping(remote_list_id=source.id, sync_group_id=group.id,
                          read_enabled=True, write_enabled=False))

        # Write here, never read from it.
        target = RemoteListRow(account_id=google.id, remote_id=TARGET_LIST,
                               name="Shared Grocery List", kind=CollectionKind.TASKS)
        s.add(target)
        s.flush()
        s.add(ListMapping(remote_list_id=target.id, sync_group_id=group.id,
                          read_enabled=False, write_enabled=True))
        s.commit()

        CONNECTORS[local.id] = ListAwareStub(
            "local", LOSSLESS, ServiceKind.RADICALE, [("mom", "Mom")]
        )
        CONNECTORS[google.id] = ListAwareStub(
            "google", DATE_ONLY, ServiceKind.GOOGLE,
            [(SOURCE_LIST, "Grocery Shopping"), (TARGET_LIST, "Shared Grocery List")],
        )
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

# A task added on the phone, in the list that is only ever read.
google.store[SOURCE_LIST]["g-milk"] = CanonicalRecord(
    uid="milk", title="Milk", kind=CollectionKind.TASKS
)
google.stamps[SOURCE_LIST]["g-milk"] = dt.datetime.now(UTC)

print("A task read from one list is written into a different one")
run = run_sync()
check("the sync completed without errors", run.errors == 0, f"{run.errors} error(s)")
check("it reached the collection", len(local.store["mom"]) == 1,
      str(sorted(local.store["mom"])))
check("it was created in the write-back list",
      len(google.store[TARGET_LIST]) == 1, str(sorted(google.store[TARGET_LIST])))
check("the read-only list gained no duplicate",
      sorted(google.store[SOURCE_LIST]) == ["g-milk"],
      str(sorted(google.store[SOURCE_LIST])))

print("\nThe two lists keep separate links, so neither overwrites the other")
with session_scope() as s:
    links = s.execute(select(ItemLink)).scalars().all()
    google_links = [
        link for link in links
        if s.get(RemoteListRow, link.remote_list_id) is not None
        and s.get(RemoteListRow, link.remote_list_id).account_id == google_id
    ]
    lists_seen = {
        s.get(RemoteListRow, link.remote_list_id).remote_id for link in google_links
    }
check("one link per Google list", len(google_links) == 2, f"{len(google_links)} link(s)")
check("both lists are represented", lists_seen == {SOURCE_LIST, TARGET_LIST},
      str(lists_seen))

print("\nA change made in the collection updates the write-back list, not the source")
record = next(iter(local.store["mom"].values()))
record.title = "Milk, semi-skimmed"
local.stamps["mom"][next(iter(local.store["mom"]))] = dt.datetime.now(UTC)
google.writes = 0
run = run_sync()
check("the sync completed without errors", run.errors == 0, f"{run.errors} error(s)")
target_titles = [r.title for r in google.store[TARGET_LIST].values()]
check("the write-back list has the new title",
      target_titles == ["Milk, semi-skimmed"], str(target_titles))
check("the source list was left alone",
      google.store[SOURCE_LIST]["g-milk"].title == "Milk",
      google.store[SOURCE_LIST]["g-milk"].title)
check("no duplicate appeared in the write-back list",
      len(google.store[TARGET_LIST]) == 1, str(sorted(google.store[TARGET_LIST])))

print("\nA second pass with nothing changed writes nothing at all")
google.writes = 0
run_sync()
check("no redundant writes", google.writes == 0, f"{google.writes} write(s)")

print("\nWriting out to the source list AND a second list never duplicates")
# The arrangement Paul described: a task added to "Test sync list" lands in the
# collection and is written back out to both that list and a second one. The
# source already has a link from the pull, so it must be updated in place while
# the second gets one creation and no more, however many passes run.
with session_scope() as s:
    source_row = s.execute(
        select(RemoteListRow).where(RemoteListRow.remote_id == SOURCE_LIST)
    ).scalar_one()
    mapping = s.execute(
        select(ListMapping).where(ListMapping.remote_list_id == source_row.id)
    ).scalar_one()
    mapping.write_enabled = True   # now writes back to itself as well
    s.commit()

before_source = dict(google.store[SOURCE_LIST])
before_target = dict(google.store[TARGET_LIST])
for _ in range(3):
    run = run_sync()
    check("repeated syncs stay clean", run.errors == 0, f"{run.errors} error(s)")
check("the source list gained nothing",
      sorted(google.store[SOURCE_LIST]) == sorted(before_source),
      str(sorted(google.store[SOURCE_LIST])))
check("the second list gained nothing",
      sorted(google.store[TARGET_LIST]) == sorted(before_target),
      str(sorted(google.store[TARGET_LIST])))
check("still exactly one item in each",
      len(google.store[SOURCE_LIST]) == 1 and len(google.store[TARGET_LIST]) == 1,
      f"{len(google.store[SOURCE_LIST])} / {len(google.store[TARGET_LIST])}")

with session_scope() as s:
    from app.db.models import Item
    items = s.execute(select(Item)).scalars().all()
check("the collection still holds one canonical item", len(items) == 1,
      f"{len(items)} items")

print("\n\"Changes only\": completions come back, new tasks do not")
# The distinction Paul asked for. The write-back list is read, so a task
# completed there completes everywhere -- but a task *created* there is a local
# addition to a copy and must not be pushed into the original, or the two lists
# become mirrors of each other.
with session_scope() as s:
    target_row = s.execute(
        select(RemoteListRow).where(RemoteListRow.remote_id == TARGET_LIST)
    ).scalar_one()
    mapping = s.execute(
        select(ListMapping).where(ListMapping.remote_list_id == target_row.id)
    ).scalar_one()
    mapping.read_enabled = True
    mapping.create_from_remote = False      # changes only
    s.commit()

# Somebody adds a task directly to the write-back list.
google.store[TARGET_LIST]["g-intruder"] = CanonicalRecord(
    uid="intruder", title="Added straight to the copy", kind=CollectionKind.TASKS
)
google.stamps[TARGET_LIST]["g-intruder"] = dt.datetime.now(UTC)

run = run_sync()
check("the sync completed without errors", run.errors == 0, f"{run.errors} error(s)")
check("the new task did NOT reach the source list",
      "Added straight to the copy" not in
      [r.title for r in google.store[SOURCE_LIST].values()],
      str([r.title for r in google.store[SOURCE_LIST].values()]))
check("the new task did NOT reach the collection",
      "Added straight to the copy" not in
      [r.title for r in local.store["mom"].values()],
      str([r.title for r in local.store["mom"].values()]))

# ...but completing a task Task Hub put there must propagate.
known_id = next(
    rid for rid, rec in google.store[TARGET_LIST].items() if rid != "g-intruder"
)
google.store[TARGET_LIST][known_id].status = ItemStatus.COMPLETED
google.store[TARGET_LIST][known_id].completed_at = dt.datetime.now(UTC)
google.stamps[TARGET_LIST][known_id] = dt.datetime.now(UTC)

run = run_sync()
check("the sync completed without errors", run.errors == 0, f"{run.errors} error(s)")
source_statuses = [r.status for r in google.store[SOURCE_LIST].values()]
check("COMPLETING IN THE COPY COMPLETED THE ORIGINAL",
      ItemStatus.COMPLETED in source_statuses, str(source_statuses))
check("and the collection agrees",
      ItemStatus.COMPLETED in [r.status for r in local.store["mom"].values()],
      str([r.status for r in local.store["mom"].values()]))

print("\nTwo readable lists in ONE account are both merged, not just the last")
# Both lists belong to the same Google account. Keying the pull results by
# account meant the second pull overwrote the first, so everything added to one
# of them was fetched and then discarded before the merge ever saw it.
with session_scope() as s:
    source_row = s.execute(
        select(RemoteListRow).where(RemoteListRow.remote_id == SOURCE_LIST)
    ).scalar_one()
    target_row = s.execute(
        select(RemoteListRow).where(RemoteListRow.remote_id == TARGET_LIST)
    ).scalar_one()
    for row in (source_row, target_row):
        mapping = s.execute(
            select(ListMapping).where(ListMapping.remote_list_id == row.id)
        ).scalar_one()
        mapping.read_enabled = True
        mapping.create_from_remote = True      # both are full sources here
    s.commit()

google.store[SOURCE_LIST]["g-fromone"] = CanonicalRecord(
    uid="fromone", title="Added in list one", kind=CollectionKind.TASKS
)
google.stamps[SOURCE_LIST]["g-fromone"] = dt.datetime.now(UTC)
google.store[TARGET_LIST]["g-fromtwo"] = CanonicalRecord(
    uid="fromtwo", title="Added in list two", kind=CollectionKind.TASKS
)
google.stamps[TARGET_LIST]["g-fromtwo"] = dt.datetime.now(UTC)

run = run_sync()
check("the sync completed without errors", run.errors == 0, f"{run.errors} error(s)")
collection_titles = [r.title for r in local.store["mom"].values()]
check("THE ITEM ADDED IN LIST ONE WAS INGESTED",
      "Added in list one" in collection_titles, str(collection_titles))
check("the item added in list two was ingested too",
      "Added in list two" in collection_titles, str(collection_titles))
check("list one's item reached list two",
      "Added in list one" in [r.title for r in google.store[TARGET_LIST].values()],
      str([r.title for r in google.store[TARGET_LIST].values()]))

print("\n\"Changes only\" limits reading, never writing")
# The distinction that matters: "changes only" says this list is not a place new
# tasks come *from*. It says nothing about what is written *to* it. A task
# completed anywhere else must still be marked complete here, or a destination
# would drift out of date the moment it stopped being the source.
with session_scope() as s:
    target_row = s.execute(
        select(RemoteListRow).where(RemoteListRow.remote_id == TARGET_LIST)
    ).scalar_one()
    mapping = s.execute(
        select(ListMapping).where(ListMapping.remote_list_id == target_row.id)
    ).scalar_one()
    mapping.read_enabled = True
    mapping.write_enabled = True
    mapping.create_from_remote = False          # changes only
    s.commit()

# Reopen everything so there is something live to complete.
for store in (google.store[SOURCE_LIST], google.store[TARGET_LIST], local.store["mom"]):
    for record in store.values():
        record.status = ItemStatus.NEEDS_ACTION
        record.completed_at = None
for stamps in (google.stamps[SOURCE_LIST], google.stamps[TARGET_LIST], local.stamps["mom"]):
    for key in stamps:
        stamps[key] = dt.datetime.now(UTC)
run_sync()

# Complete it in the source list, which is not the destination.
source_id = next(iter(google.store[SOURCE_LIST]))
google.store[SOURCE_LIST][source_id].status = ItemStatus.COMPLETED
google.store[SOURCE_LIST][source_id].completed_at = dt.datetime.now(UTC)
google.stamps[SOURCE_LIST][source_id] = dt.datetime.now(UTC)

run = run_sync()
check("the sync completed without errors", run.errors == 0, f"{run.errors} error(s)")
target_statuses = [r.status for r in google.store[TARGET_LIST].values()]
check("COMPLETING ELSEWHERE STILL COMPLETES IT IN A CHANGES-ONLY DESTINATION",
      ItemStatus.COMPLETED in target_statuses, str(target_statuses))
check("and in the collection",
      ItemStatus.COMPLETED in [r.status for r in local.store["mom"].values()],
      str([r.status for r in local.store["mom"].values()]))

print("\nAn aggregate receives only what comes from the list that named it")
# The rule Paul asked for. A destination chosen under one list's "also write out
# to" is there to gather *that* list, so it must not fill up with everything the
# collection holds. A task created in the collection itself, or in another
# member, has no business appearing there.
with session_scope() as s:
    source_row = s.execute(
        select(RemoteListRow).where(RemoteListRow.remote_id == SOURCE_LIST)
    ).scalar_one()
    target_row = s.execute(
        select(RemoteListRow).where(RemoteListRow.remote_id == TARGET_LIST)
    ).scalar_one()
    source_mapping = s.execute(
        select(ListMapping).where(ListMapping.remote_list_id == source_row.id)
    ).scalar_one()
    source_mapping.read_enabled = True
    source_mapping.write_enabled = True
    source_mapping.create_from_remote = True
    source_mapping.write_from_list_ids = None          # a full member
    target_mapping = s.execute(
        select(ListMapping).where(ListMapping.remote_list_id == target_row.id)
    ).scalar_one()
    target_mapping.read_enabled = True
    target_mapping.write_enabled = True
    target_mapping.create_from_remote = False
    target_mapping.write_from_list_ids = [source_row.id]   # gathers the source
    s.commit()

before_target = set(google.store[TARGET_LIST])

# Created in the collection itself, exactly like adding one on the web page.
local.store["mom"]["local-web"] = CanonicalRecord(
    uid="from-web", title="Added on the web page", kind=CollectionKind.TASKS
)
local.stamps["mom"]["local-web"] = dt.datetime.now(UTC)

run = run_sync()
check("the sync completed without errors", run.errors == 0, f"{run.errors} error(s)")
source_titles = [r.title for r in google.store[SOURCE_LIST].values()]
target_titles = [r.title for r in google.store[TARGET_LIST].values()]
check("a collection member still receives it",
      "Added on the web page" in source_titles, str(source_titles))
check("THE AGGREGATE DOES NOT",
      "Added on the web page" not in target_titles, str(target_titles))

# But a task that really did come from the source list still reaches it.
google.store[SOURCE_LIST]["g-fromsource"] = CanonicalRecord(
    uid="fromsource", title="Added in the source list", kind=CollectionKind.TASKS
)
google.stamps[SOURCE_LIST]["g-fromsource"] = dt.datetime.now(UTC)
run = run_sync()
target_titles = [r.title for r in google.store[TARGET_LIST].values()]
check("A TASK FROM THE NAMED LIST STILL REACHES THE AGGREGATE",
      "Added in the source list" in target_titles, str(target_titles))
check("and the aggregate gained nothing else",
      len(set(google.store[TARGET_LIST]) - before_target) == 1,
      str(sorted(set(google.store[TARGET_LIST]) - before_target)))

print("\nCompleting in the aggregate still comes back")
aggregate_id = next(
    rid for rid, rec in google.store[TARGET_LIST].items()
    if rec.title == "Added in the source list"
)
google.store[TARGET_LIST][aggregate_id].status = ItemStatus.COMPLETED
google.store[TARGET_LIST][aggregate_id].completed_at = dt.datetime.now(UTC)
google.stamps[TARGET_LIST][aggregate_id] = dt.datetime.now(UTC)
run_sync()
source_done = [
    r.status for r in google.store[SOURCE_LIST].values()
    if r.title == "Added in the source list"
]
check("ticking it off in the aggregate completes the original",
      source_done == [ItemStatus.COMPLETED], str(source_done))

if _failures:
    print(f"\n{len(_failures)} check(s) failed: {', '.join(_failures)}")
    sys.exit(1)
print("\nAll write-back list tests passed.")
