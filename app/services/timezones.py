"""Turning what a service sends into the wall time a person actually set.

Services disagree about how to express a moment, and the disagreement is not
cosmetic -- read it wrong and every timed task drifts by the size of the local
UTC offset, silently, in one direction.

Three shapes turn up:

* **Floating local time** -- ``2026-09-05T14:30:00`` with no offset. Todoist
  sends this for a task due at half past two, wherever you happen to be. The
  wall time is the value; there is nothing to convert.
* **A fixed instant** -- ``2026-09-05T14:30:00Z`` or with an offset. This is a
  point on the timeline, and the wall time a person sees depends on which zone
  they are in. TickTick always sends this form, as ``+0000``, alongside a
  separate ``timeZone`` naming the zone the user chose.
* **A date with no time at all** -- handled by the callers, not here.

Task Hub stores date, time and timezone as three separate fields, and the time
it stores is the **wall time in the named zone**. So a fixed instant has to be
converted into that zone before its clock face is read. Failing to do so is how
"17:00 in London" becomes "16:00" and then propagates that hour's error into
every other service.
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

UTC = dt.timezone.utc


def resolve_zone(name: str | None, default: str | None = None) -> dt.tzinfo:
    """The named zone, the fallback, or UTC -- never an exception.

    A bad zone name must not be able to abort a sync: one unparseable value in
    one task would stop every other task in the group from moving.
    """
    for candidate in (name, default):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            logger.debug("Unknown timezone %r; ignoring it", candidate)
    return UTC


def parse_timestamp(raw: str | None) -> dt.datetime | None:
    """Parse an ISO 8601 timestamp, tolerating the forms services really send.

    ``Z`` is accepted, and so is a compact ``+0000`` offset, which
    ``fromisoformat`` refuses before Python 3.11 and which TickTick uses
    throughout.
    """
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00").replace("z", "+00:00")
    # "+0000" -> "+00:00"
    if len(text) >= 5 and text[-5] in "+-" and ":" not in text[-5:]:
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def wall_time(
    raw: str | None,
    zone_name: str | None = None,
    default_zone: str | None = None,
) -> tuple[dt.date | None, dt.time | None, str | None]:
    """Split a timestamp into the date and clock time a person would read.

    A timestamp carrying an offset is converted into ``zone_name`` first, so
    ``16:00+0000`` with ``Europe/London`` comes back as 17:00 rather than 16:00.
    A timestamp without one is already a wall time and is taken as it stands.
    """
    moment = parse_timestamp(raw)
    if moment is None:
        # A bare date is still useful even when the rest will not parse.
        if raw and len(raw.strip()) >= 10:
            try:
                return dt.date.fromisoformat(raw.strip()[:10]), None, None
            except ValueError:
                return None, None, None
        return None, None, None

    if moment.tzinfo is None:
        # Floating: the clock face is the value.
        return moment.date(), moment.time().replace(microsecond=0), zone_name

    zone = resolve_zone(zone_name, default_zone)
    local = moment.astimezone(zone)
    label = zone_name or default_zone or "UTC"
    return local.date(), local.time().replace(microsecond=0), label


def to_utc(
    date: dt.date | None,
    time_of_day: dt.time | None,
    zone_name: str | None = None,
    default_zone: str | None = None,
) -> dt.datetime | None:
    """The instant a wall time refers to, as an aware UTC datetime.

    The inverse of :func:`wall_time`, for services that want a fixed instant
    rather than a floating one. Stamping ``+0000`` onto a local wall time
    without this conversion shifts the task by the local offset, in the opposite
    direction to the reading error -- so a round trip through a naive
    implementation moves a task twice.
    """
    if date is None:
        return None
    zone = resolve_zone(zone_name, default_zone)
    naive = dt.datetime.combine(date, time_of_day or dt.time(0, 0))
    return naive.replace(tzinfo=zone).astimezone(UTC)
