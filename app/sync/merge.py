"""Capability-aware field merging.

This is the heart of Task Hub. Everything else moves data around; this decides
what the truth is when several services disagree.

Two ideas do the work.

**Absent is not empty.** A connector reports only the fields its service can
faithfully represent. Google Tasks cannot store a time of day -- it accepts a
timestamp and discards the time -- so its connector never reports ``due_time``
at all. Because the merge only ever considers reported fields, a date edit made
in Google updates the date and leaves the time untouched. No special case for
Google is needed anywhere: the capability declaration does it.

**Conflicts resolve per field, not per record.** If a note is edited in Todoist
and a due date in Google between two syncs, both survive. Whole-record
last-writer-wins would silently discard one of them, and the user would have no
way to tell it had happened.

**An echo is not an edit.** Services report a single modification time for a
whole item, not one per field. So when a user changes a date in Google, Google
stamps the entire item as freshly modified -- including the note, which it never
touched and whose value is whatever Task Hub last sent it. Trusting that
timestamp would let Google's stale note overwrite a newer edit made in Todoist.

The fix is to remember what was last pushed to each service. If an incoming
value matches what we sent, that service is echoing us back and did not change
the field, so it is skipped before timestamps are considered at all. Only values
that differ from the baseline count as real edits. This makes the merge robust
against coarse item-level timestamps, which every service has.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.connectors.base import (
    ALL_FIELDS,
    F_DUE_DATE,
    F_DUE_TIME,
    F_END,
    F_LOCATION,
    F_NOTES,
    F_PRIORITY,
    F_RRULE,
    F_START,
    F_STATUS,
    F_TAGS,
    F_TITLE,
    Capabilities,
    RemoteItem,
)
from app.db.models import ItemStatus, ServiceKind
from app.services.ical_model import CanonicalRecord


def _as_aware(value: dt.datetime | None) -> dt.datetime | None:
    """Treat a naive datetime as UTC.

    A second line of defence behind the UTCDateTime column type: a connector
    could still hand back a naive timestamp parsed from an API response, and a
    single naive value is enough to abort a whole sync group with a comparison
    error.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


@dataclass
class Provenance:
    """When a field last changed, and which service changed it."""

    service: ServiceKind
    changed_at: dt.datetime


@dataclass
class FieldChange:
    """One field actually changing value, and which service caused it.

    Recorded rather than merely applied, because the sync history page has to
    be able to answer "why is my task different?" long after the fact. Knowing
    that a due date changed is not much use on its own; knowing it changed
    because Todoist said so is what makes the record worth keeping.
    """

    field: str
    #: What Task Hub held before this merge.
    old: Any
    #: What it holds afterwards.
    new: Any
    #: The service whose copy won this field.
    source: ServiceKind


