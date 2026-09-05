"""The connector interface every service implements.

A connector's job is narrow on purpose: translate between one service's idea of
a task or event and Task Hub's :class:`CanonicalRecord`, and say honestly what
that service can and cannot represent. All reconciliation logic lives in the
sync engine, so adding a service never means re-implementing merge rules.

The honesty is the important part. :class:`Capabilities` is not documentation --
it is load-bearing. The merge engine only ever considers fields a connector
declares it can represent, which is precisely what stops Google Tasks (no time
of day) from wiping a time set in Todoist.
"""

from __future__ import annotations

import abc
import datetime as dt
import re
from dataclasses import dataclass, field

from app.db.models import CollectionKind, ServiceKind
from app.services.ical_model import CanonicalRecord

# --- Field names shared by capabilities, provenance and the merge engine ------
#
# These strings are stored in the field_provenance table, so they must stay
# stable once shipped.

F_TITLE = "title"
F_NOTES = "notes"
F_STATUS = "status"
F_DUE_DATE = "due_date"
F_DUE_TIME = "due_time"
F_START = "start"
F_END = "end"
F_PRIORITY = "priority"
F_TAGS = "tags"
F_LOCATION = "location"
F_RRULE = "rrule"

ALL_FIELDS: frozenset[str] = frozenset(
    {
        F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE, F_DUE_TIME,
        F_START, F_END, F_PRIORITY, F_TAGS, F_LOCATION, F_RRULE,
    }
)


@dataclass(frozen=True)
class Capabilities:
    """What one service can faithfully store.

    A field marked False is not "empty" at this service -- it is *absent*, and
    absence must never overwrite. Google Tasks is the motivating case: it
    accepts an RFC 3339 timestamp and silently discards the time portion, so
    ``due_time=False`` tells the merge engine to leave the canonical time alone
    no matter what Google reports back.
    """

    fields: frozenset[str]

    #: Whether the service can delete items through its API.
    can_delete: bool = True
    #: Whether the service can create items, or is read-only by nature.
    can_create: bool = True
    #: Which fields a push may actually change, when that is narrower than what
    #: the service can *hold*. Obsidian is the motivating case: a markdown line
    #: carries a due date perfectly well, but Task Hub will only ever patch a
    #: task's completion into someone's notes. Left as None, a service can write
    #: everything it can read, which is true of every other connector.
    writable_fields: frozenset[str] | None = None
    #: Longest title the service accepts; longer ones are truncated on push.
    max_title_length: int | None = None
    #: Longest note body accepted.
    max_notes_length: int | None = None
    #: Whether this service can express one task belonging to another.
    #:
    #: Deliberately a flag of its own rather than a member of ``fields``, because
    #: parenthood is a relationship and the rest are values. The distinction is
    #: not pedantic: a service that cannot hold it must never be able to report
    #: a task as having *no* parent, since that would flatten a hierarchy rather
    #: than merely fail to carry it -- and unlike a lost due date, that cannot be
    #: reconstructed afterwards. Supernote's to-do API is the motivating case:
    #: its task rows have no parent field of any kind.
    supports_parent: bool = False

    #: Whether a note written to this service is actually shown to the person
    #: using it.
    #:
    #: Not the same question as whether the service *stores* one. Supernote's
    #: to-do API accepts a ``detail`` field and hands it back faithfully, but
    #: the tablet's To-Do app displays only the title and the date -- confirmed
    #: on the device. So folding a task's steps into its note there does not
    #: tidy them away, it hides them completely, which is worse than the clutter
    #: it was meant to avoid. Where this is False, subtasks are sent as tasks of
    #: their own instead, because a visible mess beats invisible information.
    notes_visible: bool = True

    #: Whether the canonical UID can be stored remotely and read back. When
    #: False, links are tracked in Task Hub's database instead.
    stores_uid: bool = False

    #: Whether the service preserves Task Hub's "where did this originate"
    #: marker. Only stores that round-trip arbitrary properties can -- CalDAV
    #: does, via an X- property. When False, anything read from this service is
    #: treated as having originated there, which is the correct assumption.
    carries_origin: bool = False

    def supports(self, field_name: str) -> bool:
        return field_name in self.fields

    def push_fields(self) -> frozenset[str]:
        """The fields a write is allowed to alter.

        Used for the push-suppression hash, so a change to a field this service
        cannot write does not queue a pointless write on every pass -- and, more
        importantly, does not get marked as pushed when nothing carried it.
        """
        if self.writable_fields is None:
            return self.present_fields()
        return frozenset(self.writable_fields) & self.present_fields()

    def present_fields(self) -> frozenset[str]:
        return self.fields


