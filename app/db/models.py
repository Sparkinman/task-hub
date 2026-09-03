"""Database schema for Task Hub.

The full schema is defined up front, including tables the sync engine will not
use until later phases, so that the shape of the data never has to change under
a running installation.

The central design decision lives in :class:`Item`: a due date is stored as
*separate* date, time and timezone components rather than one timestamp. That is
what makes it possible to accept a date-only edit from Google Tasks -- which
cannot represent a time of day at all -- without destroying a time of day that
was set in Todoist. A single ``due_at`` column would make that loss unavoidable,
because there would be no way to distinguish "midnight" from "no time given".
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Stores an aware datetime as UTC and reads it back still aware.

    SQLite has no timezone type: it keeps whatever string it is given and hands
    back a naive datetime. Every timestamp in this schema is UTC, but without
    this the tzinfo is lost on the way out, and comparing a stored timestamp
    against a live one raises "can't compare offset-naive and offset-aware
    datetimes" -- in the middle of a sync, where the comparison decides which
    of two edits is newer.

    Values are stored naive-UTC (what SQLite accepts) and re-stamped as UTC on
    the way back, so the rest of the code only ever sees aware datetimes.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # A naive value reaching here is assumed to already be UTC, which
            # is the convention everywhere in this application.
            return value
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)


class EnumString(TypeDecorator):
    """Stores an Enum as its ``.value`` string and reads it back as the Enum.

    A plain ``String`` column would write the value correctly but hand back a
    bare ``str`` on load, so ``account.status.value`` would silently produce
    nothing in a template rather than raising anywhere visible. Making the
    column type responsible for the round trip means every read gets a real
    enum, whatever path it came from.

    The underlying storage is unchanged -- still a VARCHAR holding the same
    strings -- so this needs no migration.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class, length: int = 32, **kwargs):
        self.enum_class = enum_class
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value.value
        # Accept a bare string so existing rows and hand-written queries work.
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return self.enum_class(value)
        except ValueError:
            # A value written by a newer version we do not know about. None is
            # safer than a raw string, which would reintroduce exactly the
            # silent-failure this class exists to prevent.
            return None


def utcnow() -> dt.datetime:
    """Timezone-aware UTC now.

    Every timestamp in this database is UTC. Local time is a presentation
    concern applied with the user's configured timezone at render time; storing
    local times would make the "which edit is newer?" comparison at the heart of
    the merge engine dependent on daylight saving transitions.
    """
    return dt.datetime.now(dt.timezone.utc)


# --- Enumerations -------------------------------------------------------------


class ServiceKind(str, enum.Enum):
    """Every service Task Hub can connect to.

    Values are stable identifiers used in URLs, badge colours and the docs
    filenames, so they must not be renamed once shipped.
    """

    GOOGLE = "google"
    TODOIST = "todoist"
    TICKTICK = "ticktick"
    APPLE = "apple"
    MICROSOFT = "microsoft"
    THINGS3 = "things3"
    OBSIDIAN = "obsidian"
    #: Not a service Task Hub connects to: the Supernote plugin stamps items it
    #: creates so they can be told apart from anything else arriving over
    #: CalDAV. It is an origin only, like LOCAL, and never appears in the
    #: service catalogue.
    SUPERNOTE = "supernote"
    RADICALE = "radicale"
    LOCAL = "local"


class CollectionKind(str, enum.Enum):
    """Whether a list holds to-dos (VTODO) or calendar events (VEVENT)."""

    TASKS = "tasks"
    CALENDAR = "calendar"


class ItemStatus(str, enum.Enum):
    """iCalendar status values, which every service maps onto."""

    NEEDS_ACTION = "needs-action"
    IN_PROCESS = "in-process"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AccountStatus(str, enum.Enum):
    NEW = "new"                 # created but never authorised
    CONNECTED = "connected"     # working
    NEEDS_AUTH = "needs_auth"   # token expired or revoked; user must reconnect
    ERROR = "error"             # transient failure; will retry
    DISABLED = "disabled"       # switched off by the user


class SyncOutcome(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"         # some connectors failed, others completed
    FAILED = "failed"


# --- Settings and users -------------------------------------------------------


class AppSetting(Base):
    """Key/value application settings.

    A key/value table rather than a single wide row: later phases add settings,
    and this way adding one is a write rather than a schema migration on a
    database that already holds the user's live data.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class User(Base):
    """A web interface login.

    Separate from the Radicale account on purpose. The Radicale password is
    typed into third-party CalDAV clients on phones and laptops, so it gets
    copied around; the web login guards the page where every OAuth token can be
    read. Reusing one password for both would couple those two risks together.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )


