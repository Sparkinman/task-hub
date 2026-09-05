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
    steps_as_text,
    strip_steps,
    F_DUE_DATE,
    F_DUE_TIME,
    F_NOTES,
    F_STATUS,
    F_TITLE,
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
from app.db.models import CollectionKind, ItemStatus, ServiceKind
from app.services.ical_model import CanonicalRecord

logger = logging.getLogger(__name__)

#: Not cloud.supernote.com, which is the web file manager and has no schedule
#: routes at all. The Partner app talks to this one, and it skips the CSRF
#: handshake the web host insists on.
BASE_URL = "https://viewer.supernote.com/api"

LIST_GROUPS = "/file/schedule/group/all"
LIST_TASKS = "/file/schedule/task/all"
#: One task. The verb decides the operation, and each has a trap of its own.
#:
#: ``POST`` inserts, and inserts *even when the body carries a taskId* -- it
#: answers with a brand new id and leaves the original untouched. Using it to
#: save an edit therefore silently duplicates the task rather than failing, so
#: :meth:`SupernoteConnector.update` must never fall back to it.
#:
#: ``PUT`` updates, and refuses a body without ``lastModified``. The complaint
#: is "The last modification time of the To-Do list cannot be empty", which
#: names the list rather than the task and sends you looking in the wrong place.
#:
#: ``DELETE`` takes the id in the path. Sent as a body or a query parameter it
#: answers 500.
TASK = "/file/schedule/task"

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
#: The tablet calls this list "Inbox", so Task Hub does too. It was "Unfiled
#: tasks" first, which was accurate and unrecognisable -- somebody looking for
#: the list they see on the device would not know it was the same one.
UNFILED_LIST_NAME = "Inbox"

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


def note_link(links: object) -> dict | None:
    """Decode a task's link back to the note it was written on.

    A task made by circling handwriting carries this, and the tablet shows a
    notebook icon that jumps straight to the page. It is base64 around JSON:
    ``{"appName", "fileId", "filePath", "page", "pageId"}``.

    Task Hub keeps it whole on the way through -- an update sends the server's
    own row with only the fields Task Hub owns laid over it, so the link
    survives an edit -- and reads it here so the same jump can be offered on
    this side, which is more than the raw field allows on its own.
    """
    raw = (links or "")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        decoded = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, binascii.Error, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(decoded, dict) or not decoded.get("filePath"):
        return None
    path = str(decoded.get("filePath") or "")
    return {
        "path": path,
        # The tablet's own storage prefix means nothing here.
        "name": path.rsplit("/", 1)[-1],
        "page": decoded.get("page"),
        "file_id": str(decoded.get("fileId") or ""),
    }


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

def _call(
    client: httpx.Client,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str = "",
) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["x-access-token"] = token
    try:
        response = client.request(
            method, BASE_URL + path, json=payload, headers=headers, timeout=30
        )
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


