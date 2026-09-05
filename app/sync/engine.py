"""The sync engine: pull, merge, plan, suppress, push.

One pass over every enabled sync group. The ordering is deliberate and the
suppress step is the one that keeps Task Hub well-behaved: without it, every
pass would rewrite every item to every service, which burns rate limits and --
the failure the user notices -- keeps pushing completed tasks back into
services that had already been told about them.

Deletion is treated with more suspicion than any other operation, because it is
the only one that destroys data. A missing item is read as a deletion only when
the pull was complete, error-free, and not suspiciously empty.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import (
    Connector,
    ConnectorAuthError,
    ConnectorError,
    ConnectorGoneError,
    RateLimitError,
    RemoteItem,
)
from app.crypto import decrypt_json, encrypt_json
from app.db import settings_store
from app.db.models import (
    Account,
    AccountStatus,
    CollectionKind,
    FieldProvenance,
    Item,
    ItemLink,
    ItemStatus,
    ListMapping,
    RemoteList,
    ServiceKind,
    SyncGroup,
    SyncLogEntry,
    SyncOutcome,
    SyncRun,
    Tombstone,
    utcnow,
)
from app.services.ical_model import CanonicalRecord, new_uid
from app.sync import ratelimit
from app.sync.merge import (
    Provenance,
    baseline_fingerprints,
    content_hash,
    merge_remote,
    project,
)

logger = logging.getLogger(__name__)

#: A non-incremental pull returning nothing, when links exist, is far more
#: likely to be a transient API failure than the user deleting everything at
#: once. Deletions are skipped in that case rather than mirrored.
EMPTY_PULL_DELETION_GUARD = True

# --- History retention --------------------------------------------------------
#
# A sync every fifteen minutes is roughly 35,000 runs a year, each with log
# entries. None of it is useful after the fact, and nobody is going to remember
# to clear it out, so the engine sweeps up after itself.

#: Runs are kept if they are among the newest this many, OR newer than
#: KEEP_RUN_DAYS. A run has to fail both tests before it is removed, so a quiet
#: fortnight cannot erase the history and a busy day cannot either.
KEEP_RUNS = 200
KEEP_RUN_DAYS = 30

#: How many items may vanish from one list mid-write before Task Hub stops
#: believing it. A service answering "no such task" for an id it gave us is
#: normally an item someone deleted there a moment ago, and treating it as a
#: deletion is right. The same answer for every task in a list means the LIST
#: went, or the account lost its access -- and acting on that would delete the
#: user's tasks from every other service at once. Above this count nothing is
#: removed and the run says so instead.
MAX_VANISHED_ON_WRITE = 5

#: Tombstones stop a deleted task being recreated by a service that has not yet
#: caught up. Once propagated they are only insurance, but deleting one too
#: early resurrects a task the user deleted -- a visible, annoying failure --
#: while keeping one costs a single small row. Hence a generous window.
KEEP_TOMBSTONE_DAYS = 90


#: Per-account master switches, stored in Account.sync_state. Lets a whole
#: kind be paused for one account without unpicking every individual list.
KINDS_KEY = "kinds_enabled"


def tombstone_keys(links) -> list[str]:
    """The "<account id>:<remote id>" keys a tombstone should remember.

    A deletion has to survive being reported back by a service that has not
    caught up yet, and the UID cannot carry that on its own: Google Tasks,
    Todoist, TickTick, Microsoft To Do and Things 3 have nowhere to store one,
    so a task they report arrives with an empty UID and matches no tombstone at
    all. The account and remote id together always identify it.
    """
    return sorted(
        {f"{link.account_id}:{link.remote_id}" for link in links if link.remote_id}
    )


def account_kind_enabled(account: Account, kind: CollectionKind) -> bool:
    """Whether this account syncs this kind of collection at all."""
    state = account.sync_state or {}
    kinds = state.get(KINDS_KEY) or {}
    return bool(kinds.get(kind.value, True))


def set_account_kind_enabled(
    account: Account, kind: CollectionKind, enabled: bool
) -> None:
    state = dict(account.sync_state or {})
    kinds = dict(state.get(KINDS_KEY) or {})
    kinds[kind.value] = bool(enabled)
    state[KINDS_KEY] = kinds
    # Reassigned rather than mutated: SQLAlchemy does not notice in-place edits
    # to a JSON column, so the change would never reach the database.
    account.sync_state = state


@dataclass
class Participant:
    """One (account, list, collection) triple taking part in a sync group.

    The read and write flags come from the mapping rather than the list,
    because a list can now feed several collections with different settings in
    each -- read into two, write back to only one.
    """

    account: Account
    remote_list: RemoteList
    mapping: ListMapping
    connector: Connector
    kind: CollectionKind

    @property
    def read_enabled(self) -> bool:
        return self.mapping.read_enabled

    @property
    def write_enabled(self) -> bool:
        return self.mapping.write_enabled

    def accepts(self, item) -> bool:
        """Whether this destination should receive a particular item.

        A list synced with the collection is a full member and holds everything
        in it. A list chosen as somewhere to *also* write out to is an
        aggregate: it exists to gather one list's tasks somewhere they can be
        seen and ticked off, and sending it the whole collection would make it a
        second copy of everything instead.

        An item whose origin list is unknown -- anything created before the
        column existed and not reachable by the backfill -- is allowed through.
        Silently withholding writes from an existing destination would be a far
        worse failure than sending one task too many.
        """
        allowed = self.mapping.write_from_list_ids
        if not allowed:
            return True
        origin = getattr(item, "origin_remote_list_id", None)
        if origin is None:
            return True
        return origin in allowed

    @property
    def create_from_remote(self) -> bool:
        """Whether this list may introduce tasks the collection has not seen.

        Older rows predate the column and read as NULL, which has always meant
        "yes" -- so only an explicit False turns it off.
        """
        return self.mapping.create_from_remote is not False

    @property
    def service(self) -> ServiceKind:
        return self.account.service

    @property
    def label(self) -> str:
        return f"{self.account.display_name} / {self.remote_list.name}"


@dataclass
class GroupStats:
    pulled: int = 0
    pushed: int = 0
    skipped: int = 0
    conflicts: int = 0
    errors: int = 0
    created: int = 0
    deleted: int = 0
    messages: list[str] = field(default_factory=list)


# --- Connector construction ---------------------------------------------------


def build_connector(session: Session, account: Account) -> Connector:
    """Instantiate the right connector for an account, with its credentials."""
    credentials = decrypt_json(account.credentials)

    if account.service == ServiceKind.RADICALE:
        from app.connectors.radicale_local import RadicaleConnector

        return RadicaleConnector(account.id, credentials, account.sync_state)

    if account.service == ServiceKind.GOOGLE:
        from app.connectors.google import GoogleConnector
        from app.web.google_setup import get_google_client_credentials

        from app.db import settings_store

        client_id, client_secret = get_google_client_credentials(session)
        return GoogleConnector(
            account.id,
            credentials,
            account.sync_state,
            client_id=client_id,
            client_secret=client_secret,
            # Only used when Google names no zone at all; it normally does.
            default_timezone=settings_store.get(session, settings_store.TIMEZONE),
        )

    if account.service == ServiceKind.OBSIDIAN:
        from app.connectors.obsidian import ObsidianConnector

        # No client credentials and no network: the vault is already on disk,
        # kept there by Obsidian's own client.
        return ObsidianConnector(account.id, credentials, account.sync_state)

    if account.service in (ServiceKind.TODOIST, ServiceKind.TICKTICK):
        from app.db import settings_store
        from app.web.oauth_setup import client_credentials_for

        client_id, client_secret = client_credentials_for(session, account.service)
        # The zone to assume when a service sends a fixed instant without naming
        # one. Without it a timed task would be read as its UTC clock face and
        # drift by the local offset.
        default_timezone = settings_store.get(session, settings_store.TIMEZONE)

        if account.service == ServiceKind.TODOIST:
            from app.connectors.todoist import TodoistConnector

            return TodoistConnector(
                account.id, credentials, account.sync_state,
                client_id=client_id, client_secret=client_secret,
                default_timezone=default_timezone,
            )

        from app.connectors.ticktick import TickTickConnector

        return TickTickConnector(
            account.id, credentials, account.sync_state,
            client_id=client_id, client_secret=client_secret,
            default_timezone=default_timezone,
        )

    if account.service == ServiceKind.MICROSOFT:
        from app.connectors.microsoft import MicrosoftConnector
        from app.db import settings_store
        from app.web.oauth_setup import client_credentials_for

        client_id, client_secret = client_credentials_for(session, account.service)
        return MicrosoftConnector(
            account.id, credentials, account.sync_state,
            client_id=client_id, client_secret=client_secret,
            default_timezone=settings_store.get(session, settings_store.TIMEZONE),
        )

    if account.service == ServiceKind.SUPERNOTE:
        from app.connectors.supernote import SupernoteConnector

        # No client credentials: Supernote has no app registration of any kind,
        # only a session token belonging to the person who signed in.
        return SupernoteConnector(account.id, credentials, account.sync_state)

    if account.service == ServiceKind.APPLE:
        from app.connectors.caldav_remote import AppleConnector
        from app.db import settings_store

        return AppleConnector(
            account.id, credentials, account.sync_state,
            default_timezone=settings_store.get(session, settings_store.TIMEZONE),
        )

    if account.service == ServiceKind.CALDAV:
        from app.connectors.caldav_remote import RemoteCalDAVConnector
        from app.db import settings_store

        return RemoteCalDAVConnector(
            account.id, credentials, account.sync_state,
            default_timezone=settings_store.get(session, settings_store.TIMEZONE),
        )

    if account.service == ServiceKind.THINGS3:
        from app.connectors.things3 import ThingsConnector
        from app.db import settings_store

        return ThingsConnector(
            account.id, credentials, account.sync_state,
            default_timezone=settings_store.get(session, settings_store.TIMEZONE),
        )

    raise ConnectorError(f"No connector is built for {account.service.value} yet.")


def refresh_remote_lists(session: Session, account: Account) -> list:
    """Ask a service what lists it has, and reconcile the stored rows.

    Shared by every service's setup page, because the reconciliation is the part
    with the rule in it and that rule has to be identical everywhere: a list the
    service stops reporting is *marked* unavailable rather than deleted, since
    its mappings and item links are still meaningful if it comes back, and
    dropping them would quietly discard the user's configuration.

    Raises ConnectorError, which the caller is expected to turn into a message.
    """
    connector = build_connector(session, account)
    try:
        found = connector.list_remote_lists()
    finally:
        connector.close()

    known = {
        row.remote_id: row
        for row in session.execute(
            select(RemoteList).where(RemoteList.account_id == account.id)
        ).scalars()
    }
    seen: set[str] = set()

    for info in found:
        seen.add(info.remote_id)
        row = known.get(info.remote_id)
        if row is None:
            session.add(RemoteList(
                account_id=account.id,
                remote_id=info.remote_id,
                name=info.name,
                kind=info.kind,
                colour=info.colour,
                is_default=info.is_default,
                read_only=info.read_only,
            ))
        else:
            row.name = info.name
            row.kind = info.kind
            row.colour = info.colour or row.colour
            row.is_default = info.is_default
            row.read_only = info.read_only
            row.unavailable = False

    for remote_id, row in known.items():
        if remote_id not in seen:
            row.unavailable = True

    account.status = AccountStatus.CONNECTED
    account.status_detail = None
    session.commit()
    return found


def ensure_radicale_account(session: Session) -> Account | None:
    """The system account representing the local CalDAV server.

    Radicale participates as an ordinary connector so the engine has one code
    path, which means it needs an Account row like any other service. It is
    created automatically and never appears in the Services UI.
    """
    from app.web.radicale_admin import get_radicale_credentials

    credentials = get_radicale_credentials(session)
    if credentials is None:
        return None
    username, password = credentials

    account = session.execute(
        select(Account).where(
            Account.service == ServiceKind.RADICALE, Account.slot == 1
        )
    ).scalar_one_or_none()

    payload = {"username": username, "password": password}
    if account is None:
        account = Account(
            service=ServiceKind.RADICALE,
            slot=1,
            label="Built-in CalDAV server",
            remote_identity=username,
            credentials=encrypt_json(payload),
            status=AccountStatus.CONNECTED,
        )
        session.add(account)
    else:
        # Keep it in step with a CalDAV password the user has since changed.
        if decrypt_json(account.credentials) != payload:
            account.credentials = encrypt_json(payload)
        account.remote_identity = username
        account.status = AccountStatus.CONNECTED
    session.commit()
    session.refresh(account)
    return account


# --- Engine -------------------------------------------------------------------


class _GroupIndex:
    """Every link and provenance row for one sync group, loaded once per pass.

    The engine reconciles a group item by item, and each item needs the same
    three lookups: which remote ids it is linked to, which service last touched
    each of its fields, and whether some remote id is already known. Asking the
    database those questions per item is correct and quietly ruinous -- a
    stress run with 70 items across eight services issued 2,141 queries per
    pass, of which 1,286 were the same link lookup repeated. Multiply that by a
    realistic two thousand tasks on a Raspberry Pi writing to an SD card and a
    sync stops finishing.

    So the whole group is read in three queries at the start of the pass and
    kept here. The index is not a cache in the awkward sense -- nothing else
    writes these tables while a group is being reconciled -- but it does have
    to be told about rows the pass itself creates and destroys, which is what
    :meth:`register` and :meth:`forget` are for. Miss one of those and the
    engine would fail to see a link it had just made, so both are called from
    every site that adds or removes one.
    """

    def __init__(self, session: Session, group_id: int):
        self.session = session
        self.group_id = group_id

        links = list(
            session.execute(
                select(ItemLink).where(ItemLink.sync_group_id == group_id)
            ).scalars()
        )
        #: (account_id, remote_id) -> link. The question "have we seen this
        #: remote id before?", asked once per remote item per pass.
        self.by_key: dict[tuple[int, str], ItemLink] = {
            (link.account_id, link.remote_id): link for link in links
        }
        #: item id -> its links, for "where else does this task live?".
        self.by_item: dict[int, list[ItemLink]] = defaultdict(list)
        for link in links:
            self.by_item[link.item_id].append(link)

        rows = list(
            session.execute(
                select(FieldProvenance)
                .join(Item, FieldProvenance.item_id == Item.id)
                .where(Item.sync_group_id == group_id)
            ).scalars()
        )
        #: item id -> field -> the row, so a save can update in place.
        self.provenance_rows: dict[int, dict[str, FieldProvenance]] = defaultdict(dict)
        for row in rows:
            self.provenance_rows[row.item_id][row.field] = row

        #: item id -> the merge's view of provenance. Handed out by reference
        #: on purpose: an item is reconciled once per service reporting it, and
        #: each merge must see what the previous one decided. Returning a copy
        #: would make the second service overwrite the first service's work.
        self.provenance: dict[int, dict[str, Provenance]] = {}
        for item_id, fields in self.provenance_rows.items():
            self.provenance[item_id] = {
                field: Provenance(service=row.source_service, changed_at=row.changed_at)
                for field, row in fields.items()
            }

        #: uid -> item, for services that can store Task Hub's own identifier.
        self.item_by_uid: dict[str, Item] = {}
        for item in session.execute(
            select(Item).where(Item.sync_group_id == group_id)
        ).scalars():
            if item.uid:
                self.item_by_uid[item.uid] = item

    def register(self, link: ItemLink) -> None:
        """Take account of a link the pass has just created."""
        self.by_key[(link.account_id, link.remote_id)] = link
        if link not in self.by_item[link.item_id]:
            self.by_item[link.item_id].append(link)

    def forget(self, link: ItemLink) -> None:
        """Take account of a link the pass has just deleted."""
        self.by_key.pop((link.account_id, link.remote_id), None)
        remaining = [l for l in self.by_item.get(link.item_id, []) if l is not link]
        self.by_item[link.item_id] = remaining

    def remember_item(self, item: Item) -> None:
        if item.uid:
            self.item_by_uid[item.uid] = item


def _parents_first(items: list) -> list:
    """Order tasks so a parent is always pushed before its children.

    A child can only name its parent at an outside service once the parent is
    actually there and has an id. Pushing in storage order would create
    children flat and leave them that way until some later pass happened to
    notice, which for a service like Google -- where a task's parent cannot be
    changed by an ordinary update -- may be never.

    Depth is walked rather than assumed to be one level: Radicale and Todoist
    both hold arbitrary depth. A cycle, which should not exist but would
    otherwise hang this, simply stops being followed.
    """
    by_uid = {item.uid: item for item in items if item.uid}
    depths: dict[int, int] = {}

    for item in items:
        depth, walk, seen = 0, item, set()
        while getattr(walk, "parent_uid", None) and depth < 32:
            if walk.uid in seen:
                break  # A cycle. Treat what we have as the depth and move on.
            seen.add(walk.uid)
            walk = by_uid.get(walk.parent_uid)
            if walk is None:
                break  # Parent is not in this group; treat as top level.
            depth += 1
        depths[id(item)] = depth

    return sorted(items, key=lambda i: depths[id(i)])


class SyncEngine:
    """Runs one complete synchronisation across every connected service.

    A run happens in four passes, in this order, and the order is what makes
    the result correct rather than merely plausible:

    1. **Pull.** Every participating list is read. Nothing is decided yet; this
       pass only gathers what each service currently believes.
    2. **Merge.** Each item is reconciled field by field, in
       :mod:`app.sync.merge`. This is where "who is newer" is settled, and
       where a service echoing back a value it was given earlier is
       distinguished from someone actually editing it.
    3. **Push.** Only fields that genuinely changed are written out, and only
       to services that can represent them. A pass that changed nothing writes
       nothing, which is what allows a stable system to settle instead of
       rewriting every item for ever.
    4. **Deletions.** Handled last and most cautiously, because a deletion that
       turns out to be a service having a bad day is not recoverable. Guards
       live in ``_detect_deletions`` and ``_resolve_vanished``.

    One engine instance handles one run. It holds the database session and the
    :class:`SyncRun` row that everything is logged against, so that the sync
    history page can reconstruct afterwards what happened and why.
    """

    def __init__(self, session: Session):
        self.session = session
        #: The database row this run logs to. None until run_sync starts.
        self.run: SyncRun | None = None
        #: Optional callback, invoked with the run id once the run exists, so
        #: the web interface can show progress while a sync is still going.
        self.on_start = None
        #: Links and provenance for the group currently being reconciled,
        #: loaded once instead of per item. None outside a group's sync, in
        #: which case every lookup below falls back to querying, so nothing
        #: depends on it having been built.
        self._index: _GroupIndex | None = None
        #: Read once per run rather than per task: it cannot change mid-pass,
        #: and it is consulted for every item at every flat service.
        self._separate_setting: bool | None = None
        #: Items a service stopped reporting during this run, collected during
        #: the push pass and judged afterwards by _resolve_vanished. They are
        #: held rather than acted on immediately because "gone" and "deleted"
        #: look identical at the moment they are noticed, and telling them
        #: apart needs the whole picture.
        self._vanished: dict[tuple[int, int], list[tuple[Participant, str, Item]]] = {}

    # -- Logging ---------------------------------------------------------------

    def log(
        self,
        message: str,
        level: str = "info",
        service: str | None = None,
        account_id: int | None = None,
        detail: dict | None = None,
    ) -> None:
        if self.run is None:
            return
        self.session.add(
            SyncLogEntry(
                run_id=self.run.id,
                level=level,
                service=service,
                account_id=account_id,
                message=message,
                detail=detail,
            )
        )
        if level in ("error", "warning"):
            logger.log(
                logging.ERROR if level == "error" else logging.WARNING, "%s", message
            )

    # -- Entry point -----------------------------------------------------------

    def run_sync(self, trigger: str = "scheduled") -> SyncRun:
        run = SyncRun(trigger=trigger, outcome=SyncOutcome.RUNNING)
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        self.run = run
        if self.on_start is not None:
            self.on_start(run.id)

        ensure_radicale_account(self.session)

        groups = (
            self.session.execute(select(SyncGroup).where(SyncGroup.enabled.is_(True)))
            .scalars()
            .all()
        )

        if not groups:
            self.log("No sync groups are configured, so there is nothing to sync.")
            run.outcome = SyncOutcome.SUCCESS
            run.finished_at = utcnow()
            self.session.commit()
            return run

        total = GroupStats()
        for group in groups:
            group_name = group.name  # Read before any rollback expires the row.
            try:
                stats = self.sync_group(group)
            except Exception as exc:  # noqa: BLE001 - one group must not stop the rest
                # The session may be in a failed transaction, in which case even
                # reading group.name to build the message would raise again and
                # take the whole run with it -- which is how three runs ended up
                # stuck reporting "running" forever.
                logger.exception("Sync group %s failed", group_name)
                try:
                    self.session.rollback()
                    self.log(
                        f"Sync group {group_name!r} failed: {exc}", level="error"
                    )
                    self.session.commit()
                except Exception:  # noqa: BLE001
                    logger.exception("Could not record the failure of %s", group_name)
                total.errors += 1
                continue
            total.pulled += stats.pulled
            total.pushed += stats.pushed
            total.skipped += stats.skipped
            total.conflicts += stats.conflicts
            total.errors += stats.errors
            total.created += stats.created
            total.deleted += stats.deleted

        # Whatever happened above, the run is finished and must say so. A row
        # left claiming to be running blocks the next manual sync and shows a
        # spinner that never stops.
        try:
            self.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        run = self.session.get(SyncRun, run.id) or run

        run.items_pulled = total.pulled
        run.items_pushed = total.pushed
        run.items_skipped = total.skipped
        run.conflicts = total.conflicts
        run.errors = total.errors
        run.outcome = (
            SyncOutcome.SUCCESS
            if total.errors == 0
            else (SyncOutcome.PARTIAL if total.pushed or total.pulled else SyncOutcome.FAILED)
        )
        run.finished_at = utcnow()
        self.session.commit()

        self.prune_history()
        return run

    # -- Housekeeping ----------------------------------------------------------

    def prune_history(self) -> None:
        """Trim sync history and expired tombstones.

        Runs after every pass rather than on a schedule of its own: the work is
        tiny, and tying it to the sync means it cannot silently stop happening
        while syncing carries on.
        """
        now = utcnow()
        try:
            cutoff = now - dt.timedelta(days=KEEP_RUN_DAYS)
            keep_ids = set(
                self.session.execute(
                    select(SyncRun.id).order_by(SyncRun.started_at.desc()).limit(KEEP_RUNS)
                ).scalars()
            )
            stale = list(
                self.session.execute(
                    select(SyncRun).where(
                        SyncRun.started_at < cutoff,
                        SyncRun.id.notin_(keep_ids or {0}),
                    )
                ).scalars()
            )
            for old_run in stale:
                # Log entries go with it: the relationship cascades, so they
                # never outlive the run they describe.
                self.session.delete(old_run)

            tomb_cutoff = now - dt.timedelta(days=KEEP_TOMBSTONE_DAYS)
            expired = list(
                self.session.execute(
                    select(Tombstone).where(
                        Tombstone.propagated.is_(True),
                        Tombstone.deleted_at < tomb_cutoff,
                    )
                ).scalars()
            )
            for tombstone in expired:
                self.session.delete(tombstone)

            if stale or expired:
                self.session.commit()
                logger.info(
                    "Pruned %d old sync run(s) and %d expired tombstone(s)",
                    len(stale), len(expired),
                )
        except Exception:  # noqa: BLE001 - housekeeping must never fail a sync
            logger.exception("History pruning failed; syncing is unaffected")
            self.session.rollback()

    # -- One group -------------------------------------------------------------

    def participants(self, group: SyncGroup) -> list[Participant]:
        """Every list taking part in this group, each with a live connector.

        A participant is one remote list plus the connector that can talk to
        it, and whether it is being read from, written to, or both. Building
        them all up front means a service that cannot be reached is discovered
        before any merging starts, rather than half way through a run with some
        items already reconciled against a partial picture.

        Connectors opened here must be closed by the caller. ``sync_group``
        does so on every path, including the early return when a group has too
        few members to reconcile.
        """
        mappings = (
            self.session.execute(
                select(ListMapping).where(ListMapping.sync_group_id == group.id)
            )
            .scalars()
            .all()
        )

        built: list[Participant] = []
        for mapping in mappings:
            if not (mapping.read_enabled or mapping.write_enabled):
                continue
            remote_list = self.session.get(RemoteList, mapping.remote_list_id)
            if remote_list is None:
                continue
            account = self.session.get(Account, remote_list.account_id)
            if account is None or not account.enabled:
                continue
            if not account_kind_enabled(account, group.kind):
                continue
            if account.status in (AccountStatus.DISABLED, AccountStatus.NEEDS_AUTH):
                self.log(
                    f"Skipping {account.display_name}: it needs reconnecting.",
                    level="warning",
                    service=account.service.value,
                    account_id=account.id,
                )
                continue

            try:
                connector = build_connector(self.session, account)
            except ConnectorError as exc:
                self.log(
                    f"Could not start {account.display_name}: {exc}",
                    level="error",
                    service=account.service.value,
                    account_id=account.id,
                )
                continue

            built.append(
                Participant(
                    account=account,
                    remote_list=remote_list,
                    mapping=mapping,
                    connector=connector,
                    kind=group.kind,
                )
            )
        return built

    def sync_group(self, group: SyncGroup) -> GroupStats:
        """Reconcile one sync group: pull, merge, push, then deletions.

        A group is a set of lists that should agree with each other. Fewer than
        two connected lists is not an error -- it is what a half-finished setup
        looks like, and it is reported and skipped rather than treated as a
        failure, because a group nobody has finished configuring should not
        make a whole run look broken.

        Each group is independent, so one service being down affects only the
        groups it belongs to. The returned statistics are what the sync history
        page shows.
        """
        stats = GroupStats()
        # Three queries now, instead of several per item later on.
        self._index = _GroupIndex(self.session, group.id)
        parts = self.participants(group)

        if len(parts) < 2:
            self.log(
                f"Group {group.name!r} has fewer than two connected lists, so "
                "there is nothing to reconcile."
            )
            for part in parts:
                part.connector.close()
            self._index = None
            return stats

        try:
            pulls = self.pull_all(parts, stats)
            self.merge_all(group, parts, pulls, stats)
            self.push_all(group, parts, stats)
        finally:
            for part in parts:
                try:
                    part.connector.close()
                except Exception:  # noqa: BLE001
                    pass
            self.session.commit()
            # Discarded with the group it describes. Anything running after
            # this -- deletion propagation, the run-level tidy-up -- belongs to
            # no single group, and an index left behind would answer their
            # questions with one group's data.
            self._index = None

        self.log(
            f"Group {group.name!r}: pulled {stats.pulled}, pushed {stats.pushed}, "
            f"skipped {stats.skipped}, created {stats.created}, "
            f"deleted {stats.deleted}, conflicts {stats.conflicts}."
        )
        return stats

    # -- Pull ------------------------------------------------------------------

    def pull_all(
        self, parts: list[Participant], stats: GroupStats
    ) -> dict[int, tuple[Participant, list[RemoteItem], bool]]:
        """Fetch from every readable list.

        Returns remote_list_id -> (part, items, complete). Keyed by list rather
        than by account: one account can now have several lists in the same
        collection -- read "Grocery Shopping" and also watch "Shared Grocery
        List" for completions -- and keying by account made the second pull
        overwrite the first, so one of the two lists was fetched and then
        silently thrown away before anything was merged.
        """
        pulls: dict[int, tuple[Participant, list[RemoteItem], bool]] = {}

        for part in parts:
            if not part.read_enabled:
                continue

            service = part.service.value
            if not ratelimit.acquire(service):
                remaining = ratelimit.cooldown_remaining(service)
                self.log(
                    f"{part.label}: skipped, {service} is rate limited for another "
                    f"{remaining:.0f}s.",
                    level="warning",
                    service=service,
                    account_id=part.account.id,
                )
                stats.errors += 1
                continue

            # Per-list state a connector asked us to remember from last time.
            # Keyed by list rather than by account because one account can have
            # several lists in a collection, and a shared bucket would let one
            # list's bookkeeping overwrite another's.
            state_key = str(part.remote_list.id)
            saved_state = (part.account.sync_state or {}).get(state_key)

            try:
                result = part.connector.pull(
                    part.remote_list.remote_id,
                    part.kind,
                    since=None,
                    state=saved_state,
                )
            except RateLimitError as exc:
                ratelimit.note_rate_limit(service, exc.retry_after)
                self.log(
                    f"{part.label}: {exc}", level="warning",
                    service=service, account_id=part.account.id,
                )
                stats.errors += 1
                continue
            except ConnectorAuthError as exc:
                part.account.status = AccountStatus.NEEDS_AUTH
                part.account.status_detail = str(exc)
                self.log(
                    f"{part.label} needs reconnecting: {exc}", level="error",
                    service=service, account_id=part.account.id,
                )
                stats.errors += 1
                continue
            except ConnectorError as exc:
                part.account.status = AccountStatus.ERROR
                part.account.status_detail = str(exc)
                self.log(
                    f"{part.label}: {exc}", level="error",
                    service=service, account_id=part.account.id,
                )
                stats.errors += 1
                continue

            self._persist_refreshed_credentials(part)

            if result.errors:
                for message in result.errors:
                    self.log(
                        f"{part.label}: {message}", level="warning",
                        service=service, account_id=part.account.id,
                    )
                stats.errors += len(result.errors)

            part.account.status = AccountStatus.CONNECTED
            part.account.status_detail = None
            part.account.last_sync_at = utcnow()

            if result.sync_state is not None:
                # Reassigned rather than mutated: SQLAlchemy only notices a JSON
                # column changing when the attribute itself is set.
                merged = dict(part.account.sync_state or {})
                merged[state_key] = result.sync_state
                part.account.sync_state = merged

            complete = not result.incremental and not result.errors
            pulls[part.remote_list.id] = (part, result.items, complete)
            stats.pulled += len(result.items)

        self.session.commit()
        self._publish_progress(stats)
        return pulls

    def _persist_refreshed_credentials(self, part: Participant) -> None:
        """Save tokens a connector rotated during the pass.

        Google hands back a new access token roughly hourly, and occasionally a
        new refresh token. Losing those would mean a fresh token request on
        every single call.
        """
        connector = part.connector
        if getattr(connector, "credentials_changed", False):
            part.account.credentials = encrypt_json(connector.current_credentials())

    # -- Merge -----------------------------------------------------------------

    def merge_all(
        self,
        group: SyncGroup,
        parts: list[Participant],
        pulls: dict[int, tuple[Participant, list[RemoteItem], bool]],
        stats: GroupStats,
    ) -> None:
        """Reconcile everything that was pulled, one item at a time.

        For each remote item this decides which local item it is -- by existing
        link, then by identity, then by title as a last resort -- and merges it
        field by field. New items are created; items that vanished are noted
        for the deletion pass rather than acted on here.

        The ``complete`` flag on each pull matters more than it looks. It says
        whether the service returned its whole list or only part of it. An item
        missing from a partial listing means nothing at all, so deletion
        detection is skipped for that list entirely -- otherwise a service
        paginating, filtering or simply having a bad day would read as the user
        deleting everything.
        """
        for _list_id, (part, items, complete) in pulls.items():
            seen_remote_ids: set[str] = set()

            for remote in items:
                seen_remote_ids.add(remote.remote_id)

                if remote.deleted:
                    self._handle_remote_deletion(group, part, remote.remote_id, stats)
                    continue

                item = self._resolve_item(group, part, remote, stats)
                if item is None:
                    continue

                link = self._link_for(item, part, remote.remote_id, group.id)
                provenance = self._load_provenance(item)
                canonical = self._item_to_record(item)

                result = merge_remote(
                    canonical,
                    remote,
                    part.service,
                    provenance,
                    baseline=link.last_pushed_fields or None,
                )

                if result.changed:
                    self._record_to_item(canonical, item)
                    item.updated_at = utcnow()
                    self._save_provenance(item, provenance)

                # Outside the changed check on purpose: a task moved under a new
                # parent may be identical in every other field, and the merge
                # engine -- which compares values -- would report no change.
                caps = part.connector.capabilities(part.kind)
                if caps.supports_parent:
                    resolved = self._parent_uid_from(remote, part)
                    # An unresolved parent means the parent has not been mapped
                    # here yet, not that there is none, so it is left alone.
                    if resolved is not None or not remote.record.parent_remote_id:
                        canonical.parent_uid = resolved
                        self.apply_parent(item, canonical, caps)
                if result.conflicts:
                    stats.conflicts += len(result.conflicts)
                    self.log(
                        f"{part.label}: kept the newer value for "
                        f"{', '.join(result.conflicts)} on {item.title!r}.",
                        level="info", service=part.service.value,
                        account_id=part.account.id,
                    )

                link.remote_etag = remote.etag
                link.remote_updated_at = remote.remote_updated_at
                link.last_seen_at = utcnow()

                # If this service already holds exactly what we would send it,
                # record that as the pushed state now. Without this, an item
                # just pulled from a service is immediately written back to that
                # same service with identical content -- so a first sync of a
                # large Google Calendar would PATCH every one of the user's real
                # events for no reason, taking many minutes and touching data it
                # had no business touching.
                caps = part.connector.capabilities(part.kind)
                # Compared over everything the service can *hold*, because the
                # question is whether it already agrees with us. Recorded over
                # what it can *write*, because that is what the push step will
                # compare against -- record the wider hash here and the two
                # never match, so every item is pushed on every pass for ever.
                if content_hash(remote.record, caps) == content_hash(canonical, caps):
                    link.last_pushed_hash = content_hash(canonical, caps, caps.push_fields())
                    link.last_pushed_fields = baseline_fingerprints(
                        part.connector.echo_of(canonical, part.kind), caps
                    )

            if complete:
                self._detect_deletions(group, part, seen_remote_ids, stats)

        self.session.commit()

    def _detect_deletions(
        self,
        group: SyncGroup,
        part: Participant,
        seen_remote_ids: set[str],
        stats: GroupStats,
    ) -> None:
        """Treat items absent from a complete listing as deleted remotely."""
        group_links = list(
            self.session.execute(
                select(ItemLink).where(
                    ItemLink.account_id == part.account.id,
                    ItemLink.remote_list_id == part.remote_list.id,
                    ItemLink.sync_group_id == group.id,
                )
            ).scalars()
        )

        if not group_links:
            return

        if EMPTY_PULL_DELETION_GUARD and not seen_remote_ids:
            # An empty response with links on record is almost always a
            # transient failure, and acting on it would delete the user's
            # entire list everywhere. Refuse, and say so.
            self.log(
                f"{part.label} returned no items at all but {len(group_links)} were "
                "known. Treating this as a temporary failure rather than a mass "
                "deletion; nothing was removed.",
                level="warning", service=part.service.value, account_id=part.account.id,
            )
            stats.errors += 1
            return

        for link in group_links:
            if link.remote_id in seen_remote_ids:
                continue
            self._handle_remote_deletion(group, part, link.remote_id, stats)

    def _handle_remote_deletion(
        self, group: SyncGroup, part: Participant, remote_id: str, stats: GroupStats
    ) -> None:
        link = self._known_link(part.account.id, remote_id, group.id)
        if link is None:
            return

        item = self.session.get(Item, link.item_id)
        # Read the remaining links BEFORE the deleted one goes, so the tombstone
        # records every id this task answered to anywhere.
        known = tombstone_keys(self._all_links(link.item_id)) if item else []
        if self._index is not None:
            self._index.forget(link)
        self.session.delete(link)
        if item is None:
            return

        self.session.add(
            Tombstone(
                uid=item.uid,
                sync_group_id=group.id,
                deleted_by_service=part.service,
                remote_ids=known,
            )
        )
        self.log(
            f"{part.label}: {item.title!r} was deleted; removing it everywhere.",
            service=part.service.value, account_id=part.account.id,
        )
        stats.deleted += 1

    # -- Item identity ---------------------------------------------------------

    def _tombstoned(
        self, group: SyncGroup, part: Participant, remote: RemoteItem
    ) -> bool:
        """Whether this remote item is one already deleted in this collection.

        Two separate tests, because no single one covers every service. The UID
        works for a CalDAV store, which round-trips it. Everything else has
        nowhere to keep a UID, so a task Todoist or Google Tasks reports after
        the deletion arrives with an empty one -- and matching an empty UID
        against a tombstone finds nothing, which is how a task deleted in one
        service came back on the very next pull from another. The remote id
        recorded at deletion time is what catches those.
        """
        candidates = list(
            self.session.execute(
                select(Tombstone).where(Tombstone.sync_group_id == group.id)
            ).scalars()
        )
        if not candidates:
            return False

        # An empty UID must never match: it is what a service that cannot store
        # one always sends, so treating it as equal to a tombstone's UID would
        # block every incoming task rather than one deleted task.
        uid = (remote.record.uid or "").strip()
        if uid and any(tomb.uid == uid for tomb in candidates):
            return True

        key = f"{part.account.id}:{remote.remote_id}"
        return any(key in (tomb.remote_ids or []) for tomb in candidates)

    def _resolve_item(
        self, group: SyncGroup, part: Participant, remote: RemoteItem, stats: GroupStats
    ) -> Item | None:
        """Find or create the canonical item a remote item corresponds to."""
        link = self.session.execute(
            select(ItemLink).where(
                ItemLink.account_id == part.account.id,
                ItemLink.remote_id == remote.remote_id,
                ItemLink.sync_group_id == group.id,
            )
        ).scalar_one_or_none() if self._index is None else self._index.by_key.get(
            (part.account.id, remote.remote_id)
        )
        if link is not None:
            return self.session.get(Item, link.item_id)

        # From here on the remote item is one this collection has never seen.
        # A list configured to report changes only stops here: it is a place
        # tasks are written to, and something appearing in it that Task Hub did
        # not put there is not a new task for everyone else -- it is a local
        # addition to a copy, and pushing it back into the original is exactly
        # the mirroring this setting exists to prevent.
        if not part.create_from_remote:
            stats.skipped += 1
            return None

        # Never resurrect something already deleted elsewhere in this pass.
        if self._tombstoned(group, part, remote):
            return None

        caps = part.connector.capabilities(part.kind)

        if caps.stores_uid:
            if self._index is not None:
                existing = self._index.item_by_uid.get(remote.record.uid)
            else:
                existing = self.session.execute(
                    select(Item).where(
                        Item.uid == remote.record.uid, Item.sync_group_id == group.id
                    )
                ).scalars().first()
            if existing is not None:
                return existing

        # First contact between two services that already hold the same task.
        # Matching on title is a heuristic, so it is deliberately strict: exact
        # match after normalisation, only among items not already linked to this
        # account, and only when exactly one candidate matches. A wrong guess
        # here would merge two unrelated tasks, which is worse than creating a
        # duplicate the user can delete.
        matched = self._match_by_title(group, part, remote)
        if matched is not None:
            return matched

        # Where an item ORIGINALLY came from, which drives the coloured badge in
        # the task viewer. A CalDAV store round-trips the marker Task Hub wrote,
        # so a Google task that reached Radicale still reports Google when it is
        # read back. Anything else can only have originated at the service
        # reporting it.
        if caps.carries_origin:
            origin = remote.record.origin_service
        else:
            origin = part.service

        item = Item(
            uid=remote.record.uid or new_uid(),
            sync_group_id=group.id,
            kind=part.kind,
            origin_service=origin,
            origin_account_id=part.account.id,
            origin_remote_list_id=part.remote_list.id,
        )
        self._record_to_item(remote.record, item)
        caps = part.connector.capabilities(part.kind)
        if caps.supports_parent:
            remote.record.parent_uid = self._parent_uid_from(remote, part)
            self.apply_parent(item, remote.record, caps)
        item.origin_service = origin
        self.session.add(item)
        self.session.flush()
        # A service that stores UIDs may report this same task on a later
        # participant in this very pass; the index has to know about it or a
        # second copy would be created.
        if self._index is not None:
            self._index.remember_item(item)
        stats.created += 1
        return item

    def _match_by_title(
        self, group: SyncGroup, part: Participant, remote: RemoteItem
    ) -> Item | None:
        needle = (remote.record.title or "").strip().casefold()
        if not needle:
            return None

        candidates = (
            self.session.execute(
                select(Item).where(Item.sync_group_id == group.id)
            )
            .scalars()
            .all()
        )
        matches = []
        for candidate in candidates:
            if (candidate.title or "").strip().casefold() != needle:
                continue
            already = self.session.execute(
                select(ItemLink).where(
                    ItemLink.item_id == candidate.id,
                    ItemLink.account_id == part.account.id,
                )
            ).scalar_one_or_none()
            if already is None:
                matches.append(candidate)

        if len(matches) == 1:
            return matches[0]
        return None

    def _all_links(self, item_id: int) -> list[ItemLink]:
        """Every link this item has, in every account."""
        if self._index is not None:
            return list(self._index.by_item.get(item_id, ()))
        return list(
            self.session.execute(
                select(ItemLink).where(ItemLink.item_id == item_id)
            ).scalars()
        )

    def _links_for_account(self, item_id: int, account_id: int) -> list[ItemLink]:
        """Every link between one item and one account.

        Normally there is exactly one, but a service can legitimately report the
        same logical item under more than one id -- a recurring Google Calendar
        event and its modified occurrences share an iCalUID. Returning a list
        rather than assuming one is what keeps that from crashing the whole
        sync group.
        """
        if self._index is not None:
            return [l for l in self._index.by_item.get(item_id, ())
                    if l.account_id == account_id]
        return list(
            self.session.execute(
                select(ItemLink).where(
                    ItemLink.item_id == item_id, ItemLink.account_id == account_id
                )
            ).scalars()
        )

    def _links_in_list(
        self, item_id: int, account_id: int, remote_list_id: int
    ) -> list[ItemLink]:
        """The links tying one item to one specific list in one account.

        Write-back targets a list, not an account, and one account can now hold
        several lists that take writes from the same collection -- read from
        "Grocery Shopping" but push the merged result into "Shared Grocery
        List". Matching on the account alone would hand the push the link
        belonging to a different list and make it update the wrong task there.

        Links written before lists could differ carry no list of their own.
        There was exactly one list per account per collection then, so such a
        link can only mean this one; it is claimed rather than ignored, which
        stops the first sync after an upgrade creating a duplicate of every
        task.
        """
        links = self._links_for_account(item_id, account_id)
        exact = [link for link in links if link.remote_list_id == remote_list_id]
        if exact:
            return exact
        orphans = [link for link in links if link.remote_list_id is None]
        for link in orphans:
            link.remote_list_id = remote_list_id
        return orphans

    def _link_for(
        self, item: Item, part: Participant, remote_id: str, group_id: int
    ) -> ItemLink:
        link = self._known_link(part.account.id, remote_id, group_id)
        if link is None:
            link = ItemLink(
                item_id=item.id,
                account_id=part.account.id,
                remote_list_id=part.remote_list.id,
                sync_group_id=group_id,
                remote_id=remote_id,
            )
            self.session.add(link)
            self.session.flush()
            if self._index is not None:
                self._index.register(link)
        return link

    def _known_link(
        self, account_id: int, remote_id: str, group_id: int
    ) -> ItemLink | None:
        """The link for one remote id, from the index when there is one.

        Asked once per remote item per pass, and previously twice -- item
        resolution and link creation each looked it up separately.
        """
        if self._index is not None:
            return self._index.by_key.get((account_id, remote_id))
        return self.session.execute(
            select(ItemLink).where(
                ItemLink.account_id == account_id,
                ItemLink.remote_id == remote_id,
                ItemLink.sync_group_id == group_id,
            )
        ).scalar_one_or_none()

    # -- Push ------------------------------------------------------------------

    def push_all(
        self, group: SyncGroup, parts: list[Participant], stats: GroupStats
    ) -> None:
        items = (
            self.session.execute(
                select(Item).where(Item.sync_group_id == group.id)
            )
            .scalars()
            .all()
        )

        writable = [p for p in parts if p.write_enabled]

        # Items a service refused to accept a write for because the thing being
        # written to is gone. Collected rather than acted on one at a time, so
        # that a whole list disappearing can be told apart from one task being
        # deleted -- see _resolve_vanished.
        self._vanished = {}

        # Parents first. A child can only name its parent at a service once the
        # parent is actually there, so pushing in storage order would leave
        # children flat until some later pass happened to correct them.
        items = _parents_first(items)

        for index, item in enumerate(items, start=1):
            record = self._item_to_record(item)
            for part in writable:
                if not part.accepts(item):
                    continue
                self._push_one(group, part, item, record, stats)

            # Publish progress periodically so the history page shows a long
            # first sync advancing instead of looking frozen.
            if index % 25 == 0:
                self._publish_progress(stats)

        self._resolve_vanished(group, stats)
        self._propagate_deletions(group, writable, stats)
        self.session.commit()

    def _push_one(
        self,
        group: SyncGroup,
        part: Participant,
        item: Item,
        record: CanonicalRecord,
        stats: GroupStats,
    ) -> None:
        caps = part.connector.capabilities(part.kind)

        # A service that can change nothing is never asked to. Obsidian with
        # write-back switched off is the case this exists for: its mapping can
        # still carry a write tick, and without this the engine would offer it
        # every changed task on every pass, have each one refused, and report
        # the whole run as partial for doing exactly what was asked.
        if not caps.push_fields() and not caps.can_create and not caps.can_delete:
            stats.skipped += 1
            return

        if self._skip_as_child(item, caps):
            stats.skipped += 1
            return

        links = self._links_in_list(item.id, part.account.id, part.remote_list.id)
        link = links[0] if links else None

        # Scoped to what this service may actually change. A due date edited
        # elsewhere must not queue a write to a service that only writes
        # completions -- it would be attempted, and refused, on every pass.
        desired_hash = content_hash(record, caps, caps.push_fields())

        # A service that folds its subtasks in has to notice when one of them
        # changes. The hash covers this service's own fields, and the children
        # are not among them, so renaming a step or ticking one off would
        # otherwise leave the parent looking untouched and never be written.
        folded = self._folded_children(item, caps)
        if folded is not None:
            desired_hash = f"{desired_hash}:{folded}"

        # The suppression step. If what this service already holds matches what
        # we would send, send nothing. This is what stops completed tasks being
        # rewritten into every service on every single pass.
        if link is not None and link.last_pushed_hash == desired_hash:
            stats.skipped += 1
            return

        if link is None and item.status == ItemStatus.COMPLETED:
            # Do not create a brand-new task in a service just to immediately
            # mark it done. That is noise the user never asked for, and in a
            # service with notifications it is noise that pings their phone.
            stats.skipped += 1
            return

        if link is None and not caps.can_create:
            stats.skipped += 1
            return

        service = part.service.value
        if not ratelimit.acquire(service):
            stats.skipped += 1
            return

        projected = project(record, caps)
        projected.kind = part.kind
        projected.parent_uid = record.parent_uid if caps.supports_parent else None
        projected.parent_remote_id = (
            self._parent_remote_id(item, part) if caps.supports_parent else None
        )
        if (not caps.supports_parent and not self._subtasks_separate()
                and getattr(caps, "notes_visible", True)):
            # This service cannot nest, so its copy of a parent carries the
            # steps itself. The children are not sent as tasks of their own --
            # see _skip_as_child.
            projected.children = [
                self._item_to_record(child) for child in self._children_of(item)
            ]

        try:
            if link is None:
                outcome = part.connector.create(
                    part.remote_list.remote_id, projected, part.kind
                )
            else:
                outcome = part.connector.update(
                    part.remote_list.remote_id, link.remote_id, projected, part.kind
                )
        except RateLimitError as exc:
            ratelimit.note_rate_limit(service, exc.retry_after)
            stats.errors += 1
            return
        except ConnectorAuthError as exc:
            part.account.status = AccountStatus.NEEDS_AUTH
            part.account.status_detail = str(exc)
            stats.errors += 1
            return
        except ConnectorGoneError as exc:
            if link is None:
                # Nothing was being addressed but the list itself, so the list
                # is what has gone. Nothing to clean up on the item.
                self.log(
                    f"{part.label}: could not add {item.title!r}: {exc}",
                    level="error", service=service, account_id=part.account.id,
                )
                stats.errors += 1
                return
            # The service gave us this id and now denies it exists. That is a
            # deletion made there, but it is not acted on until the whole pass
            # is in and a single lost task can be told from a lost list.
            key = (part.account.id, part.remote_list.id)
            self._vanished.setdefault(key, []).append((part, link.remote_id, item))
            return
        except ConnectorError as exc:
            self.log(
                f"{part.label}: could not write {item.title!r}: {exc}",
                level="error", service=service, account_id=part.account.id,
            )
            stats.errors += 1
            return

        self._persist_refreshed_credentials(part)

        if outcome.skipped:
            # Declined rather than failed: nothing is wrong and nothing needs
            # saying. Logging it would repeat on every pass for every item.
            stats.skipped += 1
            return

        if not outcome.ok:
            self.log(
                f"{part.label}: could not write {item.title!r}: {outcome.error}",
                level="error", service=service, account_id=part.account.id,
            )
            stats.errors += 1
            return

        if link is None:
            link = ItemLink(
                item_id=item.id,
                account_id=part.account.id,
                remote_list_id=part.remote_list.id,
                sync_group_id=group.id,
                remote_id=outcome.remote_id or "",
            )
            self.session.add(link)

        link.remote_id = outcome.remote_id or link.remote_id
        # Registered only now: the index is keyed by remote id, and until the
        # service answered there was no id to key it under.
        if self._index is not None:
            self._index.register(link)
        link.remote_etag = outcome.etag
        link.remote_updated_at = outcome.remote_updated_at or utcnow()
        link.last_pushed_hash = desired_hash
        # Remember what this service will say when asked, field by field, so the
        # next pull can tell a real edit from an echo of our own write. Not what
        # it was told: a service that stores a due time without its zone, or on
        # a coarser priority scale, answers with something that differs from
        # what we sent through no edit of anyone's.
        link.last_pushed_fields = baseline_fingerprints(
            part.connector.echo_of(record, part.kind), caps
        )
        link.last_seen_at = utcnow()
        stats.pushed += 1

    def _resolve_vanished(self, group: SyncGroup, stats: GroupStats) -> None:
        """Act on writes a service refused because the target no longer exists.

        Retrying such a write is pointless -- no amount of waiting brings a
        deleted task back -- and left alone it logs the same error every fifteen
        minutes for as long as Task Hub runs. So it is settled here.

        One or two tasks missing from a list means someone deleted them at that
        service, and they are treated exactly like a deletion noticed on a pull:
        tombstoned, and removed everywhere on this same pass. A whole list's
        worth missing at once means something else -- the list was deleted, or
        the account lost access to it -- and deleting the user's tasks from
        every other service on that evidence is the one mistake that cannot be
        undone. Above MAX_VANISHED_ON_WRITE nothing is removed and the run says
        plainly why.
        """
        for entries in self._vanished.values():
            part = entries[0][0]
            if len(entries) > MAX_VANISHED_ON_WRITE:
                self.log(
                    f"{part.label}: {len(entries)} tasks were refused as no longer "
                    "existing. That points at the list itself being gone rather "
                    "than the tasks, so nothing has been deleted anywhere. Check "
                    "the list still exists and is still shared with this account.",
                    level="warning", service=part.service.value,
                    account_id=part.account.id,
                )
                stats.errors += 1
                continue

            # _handle_remote_deletion writes the tombstone and logs the line
            # the user reads; nothing extra is said here.
            for _part, remote_id, _item in entries:
                self._handle_remote_deletion(group, part, remote_id, stats)

        self._vanished = {}
        # The session does not autoflush, so a tombstone added above would be
        # invisible to the query _propagate_deletions runs next -- and the
        # deletion would sit unpropagated until some later pass happened to
        # look again.
        self.session.flush()

    def _propagate_deletions(
        self, group: SyncGroup, writable: list[Participant], stats: GroupStats
    ) -> None:
        tombstones = (
            self.session.execute(
                select(Tombstone).where(
                    Tombstone.sync_group_id == group.id,
                    Tombstone.propagated.is_(False),
                )
            )
            .scalars()
            .all()
        )

        for tombstone in tombstones:
            item = self.session.execute(
                select(Item).where(Item.uid == tombstone.uid)
            ).scalars().first()
            if item is None:
                tombstone.propagated = True
                continue

            links = (
                self.session.execute(
                    select(ItemLink).where(ItemLink.item_id == item.id)
                )
                .scalars()
                .all()
            )
            for link in links:
                part = next(
                    (p for p in writable if p.account.id == link.account_id), None
                )
                if part is None:
                    continue
                caps = part.connector.capabilities(part.kind)
                if not caps.can_delete:
                    continue
                try:
                    part.connector.delete(
                        part.remote_list.remote_id, link.remote_id, part.kind
                    )
                except ConnectorError as exc:
                    self.log(
                        f"{part.label}: could not delete {item.title!r}: {exc}",
                        level="warning", service=part.service.value,
                    )
                    continue
                self.session.delete(link)

            self.session.delete(item)
            tombstone.propagated = True

    def _publish_progress(self, stats: GroupStats) -> None:
        """Write running totals to the SyncRun row mid-pass."""
        if self.run is None:
            return
        self.run.items_pulled = max(self.run.items_pulled, stats.pulled)
        self.run.items_pushed = stats.pushed
        self.run.items_skipped = stats.skipped
        self.run.errors = stats.errors
        self.session.commit()

    # -- Record <-> row conversion --------------------------------------------

    @staticmethod
    def _item_to_record(item: Item) -> CanonicalRecord:  # noqa: D401
        return CanonicalRecord(
            uid=item.uid,
            kind=item.kind,
            title=item.title,
            notes=item.notes,
            status=item.status,
            completed_at=item.completed_at,
            due_date=item.due_date,
            due_time=item.due_time,
            due_tz=item.due_tz,
            start_date=item.start_date,
            start_time=item.start_time,
            start_tz=item.start_tz,
            end_date=item.end_date,
            end_time=item.end_time,
            end_tz=item.end_tz,
            all_day=item.all_day,
            location=item.location,
            priority=item.priority,
            parent_uid=item.parent_uid,
            rrule=item.rrule,
            tags=list(item.tags or []),
            origin_service=item.origin_service,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    def _subtasks_separate(self) -> bool:
        """Whether flat services should get subtasks as top-level tasks."""
        if self._separate_setting is None:
            self._separate_setting = settings_store.get_bool(
                self.session, settings_store.SUBTASKS_AS_SEPARATE
            )
        return self._separate_setting

    def _children_of(self, item: Item) -> list[Item]:
        """The tasks belonging to this one, within the same sync group."""
        if not item.uid:
            return []
        return list(self.session.execute(
            select(Item).where(
                Item.parent_uid == item.uid,
                Item.sync_group_id == item.sync_group_id,
            ).order_by(Item.id)
        ).scalars())

    def _folded_children(self, item: Item, caps) -> str | None:
        """A fingerprint of the steps this service will be given, or None.

        None means this service is not being given any -- it can nest, or the
        user has asked for separate tasks -- and the ordinary hash stands.
        """
        if (caps.supports_parent or self._subtasks_separate()
                or not getattr(caps, "notes_visible", True)):
            return None
        children = self._children_of(item)
        if not children:
            return None
        parts = [f"{c.title}\x1f{c.status.value}" for c in children]
        return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:16]

    def _skip_as_child(self, item: Item, caps) -> bool:
        """Whether this task should be left out of a flat service entirely.

        A child sent to a list that cannot nest becomes a task indistinguishable
        from its own parent. Unless somebody has asked for exactly that, it is
        folded into the parent instead and not sent on its own.
        """
        if caps.supports_parent or not item.parent_uid:
            return False
        if self._subtasks_separate():
            return False
        # Folding only helps where somebody can read the result. At a service
        # whose notes are never displayed, a folded step is not tidied away --
        # it is gone, so the child is sent as a task of its own instead.
        if not getattr(caps, "notes_visible", True):
            return False
        # Only skip it when the parent is actually going to carry it. An orphan
        # whose parent is not in this group would otherwise vanish entirely.
        parent = self.session.execute(
            select(Item).where(
                Item.uid == item.parent_uid,
                Item.sync_group_id == item.sync_group_id,
            )
        ).scalars().first()
        return parent is not None

    def _parent_uid_from(self, remote, part) -> str | None:
        """Turn a service's own parent id into Task Hub's UID for that task.

        The mirror of :meth:`_parent_remote_id`. Services name a parent by
        their id, not ours, so a report of "parent = abc123" is meaningless
        until it is matched against the link that already maps abc123 to an
        item here.

        None when the parent is not mapped yet -- a child read before its
        parent, which happens on a first sync. Rule: that must never be taken
        as "this task has no parent", and it is not, because the caller only
        applies a *set* parent and never a cleared one from an unmapped id.
        """
        remote_parent = getattr(remote.record, "parent_remote_id", None)
        if not remote_parent:
            return None
        link = self.session.execute(
            select(ItemLink).where(
                ItemLink.account_id == part.account.id,
                ItemLink.remote_list_id == part.remote_list.id,
                ItemLink.remote_id == str(remote_parent),
            )
        ).scalars().first()
        if link is None:
            return None
        parent = self.session.get(Item, link.item_id)
        return parent.uid if parent is not None else None

    def _parent_remote_id(self, item: Item, part) -> str | None:
        """What this service calls the parent of ``item``, if it holds it yet.

        None covers two different situations and both are handled the same way:
        the task has no parent, or it has one this service has not been given
        yet. Creating the child flat and correcting it on a later pass is the
        honest answer to the second -- refusing to push it at all would strand
        the task if its parent were never mapped here.
        """
        if not item.parent_uid:
            return None
        parent = self.session.execute(
            select(Item).where(
                Item.uid == item.parent_uid,
                Item.sync_group_id == item.sync_group_id,
            )
        ).scalars().first()
        if parent is None:
            return None
        links = self._links_in_list(parent.id, part.account.id, part.remote_list.id)
        return links[0].remote_id if links else None

    @staticmethod
    def apply_parent(item: Item, record: CanonicalRecord, caps) -> None:
        """Set an item's parent from a service's report, if it may say.

        The single rule that keeps a hierarchy alive across a service that has
        no idea it exists. A service which cannot express containment gets no
        say at all: not to set a parent, and -- the part that matters -- not to
        clear one. Sending a parent and eight children to a flat list produces
        nine unrelated tasks there, and if that flattening were allowed home the
        structure would be gone everywhere, permanently.

        A service that *can* express it is believed in both directions, because
        for those a missing parent really does mean the task is top level.
        """
        if not getattr(caps, "supports_parent", False):
            return
        item.parent_uid = record.parent_uid or None

    @staticmethod
    def _record_to_item(record: CanonicalRecord, item: Item) -> None:
        item.uid = record.uid or item.uid
        item.title = record.title or ""
        item.notes = record.notes
        item.status = record.status
        item.completed_at = record.completed_at
        item.due_date = record.due_date
        item.due_time = record.due_time
        item.due_tz = record.due_tz
        item.start_date = record.start_date
        item.start_time = record.start_time
        item.start_tz = record.start_tz
        item.end_date = record.end_date
        item.end_time = record.end_time
        item.end_tz = record.end_tz
        item.all_day = record.all_day
        item.location = record.location
        item.priority = record.priority
        # Deliberately not written here. Parenthood is a relationship, and a
        # service that cannot express one must never be able to clear it by
        # reporting None -- so it is applied only where that has been checked.
        # See apply_parent().
        item.rrule = record.rrule
        item.tags = list(record.tags or [])

    # -- Provenance ------------------------------------------------------------

    def _load_provenance(self, item: Item) -> dict[str, Provenance]:
        """Which service last changed each of this item's fields, and when.

        Returned by reference from the index rather than rebuilt, because one
        item is reconciled once per service reporting it and each of those
        merges must see what the previous one decided. Handing out a fresh copy
        each time would let the second service silently undo the first.
        """
        if self._index is not None:
            return self._index.provenance.setdefault(item.id, {})
        rows = (
            self.session.execute(
                select(FieldProvenance).where(FieldProvenance.item_id == item.id)
            )
            .scalars()
            .all()
        )
        return {
            row.field: Provenance(service=row.source_service, changed_at=row.changed_at)
            for row in rows
        }

    def _save_provenance(self, item: Item, provenance: dict[str, Provenance]) -> None:
        """Record which service last changed each field, and when.

        Called once per participant per item, and several participants can
        legitimately change the same field in one pass -- two services both
        editing a due time, say. Sessions here run with autoflush off, so a row
        added for the first participant was still pending and invisible to the
        second participant's lookup, which then added a *second* row for the
        same (item, field). The unique constraint rejected it at commit time,
        rolling back every merge in the group: the edits vanished and the push
        then wrote the unchanged values back over the services they came from.

        Flushing first makes pending rows visible to the query that follows, so
        the second participant updates the row rather than duplicating it.
        """
        self.session.flush()
        if self._index is not None:
            # The index already holds every row for this group, and is kept up
            # to date below, so the flush above is enough to make a pending row
            # real without also re-reading it.
            existing = self._index.provenance_rows.setdefault(item.id, {})
        else:
            existing = {
                row.field: row
                for row in self.session.execute(
                    select(FieldProvenance).where(FieldProvenance.item_id == item.id)
                ).scalars()
            }
        for field_name, entry in provenance.items():
            row = existing.get(field_name)
            if row is None:
                row = FieldProvenance(
                    item_id=item.id,
                    field=field_name,
                    source_service=entry.service,
                    changed_at=entry.changed_at,
                )
                self.session.add(row)
                # Recorded straight away. The next participant to touch this
                # item in the same pass looks the field up here, and not
                # finding it would add a second row for the same (item, field)
                # -- the duplicate that the unique constraint rejects at commit
                # time, rolling back every merge in the group.
                existing[field_name] = row
            else:
                row.source_service = entry.service
                row.changed_at = entry.changed_at


def run_sync_now(trigger: str = "manual", on_start=None) -> SyncRun:
    """Run one sync pass in its own database session.

    ``on_start`` is called with the new run's id as soon as it exists, so a
    caller running this in a background thread can report progress while it
    is still going.
    """
    from app.db.session import session_scope

    with session_scope() as session:
        engine = SyncEngine(session)
        engine.on_start = on_start
        run = engine.run_sync(trigger=trigger)
        session.expunge(run)
        return run
