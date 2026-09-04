"""Saving one service's page must not damage anything it does not own.

Driving ``save_account_mapping`` with the payload the browser form actually
posts, because the interesting failures are not in the sync engine but in what
one page's save does to configuration belonging elsewhere -- above all to the
Radicale anchor, which every other service writes into and which no service
page has any business switching off.

Run directly:  PYTHONPATH=. python3 tests/test_mapping_save.py
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from app.db.models import (
    Account, AccountStatus, CollectionKind, ListMapping, RadicaleCollection,
    RemoteList, ServiceKind, SyncGroup,
)
from app.db.session import get_session_factory, init_db
from app.web import sync_view

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def _ensure_radicale_account(db):
    account = db.execute(
        select(Account).where(Account.service == ServiceKind.RADICALE)
    ).scalar_one_or_none()
    if account is None:
        account = Account(
            service=ServiceKind.RADICALE, slot=1, label="Built-in CalDAV server",
            status=AccountStatus.CONNECTED, enabled=True,
        )
        db.add(account)
        db.commit()
    return account


sync_view.ensure_radicale_account = _ensure_radicale_account


class Request:
    """Only ``session`` is touched, by the flash helper."""

    session: dict = {}


def flashes() -> list[str]:
    return [entry["message"] for entry in Request.session.get("_flash", [])]


def setup_world():
    init_db()
    with get_session_factory()() as db:
        google = Account(service=ServiceKind.GOOGLE, slot=1, label="Paul Personal",
                         status=AccountStatus.CONNECTED, enabled=True)
        db.add(google)
        db.commit()

        collection = RadicaleCollection(
            radicale_user="paul", collection_id="anchor-task",
            display_name="Anchor Task", kind=CollectionKind.TASKS,
        )
        second = RadicaleCollection(
            radicale_user="paul", collection_id="anchor-task-2",
            display_name="Anchor Task 2", kind=CollectionKind.TASKS,
        )
        db.add_all([collection, second])
        db.commit()

        # A second service, to stand for "picking a Todoist list while you are
        # standing on the Google page".
        todoist = Account(service=ServiceKind.TODOIST, slot=1, label="Personal",
                          status=AccountStatus.CONNECTED, enabled=True)
        db.add(todoist)
        db.commit()

        source = RemoteList(account_id=google.id, remote_id="l-src",
                            name="Test sync list", kind=CollectionKind.TASKS)
        target = RemoteList(account_id=google.id, remote_id="l-tgt",
                            name="Test sync list 2", kind=CollectionKind.TASKS)
        spare = RemoteList(account_id=google.id, remote_id="l-oth",
                           name="Grocery List", kind=CollectionKind.TASKS)
        far = RemoteList(account_id=todoist.id, remote_id="t-far",
                         name="Todoist Groceries", kind=CollectionKind.TASKS)
        farther = RemoteList(account_id=todoist.id, remote_id="t-far2",
                             name="Todoist Shared", kind=CollectionKind.TASKS)
        db.add_all([source, target, spare, far, farther])
        db.commit()
        return (google.id, collection.id, second.id,
                source.id, target.id, spare.id, far.id, farther.id)


def save(account_id, rows, read, writeout, db, updatesonly=None):
    """Call the endpoint directly.

    Every Form-declared argument has to be passed explicitly: outside a request
    FastAPI never resolves the defaults, so an omitted one arrives as the Form
    marker object rather than a list.
    """
    Request.session = {}
    sync_view.save_account_mapping(
        account_id=account_id, request=Request(), row=rows, read=read,
        writeout=writeout, updatesonly=updatesonly or [],
        kind_enabled=["tasks"], db=db,
    )


def mappings(db) -> dict[str, tuple[bool, bool]]:
    out = {}
    for mapping in db.execute(select(ListMapping)).scalars():
        remote_list = db.get(RemoteList, mapping.remote_list_id)
        out[remote_list.name] = (bool(mapping.read_enabled),
                                 bool(mapping.write_enabled))
    return out


def full_state(db) -> dict[str, tuple[bool, bool, bool]]:
    """name -> (read, write, creates_new)."""
    out = {}
    for mapping in db.execute(select(ListMapping)).scalars():
        remote_list = db.get(RemoteList, mapping.remote_list_id)
        out[remote_list.name] = (
            bool(mapping.read_enabled),
            bool(mapping.write_enabled),
            mapping.create_from_remote is not False,
        )
    return out

(account_id, collection_id, second_id, src, tgt, spare,
 far, farther) = setup_world()
rows = [src, tgt, spare]

print("Ticking a collection alone makes that list two-way")
# The behaviour Paul asked for: one tick, and the list and the collection keep
# each other up to date. Nothing else needs finding or understanding.
with get_session_factory()() as db:
    save(account_id, rows, [f"{src}:{collection_id}"], [], db)
with get_session_factory()() as db:
    state = mappings(db)
check("TICKING A COLLECTION READS *AND* WRITES",
      state.get("Test sync list") == (True, True), str(state.get("Test sync list")))

print("\nWriting out elsewhere is additive, not a replacement")
with get_session_factory()() as db:
    save(account_id, rows, [f"{src}:{collection_id}"],
         [f"{src}:{src}", f"{src}:{tgt}"], db)
with get_session_factory()() as db:
    state = mappings(db)
check("the source is still two-way", state.get("Test sync list") == (True, True),
      str(state.get("Test sync list")))
# Its own row is on this page and its collection box was left clear, so that
# choice is respected rather than overridden -- but a write-only destination is
# a one-way street, so the save says so.
check("the extra destination is written to",
      state.get("Test sync list 2") == (False, True),
      str(state.get("Test sync list 2")))
check("and being one-way is reported",
      any("will not come back" in m for m in flashes()), str(flashes()))
check("an untouched list has no mapping", "Grocery List" not in state, str(state))

print("\nThe Radicale anchor keeps reading AND writing")
# It is the canonical store: everything pulled from a service is written into
# it. A save on a service page that cleared its write flag would silently stop
# every sync, which is exactly the regression this check exists to catch.
check("the anchor still writes", state.get("Anchor Task") == (True, True),
      str(state.get("Anchor Task")))

print("\nTwo collections may not write to the same list")
with get_session_factory()() as db:
    save(account_id, rows,
         [f"{src}:{collection_id}", f"{spare}:{second_id}"],
         [f"{src}:{tgt}", f"{spare}:{tgt}"], db)
    warned = [m for m in flashes() if "already takes write-back from" in m]
with get_session_factory()() as db:
    state = mappings(db)
check("the clash was reported", bool(warned), str(flashes()))
writers = [name for name, (_, w) in state.items()
           if w and name == "Test sync list 2"]
check("the target still has at most one writer", len(writers) <= 1, str(state))
check("both anchors still write",
      state.get("Anchor Task") == (True, True)
      and state.get("Anchor Task 2", (True, True))[1] is True,
      str(state))

print("\nUnticking everything clears that account's mappings")
with get_session_factory()() as db:
    save(account_id, rows, [], [], db)
with get_session_factory()() as db:
    state = mappings(db)
check("no service list is mapped any more",
      all(name.startswith("Anchor Task") for name in state), str(state))
check("the anchor survives", state.get("Anchor Task") == (True, True),
      str(state.get("Anchor Task")))

print("\nA row marked \"changes only\" stops introducing new tasks")
with get_session_factory()() as db:
    save(account_id, rows, [f"{src}:{collection_id}", f"{tgt}:{collection_id}"],
         [f"{src}:{tgt}"], db, updatesonly=[tgt])
with get_session_factory()() as db:
    flags = {}
    for mapping in db.execute(select(ListMapping)).scalars():
        remote_list = db.get(RemoteList, mapping.remote_list_id)
        flags[remote_list.name] = mapping.create_from_remote
check("the target no longer creates from remote", flags.get("Test sync list 2") is False,
      str(flags))
check("the source still does", flags.get("Test sync list") is True, str(flags))
check("the anchor still does", flags.get("Anchor Task") is True, str(flags))

print("\nA write target in ANOTHER service is set up completely from here")
# Nothing on the Google page can tick a Todoist list's boxes -- it has no row
# here. Choosing it as a target has to configure it anyway, or saving from this
# page would leave a one-way street that only the Todoist page could fix.
with get_session_factory()() as db:
    save(account_id, rows, [f"{src}:{collection_id}"],
         [f"{src}:{src}", f"{src}:{far}"], db)
with get_session_factory()() as db:
    state = full_state(db)
check("the far list writes", state.get("Todoist Groceries", (0, 0, 0))[1] is True,
      str(state.get("Todoist Groceries")))
check("the far list also reads", state.get("Todoist Groceries", (0, 0, 0))[0] is True,
      str(state.get("Todoist Groceries")))
check("...but only for changes, not as a source of new tasks",
      state.get("Todoist Groceries", (0, 0, 1))[2] is False,
      str(state.get("Todoist Groceries")))

print("\nA target already set up two-way is left exactly as it was")
with get_session_factory()() as db:
    mapping = db.execute(
        select(ListMapping).where(ListMapping.remote_list_id == farther)
    ).scalar_one_or_none()
    if mapping is None:
        mapping = ListMapping(remote_list_id=farther, sync_group_id=None)
    # Configure it deliberately, the way its own page would.
    group = db.execute(select(SyncGroup)).scalars().first()
    mapping.sync_group_id = group.id
    mapping.read_enabled = True
    mapping.create_from_remote = True
    db.add(mapping)
    db.commit()
with get_session_factory()() as db:
    save(account_id, rows, [f"{src}:{collection_id}"],
         [f"{src}:{src}", f"{src}:{farther}"], db)
with get_session_factory()() as db:
    state = full_state(db)
check("the deliberate two-way setting survived",
      state.get("Todoist Shared") == (True, True, True),
      str(state.get("Todoist Shared")))

print("\nDropping a far target takes its auto-setup away with it")
with get_session_factory()() as db:
    save(account_id, rows, [f"{src}:{collection_id}"], [f"{src}:{src}"], db)
with get_session_factory()() as db:
    state = full_state(db)
check("the changes-only reader was removed too",
      "Todoist Groceries" not in state, str(state))

print("\nSaving one service's page never switches off another service's list")
# The bug this exists to prevent: saving the Todoist page cleared write-back on
# a Google list, because from Todoist's side an unticked box and a box nobody
# ever ticked look identical. Ownership belongs to the row itself.
with get_session_factory()() as db:
    other = Account(service=ServiceKind.TICKTICK, slot=1, label="TickTick",
                    status=AccountStatus.CONNECTED, enabled=True)
    db.add(other); db.commit()
    theirs = RemoteList(account_id=other.id, remote_id="tt-1",
                        name="TickTick Work", kind=CollectionKind.TASKS)
    db.add(theirs); db.commit()
    theirs_id = theirs.id
    # They set their own list up, two-way, on their own page.
    save(other.id, [theirs_id], [f"{theirs_id}:{collection_id}"], [], db)
with get_session_factory()() as db:
    state = mappings(db)
check("their list starts out two-way", state.get("TickTick Work") == (True, True),
      str(state.get("TickTick Work")))

# Now this account saves its own page, mentioning nothing of theirs.
with get_session_factory()() as db:
    save(account_id, rows, [f"{src}:{collection_id}"], [f"{src}:{src}"], db)
with get_session_factory()() as db:
    state = mappings(db)
check("THEIR LIST IS STILL TWO-WAY AFTER OUR SAVE",
      state.get("TickTick Work") == (True, True), str(state.get("TickTick Work")))
check("and ours is unaffected", state.get("Test sync list") == (True, True),
      str(state.get("Test sync list")))
check("the anchor survives", state.get("Anchor Task") == (True, True),
      str(state.get("Anchor Task")))

print("\nA list synced with two collections gets exactly one writer")
with get_session_factory()() as db:
    save(account_id, rows, [f"{src}:{collection_id}", f"{src}:{second_id}"], [], db)
    warned = [m for m in flashes() if "write-back" in m.lower()]
with get_session_factory()() as db:
    writers = [
        m for m in db.execute(select(ListMapping)).scalars()
        if m.remote_list_id == src and m.write_enabled
    ]
    readers = [
        m for m in db.execute(select(ListMapping)).scalars()
        if m.remote_list_id == src and m.read_enabled
    ]
check("it reads into both collections", len(readers) == 2, str(len(readers)))
check("BUT ONLY ONE OF THEM WRITES BACK TO IT", len(writers) == 1, str(len(writers)))
check("and the user is told", bool(warned), str(flashes()))

print("\nSaving records which list an aggregate gathers from")
with get_session_factory()() as db:
    save(account_id, rows, [f"{src}:{collection_id}"], [f"{src}:{tgt}"], db)
with get_session_factory()() as db:
    filters = {}
    for mapping in db.execute(select(ListMapping)).scalars():
        remote_list = db.get(RemoteList, mapping.remote_list_id)
        filters[remote_list.name] = mapping.write_from_list_ids
check("a full member carries no filter and holds everything",
      filters.get("Test sync list") is None, str(filters.get("Test sync list")))
check("THE AGGREGATE IS LIMITED TO THE LIST THAT NAMED IT",
      filters.get("Test sync list 2") == [src], str(filters.get("Test sync list 2")))
check("the anchor carries no filter", filters.get("Anchor Task") is None,
      str(filters.get("Anchor Task")))

print("\nA list that is both a member and a target keeps the whole collection")
# Being explicitly synced with the collection is the stronger statement: it says
# "hold everything here". A filter left over from also being someone's write-out
# target would quietly withhold most of it.
with get_session_factory()() as db:
    save(account_id, rows, [f"{src}:{collection_id}", f"{tgt}:{collection_id}"],
         [f"{src}:{tgt}"], db)
with get_session_factory()() as db:
    filters = {}
    for mapping in db.execute(select(ListMapping)).scalars():
        remote_list = db.get(RemoteList, mapping.remote_list_id)
        filters[remote_list.name] = mapping.write_from_list_ids
check("membership wins over the filter",
      filters.get("Test sync list 2") is None, str(filters.get("Test sync list 2")))

print("\nSimple mode must not demolish what advanced mode set up")
# The "Also write out to" column is absent from the page when advanced mode is
# off, so the form says nothing about it. Absence must not read as "cleared":
# saving any row on the page would otherwise wipe an aggregate the user built
# while advanced mode was on, without ever showing them the control they lost.
from app.db import settings_store

with get_session_factory()() as db:
    settings_store.set_bool(db, settings_store.ADVANCED_MODE, True)
    db.commit()
    # Advanced on: src is a member of the collection and also writes out to far.
    save(account_id, rows, [f"{src}:{collection_id}"], [f"{src}:{far}"], db)

with get_session_factory()() as db:
    aggregates = {
        db.get(RemoteList, m.remote_list_id).name: m.write_from_list_ids
        for m in db.execute(select(ListMapping)).scalars()
        if m.write_from_list_ids
    }
check("the aggregate was created while advanced was on",
      aggregates.get("Todoist Groceries") == [src], str(aggregates))

with get_session_factory()() as db:
    settings_store.set_bool(db, settings_store.ADVANCED_MODE, False)
    db.commit()
    # Simple mode posts the same page with no writeout field at all.
    save(account_id, rows, [f"{src}:{collection_id}"], [], db)

with get_session_factory()() as db:
    after = {
        db.get(RemoteList, m.remote_list_id).name: m.write_from_list_ids
        for m in db.execute(select(ListMapping)).scalars()
        if m.write_from_list_ids
    }
    still_writes = {
        db.get(RemoteList, m.remote_list_id).name
        for m in db.execute(select(ListMapping)).scalars()
        if m.write_enabled
    }
check("THE AGGREGATE SURVIVES A SIMPLE-MODE SAVE",
      after.get("Todoist Groceries") == [src], str(after))
check("its destination still receives writes", "Todoist Groceries" in still_writes,
      str(sorted(still_writes)))

with get_session_factory()() as db:
    settings_store.set_bool(db, settings_store.ADVANCED_MODE, True)
    db.commit()
    # Advanced on again, and now genuinely cleared: that must take effect.
    save(account_id, rows, [f"{src}:{collection_id}"], [], db)

with get_session_factory()() as db:
    cleared = {
        db.get(RemoteList, m.remote_list_id).name: m.write_from_list_ids
        for m in db.execute(select(ListMapping)).scalars()
        if m.write_from_list_ids
    }
check("clearing it in advanced mode still works", "Todoist Groceries" not in cleared,
      str(cleared))

print("\n'Changes only' survives a save made in simple mode")
# Simple mode disables the control rather than hiding it, and a disabled input
# is never submitted. The form carries the current value in a hidden field, so
# the endpoint sees it exactly as it would in advanced mode -- this checks the
# round trip that hidden field is responsible for.
with get_session_factory()() as db:
    settings_store.set_bool(db, settings_store.ADVANCED_MODE, True)
    db.commit()
    save(account_id, rows, [f"{src}:{collection_id}"], [], db, updatesonly=[src])

with get_session_factory()() as db:
    flags = {db.get(RemoteList, m.remote_list_id).name: m.create_from_remote
             for m in db.execute(select(ListMapping)).scalars()}
check("'changes only' was set in advanced mode",
      flags.get("Test sync list") is False, str(flags.get("Test sync list")))

with get_session_factory()() as db:
    settings_store.set_bool(db, settings_store.ADVANCED_MODE, False)
    db.commit()
    # What the browser posts in simple mode: the value carried by the hidden
    # field, because the visible checkbox is disabled.
    save(account_id, rows, [f"{src}:{collection_id}"], [], db, updatesonly=[src])

with get_session_factory()() as db:
    flags = {db.get(RemoteList, m.remote_list_id).name: m.create_from_remote
             for m in db.execute(select(ListMapping)).scalars()}
check("IT SURVIVES A SIMPLE-MODE SAVE",
      flags.get("Test sync list") is False, str(flags.get("Test sync list")))

with get_session_factory()() as db:
    settings_store.set_bool(db, settings_store.ADVANCED_MODE, True)
    db.commit()
    save(account_id, rows, [f"{src}:{collection_id}"], [], db, updatesonly=[])

with get_session_factory()() as db:
    flags = {db.get(RemoteList, m.remote_list_id).name: m.create_from_remote
             for m in db.execute(select(ListMapping)).scalars()}
check("turning it off in advanced mode still works",
      flags.get("Test sync list") is not False, str(flags.get("Test sync list")))

if _failures:
    print(f"\n{len(_failures)} check(s) failed: {', '.join(_failures)}")
    sys.exit(1)
print("\nAll mapping-save tests passed.")
