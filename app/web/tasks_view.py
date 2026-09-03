"""The task viewer.

Reads and writes VTODOs directly against the embedded Radicale server, so what
is shown here is the same data any CalDAV client sees -- there is no separate
copy that could drift out of step.

Tasks are grouped by urgency rather than listed flat, because the question a
task list has to answer first is "what needs attention now", not "what exists".
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
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
    collection: str = "",
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

    selected = [c for c in collections if not collection or c.collection_id == collection]

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

    groups: dict[str, list[dict]] = {name: [] for name in GROUP_ORDER}
    for row in rows:
        groups[group_for(row["record"], today, tz_name)].append(row)
    for name in groups:
        groups[name].sort(key=lambda row: _sort_key(row["record"]))
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

    return deps.render(
        request, db, "tasks.html",
        configured=True,
        collections=collections,
        selected_collection=collection,
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

    deps.flash(request, f"Added {title!r}.", "success")
    return deps.redirect(back)


@router.post("/{collection_id}/{uid}/toggle")
def toggle_task(
    collection_id: str,
    uid: str,
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

    record.updated_at = dt.datetime.now(dt.timezone.utc)

    try:
        client.save_record(collection_id, record)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/tasks")

    deps.flash(request, "Task updated.", "success")
    return deps.redirect("/tasks")


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