# --- Radicale ------------------------------------------------------------------


class RadicaleCollection(Base):
    """A CalDAV collection hosted by the embedded Radicale server.

    Mirrors what exists on disk so the UI can list collections without walking
    the filesystem or issuing a CalDAV request on every page load. Radicale
    remains the source of truth; this is a cache that is refreshed on demand.
    """

    __tablename__ = "radicale_collections"
    __table_args__ = (
        UniqueConstraint("radicale_user", "collection_id", name="uq_radicale_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    radicale_user: Mapped[str] = mapped_column(String(150), index=True)
    #: Path segment under the user's home, e.g. "tasks" in /user/tasks/
    collection_id: Mapped[str] = mapped_column(String(150))
    display_name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[CollectionKind] = mapped_column(EnumString(CollectionKind, 16))
    colour: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )

    @property
    def url_path(self) -> str:
        """Path of this collection relative to the Radicale mount point."""
        return f"/{self.radicale_user}/{self.collection_id}/"


# --- Service accounts and their lists -----------------------------------------


class Account(Base):
    """One connected account for one service.

    Each service supports ten numbered slots, so several accounts of the same
    kind (work Google, personal Google, a shared family Todoist) can be
    connected simultaneously and configured independently.
    """

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("service", "slot", name="uq_account_service_slot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service: Mapped[ServiceKind] = mapped_column(EnumString(ServiceKind, 32), index=True)
    #: 1-10. Displayed to the user as "Slot 3" so accounts stay identifiable
    #: even before the service reports an email address.
    slot: Mapped[int] = mapped_column(Integer)

    label: Mapped[str] = mapped_column(String(120), default="")
    #: Email or username reported by the service, shown to disambiguate slots.
    remote_identity: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Fernet-encrypted JSON: OAuth tokens, app-specific passwords, client
    #: credentials. Never logged and never rendered back into the page.
    credentials: Mapped[str | None] = mapped_column(Text, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[AccountStatus] = mapped_column(
        EnumString(AccountStatus, 24), default=AccountStatus.NEW
    )
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_sync_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    #: Opaque per-connector state: delta tokens, sync cursors, page markers.
    sync_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    lists: Mapped[list["RemoteList"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    @property
    def display_name(self) -> str:
        if self.label:
            return self.label
        if self.remote_identity:
            return self.remote_identity
        return f"{self.service.value.title()} slot {self.slot}"


class RemoteList(Base):
    """A task list or calendar discovered inside a connected account.

    Read and write are separate switches by design. A common arrangement is to
    read a shared team calendar but never write to it, and that is only
    expressible if the two directions are independent.
    """

    __tablename__ = "remote_lists"
    __table_args__ = (
        UniqueConstraint("account_id", "remote_id", name="uq_remote_list"),
        Index("ix_remote_list_group", "sync_group_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    #: Identifier as the service knows it. For CalDAV this is the collection URL.
    remote_id: Mapped[str] = mapped_column(String(512))
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[CollectionKind] = mapped_column(EnumString(CollectionKind, 16))
    colour: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Legacy single-target columns, kept so an older database still opens and
    #: so the migration has something to read. Participation is now decided by
    #: ListMapping; nothing should consult these.
    read_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    write_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_groups.id", ondelete="SET NULL"), nullable=True
    )

    #: Set when the service still lists it but Task Hub could not read it.
    unavailable: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )

    account: Mapped[Account] = relationship(back_populates="lists")
    sync_group: Mapped["SyncGroup | None"] = relationship(back_populates="lists")


class ListMapping(Base):
    """Connects one service list to one Radicale collection.

    Replaces the single foreign key that used to live on :class:`RemoteList`,
    which allowed a list to feed exactly one collection. A list can now be read
    into as many collections as you like -- mirroring a work calendar into
    several views, for instance.

    Write-back is deliberately not symmetric: at most one mapping per list may
    have ``write_enabled``. Two collections writing to the same remote list
    would each create their own copy of every task at the far end, and would
    then undo one another on alternating passes. Reading into many is coherent;
    writing from many is not, so the restriction is enforced rather than left
    as advice.
    """

    __tablename__ = "list_mappings"
    __table_args__ = (
        UniqueConstraint("remote_list_id", "sync_group_id", name="uq_list_mapping"),
        Index("ix_list_mapping_group", "sync_group_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    remote_list_id: Mapped[int] = mapped_column(
        ForeignKey("remote_lists.id", ondelete="CASCADE"), index=True
    )
    sync_group_id: Mapped[int] = mapped_column(
        ForeignKey("sync_groups.id", ondelete="CASCADE"), index=True
    )
    read_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    write_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Whether reading this list may introduce tasks the collection has never
    #: seen, or only report changes to ones it already knows.
    #:
    #: A write-back target usually wants the second. Reading it at all is what
    #: lets a task completed there be completed everywhere; but treating it as
    #: a full source as well turns it into a mirror, where anything anyone adds
    #: to the copy is pushed back into the original. Separating "tell me about
    #: changes" from "this is a place tasks come from" is what keeps a
    #: write-back target a destination rather than a second front door.
    create_from_remote: Mapped[bool] = mapped_column(Boolean, default=True)

    #: Which lists this destination accepts tasks from, or null for "all of
    #: them".
    #:
    #: A list that is simply synced with the collection is a full member and
    #: holds everything in it, so this stays null. A list chosen under another
    #: list's "Also write out to" is something else: an aggregate, there to
    #: gather that list's tasks somewhere they can be seen and ticked off.
    #: Sending it everything the collection contains would quietly turn it into
    #: a third copy of the whole set.
    write_from_list_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)


class SyncGroup(Base):
    """A set of lists kept in sync with each other.

    Sync is deliberately not global. Grouping means "Work" lists in Google,
    Todoist and Radicale can converge while a private "Household" list stays
    entirely separate, which is impossible if everything syncs to everything.
    """

    __tablename__ = "sync_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[CollectionKind] = mapped_column(EnumString(CollectionKind, 16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    #: The Radicale collection acting as this group's canonical store.
    radicale_collection_id: Mapped[int | None] = mapped_column(
        ForeignKey("radicale_collections.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )

    lists: Mapped[list[RemoteList]] = relationship(back_populates="sync_group")
    radicale_collection: Mapped[RadicaleCollection | None] = relationship()


# --- Canonical items ----------------------------------------------------------


class Item(Base):
    """The canonical form of a task or event, merged from every service.

    Note the split due/start/end columns. ``due_time`` being NULL means "this
    task has a date but no time of day", which is a genuinely different state
    from "this task is due at midnight" -- and telling those two apart is the
    whole reason a date edit made in Google Tasks can be applied without
    discarding a time set elsewhere.
    """

    __tablename__ = "items"
    __table_args__ = (
        # Scoped to the group: when one service list is read into two
        # collections, each collection holds its own copy, and a calendar event
        # carries the same iCalUID into both.
        UniqueConstraint("uid", "sync_group_id", name="uq_item_uid_group"),
        Index("ix_item_group_status", "sync_group_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Stable canonical identifier, reused as the iCalendar UID in Radicale so
    #: the same item is recognisable across a rebuild or a restore from backup.
    uid: Mapped[str] = mapped_column(String(255), index=True)
    sync_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_groups.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[CollectionKind] = mapped_column(EnumString(CollectionKind, 16))

    title: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ItemStatus] = mapped_column(
        EnumString(ItemStatus, 24), default=ItemStatus.NEEDS_ACTION
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    # -- Due (tasks). Split into components; see the class docstring.
    due_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    due_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    due_tz: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # -- Start / end (events, and optionally tasks)
    start_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    start_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    start_tz: Mapped[str | None] = mapped_column(String(64), nullable=True)
    end_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    end_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    end_tz: Mapped[str | None] = mapped_column(String(64), nullable=True)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)

    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: iCalendar scale: 0 = undefined, 1 = highest, 9 = lowest.
    priority: Mapped[int] = mapped_column(Integer, default=0)
    #: Raw RRULE, stored verbatim so services with richer recurrence than ours
    #: round-trip unchanged instead of being flattened to a lossy approximation.
    rrule: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    #: Which service the item was first seen in. Drives the coloured badge in
    #: the task viewer, so it is never overwritten after creation.
    origin_service: Mapped[ServiceKind] = mapped_column(
        EnumString(ServiceKind, 32), default=ServiceKind.LOCAL
    )
    origin_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )

    #: The list an item was first read from, not merely the account. Write-out
    #: targets are filtered by it: a destination configured under one list is
    #: there to aggregate *that* list, and pushing it everything the collection
    #: holds would turn an aggregate into a second copy of everything.
    #:
    #: Null for items that predate the column, which are treated as unrestricted
    #: so an upgrade never stops an existing destination being updated.
    origin_remote_list_id: Mapped[int | None] = mapped_column(
        ForeignKey("remote_lists.id", ondelete="SET NULL"), nullable=True
    )

    #: Hash of the syncable fields, used to skip no-op writes during a push.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    links: Mapped[list["ItemLink"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    provenance: Mapped[list["FieldProvenance"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class ItemLink(Base):
    """Maps a canonical item to its counterpart in one remote list."""

    __tablename__ = "item_links"
    __table_args__ = (
        # One remote item can now correspond to several canonical items -- one
        # per collection it is read into -- so the link is unique per group
        # rather than per account.
        UniqueConstraint(
            "account_id", "remote_id", "sync_group_id", name="uq_item_link_scope"
        ),
        Index("ix_item_link_item", "item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    remote_list_id: Mapped[int | None] = mapped_column(
        ForeignKey("remote_lists.id", ondelete="SET NULL"), nullable=True
    )
    #: Which collection this link belongs to. Denormalised from the item so the
    #: uniqueness rule can be expressed as a constraint rather than in code.
    sync_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_groups.id", ondelete="CASCADE"), nullable=True, index=True
    )
    remote_id: Mapped[str] = mapped_column(String(512))
    remote_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_updated_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    #: Content hash of what Task Hub last wrote here. If the remote still
    #: matches this, an unchanged item needs no write at all -- the mechanism
    #: that stops completed tasks being rewritten on every pass.
    last_pushed_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Per-field fingerprints of what was last pushed to this account. Lets the
    #: merge engine tell a genuine remote edit from the service simply echoing
    #: back our own value -- which matters because services stamp a single
    #: modification time on the whole item, so a stale field looks as fresh as
    #: an edited one.
    last_pushed_fields: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    item: Mapped[Item] = relationship(back_populates="links")


class FieldProvenance(Base):
    """Records who last changed each individual field, and when.

    Conflict resolution is per field rather than per record. If a note is edited
    in Todoist and a due date is edited in Google between two sync passes, both
    edits should survive; whole-record last-writer-wins would silently discard
    one of them.
    """

    __tablename__ = "field_provenance"
    __table_args__ = (
        UniqueConstraint("item_id", "field", name="uq_provenance_item_field"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    field: Mapped[str] = mapped_column(String(48))
    #: Account id that supplied the value, or NULL when edited in Task Hub.
    source_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    source_service: Mapped[ServiceKind] = mapped_column(
        EnumString(ServiceKind, 32), default=ServiceKind.LOCAL
    )
    changed_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )

    item: Mapped[Item] = relationship(back_populates="provenance")


class Tombstone(Base):
    """Remembers deletions so they propagate instead of resurrecting.

    Without this, a delete in one service is indistinguishable from an item that
    service has simply not reported yet, and the next pull would recreate it.
    """

    __tablename__ = "tombstones"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(255), index=True)
    sync_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_groups.id", ondelete="CASCADE"), nullable=True
    )
    deleted_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )
    deleted_by_service: Mapped[ServiceKind] = mapped_column(
        EnumString(ServiceKind, 32), default=ServiceKind.LOCAL
    )
    #: Cleared once every linked account has confirmed the deletion.
    propagated: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Every remote id this item was known by when it was deleted, as
    #: ["<account id>:<remote id>", ...]. Most services cannot store Task Hub's
    #: UID, so a task they report after a deletion arrives with an empty one and
    #: the uid above can never match it. The remote id always matches, and
    #: without it a task deleted in one service is recreated by the next pull
    #: from any service that has not caught up yet.
    remote_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)


# --- Sync history -------------------------------------------------------------


class SyncRun(Base):
    """One pass of the sync engine, for the history and diagnostics views."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, index=True
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    outcome: Mapped[SyncOutcome] = mapped_column(
        EnumString(SyncOutcome, 16), default=SyncOutcome.RUNNING
    )
    trigger: Mapped[str] = mapped_column(String(24), default="scheduled")

    items_pulled: Mapped[int] = mapped_column(Integer, default=0)
    items_pushed: Mapped[int] = mapped_column(Integer, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0)
    conflicts: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)

    entries: Mapped[list["SyncLogEntry"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class SyncLogEntry(Base):
    """A single line of detail within a sync run."""

    __tablename__ = "sync_log_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), index=True
    )
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    level: Mapped[str] = mapped_column(String(16), default="info")
    service: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    run: Mapped[SyncRun] = relationship(back_populates="entries")
