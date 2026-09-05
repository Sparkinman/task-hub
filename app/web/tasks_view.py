"""The task viewer.

Reads and writes VTODOs directly against the embedded Radicale server, so what
is shown here is the same data any CalDAV client sees -- there is no separate
copy that could drift out of step.

Tasks are grouped by urgency rather than listed flat, because the question a
task list has to answer first is "what needs attention now", not "what exists".
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import settings_store
from app.db.models import CollectionKind, ItemStatus, ServiceKind
from app.db.session import get_db
from app.services.caldav_client import CalDAVError
from app.services.ical_model import CanonicalRecord, new_uid
from app.web import deps
from app.web.radicale_admin import get_radicale_client

router = APIRouter(prefix="/tasks")

#: Order matters: this is the order the groups appear on the page.
GROUP_ORDER = ("overdue", "today", "tomorrow", "week", "later", "someday", "completed")

#: How many finished tasks the page draws. Enough to find the one you just
#: ticked off by mistake, not so many that a year of history has to render.
COMPLETED_SHOWN = 50

GROUP_LABELS = {
    "overdue": "Overdue",
    "today": "Today",
    "tomorrow": "Tomorrow",
    "week": "This week",
    "later": "Later",
    "someday": "No date",
    "completed": "Completed",
}


def _parse_date(value: str | None) -> dt.date | None:
    if not value or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_time(value: str | None) -> dt.time | None:
    if not value or not value.strip():
        return None
    raw = value.strip()
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            return dt.datetime.strptime(raw, pattern).time()
        except ValueError:
            continue
    return None


def group_for(record: CanonicalRecord, today: dt.date, tz_name: str) -> str:
    """Classify a task into one of the display groups."""
    if record.status == ItemStatus.COMPLETED:
        return "completed"
    if record.due_date is None:
        return "someday"

    if record.due_date < today:
        return "overdue"
    if record.due_date == today:
        # A task due earlier today with a time that has already passed is
        # overdue in the sense that matters to the person reading the list.
        if record.due_time is not None and record.is_overdue(fallback_tz=tz_name):
            return "overdue"
        return "today"
    if record.due_date == today + dt.timedelta(days=1):
        return "tomorrow"
    if record.due_date <= today + dt.timedelta(days=7):
        return "week"
    return "later"


def _sort_key(record: CanonicalRecord) -> tuple:
    """Order within a group: soonest first, then by priority, then by title.

    Undated tasks sort last, and priority 0 ("unset") is treated as lowest
    rather than highest even though it is numerically smallest.
    """
    has_date = record.due_date is not None
    priority = record.priority if record.priority else 10
    return (
        not has_date,
        record.due_date or dt.date.max,
        record.due_time or dt.time.max,
        priority,
        record.title.lower(),
    )


def _task_collections(db: Session, client) -> list:
    return [c for c in client.list_collections() if c.kind == CollectionKind.TASKS]


@router.get("")
@router.get("/")
def index(
    request: Request,
    collection: list[str] = Query(default=[]),
    show_completed: str = "",
    q: str = "",
    db: Session = Depends(get_db),
):
    client = get_radicale_client(db)
    if client is None:
        return deps.render(
            request, db, "tasks.html",
            configured=False, collections=[], groups={}, total=0,
        )

    tz_name = settings_store.get_timezone(db)
    today = dt.datetime.now(deps.resolve_tz(tz_name)).date()
    include_completed = show_completed == "1"

    errors: list[str] = []
    try:
        collections = _task_collections(db, client)
    except CalDAVError as exc:
        return deps.render(
            request, db, "tasks.html",
            configured=True, collections=[], groups={}, total=0, errors=[str(exc)],
        )

    # Several lists at once, so somebody can look at work and home together
    # without either seeing everything or flipping between them one at a time.
    chosen = {c for c in collection if c}
    selected = [c for c in collections if not chosen or c.collection_id in chosen]

    rows: list[dict] = []
    for info in selected:
        try:
            records = client.list_records(
                info.collection_id, CollectionKind.TASKS, include_completed=True
            )
        except CalDAVError as exc:
            errors.append(f"{info.display_name}: {exc}")
            continue
        for record in records:
            rows.append({"record": record, "collection": info})

    # Every task in a hierarchy carries a badge naming the piece of work it
    # belongs to, and the badge opens that whole breakdown. The list itself is
    # grouped by when things are due, so a parent and its steps are scattered
    # across date groups -- the badge is what ties them back together, and it
    # has to be on the parent as well as the children or the parent is the one
    # row with no way through to its own steps.
    by_uid = {row["record"].uid: row["record"] for row in rows if row["record"].uid}
    kids: dict[str, list] = {}
    for row in rows:
        parent_uid = getattr(row["record"], "parent_uid", None)
        if parent_uid:
            kids.setdefault(parent_uid, []).append(row["record"])

    def root_of(record):
        """The top of this task's tree, however deep it sits."""
        seen = {record.uid}
        while getattr(record, "parent_uid", None):
            parent = by_uid.get(record.parent_uid)
            # A parent outside this view, or a cycle: stop where we are rather
            # than looping or pointing at something that cannot be opened.
            if parent is None or parent.uid in seen:
                break
            seen.add(parent.uid)
            record = parent
        return record

    for row in rows:
        record = row["record"]
        mine = kids.get(record.uid or "", [])
        row["step_count"] = len(mine)
        row["steps_done"] = sum(1 for k in mine if k.is_completed)

        parent = by_uid.get(getattr(record, "parent_uid", None) or "")
        row["parent_title"] = parent.title if parent is not None else None
        row["parent_uid"] = parent.uid if parent is not None else None

        # Present on both halves of a relationship, absent on a lone task.
        if parent is not None or mine:
            root = root_of(record)
            row["tree_title"] = root.title
            row["tree_uid"] = root.uid
            row["tree_size"] = sum(
                1 for r in rows
                if root_of(r["record"]).uid == root.uid
            ) - 1
        else:
            row["tree_title"] = None
            row["tree_uid"] = None
            row["tree_size"] = 0

    needle = q.strip().lower()
    if needle:
        rows = [
            row for row in rows
            if needle in row["record"].title.lower()
            or needle in (row["record"].notes or "").lower()
        ]

    # Completed tasks are always kept. They live in a collapsed section at the
    # bottom rather than behind a tick box: the reason to look at them is
    # usually to un-tick one, and a control you have to find first makes that
    # harder than it needs to be.

    # A family stays together. A step is filed under whichever heading its
    # parent is under, rather than under the one its own due date would put it
    # in -- otherwise a piece of work is scattered across four headings and the
    # only thing holding it together is a badge.
    by_uid_row = {row["record"].uid: row for row in rows if row["record"].uid}

    # A piece of work is as urgent as the soonest thing in it. A parent with no
    # date of its own whose step is due today belongs under Today, not under
    # Someday with the step dragged down beside it -- what needs doing this
    # morning should be where somebody looks for what needs doing this morning.
    steps_of: dict[str, list] = {}
    for row in rows:
        parent_uid = getattr(row["record"], "parent_uid", None)
        if parent_uid:
            steps_of.setdefault(parent_uid, []).append(row["record"])

    def urgent_of(record):
        """The soonest due date in this task and its steps, if any has one."""
        dates = [record.due_date] if record.due_date else []
        for step in steps_of.get(record.uid or "", []):
            # A finished step no longer makes anything urgent.
            if step.due_date and not step.is_completed:
                dates.append(step.due_date)
        return min(dates) if dates else None
    groups: dict[str, list[dict]] = {name: [] for name in GROUP_ORDER}
    for row in rows:
        record = row["record"]
        head = by_uid_row.get(getattr(record, "parent_uid", None) or "")
        # Completion is the exception: a finished step belongs with the other
        # finished things, or the completed section would be missing the very
        # rows somebody opened it to un-tick.
        if record.is_completed:
            where = "completed"
        else:
            # The whole family is placed by its soonest date, so it moves as one
            # block rather than splitting across headings.
            anchor = head["record"] if head is not None else record
            soonest = urgent_of(anchor)
            if soonest is None:
                where = "someday"
            else:
                stand_in = dataclasses.replace(
                    anchor, due_date=soonest,
                    due_time=anchor.due_time if soonest == anchor.due_date else None,
                    status=ItemStatus.NEEDS_ACTION,
                )
                where = group_for(stand_in, today, tz_name)
        groups[where].append(row)

    def family_key(row: dict) -> tuple:
        """Sort parents by date, and hold each one's steps beneath it.

        Latest first, with undated tasks last -- and a step is ordered by its
        parent's date, then by its own, so the steps of one piece of work stay
        in one block and in a sensible order within it.
        """
        record = row["record"]
        head = by_uid_row.get(getattr(record, "parent_uid", None) or "")
        anchor = head["record"] if head is not None else record
        soonest = urgent_of(anchor)
        # Undated last whichever way the dated ones are ordered.
        undated = soonest is None
        # Negated rather than reversed, so "latest first" applies to the
        # families without also turning each family upside down.
        stamp = -soonest.toordinal() if soonest else 0
        return (
            undated,
            stamp,
            anchor.title.lower(),
            head is not None,              # the parent leads its own steps
            _sort_key(record) if head is not None else (),
        )

    for name in groups:
        groups[name].sort(key=family_key)
    # Most recently finished first is more useful than oldest-first here.
    groups["completed"].sort(
        key=lambda row: row["record"].completed_at or dt.datetime.min.replace(
            tzinfo=dt.timezone.utc
        ),
        reverse=True,
    )

    open_count = sum(
        len(items) for name, items in groups.items() if name != "completed"
    )

    # A long history is not worth rendering in full; the recent end is the part
    # anyone actually reaches for.
    completed_total = len(groups["completed"])
    groups["completed"] = groups["completed"][:COMPLETED_SHOWN]

    # Offered as possible parents: everything still open, so a task can be
    # made as a step of something in one go rather than created and then
    # attached. Completed tasks are left out -- nobody adds a step to finished
    # work -- and so are tasks that are already steps, since one level is what
    # the interface shows.
    parent_choices = [
        {
            "uid": row["record"].uid,
            "title": row["record"].title,
            "collection_id": row["collection"].collection_id,
        }
        for row in rows
        if not row["record"].is_completed and not row["record"].parent_uid
    ]
    parent_choices.sort(key=lambda p: p["title"].lower())

    return deps.render(
        request, db, "tasks.html",
        configured=True,
        parent_choices=parent_choices,
        repeat_labels=REPEAT_LABELS,
        repeat_key=repeat_key,
        collections=collections,
        selected_collections=chosen,
        # Kept for everything that only makes sense with one list in view:
        # which list a new task goes into, and whether each row needs to say
        # where it came from. Empty when several are showing, which is right.
        selected_collection=(next(iter(chosen)) if len(chosen) == 1 else ""),
        groups=groups,
        group_order=GROUP_ORDER,
        group_labels=GROUP_LABELS,
        total=len(rows),
        open_count=open_count,
        completed_count=completed_total,
        completed_shown=len(groups["completed"]),
        completed_limit=COMPLETED_SHOWN,
        show_completed=include_completed,
        query=q,
        today=today,
        errors=errors,
    )