@dataclass
class MergeResult:
    """What one merge decided: what changed, and what was refused.

    Both halves matter. The changes drive what gets written out to the other
    services; the conflicts are the fields where an incoming value lost, which
    is normal and expected rather than an error -- every service echoes back
    stale copies of fields it was told about earlier, and refusing them is the
    entire point of the exercise.
    """

    changes: list[FieldChange] = field(default_factory=list)
    #: Fields where an incoming value was rejected as older than what we hold.
    conflicts: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Whether anything at all was accepted.

        Used to decide whether this item needs writing out again. A merge that
        changed nothing must not trigger a push, or every sync would rewrite
        every item for ever and no system would ever settle.
        """
        return bool(self.changes)


# --- Field access -------------------------------------------------------------
#
# Several logical fields span more than one column. "start" is a date, a time
# and a timezone together, because splitting them across separate merge
# decisions could produce a start date from one service and a start time from
# another -- a combination neither service ever had.


def get_field(record: CanonicalRecord, name: str) -> Any:
    """Read one logical field, as a single value the merge can compare.

    The returned shape matters as much as the value. Fields that span several
    database columns come back as a tuple, so that comparing "the start" is one
    decision rather than three independent ones. Empty values are normalised
    here too -- an empty title reads as ``""`` and an empty note as ``None`` --
    so that the merge never has to decide whether ``None`` and ``""`` mean the
    same thing while it is also deciding which service is newer.

    Raises ``KeyError`` for an unknown field rather than returning ``None``: a
    typo in a field name would otherwise look like a permanently empty value
    and silently stop that field ever syncing.
    """
    if name == F_TITLE:
        return record.title or ""
    if name == F_NOTES:
        return record.notes or None
    if name == F_STATUS:
        return record.status
    if name == F_DUE_DATE:
        return record.due_date
    if name == F_DUE_TIME:
        return (record.due_time, record.due_tz)
    if name == F_START:
        return (record.start_date, record.start_time, record.start_tz)
    if name == F_END:
        return (record.end_date, record.end_time, record.end_tz)
    if name == F_PRIORITY:
        return record.priority or 0
    if name == F_TAGS:
        return tuple(sorted(record.tags or []))
    if name == F_LOCATION:
        return record.location or None
    if name == F_RRULE:
        return record.rrule or None
    raise KeyError(f"Unknown field {name!r}")


def set_field(record: CanonicalRecord, name: str, value: Any) -> None:
    """Write one logical field back, undoing what :func:`get_field` packed up.

    The inverse of :func:`get_field`, and deliberately its mirror image: a
    field that reads as a tuple is written back as a tuple, so a merged start
    date and start time are stored together or not at all. Keeping the two
    functions symmetrical is what stops a half-applied value -- a date from one
    service paired with a time from another -- reaching the database.
    """
    if name == F_TITLE:
        record.title = value or ""
    elif name == F_NOTES:
        record.notes = value or None
    elif name == F_STATUS:
        record.status = value
    elif name == F_DUE_DATE:
        record.due_date = value
        if value is None:
            # A time of day with no date is meaningless, and would be
            # unrepresentable in iCalendar. Clearing the date clears both.
            record.due_time = None
            record.due_tz = None
    elif name == F_DUE_TIME:
        record.due_time, record.due_tz = value
    elif name == F_START:
        record.start_date, record.start_time, record.start_tz = value
    elif name == F_END:
        record.end_date, record.end_time, record.end_tz = value
    elif name == F_PRIORITY:
        record.priority = int(value or 0)
    elif name == F_TAGS:
        record.tags = list(value or [])
    elif name == F_LOCATION:
        record.location = value or None
    elif name == F_RRULE:
        record.rrule = value or None
    else:
        raise KeyError(f"Unknown field {name!r}")


def _equivalent(name: str, left: Any, right: Any) -> bool:
    """Compare two values for a field, ignoring differences that do not matter.

    Services round-trip text with small cosmetic differences -- trailing
    whitespace, empty string versus null. Treating those as real changes would
    make every sync pass rewrite every item forever.
    """
    if name in (F_TITLE, F_NOTES, F_LOCATION):
        return (left or "").strip() == (right or "").strip()
    if name == F_TAGS:
        return tuple(sorted(left or ())) == tuple(sorted(right or ()))
    if name == F_RRULE:
        return (left or "").strip().upper() == (right or "").strip().upper()
    return left == right


def field_fingerprint(record: CanonicalRecord, name: str) -> str:
    """A stable hash of one field's value, for baseline comparison.

    Hashes rather than raw values so the stored baseline stays compact and needs
    no date/time deserialisation when it is read back.
    """
    value = get_field(record, name)
    if isinstance(value, tuple):
        payload = [_json_safe(v) for v in value]
    else:
        payload = _json_safe(value)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def baseline_fingerprints(record: CanonicalRecord, caps: Capabilities) -> dict[str, str]:
    """Fingerprint every field a service can see, to store against its link."""
    projected = project(record, caps)
    return {
        name: field_fingerprint(projected, name)
        for name in sorted(caps.present_fields())
    }


# --- The merge ----------------------------------------------------------------


def merge_remote(
    canonical: CanonicalRecord,
    remote: RemoteItem,
    source: ServiceKind,
    provenance: dict[str, Provenance],
    now: dt.datetime | None = None,
    baseline: dict[str, str] | None = None,
) -> MergeResult:
    """Fold one service's view of an item into the canonical record.

    ``canonical`` is modified in place. ``provenance`` maps field name to when
    that field last changed and which service changed it; it is updated for
    every field this call accepts.

    ``baseline`` holds the field fingerprints Task Hub last pushed to this
    account. Any incoming value matching its baseline is an echo of our own
    write, not an edit by this service, and is ignored. Passing None means "no
    record of what we sent", which happens on the very first sync of an item;
    the merge then falls back to timestamps alone.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    result = MergeResult()

    # Undated remotes are treated as "changed just now". Some services report no
    # modification time at all, and refusing their edits entirely would be worse
    # than occasionally preferring a slightly stale one.
    remote_time = _as_aware(remote.remote_updated_at) or now

    for name in sorted(remote.fields_present & ALL_FIELDS):
        # Status carries completion semantics that plain comparison would get
        # wrong, so it is handled separately below.
        if name == F_STATUS:
            continue

        incoming = get_field(remote.record, name)
        current = get_field(canonical, name)

        if _equivalent(name, current, incoming):
            continue

        if baseline is not None and baseline.get(name) == field_fingerprint(
            remote.record, name
        ):
            # This service is reporting back exactly what we last wrote to it.
            # It did not change this field, so its item-level timestamp says
            # nothing about it -- and acting on that timestamp is what would let
            # a stale copy overwrite a newer edit made somewhere else.
            continue

        known = provenance.get(name)
        if known is not None and known.service != source and _as_aware(known.changed_at) > remote_time:
            # Another service changed this field more recently. Keep ours and
            # record the disagreement so it can be shown in the sync log rather
            # than vanishing.
            result.conflicts.append(name)
            continue

        set_field(canonical, name, incoming)
        result.changes.append(FieldChange(name, current, incoming, source))
        provenance[name] = Provenance(service=source, changed_at=remote_time)

    if F_STATUS in remote.fields_present:
        echoed_status = baseline is not None and baseline.get(
            F_STATUS
        ) == field_fingerprint(remote.record, F_STATUS)
        if not echoed_status:
            _merge_status(canonical, remote, source, provenance, result, remote_time, now)

    return result


