"""Microsoft To Do and Outlook Calendar, through the Microsoft Graph API.

One account covers both, so this connector serves either kind of collection
depending on which it is asked for.

The trap here is the same shape as Google's. **Microsoft To Do stores a due
date, not a due time.** Its API accepts a full timestamp and its own apps show
only the date; a time sent in is quietly ignored. So the To Do capability set
omits ``due_time`` entirely, which is what stops it clearing a time set in
Todoist or on a CalDAV client. Outlook Calendar has no such limitation and is
declared fully.

Personal accounts (outlook.com, hotmail.com, live.com) and work or school
accounts both work. A work account may need an administrator to approve the
permissions, which the setup guide explains.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import httpx

from app.connectors.base import (
    folded_steps,
    F_DUE_DATE,
    F_END,
    F_LOCATION,
    F_NOTES,
    F_PRIORITY,
    F_RRULE,
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
from app.services.ical_model import CanonicalRecord, new_uid

logger = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
AUTH_ENDPOINT = f"{AUTHORITY}/authorize"
TOKEN_ENDPOINT = f"{AUTHORITY}/token"
GRAPH = "https://graph.microsoft.com/v1.0"

#: offline_access is what yields a refresh token; without it access lasts an
#: hour and syncing stops as soon as the browser tab closes.
SCOPES = [
    "offline_access",
    "User.Read",
    "Tasks.ReadWrite",
    "Calendars.ReadWrite",
]

#: Microsoft To Do importance is a three-way word, not a number.
_IMPORTANCE_TO_CANONICAL = {"high": 1, "normal": 5, "low": 9}
_CANONICAL_TO_IMPORTANCE = {0: "normal", 1: "high", 2: "high", 3: "high",
                            4: "normal", 5: "normal", 6: "normal",
                            7: "low", 8: "low", 9: "low"}


# --- OAuth --------------------------------------------------------------------


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode

    return f"{AUTH_ENDPOINT}?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state,
        # Always ask, so reconnecting a broken account genuinely re-consents
        # and returns a fresh refresh token rather than silently reusing one.
        "prompt": "consent",
    })


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    response = httpx.post(TOKEN_ENDPOINT, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
    }, timeout=30)
    if response.status_code != 200:
        raise ConnectorAuthError(_explain_token_error(response))
    payload = response.json()
    if not payload.get("refresh_token"):
        raise ConnectorAuthError(
            "Microsoft did not return a long-term token. Check that "
            "'offline_access' is among the permissions on your app "
            "registration, then connect again."
        )
    return payload


def _explain_token_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"Microsoft rejected the request (HTTP {response.status_code})."
    code = body.get("error", "")
    detail = body.get("error_description", "") or ""
    if code == "invalid_client":
        return ("Microsoft rejected the Application ID or the client secret. "
                "Check both were copied in full — and note that the secret is "
                "the *Value*, not the Secret ID.")
    if code == "redirect_uri_mismatch" or "AADSTS50011" in detail:
        return ("The redirect address does not match the one registered in "
                "Azure. Copy the exact address shown on this page into your "
                "app registration's Redirect URIs.")
    if "AADSTS7000215" in detail:
        return ("Microsoft rejected the client secret. In Azure you must copy "
                "the secret's *Value* column, which is only shown once — the "
                "Secret ID will not work.")
    if "AADSTS65001" in detail:
        return ("Nobody has consented to these permissions. If this is a work "
                "or school account, an administrator has to approve them.")
    return f"Microsoft returned {code or response.status_code}: {detail[:200]}".strip()


class MicrosoftAuth:
    """Holds an account's tokens and refreshes the access token as needed."""

    def __init__(self, client_id: str, client_secret: str, credentials: dict):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = credentials.get("refresh_token")
        self.access_token = credentials.get("access_token")
        self.expires_at = float(credentials.get("expires_at") or 0)
        self.dirty = False

    def token(self) -> str:
        if not self.refresh_token:
            raise ConnectorAuthError(
                "This Microsoft account has no saved sign-in. Click Reconnect."
            )
        # Refresh a minute early: a token expiring mid-request would fail a
        # write that has no safe automatic retry.
        if self.access_token and time.time() < self.expires_at - 60:
            return self.access_token
        self._refresh()
        return self.access_token  # type: ignore[return-value]

    def _refresh(self) -> None:
        response = httpx.post(TOKEN_ENDPOINT, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(SCOPES),
        }, timeout=30)
        if response.status_code != 200:
            raise ConnectorAuthError(_explain_token_error(response))
        payload = response.json()
        self.access_token = payload["access_token"]
        self.expires_at = time.time() + int(payload.get("expires_in", 3600))
        # Microsoft rotates refresh tokens; losing the new one logs you out.
        if payload.get("refresh_token"):
            self.refresh_token = payload["refresh_token"]
        self.dirty = True

    def as_credentials(self) -> dict:
        return {"refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "expires_at": self.expires_at}


# --- Date helpers -------------------------------------------------------------


def _parse_graph_datetime(value: dict | None) -> tuple[dt.date | None, dt.time | None, str | None]:
    """Split a Graph DateTimeTimeZone into date, time and zone."""
    if not value or not value.get("dateTime"):
        return None, None, None
    raw = value["dateTime"]
    zone = value.get("timeZone") or None
    if zone in ("UTC", "Etc/GMT"):
        zone = "UTC"
    try:
        # Graph sends fractional seconds of varying length and no offset.
        cleaned = raw.replace("Z", "")
        if "." in cleaned:
            head, _, frac = cleaned.partition(".")
            cleaned = f"{head}.{frac[:6]}"
        moment = dt.datetime.fromisoformat(cleaned)
    except ValueError:
        try:
            return dt.date.fromisoformat(raw[:10]), None, None
        except ValueError:
            return None, None, None
    return moment.date(), moment.time().replace(microsecond=0), zone


def _graph_datetime(date_part: dt.date, time_part: dt.time | None, zone: str | None) -> dict:
    moment = dt.datetime.combine(date_part, time_part or dt.time(0, 0))
    return {"dateTime": moment.isoformat(timespec="seconds"),
            "timeZone": zone or "UTC"}


def _parse_timestamp(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


# --- Connector ----------------------------------------------------------------


class MicrosoftConnector(Connector):
    service = ServiceKind.MICROSOFT
    name = "Microsoft"

    def __init__(self, account_id: int, credentials: dict, sync_state: dict | None = None,
                 client_id: str = "", client_secret: str = "",
                 default_timezone: str | None = None):
        super().__init__(account_id, credentials, sync_state)
        if not client_id or not client_secret:
            raise ConnectorError(
                "Microsoft is not configured yet. Add your Application ID and "
                "client secret on the Microsoft service page first."
            )
        self.auth = MicrosoftAuth(client_id, client_secret, credentials)
        self.default_timezone = default_timezone or "UTC"
        self._client = httpx.Client(timeout=45)

    def close(self) -> None:
        self._client.close()

    @property
    def credentials_changed(self) -> bool:
        return self.auth.dirty

    def current_credentials(self) -> dict:
        return self.auth.as_credentials()

    # -- Capabilities ----------------------------------------------------------

    def echo_of(self, record: CanonicalRecord, kind: CollectionKind) -> CanonicalRecord:
        """Microsoft coarsens the priority, and will say so when asked again.

        To Do has three levels -- high, normal and low -- against iCalendar's
        nine, so a 2 goes out as "high" and comes back as a 1. Nothing is lost
        that To Do ever claimed to hold, and it is not an edit, but it differs
        from what was sent and the echo check compares against what was sent.

        Left undeclared, this cost 604 writes on the second pass of a 400-item
        live run: every prioritised task looked edited the moment Microsoft
        reported it back, so the canonical record was rewritten and the
        "change" pushed out to every other service in the group. The same fault
        was found and fixed for Todoist and TickTick before Microsoft had ever
        been connected to anything, which is why it survived here.

        The times need no such handling. A task's time of day is not claimed at
        all, so projection drops it before this is reached, and an event comes
        back relabelled to UTC rather than moved -- the merge compares timed
        fields by the moment they denote, so the same instant under another
        zone name is already recognised as unchanged.
        """
        from copy import deepcopy

        echoed = deepcopy(record)
        echoed.priority = _IMPORTANCE_TO_CANONICAL.get(
            _CANONICAL_TO_IMPORTANCE.get(record.priority or 5, "normal"), 5
        )
        return echoed

    def capabilities(self, kind: CollectionKind) -> Capabilities:
        if kind == CollectionKind.TASKS:
            return Capabilities(
                # No due_time. Microsoft To Do keeps only the date, exactly as
                # Google Tasks does, and omitting the field is what protects a
                # time set anywhere else.
                fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE,
                                  F_PRIORITY, F_TAGS}),
                stores_uid=False,
                max_title_length=255,
            )
        return Capabilities(
            fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_START, F_END,
                              F_LOCATION, F_RRULE}),
            stores_uid=True,
        )

    # -- HTTP ------------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> Any:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.auth.token()}"
        headers.setdefault("Accept", "application/json")
        if not url.startswith("http"):
            url = f"{GRAPH}{url}"
        try:
            response = self._client.request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise ConnectorError(f"Could not reach Microsoft: {exc}") from exc

        if response.status_code in (401, 403):
            raise ConnectorAuthError(
                f"Microsoft refused access: {_graph_error(response)}. "
                "Try reconnecting this account."
            )
        if response.status_code == 429:
            raise RateLimitError("Microsoft is rate limiting this account.",
                                 retry_after=_retry_after(response))
        if response.status_code == 404:
            raise ConnectorGoneError("That list or item no longer exists in Microsoft.")
        if response.status_code >= 500:
            raise RateLimitError(
                f"Microsoft returned a server error ({response.status_code}).",
                retry_after=_retry_after(response) or 30)
        if response.status_code >= 400:
            raise ConnectorError(_graph_error(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _paged(self, url: str, params: dict | None = None) -> list[dict]:
        """Follow Graph's @odata.nextLink until everything is collected."""
        results: list[dict] = []
        next_url: str | None = url
        first = True
        for _ in range(100):
            payload = self._request(
                "GET", next_url, params=params if first else None
            ) or {}
            first = False
            results.extend(payload.get("value") or [])
            next_url = payload.get("@odata.nextLink")
            if not next_url:
                break
        return results

    # -- Discovery -------------------------------------------------------------

    def verify(self) -> str:
        me = self._request("GET", "/me") or {}
        return me.get("userPrincipalName") or me.get("mail") or "Microsoft account"

    def list_remote_lists(self) -> list[RemoteList]:
        lists: list[RemoteList] = []

        for entry in self._paged("/me/todo/lists"):
            lists.append(RemoteList(
                remote_id=entry["id"],
                name=entry.get("displayName") or "Untitled list",
                kind=CollectionKind.TASKS,
                is_default=entry.get("wellknownListName") == "defaultList",
            ))

        for entry in self._paged("/me/calendars"):
            lists.append(RemoteList(
                remote_id=entry["id"],
                name=entry.get("name") or "Untitled calendar",
                kind=CollectionKind.CALENDAR,
                colour=entry.get("hexColor") or None,
                is_default=bool(entry.get("isDefaultCalendar")),
                # Graph says whether the account may write; a shared calendar
                # offered as writable would fail every push.
                read_only=not entry.get("canEdit", True),
            ))
        return lists

    # -- Reading ---------------------------------------------------------------

    def pull(self, remote_list_id: str, kind: CollectionKind,
             since: dt.datetime | None = None, state: dict | None = None) -> PullResult:
        if kind == CollectionKind.TASKS:
            return self._pull_tasks(remote_list_id)
        return self._pull_events(remote_list_id)

    def _pull_tasks(self, list_id: str) -> PullResult:
        caps = self.capabilities(CollectionKind.TASKS)
        entries = self._paged(f"/me/todo/lists/{list_id}/tasks", {"$top": 100})
        items = [
            RemoteItem(
                remote_id=entry["id"],
                record=self._task_to_record(entry),
                fields_present=caps.present_fields(),
                remote_updated_at=_parse_timestamp(entry.get("lastModifiedDateTime")),
            )
            for entry in entries
        ]
        return PullResult(items=items, incremental=False)

    def _task_to_record(self, entry: dict) -> CanonicalRecord:
        status = (entry.get("status") or "").lower()
        done = status == "completed"
        body = entry.get("body") or {}
        due_date, _due_time, _zone = _parse_graph_datetime(entry.get("dueDateTime"))
        completed_at, _t, _z = _parse_graph_datetime(entry.get("completedDateTime"))
        return CanonicalRecord(
            uid=new_uid(),
            kind=CollectionKind.TASKS,
            title=entry.get("title") or "",
            notes=(body.get("content") or "").strip() or None,
            status=ItemStatus.COMPLETED if done else (
                ItemStatus.IN_PROCESS if status == "inprogress" else ItemStatus.NEEDS_ACTION
            ),
            completed_at=(
                dt.datetime.combine(completed_at, dt.time.min, dt.timezone.utc)
                if completed_at else None
            ),
            due_date=due_date,
            # Deliberately unset: To Do cannot hold a time, so reporting one --
            # even as None -- would let it overwrite the real value.
            due_time=None, due_tz=None,
            priority=_IMPORTANCE_TO_CANONICAL.get(entry.get("importance") or "normal", 5),
            tags=list(entry.get("categories") or []),
            origin_service=ServiceKind.MICROSOFT,
            created_at=_parse_timestamp(entry.get("createdDateTime")),
            updated_at=_parse_timestamp(entry.get("lastModifiedDateTime")),
        )

    def _pull_events(self, calendar_id: str) -> PullResult:
        caps = self.capabilities(CollectionKind.CALENDAR)
        # The plain events collection returns each series once, as its master,
        # rather than expanding it into thousands of instances.
        entries = self._paged(f"/me/calendars/{calendar_id}/events", {"$top": 100})
        items = [
            RemoteItem(
                remote_id=entry["id"],
                record=self._event_to_record(entry),
                fields_present=caps.present_fields(),
                remote_updated_at=_parse_timestamp(entry.get("lastModifiedDateTime")),
                deleted=bool(entry.get("isCancelled")),
            )
            for entry in entries
        ]
        return PullResult(items=items, incremental=False)

    def _event_to_record(self, entry: dict) -> CanonicalRecord:
        all_day = bool(entry.get("isAllDay"))
        s_date, s_time, s_zone = _parse_graph_datetime(entry.get("start"))
        e_date, e_time, e_zone = _parse_graph_datetime(entry.get("end"))
        if all_day:
            s_time = e_time = None
            s_zone = e_zone = None
        body = entry.get("body") or {}
        location = (entry.get("location") or {}).get("displayName") or None
        return CanonicalRecord(
            uid=entry.get("iCalUId") or new_uid(),
            kind=CollectionKind.CALENDAR,
            title=entry.get("subject") or "",
            notes=_strip_html(body.get("content")) if body.get("content") else None,
            location=location,
            status=ItemStatus.CANCELLED if entry.get("isCancelled") else ItemStatus.NEEDS_ACTION,
            start_date=s_date, start_time=s_time, start_tz=s_zone,
            end_date=e_date, end_time=e_time, end_tz=e_zone,
            all_day=all_day,
            rrule=_recurrence_to_rrule(entry.get("recurrence")),
            origin_service=ServiceKind.MICROSOFT,
            created_at=_parse_timestamp(entry.get("createdDateTime")),
            updated_at=_parse_timestamp(entry.get("lastModifiedDateTime")),
        )

    # -- Writing ---------------------------------------------------------------

    def _write_steps(self, list_id: str, task_id: str | None,
                     record: CanonicalRecord) -> None:
        """Put a task's subtasks on it as Microsoft's own checklist items.

        Verified against a live account: a checklist item holds a name and
        whether it is ticked, and nothing else -- a due date on one is refused
        outright. So this is the honest shape for subtasks here, and their dates
        stay in Task Hub rather than being silently dropped on the floor.

        Replaced wholesale rather than merged. They carry no identifier of ours,
        so matching them up would mean guessing by title, and two steps called
        "Call back" would defeat it.
        """
        steps = folded_steps(record)
        if not task_id:
            return
        base = f"/me/todo/lists/{list_id}/tasks/{task_id}/checklistItems"
        try:
            existing = (self._request("GET", base) or {}).get("value") or []
            if not steps and not existing:
                return
            for item in existing:
                self._request("DELETE", f"{base}/{item['id']}")
            for title, done in steps:
                self._request("POST", base,
                              json={"displayName": title, "isChecked": done})
        except ConnectorError as exc:
            # The task itself saved; only its steps did not. Worth a line in the
            # log and another try next pass, not a failed write.
            logger.info("Could not write checklist items: %s", exc)

    def create(self, remote_list_id: str, record: CanonicalRecord,
               kind: CollectionKind) -> PushOutcome:
        try:
            if kind == CollectionKind.TASKS:
                created = self._request(
                    "POST", f"/me/todo/lists/{remote_list_id}/tasks",
                    json=self._record_to_task(record))
                self._write_steps(remote_list_id, (created or {}).get("id"), record)
            else:
                created = self._request(
                    "POST", f"/me/calendars/{remote_list_id}/events",
                    json=self._record_to_event(record))
        except ConnectorError as exc:
            return PushOutcome(remote_id=None, error=str(exc))
        created = created or {}
        return PushOutcome(
            remote_id=created.get("id"),
            remote_updated_at=_parse_timestamp(created.get("lastModifiedDateTime")),
        )

    def update(self, remote_list_id: str, remote_id: str, record: CanonicalRecord,
               kind: CollectionKind) -> PushOutcome:
        try:
            if kind == CollectionKind.TASKS:
                updated = self._request(
                    "PATCH", f"/me/todo/lists/{remote_list_id}/tasks/{remote_id}",
                    json=self._record_to_task(record))
                self._write_steps(remote_list_id, remote_id, record)
            else:
                updated = self._request(
                    "PATCH", f"/me/calendars/{remote_list_id}/events/{remote_id}",
                    json=self._record_to_event(record))
        except ConnectorError as exc:
            return PushOutcome(remote_id=remote_id, error=str(exc))
        updated = updated or {}
        return PushOutcome(
            remote_id=updated.get("id", remote_id),
            remote_updated_at=_parse_timestamp(updated.get("lastModifiedDateTime")),
        )

    def delete(self, remote_list_id: str, remote_id: str,
               kind: CollectionKind) -> PushOutcome:
        try:
            if kind == CollectionKind.TASKS:
                self._request("DELETE", f"/me/todo/lists/{remote_list_id}/tasks/{remote_id}")
            else:
                self._request("DELETE", f"/me/calendars/{remote_list_id}/events/{remote_id}")
        except ConnectorGoneError:
            # Already gone, which is what a delete was asking for.
            return PushOutcome(remote_id=remote_id)
        except ConnectorError as exc:
            return PushOutcome(remote_id=remote_id, error=str(exc))
        return PushOutcome(remote_id=remote_id)

    def _record_to_task(self, record: CanonicalRecord) -> dict:
        body: dict[str, Any] = {
            "title": (record.title or "")[:255],
            "status": "completed" if record.status == ItemStatus.COMPLETED else "notStarted",
            "importance": _CANONICAL_TO_IMPORTANCE.get(record.priority or 5, "normal"),
            "categories": list(record.tags or []),
            "body": {"content": record.notes or "", "contentType": "text"},
        }
        if record.due_date:
            # Sent as midnight UTC, which is how To Do stores a date. A local
            # time here would be shifted a day for anyone west of UTC.
            body["dueDateTime"] = {
                "dateTime": dt.datetime.combine(record.due_date, dt.time.min).isoformat(
                    timespec="seconds"),
                "timeZone": "UTC",
            }
        else:
            body["dueDateTime"] = None
        return body

    def _record_to_event(self, record: CanonicalRecord) -> dict:
        body: dict[str, Any] = {
            "subject": record.title or "",
            "body": {"contentType": "text", "content": record.notes or ""},
            "isAllDay": record.start_time is None and record.start_date is not None,
        }
        if record.location:
            body["location"] = {"displayName": record.location}

        if record.start_date and record.start_time is None:
            end = record.end_date or (record.start_date + dt.timedelta(days=1))
            # Graph wants midnight-to-midnight for an all-day event, with the
            # same exclusive end iCalendar uses.
            body["start"] = {"dateTime": f"{record.start_date}T00:00:00", "timeZone": "UTC"}
            body["end"] = {"dateTime": f"{end}T00:00:00", "timeZone": "UTC"}
        elif record.start_date:
            zone = record.start_tz or self.default_timezone
            body["start"] = _graph_datetime(record.start_date, record.start_time, zone)
            end_date = record.end_date or record.start_date
            end_time = record.end_time or record.start_time
            body["end"] = _graph_datetime(end_date, end_time,
                                          record.end_tz or zone)
        return body


def _strip_html(text: str) -> str:
    """Graph returns HTML bodies; the canonical record holds plain text."""
    import re

    without_tags = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    without_tags = re.sub(r"</p\s*>", "\n", without_tags, flags=re.I)
    without_tags = re.sub(r"<[^>]+>", "", without_tags)
    return (without_tags
            .replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .strip())


def _recurrence_to_rrule(recurrence: dict | None) -> str | None:
    """Convert a Graph recurrence into an RRULE, for the common patterns.

    Graph describes recurrence as a structured object rather than a rule
    string. Only the shapes that map cleanly are converted; anything else
    returns nothing, so an event syncs as a single occurrence rather than as a
    wrong repeating one.
    """
    if not recurrence:
        return None
    pattern = recurrence.get("pattern") or {}
    kind = (pattern.get("type") or "").lower()
    interval = pattern.get("interval") or 1
    days = [d[:2].upper() for d in (pattern.get("daysOfWeek") or [])]

    if kind == "daily":
        rule = f"FREQ=DAILY;INTERVAL={interval}"
    elif kind in ("weekly",):
        rule = f"FREQ=WEEKLY;INTERVAL={interval}"
        if days:
            rule += ";BYDAY=" + ",".join(days)
    elif kind in ("absolutemonthly",):
        rule = f"FREQ=MONTHLY;INTERVAL={interval}"
        if pattern.get("dayOfMonth"):
            rule += f";BYMONTHDAY={pattern['dayOfMonth']}"
    elif kind in ("absoluteyearly",):
        rule = f"FREQ=YEARLY;INTERVAL={interval}"
    else:
        return None

    span = recurrence.get("range") or {}
    if span.get("type") == "endDate" and span.get("endDate"):
        rule += ";UNTIL=" + str(span["endDate"]).replace("-", "") + "T000000Z"
    elif span.get("type") == "numbered" and span.get("numberOfOccurrences"):
        rule += f";COUNT={span['numberOfOccurrences']}"
    return rule


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _graph_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = body.get("error")
    if isinstance(error, dict):
        return error.get("message") or str(error)
    return str(error or f"HTTP {response.status_code}")