@router.get("/{collection_id}/{uid}/steps")
def steps(collection_id: str, uid: str, request: Request,
          db: Session = Depends(get_db)):
    """One task and everything under it, in order, on a page of its own.

    The main list is grouped by when things are due, which is the right answer
    for deciding what to do next and the wrong one for seeing how a piece of
    work is put together -- a parent and its steps end up scattered across four
    date groups. This is the other view: the shape rather than the schedule.
    """
    client = get_radicale_client(db)
    if client is None:
        return deps.redirect("/tasks")

    info = next(
        (c for c in _task_collections(db, client) if c.collection_id == collection_id),
        None,
    )
    if info is None:
        deps.flash(request, "That task list is no longer here.", "error")
        return deps.redirect("/tasks")

    try:
        records = client.list_records(
            info.collection_id, CollectionKind.TASKS, include_completed=True
        )
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/tasks")

    by_uid = {r.uid: r for r in records if r.uid}
    root = by_uid.get(uid)
    if root is None:
        deps.flash(request, "That task is no longer here.", "error")
        return deps.redirect("/tasks")

    # Shown from the top of the tree rather than from whichever task was
    # clicked: opening a middle step and being shown only what hangs off it
    # hides the very context the page exists to give.
    seen = {root.uid}
    while root.parent_uid and root.parent_uid in by_uid:
        if root.parent_uid in seen:
            break  # A cycle; stop rather than loop.
        seen.add(root.parent_uid)
        root = by_uid[root.parent_uid]

    children: dict[str, list] = {}
    for record in records:
        if record.parent_uid:
            children.setdefault(record.parent_uid, []).append(record)
    for group in children.values():
        group.sort(key=_sort_key)

    def walk(record, depth: int) -> list[dict]:
        """The task, then everything under it, depth-first."""
        mine = children.get(record.uid or "", [])
        done = sum(1 for k in mine if k.is_completed)
        rows = [{
            "record": record, "depth": depth,
            "step_count": len(mine), "steps_done": done,
        }]
        if depth < 16:  # A guard, not a limit anybody will meet.
            for child in mine:
                rows.extend(walk(child, depth + 1))
        return rows

    rows = walk(root, 0)
    total = len(rows) - 1
    return deps.render(
        request, db, "task_steps.html",
        collection=info,
        rows=rows,
        root=root,
        focused=uid,
        total_steps=total,
        done_steps=sum(1 for r in rows[1:] if r["record"].is_completed),
        today=dt.datetime.now(deps.resolve_tz(settings_store.get_timezone(db))).date(),
    )


