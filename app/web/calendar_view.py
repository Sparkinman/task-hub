"""The calendar: month, week, day and agenda, with tasks alongside events.

Four views rather than one, because the question changes with the horizon. A
month grid answers "how busy is October"; a day column answers "what is
happening this afternoon". An agenda answers "what is next" and is the only one
that copes gracefully with a nearly empty calendar.

Tasks are drawn in alongside events because they compete for the same hours. A
calendar that hides them tells you an afternoon is free when three things are
due in it. They stay visually distinct, can be switched off, and completed ones
can be hidden separately.

Everything is read from the embedded Radicale server, so this shows exactly what
any connected CalDAV client sees.
"""

from __future__ import annotations

import calendar as calendar_module
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
from app.web.tasks_view import _parse_date, _parse_time

router = APIRouter(prefix="/calendar")

VIEWS = ("month", "week", "day", "agenda")
DEFAULT_VIEW = "month"

#: The hours a day and week grid draws. Outside this, events are still shown --
#: they are pinned to the edge rather than hidden -- but the grid does not waste
#: half its height on the small hours nobody has appointments in.
DAY_START_HOUR = 7
DAY_END_HOUR = 22

#: How far the agenda's side panel looks ahead, in days beyond its first day.
LOOKAHEAD_DAYS = 7

#: The longest run of days one item is allowed to occupy. A holiday or a
#: conference is a handful of days; anything claiming to last months is a task
#: that picked up a start date years ago, and drawing it across every cell of
#: every month would bury everything else. Past this it is shown on its own end
#: day alone, which is the day that actually matters.
MAX_SPAN_DAYS = 60


def _checkbox(carrier: str, value: str, default: bool) -> bool:
    """Read a checkbox that may not have been submitted at all.

    An unticked checkbox sends nothing, which on its own is indistinguishable
    from "the form was never involved" -- which is why the tasks toggle used to
    spring straight back on. The form carries a hidden marker alongside it; when
    that marker is present a missing value genuinely means unticked, and when it
    is absent we are following a plain link and fall back to the default.
    """
    if carrier == "1":
        return value == "1"
    if value == "0":
        return False
    if value == "1":
        return True
    return default


def _parse_anchor(value: str, today: dt.date) -> dt.date:
    if not value:
        return today
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        return today


def _week_start(day: dt.date, week_starts_sunday: bool) -> dt.date:
    """The first day of the week containing ``day``."""
    if week_starts_sunday:
        return day - dt.timedelta(days=(day.weekday() + 1) % 7)
    return day - dt.timedelta(days=day.weekday())


def _range_for(view: str, anchor: dt.date, week_starts_sunday: bool) -> tuple[dt.date, dt.date]:
    """Inclusive first and last dates a view needs loaded."""
    if view == "day":
        return anchor, anchor
    if view == "week":
        start = _week_start(anchor, week_starts_sunday)
        return start, start + dt.timedelta(days=6)
    if view == "agenda":
        return anchor, anchor + dt.timedelta(days=60)
    # Month: whole grid, including the leading and trailing days that fill it.
    first = anchor.replace(day=1)
    last = first.replace(day=calendar_module.monthrange(first.year, first.month)[1])
    return (_week_start(first, week_starts_sunday),
            _week_start(last, week_starts_sunday) + dt.timedelta(days=6))


def _shift(view: str, anchor: dt.date, direction: int) -> dt.date:
    """Move one view-sized step forwards or backwards."""
    if view == "day":
        return anchor + dt.timedelta(days=direction)
    if view in ("week", "agenda"):
        return anchor + dt.timedelta(weeks=direction)
    month = anchor.month - 1 + direction
    year = anchor.year + month // 12
    month = month % 12 + 1
    day = min(anchor.day, calendar_module.monthrange(year, month)[1])
    return dt.date(year, month, day)


