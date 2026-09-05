"""Typed accessors over the ``app_settings`` key/value table.

Keeping the keys and their defaults in one place means the rest of the codebase
never has to remember that the sync interval is stored as a string, or repeat
the three-minute floor.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSetting

# --- Keys ---------------------------------------------------------------------

ONBOARDING_COMPLETE: Final = "onboarding_complete"
TIMEZONE: Final = "timezone"
DATE_FORMAT: Final = "date_format"
TIME_FORMAT: Final = "time_format"
WEEK_START: Final = "week_start"
SYNC_INTERVAL_MINUTES: Final = "sync_interval_minutes"
SYNC_ENABLED: Final = "sync_enabled"
RADICALE_USERNAME: Final = "radicale_username"
BASE_URL_OVERRIDE: Final = "base_url_override"

#: Supernote note backup: which folders, how often, and whether it runs at all.
SUPERNOTE_BACKUP_ENABLED: Final = "supernote_backup_enabled"
SUPERNOTE_BACKUP_FOLDERS: Final = "supernote_backup_folders"
SUPERNOTE_BACKUP_INTERVAL_MINUTES: Final = "supernote_backup_interval_minutes"

#: Push notifications. The private half is encrypted at rest like any other
#: credential; the public half is handed to every browser that subscribes.
PUSH_PRIVATE_KEY: Final = "push_vapid_private_enc"
PUSH_PUBLIC_KEY: Final = "push_vapid_public"
#: Supernote digests: which libraries, and whether to mirror them at all.
#: Send subtasks to services that cannot nest as separate top-level tasks.
#:
#: Off by default. A parent and its eight steps arrive at a flat list as nine
#: unrelated tasks with nothing marking which is which, which on a small screen
#: is a mess; folded into the parent instead, they stay legible. On for anybody
#: who would rather tick them off individually on the device.
SUBTASKS_AS_SEPARATE: Final = "subtasks_as_separate_tasks"

#: How subtasks are shown on the Supernote, which displays only a title and a
#: date -- no notes, confirmed on the device.
#:
#: "label"  puts each task's place in its piece of work into the title itself.
#: "lists"  makes a to-do list on the tablet named after the parent task and
#:          puts its steps in there.
#: "plain"  sends them as ordinary tasks with nothing to tie them together.
SUPERNOTE_SUBTASK_STYLE: Final = "supernote_subtask_style"

#: Remove lists Task Hub made once nothing is left in them.
SUPERNOTE_TIDY_LISTS: Final = "supernote_tidy_lists"

SUPERNOTE_DIGEST_ENABLED: Final = "supernote_digest_enabled"
SUPERNOTE_DIGEST_LIBRARIES: Final = "supernote_digest_libraries"
#: Every library on the account, remembered from the last sync as JSON.
#: Filing a new digest must offer all of them, not only the ones being
#: mirrored, and a page should not make a request to Supernote to draw a menu.
SUPERNOTE_DIGEST_LIBRARY_CACHE: Final = "supernote_digest_library_cache"

PUSH_ENABLED: Final = "push_enabled"

#: What a notification may be about. Separate switches rather than one, because
#: the three are wanted by different people: a failing sync is an emergency to
#: somebody relying on it and noise to somebody who checks the page anyway.
PUSH_ON_TASKS: Final = "push_on_tasks"
PUSH_ON_SYNC_FAILURE: Final = "push_on_sync_failure"
PUSH_ON_EXPIRING: Final = "push_on_expiring"
THEME: Final = "theme"
ADVANCED_MODE: Final = "advanced_mode"
TUNNEL_ENABLED: Final = "tunnel_enabled"
TUNNEL_TOKEN: Final = "tunnel_token_enc"

# --- Outgoing mail ------------------------------------------------------------
#
# Task Hub sends mail and never receives any: there is no inbox, no listener and
# no open port. The password is stored encrypted like every other credential,
# and the interface asks for an app-specific one wherever the provider offers
# them, because this is otherwise a real mailbox password.
SMTP_HOST: Final = "smtp_host"
SMTP_PORT: Final = "smtp_port"
SMTP_SECURITY: Final = "smtp_security"
SMTP_USERNAME: Final = "smtp_username"
SMTP_PASSWORD: Final = "smtp_password_enc"
SMTP_FROM: Final = "smtp_from"
#: How the last "send a test message" went, as JSON: whether it worked, where it
#: went, when, and the server's complaint if it did not. Kept because a flash
#: message is gone on the next page load, and "is my email actually working?" is
#: a question people come back to the settings page to ask.
SMTP_TEST_RESULT: Final = "smtp_test_result"

#: The daily summary of what is due.
DIGEST_ENABLED: Final = "digest_enabled"
DIGEST_TIME: Final = "digest_time"
DIGEST_TO: Final = "digest_to"
#: Which days it goes out, as lowercase three-letter day names in the order a
#: cron expression wants them ("mon,wed,fri"). Stored as days rather than as a
#: "daily / weekdays / custom" mode because the mode is only ever a shortcut for
#: picking days, and storing the shortcut would mean two places to get wrong.
DIGEST_DAYS: Final = "digest_days"
#: Whether to send on a day with nothing due. Off by default: a message that
#: arrives every morning saying nothing stops being read within a week, and then
#: the one that matters is not read either.
DIGEST_WHEN_EMPTY: Final = "digest_when_empty"

#: Anything below this hammers the upstream APIs and earns HTTP 429 responses,
#: which then back off and make syncing *slower* than a polite interval would.
#: Enforced here rather than only in the form so an API caller cannot bypass it.
MIN_SYNC_INTERVAL_MINUTES: Final = 3

#: The note backup runs on its own clock, far slower than the task sync.
#:
#: Converting a notebook happens on Ratta's servers, on an API they never
#: published and owe nobody. Polling it at task-sync speed would be both rude
#: and the surest way to have the access withdrawn, so the floor here is thirty
#: minutes rather than three, and the default is measured in hours. Notes are
#: not urgent: a notebook written this morning is no less useful this evening.
MIN_NOTE_BACKUP_INTERVAL_MINUTES: Final = 30
DEFAULT_NOTE_BACKUP_INTERVAL_MINUTES: Final = 360

DEFAULTS: Final[dict[str, str]] = {
    SUPERNOTE_BACKUP_ENABLED: "0",
    SUPERNOTE_BACKUP_FOLDERS: "",
    SUPERNOTE_BACKUP_INTERVAL_MINUTES: "360",
    SUBTASKS_AS_SEPARATE: "0",
    SUPERNOTE_SUBTASK_STYLE: "label",
    SUPERNOTE_TIDY_LISTS: "1",
    SUPERNOTE_DIGEST_ENABLED: "0",
    SUPERNOTE_DIGEST_LIBRARIES: "",
    SUPERNOTE_DIGEST_LIBRARY_CACHE: "",
    PUSH_ENABLED: "1",
    # Off by default. A notification nobody asked for is the fastest way to
    # have every notification switched off.
    PUSH_ON_TASKS: "0",
    PUSH_ON_SYNC_FAILURE: "0",
    PUSH_ON_EXPIRING: "0",
    ONBOARDING_COMPLETE: "0",
    TIMEZONE: "UTC",
    DATE_FORMAT: "YYYY-MM-DD",
    TIME_FORMAT: "24h",
    WEEK_START: "monday",
    SYNC_INTERVAL_MINUTES: "15",
    SYNC_ENABLED: "1",
    RADICALE_USERNAME: "",
    BASE_URL_OVERRIDE: "",
    THEME: "system",
    ADVANCED_MODE: "0",
    TUNNEL_ENABLED: "0",
    TUNNEL_TOKEN: "",
    SMTP_HOST: "",
    SMTP_PORT: "587",
    SMTP_SECURITY: "starttls",
    SMTP_USERNAME: "",
    SMTP_PASSWORD: "",
    SMTP_FROM: "",
    SMTP_TEST_RESULT: "",
    DIGEST_ENABLED: "0",
    DIGEST_TIME: "07:00",
    DIGEST_TO: "",
    DIGEST_DAYS: "mon,tue,wed,thu,fri,sat,sun",
    DIGEST_WHEN_EMPTY: "0",
}

#: The days of the week, in the order they are offered and stored. Monday first
#: regardless of the week-start preference: this is a cron field, not a calendar.
DIGEST_DAY_CODES: Final[tuple[str, ...]] = (
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
)
DIGEST_DAY_NAMES: Final[dict[str, str]] = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}


def digest_days(session: Session) -> list[str]:
    """The days the summary goes out, always in week order and never empty.

    A stored value that has lost every valid day -- hand-edited, or written by
    an older version -- falls back to every day rather than silently switching
    the summary off. Somebody who wanted it off would have used the switch.
    """
    raw = (get(session, DIGEST_DAYS) or "").lower()
    chosen = {part.strip() for part in raw.split(",") if part.strip()}
    days = [code for code in DIGEST_DAY_CODES if code in chosen]
    return days or list(DIGEST_DAY_CODES)


# --- Raw access ---------------------------------------------------------------


def get(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(AppSetting, key)
    if row is not None and row.value is not None:
        return row.value
    if default is not None:
        return default
    return DEFAULTS.get(key)


def set_value(session: Session, key: str, value: str | None) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def set_many(session: Session, values: dict[str, str | None]) -> None:
    for key, value in values.items():
        set_value(session, key, value)


def all_settings(session: Session) -> dict[str, str]:
    """Every setting, with defaults filled in for keys never written."""
    stored = {
        row.key: row.value
        for row in session.execute(select(AppSetting)).scalars()
        if row.value is not None
    }
    return {**DEFAULTS, **stored}


# --- Typed helpers ------------------------------------------------------------


def get_bool(session: Session, key: str) -> bool:
    return (get(session, key) or "0").strip().lower() in {"1", "true", "yes", "on"}


def set_bool(session: Session, key: str, value: bool) -> None:
    set_value(session, key, "1" if value else "0")


def get_int(session: Session, key: str, fallback: int = 0) -> int:
    try:
        return int((get(session, key) or "").strip())
    except (TypeError, ValueError):
        return fallback


def is_advanced(session: Session) -> bool:
    """Whether the advanced controls are shown.

    Off by default. The features it reveals -- writing a list's tasks out to a
    different list, and Radicale's raw CalDAV editor -- are genuinely useful but
    easy to misconfigure, and someone who does not need them is better off never
    being asked about them. When off they are absent from the page entirely
    rather than disabled, so there is nothing to wonder about.
    """
    return get_bool(session, ADVANCED_MODE)


def is_onboarded(session: Session) -> bool:
    return get_bool(session, ONBOARDING_COMPLETE)


def get_timezone(session: Session) -> str:
    return get(session, TIMEZONE) or "UTC"


def get_sync_interval(session: Session) -> int:
    """Sync interval in minutes, never below the safe floor."""
    return max(
        MIN_SYNC_INTERVAL_MINUTES,
        get_int(session, SYNC_INTERVAL_MINUTES, 15),
    )


def set_sync_interval(session: Session, minutes: int) -> int:
    """Store the sync interval, clamping to the floor. Returns what was stored."""
    clamped = max(MIN_SYNC_INTERVAL_MINUTES, int(minutes))
    set_value(session, SYNC_INTERVAL_MINUTES, str(clamped))
    return clamped
