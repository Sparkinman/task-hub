"""The embedded Radicale server, exposed as a connector.

Radicale is a peer in the sync, not a special case. Treating it as just another
connector means the engine has exactly one code path, and the Radicale
collection acting as a sync group's anchor is reconciled by the same merge rules
as Google or Todoist.

It is the only lossless participant: iCalendar can express every field in the
canonical record, so this connector declares full capabilities and its
projections never drop anything.
"""

from __future__ import annotations

import datetime as dt
import logging

from app.connectors.base import (
    FULL_CAPABILITIES,
    Capabilities,
    Connector,
    ConnectorError,
    PullResult,
    PushOutcome,
    RemoteItem,
    RemoteList,
)
from app.db.models import CollectionKind, ServiceKind
from app.services.caldav_client import CalDAVAuthError, CalDAVError, RadicaleClient
from app.services.ical_model import CanonicalRecord

logger = logging.getLogger(__name__)


class RadicaleConnector(Connector):
    """Reads and writes the local CalDAV collections."""

    service = ServiceKind.RADICALE
    name = "Radicale"

    def __init__(self, account_id: int, credentials: dict, sync_state: dict | None = None):
        super().__init__(account_id, credentials, sync_state)
        username = credentials.get("username", "")
        password = credentials.get("password", "")
        if not username or not password:
            raise ConnectorError("Radicale credentials are missing.")
        self.client = RadicaleClient(username, password)

    def close(self) -> None:
        """Hand the close on to the CalDAV client, which owns the session."""
        self.client.close()

    def capabilities(self, kind: CollectionKind) -> Capabilities:
        """Everything, for both tasks and calendar entries.

        Radicale stores raw iCalendar, so there is no field Task Hub can hold
        that it cannot. That makes this connector the reference against which
        every lossy service is measured: a value that survives a round trip
        here and not elsewhere is a limit of the other service, not a bug.
        """
        return FULL_CAPABILITIES

    def verify(self) -> str:
        """Confirm the CalDAV credentials work, and return the username.

        Authentication failure is raised as a distinct error from any other
        problem, because the two need different advice: a wrong password is
        something the user can fix, and an unreachable server is not.
        """
        try:
            self.client.check_connection()
        except CalDAVAuthError as exc:
            from app.connectors.base import ConnectorAuthError

            raise ConnectorAuthError(str(exc)) from exc
        except CalDAVError as exc:
            raise ConnectorError(str(exc)) from exc
        return self.client.username

    def list_remote_lists(self) -> list[RemoteList]:
        """Every collection this CalDAV account can see, with its colour.

        These are the lists offered when setting up a mapping. The colour comes
        along so the interface can show the same one the user chose in their
        calendar app.
        """
        try:
            collections = self.client.list_collections()
        except CalDAVError as exc:
            raise ConnectorError(str(exc)) from exc
        return [
            RemoteList(
                remote_id=info.collection_id,
                name=info.display_name,
                kind=info.kind,
                colour=info.colour,
            )
            for info in collections
        ]

    def pull(
        self,
        remote_list_id: str,
        kind: CollectionKind,
        since: dt.datetime | None = None,
        state: dict | None = None,
    ) -> PullResult:
        """Read a whole collection, completed items included.

        ``since`` is accepted and ignored: this is a local server on the other
        end of a loopback connection, so a full read costs almost nothing, and
        a complete listing is worth far more than a fast one. Absence from a
        complete listing genuinely means deletion, which is what allows the
        engine to act on it -- an incremental pull can never support that.
        """
        try:
            records = self.client.list_records(remote_list_id, kind, include_completed=True)
        except CalDAVError as exc:
            return PullResult(errors=[str(exc)])

        fields = FULL_CAPABILITIES.present_fields()
        items = [
            RemoteItem(
                # Radicale is keyed by iCalendar UID, so the canonical UID and
                # the remote id are the same value. That makes links here
                # self-healing: even if Task Hub's database were lost, the
                # relationship could be rebuilt from the collection alone.
                remote_id=record.uid,
                record=record,
                fields_present=fields,
                remote_updated_at=record.updated_at,
                etag=record.etag,
            )
            for record in records
        ]
        # Always a complete listing, never a delta, so absence genuinely means
        # deletion and the engine can act on it.
        return PullResult(items=items, incremental=False)

    def create(
        self, remote_list_id: str, record: CanonicalRecord, kind: CollectionKind
    ) -> PushOutcome:
        """Store a new item. Identical to an update, since CalDAV is keyed by UID."""
        return self._write(remote_list_id, record, kind)

    def update(
        self,
        remote_list_id: str,
        remote_id: str,
        record: CanonicalRecord,
        kind: CollectionKind,
    ) -> PushOutcome:
        # CalDAV writes are addressed by UID, so create and update are the same
        # operation. The record keeps whatever UID it already has.
        return self._write(remote_list_id, record, kind)

    def _write(
        self, collection_id: str, record: CanonicalRecord, kind: CollectionKind
    ) -> PushOutcome:
        """The single write path behind both create and update.

        A CalDAV PUT to a UID's address creates or replaces, with no distinction
        between the two, so keeping one implementation removes any chance of the
        two drifting apart.

        Failures are returned rather than raised: one item that will not save
        should not abandon the rest of the push.
        """
        record.kind = kind
        try:
            saved = self.client.save_record(collection_id, record)
        except CalDAVError as exc:
            return PushOutcome(remote_id=record.uid, error=str(exc))
        return PushOutcome(
            remote_id=saved.uid,
            etag=saved.etag,
            remote_updated_at=dt.datetime.now(dt.timezone.utc),
        )

    def delete(
        self, remote_list_id: str, remote_id: str, kind: CollectionKind
    ) -> PushOutcome:
        """Remove an item from a collection.

        A failure is reported rather than raised, so that the rest of the pass
        continues; the engine decides whether a delete that did not happen is
        worth retrying next time.
        """
        try:
            self.client.delete_record(remote_list_id, remote_id, kind)
        except CalDAVError as exc:
            return PushOutcome(remote_id=remote_id, error=str(exc))
        return PushOutcome(remote_id=remote_id)
