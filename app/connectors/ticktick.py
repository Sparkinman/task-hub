"""TickTick, through the public Open API.

TickTick's Open API is much narrower than Todoist's or Google's, and two of its
gaps shape almost everything in this file.

**There is no "list everything" endpoint.** The only way to read tasks is
``GET /project/{id}/data``, which returns a project together with its *open*
tasks. Completed tasks are simply not in the response.

That second point is dangerous rather than merely inconvenient. The sync engine
treats a task that has vanished from a complete listing as deleted everywhere --
which is right for a real deletion and catastrophic for a task someone merely
ticked off. So this connector never claims a complete listing: it marks every
pull incremental, which switches that inference off, and then works out for
itself what happened to the tasks that disappeared. It remembers the ids it saw
last time, and asks TickTick about each missing one individually --
``GET /project/{id}/task/{taskId}`` answers for a completed task even though the
listing omits it. A task that comes back completed is reported as completed; one
that is genuinely gone answers 404 and is reported as deleted.

That costs one request per disappeared task, which is bounded by how much you
actually get done, and it is the only way to tell "done" from "deleted" here.
"""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any
from urllib.parse import urlencode

import httpx

from app.connectors.base import (
    F_DUE_DATE,
    F_DUE_TIME,
    F_NOTES,
    F_PRIORITY,
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
from app.services.ical_model import CanonicalRecord
from app.services.timezones import to_utc, wall_time

API_BASE = "https://api.ticktick.com/open/v1"
AUTH_ENDPOINT = "https://ticktick.com/oauth/authorize"
TOKEN_ENDPOINT = "https://ticktick.com/oauth/token"

#: TickTick's two scopes, space separated. There is no separate delete scope.
SCOPES = "tasks:read tasks:write"

#: Where the user registers an app and reads off the Client ID and Secret.
DEVELOPER_CENTRE = "https://developer.ticktick.com/manage"

#: TickTick's status values.
STATUS_ACTIVE = 0
STATUS_COMPLETED = 2

#: How many disappeared tasks to probe in one pull. A list that loses hundreds
#: of tasks at once is far more likely to be a service fault than a burst of
#: productivity, and hammering the API to confirm it helps nobody.
MAX_PROBES = 60


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return f"{AUTH_ENDPOINT}?" + urlencode(
        {
            "client_id": client_id,
            "scope": SCOPES,
            "state": state,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
    )


def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    """Swap the authorization code for a token.

    TickTick wants the client credentials as HTTP Basic auth rather than in the
    form body, and answers 400 with no useful detail if they are sent the other
    way -- which is the single most common reason a TickTick connection fails.
    """
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = httpx.post(
        TOKEN_ENDPOINT,
        headers={"Authorization": f"Basic {basic}"},
        data={
            "code": code,
            "grant_type": "authorization_code",
            "scope": SCOPES,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise ConnectorAuthError(_explain_token_error(response))

    payload = response.json()
    if not payload.get("access_token"):
        raise ConnectorAuthError("TickTick did not return an access token.")
    return {
        "access_token": payload["access_token"],
        "token_type": payload.get("token_type", "Bearer"),
        "scope": payload.get("scope", SCOPES),
    }


def _explain_token_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") or payload.get("error_description") or ""

    if response.status_code in (400, 401) and "redirect" in str(error).lower():
        return (
            "TickTick rejected the redirect URI. It must match the one saved in "
            "the TickTick Developer Center exactly, including http vs https, the "
            "port and any trailing slash."
        )
    if response.status_code in (400, 401):
        return (
            "TickTick rejected the Client ID or Client Secret. Check both on the "
            "Developer Center, and that the redirect URI registered there is "
            "exactly the one Task Hub shows."
        )
    return f"TickTick returned {response.status_code}: {error or response.text[:200]}"


# --- Priority -----------------------------------------------------------------
#
# TickTick uses 0 none, 1 low, 3 medium, 5 high. iCalendar uses 1-9 with 1 most
# urgent and 0 unset. Written out both ways so the mapping round-trips exactly:
# a task moved TickTick -> Task Hub -> TickTick comes back with the priority it
# started with rather than drifting a step each pass.

_TO_CANONICAL = {5: 1, 3: 5, 1: 9, 0: 0}


def ticktick_priority_to_canonical(value: Any) -> int:
    try:
        return _TO_CANONICAL.get(int(value), 0)
    except (TypeError, ValueError):
        return 0


def canonical_priority_to_ticktick(value: int | None) -> int:
    if not value:
        return 0
    if value <= 2:
        return 5
    if value <= 6:
        return 3
    return 1


def _parse_moment(
    raw: str | None,
    all_day: bool,
    timezone: str | None,
    default_zone: str | None = None,
) -> tuple[dt.date | None, dt.time | None, str | None]:
    """Split a TickTick timestamp into date, time and timezone.

    TickTick always sends the instant in UTC (``...+0000``) and names the user's
    zone separately in ``timeZone``. The clock time the user set is therefore the
    UTC instant *converted into that zone*: 16:00+0000 with Europe/London is a
    task due at five o'clock, not four. Reading the UTC clock face directly
    shifts every timed task by the local offset.

    All-day tasks still arrive with a time portion, and it is meaningless.
    Returning it would invent a midnight that then propagates everywhere.
    """
    if not raw:
        return None, None, None
    if all_day:
        date, _, _ = wall_time(raw, timezone, default_zone)
        return date, None, None
    return wall_time(raw, timezone, default_zone)


def _format_moment(
    date: dt.date | None,
    time_of_day: dt.time | None,
    timezone: str | None = None,
    default_zone: str | None = None,
) -> str | None:
    """Render a wall time as the UTC instant TickTick expects.

    The mirror of :func:`_parse_moment`. Stamping ``+0000`` onto a local wall
    time without converting it first shifts the task by the local offset -- and
    in the opposite direction to the reading error, so a task round-tripping
    through a naive implementation moves twice.
    """
    moment = to_utc(date, time_of_day, timezone, default_zone)
    if moment is None:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S+0000")


class TickTickConnector(Connector):
    service = ServiceKind.TICKTICK
    name = "TickTick"

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
        #: Used when TickTick names no timezone of its own.
        self.default_timezone = default_timezone
        self.access_token = credentials.get("access_token")
        self._client = httpx.Client(timeout=30)

    def close(self) -> None:
        self._client.close()

    @property
    def credentials_changed(self) -> bool:
        # TickTick issues no refresh token, so there is nothing to write back.
        return False

    def current_credentials(self) -> dict[str, Any]:
        return dict(self.credentials)

    # -- Capabilities ---------------------------------------------------------

    def capabilities(self, kind: CollectionKind) -> Capabilities:
        """Tasks only, and no tags.

        TickTick does have tags, but the Open API neither returns them on a task
        nor accepts them on a write, so claiming the field would let an empty
        value here erase tags set in Todoist. Recurrence is claimed because
        ``repeatFlag`` really is an RRULE.
        """
        if kind != CollectionKind.TASKS:
            return Capabilities(fields=frozenset(), can_create=False, can_delete=False)
        return Capabilities(
            fields=frozenset(
                {
                    F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE, F_DUE_TIME,
                    F_PRIORITY, F_RRULE,
                }
            ),
            can_delete=True,
            can_create=True,
            stores_uid=False,
        )

    def supports_kind(self, kind: CollectionKind) -> bool:
        return kind == CollectionKind.TASKS

    # -- HTTP -----------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        if not self.access_token:
            raise ConnectorAuthError(
                "This TickTick account has no saved login. Click Reconnect."
            )
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            response = self._client.request(
                method, f"{API_BASE}{path}", headers=headers, **kwargs
            )
        except httpx.RequestError as exc:
            raise ConnectorError(f"Could not reach TickTick: {exc}") from exc

        if response.status_code in (401, 403):
            raise ConnectorAuthError(
                "TickTick refused this login. TickTick tokens cannot be renewed "
                "automatically, so reconnect the account."
            )
        if response.status_code == 429:
            raise RateLimitError(
                "TickTick is rate limiting this account.",
                retry_after=_retry_after(response),
            )
        if response.status_code == 404:
            raise ConnectorGoneError("That list or task no longer exists in TickTick.")
        if response.status_code >= 500:
            raise RateLimitError(
                f"TickTick returned a server error ({response.status_code}).",
                retry_after=_retry_after(response) or 30,
            )
        if response.status_code >= 400:
            raise ConnectorError(
                f"TickTick returned {response.status_code}: {response.text[:200]}"
            )

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # -- Discovery ------------------------------------------------------------

    def verify(self) -> str:
        # The Open API exposes no "who am I" endpoint, so listing projects is
        # the cheapest call that proves the token works.
        self._request("GET", "/project")
        # Nothing useful to report. An empty identity leaves the interface
        # showing the account's friendly name alone, which is honest -- a
        # placeholder like "TickTick account" would be indistinguishable
        # between two connected TickTick accounts, and worse than nothing.
        return ""

    def list_remote_lists(self) -> list[RemoteList]:
        """Every project TickTick will admit to.

        The Inbox is deliberately absent from this endpoint's response, which is
        a TickTick limitation rather than an oversight here: tasks with no
        project cannot be reached through the Open API at all.
        """
        projects = self._request("GET", "/project") or []
        lists: list[RemoteList] = []
        for project in projects:
            lists.append(
                RemoteList(
                    remote_id=str(project.get("id")),
                    name=project.get("name") or "Untitled list",
                    kind=CollectionKind.TASKS,
                    colour=project.get("color"),
                    read_only=str(project.get("permission") or "") == "read",
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
            return PullResult(items=[], incremental=True)

        caps = self.capabilities(kind)
        payload = self._request("GET", f"/project/{remote_list_id}/data") or {}
        tasks = payload.get("tasks") or []

        items: list[RemoteItem] = []
        for entry in tasks:
            items.append(
                RemoteItem(
                    remote_id=str(entry.get("id")),
                    record=self._task_to_record(entry),
                    fields_present=caps.present_fields(),
                    remote_updated_at=_parse_timestamp(entry.get("modifiedTime")),
                )
            )

        present = {item.remote_id for item in items}
        errors: list[str] = []

        # Work out what happened to anything that was here last time and is not
        # here now. Without this the listing simply shrinks and nobody ever
        # learns that a task was completed.
        previously = list((state or {}).get("seen") or [])
        missing = [task_id for task_id in previously if task_id not in present]

        if len(missing) > MAX_PROBES:
            errors.append(
                f"{len(missing)} tasks disappeared from this list at once. That "
                "looks like a TickTick fault rather than real changes, so they "
                "have been left alone."
            )
            missing = []

        for task_id in missing:
            try:
                entry = self._request(
                    "GET", f"/project/{remote_list_id}/task/{task_id}"
                )
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
                    fields_present=caps.present_fields(),
                    remote_updated_at=_parse_timestamp(entry.get("modifiedTime")),
                )
            )
            present.add(task_id)

        return PullResult(
            items=items,
            # Never a complete listing: TickTick omits completed tasks, and
            # letting the engine read that omission as deletion would delete
            # every finished task from every other service.
            incremental=True,
            sync_state={"seen": sorted(present)},
            errors=errors,
        )

    def _task_to_record(self, entry: dict) -> CanonicalRecord:
        all_day = bool(entry.get("isAllDay"))
        timezone = entry.get("timeZone") or None
        due_date, due_time, due_tz = _parse_moment(
            entry.get("dueDate"), all_day, timezone, self.default_timezone
        )

        # TickTick's startDate is never reported back, and the connector does
        # not claim the field at all.
        #
        # TickTick always has a startDate. It fills one in by itself, mirroring
        # the due date on an ordinary task, and it silently ignores a request to
        # clear one -- verified against the live API, which kept a stale value
        # after being sent an explicit null. So its startDate cannot be trusted
        # to mean "the user set a range"; most of the time it means nothing at
        # all, and sometimes it is simply out of date.
        #
        # Reading it invented spans nobody created, and a span moves a task's
        # visible date in Todoist to its start -- so changing a due date looked
        # like it had failed to sync. Not claiming the field means TickTick can
        # never erase or invent a start; it contributes only the due date, which
        # it does hold reliably. A genuine range set in TickTick will not travel
        # outward, which is a real but much smaller loss.
        start_date = start_time = start_tz = None

        # TickTick cannot express a floating time -- every task carries a zone --
        # so a floating one is written out in the user's own zone. Reading that
        # zone back as an explicit setting would pin the task to it, and Task Hub
        # would then report a change no one made on every single pass, bouncing
        # the value between "10am floating" and "10am in Denver" forever.
        # Its own zone therefore reads back as floating, which is what was
        # written. A genuinely different zone is a real choice and is kept.
        if due_tz and self.default_timezone and due_tz == self.default_timezone:
            due_tz = None
        if start_tz and self.default_timezone and start_tz == self.default_timezone:
            start_tz = None
        done = int(entry.get("status") or STATUS_ACTIVE) == STATUS_COMPLETED

        return CanonicalRecord(
            uid="",
            kind=CollectionKind.TASKS,
            title=entry.get("title") or "",
            notes=entry.get("content") or entry.get("desc") or None,
            status=ItemStatus.COMPLETED if done else ItemStatus.NEEDS_ACTION,
            completed_at=_parse_timestamp(entry.get("completedTime")),
            due_date=due_date,
            due_time=due_time,
            due_tz=due_tz,
            start_date=start_date,
            start_time=start_time,
            start_tz=start_tz,
            all_day=all_day,
            priority=ticktick_priority_to_canonical(entry.get("priority")),
            rrule=entry.get("repeatFlag") or None,
            origin_service=ServiceKind.TICKTICK,
            updated_at=_parse_timestamp(entry.get("modifiedTime")),
        )

    # -- Writing --------------------------------------------------------------

    def create(
        self, remote_list_id: str, record: CanonicalRecord, kind: CollectionKind
    ) -> PushOutcome:
        if kind != CollectionKind.TASKS:
            return PushOutcome(remote_id=None, error="TickTick stores tasks only.")

        body = self._record_to_body(record)
        body["projectId"] = remote_list_id
        created = self._request("POST", "/task", json=body) or {}
        remote_id = str(created.get("id")) if created.get("id") else None
        if not remote_id:
            return PushOutcome(remote_id=None, error="TickTick did not return a task id.")

        if record.status == ItemStatus.COMPLETED:
            self._request(
                "POST", f"/project/{remote_list_id}/task/{remote_id}/complete"
            )

        return PushOutcome(
            remote_id=remote_id,
            remote_updated_at=_parse_timestamp(created.get("modifiedTime")),
        )

    def update(
        self,
        remote_list_id: str,
        remote_id: str,
        record: CanonicalRecord,
        kind: CollectionKind,
    ) -> PushOutcome:
        if kind != CollectionKind.TASKS:
            return PushOutcome(remote_id=remote_id, error="TickTick stores tasks only.")

        body = self._record_to_body(record)
        body["id"] = remote_id
        body["projectId"] = remote_list_id
        updated = self._request("POST", f"/task/{remote_id}", json=body) or {}

        # TickTick has a complete endpoint but no reopen one, so a task can be
        # finished from Task Hub and not un-finished. Writing the status field
        # directly on the update is the only lever available for reopening, and
        # it is honoured where completing through the body is not.
        wants_done = record.status == ItemStatus.COMPLETED
        if wants_done:
            self._request(
                "POST", f"/project/{remote_list_id}/task/{remote_id}/complete"
            )

        return PushOutcome(
            remote_id=remote_id,
            remote_updated_at=_parse_timestamp(updated.get("modifiedTime")),
        )

    def delete(
        self, remote_list_id: str, remote_id: str, kind: CollectionKind
    ) -> PushOutcome:
        try:
            self._request("DELETE", f"/project/{remote_list_id}/task/{remote_id}")
        except ConnectorGoneError:
            # Already gone, which is what a delete was asking for.
            return PushOutcome(remote_id=remote_id)
        return PushOutcome(remote_id=remote_id)

    def _record_to_body(self, record: CanonicalRecord) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": record.title or "",
            "content": record.notes or "",
            "priority": canonical_priority_to_ticktick(record.priority),
            "status": (
                STATUS_COMPLETED
                if record.status == ItemStatus.COMPLETED
                else STATUS_ACTIVE
            ),
        }

        all_day = record.due_time is None and record.due_date is not None
        # Each moment carries its own zone, falling back to the user's
        # configured one. Borrowing across fields is what produced tasks landing
        # six hours out: a due time that was floating (Todoist reports
        # "timezone": null, meaning "this clock time, wherever you are") picked
        # up a stray UTC left on the start field by an earlier round trip, so
        # 10am was written as 10:00 UTC and shown at 4am in Denver.
        due_zone = record.due_tz or self.default_timezone
        start_zone = record.start_tz or self.default_timezone
        zone = due_zone or start_zone
        due = _format_moment(record.due_date, record.due_time, due_zone,
                             self.default_timezone)
        if due:
            body["dueDate"] = due
            body["isAllDay"] = all_day
        start = _format_moment(record.start_date, record.start_time, start_zone,
                               self.default_timezone)
        if start:
            body["startDate"] = start
        elif due:
            # No start: mirror the due date, which is exactly how TickTick
            # represents a single-moment task. Sending null does not work --
            # TickTick ignores it and keeps whatever it had -- so a stale range
            # would otherwise stay visible in the app forever.
            body["startDate"] = due
        if zone:
            body["timeZone"] = zone
        if record.rrule:
            body["repeatFlag"] = record.rrule
        return body


def _parse_timestamp(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    if len(text) >= 5 and (text[-5] in "+-") and ":" not in text[-5:]:
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
