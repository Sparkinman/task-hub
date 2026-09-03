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
THEME: Final = "theme"
ADVANCED_MODE: Final = "advanced_mode"
TUNNEL_ENABLED: Final = "tunnel_enabled"
TUNNEL_TOKEN: Final = "tunnel_token_enc"

#: Anything below this hammers the upstream APIs and earns HTTP 429 responses,
#: which then back off and make syncing *slower* than a polite interval would.
#: Enforced here rather than only in the form so an API caller cannot bypass it.
MIN_SYNC_INTERVAL_MINUTES: Final = 3

DEFAULTS: Final[dict[str, str]] = {
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
}


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
