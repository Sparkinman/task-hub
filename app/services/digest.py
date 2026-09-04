"""The daily summary of what is due, and the job that sends it.

One message, listing what is overdue, what is due today, and what is coming over
the following week. Nothing reads it back and nothing depends on it, which makes
it the safest thing Task Hub can send and the reason it exists before anything
else that needs mail.

Four decisions in here are worth stating, because each is the difference
between a message somebody reads and one they filter away:

**Overdue comes first.** A list that opens with today's work buries the thing
that was already late, which is the item most likely to matter.

**Every line names where it came from.** Somebody with four services connected
is reading one list of things from four different places, and "which app do I
open to deal with this?" is the question the summary exists to answer.

**A day with nothing due sends nothing**, unless asked otherwise. A message that
arrives every morning saying "nothing due" stops being read inside a week, and
then the one that matters is not read either. The week ahead is context for
today's list, never a reason to write on its own.

**Times are the user's, not the server's.** The cutoff for "today" is midnight
where they are, so somebody in Denver does not get tomorrow's list at five in
the afternoon.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.db import settings_store
from app.db.models import (
    SERVICE_DISPLAY_NAMES,
    Item,
    ItemStatus,
    RadicaleCollection,
    SyncGroup,
)
from app.services.mailer import MailSettings, send

logger = logging.getLogger(__name__)


#: How far ahead the "coming week" section looks, not counting today.
UPCOMING_DAYS = 7


@dataclass
class Digest:
    """What one morning's message contains."""

    on: dt.date
    overdue: list[Item] = field(default_factory=list)
    today: list[Item] = field(default_factory=list)
    upcoming: list[Item] = field(default_factory=list)

    @property
    def tomorrow(self) -> list[Item]:
        """The part of the coming week that is tomorrow."""
        due = self.on + dt.timedelta(days=1)
        return [item for item in self.upcoming if item.due_date == due]

    @property
    def empty(self) -> bool:
        """Whether there is anything worth sending.

        The week ahead alone does not justify a message. It is context for
        today's list, not a reason to write.
        """
        return not (self.overdue or self.today)

    @property
    def subject_counts(self) -> str:
        parts = []
        if self.overdue:
            parts.append(f"{len(self.overdue)} overdue")
        if self.today:
            parts.append(f"{len(self.today)} due today")
        return ", ".join(parts) or "nothing due"


def timezone_for(session) -> ZoneInfo | dt.tzinfo:
    name = settings_store.get(session, settings_store.TIMEZONE) or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def collect(session, today: dt.date | None = None) -> Digest:
    """Everything due, from every enabled collection.

    Completed and cancelled items are left out: a summary of what to do should
    not be padded with what is already done.
    """
    if today is None:
        today = dt.datetime.now(timezone_for(session)).date()
    horizon = today + dt.timedelta(days=UPCOMING_DAYS)

    enabled_groups = {
        group.id
        for group in session.execute(
            select(SyncGroup).where(SyncGroup.enabled.is_(True))
        ).scalars()
    }

    digest = Digest(on=today)
    rows = session.execute(
        select(Item).where(Item.due_date.is_not(None))
    ).scalars()

    for item in rows:
        if item.sync_group_id not in enabled_groups:
            continue
        if item.status in (ItemStatus.COMPLETED, ItemStatus.CANCELLED):
            continue
        if item.due_date < today:
            digest.overdue.append(item)
        elif item.due_date == today:
            digest.today.append(item)
        elif item.due_date <= horizon:
            digest.upcoming.append(item)

    # Oldest first among the overdue, so the most neglected is at the top; by
    # time of day within a single day, so the morning's work reads in order.
    digest.overdue.sort(key=lambda i: (i.due_date, i.due_time or dt.time.min))
    digest.today.sort(key=lambda i: (i.due_time or dt.time.min, (i.title or "").lower()))
    digest.upcoming.sort(
        key=lambda i: (i.due_date, i.due_time or dt.time.min, (i.title or "").lower())
    )
    return digest


