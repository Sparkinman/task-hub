"""Sync groups, list assignment, manual sync and history.

A sync group is a set of lists that are kept in step with each other, anchored
on one Radicale collection. Sync is deliberately not global: grouping is what
lets a "Work" list in Google and Todoist converge while a private list stays
entirely separate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Account,
    CollectionKind,
    ListMapping,
    RadicaleCollection,
    RemoteList,
    ServiceKind,
    SyncGroup,
    SyncLogEntry,
    SyncRun,
)
from app.db import settings_store
from app.db.session import get_db
from app.sync import scheduler
from app.sync.engine import ensure_radicale_account, set_account_kind_enabled
from app.web import deps

router = APIRouter(prefix="/sync")


def _radicale_list_for(
    db: Session, group: SyncGroup, collection: RadicaleCollection
) -> RemoteList | None:
    """Ensure the group's Radicale collection takes part as an ordinary list.

    Radicale is a peer connector, so its collection needs a RemoteList row like
    any other. Creating it here keeps the engine's participant lookup uniform
    instead of special-casing the anchor.
    """
    account = ensure_radicale_account(db)
    if account is None:
        return None

    row = db.execute(
        select(RemoteList).where(
            RemoteList.account_id == account.id,
            RemoteList.remote_id == collection.collection_id,
        )
    ).scalar_one_or_none()

    if row is None:
        row = RemoteList(
            account_id=account.id,
            remote_id=collection.collection_id,
            name=collection.display_name,
            kind=collection.kind,
            colour=collection.colour,
        )
        db.add(row)

    row.name = collection.display_name
    row.sync_group_id = group.id       # legacy column, kept in step
    row.read_enabled = True
    row.write_enabled = True
    db.flush()

    # The anchor is always read and write: it is the canonical store.
    mapping = db.execute(
        select(ListMapping).where(
            ListMapping.remote_list_id == row.id,
            ListMapping.sync_group_id == group.id,
        )
    ).scalar_one_or_none()
    if mapping is None:
        mapping = ListMapping(remote_list_id=row.id, sync_group_id=group.id)
        db.add(mapping)
    mapping.read_enabled = True
    mapping.write_enabled = True
    db.commit()
    return row


@router.get("")
@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    """Show one panel per Radicale collection.

    The Radicale collection is the thing the user actually thinks about -- "my
    Work task list" -- so it anchors the page. For each one they tick which
    service lists feed into it, and which it writes back out to. The underlying
    sync group is created and named automatically; it is an implementation
    detail, not a concept the user should have to assemble by hand.
    """
    collections = (
        db.execute(select(RadicaleCollection).order_by(RadicaleCollection.display_name))
        .scalars()
        .all()
    )

    radicale_account = db.execute(
        select(Account).where(Account.service == ServiceKind.RADICALE)
    ).scalar_one_or_none()
    radicale_account_id = radicale_account.id if radicale_account else None

    service_lists = []
    for row in db.execute(
        select(RemoteList).order_by(RemoteList.kind, RemoteList.name)
    ).scalars():
        if row.account_id == radicale_account_id:
            continue
        account = db.get(Account, row.account_id)
        if account is not None:
            service_lists.append({"list": row, "account": account})

    panels = []
    for collection in collections:
        group = db.execute(
            select(SyncGroup).where(SyncGroup.radicale_collection_id == collection.id)
        ).scalar_one_or_none()

        rows = []
        # Only lists of the same kind can join: every service stores tasks and
        # events separately, so mixing them has no meaning.
        for entry in service_lists:
            row = entry["list"]
            if row.kind != collection.kind:
                continue
            mine = group is not None and row.sync_group_id == group.id
            other = None
            if row.sync_group_id and not mine:
                claimed = db.get(SyncGroup, row.sync_group_id)
                other = claimed.name if claimed else None
            rows.append(
                {
                    "list": row,
                    "account": entry["account"],
                    "read": mine and row.read_enabled,
                    "write": mine and row.write_enabled,
                    # A list already feeding another collection is shown but
                    # locked, so the user can see why rather than ticking a box
                    # that would silently steal it from somewhere else.
                    "claimed_by": other,
                }
            )

        active_read = sum(1 for r in rows if r["read"])
        active_write = sum(1 for r in rows if r["write"])
        panels.append(
            {
                "collection": collection,
                "group": group,
                "rows": rows,
                "active_read": active_read,
                "active_write": active_write,
                "syncing": bool(group and group.enabled and (active_read or active_write)),
            }
        )

    has_accounts = any(
        a.service != ServiceKind.RADICALE
        for a in db.execute(select(Account)).scalars()
    )

    return deps.render(
        request, db, "sync_groups.html",
        panels=panels,
        has_accounts=has_accounts,
        sync_running=scheduler.is_running(),
        next_run=scheduler.next_run_time(),
    )


@router.post("/collections/{collection_id}/save")
def save_collection(
    collection_id: int,
    request: Request,
    read: list[int] = Form(default=[]),
    write: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Apply the read and write choices for one Radicale collection."""
    collection = db.get(RadicaleCollection, collection_id)
    if collection is None:
        deps.flash(request, "That collection no longer exists.", "error")
        return deps.redirect("/sync")

    group = db.execute(
        select(SyncGroup).where(SyncGroup.radicale_collection_id == collection.id)
    ).scalar_one_or_none()

    read_ids, write_ids = set(read), set(write)
    chosen = read_ids | write_ids

    if group is None:
        if not chosen:
            deps.flash(request, f"Nothing selected for {collection.display_name!r}.", "info")
            return deps.redirect("/sync")
        group = SyncGroup(
            name=collection.display_name,
            kind=collection.kind,
            radicale_collection_id=collection.id,
            enabled=True,
        )
        db.add(group)
        db.commit()
        db.refresh(group)

    if _radicale_list_for(db, group, collection) is None:
        deps.flash(
            request, "Radicale is not configured, so this collection cannot sync yet.",
            "error",
        )
        return deps.redirect("/sync")

    radicale_account = db.execute(
        select(Account).where(Account.service == ServiceKind.RADICALE)
    ).scalar_one_or_none()
    radicale_account_id = radicale_account.id if radicale_account else None

    # Detach anything the user just unticked, rather than leaving it quietly on.
    for row in db.execute(
        select(RemoteList).where(RemoteList.sync_group_id == group.id)
    ).scalars():
        if row.account_id == radicale_account_id or row.id in chosen:
            continue
        row.sync_group_id = None
        row.read_enabled = False
        row.write_enabled = False

    problems: list[str] = []
    for list_id in chosen:
        row = db.get(RemoteList, list_id)
        if row is None:
            continue
        if row.kind != collection.kind:
            problems.append(f"{row.name!r} does not hold {collection.kind.value}.")
            continue
        if row.sync_group_id and row.sync_group_id != group.id:
            other = db.get(SyncGroup, row.sync_group_id)
            problems.append(
                f"{row.name!r} is already syncing with "
                f"{other.name if other else 'another collection'}."
            )
            continue
        row.sync_group_id = group.id
        row.read_enabled = row.id in read_ids
        row.write_enabled = row.id in write_ids

    db.commit()

    for problem in problems:
        deps.flash(request, problem, "error")
    deps.flash(
        request,
        f"{collection.display_name}: reading from {len(read_ids)} list(s), "
        f"writing back to {len(write_ids)}.",
        "success",
    )
    return deps.redirect("/sync")


