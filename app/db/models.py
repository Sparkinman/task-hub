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
    #: Any other CalDAV server: Nextcloud, Fastmail, Baikal, Synology, a friend's
    #: Radicale. Apple has its own value because its quirks need naming; every
    #: other server answers the same conversation and shares this one.
    CALDAV = "caldav"
    MICROSOFT = "microsoft"
    THINGS3 = "things3"
    OBSIDIAN = "obsidian"
    #: Two things at once, which is worth knowing before changing either. It is
    #: a connected service -- the tablet's built-in To-Do app, over an API read
    #: out of the Partner app -- and it is also the stamp the Supernote plugin
    #: puts on items it creates so they can be told apart from anything else
    #: arriving over CalDAV. An item carrying this origin may have come by
    #: either route.
    SUPERNOTE = "supernote"
    RADICALE = "radicale"
    LOCAL = "local"

    @property
    def display_name(self) -> str:
        """The service's name as a person writes it.

        Kept beside the enum rather than in the web layer because the daily
        summary email names services too, and two lists of names would drift.
        """
        return SERVICE_DISPLAY_NAMES.get(self.value, self.value.title())


#: Names as their makers spell them. "Things 3" carries its number, Microsoft's
#: list app is To Do rather than "Microsoft", and an item that arrived over
#: CalDAV is named for the protocol because Task Hub cannot know which app on
#: the other end wrote it.
SERVICE_DISPLAY_NAMES: dict[str, str] = {
    "google": "Google",
    "todoist": "Todoist",
    "ticktick": "TickTick",
    "apple": "Apple",
    "caldav": "CalDAV",
    "microsoft": "Microsoft To Do",
    "things3": "Things 3",
    "obsidian": "Obsidian",
    "supernote": "Supernote",
    "radicale": "CalDAV",
    "local": "Task Hub",
}


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

    #: The redirect URI this account was connected with, recorded so Task Hub
    #: can notice that the address has since changed. Refreshing a token never
    #: sends an address, so moving to a tunnel breaks nothing today -- it breaks
    #: the *next* reconnection, weeks later, with an error that points at the
    #: service rather than at the move. Null on accounts connected before this
    #: was recorded, and on ones that never used a redirect at all (a password,
    #: a personal token), both of which mean "nothing to compare" rather than
    #: "no drift", so the check stays quiet.
    connected_redirect_uri: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )

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

    #: Whether the service says this list cannot be written to. Distinct from a
    #: mapping's write switch, which is the user's choice: this is the service's
    #: own answer, and no choice can override it. Supernote's "Unfiled tasks" is
    #: the case that needed it -- a view of tasks belonging to no list, so a new
    #: task written there would have nowhere to go, and the engine offered it as
    #: a target and then failed on every pass.
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)

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