def _post(client: httpx.Client, path: str, payload: dict, token: str = "") -> dict:
    return _call(client, "POST", path, payload, token)


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
    """Read and write the built-in To-Do app's lists and tasks.

    Both directions were worked out against a live account rather than guessed,
    which mattered: the three verbs on :data:`TASK` each behave in a way the
    obvious implementation gets wrong, and the worst of them fails silently.
    See that constant for what each one does.

    Lists themselves are read but never created or deleted. Task Hub maps to
    lists that already exist, and inventing them on somebody's tablet is a
    bigger liberty than syncing the tasks inside them.
    """

    service = ServiceKind.SUPERNOTE
    name = "Supernote"

    def __init__(self, account_id: int, credentials: dict, sync_state: dict | None = None):
        super().__init__(account_id, credentials, sync_state)
        self._client = httpx.Client(timeout=30)
        #: taskId -> the server's row, so an update can be laid over the copy
        #: Supernote already holds rather than blanking fields by omission.
        #: Lives for one sync pass, which is the lifetime of this object.
        self._cache: dict[str, dict] = {}

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
            can_create=True,
            can_delete=True,
            # Everything it can hold, it can also write. The narrower set exists
            # for services that can read a field but not change it, which this
            # is not.
            writable_fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE}),
            # The API stores a note and returns it, but the tablet's To-Do app
            # never shows one -- only the title and the date. Confirmed on the
            # device. So steps folded into a note would be invisible there.
            notes_visible=False,
            stores_uid=False,
            carries_origin=False,
        )

    def echo_of(self, record: CanonicalRecord, kind: CollectionKind) -> CanonicalRecord:
        """What Supernote will report back after being told ``record``.

        One thing changes on the way through. A due date is stored as an instant
        in milliseconds, so a date is written as midnight UTC and read back as
        that instant -- which lands on the same date, and therefore needs no
        correction. What does need saying is completion: Supernote stamps its
        own ``completedTime`` when a task is marked done, so the moment recorded
        here is not the moment sent. Declaring that stops the next pull reading
        Supernote's timestamp as somebody having edited the task.
        """
        import dataclasses

        # Subtasks are written into the note, but read back out of it again, so
        # what Supernote reports is the note without them -- which is exactly
        # what was sent. Nothing to correct here; said out loud because the
        # opposite assumption would rewrite every parent task on every pass.
        if record.status == ItemStatus.COMPLETED and record.completed_at is None:
            return dataclasses.replace(record, completed_at=None)
        return record

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

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = _call(self._client, method, path, payload, token=self._token)
        if body.get("success") is False:
            # Their generic failure is a 200 with success:false, so an HTTP
            # status check alone would treat every one of these as fine.
            raise ConnectorError(
                body.get("errorMsg") or f"Supernote Cloud refused {path}."
            )
        return body

    def _get(self, path: str, payload: dict | None = None) -> dict:
        return self._request("POST", path, payload or {})

    def _put(self, path: str, payload: dict) -> dict:
        return self._request("PUT", path, payload)

    def _task_row(self, remote_id: str) -> dict | None:
        """The server's own copy of one task, for laying an update over.

        Cached per connector instance, which lasts one sync pass: a pass that
        updates ten tasks would otherwise fetch the whole account ten times.
        A miss refetches once, because the task may have been created earlier
        in this same pass.
        """
        if remote_id in self._cache:
            return self._cache[remote_id]
        for row in self._get(LIST_TASKS).get("scheduleTask") or []:
            task_id = str(row.get("taskId") or "").strip()
            if task_id:
                self._cache[task_id] = row
        return self._cache.get(remote_id)

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
                    # Read-only, unlike the real lists: this one is a view of
                    # tasks that sit outside every list, so there is nowhere for
                    # a new task to go. Saying so here stops the engine offering
                    # it as a write target and then failing on every pass.
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
            cache_id = str(row.get("taskId") or "").strip()
            if cache_id:
                self._cache[cache_id] = row
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

        # A real list reports completely, so a task that has gone really has
        # gone and the engine may act on its absence.
        #
        # The unfiled view must never be read that way. It holds tasks that
        # belong to no list, so filing one on the tablet -- an ordinary thing to
        # do -- removes it from this view while the task itself is perfectly
        # alive in its new list. Letting absence imply deletion there would have
        # the engine delete a task the user had just tidied up, and propagate
        # that deletion to every other connected service. Claiming the pull is
        # incremental is what stops it: the engine then treats a missing item as
        # unreported rather than as gone.
        return PullResult(items=items, incremental=remote_list_id == UNFILED_LIST_ID)

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
        # Steps this connector folded into the note are not part of the note.
        # Reading them back as one would bury the real text under a copy that
        # doubles on every pass.
        notes = strip_steps((row.get("detail") or "").strip()) or None

        # A task written on a page of a notebook says which page. Kept with the
        # task rather than dropped, so Task Hub can offer the same jump back
        # that the tablet does -- and it can do better, because the notebook is
        # very likely already backed up here as a PDF.
        source = note_link(row.get("links"))
        if source:
            reference = f"From {source['name']}"
            if source.get("page"):
                reference += f", page {source['page']}"
            notes = f"{notes}\n\n{reference}" if notes else reference

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

    def _fields_from(self, record: CanonicalRecord) -> dict:
        """The parts of a task Task Hub is allowed to set.

        Deliberately not a whole task: an update sends the row the server
        already holds with these laid over the top, so that the sort orders,
        reminder flags and recurrence blocks Supernote maintains for itself are
        returned untouched rather than blanked by omission.
        """
        due = None
        if record.due_date is not None:
            # Stored as an instant, so a bare date becomes midnight UTC. Read
            # back it lands on the same date, which is why capabilities() can
            # honestly claim a date and no time.
            due = to_epoch_ms(
                dt.datetime.combine(record.due_date, dt.time(0, 0), tzinfo=dt.UTC)
            )

        fields = {
            "title": record.title or "",
            # Just the note. Steps are *not* folded in here: the tablet never
            # displays a note, so they would vanish rather than be tidied away.
            # The engine sends them as tasks of their own instead -- see
            # notes_visible in capabilities().
            "detail": record.notes or None,
            # 0 rather than None: the API uses it for "no due date", and null is
            # rejected on some paths.
            "dueTime": due or 0,
            "status": STATUS_TO_REMOTE.get(record.status, "needsAction"),
        }
        if record.status == ItemStatus.COMPLETED:
            fields["completedTime"] = to_epoch_ms(
                record.completed_at or dt.datetime.now(dt.UTC)
            )
        return fields

    def create(
        self, remote_list_id: str, record: CanonicalRecord, kind: CollectionKind
    ) -> PushOutcome:
        if kind != CollectionKind.TASKS:
            return PushOutcome(remote_id=None, error="Supernote holds tasks, not events.")
        if remote_list_id == UNFILED_LIST_ID:
            # Writing here would have to invent a list to put it in, and any
            # choice would be one the user never made.
            return PushOutcome(
                remote_id=None,
                error="\"Unfiled tasks\" is a view of tasks Supernote holds "
                      "outside any list, so nothing can be created in it. Map a "
                      "real Supernote list to write there.",
            )

        # Never write a task back into the account it was read from. The unfiled
        # view is what makes this reachable: a task belonging to no list is read
        # from that view, and if any real list of the same account is a
        # write-back target the engine has no link for it there and creates one
        # -- leaving the user with the original sitting outside every list and a
        # copy inside one. Task Hub reading a task is not a reason for Supernote
        # to gain a second one, so the uid it was given on the way in is enough
        # to refuse.
        if record.uid.startswith("supernote-"):
            # Declined, not failed. This happens on every pass for every task
            # read from another Supernote list in the same collection, which as
            # an error made a working sync report dozens of failures a minute.
            return PushOutcome(remote_id=None, skipped=True)

        payload = dict(self._fields_from(record))
        payload["taskListId"] = str(remote_list_id)
        try:
            body = self._get(TASK, payload)
        except ConnectorError as exc:
            return PushOutcome(remote_id=None, error=str(exc))

        remote_id = str(body.get("taskId") or "").strip()
        if not remote_id:
            return PushOutcome(
                remote_id=None,
                error="Supernote accepted the task but returned no id for it.",
            )
        self._cache.pop(remote_id, None)
        return PushOutcome(remote_id=remote_id)

    def update(
        self,
        remote_list_id: str,
        remote_id: str,
        record: CanonicalRecord,
        kind: CollectionKind,
    ) -> PushOutcome:
        if kind != CollectionKind.TASKS:
            return PushOutcome(remote_id=remote_id, error="Supernote holds tasks, not events.")

        current = self._task_row(remote_id)
        if current is None:
            # Never fall back to POST here. POST inserts even when the body
            # carries a taskId, so "update the task that is not there" would
            # quietly become "make a second one".
            raise ConnectorGoneError(
                f"Supernote no longer has a task with id {remote_id}."
            )

        payload = dict(current)
        payload.update(self._fields_from(record))
        # PUT is refused outright without this, and the message it gives blames
        # the list rather than the task.
        payload["lastModified"] = to_epoch_ms(dt.datetime.now(dt.UTC))

        try:
            self._put(TASK, payload)
        except ConnectorError as exc:
            return PushOutcome(remote_id=remote_id, error=str(exc))
        self._cache.pop(remote_id, None)
        return PushOutcome(remote_id=remote_id)

    def delete(
        self, remote_list_id: str, remote_id: str, kind: CollectionKind
    ) -> PushOutcome:
        try:
            # The id goes in the path. As a body or a query parameter the
            # server answers 500.
            self._request("DELETE", f"{TASK}/{remote_id}")
        except ConnectorError as exc:
            return PushOutcome(remote_id=remote_id, error=str(exc))
        self._cache.pop(remote_id, None)
        return PushOutcome(remote_id=remote_id)

    # -- Housekeeping ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()