#: The repeats offered on the task forms, as iCalendar rules.
#:
#: A deliberately short list. RRULE can express "the last working day of every
#: quarter", but a menu that can say that is a menu nobody can read, and every
#: service Task Hub talks to supports these four.
REPEATS: dict[str, str] = {
    "": "",
    "daily": "FREQ=DAILY",
    "weekly": "FREQ=WEEKLY",
    "monthly": "FREQ=MONTHLY",
    "yearly": "FREQ=YEARLY",
}

REPEAT_LABELS: dict[str, str] = {
    "": "Does not repeat",
    "daily": "Every day",
    "weekly": "Every week",
    "monthly": "Every month",
    "yearly": "Every year",
}


def repeat_key(rrule: str | None) -> str:
    """Which of the offered repeats this rule is, if it is one of them.

    A rule written elsewhere -- by another CalDAV client, or by a service with a
    richer editor -- will not match, and that is deliberate: it is shown as
    "custom" and left completely alone rather than being flattened into the
    nearest thing this menu can say.
    """
    if not rrule:
        return ""
    normalised = rrule.upper().replace("RRULE:", "").strip()
    for key, value in REPEATS.items():
        if value and normalised == value:
            return key
    return "custom"


def _add_steps(client, collection_id: str, parent_uid: str, steps: str,
               now: dt.datetime) -> tuple[int, int]:
    """Create a subtask for each non-empty line. Returns (made, failed).

    Only ever adds. Nothing here renames or removes an existing step, which is
    what makes it safe to offer on a form somebody may submit repeatedly: the
    box is never filled in with what is already there, so saving a task without
    typing in it adds nothing at all.

    Each failure is counted rather than aborting the rest -- losing four steps
    because the third had a bad character would be a poor trade for somebody
    who typed all five.
    """
    made = failed = 0
    for line in (steps or "").splitlines():
        step = line.strip().lstrip("-*").strip()
        if not step:
            continue
        child = CanonicalRecord(
            uid=new_uid(),
            kind=CollectionKind.TASKS,
            parent_uid=parent_uid,
            title=step[:500],
            status=ItemStatus.NEEDS_ACTION,
            origin_service=ServiceKind.LOCAL,
            created_at=now,
            updated_at=now,
        )
        try:
            client.save_record(collection_id, child)
            made += 1
        except CalDAVError:
            failed += 1
    return made, failed


