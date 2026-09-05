"""Supernote Cloud: the to-do lists from the e-ink tablet's built-in To-Do app.

This is the one connector in Task Hub built on an API nobody published. Ratta
document nothing, and the two open-source Supernote clients that exist cover
file storage only. The endpoints here were read out of the Partner app's own
compiled Dart, and confirmed against a live account.

That has consequences the rest of the code has to respect:

* **It can stop working without warning.** Nothing here is versioned or
  promised. A Partner app release could rename any of it, and the first anyone
  would know is a sync failing. The service is marked accordingly everywhere it
  is shown, in stronger terms than the usual "not yet tested" label.
* **The vocabulary is theirs, not ours.** A to-do list is a "schedule task
  group", a task is a "schedule task", and booleans are the strings ``"Y"`` and
  ``"N"``. Every one of those is translated here so nothing leaks outwards.

Two things about the data are worth knowing before changing anything:

``completedTime`` does not mean the task is finished. Every task on the account
this was built against carried one while all of them reported
``status: needsAction``. Reading completion from that field would have marked
every task done and pushed it to every other service the moment sync ran.
:data:`STATUS_FROM_REMOTE` reads ``status`` and nothing else, and
``completedTime`` is used only to fill in *when* something was completed once
``status`` has already said that it was.

The session is a JWT valid for thirty days with no renewal endpoint of any
kind -- ``/user/info``, ``/quickLogin`` and a ``login/new2`` were all tried and
none exist. So a token cannot be refreshed in the background: it runs out and a
person has to sign in again, with a verification code emailed to them. Since
the expiry is legible inside the token, :func:`token_expiry` reads it and the
service page warns before the day arrives rather than after.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import logging

import httpx

from app.connectors.base import (
    F_DUE_DATE,
    F_DUE_TIME,
    F_NOTES,
    F_STATUS,
    F_TITLE,
    Capabilities,
    Connector,
    ConnectorAuthError,
    ConnectorError,
    PullResult,
    PushOutcome,
    RemoteItem,
    RemoteList,
)
from app.db.models import CollectionKind, ItemStatus, ServiceKind
from app.services.ical_model import CanonicalRecord

logger = logging.getLogger(__name__)

#: Not cloud.supernote.com, which is the web file manager and has no schedule
#: routes at all. The Partner app talks to this one, and it skips the CSRF
#: handshake the web host insists on.
BASE_URL = "https://viewer.supernote.com/api"

LIST_GROUPS = "/file/schedule/group/all"
LIST_TASKS = "/file/schedule/task/all"

#: Stand-in list for tasks that belong to no list at all.
#:
#: The account this was built against had one: a task with ``taskListId: null``,
#: sitting in the To-Do app's "All" view and in none of its four lists. Filtering
#: tasks by list -- the obvious implementation -- dropped it without a word,
#: which is the worst way to lose somebody's data. Rather than guess a list for
#: it, which would put it somewhere the user never chose, it is offered as a
#: list of its own that they can map or ignore knowingly.
#:
#: The value cannot collide with a real id: theirs are 32-character hex strings,
#: or the literal "1" for the default list.
UNFILED_LIST_ID = "__unfiled__"
UNFILED_LIST_NAME = "Unfiled tasks"

#: Their status vocabulary happens to match Google Tasks exactly.
STATUS_FROM_REMOTE = {
    "needsAction": ItemStatus.NEEDS_ACTION,
    "completed": ItemStatus.COMPLETED,
}
STATUS_TO_REMOTE = {
    ItemStatus.NEEDS_ACTION: "needsAction",
    ItemStatus.COMPLETED: "completed",
}


# --- Small conversions --------------------------------------------------------

def yes(value: object) -> bool:
    """Their booleans are the strings "Y" and "N"."""
    return str(value or "").strip().upper() == "Y"


def from_epoch_ms(value: object) -> dt.datetime | None:
    """Milliseconds since the epoch, UTC, or None for anything unusable.

    Zero is treated as absent rather than as 1970: the API uses it for "not
    set", and a task due at the dawn of the epoch would be a strange thing to
    show somebody.
    """
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value / 1000, dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def to_epoch_ms(moment: dt.datetime | None) -> int | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return int(moment.timestamp() * 1000)


def token_expiry(token: str) -> dt.datetime | None:
    """When this session token stops working, read out of the token itself.

    The token is a JWT whose payload carries ``exp``. Reading it means Task Hub
    can say "this expires on the 5th" while everything still works, instead of
    discovering it during a sync at three in the morning. Anything unexpected
    returns None, which callers treat as "unknown" rather than "expired" -- a
    wrong guess in that direction would nag about a working account forever.
    """
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    expires = claims.get("exp")
    if not isinstance(expires, (int, float)) or expires <= 0:
        return None
    # Seconds or milliseconds; both appear in this API depending on the field.
    seconds = expires / 1000 if expires > 1e11 else expires
    try:
        return dt.datetime.fromtimestamp(seconds, dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def password_digest(password: str, random_code: str) -> str:
    """Hex MD5 of the password, then SHA-256 of that with the server's nonce.

    The plain password never crosses the wire, which is why the login is a
    two-step dance rather than one request.
    """
    return hashlib.sha256(
        (hashlib.md5(password.encode()).hexdigest() + random_code).encode()
    ).hexdigest()


# --- Signing in ---------------------------------------------------------------
#
# Kept as module functions rather than methods: the web layer runs these while
# there is no account to build a connector from yet.

def _post(client: httpx.Client, path: str, payload: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["x-access-token"] = token
    try:
        response = client.post(BASE_URL + path, json=payload, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        raise ConnectorError(f"Could not reach Supernote Cloud: {exc}") from exc
    if response.status_code in (401, 403):
        raise ConnectorAuthError(
            "Supernote Cloud rejected this session. It lasts thirty days and "
            "cannot be renewed automatically, so sign in again to continue."
        )
    try:
        body = response.json()
    except ValueError:
        raise ConnectorError(
            f"Supernote Cloud answered {response.status_code} with something "
            "that was not JSON. The unofficial API it uses may have changed."
        ) from None
    if not isinstance(body, dict):
        raise ConnectorError("Supernote Cloud returned an unexpected response.")
    return body


def begin_sign_in(email: str, password: str) -> dict:
    """Step one: offer the password, and ask for the emailed code.

    Returns either ``{"token": ...}`` when the account needs no verification, or
    ``{"validCodeKey": ..., "timestamp": ...}`` to hand back to
    :func:`finish_sign_in` along with the code the user received.
    """
    email = email.strip()
    with httpx.Client() as client:
        challenge = _post(
            client, "/official/user/query/random/code",
            {"countryCode": "1", "account": email},
        )
        random_code = challenge.get("randomCode")
        timestamp = challenge.get("timestamp")
        if not random_code:
            raise ConnectorError(
                challenge.get("errorMsg") or "Supernote Cloud would not start a sign-in."
            )

        result = _post(client, "/official/user/account/login/new", {
            "countryCode": 1,
            "account": email,
            "password": password_digest(password, random_code),
            "browser": "Chrome107",
            "equipment": "1",
            "loginMethod": "1",
            "timestamp": timestamp,
            "language": "en",
        })
        if result.get("token"):
            return {"token": result["token"]}
        if result.get("errorCode") != "E1760":
            raise ConnectorAuthError(
                result.get("errorMsg") or "Supernote Cloud refused those details."
            )

        # A code is wanted. The endpoint that sends it is signed with a key the
        # server hides inside a token it hands out: the token's last character
        # is an index into its own dash-separated parts, and the part at that
        # index is what gets hashed with the address.
        pre_auth = _post(client, "/user/validcode/pre-auth", {"account": email})
        pre_token = pre_auth.get("token") or ""
        try:
            real_key = pre_token.split("-")[int(pre_token[-1])]
        except (ValueError, IndexError):
            raise ConnectorError(
                "Supernote Cloud returned a verification token in a shape this "
                "version does not recognise."
            ) from None

        sent = _post(client, "/user/mail/validcode/send", {
            "email": email,
            "timestamp": timestamp,
            "token": pre_token,
            "sign": hashlib.sha256(f"{email}{real_key}".encode()).hexdigest(),
        })
        if not sent.get("validCodeKey"):
            raise ConnectorError(
                sent.get("errorMsg") or "Supernote Cloud would not send a verification code."
            )
        return {"validCodeKey": sent["validCodeKey"], "timestamp": timestamp}


def finish_sign_in(email: str, code: str, valid_code_key: str, timestamp: object) -> str:
    """Step two: exchange the emailed code for a session token."""
    with httpx.Client() as client:
        result = _post(client, "/official/user/sms/login", {
            "email": email.strip(),
            "validCode": code.strip().upper(),
            "validCodeKey": valid_code_key,
            "timestamp": timestamp,
            "browser": "Chrome107",
            "equipment": "4",
        })
    token = result.get("token")
    if not token:
        raise ConnectorAuthError(
            result.get("errorMsg")
            or "That verification code was not accepted. They expire quickly, so "
               "request a new one if it has been more than a few minutes."
        )
    return token


# --- The connector ------------------------------------------------------------

class SupernoteConnector(Connector):
    """Read the built-in To-Do app's lists and tasks.

    Read-only for now. The Partner app's own code carries ``taskInsert``,
    ``taskUpdate``, ``taskDelete`` and ``taskConfirm``, so writing back is
    reachable, but the request shapes for those were never observed against a
    live account -- and a write built on a guess would damage the user's tasks
    rather than merely fail to read them.
    """

    service = ServiceKind.SUPERNOTE
    name = "Supernote"

    def __init__(self, account_id: int, credentials: dict, sync_state: dict | None = None):
        super().__init__(account_id, credentials, sync_state)
        self._client = httpx.Client(timeout=30)

    # -- Capabilities ---------------------------------------------------------

    def capabilities(self, kind: CollectionKind) -> Capabilities:
        """Titles, notes, completion and a due date -- and no due *time*.

        Their due date is an epoch-millisecond instant, but the To-Do app only
        ever offers a date to set it from, so a time read back would be an
        artefact of the encoding rather than anything the user chose. Declaring
        due_time False is what stops that artefact overwriting a real time set
        in Todoist or Google.
        """
        return Capabilities(
            fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE}),
            can_create=False,
            can_delete=False,
            writable_fields=frozenset(),
            stores_uid=False,
            carries_origin=False,
        )

    def supports_kind(self, kind: CollectionKind) -> bool:
        return kind == CollectionKind.TASKS

    # -- Session --------------------------------------------------------------

    @property
    def _token(self) -> str:
        token = self.credentials.get("token") or ""
        if not token:
            raise ConnectorAuthError("This Supernote account has no saved session.")
        return token

    def expires_at(self) -> dt.datetime | None:
        return token_expiry(self.credentials.get("token") or "")

    def _get(self, path: str, payload: dict | None = None) -> dict:
        body = _post(self._client, path, payload or {}, token=self._token)
        if body.get("success") is False:
            # Their generic failure is a 200 with success:false, so an HTTP
            # status check alone would treat every one of these as fine.
            raise ConnectorError(
                body.get("errorMsg") or f"Supernote Cloud refused {path}."
            )
        return body

    def verify(self) -> str:
        """Confirm the session works, and say when it runs out."""
        self._get(LIST_GROUPS)
        expires = self.expires_at()
        identity = self.credentials.get("email") or "Supernote"
        if expires:
            return f"{identity} (session valid until {expires:%-d %B %Y})"
        return identity

    # -- Discovery ------------------------------------------------------------

    def _live_list_ids(self) -> set[str]:
        """The ids of every list that currently exists and is not deleted."""
        body = self._get(LIST_GROUPS)
        return {
            str(row.get("taskListId")).strip()
            for row in body.get("scheduleTaskGroup") or []
            if row.get("taskListId") is not None and not yes(row.get("isDeleted"))
        }

    def list_remote_lists(self) -> list[RemoteList]:
        body = self._get(LIST_GROUPS)
        lists: list[RemoteList] = []
        for row in body.get("scheduleTaskGroup") or []:
            if yes(row.get("isDeleted")):
                continue
            remote_id = str(row.get("taskListId") or "").strip()
            if not remote_id:
                continue
            lists.append(
                RemoteList(
                    remote_id=remote_id,
                    name=(row.get("title") or "").strip() or "Untitled list",
                    kind=CollectionKind.TASKS,
                    read_only=True,
                )
            )

        # Only offered when something is actually in it, so that an account with
        # every task properly filed never sees a puzzling empty list.
        if self._unfiled_count(lists):
            lists.append(
                RemoteList(
                    remote_id=UNFILED_LIST_ID,
                    name=UNFILED_LIST_NAME,
                    kind=CollectionKind.TASKS,
                    read_only=True,
                )
            )
        return lists

    def _unfiled_count(self, known: list[RemoteList]) -> int:
        live = {entry.remote_id for entry in known}
        body = self._get(LIST_TASKS)
        return sum(
            1
            for row in body.get("scheduleTask") or []
            if not yes(row.get("isDeleted")) and not self._filed_under(row, live)
        )

    @staticmethod
    def _filed_under(row: dict, live_ids: set[str]) -> bool:
        """Whether this task belongs to a list that still exists.

        A task can fail this two ways: no list id at all, or one naming a list
        that has since been deleted. Both leave it invisible to a per-list read,
        so both are treated the same.
        """
        raw = row.get("taskListId")
        return raw is not None and str(raw).strip() in live_ids

    # -- Reading --------------------------------------------------------------

    def pull(
        self,
        remote_list_id: str,
        kind: CollectionKind,
        since: dt.datetime | None = None,
        state: dict | None = None,
    ) -> PullResult:
        """Every task in one list.

        The API returns all of an account's tasks at once and tags each with the
        list it belongs to, so this filters rather than making a request per
        list. ``nextSyncToken`` comes back in the response and would allow a
        delta read, but replaying it under the obvious parameter name returned
        everything unchanged, so this asks for the full set and says so. A wrong
        guess there would look like "nothing changed" forever.
        """
        if kind != CollectionKind.TASKS:
            return PullResult(items=[], incremental=False)

        body = self._get(LIST_TASKS)
        items: list[RemoteItem] = []
        present = self.capabilities(kind).fields

        # Only needed for the unfiled list, and it costs a request, so it is not
        # fetched when reading an ordinary one.
        live_ids = self._live_list_ids() if remote_list_id == UNFILED_LIST_ID else set()

        for row in body.get("scheduleTask") or []:
            if remote_list_id == UNFILED_LIST_ID:
                if self._filed_under(row, live_ids):
                    continue
            elif str(row.get("taskListId") or "") != str(remote_list_id):
                continue
            remote_id = str(row.get("taskId") or "").strip()
            if not remote_id:
                continue

            record = self._record_from(row, remote_id)
            items.append(
                RemoteItem(
                    remote_id=remote_id,
                    record=record,
                    fields_present=present,
                    remote_updated_at=from_epoch_ms(row.get("lastModified")),
                    deleted=yes(row.get("isDeleted")),
                )
            )
        return PullResult(items=items, incremental=False)

    def _record_from(self, row: dict, remote_id: str) -> CanonicalRecord:
        status = STATUS_FROM_REMOTE.get(
            str(row.get("status") or ""), ItemStatus.NEEDS_ACTION
        )

        # completedTime is present on tasks that are not completed, so it is
        # read only to date a completion that `status` has already established.
        # Trusting it on its own would mark everything done. See the module
        # docstring: this is the mistake this connector exists having avoided.
        completed_at = None
        if status == ItemStatus.COMPLETED:
            completed_at = from_epoch_ms(row.get("completedTime"))

        due = from_epoch_ms(row.get("dueTime"))
        notes = (row.get("detail") or "").strip() or None

        return CanonicalRecord(
            uid=f"supernote-{remote_id}",
            kind=CollectionKind.TASKS,
            title=(row.get("title") or "").strip(),
            notes=notes,
            status=status,
            completed_at=completed_at,
            # Date only, deliberately: see capabilities().
            due_date=due.date() if due else None,
            origin_service=ServiceKind.SUPERNOTE,
            origin_name=self.name,
            updated_at=from_epoch_ms(row.get("lastModified")),
        )

    # -- Writing --------------------------------------------------------------
    #
    # Declared unsupported rather than silently doing nothing, so that a
    # misconfiguration surfaces as a clear message instead of tasks quietly
    # failing to appear on the tablet.

    _READ_ONLY = (
        "Task Hub can read the Supernote To-Do app but not write to it yet. Its "
        "write API exists but has never been exercised against a real account, "
        "and a wrong guess there would damage tasks rather than just fail."
    )

    def create(self, remote_list_id: str, record: CanonicalRecord, kind: CollectionKind) -> PushOutcome:
        return PushOutcome(remote_id=None, error=self._READ_ONLY)

    def update(
        self, remote_list_id: str, remote_id: str, record: CanonicalRecord, kind: CollectionKind
    ) -> PushOutcome:
        return PushOutcome(remote_id=remote_id, error=self._READ_ONLY)

    def delete(self, remote_list_id: str, remote_id: str, kind: CollectionKind) -> PushOutcome:
        return PushOutcome(remote_id=remote_id, error=self._READ_ONLY)

    # -- Housekeeping ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()
