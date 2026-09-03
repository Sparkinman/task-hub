"""CalDAV access to the embedded Radicale server.

Task Hub talks to its own Radicale over HTTP rather than by reaching into
Radicale's storage directory. That costs a loopback request per operation, and
buys two things worth far more: Radicale keeps ownership of its own locking and
sync-token bookkeeping, and the exact same code path works if Radicale is later
moved to a separate container or replaced with a different CalDAV server.

All calls here block, so callers must run them off the event loop. Route
handlers that use this client are declared with ``def`` rather than ``async def``
so Starlette dispatches them to a threadpool automatically -- which also means
the loopback request can be served while the caller waits for it.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import caldav
from caldav.elements import cdav as cdav_elements
from caldav.elements import dav as dav_elements
from caldav.elements import ical as ical_elements
from caldav.lib.error import AuthorizationError, DAVError, NotFoundError

from app.config import RUNTIME
from app.db.models import CollectionKind
from app.services.ical_model import (
    CanonicalRecord,
    parse_calendar,
    record_to_ics,
)

logger = logging.getLogger(__name__)

#: Component type each kind of collection accepts. Radicale enforces this, so a
#: VTODO cannot be written into a collection declared as VEVENT-only.
COMPONENT_SET = {
    CollectionKind.TASKS: ["VTODO"],
    CollectionKind.CALENDAR: ["VEVENT"],
}

_SAFE_ID = re.compile(r"[^a-z0-9_-]+")


class CalDAVError(RuntimeError):
    """A CalDAV operation failed in a way worth showing to the user."""


class CalDAVAuthError(CalDAVError):
    """Credentials were rejected by the server."""


@dataclass
class CollectionInfo:
    """A collection as it currently exists on the Radicale server."""

    collection_id: str
    display_name: str
    kind: CollectionKind
    url: str
    colour: str | None = None
    description: str | None = None
    item_count: int | None = None


def slugify_collection_id(name: str) -> str:
    """Derive a URL-safe collection id from a display name.

    The id becomes a path segment and a directory name on disk, and is visible
    in the CalDAV URL that gets typed into phones, so it is kept to lowercase
    ASCII rather than percent-encoding whatever the user typed.
    """
    slug = _SAFE_ID.sub("-", name.strip().lower().replace(" ", "-")).strip("-")
    return slug[:60] or "collection"


class RadicaleClient:
    """A connection to the embedded Radicale server as one specific user."""

    def __init__(self, username: str, password: str, base_url: str | None = None):
        self.username = username
        self._password = password
        self.base_url = (base_url or RUNTIME.radicale_url).rstrip("/")
        self._client: caldav.DAVClient | None = None
        self._principal: caldav.Principal | None = None

    # -- Connection ------------------------------------------------------------

    def _connect(self) -> caldav.Principal:
        if self._principal is not None:
            return self._principal
        try:
            self._client = caldav.DAVClient(
                url=f"{self.base_url}/",
                username=self.username,
                password=self._password,
                # The embedded server is reached over loopback HTTP. caldav 2.x
                # refuses plaintext by default, which is the right default for
                # remote servers and wrong for 127.0.0.1 inside one container.
                require_tls=False,
                # No DNS-SRV discovery: we know exactly where the server is, and
                # lookups against a made-up hostname just add latency.
                enable_rfc6764=False,
                timeout=30,
            )
            self._principal = self._client.principal()
            return self._principal
        except AuthorizationError as exc:
            raise CalDAVAuthError(
                "Radicale rejected these credentials. Check the username and "
                "password on the Radicale tab."
            ) from exc
        except DAVError as exc:
            raise CalDAVError(f"Could not reach the Radicale server: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as-is
            raise CalDAVError(f"Could not reach the Radicale server: {exc}") from exc

    def check_connection(self) -> bool:
        """Verify the credentials work. Raises CalDAVAuthError if they do not."""
        self._connect()
        return True

    def collection_url(self, collection_id: str) -> str:
        return f"{self.base_url}/{self.username}/{collection_id}/"

    def public_collection_url(self, collection_id: str, public_base: str) -> str:
        """The URL to hand to an external CalDAV client such as a phone."""
        from app.config import RADICALE_MOUNT_PATH

        return f"{public_base.rstrip('/')}{RADICALE_MOUNT_PATH}/{self.username}/{collection_id}/"

    # -- Collections -----------------------------------------------------------

    def _calendar(self, collection_id: str) -> caldav.Calendar:
        principal = self._connect()
        return caldav.Calendar(
            client=principal.client, url=self.collection_url(collection_id)
        )

    @staticmethod
    def _kind_of(calendar: caldav.Calendar) -> CollectionKind:
        """Determine whether a collection holds tasks or events.

        Radicale reports a supported component set; when it is missing or lists
        both, we treat the collection as a calendar, since VEVENT is the
        default meaning of a CalDAV collection.
        """
        try:
            components = calendar.get_supported_components()
        except Exception:  # noqa: BLE001 - property is optional in CalDAV
            return CollectionKind.CALENDAR
        upper = {str(c).upper() for c in (components or [])}
        if "VTODO" in upper and "VEVENT" not in upper:
            return CollectionKind.TASKS
        return CollectionKind.CALENDAR

    def list_collections(self, with_counts: bool = False) -> list[CollectionInfo]:
        principal = self._connect()
        try:
            calendars = principal.calendars()
        except DAVError as exc:
            raise CalDAVError(f"Could not list collections: {exc}") from exc

        results: list[CollectionInfo] = []
        for calendar in calendars:
            url = str(calendar.url)
            collection_id = url.rstrip("/").rsplit("/", 1)[-1]
            kind = self._kind_of(calendar)

            try:
                display_name = calendar.get_display_name() or collection_id
            except Exception:  # noqa: BLE001
                display_name = collection_id

            colour = None
            try:
                props = calendar.get_properties([ical_elements.CalendarColor()])
                colour = next((v for v in props.values() if v), None)
            except Exception as exc:  # noqa: BLE001 - an optional CalDAV extension
                # Logged rather than silently swallowed: a server that does not
                # support calendar colours is normal, but a bug in this call
                # would otherwise be invisible.
                logger.debug("No colour property for %s: %s", collection_id, exc)

            count = None
            if with_counts:
                try:
                    count = len(self.list_records(collection_id, kind, include_completed=True))
                except CalDAVError:
                    count = None

            results.append(
                CollectionInfo(
                    collection_id=collection_id,
                    display_name=str(display_name),
                    kind=kind,
                    url=url,
                    colour=str(colour) if colour else None,
                    item_count=count,
                )
            )
        results.sort(key=lambda c: (c.kind.value, c.display_name.lower()))
        return results

    def create_collection(
        self,
        collection_id: str,
        display_name: str,
        kind: CollectionKind,
        colour: str | None = None,
    ) -> CollectionInfo:
        principal = self._connect()
        try:
            calendar = principal.make_calendar(
                name=display_name,
                cal_id=collection_id,
                supported_calendar_component_set=COMPONENT_SET[kind],
            )
        except DAVError as exc:
            raise CalDAVError(
                f"Could not create the collection {display_name!r}: {exc}"
            ) from exc

        if colour:
            try:
                calendar.set_properties([ical_elements.CalendarColor(colour)])
            except Exception as exc:  # noqa: BLE001 - cosmetic, never fatal
                logger.warning(
                    "Could not set colour on collection %s: %s", collection_id, exc
                )

        return CollectionInfo(
            collection_id=collection_id,
            display_name=display_name,
            kind=kind,
            url=str(calendar.url),
            colour=colour,
            item_count=0,
        )

    def rename_collection(self, collection_id: str, display_name: str,
                          colour: str | None = None) -> None:
        calendar = self._calendar(collection_id)
        props = [dav_elements.DisplayName(display_name)]
        if colour:
            props.append(ical_elements.CalendarColor(colour))
        try:
            calendar.set_properties(props)
        except DAVError as exc:
            raise CalDAVError(f"Could not rename the collection: {exc}") from exc

    def set_collection_kind(self, collection_id: str, kind: CollectionKind) -> None:
        """Change whether a collection holds tasks or calendar events.

        Most CalDAV servers treat the supported component set as protected and
        refuse to change it after creation; Radicale accepts a PROPPATCH, which
        is what makes this possible without recreating the collection and
        losing its URL. Callers must only do this on an empty collection: a
        VTODO and a VEVENT are different objects, and existing items would not
        survive being reinterpreted as the other.
        """
        calendar = self._calendar(collection_id)
        component_set = cdav_elements.SupportedCalendarComponentSet() + [
            cdav_elements.Comp(name) for name in COMPONENT_SET[kind]
        ]
        try:
            calendar.set_properties([component_set])
        except DAVError as exc:
            raise CalDAVError(
                f"Could not change the collection type: {exc}"
            ) from exc

    def count_records(self, collection_id: str) -> int:
        """How many items a collection holds, counting both component types.

        Both are counted rather than just the declared kind, because the point
        of asking is usually to find out whether a collection is safe to
        retype -- and an item of the "wrong" type still stands in the way.
        """
        total = 0
        for probe in (CollectionKind.TASKS, CollectionKind.CALENDAR):
            try:
                total += len(
                    self.list_records(collection_id, probe, include_completed=True)
                )
            except CalDAVError:
                continue
        return total

    def delete_collection(self, collection_id: str) -> None:
        calendar = self._calendar(collection_id)
        try:
            calendar.delete()
        except NotFoundError:
            return
        except DAVError as exc:
            raise CalDAVError(f"Could not delete the collection: {exc}") from exc

    # -- Items -----------------------------------------------------------------

    def _fetch_objects(self, collection_id: str, kind: CollectionKind) -> list:
        """Every object in a collection, completed items included.

        Deliberately does not use the library's ``object_by_uid``. That issues a
        CalDAV search whose default filter excludes completed to-dos, so a task
        becomes invisible the moment it is ticked off -- which would make
        un-completing or deleting a finished task impossible. Listing and
        matching locally is a little more work per lookup and is correct for
        every item regardless of status.
        """
        calendar = self._calendar(collection_id)
        try:
            if kind == CollectionKind.TASKS:
                return calendar.todos(include_completed=True, sort_keys=())
            return calendar.events()
        except NotFoundError:
            raise CalDAVError(f"Collection {collection_id!r} does not exist.") from None
        except DAVError as exc:
            raise CalDAVError(f"Could not read {collection_id!r}: {exc}") from exc

    def _parse_objects(
        self, objects, kind: CollectionKind
    ) -> list[tuple[CanonicalRecord, object]]:
        """Parse CalDAV objects into (record, object) pairs, skipping bad data."""
        wanted = "VTODO" if kind == CollectionKind.TASKS else "VEVENT"
        results: list[tuple[CanonicalRecord, object]] = []
        for obj in objects:
            try:
                parsed = parse_calendar(obj.data)
            except Exception:  # noqa: BLE001
                # One malformed item, very likely written by another client,
                # must not blank out the whole list for the user.
                logger.warning("Skipping unparseable item at %s", obj.url)
                continue
            for record, name in parsed:
                if name != wanted:
                    continue
                record.href = str(obj.url)
                record.etag = getattr(obj, "etag", None)
                results.append((record, obj))
        return results

    def list_records(
        self,
        collection_id: str,
        kind: CollectionKind,
        include_completed: bool = True,
    ) -> list[CanonicalRecord]:
        """Fetch every item in a collection as canonical records."""
        objects = self._fetch_objects(collection_id, kind)
        records = [record for record, _ in self._parse_objects(objects, kind)]
        if not include_completed:
            records = [r for r in records if not r.is_completed]
        return records

    def list_events_in_range(
        self, collection_id: str, first: dt.date, last: dt.date
    ) -> list[CanonicalRecord]:
        """Events overlapping an inclusive date window.

        Asks the server to do the filtering. On a calendar holding a few
        thousand events that is the difference between a page that takes three
        seconds and one that takes a tenth of that, because the whole
        collection no longer has to be transferred and parsed to draw one
        month of it.

        The window returned is deliberately a little wider than asked for --
        see below -- so callers must still narrow it to the dates they want.

        The server expands recurrence rules when it applies the filter, so a
        yearly event created in 2011 is still returned for a window in 2026
        even though its stored start is nowhere near it. If the search fails
        for any reason the full listing is used instead -- slow beats blank.
        """
        calendar = self._calendar(collection_id)
        # Padded by a day at each end. The server filters in UTC while the
        # caller thinks in local dates, so an evening event can sit on the far
        # side of midnight UTC from the day it belongs to -- an 18:30 event on
        # the 10th is 00:30 UTC on the 11th. The caller narrows to the exact
        # local dates afterwards; this only has to avoid excluding anything.
        try:
            objects = calendar.search(
                start=dt.datetime.combine(first - dt.timedelta(days=1), dt.time.min),
                end=dt.datetime.combine(last + dt.timedelta(days=2), dt.time.min),
                event=True,
                expand=False,
            )
        except NotFoundError:
            raise CalDAVError(f"Collection {collection_id!r} does not exist.") from None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Date-range search failed on %s (%s); falling back to a full read.",
                collection_id, exc,
            )
            return self.list_records(collection_id, CollectionKind.CALENDAR)
        return [r for r, _ in self._parse_objects(objects, CollectionKind.CALENDAR)]

    def _object_by_uid(self, collection_id: str, uid: str, kind: CollectionKind):
        """Try to fetch one item straight from its own address.

        Radicale names an item's file after its UID, so this normally resolves
        in a single request. It is only ever a shortcut: anything unexpected --
        a missing file, or a file whose UID is not the one asked for, which is
        what an item written by another client with its own naming would look
        like -- returns None so the caller falls back to searching properly.
        """
        from urllib.parse import quote

        calendar = self._calendar(collection_id)
        url = f"{self.collection_url(collection_id)}{quote(uid, safe='')}.ics"
        try:
            obj = caldav.CalendarObjectResource(
                client=calendar.client, url=url, parent=calendar
            )
            obj.load()
        except Exception:  # noqa: BLE001
            return None
        for record, name in self._parse_objects([obj], kind):
            if record.uid == uid:
                return record, obj
        return None

    def _find(
        self, collection_id: str, uid: str, kind: CollectionKind
    ) -> tuple[CanonicalRecord, object] | None:
        direct = self._object_by_uid(collection_id, uid, kind)
        if direct is not None:
            return direct
        objects = self._fetch_objects(collection_id, kind)
        for record, obj in self._parse_objects(objects, kind):
            if record.uid == uid:
                return record, obj
        return None

    def get_record(
        self, collection_id: str, uid: str, kind: CollectionKind
    ) -> CanonicalRecord | None:
        found = self._find(collection_id, uid, kind)
        return found[0] if found else None

    def save_record(self, collection_id: str, record: CanonicalRecord) -> CanonicalRecord:
        """Create or replace an item, keyed on its UID."""
        calendar = self._calendar(collection_id)
        ics = record_to_ics(record)
        try:
            if record.kind == CollectionKind.TASKS:
                obj = calendar.save_todo(ics)
            else:
                obj = calendar.save_event(ics)
        except DAVError as exc:
            raise CalDAVError(f"Could not save item {record.uid}: {exc}") from exc
        record.href = str(obj.url)
        record.etag = getattr(obj, "etag", None)
        return record

    def delete_record(
        self, collection_id: str, uid: str, kind: CollectionKind = CollectionKind.TASKS
    ) -> bool:
        found = self._find(collection_id, uid, kind)
        if found is None:
            return False
        try:
            found[1].delete()
            return True
        except DAVError as exc:
            raise CalDAVError(f"Could not delete item {uid}: {exc}") from exc