def _said_about_steps(what: str, made: int, failed: int) -> str:
    if failed:
        return (f"{what} with {made} sub task{'' if made == 1 else 's'}, "
                f"but {failed} could not be saved.")
    if made:
        return f"{what} with {made} sub task{'' if made == 1 else 's'}."
    return f"{what}."


def _back_to(collection: str, show_completed: str, q: str) -> str:
    """Return to the filtered list the task was added from, not a bare /tasks."""
    from urllib.parse import urlencode

    params = {}
    if collection:
        params["collection"] = collection
    if show_completed == "1":
        params["show_completed"] = "1"
    if q:
        params["q"] = q
    return "/tasks" + (f"?{urlencode(params)}" if params else "")


@router.post("/create")
def create_task(
    request: Request,
    collection_id: str = Form(...),
    title: str = Form(...),
    due_date: str = Form(""),
    due_time: str = Form(""),
    priority: str = Form("0"),
    notes: str = Form(""),
    parent_uid: str = Form(""),
    steps: str = Form(""),
    repeats: str = Form(""),
    back_collection: str = Form(""),
    back_completed: str = Form(""),
    back_q: str = Form(""),
    db: Session = Depends(get_db),
):
    back = _back_to(back_collection, back_completed, back_q)
    client = get_radicale_client(db)
    if client is None:
        deps.flash(request, "Radicale is not configured yet.", "error")
        return deps.redirect(back)

    title = title.strip()
    if not title:
        deps.flash(request, "Give the task a title.", "error")
        return deps.redirect(back)

    parsed_date = _parse_date(due_date)
    parsed_time = _parse_time(due_time)
    if parsed_time is not None and parsed_date is None:
        deps.flash(request, "A time needs a date to go with it.", "error")
        return deps.redirect(back)

    try:
        priority_value = max(0, min(9, int(priority)))
    except ValueError:
        priority_value = 0

    now = dt.datetime.now(dt.timezone.utc)
    record = CanonicalRecord(
        uid=new_uid(),
        kind=CollectionKind.TASKS,
        # A step of something larger, when one was chosen. Held as the parent's
        # UID rather than anything positional, so it survives both tasks being
        # renamed, re-dated or moved between date groups.
        parent_uid=parent_uid.strip() or None,
        title=title,
        notes=notes.strip() or None,
        status=ItemStatus.NEEDS_ACTION,
        due_date=parsed_date,
        due_time=parsed_time,
        # Only attach a timezone when there is a time for it to qualify. A
        # date-only due date is deliberately zone-free, so it means the same
        # calendar day everywhere rather than shifting across a date line.
        due_tz=settings_store.get_timezone(db) if parsed_time else None,
        priority=priority_value,
        rrule=REPEATS.get(repeats) or None,
        # Created here, so it earns the blue "Task Hub" badge.
        origin_service=ServiceKind.LOCAL,
        created_at=now,
        updated_at=now,
    )

    try:
        client.save_record(collection_id, record)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(back)

    made, failed = _add_steps(client, collection_id, record.uid, steps, now)
    deps.flash(request, _said_about_steps(f"Added {title!r}", made, failed),
               "warning" if failed else "success")
    return deps.redirect(back)