def _day(on: dt.date) -> str:
    """A date as a person reads it: "Wed 3 Sep".

    Deliberately not ISO in the body. The subject line keeps the ISO date, where
    it sorts and is unambiguous; a list of twenty lines is read, not sorted.
    """
    return f"{on.strftime('%a')} {on.day} {on.strftime('%b')}"


def source_of(item: Item) -> str:
    """Which service this item came from, named as its maker spells it.

    ``origin_service`` is the service an item was *first seen in* and is never
    overwritten afterwards, which is exactly the question somebody reading the
    summary is asking: where do I go to deal with this? An item that has since
    been mirrored into three other services still has one home.
    """
    origin = getattr(item, "origin_service", None)
    if origin is None:
        return ""
    return getattr(origin, "display_name", None) or SERVICE_DISPLAY_NAMES.get(
        str(getattr(origin, "value", origin)), ""
    )


def _collection(session, item: Item) -> str:
    group = session.get(SyncGroup, item.sync_group_id) if item.sync_group_id else None
    if group is None:
        return ""
    row = (
        session.get(RadicaleCollection, group.radicale_collection_id)
        if group.radicale_collection_id else None
    )
    return (row.display_name if row else group.name) or ""


def _line(session, item: Item, show_date: bool = False) -> str:
    when = _day(item.due_date) if (show_date and item.due_date) else ""
    if item.due_time:
        when = f"{when} {item.due_time.strftime('%H:%M')}".strip()

    # Source first, collection second: the service is what tells you which app
    # to open, and the collection only narrows it down further.
    tail = " · ".join(part for part in (source_of(item), _collection(session, item)) if part)

    bits = [f"- {item.title or '(untitled)'}"]
    if when:
        bits.append(f"({when})")
    if tail:
        bits.append(f"[{tail}]")
    return " ".join(bits)


def render(session, digest: Digest) -> tuple[str, str]:
    """The subject and body of the message."""
    subject = f"Task Hub — {digest.subject_counts} ({digest.on.isoformat()})"

    lines: list[str] = []
    if digest.overdue:
        lines.append(f"OVERDUE ({len(digest.overdue)})")
        lines.extend(_line(session, i, show_date=True) for i in digest.overdue)
        lines.append("")
    if digest.today:
        lines.append(f"DUE TODAY ({len(digest.today)})")
        lines.extend(_line(session, i) for i in digest.today)
        lines.append("")
    if not digest.overdue and not digest.today:
        lines.append("Nothing is due today, and nothing is overdue.")
        lines.append("")
    if digest.upcoming:
        lines.append(f"THE NEXT {UPCOMING_DAYS} DAYS ({len(digest.upcoming)})")
        lines.extend(_line(session, i, show_date=True) for i in digest.upcoming)
        lines.append("")

    lines.append("— Task Hub")
    return subject, "\n".join(lines)


def mail_settings(session) -> MailSettings:
    from app.crypto import decrypt_json

    return MailSettings(
        host=(settings_store.get(session, settings_store.SMTP_HOST) or "").strip(),
        port=settings_store.get_int(session, settings_store.SMTP_PORT, 587),
        security=(settings_store.get(session, settings_store.SMTP_SECURITY)
                  or "starttls").strip(),
        username=(settings_store.get(session, settings_store.SMTP_USERNAME) or "").strip(),
        password=decrypt_json(
            settings_store.get(session, settings_store.SMTP_PASSWORD)
        ).get("password", ""),
        from_address=(settings_store.get(session, settings_store.SMTP_FROM) or "").strip(),
    )


def send_digest(session, force: bool = False) -> str:
    """Build and send today's summary. Returns what happened, for the log.

    ``force`` is what the "Send one now" button uses: it sends even on a quiet
    day, because a test that silently does nothing proves nothing.
    """
    digest = collect(session)
    if digest.empty and not force and not settings_store.get_bool(
        session, settings_store.DIGEST_WHEN_EMPTY
    ):
        return "Nothing due; no message sent."

    to = (settings_store.get(session, settings_store.DIGEST_TO) or "").strip()
    if not to:
        return "No recipient address set; no message sent."

    subject, body = render(session, digest)
    send(mail_settings(session), to, subject, body)
    return f"Sent to {to}: {digest.subject_counts}."