#: Convenience: everything iCalendar can express. Used by the Radicale connector,
#: which is the only fully lossless store in the system.
def folded_steps(record) -> list[tuple[str, bool]]:
    """A parent's children as (title, done) pairs, for services that cannot nest.

    Shared because three connectors need the same thing and the tidying is the
    same each time: skip the untitled, keep the order the engine sent, and say
    whether each is finished. What a connector does with the pairs is its own
    business -- a real checklist where there is one, a few lines of notes where
    there is not.
    """
    steps: list[tuple[str, bool]] = []
    for child in getattr(record, "children", None) or []:
        title = (getattr(child, "title", "") or "").strip()
        if not title:
            continue
        steps.append((title, getattr(child, "is_completed", False)))
    return steps


#: The heading written above folded subtasks in a notes field.
STEPS_HEADING = "Steps:"

#: A label appended to a title at a service that shows nothing but titles.
#:
#: Supernote is the case: its To-Do app displays a title and a date and nothing
#: else, so a task's place in a piece of work has nowhere to go except the
#: title itself. Written as a bracketed suffix and matched here so it can be
#: taken off again -- a service reports the labelled title straight back, and
#: reading that as the real title would relabel the label on the next pass.
#: The unmistakable form, carrying the parent's name after a middle dot. Only
#: this module writes that, so it is removed wherever it appears -- including
#: mid-title, which is what happens when somebody types after it on the device.
_LABEL_FULL = re.compile(r"\s*\[\d+ of \d+(?: \u00b7 [^\]]*)?\]")

#: The bare form, on a parent, which somebody could plausibly have typed
#: themselves. Removed only at the very end, where this writes it.
_LABEL_BARE = re.compile(r"\s*\[\d+ of \d+(?: done)?\]\s*$")


def strip_label(title: str | None) -> str | None:
    """Remove a step label this wrote from a title read back off a service.

    Deliberately asymmetric. The form carrying a parent's name is unambiguous,
    so it goes wherever it sits -- otherwise typing a word after it on the
    tablet would leave the label embedded in the real title, and the next pass
    would add a second one beside it. The bare ``[1/3]`` form is something a
    person could have written, so it is only taken off the end.
    """
    if not title:
        return title
    cleaned = _LABEL_BARE.sub("", _LABEL_FULL.sub("", title)).strip()
    # Never hand back nothing: a task called "[1/3]" and nothing else is
    # somebody's own odd title, not a label wrapped around a name.
    return cleaned or title


def label_title(title: str, done: int, total: int, belongs_to: str = "",
                index: int = 0) -> str:
    """Put a task's place in its piece of work into the title itself."""
    base = strip_label(title) or title
    if total <= 0:
        return base
    if belongs_to:
        # A step: which one it is, and what it belongs to.
        inside = f"{index or done} of {total} \u00b7 {strip_label(belongs_to) or belongs_to}"
    else:
        # The parent: how much of it is finished.
        inside = f"{done} of {total} done"
    return f"{base} [{inside}]"