@router.post("/groups/{group_id}/toggle")
def toggle_group(group_id: int, request: Request, db: Session = Depends(get_db)):
    group = db.get(SyncGroup, group_id)
    if group is not None:
        group.enabled = not group.enabled
        db.commit()
        deps.flash(
            request,
            f"{group.name!r} is now {'enabled' if group.enabled else 'paused'}.",
            "success",
        )
    return deps.redirect("/sync")


@router.post("/groups/{group_id}/delete")
def delete_group(group_id: int, request: Request, db: Session = Depends(get_db)):
    group = db.get(SyncGroup, group_id)
    if group is None:
        return deps.redirect("/sync")

    name = group.name
    # Detach lists rather than deleting them, so the user's read/write choices
    # survive and nothing is removed from any service.
    for row in db.execute(
        select(RemoteList).where(RemoteList.sync_group_id == group.id)
    ).scalars():
        row.sync_group_id = None

    db.delete(group)
    db.commit()
    deps.flash(
        request,
        f"Deleted the {name!r} group. Nothing was removed from any service, and "
        "your lists keep their settings.",
        "success",
    )
    return deps.redirect("/sync")


def _wants_json(request: Request) -> bool:
    """Whether the caller is the page's own script rather than a form post.

    Pressing Sync now used to navigate to the history page, which threw away
    whatever you were in the middle of configuring. Answering the script in
    JSON lets the page stay exactly where it is and report progress in place.
    """
    return (
        request.headers.get("x-requested-with") == "fetch"
        or "application/json" in (request.headers.get("accept") or "")
    )


