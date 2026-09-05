"""Google Tasks and Google Calendar.

One account covers both services, so this connector handles either kind of
collection depending on which it is asked for.

Two Google-specific traps are handled here, and both would silently corrupt data
if they were not:

**Google Tasks discards the time of day.** The API accepts an RFC 3339 timestamp
for ``due`` and stores only the date. This connector therefore declares no
``due_time`` capability at all, which is what stops the merge engine from ever
letting Google clear a time set elsewhere.

**Google Tasks reports due dates as UTC midnight.** A task due "5 March" comes
back as ``2026-03-05T00:00:00.000Z``. Converting that into a user's local
timezone -- the obvious thing to do with a timestamp -- would render it as 4
March at 19:00 in New York, moving every task a day earlier for anyone west of
UTC. The date is therefore read as a *floating* calendar date: the date portion
of the string is taken literally and the timezone ignored.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import httpx

from app.connectors.base import (
    F_DUE_DATE,
    F_END,
    F_LOCATION,
    F_NOTES,
    F_RRULE,
    F_START,
    F_STATUS,
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
from app.services.timezones import wall_time

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
TASKS_API = "https://tasks.googleapis.com/tasks/v1"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"

#: Requested at authorisation. Tasks and Calendar are both "sensitive" scopes,
#: so an unverified app shows a warning screen the user clicks through; that is
#: expected and harmless for a personal installation.
SCOPES = [
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

#: Google Tasks caps titles at 1024 characters and notes at 8192.
TASK_TITLE_LIMIT = 1024
TASK_NOTES_LIMIT = 8192


# --- OAuth --------------------------------------------------------------------


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the URL that sends the user to Google to grant access."""
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline is what yields a refresh token; without it access lasts an
        # hour and syncing stops as soon as the browser tab is closed.
        "access_type": "offline",
        # Google only returns a refresh token on the *first* authorisation for a
        # given client/user pair. Forcing the consent screen guarantees one on
        # every reconnect, so re-authorising a broken account actually fixes it.
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    """Swap an authorisation code for access and refresh tokens."""
    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise ConnectorAuthError(_explain_token_error(response))

    payload = response.json()
    if not payload.get("refresh_token"):
        raise ConnectorAuthError(
            "Google did not return a refresh token. This usually means the "
            "account has authorised this app before. Remove Task Hub at "
            "myaccount.google.com/permissions and connect again."
        )
    return payload


def _explain_token_error(response: httpx.Response) -> str:
    """Turn Google's terse token errors into something actionable."""
    try:
        body = response.json()
    except ValueError:
        return f"Google rejected the request (HTTP {response.status_code})."

    code = body.get("error", "")
    detail = body.get("error_description", "")

    if code == "invalid_client":
        return (
            "Google rejected the Client ID or Client Secret. Check they were "
            "pasted completely, with no leading or trailing spaces."
        )
    if code == "redirect_uri_mismatch":
        return (
            "The redirect address does not match the one registered in the "
            "Google Cloud Console. Copy the exact address shown on this page "
            "into your OAuth client's Authorised redirect URIs and try again."
        )
    if code == "invalid_grant":
        return (
            "Google refused the login. The most common cause is an OAuth "
            "consent screen still set to Testing, which expires access after 7 "
            "days. Set the app to Production and reconnect."
        )
    return f"Google returned {code or response.status_code}: {detail}".strip()


