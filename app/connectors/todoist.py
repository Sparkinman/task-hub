"""Todoist, through the unified API v1.

Todoist used to have two separate interfaces -- REST v2 and Sync v9 -- and both
were shut down in early 2026 in favour of a single ``/api/v1``. Nothing here
talks to the old ones: the base URL, the paginated response shape and the object
ids are all different, and code written against v2 does not merely warn, it
fails outright.

Two consequences of v1 worth knowing while reading this file:

* Access tokens now expire after an hour, and applications created since the
  change are issued a refresh token. Older applications still hold a long-lived
  token and never get one, so :class:`TodoistAuth` treats a missing refresh
  token as "this token is simply valid" rather than as an error.
* Every list endpoint is cursor-paginated and answers ``{"results": [...],
  "next_cursor": ...}``, not a bare array.
"""

from __future__ import annotations

import logging

import datetime as dt
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.connectors.base import (
    F_DUE_DATE,
    F_DUE_TIME,
    F_NOTES,
    F_PRIORITY,
    F_START,
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
from app.services.ical_model import CanonicalRecord
from app.services.timezones import to_utc, wall_time

logger = logging.getLogger(__name__)

API_BASE = "https://api.todoist.com/api/v1"
AUTH_ENDPOINT = "https://app.todoist.com/oauth/authorize"
TOKEN_ENDPOINT = "https://api.todoist.com/oauth/access_token"

#: Read and write tasks, and delete them. Todoist separates deletion into its
#: own scope, and without it a task removed in another service could never be
#: removed here -- it would silently come back on the next pull.
SCOPES = "data:read_write,data:delete"

#: Todoist truncates rather than refusing, so we truncate deliberately instead
#: and keep the canonical copy intact.
CONTENT_LIMIT = 500
DESCRIPTION_LIMIT = 16384

#: How far back to look for tasks completed elsewhere. Todoist will not return
#: an unbounded completed history, and a task completed more than a month ago is
#: not something another service still needs telling about.
COMPLETED_WINDOW_DAYS = 30

_PAGE_LIMIT = 200

#: How many vanished tasks to ask about individually in one pull. A project that
#: loses hundreds at once is far more likely to be a Todoist fault than a burst
#: of productivity.
MAX_PROBES = 60


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Where to send the browser to start the OAuth dance."""
    return f"{AUTH_ENDPOINT}?" + urlencode(
        {
            "client_id": client_id,
            "scope": SCOPES,
            "state": state,
            "redirect_uri": redirect_uri,
        }
    )


def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    """Turn the authorization code into stored credentials."""
    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise ConnectorAuthError(_explain_token_error(response))

    payload = response.json()
    if not payload.get("access_token"):
        raise ConnectorAuthError("Todoist did not return an access token.")

    expires_in = payload.get("expires_in")
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        # A legacy application gets no expiry at all; recording 0 makes
        # TodoistAuth treat the token as good until the service says otherwise.
        "expires_at": time.time() + int(expires_in) if expires_in else 0.0,
    }


def _explain_token_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") or payload.get("error_description") or ""

    if error == "invalid_client":
        return (
            "Todoist rejected the Client ID or Client Secret. Check both on the "
            "Todoist App Management page, and that you copied them into the "
            "matching boxes."
        )
    if error == "redirect_uri_mismatch":
        return (
            "The redirect URI Task Hub sent does not match the one registered "
            "with your Todoist app. They must be identical, including http vs "
            "https, the port and any trailing slash."
        )
    if error == "invalid_grant":
        return (
            "That authorization code was already used or has expired. Start the "
            "connection again."
        )
    return f"Todoist returned {response.status_code}: {error or response.text[:200]}"


class TodoistAuth:
    """Holds an account's tokens and refreshes them when v1 expires them."""

    def __init__(self, client_id: str, client_secret: str, credentials: dict):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = credentials.get("access_token")
        self.refresh_token = credentials.get("refresh_token")
        expiry = credentials.get("expires_at")
        self.expires_at = float(expiry) if expiry else 0.0
        self.dirty = False

    def token(self) -> str:
        if not self.access_token:
            raise ConnectorAuthError(
                "This Todoist account has no saved login. Click Reconnect."
            )
        # No refresh token means a legacy long-lived token: there is nothing to
        # refresh, and treating its absent expiry as "expired" would lock out an
        # account that works perfectly well.
        if not self.refresh_token:
            return self.access_token
        # Refresh a minute early so a token cannot expire mid-request, where a
        # write has no safe automatic retry.
        if self.expires_at and time.time() < self.expires_at - 60:
            return self.access_token
        if not self.expires_at:
            return self.access_token
        self._refresh()
        return self.access_token  # type: ignore[return-value]

    def _refresh(self) -> None:
        response = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise ConnectorAuthError(
                "Todoist would not renew this login: "
                f"{_explain_token_error(response)} Click Reconnect."
            )
        payload = response.json()
        self.access_token = payload["access_token"]
        expires_in = payload.get("expires_in")
        self.expires_at = time.time() + int(expires_in) if expires_in else 0.0
        if payload.get("refresh_token"):
            self.refresh_token = payload["refresh_token"]
        self.dirty = True

    def as_credentials(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }


# --- Priority -----------------------------------------------------------------
#
# Todoist runs 1-4 with 4 as the most urgent and 1 meaning "no priority set".
# iCalendar runs 1-9 with 1 as the most urgent and 0 meaning unset -- the two
# scales are inverted as well as differently sized. Both directions are written
# out explicitly rather than computed, because an arithmetic conversion that is
# subtly wrong in the middle of the range is very hard to notice.

_TO_CANONICAL = {4: 1, 3: 3, 2: 5, 1: 0}


def todoist_priority_to_canonical(value: Any) -> int:
    try:
        return _TO_CANONICAL.get(int(value), 0)
    except (TypeError, ValueError):
        return 0


def canonical_priority_to_todoist(value: int | None) -> int:
    if not value:
        return 1
    if value <= 2:
        return 4
    if value <= 4:
        return 3
    return 2


def _parse_due(
    due: dict | None, default_zone: str | None = None
) -> tuple[dt.date | None, dt.time | None, str | None]:
    """Split Todoist's due object into date, time and timezone.

    ``date`` is ``YYYY-MM-DD`` for an all-day task, a floating local timestamp
    for a task due at a particular clock time, or a timestamp with an offset for
    one pinned to an instant. The last of those has to be converted into the
    task's own timezone before its clock face is read -- ``14:30Z`` on a task
    whose timezone is Europe/London is half past three there, not half past two,
    and storing the wrong one moves the task in every other service too.
    """
    if not due:
        return None, None, None
    raw = due.get("date")
    if not raw:
        return None, None, None

    if len(raw) == 10:
        try:
            return dt.date.fromisoformat(raw), None, None
        except ValueError:
            return None, None, None

    return wall_time(raw, due.get("timezone"), default_zone)


def _parse_timestamp(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class TodoistConnector(Connector):
    service = ServiceKind.TODOIST
    name = "Todoist"

    def __init__(
        self,
        account_id: int,
        credentials: dict,
        sync_state: dict | None = None,
        client_id: str = "",
        client_secret: str = "",
        default_timezone: str | None = None,
    ):
        super().__init__(account_id, credentials, sync_state)
        #: Used when Todoist names no timezone of its own for a fixed instant.
        self.default_timezone = default_timezone

        # Two ways in, and the simple one is the default.
        #
        # A personal API token, copied from Todoist's own settings, is a bearer
        # token that never expires and needs no application registered anywhere.
        # For a self-hosted hub with one owner that is the whole of what OAuth
        # would have achieved, minus the redirect URI, the client secret and the
        # hourly refresh. OAuth remains available for anyone who would rather
        # not paste a token with full account access.
        self.api_token = (credentials.get("api_token") or "").strip()
        if self.api_token:
            self.auth = None
        else:
            if not client_id or not client_secret:
                raise ConnectorError(
                    "Todoist is not connected yet. Paste an API token on the "
                    "Todoist service page, or set up OAuth there."
                )
            self.auth = TodoistAuth(client_id, client_secret, credentials)

        self._client = httpx.Client(timeout=30)

    def close(self) -> None:
        self._client.close()

    @property
    def credentials_changed(self) -> bool:
        # A pasted token is never rewritten, so there is nothing to save back.
        return bool(self.auth and self.auth.dirty)

    def current_credentials(self) -> dict[str, Any]:
        if self.auth is None:
            return dict(self.credentials)
        return self.auth.as_credentials()

    # -- Capabilities ---------------------------------------------------------

    def capabilities(self, kind: CollectionKind) -> Capabilities:
        """Todoist is tasks only, and says so honestly.

        Note what is absent as much as what is present. Todoist has no location
        field, and its recurrence is a natural-language string rather than an
        RRULE, so neither is claimed -- a value Task Hub holds for either is
        left untouched by anything Todoist reports.
        """
        if kind != CollectionKind.TASKS:
            return Capabilities(fields=frozenset(), can_create=False, can_delete=False)
        return Capabilities(
            fields=frozenset(
                {F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE, F_DUE_TIME, F_PRIORITY,
                 F_TAGS, F_START}
            ),
            can_delete=True,
            can_create=True,
            # Proven on a live account: parent_id nests at least four levels.
            # Note that Todoist completes every descendant along with a parent,
            # which is why the engine never propagates completion downwards.
            supports_parent=True,
            max_title_length=CONTENT_LIMIT,
            max_notes_length=DESCRIPTION_LIMIT,
            stores_uid=False,
        )

    def echo_of(self, record: CanonicalRecord, kind: CollectionKind) -> CanonicalRecord:
        """Todoist forgets the zone and coarsens the priority. Both are certain.

        A due time is stored as a floating clock face -- the API returns
        ``"timezone": null`` alongside the value it was given -- so the zone
        label never survives. And four priority levels cannot carry iCalendar's
        nine, so the value comes back as whichever of the four it landed in.

        Measured against a real account: both differences appeared on the very
        first read-back of every timed, prioritised task, and each one was
        costing a redundant write to every other service in the group.
        """
        from copy import deepcopy

        echoed = deepcopy(record)
        echoed.due_tz = None
        echoed.start_tz = None
        echoed.end_tz = None
        echoed.priority = todoist_priority_to_canonical(
            canonical_priority_to_todoist(record.priority)
        )
        return echoed

    def supports_kind(self, kind: CollectionKind) -> bool:
        return kind == CollectionKind.TASKS

    # -- HTTP -----------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        headers = kwargs.pop("headers", {})
        token = self.api_token or self.auth.token()  # type: ignore[union-attr]
        headers["Authorization"] = f"Bearer {token}"

        try:
            response = self._client.request(
                method, f"{API_BASE}{path}", headers=headers, **kwargs
            )
        except httpx.RequestError as exc:
            raise ConnectorError(f"Could not reach Todoist: {exc}") from exc

        if response.status_code == 401:
            if self.api_token:
                raise ConnectorAuthError(
                    "Todoist rejected this API token. It may have been "
                    "regenerated in Todoist's settings; paste the current one."
                )
            raise ConnectorAuthError(
                "Todoist refused this login. Try reconnecting the account."
            )
        if response.status_code == 403:
            raise ConnectorAuthError(
                "Todoist refused access. The app may be missing the "
                f"{SCOPES} scopes; reconnect the account to grant them."
            )
        if response.status_code == 429:
            raise RateLimitError(
                "Todoist is rate limiting this account.",
                retry_after=_retry_after(response),
            )
        if response.status_code == 404:
            raise ConnectorGoneError("That project or task no longer exists in Todoist.")
        if response.status_code >= 500:
            raise RateLimitError(
                f"Todoist returned a server error ({response.status_code}).",
                retry_after=_retry_after(response) or 30,
            )
        if response.status_code >= 400:
            raise ConnectorError(_error_message(response))

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _paged(self, path: str, params: dict | None = None) -> list[dict]:
        """Follow ``next_cursor`` until the last page.

        v1 wraps every list in ``{"results": [...], "next_cursor": ...}``. A bare
        list is still accepted here so a single endpoint that has not been
        converted cannot take a whole sync down.
        """
        collected: list[dict] = []
        cursor: str | None = None
        for _ in range(100):  # A hard stop far beyond any real account.
            query = dict(params or {})
            query["limit"] = _PAGE_LIMIT
            if cursor:
                query["cursor"] = cursor
            payload = self._request("GET", path, params=query)

            if isinstance(payload, list):
                collected.extend(payload)
                break
            if not isinstance(payload, dict):
                break

            collected.extend(payload.get("results") or [])
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return collected

    # -- Discovery ------------------------------------------------------------

    def verify(self) -> str:
        payload = self._request("GET", "/user") or {}
        return payload.get("email") or payload.get("full_name") or "Todoist account"

    def list_remote_lists(self) -> list[RemoteList]:
        lists: list[RemoteList] = []
        for project in self._paged("/projects"):
            lists.append(
                RemoteList(
                    remote_id=str(project.get("id")),
                    name=project.get("name") or "Untitled project",
                    kind=CollectionKind.TASKS,
                    colour=None,
                    is_default=bool(project.get("is_inbox_project")),
                )
            )
        return lists

    # -- Reading --------------------------------------------------------------

    def pull(
        self,
        remote_list_id: str,
        kind: CollectionKind,
        since: dt.datetime | None = None,
        state: dict | None = None,
    ) -> PullResult:
        if kind != CollectionKind.TASKS:
            return PullResult(items=[], incremental=False)

        caps = self.capabilities(kind)
        items: list[RemoteItem] = []
        errors: list[str] = []

        for entry in self._paged("/tasks", {"project_id": remote_list_id}):
            items.append(
                RemoteItem(
                    remote_id=str(entry.get("id")),
                    record=self._task_to_record(entry),
                    fields_present=fields_for(entry, caps),
                    remote_updated_at=_parse_timestamp(entry.get("updated_at")),
                )
            )

        # /tasks returns only what is still open, so a completed task is simply
        # absent -- exactly as an intentionally deleted one is. Getting that
        # distinction wrong destroys data, so it is never guessed at.
        seen = {item.remote_id for item in items}
        try:
            until = dt.datetime.now(dt.timezone.utc)
            since_window = until - dt.timedelta(days=COMPLETED_WINDOW_DAYS)
            for entry in self._paged(
                "/tasks/completed/by_completion_date",
                {
                    "project_id": remote_list_id,
                    "since": since_window.strftime("%Y-%m-%dT%H:%M:%S"),
                    "until": until.strftime("%Y-%m-%dT%H:%M:%S"),
                },
            ):
                remote_id = str(entry.get("id"))
                if remote_id in seen:
                    continue
                seen.add(remote_id)
                items.append(
                    RemoteItem(
                        remote_id=remote_id,
                        record=self._task_to_record(entry, completed=True),
                        fields_present=fields_for(entry, caps),
                        remote_updated_at=_parse_timestamp(
                            entry.get("completed_at") or entry.get("updated_at")
                        ),
                    )
                )
        except ConnectorError as exc:
            errors.append(f"Could not read completed tasks: {exc}")

        # Anything still missing gets asked about by name. The completed-tasks
        # listing is windowed and has filters of its own, so a task can be
        # genuinely finished and still not appear in it -- and treating that as a
        # deletion removes the task from every other service. Todoist answers for
        # a completed task by id, and 404s for one that is really gone.
        previously = list((state or {}).get("seen") or [])
        missing = [task_id for task_id in previously if task_id not in seen]

        if len(missing) > MAX_PROBES:
            errors.append(
                f"{len(missing)} tasks disappeared from this project at once. "
                "That looks like a Todoist fault rather than real changes, so "
                "they have been left alone."
            )
            missing = []

        for task_id in missing:
            try:
                entry = self._request("GET", f"/tasks/{task_id}")
            except ConnectorGoneError:
                items.append(
                    RemoteItem(
                        remote_id=task_id,
                        record=CanonicalRecord(uid="", kind=CollectionKind.TASKS),
                        fields_present=caps.present_fields(),
                        deleted=True,
                    )
                )
                continue
            except ConnectorError as exc:
                errors.append(str(exc))
                continue
            if not entry:
                continue
            items.append(
                RemoteItem(
                    remote_id=task_id,
                    record=self._task_to_record(entry),
                    fields_present=fields_for(entry, caps),
                    remote_updated_at=_parse_timestamp(entry.get("updated_at")),
                )
            )
            seen.add(task_id)

        return PullResult(
            items=items,
            # Never a complete listing. A task absent from /tasks may be
            # completed rather than deleted, and letting the engine infer
            # deletion from absence deletes finished work everywhere.
            incremental=True,
            sync_state={"seen": sorted(seen)},
            errors=errors,
        )

    def _task_to_record(self, entry: dict, completed: bool = False) -> CanonicalRecord:
        # parent_id is Todoist's own id; the engine maps it back to a UID.
        due_date, due_time, due_tz = _parse_due(
            entry.get("due"), self.default_timezone
        )

        # Todoist has one scheduling date plus a separate, date-only deadline.
        # A task that spans time is written with the START in the date field and
        # the real deadline in the deadline field, so it appears in Today when
        # work should begin while still carrying its actual deadline. Reading it
        # back has to undo exactly that, or the next sync takes the start for a
        # deadline and destroys the real one.
        deadline_raw = (entry.get("deadline") or {}).get("date")
        start_date = start_time = start_tz = None
        if deadline_raw:
            start_date, start_time, start_tz = due_date, due_time, due_tz
            try:
                due_date = dt.date.fromisoformat(deadline_raw[:10])
            except ValueError:
                due_date = start_date
            # The deadline carries no time of day. Saying nothing about the due
            # time is what stops Todoist blanking a time held elsewhere; the
            # pull narrows its reported fields to match.
            due_time = None
            due_tz = None
        is_done = (
            completed
            or bool(entry.get("is_completed"))
            or bool(entry.get("checked"))
        )
        return CanonicalRecord(
            uid="",  # Todoist cannot store our UID; links are kept locally.
            kind=CollectionKind.TASKS,
            title=entry.get("content") or "",
            notes=entry.get("description") or None,
            status=ItemStatus.COMPLETED if is_done else ItemStatus.NEEDS_ACTION,
            completed_at=_parse_timestamp(entry.get("completed_at")),
            due_date=due_date,
            due_time=due_time,
            due_tz=due_tz,
            start_date=start_date,
            start_time=start_time,
            start_tz=start_tz,
            priority=todoist_priority_to_canonical(entry.get("priority")),
            tags=list(entry.get("labels") or []),
            origin_service=ServiceKind.TODOIST,
            # Todoist names the parent by its own id; the engine maps it back.
            parent_remote_id=str(entry["parent_id"]) if entry.get("parent_id") else None,
            created_at=_parse_timestamp(entry.get("added_at")),
            updated_at=_parse_timestamp(entry.get("updated_at")),
        )

    # -- Writing --------------------------------------------------------------

    def create(
        self, remote_list_id: str, record: CanonicalRecord, kind: CollectionKind
    ) -> PushOutcome:
        if kind != CollectionKind.TASKS:
            return PushOutcome(remote_id=None, error="Todoist stores tasks only.")

        body = self._record_to_body(record)
        body["project_id"] = remote_list_id
        if record.parent_remote_id:
            body["parent_id"] = record.parent_remote_id
        created = self._request("POST", "/tasks", json=body) or {}
        remote_id = str(created.get("id")) if created.get("id") else None
        if not remote_id:
            return PushOutcome(remote_id=None, error="Todoist did not return a task id.")

        # Creating and then completing is two calls: Todoist has no way to add a
        # task that is already done.
        if record.status == ItemStatus.COMPLETED:
            self._request("POST", f"/tasks/{remote_id}/close")

        return PushOutcome(
            remote_id=remote_id,
            remote_updated_at=_parse_timestamp(created.get("updated_at")),
        )

    def update(
        self,
        remote_list_id: str,
        remote_id: str,
        record: CanonicalRecord,
        kind: CollectionKind,
    ) -> PushOutcome:
        if kind != CollectionKind.TASKS:
            return PushOutcome(remote_id=remote_id, error="Todoist stores tasks only.")

        # Completion is its own endpoint; sending status in the body does
        # nothing. Do the content update first so a task that is about to be
        # closed still ends up carrying the right text.
        updated = self._request("POST", f"/tasks/{remote_id}", json=self._record_to_body(record))

        # Re-parenting is its own endpoint. Sending parent_id on the ordinary
        # update is accepted and ignored, so a task moved under a different
        # parent would appear to save and quietly stay where it was.
        self._move_under(remote_id, record.parent_remote_id)

        current_done = bool((updated or {}).get("is_completed") or (updated or {}).get("checked"))
        wants_done = record.status == ItemStatus.COMPLETED
        if wants_done and not current_done:
            self._request("POST", f"/tasks/{remote_id}/close")
        elif current_done and not wants_done:
            self._request("POST", f"/tasks/{remote_id}/reopen")

        return PushOutcome(
            remote_id=remote_id,
            remote_updated_at=_parse_timestamp((updated or {}).get("updated_at")),
        )

    def _move_under(self, remote_id: str, parent_id: str | None) -> None:
        """Move a task under a parent, or back to the top of its project.

        Best effort: losing the nesting is worth reporting quietly and retrying
        next pass, not failing an update whose text and dates all landed.
        """
        try:
            current = self._request("GET", f"/tasks/{remote_id}") or {}
            if str(current.get("parent_id") or "") == str(parent_id or ""):
                return
            if parent_id:
                self._request("POST", f"/tasks/{remote_id}/move",
                              json={"parent_id": str(parent_id)})
            else:
                self._request("POST", f"/tasks/{remote_id}/move",
                              json={"project_id": str(current.get("project_id") or "")})
        except ConnectorError as exc:
            logger.info("Could not re-parent a Todoist task: %s", exc)

    def delete(
        self, remote_list_id: str, remote_id: str, kind: CollectionKind
    ) -> PushOutcome:
        try:
            self._request("DELETE", f"/tasks/{remote_id}")
        except ConnectorGoneError:
            # Already gone, which is what a delete was asking for.
            return PushOutcome(remote_id=remote_id)
        return PushOutcome(remote_id=remote_id)

    def _record_to_body(self, record: CanonicalRecord) -> dict[str, Any]:
        body: dict[str, Any] = {
            "content": (record.title or "")[:CONTENT_LIMIT],
            "description": (record.notes or "")[:DESCRIPTION_LIMIT],
            "priority": canonical_priority_to_todoist(record.priority),
            "labels": list(record.tags or []),
        }

        # A task that genuinely spans time -- a start that differs from its due
        # -- puts the START in Todoist's scheduling date so it surfaces in Today
        # when work should begin, and the real deadline in the deadline field.
        # Todoist's deadline is date-only, which is why the due time is reported
        # as absent on the way back rather than as empty.
        # Only a task running across DIFFERENT DAYS is a span. A start and due
        # on the same day gains nothing from the deadline field -- Todoist's
        # deadline is date-only, so both halves would read as the same date --
        # and treating it as one moves the task's visible date to its start,
        # which looks exactly like an edit that failed to apply.
        spans = (
            record.start_date is not None
            and record.due_date is not None
            and record.start_date != record.due_date
        )
        if spans:
            scheduled_date, scheduled_time = record.start_date, record.start_time
            body["deadline_date"] = record.due_date.isoformat()
        else:
            scheduled_date, scheduled_time = record.due_date, record.due_time
            # Explicitly cleared: a task that stops spanning must not keep a
            # deadline from when it did.
            body["deadline_date"] = None

        # Todoist takes either a date or a datetime, never both, and sending
        # due_datetime for an all-day task would invent a midnight that no other
        # service asked for.
        if scheduled_date and scheduled_time:
            moment = dt.datetime.combine(scheduled_date, scheduled_time)
            body["due_datetime"] = moment.strftime("%Y-%m-%dT%H:%M:%S")
        elif scheduled_date:
            body["due_date"] = scheduled_date.isoformat()
        else:
            body["due_string"] = "no date"

        return body


def fields_for(entry: dict, caps) -> frozenset[str]:
    """Which fields this particular Todoist task can actually speak for.

    Capabilities describe the service; this narrows them per task, which the
    connector protocol explicitly allows. Two cases matter:

    * A task with a deadline is a span. Its deadline is date-only, so it has
      nothing to say about the due *time* -- reporting one would blank a time
      set in TickTick or Radicale.
    * A task without a deadline has no start at all. Claiming the field would
      let Todoist's silence erase a start held elsewhere.
    """
    fields = set(caps.present_fields())
    if (entry.get("deadline") or {}).get("date"):
        fields.discard(F_DUE_TIME)
    else:
        fields.discard(F_START)
    return frozenset(fields)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Todoist returned {response.status_code}: {response.text[:200]}"
    if isinstance(payload, dict):
        detail = (
            payload.get("error")
            or payload.get("error_message")
            or payload.get("detail")
        )
        if detail:
            return f"Todoist returned {response.status_code}: {detail}"
    return f"Todoist returned {response.status_code}."