def _occurrences(
    record: CanonicalRecord, first: dt.date, last: dt.date,
    anchor: dt.date | None = None,
) -> list[dt.date]:
    """Every date in the window on which this record should appear.

    Recurring events are expanded from their rule. Only the common frequencies
    are handled; anything more elaborate falls back to showing the original
    occurrence, which is honest -- better a single visible entry than a silently
    empty calendar or a wrong guess repeated fifty times.

    The anchor is the day the first occurrence sits on. It defaults to the
    record's own start, and is passed explicitly by callers that need to pin an
    item somewhere else -- a task that is not spanning belongs on the day it is
    due, not on a start date it may have been carrying for years.
    """
    start = anchor or record.start_date or record.due_date
    if start is None:
        return []
    if not record.rrule:
        return [start] if first <= start <= last else []

    rule = record.rrule.upper().replace("RRULE:", "")
    parts = dict(piece.split("=", 1) for piece in rule.split(";") if "=" in piece)
    freq = parts.get("FREQ", "")
    try:
        interval = max(1, int(parts.get("INTERVAL", "1")))
    except ValueError:
        interval = 1

    until: dt.date | None = None
    raw_until = (parts.get("UNTIL") or "")[:8]
    if len(raw_until) == 8 and raw_until.isdigit():
        try:
            until = dt.date(int(raw_until[:4]), int(raw_until[4:6]), int(raw_until[6:]))
        except ValueError:
            until = None

    dates: list[dt.date] = []
    cursor = start
    guard = 0
    while cursor <= last and guard < 800:
        guard += 1
        if cursor >= first and (until is None or cursor <= until):
            dates.append(cursor)
        if freq == "DAILY":
            cursor += dt.timedelta(days=interval)
        elif freq == "WEEKLY":
            cursor += dt.timedelta(weeks=interval)
        elif freq == "MONTHLY":
            month = cursor.month - 1 + interval
            year = cursor.year + month // 12
            month = month % 12 + 1
            day = min(cursor.day, calendar_module.monthrange(year, month)[1])
            cursor = dt.date(year, month, day)
        elif freq == "YEARLY":
            try:
                cursor = cursor.replace(year=cursor.year + interval)
            except ValueError:      # 29 February in a non-leap year
                cursor = cursor.replace(year=cursor.year + interval, day=28)
        else:
            break
        if until is not None and cursor > until:
            break
    return dates


def _span_days(record: CanonicalRecord, kind: str) -> int:
    """How many days one occurrence of this record covers.

    An all-day event stores the day AFTER it finishes as its end, because that
    is what iCalendar means by DTEND on a date value. A timed event stores the
    day the end time falls on. Getting that one day wrong shows every all-day
    event running a day longer than it does, so the two cases are separated
    rather than averaged.

    A task spans only when it carries a start date genuinely earlier than its
    due date. Most tasks have no start at all, or a start equal to the due date,
    and those stay on the single day they are due.
    """
    if kind == "task":
        start, finish = record.start_date, record.due_date
    else:
        start = record.start_date or record.due_date
        finish = record.end_date
        if finish is not None and record.start_time is None and record.end_time is None:
            finish = finish - dt.timedelta(days=1)

    if start is None or finish is None or finish <= start:
        return 1
    return (finish - start).days + 1


def _placements(
    record: CanonicalRecord, first: dt.date, last: dt.date, kind: str
) -> list[tuple[dt.date, int, int]]:
    """Every (day, position in the run, length of the run) to draw.

    A three-day event is three placements, so it appears on all three days
    rather than only the first. Position and length come along because the
    template draws a run differently from three unrelated copies: only the first
    day carries the time, and the others say they are a continuation.
    """
    span = _span_days(record, kind)

    if span == 1 or span > MAX_SPAN_DAYS:
        # One day only. For an event that is its start; for a task it is the day
        # it is due, which is not the same thing -- a task can carry a start date
        # from long ago, and anchoring on that would file it under the wrong day
        # entirely. An over-long run collapses to its end day for the same
        # reason: the deadline is the part worth seeing.
        span = 1
        anchor = record.due_date if kind == "task" else None
    else:
        anchor = None

    # Look back far enough to catch a run that began before this window and is
    # still going inside it -- otherwise a week view opened on the Wednesday of
    # a Monday-to-Friday trip would show nothing at all.
    starts = _occurrences(
        record, first - dt.timedelta(days=span - 1), last, anchor=anchor
    )

    placements: list[tuple[dt.date, int, int]] = []
    for start in starts:
        for offset in range(span):
            day = start + dt.timedelta(days=offset)
            if day > last:
                break
            if day >= first:
                placements.append((day, offset, span))
    return placements


