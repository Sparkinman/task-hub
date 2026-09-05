"""Things 3, through the unofficial Things Cloud endpoint.

**This connector is not built on a published API, and Cultured Code does not
offer one.** It talks to the same endpoint the Things apps use, as documented by
the community through observation. That has three consequences worth stating
plainly rather than burying:

* It can stop working without warning if Cultured Code changes their backend.
  Nothing here is a contract.
* It needs your Things Cloud email and password, because there is no OAuth to
  delegate to. The password is encrypted at rest with the same key as every
  other credential and is never logged.
* It was written against a description rather than a documented API, and the
  first live account it met broke four of those assumptions at once: the entity
  name is versioned (``Task7``, not ``Task``), notes arrive as an object rather
  than a string, sign-in is a GET with the password in a header, and the history
  stream carries no reliable list membership. All four are fixed and pinned by
  tests/test_things_parsing.py. The first thing it does on connecting is still
  to prove it can sign in and read, so the next change at Cultured Code's end
  surfaces immediately rather than as silent data loss later.

Failures here are deliberately contained. A Things outage marks this one account
as needing attention and the sync pass carries on with every other service, so a
service that may break without notice can never take the rest down with it.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

import httpx

from app.connectors.base import (
    F_DUE_DATE,
    F_NOTES,
    F_STATUS,
    F_TAGS,
    F_TITLE,
    Capabilities,
    Connector,
    ConnectorAuthError,
    ConnectorError,
    ConnectorGoneError,
    PullResult,
    PushOutcome,
    RateLimitError,
    RemoteItem,
    RemoteList,
)
from app.db.models import CollectionKind, ItemStatus, ServiceKind
from app.services.ical_model import CanonicalRecord, new_uid

logger = logging.getLogger(__name__)

BASE = "https://cloud.culturedcode.com/version/1"

#: Things stores a *day* a task is scheduled for, never a time of day: its own
#: interface offers reminders separately and does not put a clock on a to-do.
#: The field is therefore not claimed, which is what stops it clearing a time
#: set in Todoist or on a CalDAV client.
#: What Things can be read for, and nothing it can be asked to do. Every write
#: method here refuses -- there is no supported way to write to Things Cloud, and
#: guessing at one against an unpublished endpoint is not a risk worth taking
#: with somebody's task list -- so the declaration has to say so.
#:
#: Claiming otherwise is not a harmless overstatement. The engine offers a
#: changed task to every participant that says it can take one, so a connector
#: that accepts the offer and then refuses it turns every edited task into a
#: failed write, every pass, for ever: an error in the log per task and a run
#: reported as partial for doing exactly what it was told. Obsidian in read-only
#: mode is the same shape and carries the same declaration.
THINGS_CAPABILITIES = Capabilities(
    fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE, F_TAGS}),
    can_create=False,
    can_delete=False,
    writable_fields=frozenset(),
    stores_uid=False,
    # Things has checklist items -- the sync payload carries them as their own
    # ChecklistItem3 entity, with the parent task's id in a one-element list --
    # but they are steps rather than tasks: no dates, no identity of their own.
    # Reading them as subtasks would promise more than they are, and this
    # connector cannot write anything back regardless, so nesting is left out
    # deliberately rather than by omission.
    supports_parent=False,
)

def _notes(value: Any) -> str | None:
    """The note text, whatever shape this schema wraps it in.

    Older schemas stored a plain string. Current ones store a small object --
    ``{"_t": "tx", "ch": <checksum>, "v": "the text", "t": 1}`` -- and the text
    is in ``v``. Reading it as a string raised an AttributeError on the first
    real account this connector ever saw, which is a better failure than
    silently importing the word "dict" as somebody's note, but not by much.

    Both shapes are handled because there is no way to know which a given
    account is on, and the endpoint is unpublished, so the next one may differ
    again.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        text = value.get("v")
        if isinstance(text, str):
            return text.strip() or None
    return None


#: Which history entries are to-dos. Things numbers its entity names by schema
#: version -- Task6 on one release, Task7 on the next -- and the connector was
#: written against a list of the names that existed at the time. A live account
#: on schema 301 returns Task7, matched none of them, and read as an empty
#: account: signing in worked, the lists appeared, and every one of them was
#: silently empty.
#:
#: Matching the shape rather than an enumeration means the next bump does not
#: repeat it. The number is deliberately optional, because the oldest entries
#: are plain "Task", and the match is anchored so that no other entity beginning
#: with those four letters is swept in.
_TASK_ENTITY = re.compile(r"Task\d*")

#: Things' own status numbering, as observed.
STATUS_OPEN = 0
STATUS_CANCELLED = 2
STATUS_COMPLETE = 3


