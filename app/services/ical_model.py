"""Conversion between iCalendar components and Task Hub's canonical record.

Radicale stores plain ``.ics`` files, and external clients (Apple Calendar,
DAVx5, Thunderbird) read and write those same files. So the iCalendar form is
not an export format -- it is a shared, concurrently-edited representation, and
anything this module fails to preserve is data a third-party client can destroy.

The critical detail is the DUE property. iCalendar distinguishes a ``DATE``
value from a ``DATE-TIME`` value, which is exactly the "date only" versus "date
and time" distinction Task Hub needs in order to protect a time of day from
services like Google Tasks that cannot represent one. That distinction is
carried through faithfully in both directions.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar, Event, Todo

from app.db.models import CollectionKind, ItemStatus, ServiceKind

#: Custom property recording where an item was first created. Kept on the
#: component itself so the coloured source badge survives a full round trip
#: through Radicale, and even through a third-party CalDAV client that knows
#: nothing about Task Hub. X- properties are the sanctioned extension point and
#: clients are required to preserve them.
X_ORIGIN = "X-TASKHUB-ORIGIN"
X_ORIGIN_NAME = "X-TASKHUB-ORIGIN-NAME"

PRODID = "-//Task Hub//Task Hub Sync//EN"
ICAL_VERSION = "2.0"


def _tz(name: str | None) -> dt.tzinfo:
    """Resolve a timezone name, falling back to UTC when it is unknown.

    Timezone databases differ between the machine that wrote an event and the
    one reading it. An unresolvable zone should degrade to UTC, not abort the
    parse and hide the whole item from the user.
    """
    if not name:
        return dt.timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def _tzname_of(value: dt.datetime) -> str | None:
    """Best-effort IANA name for a datetime's tzinfo."""
    tzinfo = value.tzinfo
    if tzinfo is None:
        return None
    key = getattr(tzinfo, "key", None)  # ZoneInfo
    if key:
        return str(key)
    if tzinfo is dt.timezone.utc or value.utcoffset() == dt.timedelta(0):
        return "UTC"
    return getattr(tzinfo, "zone", None)  # pytz-style, used by some clients


@dataclass
class CanonicalRecord:
    """A task or event in Task Hub's own terms.

    Mirrors the columns of :class:`app.db.models.Item`, including the split
    date/time/timezone components. Used as the common currency between the
    iCalendar layer, the sync connectors and the web UI so that each of those
    only has to know how to convert to and from this one shape.
    """

    uid: str
    kind: CollectionKind = CollectionKind.TASKS
    title: str = ""
    notes: str | None = None
    status: ItemStatus = ItemStatus.NEEDS_ACTION
    completed_at: dt.datetime | None = None

    due_date: dt.date | None = None
    due_time: dt.time | None = None
    due_tz: str | None = None

    start_date: dt.date | None = None
    start_time: dt.time | None = None
    start_tz: str | None = None
    end_date: dt.date | None = None
    end_time: dt.time | None = None
    end_tz: str | None = None
    all_day: bool = False

    location: str | None = None
    priority: int = 0
    rrule: str | None = None
    tags: list[str] = field(default_factory=list)

    origin_service: ServiceKind = ServiceKind.LOCAL
    origin_name: str | None = None

    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None

    #: CalDAV href and etag, populated when the record came from a server.
    href: str | None = None
    etag: str | None = None

    # -- Convenience -----------------------------------------------------------

    @property
    def is_completed(self) -> bool:
        return self.status == ItemStatus.COMPLETED

    @property
    def has_due_time(self) -> bool:
        """Whether a specific time of day is set, as opposed to a bare date.

        This is the property the merge engine protects: a service that cannot
        express a time must never be allowed to turn this from True to False.
        """
        return self.due_time is not None

    def due_datetime(self, fallback_tz: str = "UTC") -> dt.datetime | None:
        """Due date and time combined into an aware datetime, if a date is set.

        A date without a time is treated as the end of that day, which is what
        "due Tuesday" means to a person deciding whether something is overdue.
        """
        if self.due_date is None:
            return None
        tzinfo = _tz(self.due_tz or fallback_tz)
        if self.due_time is None:
            return dt.datetime.combine(self.due_date, dt.time(23, 59, 59), tzinfo)
        return dt.datetime.combine(self.due_date, self.due_time, tzinfo)

    def is_overdue(self, now: dt.datetime | None = None, fallback_tz: str = "UTC") -> bool:
        if self.is_completed:
            return False
        due = self.due_datetime(fallback_tz)
        if due is None:
            return False
        return due < (now or dt.datetime.now(dt.timezone.utc))


def new_uid() -> str:
    """Generate a UID that is valid in iCalendar and readable in a filename."""
    return f"{uuid.uuid4()}@taskhub"


# --- Parsing: iCalendar -> CanonicalRecord ------------------------------------