def _entry(
    record: CanonicalRecord, info, kind: str, on: dt.date,
    offset: int = 0, span: int = 1,
) -> dict:
    badge = deps.badge_for(record.origin_service)
    return {
        "record": record,
        "collection": info,
        "kind": kind,                       # "event" or "task"
        "date": on,
        "badge": badge,
        "all_day": record.start_time is None if kind == "event" else record.due_time is None,
        # A run's clock times belong to its ends. Repeating "09:00" on the
        # middle day of a three-day trip states something untrue about that day.
        "time": (record.start_time if kind == "event" else record.due_time)
                if offset == 0 else None,
        "end_time": record.end_time if kind == "event" and offset == span - 1 else None,
        "done": record.status == ItemStatus.COMPLETED,
        "recurring": bool(record.rrule),
        #: Where this day sits in a run of days, for the template.
        "span": span,
        "spans": span > 1,
        "span_start": offset == 0,
        "span_end": offset == span - 1,
        "span_middle": 0 < offset < span - 1,
        #: The real first and last day of the run, so a continuation day can say
        #: what it is part of instead of just showing a title with no dates.
        "span_first": on - dt.timedelta(days=offset),
        "span_last": on + dt.timedelta(days=span - 1 - offset),
    }


def _sort_key(entry: dict):
    # All-day items first, then by clock time; a stable, readable order.
    return (entry["time"] is not None, entry["time"] or dt.time.min,
            entry["record"].title.lower())


def _iso_week(row_start: dt.date) -> int:
    """The week number to print beside a row of the month grid.

    Taken from midweek rather than the first cell, so the number is right
    whether the week is drawn Monday-first or Sunday-first -- a Sunday belongs
    to the ISO week that starts the following day, and labelling the row by its
    Sunday would be off by one for six days out of seven.
    """
    return (row_start + dt.timedelta(days=3)).isocalendar()[1]


