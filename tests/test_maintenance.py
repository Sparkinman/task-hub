"""Retention and repairs.

Orphans are manufactured on purpose here, because the live database has none
and a repair tool that has never been shown a real problem is not a tool. The
last section is the important one: it proves the repairs leave alone the rows
whose loss would cause duplicate tasks.

Run directly:  PYTHONPATH=. python3 tests/test_maintenance.py
"""

from __future__ import annotations

import datetime as dt
import sys

from sqlalchemy import select

from app.db.models import (
    Account, AccountStatus, CollectionKind, Item, ItemLink, ListMapping,
    RadicaleCollection, RemoteList, ServiceKind, SyncGroup, SyncLogEntry,
    SyncOutcome, SyncRun, Tombstone,
)
from app.db.session import get_session_factory, init_db
from app.sync import engine as engine_module
from app.web import maintenance

UTC = dt.timezone.utc
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


init_db()


def build_world():
    """A healthy setup, then orphans made by deleting parents behind its back."""
    with get_session_factory()() as db:
        acct = Account(service=ServiceKind.GOOGLE, slot=1, label="G",
                       status=AccountStatus.CONNECTED, enabled=True)
        gone = Account(service=ServiceKind.TODOIST, slot=1, label="Gone",
                       status=AccountStatus.CONNECTED, enabled=True)
        coll = RadicaleCollection(radicale_user="p", collection_id="tasks",
                                  display_name="Tasks", kind=CollectionKind.TASKS)
        db.add_all([acct, gone, coll]); db.commit()

        group = SyncGroup(name="Tasks", kind=CollectionKind.TASKS,
                          radicale_collection_id=coll.id, enabled=True)
        db.add(group); db.commit()

        live = RemoteList(account_id=acct.id, remote_id="l1", name="Live",
                          kind=CollectionKind.TASKS)
        orphan_list = RemoteList(account_id=gone.id, remote_id="l2", name="Orphan",
                                 kind=CollectionKind.TASKS)
        db.add_all([live, orphan_list]); db.commit()

        db.add(ListMapping(remote_list_id=live.id, sync_group_id=group.id,
                           read_enabled=True, write_enabled=True))
        item = Item(uid="u1", sync_group_id=group.id, kind=CollectionKind.TASKS,
                    title="Real task")
        db.add(item); db.commit()

        # A healthy link, which must survive every repair.
        db.add(ItemLink(item_id=item.id, account_id=acct.id,
                        remote_list_id=live.id, sync_group_id=group.id,
                        remote_id="r1"))
        db.commit()
        ids = dict(acct=acct.id, gone=gone.id, group=group.id, coll=coll.id,
                   live=live.id, orphan_list=orphan_list.id, item=item.id)

    with get_session_factory()() as db:
        ghost = Item(uid="u-ghost", sync_group_id=ids["group"],
                     kind=CollectionKind.TASKS, title="Ghost")
        db.add(ghost); db.commit()
        ids["ghost"] = ghost.id
        db.add_all([
            ItemLink(item_id=ghost.id, account_id=ids["acct"],
                     remote_list_id=ids["live"], sync_group_id=ids["group"],
                     remote_id="r-ghost"),
            ItemLink(item_id=ids["item"], account_id=ids["gone"],
                     remote_list_id=ids["orphan_list"], sync_group_id=ids["group"],
                     remote_id="r-gone"),
        ])
        db.add(ListMapping(remote_list_id=ids["orphan_list"],
                           sync_group_id=ids["group"], read_enabled=True))
        db.commit()

    # Manufacture the orphans through the raw driver. Deleting a parent through
    # the ORM tidies its children on the way out -- which is exactly why the
    # live database has none of these -- so the first attempt at this test
    # created nothing at all. Raw SQL with foreign keys disabled reproduces the
    # state a crash or an older, buggier version could actually leave behind.
    from app.db.session import get_engine

    raw = get_engine().raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("DELETE FROM items WHERE id = ?", (ids["ghost"],))
        cur.execute("DELETE FROM accounts WHERE id = ?", (ids["gone"],))
        raw.commit()
    finally:
        raw.close()
    return ids


ids = build_world()

print("The scan finds each manufactured orphan")
with get_session_factory()() as db:
    found = {f.key: f.count for f in maintenance.scan(db)}
check("a link whose task is gone is found", found.get("links_no_item") == 1, str(found))
check("a link whose account is gone is found", found.get("links_no_account") == 1, str(found))
check("a list whose account is gone is found", found.get("lists_no_account") == 1, str(found))
check("every finding carries a count", all(v > 0 for v in found.values()), str(found))