def strip_steps(text: str | None) -> str | None:
    """Remove a folded step list from notes read back off a service.

    The counterpart to :func:`steps_as_text`, and not optional. A service given
    a task whose steps were written into its notes reports those notes back
    verbatim; taken at face value they become the canonical note, and the next
    push folds the steps in again -- below the copy already there. Left alone
    that grows without limit, and the real note is buried in it.

    Only a block this wrote is removed. It has to be the last thing in the text
    and every line under the heading has to look like a step, so a note where
    somebody has genuinely typed "Steps:" and then a paragraph survives intact.
    """
    if not text or STEPS_HEADING not in text:
        return text
    head, _, tail = text.rpartition(f"\n\n{STEPS_HEADING}\n")
    if not head and not _:
        # The block is the whole note rather than appended to one.
        if text.startswith(f"{STEPS_HEADING}\n"):
            head, tail = "", text[len(STEPS_HEADING) + 1:]
        else:
            return text
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines or not all(
        line.lstrip().startswith(("[ ]", "[x]", "[X]")) for line in lines
    ):
        return text  # Somebody's own writing, not ours. Leave it be.
    return head or None


def steps_as_text(record, existing: str | None = None) -> str | None:
    """The children written into a notes field, for services with nothing better.

    Appended below whatever notes the task already has, under a heading, so it
    reads as a list somebody wrote rather than mangled text. Marked with the
    same tick and bullet characters a person would use by hand.
    """
    steps = folded_steps(record)
    if not steps:
        return existing
    lines = [f"{'[x]' if done else '[ ]'} {title}" for title, done in steps]
    block = f"{STEPS_HEADING}\n" + "\n".join(lines)
    body = (existing or "").rstrip()
    return f"{body}\n\n{block}" if body else block


FULL_CAPABILITIES = Capabilities(
    fields=ALL_FIELDS, stores_uid=True, carries_origin=True, supports_parent=True
)


@dataclass
class RemoteList:
    """A task list or calendar as the service describes it."""

    remote_id: str
    name: str
    kind: CollectionKind
    colour: str | None = None
    is_default: bool = False
    read_only: bool = False


@dataclass
class RemoteItem:
    """One item as reported by a service, ready for merging.

    ``fields_present`` is normally the connector's capability set, but a
    connector may narrow it further per item when it genuinely does not know a
    field's value -- for example when a paged API omits a field in list
    responses. Narrowing is always safe; widening is not.
    """

    remote_id: str
    record: CanonicalRecord
    fields_present: frozenset[str]
    remote_updated_at: dt.datetime | None = None
    etag: str | None = None
    #: Set when the service reports the item as deleted rather than omitting it.
    deleted: bool = False


@dataclass
class PullResult:
    """Everything a single pull produced, plus what went wrong."""

    items: list[RemoteItem] = field(default_factory=list)
    #: Opaque cursor or delta token to hand back on the next pull.
    sync_state: dict | None = None
    #: True when the service reported only what changed rather than everything.
    #: Deletion detection differs: a full listing lets absence imply deletion,
    #: an incremental one does not.
    incremental: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class PushOutcome:
    """The result of writing one item to a service."""

    remote_id: str | None
    etag: str | None = None
    remote_updated_at: dt.datetime | None = None
    error: str | None = None
    #: This item was not this service's to write, and nothing went wrong.
    #:
    #: Distinct from an error because it happens on every pass, for ever, by
    #: design -- a service that already holds an item under another list should
    #: not be offered it again, and reporting that as a failure turns a correct
    #: refusal into a permanently broken-looking sync. Supernote is the case:
    #: a task read from one of its lists must never be written back into
    #: another, or the account gains a second copy.
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


class ConnectorError(RuntimeError):
    """A connector failed in a way the user should see."""


class ConnectorAuthError(ConnectorError):
    """Credentials are expired or revoked; the account needs reconnecting.

    Distinct from ConnectorError because the remedy is different: the user must
    click Reconnect, and retrying on a schedule will never help.
    """