@router.get("")
@router.get("/")
def index(
    request: Request,
    view: str = DEFAULT_VIEW,
    anchor: str = "",
    collection: str = "",
    show_tasks: str = "",
    hide_done: str = "",
    filters: str = "",
    db: Session = Depends(get_db),
):
    client = get_radicale_client(db)
    tz_name = settings_store.get_timezone(db)
    today = dt.datetime.now(deps.resolve_tz(tz_name)).date()
    week_starts_sunday = settings_store.get(db, settings_store.WEEK_START) == "sunday"

    view = view if view in VIEWS else DEFAULT_VIEW
    focus = _parse_anchor(anchor, today)
    with_tasks = _checkbox(filters, show_tasks, True)
    without_done = _checkbox(filters, hide_done, False)

    base = dict(
        view=view, anchor=focus, today=today, selected_collection=collection,
        show_tasks=with_tasks, hide_done=without_done,
        week_starts_sunday=week_starts_sunday,
        prev_anchor=_shift(view, focus, -1).isoformat(),
        next_anchor=_shift(view, focus, 1).isoformat(),
        today_anchor=today.isoformat(),
        day_start=DAY_START_HOUR, day_end=DAY_END_HOUR,
        # An all-day event's stored end is the day AFTER it finishes, so the
        # editor has to subtract one to show the date a person would say.
        one_day=dt.timedelta(days=1),
        hours=list(range(DAY_START_HOUR, DAY_END_HOUR + 1)),
    )

    if client is None:
        return deps.render(request, db, "calendar.html", configured=False,
                           collections=[], task_collections=[], grid=[],
                           weeks=[], side_days=[], side_due_count=0, **base)

    first, last = _range_for(view, focus, week_starts_sunday)
    errors: list[str] = []

    try:
        every = client.list_collections()
    except CalDAVError as exc:
        return deps.render(request, db, "calendar.html", configured=True,
                           collections=[], task_collections=[], grid=[],
                           weeks=[], side_days=[], side_due_count=0, errors=[str(exc)], **base)

    calendars = [c for c in every if c.kind == CollectionKind.CALENDAR]
    task_lists = [c for c in every if c.kind == CollectionKind.TASKS]
    chosen = [c for c in calendars if not collection or c.collection_id == collection]

    by_day: dict[dt.date, list[dict]] = {}

    for info in chosen:
        try:
            # Only the window being drawn, filtered by the server. Reading the
            # whole collection to show one month of it is what made this page
            # take seconds on a calendar with a few thousand events in it.
            records = client.list_events_in_range(info.collection_id, first, last)
        except CalDAVError as exc:
            errors.append(f"{info.display_name}: {exc}")
            continue
        for record in records:
            for on, offset, span in _placements(record, first, last, "event"):
                by_day.setdefault(on, []).append(
                    _entry(record, info, "event", on, offset, span)
                )

    # Tasks are loaded for a window wide enough to cover both the view itself
    # and the side panel's lookahead, so the panel never has to re-fetch.
    task_first = min(first, focus)
    task_last = max(last, focus + dt.timedelta(days=LOOKAHEAD_DAYS))
    task_entries: list[dict] = []

    if with_tasks:
        for info in task_lists:
            try:
                # Completed tasks are fetched and then filtered here rather than
                # at the server, so the "hide completed" box can be answered
                # without a second round trip.
                records = client.list_records(
                    info.collection_id, CollectionKind.TASKS, include_completed=True
                )
            except CalDAVError as exc:
                errors.append(f"{info.display_name}: {exc}")
                continue
            for record in records:
                if record.due_date is None:
                    continue
                if without_done and record.status == ItemStatus.COMPLETED:
                    continue
                # A task with a start date earlier than its due date occupies
                # every day between the two, the same way an event does.
                for on, offset, span in _placements(
                    record, task_first, task_last, "task"
                ):
                    task_entries.append(
                        _entry(record, info, "task", on, offset, span)
                    )

    for item in task_entries:
        if first <= item["date"] <= last:
            by_day.setdefault(item["date"], []).append(item)

    for day in by_day:
        by_day[day].sort(key=_sort_key)

    # One flat list of days; the template arranges it into whichever shape the
    # chosen view needs. Keeping the shaping in one place means a day cell looks
    # identical whether it is drawn in a month grid or a week column.
    grid = []
    cursor = first
    while cursor <= last:
        entries = by_day.get(cursor, [])
        grid.append({
            "date": cursor,
            "entries": entries,
            "is_today": cursor == today,
            "in_focus": (cursor.month == focus.month) if view == "month" else True,
            "is_weekend": cursor.weekday() >= 5,
        })
        cursor += dt.timedelta(days=1)

    # Month is drawn row by row so each row can carry its week number.
    weeks = []
    if view == "month":
        for start in range(0, len(grid), 7):
            row = grid[start:start + 7]
            if not row:
                continue
            weeks.append({
                "days": row,
                "iso": _iso_week(row[0]["date"]),
                "anchor": row[0]["date"].isoformat(),
            })

    # The side panel: the focused day's tasks, then a lookahead. Day view shows
    # only its own day, because a day column that also lists next Tuesday is
    # answering a question nobody asked while looking at one day.
    side_days = []
    if with_tasks and view in ("day", "agenda"):
        span = 0 if view == "day" else LOOKAHEAD_DAYS
        for offset in range(span + 1):
            when = focus + dt.timedelta(days=offset)
            # One day in view shows everything that touches it, including a run
            # passing through on its way to a deadline later in the week --
            # otherwise the day column says a task is not your problem today
            # while the month grid, looking at the same day, says it is.
            #
            # The lookahead keeps to what is actually *due* on each day. A task
            # running for six weeks would otherwise appear on all eight days of
            # it and drown the days it is genuinely due.
            if view == "day":
                items = sorted(
                    (t for t in task_entries if t["date"] == when), key=_sort_key
                )
            else:
                items = sorted(
                    (t for t in task_entries
                     if t["date"] == when and t["span_end"]),
                    key=_sort_key,
                )
            side_days.append({
                "date": when,
                "is_today": when == today,
                "tasks": items,
                "label": ("Today" if when == today else
                          "Tomorrow" if when == today + dt.timedelta(days=1) else
                          when.strftime("%A %-d %b")),
            })

    # What the panel heading counts: tasks actually due in the window, not runs
    # merely passing through it, which would report the same task several times.
    side_due_count = sum(
        1 for day in side_days for t in day["tasks"] if t["span_end"]
    )

    if view == "agenda":
        grid = [d for d in grid if d["entries"]]

    heading = {
        "month": focus.strftime("%B %Y"),
        "week": f"{first.strftime('%-d %b')} – {last.strftime('%-d %b %Y')}",
        "day": focus.strftime("%A %-d %B %Y"),
        "agenda": f"From {focus.strftime('%-d %b %Y')}",
    }[view]

    return deps.render(
        request, db, "calendar.html",
        configured=True,
        collections=calendars,
        task_collections=task_lists,
        grid=grid,
        weeks=weeks,
        side_days=side_days,
        side_due_count=side_due_count,
        heading=heading,
        range_first=first,
        range_last=last,
        total=sum(len(d["entries"]) for d in grid),
        errors=errors,
        **base,
    )