@router.post("/run")
def run_now(request: Request, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse

    def answer(message: str, started: bool, fallback: str):
        if _wants_json(request):
            return JSONResponse({"started": started, "message": message})
        deps.flash(request, message, "info")
        return deps.redirect(fallback)

    groups = db.execute(
        select(SyncGroup).where(SyncGroup.enabled.is_(True))
    ).scalars().all()
    if not groups:
        return answer(
            "Nothing is set up to sync yet. Tick a collection on at least one "
            "service list first.",
            False,
            "/sync",
        )

    if not scheduler.start_manual_sync():
        return answer("A sync is already running. Give it a moment.", False, "/sync")

    return answer(
        "Sync started. Progress shows at the top of the page; open Details for "
        "the full history.",
        True,
        "/sync/history",
    )


@router.post("/account/{account_id}/save")
def save_account_mapping(
    account_id: int,
    request: Request,
    row: list[int] = Form(default=[]),
    read: list[str] = Form(default=[]),
    writeout: list[str] = Form(default=[]),
    updatesonly: list[int] = Form(default=[]),
    kind_enabled: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Save one account's sync configuration.

    The model is deliberately simple at the front and only gets complicated if
    you ask it to.

    **Ticking a collection means two-way.** ``read`` carries
    ``"<list_id>:<collection_id>"`` for each ticked box, and a ticked box means
    that list and that collection keep each other up to date in both
    directions. That is what almost everyone wants, and making it the default
    removes the step people kept missing: previously you had to tick the list in
    its own write column as well, and a list that read but never wrote looked
    exactly like one that was working.

    **Writing out elsewhere is the advanced option.** ``writeout`` carries
    ``"<list_id>:<target_list_id>"`` for anyone who wants the merged result to
    land somewhere other than where it came from -- read "Grocery Shopping",
    write the result into "Shared Grocery List". Targets may be in any connected
    service.

    ``updatesonly`` names rows that should report changes without introducing
    tasks the collection has not seen.

    Two rules are enforced rather than left as advice:

    * A list may accept write-back from only one collection, because two would
      each create their own copy of every task and then undo one another.
    * **A list synced with a collection through its own row is never switched
      off by a save made on another service's page.** Saving the Todoist page
      used to clear write-back on a Google list, because from Todoist's side an
      unticked box is indistinguishable from one nobody ever ticked. Ownership
      now sits with the row itself.
    """
    account = db.get(Account, account_id)
    if account is None:
        deps.flash(request, "That account no longer exists.", "error")
        return deps.redirect("/services")

    for kind in (CollectionKind.TASKS, CollectionKind.CALENDAR):
        set_account_kind_enabled(account, kind, kind.value in kind_enabled)

    def _pairs(entries: list[str]) -> dict[int, set[int]]:
        out: dict[int, set[int]] = {}
        for entry in entries:
            left, _, right = entry.partition(":")
            if not left.isdigit():
                continue
            out.setdefault(int(left), set())
            if right.isdigit():
                out[int(left)].add(int(right))
        return out

    reads = _pairs(read)
    extras = _pairs(writeout)
    updates_only = {int(list_id) for list_id in updatesonly}

    rows = [
        remote_list
        for remote_list in (db.get(RemoteList, list_id) for list_id in row)
        if remote_list is not None and remote_list.account_id == account.id
    ]
    row_ids = {remote_list.id for remote_list in rows}
    problems: list[str] = []

    if not settings_store.is_advanced(db) and not writeout:
        # The "Also write out to" column is not on the page in simple mode, so
        # the form says nothing about it. Absence would otherwise read as "the
        # user cleared every one of these", and saving an unrelated row would
        # quietly demolish an aggregate set up while advanced mode was on.
        # Rebuilding the column's answer from what is already stored makes a
        # simple-mode save leave it exactly as it found it.
        #
        # Only when the form said nothing at all. A payload that does carry
        # write-out targets is answered on its own terms, so clearing them in
        # advanced mode still works and the endpoint stays honest to any caller.
        extras = {}
        for mapping in db.execute(
            select(ListMapping).where(ListMapping.write_from_list_ids.isnot(None))
        ).scalars():
            for source_id in mapping.write_from_list_ids or []:
                if source_id in row_ids:
                    extras.setdefault(source_id, set()).add(mapping.remote_list_id)

    desired: dict[tuple[int, int], dict[str, bool]] = {}

    def _entry(list_id: int, group_id: int) -> dict:
        return desired.setdefault(
            (list_id, group_id),
            # "full" means a member of the collection, holding everything in it.
            # "sources" names the lists an aggregate destination gathers from.
            {"read": False, "write": False, "create": True,
             "full": False, "sources": set()},
        )

    def _want(list_id: int, group_id: int, field: str) -> None:
        entry = _entry(list_id, group_id)
        entry[field] = True
        if list_id in updates_only:
            entry["create"] = False

    def _group_ok(remote_list: RemoteList, collection_id: int):
        collection = db.get(RadicaleCollection, collection_id)
        if collection is None:
            return None
        if collection.kind != remote_list.kind:
            problems.append(
                f"{remote_list.name!r} holds {remote_list.kind.value} but "
                f"{collection.display_name!r} holds {collection.kind.value}."
            )
            return None
        group = _group_for_collection(db, collection)
        if group is None:
            problems.append("Radicale is not configured, so nothing can sync yet.")
        return group

    # Who already writes to each list, so a second claimant can be named rather
    # than silently dropped. Seeded from the database because a claim made on
    # another service's page is just as real as one made here.
    writer_of: dict[int, tuple[int, str]] = {}
    for mapping in db.execute(
        select(ListMapping).where(ListMapping.write_enabled.is_(True))
    ).scalars():
        if mapping.remote_list_id in row_ids:
            continue  # This page is about to restate its own rows.
        group = db.get(SyncGroup, mapping.sync_group_id)
        if group is not None:
            writer_of[mapping.remote_list_id] = (group.id, group.name)

    def _claim_write(list_id: int, group, label: str) -> bool:
        claimed = writer_of.get(list_id)
        if claimed is not None and claimed[0] != group.id:
            problems.append(
                f"{label} already takes write-back from {claimed[1]!r}, so "
                f"{group.name!r} was left reading it only. One collection may "
                "write to a list; two would each create their own copy of every "
                "task and then undo the other."
            )
            return False
        writer_of[list_id] = (group.id, group.name)
        return True

    # 1. This account's own rows. A ticked collection is a two-way link.
    row_groups: dict[int, list] = {}
    for remote_list in rows:
        groups = []
        for collection_id in sorted(reads.get(remote_list.id, set())):
            group = _group_ok(remote_list, collection_id)
            if group is None:
                continue
            groups.append(group)
            _want(remote_list.id, group.id, "read")
            _entry(remote_list.id, group.id)["full"] = True
            if _claim_write(remote_list.id, group, repr(remote_list.name)):
                _want(remote_list.id, group.id, "write")
        row_groups[remote_list.id] = groups

    # 2. The advanced option: send the result somewhere else as well.
    for remote_list in rows:
        groups = row_groups.get(remote_list.id) or []
        targets = sorted(extras.get(remote_list.id, set()))
        if targets and not groups:
            problems.append(
                f"{remote_list.name!r} is set to write out to another list, but "
                "it is not synced with any collection, so there is nothing to "
                "write. Tick a collection for it first."
            )
            continue
        for target_id in targets:
            target = db.get(RemoteList, target_id)
            if target is None:
                continue
            owner = db.get(Account, target.account_id)
            if owner is None or not owner.enabled:
                continue
            if target.kind != remote_list.kind:
                problems.append(
                    f"{remote_list.name!r} holds {remote_list.kind.value} and "
                    f"cannot be written out to {target.name!r}."
                )
                continue
            for group in groups:
                if not _claim_write(target.id, group, repr(target.name)):
                    continue
                _want(target.id, group.id, "write")
                _entry(target.id, group.id)["sources"].add(remote_list.id)
                # A target that is not itself synced with the collection still
                # needs reading, or a task completed there can never come back.
                # Changes only, so it stays a destination rather than becoming a
                # second front door.
                existing = db.execute(
                    select(ListMapping).where(
                        ListMapping.remote_list_id == target.id,
                        ListMapping.sync_group_id == group.id,
                    )
                ).scalar_one_or_none()
                if target.id in row_ids:
                    continue  # Its own row already decided how it reads.
                if existing is not None and existing.read_enabled:
                    desired[(target.id, group.id)]["read"] = True
                    desired[(target.id, group.id)]["create"] = (
                        existing.create_from_remote is not False
                    )
                else:
                    desired[(target.id, group.id)]["read"] = True
                    desired[(target.id, group.id)]["create"] = False

    # 3. Apply. This account's rows are set to exactly what was submitted.
    active = 0
    for remote_list in rows:
        for mapping in db.execute(
            select(ListMapping).where(ListMapping.remote_list_id == remote_list.id)
        ).scalars().all():
            if (remote_list.id, mapping.sync_group_id) not in desired:
                db.delete(mapping)

    # A list belonging to another account is only ever cleared when this page
    # was the thing keeping it alive: a pure write-only destination, reading
    # nothing of its own. Anything that is genuinely synced with the collection
    # through its own page is left completely alone.
    fed_groups = {
        group_id
        for (list_id, group_id), wanted in desired.items()
        if wanted["read"] and list_id in row_ids
    }
    if fed_groups:
        for mapping in db.execute(
            select(ListMapping).where(
                ListMapping.sync_group_id.in_(fed_groups),
                ListMapping.write_enabled.is_(True),
                ListMapping.remote_list_id.notin_(row_ids or {0}),
            )
        ).scalars().all():
            if _is_anchor(db, mapping.remote_list_id):
                continue
            key = (mapping.remote_list_id, mapping.sync_group_id)
            if desired.get(key, {}).get("write"):
                continue
            if mapping.create_from_remote is not False:
                # Reads the collection as a full source: somebody set this up
                # deliberately on its own page. Not ours to switch off.
                continue
            mapping.write_enabled = False
            db.delete(mapping)

    for (list_id, group_id), wanted in desired.items():
        mapping = db.execute(
            select(ListMapping).where(
                ListMapping.remote_list_id == list_id,
                ListMapping.sync_group_id == group_id,
            )
        ).scalar_one_or_none()
        if mapping is None:
            mapping = ListMapping(remote_list_id=list_id, sync_group_id=group_id)
            db.add(mapping)
        mapping.read_enabled = wanted["read"]
        mapping.write_enabled = wanted["write"]
        mapping.create_from_remote = wanted.get("create", True)
        # A full member holds the whole collection, so it carries no filter. An
        # aggregate carries the lists it was set up to gather.
        if wanted.get("full") or not wanted.get("sources"):
            mapping.write_from_list_ids = None
        else:
            mapping.write_from_list_ids = sorted(wanted["sources"])
        if mapping.read_enabled or mapping.write_enabled:
            active += 1

    # A destination nobody reads is a one-way street, and a silent one: complete
    # the task there and nothing comes back. Legitimate if you are publishing
    # into a list you never look at, and almost never what someone means, so it
    # is said out loud rather than left to be discovered.
    for (list_id, group_id), wanted in desired.items():
        if not wanted["write"] or wanted["read"]:
            continue
        if _is_anchor(db, list_id):
            continue
        target = db.get(RemoteList, list_id)
        problems.append(
            f"{(target.name if target else 'That list')!r} is written to but not "
            "synced with any collection, so changes made there — completing a "
            "task, for instance — will not come back. Tick a collection for it "
            "to make it two-way."
        )

    # Keep the legacy columns roughly in step; the engine no longer reads them.
    for remote_list in rows:
        remote_list.sync_group_id = None
        remote_list.read_enabled = any(
            key[0] == remote_list.id and value["read"] for key, value in desired.items()
        )
        remote_list.write_enabled = any(
            key[0] == remote_list.id and value["write"] for key, value in desired.items()
        )

    db.commit()

    for problem in dict.fromkeys(problems):
        deps.flash(request, problem, "warning")
    deps.flash(
        request,
        f"Saved. {active} list-to-collection link(s) active. "
        "Press \u201cSync now\u201d above to apply it straight away, or wait for "
        "the next scheduled sync.",
        "success",
    )
    return deps.redirect(f"/services/{account.service.value}")


def _is_anchor(db: Session, remote_list_id: int) -> bool:
    """Whether a list is a Radicale collection's own list.

    The anchor is the canonical store: it always reads and always writes, and
    it is never in competition with a service list for the right to write. Any
    rule about write-back has to step around it, or saving one service's page
    would switch off the very thing every other service writes into.
    """
    remote_list = db.get(RemoteList, remote_list_id)
    if remote_list is None:
        return False
    account = db.get(Account, remote_list.account_id)
    return account is not None and account.service == ServiceKind.RADICALE


def _group_for_collection(db: Session, collection: RadicaleCollection) -> SyncGroup | None:
    """Find or create the sync group anchored on a Radicale collection."""
    group = db.execute(
        select(SyncGroup).where(SyncGroup.radicale_collection_id == collection.id)
    ).scalar_one_or_none()

    if group is None:
        group = SyncGroup(
            name=collection.display_name,
            kind=collection.kind,
            radicale_collection_id=collection.id,
            enabled=True,
        )
        db.add(group)
        db.commit()
        db.refresh(group)

    if _radicale_list_for(db, group, collection) is None:
        return None
    return group


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """Live progress, polled by the sync and history pages."""
    from fastapi.responses import JSONResponse

    running = scheduler.is_running()
    run_id = scheduler.active_run_id()
    run = db.get(SyncRun, run_id) if run_id else None
    if run is None:
        run = db.execute(
            select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
        ).scalar_one_or_none()

    if run is None:
        return JSONResponse({"running": running, "run": None})

    return JSONResponse(
        {
            "running": running,
            "run": {
                "id": run.id,
                "outcome": run.outcome.value if run.outcome else None,
                "trigger": run.trigger,
                "pulled": run.items_pulled,
                "pushed": run.items_pushed,
                "skipped": run.items_skipped,
                "errors": run.errors,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            },
        }
    )


@router.get("/history")
def history(request: Request, run_id: int = 0, db: Session = Depends(get_db)):
    runs = (
        db.execute(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(25))
        .scalars()
        .all()
    )
    selected = db.get(SyncRun, run_id) if run_id else (runs[0] if runs else None)
    entries = []
    if selected is not None:
        entries = (
            db.execute(
                select(SyncLogEntry)
                .where(SyncLogEntry.run_id == selected.id)
                .order_by(SyncLogEntry.at)
            )
            .scalars()
            .all()
        )

    return deps.render(
        request, db, "sync_history.html",
        runs=runs, selected=selected, entries=entries,
    )