class SupernoteNote(Base):
    """One Supernote notebook, backed up as a PDF Task Hub can show.

    A backup rather than a sync: the folders it reads are never written to, and
    nothing here travels back to the tablet. The PDF is Supernote's own
    rendering of the note, because ``.note`` is an undocumented binary format
    and their converter is the only one guaranteed to understand the version
    the tablet is writing today.

    ``source_md5`` is what makes the job polite. Supernote reports an md5 for
    every file, so a note that was opened but not changed is recognised and
    never sent for conversion again -- conversion happens on Ratta's servers at
    their expense, and re-rendering unchanged notebooks on a timer is the
    fastest way to have this access withdrawn.
    """

    __tablename__ = "supernote_notes"
    __table_args__ = (
        UniqueConstraint("account_id", "note_id", name="uq_supernote_note"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    #: Supernote's own id for the .note file.
    note_id: Mapped[str] = mapped_column(String(64), index=True)
    #: The folder the user chose, so a note can be dropped when they untick it.
    root_folder_id: Mapped[str] = mapped_column(String(64), index=True)

    name: Mapped[str] = mapped_column(String(512))
    #: Folder path below the chosen root, for display only.
    folder_path: Mapped[str] = mapped_column(String(1024), default="")

    #: The md5 of the .note the stored PDF was made from.
    source_md5: Mapped[str] = mapped_column(String(64), default="")
    source_size: Mapped[int] = mapped_column(Integer, default=0)
    source_updated_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    #: File name under the notes directory. Not a path the user supplies, and
    #: never derived from the note's own name, which could contain anything.
    pdf_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pdf_size: Mapped[int] = mapped_column(Integer, default=0)

    #: A small PNG of the first page, extracted from the PDF Task Hub already
    #: holds rather than fetched separately. Null when one could not be made,
    #: which is not a fault: the notebook is still readable, it just has no
    #: picture beside its name.
    thumb_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    converted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: Why the last attempt failed, or null. Kept so a note that cannot be
    #: converted says so on the page instead of silently never appearing.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set when the user deletes a notebook's copy from Task Hub. The row stays
    #: behind deliberately: the notebook is still sitting in a folder they chose
    #: to back up, so without a record of the decision the very next pass would
    #: fetch it again and the delete button would appear not to work. Nothing is
    #: touched on the tablet -- only Task Hub's copy goes.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class PushSubscription(Base):
    """One browser that has agreed to receive notifications.

    A subscription belongs to a device and a browser rather than to a person:
    the same user on a phone and a laptop is two rows, and each has its own
    keys. They go stale on their own -- a browser reinstalled, permission
    withdrawn, an app removed from a home screen -- and the push service says so
    with a 404 or 410, at which point the row is deleted rather than retried
    forever.
    """

    __tablename__ = "push_subscriptions"
    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_endpoint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Where the browser's own push service expects the message. Long: Apple's
    #: run past 300 characters.
    endpoint: Mapped[str] = mapped_column(String(1024))
    #: The browser's public key and auth secret, both base64url. Not secrets of
    #: ours -- they are useless without the endpoint, and the endpoint is
    #: useless without the server's VAPID key.
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))

    #: Whatever the browser said about itself, to tell two devices apart in the
    #: list. Cosmetic, and truncated hard because it is user-supplied.
    label: Mapped[str] = mapped_column(String(120), default="")

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SupernoteDigestItem(Base):
    """One Supernote digest -- a passage highlighted out of a document.

    Mirrored rather than fetched on demand so the page is readable, and
    searchable, while Supernote is unreachable. The copy is what Task Hub shows;
    Supernote remains the authority, and a change there wins on the next pass.

    Unlike the notebook backup, this one is two-way: a digest written here is
    created on Supernote, and appears on the tablet.
    """

    __tablename__ = "supernote_digests"
    __table_args__ = (
        UniqueConstraint("account_id", "remote_id", name="uq_supernote_digest"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    #: Supernote's own numeric id, as a string. Their ids exceed what SQLite
    #: stores comfortably as an integer, and nothing here does arithmetic on it.
    remote_id: Mapped[str] = mapped_column(String(64), index=True)

    #: The library this belongs to, by Supernote's unique identifier for it.
    #: Empty for a digest filed in no library, which the tablet permits.
    library_uid: Mapped[str] = mapped_column(String(64), default="", index=True)
    library_name: Mapped[str] = mapped_column(String(255), default="")

    content: Mapped[str] = mapped_column(Text, default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    #: The file the passage came from, and where in it.
    source_path: Mapped[str] = mapped_column(String(1024), default="")
    source_type: Mapped[int] = mapped_column(Integer, default=0)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Set when the digest carries a handwritten comment.
    has_handwriting: Mapped[bool] = mapped_column(Boolean, default=False)
    #: File name of the decoded handwriting, under the notes directory. Null
    #: when there is none, or when the file could not be read -- the flag above
    #: stays true in that case, so the page can still say the tablet has more.
    handwriting_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Supernote's checksum for the handwriting, so it is fetched again only
    #: when it actually changes.
    handwriting_md5: Mapped[str] = mapped_column(String(64), default="")

    remote_md5: Mapped[str] = mapped_column(String(64), default="")
    remote_created_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    remote_updated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