class ConnectorGoneError(ConnectorError):
    """The list or item addressed does not exist at the service any more.

    Distinct from ConnectorError because it is the one failure that will never
    succeed on a retry: nothing about waiting brings a deleted task back. The
    engine acts on it instead of logging the same error every fifteen minutes
    forever. Raise it only for an unambiguous 404 or 410 on an id the service
    itself gave us -- never for a general failure, because the engine may treat
    it as a deletion and that destroys data.
    """


class RateLimitError(ConnectorError):
    """The service asked us to slow down."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Connector(abc.ABC):
    """Base class for every service integration."""

    #: Which service this is, for badges and logging.
    service: ServiceKind
    #: Human-readable name.
    name: str

    def __init__(self, account_id: int, credentials: dict, sync_state: dict | None = None):
        self.account_id = account_id
        self.credentials = credentials
        self.sync_state = sync_state or {}

    # -- Capabilities ---------------------------------------------------------

    @abc.abstractmethod
    def capabilities(self, kind: CollectionKind) -> Capabilities:
        """What this service can represent for tasks, or for calendar events."""

    def supports_kind(self, kind: CollectionKind) -> bool:
        """Whether this service handles tasks, calendars, or both."""
        return True

    def echo_of(self, record: CanonicalRecord, kind: CollectionKind) -> CanonicalRecord:
        """What this service will report back after being told ``record``.

        :class:`Capabilities` says which fields a service can hold at all. This
        says what it does to the ones it can, and it exists because those are
        different problems. Todoist stores a due time as a floating clock face
        and returns no zone; its four priorities cannot express iCalendar's
        nine, so a 2 comes back as a 1. Nothing is lost that the service ever
        claimed to keep, and neither value is an edit -- but both differ from
        what was sent, and the echo check compares what was sent.

        The baseline recorded against a link is therefore taken from this rather
        than from the record itself. Get it wrong in the direction of saying too
        little and the only cost is a redundant write; get it wrong in the other
        direction and a real edit made in that service is mistaken for an echo
        and silently dropped, so an override should only ever describe changes
        the service is *certain* to make.

        Returning the record unchanged, as the default does, is right for any
        service that round-trips faithfully -- Radicale, and Google Calendar
        now that it converts zones properly on the way back.
        """
        return record

    # -- Discovery ------------------------------------------------------------

    @abc.abstractmethod
    def verify(self) -> str:
        """Confirm the credentials work; return the account's identity string.

        Called immediately after connecting so a bad credential surfaces at the
        moment the user can still do something about it, rather than silently at
        3am during a scheduled sync.
        """

    @abc.abstractmethod
    def list_remote_lists(self) -> list[RemoteList]:
        """Every task list and calendar visible to this account."""

    # -- Reading --------------------------------------------------------------

    @abc.abstractmethod
    def pull(
        self,
        remote_list_id: str,
        kind: CollectionKind,
        since: dt.datetime | None = None,
        state: dict | None = None,
    ) -> PullResult:
        """Fetch items from one remote list.

        ``since`` is a hint, not a contract: a connector may ignore it and
        return everything. The engine handles both, but honouring it keeps the
        request count -- and therefore the rate-limit pressure -- down.
        """

    # -- Writing --------------------------------------------------------------

    @abc.abstractmethod
    def create(
        self, remote_list_id: str, record: CanonicalRecord, kind: CollectionKind
    ) -> PushOutcome:
        """Create a new item in a remote list."""

    @abc.abstractmethod
    def update(
        self,
        remote_list_id: str,
        remote_id: str,
        record: CanonicalRecord,
        kind: CollectionKind,
    ) -> PushOutcome:
        """Replace an existing remote item with the canonical version."""

    @abc.abstractmethod
    def delete(self, remote_list_id: str, remote_id: str, kind: CollectionKind) -> PushOutcome:
        """Delete a remote item."""

    # -- Housekeeping ---------------------------------------------------------

    def close(self) -> None:
        """Release any held connections. Called once per sync pass."""