@router.post("/{collection_id}/{uid}/toggle")
def toggle_task(
    collection_id: str,
    uid: str,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Flip a task between completed and needs-action.

    Returns JSON so the page can update a single row without a full reload,
    which keeps the list from jumping under the cursor mid-click.
    """
    client = get_radicale_client(db)
    if client is None:
        return JSONResponse({"error": "Radicale is not configured."}, status_code=400)

    try:
        record = client.get_record(collection_id, uid, CollectionKind.TASKS)
    except CalDAVError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    if record is None:
        return JSONResponse({"error": "That task no longer exists."}, status_code=404)

    now = dt.datetime.now(dt.timezone.utc)
    if record.status == ItemStatus.COMPLETED:
        record.status = ItemStatus.NEEDS_ACTION
        record.completed_at = None
    else:
        record.status = ItemStatus.COMPLETED
        record.completed_at = now
    record.updated_at = now

    try:
        client.save_record(collection_id, record)
    except CalDAVError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    # A form submitted without JavaScript names where to go back to. Answering
    # that with JSON is what put a page of raw data in front of somebody who
    # only ticked a box.
    if return_to:
        return deps.redirect(return_to)

    return JSONResponse(
        {
            "uid": uid,
            "completed": record.status == ItemStatus.COMPLETED,
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        }
    )


@router.post("/{collection_id}/{uid}/update")
def update_task(
    request: Request,
    collection_id: str,
    uid: str,
    title: str = Form(...),
    due_date: str = Form(""),
    due_time: str = Form(""),
    priority: str = Form("0"),
    notes: str = Form(""),
    steps: str = Form(""),
    repeats: str = Form("custom"),
    db: Session = Depends(get_db),
):
    client = get_radicale_client(db)
    if client is None:
        return deps.redirect("/tasks")

    try:
        record = client.get_record(collection_id, uid, CollectionKind.TASKS)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/tasks")

    if record is None:
        deps.flash(request, "That task no longer exists.", "error")
        return deps.redirect("/tasks")

    title = title.strip()
    if not title:
        deps.flash(request, "The title cannot be empty.", "error")
        return deps.redirect("/tasks")

    parsed_date = _parse_date(due_date)
    parsed_time = _parse_time(due_time)

    record.title = title
    record.notes = notes.strip() or None
    record.due_date = parsed_date
    record.due_time = parsed_time if parsed_date is not None else None
    if record.due_time is not None and not record.due_tz:
        record.due_tz = settings_store.get_timezone(db)
    if record.due_time is None:
        record.due_tz = None

    try:
        record.priority = max(0, min(9, int(priority)))
    except ValueError:
        record.priority = 0

    # "custom" means the form could not represent what is already there, so it
    # is left exactly as it is. Anything else is a choice somebody just made.
    if repeats != "custom":
        record.rrule = REPEATS.get(repeats) or None

    record.updated_at = dt.datetime.now(dt.timezone.utc)

    try:
        client.save_record(collection_id, record)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/tasks")

    # Purely additive, and the box is never filled in with the steps that
    # already exist. Saving the form without typing in it therefore changes
    # nothing, however many times it is saved -- which is what makes offering
    # this here safe. Filling it in with the current steps and trying to work
    # out the difference is the version that duplicates and deletes.
    made, failed = _add_steps(client, collection_id, record.uid, steps,
                              record.updated_at)
    deps.flash(request, _said_about_steps("Task updated", made, failed),
               "warning" if failed else "success")
    return deps.redirect("/tasks")


@router.post("/clear-completed")
def clear_completed(
    request: Request,
    collection: list[str] = Query(default=[]),
    back_collection: str = Form(""),
    db: Session = Depends(get_db),
):
    """Remove finished tasks, leaving anything that repeats.

    A repeating task is one object, not one per occurrence: the rule lives on
    the task itself. So ticking off this week's instance and then deleting it
    would not tidy away a finished thing -- it would delete the whole series,
    every future occurrence with it. Anything carrying a repeat rule is
    therefore skipped, and said so afterwards.

    Google is the reason to be careful rather than clever here. It handles a
    repeating task by keeping the completed one and making the next itself, so
    what looks like a finished task may be Google's own record of an occurrence
    rather than something anybody wants removed.

    Deleting reaches every service, the same as deleting a task by hand does.
    That is the point -- clearing them here and leaving them everywhere else
    would tidy nothing -- but it is why the button asks first.
    """
    client = get_radicale_client(db)
    if client is None:
        return deps.redirect("/tasks")

    chosen = {c for c in collection if c}
    removed = kept = failed = 0
    for info in _task_collections(db, client):
        if chosen and info.collection_id not in chosen:
            continue
        try:
            records = client.list_records(
                info.collection_id, CollectionKind.TASKS, include_completed=True
            )
        except CalDAVError:
            continue
        for record in records:
            if not record.is_completed:
                continue
            if record.rrule:
                kept += 1
                continue
            try:
                client.delete_record(info.collection_id, record.uid)
                removed += 1
            except CalDAVError:
                failed += 1

    said = f"Cleared {removed} completed task{'' if removed == 1 else 's'}."
    if kept:
        said += (f" {kept} left alone because {'it repeats' if kept == 1 else 'they repeat'}"
                 " — deleting one would end the whole series.")
    if failed:
        said += f" {failed} could not be removed."
    deps.flash(request, said, "warning" if failed else "success")
    return deps.redirect(_back_to(back_collection, "1", ""))


@router.post("/{collection_id}/{uid}/delete")
def delete_task(
    request: Request,
    collection_id: str,
    uid: str,
    db: Session = Depends(get_db),
):
    client = get_radicale_client(db)
    if client is None:
        return deps.redirect("/tasks")
    try:
        client.delete_record(collection_id, uid)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/tasks")
    deps.flash(request, "Task deleted.", "success")
    return deps.redirect("/tasks")