# --- Creating and editing -----------------------------------------------------


def _event_from_form(
    db: Session, title: str, start_date: str, start_time: str, end_date: str,
    end_time: str, all_day: str, location: str, notes: str, uid: str | None = None,
) -> tuple[CanonicalRecord | None, str | None]:
    """Build a record from the event form, or explain what is missing."""
    title = title.strip()
    parsed_start = _parse_date(start_date)
    if not title:
        return None, "Give the event a title."
    if parsed_start is None:
        return None, "An event needs a start date."

    whole_day = all_day == "1"
    tz_name = settings_store.get_timezone(db)
    start_t = None if whole_day else _parse_time(start_time)
    end_t = None if whole_day else _parse_time(end_time)
    parsed_end = _parse_date(end_date) or parsed_start

    # Checked on the dates the person actually typed, before any adjustment.
    # The all-day correction below adds a day, which used to make an end one
    # day earlier than the start compare as equal and slip through.
    if parsed_end < parsed_start:
        return None, "The event cannot end before it starts."
    if (not whole_day and parsed_end == parsed_start
            and start_t is not None and end_t is not None and end_t < start_t):
        return None, "The event cannot finish before it begins."

    if whole_day:
        # An all-day VEVENT's DTEND is exclusive: a one-day event ends on the
        # following day. Getting this wrong shows it a day short in every
        # standards-compliant client.
        parsed_end = parsed_end + dt.timedelta(days=1)
    elif start_t is not None and end_t is None:
        end_t = (dt.datetime.combine(parsed_start, start_t) + dt.timedelta(hours=1)).time()

    now = dt.datetime.now(dt.timezone.utc)
    return CanonicalRecord(
        uid=uid or new_uid(),
        kind=CollectionKind.CALENDAR,
        title=title,
        notes=notes.strip() or None,
        location=location.strip() or None,
        start_date=parsed_start, start_time=start_t,
        start_tz=None if whole_day else tz_name,
        end_date=parsed_end, end_time=end_t,
        end_tz=None if whole_day else tz_name,
        all_day=whole_day,
        origin_service=ServiceKind.LOCAL,
        created_at=now, updated_at=now,
    ), None


def _back_to(view: str, anchor: str, collection: str,
             show_tasks: str, hide_done: str) -> str:
    """Return to the view the user was looking at, not a default one."""
    from urllib.parse import urlencode

    params = {"view": view or DEFAULT_VIEW}
    if anchor:
        params["anchor"] = anchor
    if collection:
        params["collection"] = collection
    # The marker travels too, so the filters survive the round trip exactly as
    # they were rather than reverting to the defaults.
    params["filters"] = "1"
    params["show_tasks"] = "0" if show_tasks == "0" else "1"
    params["hide_done"] = "1" if hide_done == "1" else "0"
    return f"/calendar?{urlencode(params)}"