def _split_datetime(value) -> tuple[dt.date | None, dt.time | None, str | None]:
    """Decompose an icalendar DATE or DATE-TIME into (date, time, tzname).

    Returning ``time=None`` for a DATE value is the whole point: it is how "no
    time of day was ever specified" is represented, distinct from midnight.
    """
    if value is None:
        return None, None, None
    raw = getattr(value, "dt", value)

    if isinstance(raw, dt.datetime):
        tzname = _tzname_of(raw)
        if raw.tzinfo is None:
            # A floating time: means "this wall-clock time, wherever you are".
            # Recorded with no zone so the viewer applies the user's timezone.
            return raw.date(), raw.time(), None
        return raw.date(), raw.time(), tzname
    if isinstance(raw, dt.date):
        return raw, None, None
    return None, None, None


def _as_utc(value) -> dt.datetime | None:
    if value is None:
        return None
    raw = getattr(value, "dt", value)
    if isinstance(raw, dt.datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=dt.timezone.utc)
        return raw.astimezone(dt.timezone.utc)
    if isinstance(raw, dt.date):
        return dt.datetime.combine(raw, dt.time.min, dt.timezone.utc)
    return None


def _text(component, name: str) -> str | None:
    value = component.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_status(component, kind: CollectionKind) -> ItemStatus:
    raw = (_text(component, "STATUS") or "").upper().replace("_", "-")
    mapping = {
        "COMPLETED": ItemStatus.COMPLETED,
        "NEEDS-ACTION": ItemStatus.NEEDS_ACTION,
        "IN-PROCESS": ItemStatus.IN_PROCESS,
        "CANCELLED": ItemStatus.CANCELLED,
    }
    if raw in mapping:
        return mapping[raw]
    if kind == CollectionKind.TASKS:
        # Some clients mark completion only with PERCENT-COMPLETE or by setting
        # a COMPLETED timestamp, without ever writing STATUS.
        try:
            if int(component.get("PERCENT-COMPLETE", 0)) >= 100:
                return ItemStatus.COMPLETED
        except (TypeError, ValueError):
            pass
        if component.get("COMPLETED") is not None:
            return ItemStatus.COMPLETED
    return ItemStatus.NEEDS_ACTION


def _parse_tags(component) -> list[str]:
    raw = component.get("CATEGORIES")
    if raw is None:
        return []
    tags: list[str] = []
    # CATEGORIES may appear more than once, and each may hold a list.
    for entry in raw if isinstance(raw, list) else [raw]:
        cats = getattr(entry, "cats", None)
        if cats:
            tags.extend(str(c).strip() for c in cats)
        else:
            tags.extend(part.strip() for part in str(entry).split(","))
    return [t for t in tags if t]


def component_to_record(component, kind: CollectionKind) -> CanonicalRecord:
    """Convert a VTODO or VEVENT component into a canonical record."""
    uid = _text(component, "UID") or new_uid()

    origin_raw = (_text(component, X_ORIGIN) or "").lower()
    try:
        origin = ServiceKind(origin_raw)
    except ValueError:
        # No origin marker at all means some other CalDAV client wrote this --
        # a phone, Thunderbird, Apple Calendar -- because everything Task Hub
        # writes is stamped. An unrecognised marker means a service we no longer
        # know about. Both are third party, and neither may be mistaken for an
        # item created here: the badge is meant to tell the user where a task
        # came from, so guessing "local" would be worse than saying "3rd party".
        origin = ServiceKind.RADICALE

    record = CanonicalRecord(
        uid=uid,
        kind=kind,
        title=_text(component, "SUMMARY") or "",
        notes=_text(component, "DESCRIPTION"),
        status=_parse_status(component, kind),
        completed_at=_as_utc(component.get("COMPLETED")),
        location=_text(component, "LOCATION"),
        tags=_parse_tags(component),
        origin_service=origin,
        origin_name=_text(component, X_ORIGIN_NAME),
        created_at=_as_utc(component.get("CREATED")),
        updated_at=_as_utc(component.get("LAST-MODIFIED")) or _as_utc(component.get("DTSTAMP")),
    )

    try:
        record.priority = int(component.get("PRIORITY", 0) or 0)
    except (TypeError, ValueError):
        record.priority = 0

    rrule = component.get("RRULE")
    if rrule is not None:
        record.rrule = rrule.to_ical().decode("utf-8") if hasattr(rrule, "to_ical") else str(rrule)

    record.due_date, record.due_time, record.due_tz = _split_datetime(component.get("DUE"))
    record.start_date, record.start_time, record.start_tz = _split_datetime(component.get("DTSTART"))
    record.end_date, record.end_time, record.end_tz = _split_datetime(component.get("DTEND"))

    # A VEVENT with DATE-valued endpoints is an all-day event.
    if kind == CollectionKind.CALENDAR:
        record.all_day = record.start_date is not None and record.start_time is None

    return record


