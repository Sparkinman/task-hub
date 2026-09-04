"""A connector for any remote CalDAV server, and Apple iCloud in particular.

Unlike the embedded Radicale connector, this one cannot assume where anything
lives. A remote server is asked what the account owns -- principal discovery --
and the collections it reports are used as they are found. iCloud, Fastmail,
Nextcloud and a self-hosted Baikal all answer that conversation the same way,
so the same code serves them.

Two services are built on it. ``CalDAV`` takes any server address and is how
Nextcloud, Fastmail, Baikal, Synology and everything else of that shape
connect; ``Apple`` is the same connector pinned to iCloud's address, with the
Reminders quirks below named where somebody will meet them. The split is for
the person reading the page, not for the protocol: an Apple account needs three
paragraphs of warning that would be meaningless on a Nextcloud server.

CalDAV is the only lossless transport Task Hub speaks: a collection stores real
iCalendar, so every field survives. Its capabilities are therefore complete, and
a value never has to be withheld to protect it from the far end.

**Apple's two accounts problem.** Apple's Reminders app moves an "upgraded"
primary iCloud account's to-do lists into a private store that CalDAV cannot
reach, so Reminders on the account you use every day are invisible here.
Calendars are unaffected. A *second* Apple ID, added to your devices as a manual
CalDAV account, keeps its Reminders in the open where this connector can see
them. That is covered step by step in the Apple setup guide.
"""

from __future__ import annotations

import datetime as dt
import logging
from urllib.parse import urlparse

import caldav
from caldav.lib.error import AuthorizationError, DAVError, NotFoundError
from caldav.lib.url import URL

from app.connectors.base import (
    ALL_FIELDS,
    Capabilities,
    Connector,
    ConnectorAuthError,
    ConnectorError,
    ConnectorGoneError,
    PullResult,
    PushOutcome,
    RemoteItem,
    RemoteList,
)
from app.db.models import CollectionKind, ServiceKind
from app.services.ical_model import CanonicalRecord, parse_calendar

logger = logging.getLogger(__name__)

#: Apple's CalDAV entry point. Discovery finds everything else from here.
ICLOUD_URL = "https://caldav.icloud.com/"

#: CalDAV round-trips iCalendar, so nothing is lost and nothing is withheld.
CALDAV_CAPABILITIES = Capabilities(
    fields=ALL_FIELDS, stores_uid=True, carries_origin=True
)