print("\nEach repair removes only its own category")
with get_session_factory()() as db:
    removed = maintenance.repair(db, "links_no_item")
check("the repair reports what it removed", removed == 1, str(removed))
with get_session_factory()() as db:
    found = {f.key: f.count for f in maintenance.scan(db)}
check("that category is now clean", "links_no_item" not in found, str(found))
check("the others are untouched", found.get("links_no_account") == 1, str(found))

print("\nThe healthy link is never touched")
# Removing a parent can orphan its children, so the whole set is applied twice.
# The order in CHECKS is meant to settle it in one pass; the second pass proves
# it converges rather than uncovering something new every time.
with get_session_factory()() as db:
    for _pass in range(2):
        for key in list(maintenance.CHECKS):
            maintenance.repair(db, key)
with get_session_factory()() as db:
    survivors = [(l.item_id, l.remote_id) for l in db.execute(select(ItemLink)).scalars()]
    findings = maintenance.scan(db)
check("running every repair leaves the good link alone",
      survivors == [(ids["item"], "r1")], str(survivors))
with get_session_factory()() as db:
    check("the task survived", db.get(Item, ids["item"]) is not None)
    check("its mapping survived",
          db.execute(select(ListMapping).where(
              ListMapping.remote_list_id == ids["live"])).scalar_one_or_none() is not None)
check("the database now reports clean", findings == [], str(findings))

print("\nRepairing an unknown key does nothing")
with get_session_factory()() as db:
    check("unknown repairs are refused", maintenance.repair(db, "nonsense") == 0)

print("\nHistory retention keeps recent runs and drops the rest")
# A run has to be BOTH beyond the newest 200 AND older than 30 days before it
# goes, so a handful of ancient runs are kept and prove nothing. Enough are
# created here to push the oldest past both tests.
now = dt.datetime.now(UTC)
ANCIENT = engine_module.KEEP_RUNS + 6
with get_session_factory()() as db:
    for i in range(ANCIENT):
        db.add(SyncRun(started_at=now - dt.timedelta(days=200, minutes=i),
                       finished_at=now - dt.timedelta(days=200, minutes=i),
                       outcome=SyncOutcome.SUCCESS, trigger="scheduled"))
    recent = SyncRun(started_at=now - dt.timedelta(days=1), finished_at=now,
                     outcome=SyncOutcome.SUCCESS, trigger="scheduled")
    db.add(recent); db.commit()
    db.add(SyncLogEntry(run_id=recent.id, message="kept"))
    db.add(Tombstone(uid="old", sync_group_id=ids["group"], propagated=True,
                     deleted_at=now - dt.timedelta(days=200)))
    db.add(Tombstone(uid="fresh", sync_group_id=ids["group"], propagated=True,
                     deleted_at=now - dt.timedelta(days=2)))
    db.commit()
    before = db.execute(select(SyncRun)).scalars().all()

with get_session_factory()() as db:
    eng = engine_module.SyncEngine(db)
    eng.prune_history()

with get_session_factory()() as db:
    runs = db.execute(select(SyncRun)).scalars().all()
    tombs = {t.uid for t in db.execute(select(Tombstone)).scalars()}
    logs = db.execute(select(SyncLogEntry)).scalars().all()
check("ancient runs beyond the cap were removed",
      len(runs) < len(before), f"{len(before)} -> {len(runs)}")
check("the newest 200 are still kept whatever their age",
      len(runs) >= engine_module.KEEP_RUNS, str(len(runs)))
check("the recent run was kept", any(r.trigger == "scheduled" and
      (now - r.started_at).days < 2 for r in runs), str(len(runs)))
check("its log line was kept", len(logs) == 1, str(len(logs)))
check("an expired tombstone went", "old" not in tombs, str(tombs))
check("A RECENT TOMBSTONE STAYED", "fresh" in tombs, str(tombs))

print("\nPruning a small history changes nothing")
with get_session_factory()() as db:
    count_before = len(db.execute(select(SyncRun)).scalars().all())
    engine_module.SyncEngine(db).prune_history()
with get_session_factory()() as db:
    check("nothing was removed on a second pass",
          len(db.execute(select(SyncRun)).scalars().all()) == count_before)

if _failures:
    print(f"\n{len(_failures)} check(s) failed: {', '.join(_failures)}")
    sys.exit(1)
print("\nAll maintenance tests passed.")