def _merge_status(
    canonical: CanonicalRecord,
    remote: RemoteItem,
    source: ServiceKind,
    provenance: dict[str, Provenance],
    result: MergeResult,
    remote_time: dt.datetime,
    now: dt.datetime,
) -> None:
    """Reconcile completion, which is deliberately asymmetric.

    Completing something anywhere propagates immediately. Un-completing requires
    a strictly newer timestamp than the completion it is undoing.

    The asymmetry is on purpose. Ticking a task off is an explicit act; a
    service reporting "not completed" is usually just a stale view that has not
    caught up yet. Treating both directions equally causes the classic
    resurrection bug, where a task ticked off in one app is un-ticked on the
    next pass by a service that had not yet heard about it.
    """
    incoming = remote.record.status
    current = canonical.status

    if incoming == current:
        return

    incoming_done = incoming == ItemStatus.COMPLETED
    current_done = current == ItemStatus.COMPLETED

    if incoming_done and not current_done:
        canonical.status = ItemStatus.COMPLETED
        canonical.completed_at = remote.record.completed_at or remote_time or now
        result.changes.append(FieldChange(F_STATUS, current, incoming, source))
        provenance[F_STATUS] = Provenance(service=source, changed_at=remote_time)
        return

    if current_done and not incoming_done:
        known = provenance.get(F_STATUS)
        if known is not None and _as_aware(known.changed_at) >= remote_time:
            result.conflicts.append(F_STATUS)
            return
        canonical.status = incoming
        canonical.completed_at = None
        result.changes.append(FieldChange(F_STATUS, current, incoming, source))
        provenance[F_STATUS] = Provenance(service=source, changed_at=remote_time)
        return

    # Neither side is "completed" -- e.g. needs-action versus in-process.
    known = provenance.get(F_STATUS)
    if known is not None and known.service != source and _as_aware(known.changed_at) > remote_time:
        result.conflicts.append(F_STATUS)
        return
    canonical.status = incoming
    result.changes.append(FieldChange(F_STATUS, current, incoming, source))
    provenance[F_STATUS] = Provenance(service=source, changed_at=remote_time)


# --- Projection and hashing ---------------------------------------------------


def project(record: CanonicalRecord, caps: Capabilities) -> CanonicalRecord:
    """The version of a record a given service is able to hold.

    Used both for writing and for deciding whether a write is needed at all.
    Fields the service cannot represent are blanked here so that comparing two
    projections never reports a difference the service could not have caused.
    """
    from copy import deepcopy

    projected = deepcopy(record)

    if not caps.supports(F_NOTES):
        projected.notes = None
    if not caps.supports(F_PRIORITY):
        projected.priority = 0
    if not caps.supports(F_TAGS):
        projected.tags = []
    if not caps.supports(F_LOCATION):
        projected.location = None
    if not caps.supports(F_RRULE):
        projected.rrule = None
    if not caps.supports(F_DUE_DATE):
        projected.due_date = None
        projected.due_time = None
        projected.due_tz = None
    elif not caps.supports(F_DUE_TIME):
        # The service keeps the date but cannot keep the time. Dropping the time
        # here is what makes the round trip stable: without it, every pull would
        # look like the service had deleted the time, and every push would try
        # to send it again.
        projected.due_time = None
        projected.due_tz = None
    if not caps.supports(F_START):
        projected.start_date = projected.start_time = projected.start_tz = None
    if not caps.supports(F_END):
        projected.end_date = projected.end_time = projected.end_tz = None

    if caps.max_title_length and len(projected.title) > caps.max_title_length:
        projected.title = projected.title[: caps.max_title_length]
    if caps.max_notes_length and projected.notes and len(projected.notes) > caps.max_notes_length:
        projected.notes = projected.notes[: caps.max_notes_length]

    return projected


def content_hash(
    record: CanonicalRecord, caps: Capabilities, only: frozenset[str] | None = None
) -> str:
    """Fingerprint of everything a given service can see about an item.

    Restricted to the service's own capabilities on purpose. If the hash covered
    fields the service cannot store, then editing a note in Todoist would change
    the Google hash and force a pointless rewrite to Google on every pass -- and
    pointless writes are what burn through rate limits and cause the endless
    rewriting of completed tasks.
    """
    projected = project(record, caps)
    payload: dict[str, Any] = {}

    for name in sorted(only if only is not None else caps.present_fields()):
        value = get_field(projected, name)
        if isinstance(value, tuple):
            payload[name] = [_json_safe(v) for v in value]
        else:
            payload[name] = _json_safe(value)

    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    """Convert a field value into something JSON can store.

    Provenance is written to the database as JSON, and dates, times and status
    enumerations are not JSON types. Converting them to strings here keeps that
    knowledge in one place rather than at every point that saves provenance.

    Anything else is returned untouched, on the assumption that the remaining
    field types -- strings, numbers, lists of tags -- are already storable.
    """
    if isinstance(value, (dt.date, dt.time, dt.datetime)):
        return value.isoformat()
    if isinstance(value, ItemStatus):
        return value.value
    return value