class GoogleAuth:
    """Holds an account's tokens and refreshes the access token as needed."""

    def __init__(self, client_id: str, client_secret: str, credentials: dict):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = credentials.get("refresh_token")
        self.access_token = credentials.get("access_token")
        expiry = credentials.get("expires_at")
        self.expires_at = float(expiry) if expiry else 0.0
        self.dirty = False

    def token(self) -> str:
        """A valid access token, refreshing it when it is close to expiring."""
        if not self.refresh_token:
            raise ConnectorAuthError(
                "This Google account has no saved login. Click Reconnect."
            )
        # Refresh a minute early: a token that expires mid-request would
        # otherwise fail a write that has no safe automatic retry.
        if self.access_token and time.time() < self.expires_at - 60:
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
            raise ConnectorAuthError(_explain_token_error(response))
        payload = response.json()
        self.access_token = payload["access_token"]
        self.expires_at = time.time() + int(payload.get("expires_in", 3600))
        # Google occasionally rotates the refresh token.
        if payload.get("refresh_token"):
            self.refresh_token = payload["refresh_token"]
        self.dirty = True

    def as_credentials(self) -> dict[str, Any]:
        return {
            "refresh_token": self.refresh_token,
            "access_token": self.access_token,
            "expires_at": self.expires_at,
        }


# --- Date handling ------------------------------------------------------------


def parse_google_task_due(raw: str | None) -> dt.date | None:
    """Read a Google Tasks due value as a floating calendar date.

    Google returns UTC midnight, e.g. ``2026-03-05T00:00:00.000Z``. Only the
    date portion is meaningful -- see the module docstring for why converting
    the timestamp would move dates by a day for anyone west of UTC.
    """
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def format_google_task_due(value: dt.date | None) -> str | None:
    """Write a date as the UTC-midnight timestamp Google Tasks expects."""
    if value is None:
        return None
    return f"{value.isoformat()}T00:00:00.000Z"


def _parse_rfc3339(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            dt.timezone.utc
        )
    except ValueError:
        return None


# --- Connector ----------------------------------------------------------------