def parse_calendar(data: str | bytes) -> list[tuple[CanonicalRecord, str]]:
    """Parse an ``.ics`` payload into (record, component_name) pairs.

    A single CalDAV resource can hold several components -- a recurring item
    plus its overrides, for instance -- so this returns a list rather than
    assuming one.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    results: list[tuple[CanonicalRecord, str]] = []
    calendar = Calendar.from_ical(data)
    for component in calendar.walk():
        name = component.name
        if name == "VTODO":
            results.append((component_to_record(component, CollectionKind.TASKS), name))
        elif name == "VEVENT":
            results.append((component_to_record(component, CollectionKind.CALENDAR), name))
    return results


def parse_single(data: str | bytes, kind: CollectionKind) -> CanonicalRecord | None:
    """Parse the first matching component from an ``.ics`` payload."""
    wanted = "VTODO" if kind == CollectionKind.TASKS else "VEVENT"
    for record, name in parse_calendar(data):
        if name == wanted:
            return record
    return None


# --- Serialising: CanonicalRecord -> iCalendar --------------------------------


def _apply_datetime(component, prop: str, date_part, time_part, tzname) -> None:
    """Write a DATE, a floating DATE-TIME or a zoned DATE-TIME, or remove it.

    All three forms are real and distinct in iCalendar, and collapsing them
    loses information every time:

    * no time at all -- ``DUE;VALUE=DATE:20260905``
    * a floating time -- ``DUE:20260905T100000``, meaning that clock time
      wherever the reader happens to be
    * a zoned time -- ``DUE;TZID=America/Denver:20260905T100000``

    A floating time used to be stamped as UTC, because the timezone helper falls
    back to UTC for an unknown name. That pinned it: a task set for 10am with no
    zone became 10:00 UTC, and every client then rendered it in local time --
    4am in Denver. Todoist reports exactly this shape (``"timezone": null``), so
    it happened to any timed task created there.
    """
    component.pop(prop, None)
    if date_part is None:
        return
    if time_part is None:
        # DATE value: preserves "no time of day was specified".
        component.add(prop, date_part)
    elif tzname is None:
        # Floating: a naive datetime serialises with neither TZID nor Z, which
        # is the iCalendar way of saying "this clock time, wherever you are".
        component.add(prop, dt.datetime.combine(date_part, time_part))
    else:
        component.add(prop, dt.datetime.combine(date_part, time_part, _tz(tzname)))


def record_to_component(record: CanonicalRecord):
    """Build a VTODO or VEVENT component from a canonical record."""
    component = Todo() if record.kind == CollectionKind.TASKS else Event()
    now = dt.datetime.now(dt.timezone.utc)

    component.add("UID", record.uid)
    component.add("DTSTAMP", now)
    component.add("SUMMARY", record.title or "")
    component.add("CREATED", record.created_at or now)
    component.add("LAST-MODIFIED", record.updated_at or now)

    if record.notes:
        component.add("DESCRIPTION", record.notes)
    if record.location:
        component.add("LOCATION", record.location)
    if record.priority:
        component.add("PRIORITY", int(record.priority))
    if record.tags:
        component.add("CATEGORIES", record.tags)
    if record.rrule:
        component.add("RRULE", _parse_rrule(record.rrule))

    _apply_datetime(component, "DUE", record.due_date, record.due_time, record.due_tz)
    _apply_datetime(component, "DTSTART", record.start_date, record.start_time, record.start_tz)
    _apply_datetime(component, "DTEND", record.end_date, record.end_time, record.end_tz)

    if record.kind == CollectionKind.TASKS:
        component.add("STATUS", record.status.value.upper())
        if record.status == ItemStatus.COMPLETED:
            component.add("COMPLETED", record.completed_at or now)
            component.add("PERCENT-COMPLETE", 100)
        else:
            component.add("PERCENT-COMPLETE", 0)
    elif record.status == ItemStatus.CANCELLED:
        component.add("STATUS", "CANCELLED")

    component.add(X_ORIGIN, record.origin_service.value)
    if record.origin_name:
        component.add(X_ORIGIN_NAME, record.origin_name)

    return component


def _parse_rrule(rrule: str):
    """Turn a stored RRULE string back into the structure icalendar expects."""
    from icalendar.prop import vRecur

    cleaned = rrule.strip()
    if cleaned.upper().startswith("RRULE:"):
        cleaned = cleaned[6:]
    try:
        return vRecur.from_ical(cleaned)
    except (ValueError, TypeError):
        return None


def record_to_ics(record: CanonicalRecord) -> str:
    """Serialise a record as a complete, standalone ``.ics`` document."""
    calendar = Calendar()
    calendar.add("PRODID", PRODID)
    calendar.add("VERSION", ICAL_VERSION)
    calendar.add("CALSCALE", "GREGORIAN")
    calendar.add_component(record_to_component(record))
    return calendar.to_ical().decode("utf-8")