@router.post("/create")
def create_event(
    request: Request,
    collection_id: str = Form(...),
    title: str = Form(...),
    start_date: str = Form(""),
    start_time: str = Form(""),
    end_date: str = Form(""),
    end_time: str = Form(""),
    all_day: str = Form(""),
    location: str = Form(""),
    notes: str = Form(""),
    view: str = Form(DEFAULT_VIEW),
    anchor: str = Form(""),
    collection: str = Form(""),
    show_tasks: str = Form("1"),
    hide_done: str = Form("0"),
    db: Session = Depends(get_db),
):
    back = _back_to(view, anchor, collection, show_tasks, hide_done)
    client = get_radicale_client(db)
    if client is None:
        deps.flash(request, "Radicale is not configured yet.", "error")
        return deps.redirect(back)

    record, problem = _event_from_form(
        db, title, start_date, start_time, end_date, end_time, all_day, location, notes
    )
    if problem:
        deps.flash(request, problem, "error")
        return deps.redirect(back)

    try:
        client.save_record(collection_id, record)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(back)

    deps.flash(request, f"Added {record.title!r}.", "success")
    return deps.redirect(back)


@router.post("/task/create")
def create_task_here(
    request: Request,
    collection_id: str = Form(...),
    title: str = Form(...),
    due_date: str = Form(""),
    due_time: str = Form(""),
    notes: str = Form(""),
    view: str = Form(DEFAULT_VIEW),
    anchor: str = Form(""),
    collection: str = Form(""),
    show_tasks: str = Form("1"),
    hide_done: str = Form("0"),
    db: Session = Depends(get_db),
):
    """Quick-add a task from the calendar's task panel."""
    back = _back_to(view, anchor, collection, show_tasks, hide_done)
    client = get_radicale_client(db)
    if client is None:
        deps.flash(request, "Radicale is not configured yet.", "error")
        return deps.redirect(back)

    record, problem = _task_from_form(db, title, due_date, due_time, notes, "0")
    if problem:
        deps.flash(request, problem, "error")
        return deps.redirect(back)

    try:
        client.save_record(collection_id, record)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(back)

    deps.flash(request, f"Added {record.title!r}.", "success")
    return deps.redirect(back)


def _task_from_form(
    db: Session, title: str, due_date: str, due_time: str, notes: str,
    priority: str, uid: str | None = None,
) -> tuple[CanonicalRecord | None, str | None]:
    title = title.strip()
    if not title:
        return None, "Give the task a title."

    parsed_date = _parse_date(due_date)
    parsed_time = _parse_time(due_time)
    if parsed_time is not None and parsed_date is None:
        return None, "A time needs a date to go with it."

    try:
        priority_value = max(0, min(9, int(priority)))
    except ValueError:
        priority_value = 0

    now = dt.datetime.now(dt.timezone.utc)
    return CanonicalRecord(
        uid=uid or new_uid(),
        kind=CollectionKind.TASKS,
        title=title,
        notes=notes.strip() or None,
        status=ItemStatus.NEEDS_ACTION,
        due_date=parsed_date,
        due_time=parsed_time,
        # Only attach a timezone when there is a time for it to qualify, so a
        # date-only due date means the same calendar day everywhere.
        due_tz=settings_store.get_timezone(db) if parsed_time else None,
        priority=priority_value,
        origin_service=ServiceKind.LOCAL,
        created_at=now, updated_at=now,
    ), None


@router.post("/task/{collection_id}/{uid}/update")
def update_task_here(
    request: Request,
    collection_id: str,
    uid: str,
    title: str = Form(...),
    due_date: str = Form(""),
    due_time: str = Form(""),
    priority: str = Form("0"),
    notes: str = Form(""),
    view: str = Form(DEFAULT_VIEW),
    anchor: str = Form(""),
    collection: str = Form(""),
    show_tasks: str = Form("1"),
    hide_done: str = Form("0"),
    db: Session = Depends(get_db),
):
    back = _back_to(view, anchor, collection, show_tasks, hide_done)
    client = get_radicale_client(db)
    if client is None:
        return deps.redirect(back)

    try:
        existing = client.get_record(collection_id, uid, CollectionKind.TASKS)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(back)
    if existing is None:
        deps.flash(request, "That task no longer exists.", "error")
        return deps.redirect(back)

    record, problem = _task_from_form(db, title, due_date, due_time, notes,
                                      priority, uid=uid)
    if problem:
        deps.flash(request, problem, "error")
        return deps.redirect(back)

    # Keep what the quick editor does not offer, so renaming a task cannot
    # quietly discard its tags, its completion or where it came from.
    record.status = existing.status
    record.completed_at = existing.completed_at
    record.tags = list(existing.tags or [])
    record.rrule = existing.rrule
    record.origin_service = existing.origin_service
    record.created_at = existing.created_at

    try:
        client.save_record(collection_id, record)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(back)

    deps.flash(request, "Task updated.", "success")
    return deps.redirect(back)