class GoogleConnector(Connector):
    service = ServiceKind.GOOGLE
    name = "Google"

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
        if not client_id or not client_secret:
            raise ConnectorError(
                "Google is not configured yet. Add your Client ID and Client "
                "Secret on the Google service page first."
            )
        self.auth = GoogleAuth(client_id, client_secret, credentials)
        # The zone to assume when Google names none. It always does name one on
        # a timed event, so this is a fallback rather than the normal path, but
        # falling back to UTC would relabel the instant rather than lose it.
        self.default_timezone = default_timezone
        self._client = httpx.Client(timeout=30)

    def close(self) -> None:
        self._client.close()

    @property
    def credentials_changed(self) -> bool:
        """Whether tokens were refreshed and need saving back to the database."""
        return self.auth.dirty

    def current_credentials(self) -> dict[str, Any]:
        return self.auth.as_credentials()

    # -- Capabilities ---------------------------------------------------------

    def capabilities(self, kind: CollectionKind) -> Capabilities:
        if kind == CollectionKind.TASKS:
            return Capabilities(
                # Note the absence of F_DUE_TIME. That single omission is what
                # protects a time of day set in any other service.
                fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE}),
                can_delete=True,
                can_create=True,
                # Proven on a live account: Google accepted a grandchild, so
                # the API is not limited to one level even though its own apps
                # appear to be. Nesting is stored either way.
                supports_parent=True,
                max_title_length=TASK_TITLE_LIMIT,
                max_notes_length=TASK_NOTES_LIMIT,
                stores_uid=False,
            )
        return Capabilities(
            fields=frozenset(
                {F_TITLE, F_NOTES, F_STATUS, F_START, F_END, F_LOCATION, F_RRULE}
            ),
            can_delete=True,
            can_create=True,
            stores_uid=True,
        )

    def supports_kind(self, kind: CollectionKind) -> bool:
        return True

    # -- HTTP -----------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> Any:
        """Make an authenticated call, translating Google's errors into ours."""
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.auth.token()}"

        try:
            response = self._client.request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise ConnectorError(f"Could not reach Google: {exc}") from exc

        if response.status_code in (401, 403):
            detail = _google_error_message(response)
            # 403 covers both "your token is no good" and "you are going too
            # fast", and they need opposite responses: one needs the user, the
            # other needs patience.
            if "rateLimitExceeded" in detail or "userRateLimitExceeded" in detail:
                raise RateLimitError(detail, retry_after=_retry_after(response))
            if response.status_code == 403 and "insufficient" not in detail.lower():
                raise ConnectorError(detail)
            raise ConnectorAuthError(
                f"Google refused access: {detail}. Try reconnecting this account."
            )
        if response.status_code == 429:
            raise RateLimitError(
                "Google is rate limiting this account.",
                retry_after=_retry_after(response),
            )
        if response.status_code == 404:
            raise ConnectorGoneError("That list or item no longer exists in Google.")
        if response.status_code >= 500:
            raise RateLimitError(
                f"Google returned a server error ({response.status_code}).",
                retry_after=_retry_after(response) or 30,
            )
        if response.status_code >= 400:
            raise ConnectorError(_google_error_message(response))

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _paged(self, url: str, params: dict, key: str = "items") -> list[dict]:
        """Follow Google's nextPageToken until every page is collected."""
        results: list[dict] = []
        page_params = dict(params)
        for _ in range(100):  # A hard stop; 100 pages is far beyond normal use.
            payload = self._request("GET", url, params=page_params) or {}
            results.extend(payload.get(key) or [])
            token = payload.get("nextPageToken")
            if not token:
                break
            page_params["pageToken"] = token
        return results

    # -- Discovery ------------------------------------------------------------

    def verify(self) -> str:
        payload = self._request("GET", USERINFO_ENDPOINT) or {}
        return payload.get("email") or payload.get("sub") or "Google account"

    def list_remote_lists(self) -> list[RemoteList]:
        lists: list[RemoteList] = []

        for entry in self._paged(f"{TASKS_API}/users/@me/lists", {"maxResults": 100}):
            lists.append(
                RemoteList(
                    remote_id=entry["id"],
                    name=entry.get("title") or "Untitled list",
                    kind=CollectionKind.TASKS,
                )
            )

        for entry in self._paged(
            f"{CALENDAR_API}/users/me/calendarList", {"maxResults": 250}
        ):
            access = entry.get("accessRole", "reader")
            lists.append(
                RemoteList(
                    remote_id=entry["id"],
                    name=entry.get("summary") or entry["id"],
                    kind=CollectionKind.CALENDAR,
                    colour=entry.get("backgroundColor"),
                    is_default=bool(entry.get("primary")),
                    # Google reports the access level, so a subscribed holiday
                    # calendar can be offered as read-only rather than letting
                    # the user enable a write that would always fail.
                    read_only=access in ("reader", "freeBusyReader"),
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
        if kind == CollectionKind.TASKS:
            return self._pull_tasks(remote_list_id, since)
        return self._pull_events(remote_list_id, since)

    def _pull_tasks(self, list_id: str, since: dt.datetime | None) -> PullResult:
        params: dict[str, Any] = {
            "maxResults": 100,
            # Without both of these, completed tasks vanish from the response
            # and Task Hub would read that as "deleted", resurrecting every
            # task the user had ticked off in Google.
            "showCompleted": "true",
            "showHidden": "true",
        }
        incremental = False
        if since is not None:
            params["updatedMin"] = since.astimezone(dt.timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            # Only worth asking for deleted items on an incremental pull, where
            # absence from the response means "unchanged" rather than "gone".
            # On a full listing, absence already implies deletion, and asking
            # for Google's tombstones just returns years of deleted history --
            # one real list came back with 168 tombstones and no live tasks.
            params["showDeleted"] = "true"
            incremental = True

        entries = self._paged(f"{TASKS_API}/lists/{list_id}/tasks", params)
        fields = self.capabilities(CollectionKind.TASKS).present_fields()

        items = []
        for entry in entries:
            items.append(
                RemoteItem(
                    remote_id=entry["id"],
                    record=self._task_to_record(entry),
                    fields_present=fields,
                    remote_updated_at=_parse_rfc3339(entry.get("updated")),
                    deleted=bool(entry.get("deleted")),
                )
            )
        return PullResult(items=items, incremental=incremental)

    def _task_to_record(self, entry: dict) -> CanonicalRecord:
        # Google names the parent by *its* id. The engine turns that back into
        # a canonical UID; carrying it here as parent_remote_id keeps the two
        # kinds of identifier from being confused for one another.
        completed = entry.get("status") == "completed"
        record = CanonicalRecord(
            uid=new_uid(),  # Replaced by the engine when a link already exists.
            kind=CollectionKind.TASKS,
            title=entry.get("title") or "",
            notes=entry.get("notes") or None,
            status=ItemStatus.COMPLETED if completed else ItemStatus.NEEDS_ACTION,
            completed_at=_parse_rfc3339(entry.get("completed")),
            due_date=parse_google_task_due(entry.get("due")),
            # Deliberately left unset: Google cannot store a time, so reporting
            # one -- even as None -- would let it overwrite the real value.
            due_time=None,
            due_tz=None,
            origin_service=ServiceKind.GOOGLE,
            updated_at=_parse_rfc3339(entry.get("updated")),
        )
        record.parent_remote_id = entry.get("parent") or None
        return record

    def _pull_events(self, calendar_id: str, since: dt.datetime | None) -> PullResult:
        params: dict[str, Any] = {
            "maxResults": 250,
            "showDeleted": "true",
            # Recurring events are kept as their master definition rather than
            # expanded into thousands of instances, which is both far cheaper
            # and what iCalendar stores natively.
            "singleEvents": "false",
        }
        incremental = False
        if since is not None:
            params["updatedMin"] = since.astimezone(dt.timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            incremental = True

        entries = self._paged(f"{CALENDAR_API}/calendars/{calendar_id}/events", params)
        fields = self.capabilities(CollectionKind.CALENDAR).present_fields()

        items = []
        for entry in entries:
            # A recurring event comes back as a master plus one entry per
            # modified occurrence. Those overrides carry a different id but the
            # SAME iCalUID as their master, so ingesting them would map several
            # remote items onto one canonical item -- and Task Hub's model holds
            # a single RRULE per item, with no way to represent "this Tuesday
            # only, moved an hour later". Only the master is taken; per-instance
            # edits made in Google stay in Google.
            if entry.get("recurringEventId"):
                continue
            items.append(
                RemoteItem(
                    remote_id=entry["id"],
                    record=self._event_to_record(entry),
                    fields_present=fields,
                    remote_updated_at=_parse_rfc3339(entry.get("updated")),
                    deleted=entry.get("status") == "cancelled",
                )
            )
        return PullResult(items=items, incremental=incremental)

    def _split_endpoint(
        self, value: dict | None
    ) -> tuple[dt.date | None, dt.time | None, str | None]:
        """Decompose a Google start/end object into date, time and timezone.

        The conversion is the whole job. Google stores the instant and names the
        zone separately, so a ten o'clock event in London during British Summer
        Time comes back as ``2026-09-09T09:00:00Z`` with ``timeZone:
        Europe/London``. Reading the clock face off that string and pairing it
        with the zone label gives nine o'clock -- an event an hour earlier than
        the one the user created, which is then written back to every other
        service as though somebody had moved it.

        It is a summer-only fault, which is what let it survive: in winter
        London is UTC and the unconverted value is accidentally right.
        """
        if not value:
            return None, None, None
        if value.get("date"):
            # An all-day endpoint. No time, and no timezone to apply -- and
            # converting one would move the date for anyone west of UTC.
            try:
                return dt.date.fromisoformat(value["date"]), None, None
            except ValueError:
                return None, None, None
        raw = value.get("dateTime")
        if not raw:
            return None, None, None
        return wall_time(raw, value.get("timeZone"), self.default_timezone)

    def _event_to_record(self, entry: dict) -> CanonicalRecord:
        start_date, start_time, start_tz = self._split_endpoint(entry.get("start"))
        end_date, end_time, end_tz = self._split_endpoint(entry.get("end"))

        # Google returns the rule with its iCalendar property name attached --
        # "RRULE:FREQ=YEARLY" -- while the iCalendar layer, and therefore
        # everything else here, works in the bare "FREQ=YEARLY". Keeping the
        # prefix made the two disagree on every single pass: the merge engine
        # saw a changed value, resolved the conflict, wrote it back, and found
        # it changed again next time. A yearly birthday was re-resolved every
        # fifteen minutes for as long as the account was connected.
        recurrence = entry.get("recurrence") or []
        rrule = next((r for r in recurrence if r.upper().startswith("RRULE")), None)
        if rrule and rrule.upper().startswith("RRULE:"):
            rrule = rrule.split(":", 1)[1].strip()
        elif rrule is None:
            # Google always sends the property name, but accepting a bare rule
            # costs nothing. Matching on FREQ= rather than "anything left"
            # keeps EXDATE and RDATE lines from being mistaken for the rule.
            rrule = next(
                (r.strip() for r in recurrence if r.upper().startswith("FREQ=")), None
            )

        return CanonicalRecord(
            uid=entry.get("iCalUID") or new_uid(),
            kind=CollectionKind.CALENDAR,
            title=entry.get("summary") or "",
            notes=entry.get("description") or None,
            location=entry.get("location") or None,
            status=(
                ItemStatus.CANCELLED
                if entry.get("status") == "cancelled"
                else ItemStatus.NEEDS_ACTION
            ),
            start_date=start_date,
            start_time=start_time,
            start_tz=start_tz,
            end_date=end_date,
            end_time=end_time,
            end_tz=end_tz,
            all_day=start_time is None and start_date is not None,
            rrule=rrule,
            origin_service=ServiceKind.GOOGLE,
            updated_at=_parse_rfc3339(entry.get("updated")),
        )

    # -- Writing --------------------------------------------------------------

    def create(
        self, remote_list_id: str, record: CanonicalRecord, kind: CollectionKind
    ) -> PushOutcome:
        try:
            if kind == CollectionKind.TASKS:
                payload = self._record_to_task(record)
                # The parent is a query parameter here, not part of the body --
                # putting it in the JSON is accepted and silently ignored.
                params = ({"parent": record.parent_remote_id}
                          if record.parent_remote_id else None)
                created = self._request(
                    "POST", f"{TASKS_API}/lists/{remote_list_id}/tasks",
                    json=payload, params=params,
                )
            else:
                payload = self._record_to_event(record)
                created = self._request(
                    "POST",
                    f"{CALENDAR_API}/calendars/{remote_list_id}/events",
                    json=payload,
                )
        except ConnectorError as exc:
            return PushOutcome(remote_id=None, error=str(exc))

        created = created or {}
        return PushOutcome(
            remote_id=created.get("id"),
            etag=created.get("etag"),
            remote_updated_at=_parse_rfc3339(created.get("updated")),
        )

    def update(
        self,
        remote_list_id: str,
        remote_id: str,
        record: CanonicalRecord,
        kind: CollectionKind,
    ) -> PushOutcome:
        try:
            if kind == CollectionKind.TASKS:
                payload = self._record_to_task(record)
                updated = self._request(
                    "PATCH",
                    f"{TASKS_API}/lists/{remote_list_id}/tasks/{remote_id}",
                    json=payload,
                )
                # A task's parent cannot be changed by an ordinary update:
                # PATCH accepts the field and ignores it, so re-parenting has
                # to go through move, which is a separate call.
                self._move_under(remote_list_id, remote_id, record.parent_remote_id)
            else:
                payload = self._record_to_event(record)
                updated = self._request(
                    "PATCH",
                    f"{CALENDAR_API}/calendars/{remote_list_id}/events/{remote_id}",
                    json=payload,
                )
        except ConnectorError as exc:
            return PushOutcome(remote_id=remote_id, error=str(exc))

        updated = updated or {}
        return PushOutcome(
            remote_id=updated.get("id", remote_id),
            etag=updated.get("etag"),
            remote_updated_at=_parse_rfc3339(updated.get("updated")),
        )

    def _move_under(self, list_id: str, task_id: str, parent_id: str | None) -> None:
        """Re-parent a task, or move it back to the top level.

        Best effort by design. A failure here costs the nesting, which the next
        pass will try again; treating it as a failure of the whole update would
        report a task as unsynced when its title, notes and due date all landed
        perfectly well.
        """
        try:
            current = self._request(
                "GET", f"{TASKS_API}/lists/{list_id}/tasks/{task_id}"
            ) or {}
            if (current.get("parent") or None) == (parent_id or None):
                return  # Already where it should be; move is not free.
            self._request(
                "POST", f"{TASKS_API}/lists/{list_id}/tasks/{task_id}/move",
                params={"parent": parent_id} if parent_id else {},
            )
        except ConnectorError as exc:
            logger.info("Could not re-parent a Google task: %s", exc)

    def delete(
        self, remote_list_id: str, remote_id: str, kind: CollectionKind
    ) -> PushOutcome:
        try:
            if kind == CollectionKind.TASKS:
                self._request(
                    "DELETE", f"{TASKS_API}/lists/{remote_list_id}/tasks/{remote_id}"
                )
            else:
                self._request(
                    "DELETE",
                    f"{CALENDAR_API}/calendars/{remote_list_id}/events/{remote_id}",
                )
        except ConnectorGoneError:
            # Already gone, which is what a delete was asking for.
            return PushOutcome(remote_id=remote_id)
        except ConnectorError as exc:
            return PushOutcome(remote_id=remote_id, error=str(exc))
        return PushOutcome(remote_id=remote_id)

    def _record_to_task(self, record: CanonicalRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": (record.title or "")[:TASK_TITLE_LIMIT],
            "status": "completed" if record.status == ItemStatus.COMPLETED else "needsAction",
        }
        # Explicit nulls matter: omitting a key leaves the old value in place on
        # a PATCH, so clearing a note or a due date requires sending null.
        payload["notes"] = (record.notes or None) and record.notes[:TASK_NOTES_LIMIT]
        payload["due"] = format_google_task_due(record.due_date)

        if record.status == ItemStatus.COMPLETED:
            completed_at = record.completed_at or dt.datetime.now(dt.timezone.utc)
            payload["completed"] = completed_at.astimezone(dt.timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        else:
            payload["completed"] = None
        return payload

    def _record_to_event(self, record: CanonicalRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "summary": record.title or "",
            "description": record.notes or None,
            "location": record.location or None,
        }

        if record.start_date and record.start_time is None:
            payload["start"] = {"date": record.start_date.isoformat()}
            # Google's all-day end date is exclusive, matching iCalendar, so the
            # canonical value carries across unchanged.
            end = record.end_date or (record.start_date + dt.timedelta(days=1))
            payload["end"] = {"date": end.isoformat()}
        elif record.start_date:
            payload["start"] = _google_datetime(
                record.start_date, record.start_time, record.start_tz
            )
            end_date = record.end_date or record.start_date
            end_time = record.end_time or record.start_time
            payload["end"] = _google_datetime(end_date, end_time, record.end_tz or record.start_tz)

        if record.rrule:
            rule = record.rrule.strip()
            if not rule.upper().startswith("RRULE:"):
                rule = f"RRULE:{rule}"
            payload["recurrence"] = [rule]

        return payload


def _google_datetime(
    date_part: dt.date, time_part: dt.time | None, tz_name: str | None
) -> dict[str, str]:
    moment = dt.datetime.combine(date_part, time_part or dt.time(0, 0))
    entry = {"dateTime": moment.isoformat()}
    if tz_name:
        entry["timeZone"] = tz_name
    return entry


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _google_error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = body.get("error")
    if isinstance(error, dict):
        return error.get("message") or str(error)
    return str(error or f"HTTP {response.status_code}")