class RemoteCalDAVConnector(Connector):
    """Talks to a CalDAV server discovered from a base URL."""

    service = ServiceKind.CALDAV
    name = "CalDAV"

    def __init__(self, account_id: int, credentials: dict, sync_state: dict | None = None,
                 default_timezone: str | None = None):
        super().__init__(account_id, credentials, sync_state)
        self.username = (credentials.get("username") or "").strip()
        self._password = credentials.get("password") or ""
        self.base_url = (credentials.get("url") or "").strip()
        self.default_timezone = default_timezone
        if not self.base_url:
            raise ConnectorError(
                "This account has no server address saved. Add the CalDAV "
                "address of your server, then try again."
            )
        if not self.username or not self._password:
            raise ConnectorError(
                "This account has no saved sign-in. Add the username and "
                "password, then try again."
            )
        self._client: caldav.DAVClient | None = None
        self._principal: caldav.Principal | None = None

    # -- Connection ------------------------------------------------------------

    def _connect(self) -> caldav.Principal:
        if self._principal is not None:
            return self._principal
        insecure = urlparse(self.base_url).scheme == "http"
        try:
            self._client = caldav.DAVClient(
                url=self.base_url,
                username=self.username,
                password=self._password,
                # Only relaxed for an explicitly plain-http URL, which is a
                # deliberate choice for a server on your own network. iCloud is
                # https, so this stays on for it.
                require_tls=not insecure,
                timeout=45,
            )
            self._principal = self._client.principal()
            self._follow_discovery()
            return self._principal
        except AuthorizationError as exc:
            raise ConnectorAuthError(self._explain_auth_failure()) from exc
        except DAVError as exc:
            message = str(exc)
            if "401" in message or "403" in message:
                raise ConnectorAuthError(self._explain_auth_failure()) from exc
            raise ConnectorError(
                f"Could not reach the CalDAV server at {self.base_url}: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - network stacks raise widely
            raise ConnectorError(f"Could not connect: {exc}") from exc

    def _follow_discovery(self) -> None:
        """Move the client to whichever host discovery actually pointed at.

        iCloud signs you in at ``caldav.icloud.com`` and then hands back a
        principal, and every collection under it, on a numbered shard such as
        ``p195-caldav.icloud.com``. The caldav library builds every object it
        returns by joining the object's URL onto the client's base URL, and that
        join refuses outright when the hostnames differ. So a client left
        pointing at the sign-in host cannot construct a single one of the
        objects discovery just told it about.

        The failure is worth describing because it looks like something else
        entirely: signing in works, listing collections works, and the account
        page shows every calendar correctly. It is only when a sync reads one
        that a ``ValueError`` about URLs surfaces -- and because a sync group
        fails as a unit, mapping one iCloud calendar took Google and Radicale
        down with it, with nothing in the message naming Apple.

        Following the redirect once here fixes reads, writes and deletes
        together, rather than patching each construction site as it is found.
        """
        if self._client is None or self._principal is None:
            return
        try:
            # The principal is not the thing that moves. On iCloud it stays on
            # the sign-in host and only the calendar home set is on the shard,
            # so following the principal fixes nothing at all.
            discovered = URL.objectify(str(self._principal.calendar_home_set.url))
            current = URL.objectify(str(self._client.url))
        except Exception:  # noqa: BLE001 - a URL we cannot parse is left alone
            return
        if discovered.hostname and discovered.hostname != current.hostname:
            logger.info(
                "CalDAV discovery moved this account from %s to %s",
                current.hostname, discovered.hostname,
            )
            self._client.url = discovered

    def _explain_auth_failure(self) -> str:
        if "icloud" in self.base_url:
            return (
                "Apple rejected the sign-in. Apple never accepts your normal "
                "Apple ID password here -- it needs an app-specific password "
                "generated at account.apple.com. If you are already using one, "
                "generate a fresh one; they stop working when the Apple ID "
                "password changes."
            )
        return (
            "The server rejected that username and password. Many servers want "
            "an app password rather than the one you log in to the website "
            "with -- Nextcloud, Fastmail and Zoho all do -- and some want the "
            "username in a different form, such as the full email address."
        )

    def close(self) -> None:
        """Release the HTTP session, not merely the reference to it.

        Dropping the reference alone leaves the underlying requests session and
        its urllib3 connection pool alive until a garbage collection happens to
        take them, holding sockets open in the meantime. A connector is built
        fresh for every account on every sync pass, so on a fifteen-minute
        schedule that is four abandoned pools an hour, per CalDAV account, for
        as long as the container runs.

        It showed up as a slow rise in live objects across identical sync passes
        -- weakrefs, thread locks and urllib3 pool dictionaries, never
        application objects, which is what says "connections" rather than "data".
        """
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
        self._principal = None
        self._client = None

    # -- Capabilities ----------------------------------------------------------

    def capabilities(self, kind: CollectionKind) -> Capabilities:
        return CALDAV_CAPABILITIES

    # -- Discovery -------------------------------------------------------------

    def verify(self) -> str:
        principal = self._connect()
        try:
            principal.calendars()
        except DAVError as exc:
            raise ConnectorError(f"Signed in, but could not list calendars: {exc}") from exc
        return self.identity()

    def identity(self) -> str:
        """What the account is called in the interface once it is connected.

        A username on its own is enough for Apple, where there is only one
        server it can belong to. For everything else it is not: somebody with a
        Nextcloud and a Fastmail account is looking at two slots both saying
        "paul", so the host goes in the name.
        """
        host = urlparse(self.base_url).hostname or self.base_url
        return f"{self.username} at {host}"

    @staticmethod
    def _kind_of(calendar) -> CollectionKind | None:
        """Whether a collection holds events or to-dos.

        A CalDAV collection declares which components it accepts. Guessing from
        the name instead would put Reminders lists in the calendar view and vice
        versa, and writing a VTODO into an events-only collection is rejected by
        the server.
        """
        try:
            components = calendar.get_supported_components()
        except Exception:  # noqa: BLE001 - not every server answers this
            components = []
        components = [str(c).upper() for c in (components or [])]
        if "VTODO" in components and "VEVENT" not in components:
            return CollectionKind.TASKS
        if "VEVENT" in components:
            return CollectionKind.CALENDAR
        if components:
            return None
        # Said nothing: assume a calendar, which is what most collections are.
        return CollectionKind.CALENDAR

    def list_remote_lists(self) -> list[RemoteList]:
        principal = self._connect()
        try:
            calendars = principal.calendars()
        except DAVError as exc:
            raise ConnectorError(f"Could not list collections: {exc}") from exc

        found: list[RemoteList] = []
        for calendar in calendars:
            kind = self._kind_of(calendar)
            if kind is None:
                continue
            try:
                name = calendar.get_display_name() or str(calendar.url).rstrip("/").split("/")[-1]
            except Exception:  # noqa: BLE001
                name = str(calendar.url).rstrip("/").split("/")[-1]
            name = name or "Untitled"

            if kind == CollectionKind.TASKS and self._is_upgrade_tombstone(
                str(calendar.url), name
            ):
                logger.warning(
                    "Skipping %r: Apple has upgraded this account's Reminders, so "
                    "the list is a placeholder rather than real reminders. "
                    "Calendars are unaffected.",
                    name,
                )
                continue

            found.append(
                RemoteList(remote_id=str(calendar.url), name=name, kind=kind)
            )
        return found

    #: What Apple leaves behind when Reminders are upgraded. The real reminders
    #: move to a store CalDAV cannot see, and these two placeholders are put in
    #: the old list in their place -- so a list containing nothing else is a
    #: headstone, not a list. Matched exactly as Apple writes them, lowercased.
    UPGRADE_PLACEHOLDERS = frozenset({
        "where are my reminders?",
        "the creator of this list has upgraded these reminders.",
    })

    #: Apple also appends a warning sign to the display name of such a list.
    UPGRADE_MARKER = "⚠"

    def _is_upgrade_tombstone(self, url: str, name: str) -> bool:
        """Whether this reminder list is Apple's "these have moved" placeholder.

        Offering one of these to be mapped is worse than showing nothing: it
        looks like a working list, and syncing it copies two meaningless
        sentences into every other service the user has connected.

        Both signals are required. The warning sign in the name is Apple's own
        doing and is the cheap test, but somebody is perfectly entitled to put
        one in a list name themselves, and silently hiding a real list would be
        a far worse fault than showing a dead one. So a marked list is opened
        and skipped only when its entire contents are Apple's placeholders --
        which costs one request, and only for lists that are already suspicious.
        """
        if self.UPGRADE_MARKER not in name:
            return False
        try:
            items = self.pull(url, CollectionKind.TASKS).items
        except Exception:  # noqa: BLE001 - if it cannot be read, leave it listed
            return False
        if not items:
            return False
        return all(
            (item.record.title or "").strip().lower() in self.UPGRADE_PLACEHOLDERS
            for item in items
        )

    # -- Helpers ---------------------------------------------------------------

    def _calendar(self, remote_list_id: str):
        """A collection handle from the URL discovery gave us.

        The URL is set after construction rather than passed in, and that is
        load-bearing rather than fussy. Given both a client and a url, the caldav
        library joins the url onto the client's base and refuses the join when
        the hostnames differ -- which is exactly what iCloud does. Sign-in starts
        at ``caldav.icloud.com`` and discovery hands back collections on a
        numbered shard such as ``p195-caldav.icloud.com``, so every collection
        this connector was told about is on a different host from the client that
        found it.

        The result was that mapping any iCloud calendar made the whole sync group
        fail -- Google and Radicale included, because a group fails as a unit --
        with a ValueError about URLs that cannot be joined and nothing pointing
        at Apple as the cause. Discovery itself worked, which is what made it look
        like the account was fine.
        """
        self._connect()
        collection = caldav.Calendar(client=self._client)
        collection.url = URL.objectify(remote_list_id)
        return collection

    # -- Reading ---------------------------------------------------------------

    def pull(
        self,
        remote_list_id: str,
        kind: CollectionKind,
        since: dt.datetime | None = None,
        state: dict | None = None,
    ) -> PullResult:
        calendar = self._calendar(remote_list_id)
        try:
            if kind == CollectionKind.TASKS:
                objects = calendar.todos(include_completed=True, sort_keys=())
            else:
                objects = calendar.events()
        except NotFoundError:
            return PullResult(errors=[f"That collection no longer exists on the server."])
        except AuthorizationError as exc:
            raise ConnectorAuthError(self._explain_auth_failure()) from exc
        except DAVError as exc:
            return PullResult(errors=[f"Could not read the collection: {exc}"])

        wanted = "VTODO" if kind == CollectionKind.TASKS else "VEVENT"
        items: list[RemoteItem] = []
        for obj in objects:
            try:
                parsed = parse_calendar(obj.data)
            except Exception:  # noqa: BLE001
                # One malformed item, very likely written by another client,
                # must not blank out the whole collection.
                logger.warning("Skipping unreadable item at %s", obj.url)
                continue
            for record, component in parsed:
                if component != wanted:
                    continue
                items.append(
                    RemoteItem(
                        remote_id=record.uid,
                        record=record,
                        fields_present=CALDAV_CAPABILITIES.present_fields(),
                        remote_updated_at=record.updated_at,
                        etag=getattr(obj, "etag", None),
                    )
                )
        # A full listing every time, so an absent item really is deleted.
        return PullResult(items=items, incremental=False)

    # -- Writing ---------------------------------------------------------------

    def create(
        self, remote_list_id: str, record: CanonicalRecord, kind: CollectionKind
    ) -> PushOutcome:
        return self._write(remote_list_id, record, kind)

    def update(
        self, remote_list_id: str, remote_id: str, record: CanonicalRecord,
        kind: CollectionKind,
    ) -> PushOutcome:
        # CalDAV addresses an item by its UID, so creating and replacing are the
        # same operation.
        return self._write(remote_list_id, record, kind)

    def _write(
        self, remote_list_id: str, record: CanonicalRecord, kind: CollectionKind
    ) -> PushOutcome:
        from app.services.ical_model import record_to_ics

        record.kind = kind
        calendar = self._calendar(remote_list_id)
        payload = record_to_ics(record)
        try:
            if kind == CollectionKind.TASKS:
                calendar.save_todo(payload)
            else:
                calendar.save_event(payload)
        except AuthorizationError as exc:
            raise ConnectorAuthError(self._explain_auth_failure()) from exc
        except DAVError as exc:
            return PushOutcome(remote_id=record.uid, error=f"Could not save: {exc}")
        return PushOutcome(
            remote_id=record.uid,
            remote_updated_at=dt.datetime.now(dt.timezone.utc),
        )

    def delete(
        self, remote_list_id: str, remote_id: str, kind: CollectionKind
    ) -> PushOutcome:
        calendar = self._calendar(remote_list_id)
        try:
            if kind == CollectionKind.TASKS:
                objects = calendar.todos(include_completed=True, sort_keys=())
            else:
                objects = calendar.events()
            for obj in objects:
                for record, _component in parse_calendar(obj.data):
                    if record.uid == remote_id:
                        obj.delete()
                        return PushOutcome(remote_id=remote_id)
        except DAVError as exc:
            return PushOutcome(remote_id=remote_id, error=f"Could not delete: {exc}")
        # Already gone is a success: the outcome the caller wanted is the case.
        return PushOutcome(remote_id=remote_id)


class AppleConnector(RemoteCalDAVConnector):
    """iCloud Calendar, and Reminders on an account that still exposes them."""

    service = ServiceKind.APPLE
    name = "Apple"

    def __init__(self, account_id: int, credentials: dict, sync_state: dict | None = None,
                 default_timezone: str | None = None):
        credentials = dict(credentials)
        credentials.setdefault("url", ICLOUD_URL)
        super().__init__(account_id, credentials, sync_state, default_timezone)

    def identity(self) -> str:
        """The Apple ID alone. There is only one iCloud for it to be on."""
        return self.username