@router.post("/task/{collection_id}/{uid}/delete")
def delete_task_here(
    request: Request,
    collection_id: str,
    uid: str,
    view: str = Form(DEFAULT_VIEW),
    anchor: str = Form(""),
    collection: str = Form(""),
    show_tasks: str = Form("1"),
    hide_done: str = Form("0"),
    db: Session = Depends(get_db),
):
    back = _back_to(view, anchor, collection, show_tasks, hide_done)
    client = get_radicale_client(db)
    if client is None:
        return deps.redirect(back)
    try:
        client.delete_record(collection_id, uid, CollectionKind.TASKS)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(back)
    deps.flash(request, "Task deleted.", "success")
    return deps.redirect(back)


@router.post("/{collection_id}/{uid}/update")
def update_event(
    request: Request,
    collection_id: str,
    uid: str,
    title: str = Form(...),
    start_date: str = Form(""),
    start_time: str = Form(""),
    end_date: str = Form(""),
    end_time: str = Form(""),
    all_day: str = Form(""),
    location: str = Form(""),
    notes: str = Form(""),
    view: str = Form(DEFAULT_VIEW),
    anchor: str = Form(""),
    collection: str = Form(""),
    show_tasks: str = Form("1"),
    hide_done: str = Form("0"),
    db: Session = Depends(get_db),
):
    back = _back_to(view, anchor, collection, show_tasks, hide_done)
    client = get_radicale_client(db)
    if client is None:
        return deps.redirect(back)

    try:
        existing = client.get_record(collection_id, uid, CollectionKind.CALENDAR)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(back)
    if existing is None:
        deps.flash(request, "That event no longer exists.", "error")
        return deps.redirect(back)

    record, problem = _event_from_form(
        db, title, start_date, start_time, end_date, end_time, all_day,
        location, notes, uid=uid,
    )
    if problem:
        deps.flash(request, problem, "error")
        return deps.redirect(back)

    # Carry across what the form does not offer, so editing a title cannot
    # quietly discard a recurrence rule set in another client.
    record.rrule = existing.rrule
    record.tags = list(existing.tags or [])
    record.status = existing.status
    record.origin_service = existing.origin_service
    record.created_at = existing.created_at

    try:
        client.save_record(collection_id, record)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(back)

    deps.flash(request, "Event updated.", "success")
    return deps.redirect(back)


@router.post("/{collection_id}/{uid}/delete")
def delete_event(
    request: Request,
    collection_id: str,
    uid: str,
    view: str = Form(DEFAULT_VIEW),
    anchor: str = Form(""),
    collection: str = Form(""),
    show_tasks: str = Form("1"),
    hide_done: str = Form("0"),
    db: Session = Depends(get_db),
):
    back = _back_to(view, anchor, collection, show_tasks, hide_done)
    client = get_radicale_client(db)
    if client is None:
        return deps.redirect(back)
    try:
        client.delete_record(collection_id, uid, CollectionKind.CALENDAR)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(back)
    deps.flash(request, "Event deleted.", "success")
    return deps.redirect(back)


@router.post("/task/{collection_id}/{uid}/toggle")
def toggle_task(collection_id: str, uid: str, db: Session = Depends(get_db)):
    """Tick a task off from the calendar, without leaving the page."""
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
        record.status, record.completed_at = ItemStatus.NEEDS_ACTION, None
    else:
        record.status, record.completed_at = ItemStatus.COMPLETED, now
    record.updated_at = now
    try:
        client.save_record(collection_id, record)
    except CalDAVError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"uid": uid, "completed": record.status == ItemStatus.COMPLETED})