class ThingsConnector(Connector):
    service = ServiceKind.THINGS3
    name = "Things 3"

    def __init__(self, account_id: int, credentials: dict, sync_state: dict | None = None,
                 default_timezone: str | None = None):
        super().__init__(account_id, credentials, sync_state)
        self.email = (credentials.get("email") or "").strip()
        self._password = credentials.get("password") or ""
        self.default_timezone = default_timezone or "UTC"
        self._history_key: str | None = credentials.get("history_key")
        self._session_token: str | None = None
        if not self.email or not self._password:
            raise ConnectorError(
                "This Things account has no saved sign-in. Add the Things Cloud "
                "email and password on the Things 3 page first."
            )
        self._client = httpx.Client(timeout=45, headers={
            "Accept": "application/json",
            # Things Cloud rejects requests without a recognisable client.
            "User-Agent": "ThingsMac/31415926 (Task Hub)",
            # Every request carries the password in this header. Not Basic auth,
            # which the server answers with 400, and not a form field: the
            # scheme is the literal word "Password" followed by it. Confirmed
            # against the live endpoint, where the header returns the account
            # and both alternatives fail.
            "Authorization": f"Password {self._password}",
        })
        self.dirty = False

    def close(self) -> None:
        self._client.close()

    @property
    def credentials_changed(self) -> bool:
        return self.dirty

    def current_credentials(self) -> dict:
        return {"email": self.email, "password": self._password,
                "history_key": self._history_key}

    def capabilities(self, kind: CollectionKind) -> Capabilities:
        if kind != CollectionKind.TASKS:
            return Capabilities(fields=frozenset(), can_create=False, can_delete=False)
        return THINGS_CAPABILITIES

    def supports_kind(self, kind: CollectionKind) -> bool:
        # Things has no calendar of any sort.
        return kind == CollectionKind.TASKS

    # -- Session ---------------------------------------------------------------

    def _login(self) -> str:
        """Sign in and remember the account's history key."""
        if self._session_token and self._history_key:
            return self._history_key
        try:
            # GET, not PUT. A PUT to this address changes the account -- which
            # is why it answered 401 to a correct password and would have been
            # a far worse thing to get working. Signing in is simply reading
            # your own account with the password in the header.
            response = self._client.get(f"{BASE}/account/{self.email}")
        except httpx.RequestError as exc:
            raise ConnectorError(f"Could not reach Things Cloud: {exc}") from exc

        if response.status_code in (401, 403):
            raise ConnectorAuthError(
                "Things Cloud knows that email address but rejected the "
                "password. It is your Things Cloud password, which is not your "
                "Apple ID password and not the account you bought Things with "
                "-- it is the one you set when you turned Things Cloud on. If "
                "you are not sure, reset it at culturedcode.com/things/cloud "
                "and Things will ask your devices to sign in again."
            )
        if response.status_code == 429:
            raise RateLimitError("Things Cloud is rate limiting this account.")
        if response.status_code == 400:
            # Confirmed against the live endpoint: an address with no Things
            # Cloud account answers 400, where a wrong password answers 401. The
            # two need opposite advice, and telling somebody their endpoint has
            # changed when they have simply never made an account would send
            # them looking in entirely the wrong place.
            raise ConnectorAuthError(
                "Things Cloud has no account for that email address. Things "
                "Cloud is a separate account you create inside the Things app "
                "-- buying Things does not create one. On a Mac open Things -> "
                "Settings -> Things Cloud, or on an iPhone open Things -> "
                "Settings -> Things Cloud, and sign up or sign in there first. "
                "Check the address you used matches the one this shows."
            )
        if response.status_code >= 400:
            raise ConnectorError(
                f"Things Cloud refused the sign-in (HTTP {response.status_code}). "
                "This connector uses an endpoint Cultured Code does not publish, "
                "so it can stop working when they change it."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectorError(
                "Things Cloud replied with something this connector did not "
                "understand. The unofficial endpoint has probably changed."
            ) from exc

        key = payload.get("history-key") or payload.get("history_key")
        if not key:
            raise ConnectorError(
                "Things Cloud signed in but did not say where this account's "
                "data lives. The unofficial endpoint has probably changed."
            )
        self._history_key = key
        self._session_token = payload.get("SLA-version-accepted") or "ok"
        self.dirty = True
        return key

    def _items(self) -> list[dict]:
        """Every item in the account's history, newest state last."""
        key = self._login()
        collected: list[dict] = []
        index = 0
        for _ in range(200):
            try:
                response = self._client.get(
                    f"{BASE}/history/{key}/items",
                    params={"start-index": index},
                )
            except httpx.RequestError as exc:
                raise ConnectorError(f"Could not read from Things Cloud: {exc}") from exc
            if response.status_code == 401:
                raise ConnectorAuthError("Things Cloud rejected the session. Reconnect the account.")
            if response.status_code >= 400:
                raise ConnectorError(f"Things Cloud returned HTTP {response.status_code}.")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ConnectorError("Things Cloud sent an unreadable response.") from exc

            batch = payload.get("items") or []
            collected.extend(batch)
            latest = payload.get("latest-schema-index") or payload.get("current-item-index")
            if not batch or latest is None or index >= int(latest):
                break
            index = int(latest)
        return collected

    # -- Discovery -------------------------------------------------------------

    def verify(self) -> str:
        self._login()
        # Prove reading works too. Signing in successfully but being unable to
        # read is the failure this connector is most likely to hit.
        self._items()
        return self.email

    def list_remote_lists(self) -> list[RemoteList]:
        """One list, because there is only one this connector can actually tell apart.

        Things organises by area and project, and the history endpoint hands
        back a single undifferentiated stream of to-dos with no reliable way to
        say which built-in list any of them belongs to. Inbox, Today and Anytime
        were offered separately, and against a live account all three returned
        exactly the same thirty-seven items -- so mapping two of them would have
        imported every to-do twice, into a collection the user would then have
        had to clean out by hand.

        Offering one list named for what it is beats offering three that are
        secretly the same. If the sorting ever becomes reliable, splitting this
        up is an additive change; a user who mapped a duplicate is a mess that
        has to be undone.
        """
        self._login()
        return [
            RemoteList(remote_id="all", name="All to-dos", kind=CollectionKind.TASKS,
                       is_default=True),
        ]

    # -- Reading ---------------------------------------------------------------

    def pull(self, remote_list_id: str, kind: CollectionKind,
             since: dt.datetime | None = None, state: dict | None = None) -> PullResult:
        if kind != CollectionKind.TASKS:
            return PullResult(errors=["Things 3 holds tasks only."])

        try:
            raw = self._items()
        except (ConnectorAuthError, RateLimitError):
            raise
        except ConnectorError as exc:
            # Contained on purpose: every other service keeps syncing.
            return PullResult(errors=[str(exc)])

        latest: dict[str, dict] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            for item_id, payload in entry.items():
                if not isinstance(payload, dict):
                    continue
                if not _TASK_ENTITY.fullmatch(str(payload.get("e") or "")):
                    continue
                body = payload.get("p") or {}
                # History is a log: a later entry supersedes an earlier one.
                latest.setdefault(item_id, {}).update(body)

        items: list[RemoteItem] = []
        for item_id, body in latest.items():
            if body.get("tr"):        # a trashed item
                continue
            record = self._to_record(item_id, body)
            if record is None:
                continue
            items.append(RemoteItem(
                remote_id=item_id,
                record=record,
                fields_present=THINGS_CAPABILITIES.present_fields(),
                remote_updated_at=_timestamp(body.get("md")),
            ))

        # Never a complete listing that can be trusted for deletion: the history
        # log is append-only and this connector cannot prove it has seen all of
        # it, so absence must never be read as "deleted".
        return PullResult(items=items, incremental=True)

    def _to_record(self, item_id: str, body: dict) -> CanonicalRecord | None:
        title = (body.get("tt") or "").strip()
        if not title:
            return None
        status = body.get("ss", STATUS_OPEN)
        done = status == STATUS_COMPLETE
        return CanonicalRecord(
            uid=new_uid(),
            kind=CollectionKind.TASKS,
            title=title,
            notes=_notes(body.get("nt")),
            status=(ItemStatus.COMPLETED if done else
                    ItemStatus.CANCELLED if status == STATUS_CANCELLED else
                    ItemStatus.NEEDS_ACTION),
            completed_at=_timestamp(body.get("sp")),
            due_date=_date(body.get("dd")) or _date(body.get("sr")),
            # Things has no time of day on a to-do; see THINGS_CAPABILITIES.
            due_time=None, due_tz=None,
            tags=list(body.get("tg") or []),
            origin_service=ServiceKind.THINGS3,
            created_at=_timestamp(body.get("cd")),
            updated_at=_timestamp(body.get("md")),
        )

    # -- Writing ---------------------------------------------------------------

    def create(self, remote_list_id: str, record: CanonicalRecord,
               kind: CollectionKind) -> PushOutcome:
        return PushOutcome(remote_id=None, error=self._write_unavailable())

    def update(self, remote_list_id: str, remote_id: str, record: CanonicalRecord,
               kind: CollectionKind) -> PushOutcome:
        return PushOutcome(remote_id=remote_id, error=self._write_unavailable())

    def delete(self, remote_list_id: str, remote_id: str,
               kind: CollectionKind) -> PushOutcome:
        return PushOutcome(remote_id=remote_id, error=self._write_unavailable())

    @staticmethod
    def _write_unavailable() -> str:
        return (
            "Task Hub reads from Things 3 but does not write to it. Writing "
            "through the unofficial endpoint means composing entries in an "
            "undocumented log format, and a wrong guess there would corrupt "
            "the Things database rather than simply failing. Read-only is the "
            "honest limit until Cultured Code publishes an API."
        )


def _timestamp(value: Any) -> dt.datetime | None:
    """Things records times as a Unix timestamp."""
    if value in (None, "", 0):
        return None
    try:
        return dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _date(value: Any) -> dt.date | None:
    if value in (None, "", 0):
        return None
    try:
        return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).date()
    except (TypeError, ValueError, OSError):
        pass
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
