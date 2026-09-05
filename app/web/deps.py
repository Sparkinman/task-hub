"""Shared web plumbing: templates, the authentication gate and flash messages."""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.config import ASSET_VERSION, RUNTIME, TEMPLATES_DIR
from app.db import settings_store
from app.web import public_url
from app.db.models import ServiceKind, User

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

#: Paths served without a login. Radicale is included because CalDAV clients
#: authenticate against Radicale's own htpasswd with HTTP Basic auth -- putting
#: the session gate in front of it would lock every phone and laptop out.
PUBLIC_PREFIXES: tuple[str, ...] = (
    "/static",
    "/radicale",
    "/healthz",
    "/favicon.ico",
    # Service discovery. A CalDAV client asks for this before it has any
    # credentials and has no way to obtain a session, so gating it answers the
    # discovery request with a login page -- which iOS reports as a rejected
    # password, sending people off to reset a password that was already right.
    "/.well-known",
    # An installed web app fetches these before anyone has signed in, and a
    # service worker only controls the pages below the path it was served from
    # -- so it has to come from the root rather than from /static.
    "/sw.js",
    "/manifest.webmanifest",
    "/offline",
)

#: Reachable while logged out, so a user can actually log in or set up.
AUTH_EXEMPT_PREFIXES: tuple[str, ...] = ("/login", "/logout", "/setup")


def is_public_path(path: str) -> bool:
    """Whether a path is served without any login check.

    The match must stop at a path separator. A bare ``startswith`` would make
    every path that merely begins with the same letters public too -- which is
    how ``/radicale-admin``, the page that creates and deletes collections and
    changes the CalDAV password, once fell through the gate on the strength of
    ``/radicale``.
    """
    return any(
        path == p or path.startswith(p + "/") for p in PUBLIC_PREFIXES
    )


def is_auth_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in AUTH_EXEMPT_PREFIXES)


# --- Session helpers ----------------------------------------------------------

SESSION_USER_KEY = "user_id"
SESSION_FLASH_KEY = "_flash"


def login_user(request: Request, user: User) -> None:
    request.session[SESSION_USER_KEY] = user.id
    request.session["username"] = user.username


def logout_user(request: Request) -> None:
    request.session.clear()


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    return db.get(User, user_id)


def flash(request: Request, message: str, category: str = "info") -> None:
    """Queue a one-shot message to show on the next rendered page."""
    messages = request.session.setdefault(SESSION_FLASH_KEY, [])
    messages.append({"message": message, "category": category})
    request.session[SESSION_FLASH_KEY] = messages


def pop_flashes(request: Request) -> list[dict[str, str]]:
    messages = request.session.pop(SESSION_FLASH_KEY, [])
    return list(messages)


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    """Redirect after a form POST.

    303 rather than 302 so the browser switches to GET and a refresh cannot
    resubmit the form.
    """
    return RedirectResponse(url=url, status_code=status_code)


# --- Presentation helpers -----------------------------------------------------

#: Badge colours: Google green, Todoist red, TickTick yellow, Obsidian purple,
#: Microsoft and Things 3 in their own blues, Supernote black, items created in
#: Task Hub blue, and anything still unknown black and labelled "3rd party".
#:
#: Obsidian is named rather than lumped in with "3rd party" on purpose. A task
#: from a vault is one whose real home is a note somewhere, and knowing that at
#: a glance is the difference between "why is this here?" and "ah, that one".
SERVICE_BADGES: dict[str, dict[str, str]] = {
    ServiceKind.GOOGLE.value: {"label": "Google", "colour": "green"},
    ServiceKind.TODOIST.value: {"label": "Todoist", "colour": "red"},
    ServiceKind.TICKTICK.value: {"label": "TickTick", "colour": "yellow"},
    ServiceKind.LOCAL.value: {"label": "Task Hub", "colour": "blue"},
    ServiceKind.RADICALE.value: {"label": "3rd party", "colour": "black"},
    # Apple's own badge and its own red. It shared Todoist's name for a colour
    # nowhere -- it had no badge at all and fell through to the generic "3rd
    # party" black -- and now that it has one, the red is deliberately a cooler
    # crimson than Todoist's warm brick. Two services on one task is the normal
    # case, so the badges have to differ by hue rather than by shade.
    ServiceKind.APPLE.value: {"label": "Apple", "colour": "crimson"},
    # A task from a CalDAV account somewhere else. Named for the protocol
    # because that is genuinely all Task Hub knows: Nextcloud, Fastmail and a
    # self-hosted Baikal are indistinguishable from the wire.
    ServiceKind.CALDAV.value: {"label": "CalDAV", "colour": "amber"},
    # Named and coloured for themselves now that both are connectors rather
    # than possibilities. Each takes its own brand blue, which also keeps them
    # apart from the blue used for items made in Task Hub itself.
    ServiceKind.MICROSOFT.value: {"label": "Microsoft", "colour": "msblue"},
    ServiceKind.THINGS3.value: {"label": "Things 3", "colour": "things"},
    ServiceKind.OBSIDIAN.value: {"label": "Obsidian", "colour": "purple"},
    # Black, for a device whose whole point is electronic ink.
    ServiceKind.SUPERNOTE.value: {"label": "Supernote", "colour": "black"},
}

DEFAULT_BADGE = {"label": "3rd party", "colour": "black"}


def badge_for(service: Any) -> dict[str, str]:
    value = getattr(service, "value", service)
    return SERVICE_BADGES.get(str(value), DEFAULT_BADGE)


def resolve_tz(name: str | None) -> dt.tzinfo:
    try:
        return ZoneInfo(name) if name else dt.timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def format_date(value: dt.date | None, fmt: str = "YYYY-MM-DD") -> str:
    if value is None:
        return ""
    patterns = {
        "YYYY-MM-DD": "%Y-%m-%d",
        "DD/MM/YYYY": "%d/%m/%Y",
        "MM/DD/YYYY": "%m/%d/%Y",
        "D MMM YYYY": "%-d %b %Y",
        "MMM D, YYYY": "%b %-d, %Y",
    }
    return value.strftime(patterns.get(fmt, "%Y-%m-%d"))


def format_time(value: dt.time | None, fmt: str = "24h") -> str:
    if value is None:
        return ""
    if fmt == "12h":
        return value.strftime("%-I:%M %p")
    return value.strftime("%H:%M")


def _supernote_connected(db: Session) -> bool:
    """Whether any Supernote account is connected.

    Cheap enough for every render -- one indexed count -- and the alternative
    is every route remembering to pass it, which is exactly what this context
    exists to avoid.
    """
    from sqlalchemy import func, select as _select

    from app.db.models import Account as _Account
    from app.db.models import ServiceKind as _ServiceKind

    return bool(
        db.execute(
            _select(func.count(_Account.id)).where(
                _Account.service == _ServiceKind.SUPERNOTE,
                _Account.enabled.is_(True),
            )
        ).scalar_one()
    )


def build_template_context(request: Request, db: Session, **extra: Any) -> dict[str, Any]:
    """Base context every page render needs.

    Centralised so that adding something to the chrome -- a nav item, the theme
    setting -- does not mean editing every route handler.
    """
    settings = settings_store.all_settings(db)
    user = get_current_user(request, db)
    context: dict[str, Any] = {
        "request": request,
        "user": user,
        "settings": settings,
        "flashes": pop_flashes(request),
        "timezone": settings.get(settings_store.TIMEZONE, "UTC"),
        "date_format": settings.get(settings_store.DATE_FORMAT, "YYYY-MM-DD"),
        "time_format": settings.get(settings_store.TIME_FORMAT, "24h"),
        "theme": settings.get(settings_store.THEME, "system"),
        # Exposed to every render so shared partials can hide advanced controls
        # without each route having to remember to pass it down.
        "advanced_mode": settings_store.is_advanced(db),
        # Not a configured value: the address this request arrived on, so a
        # page copied from a phone shows what the phone can actually reach.
        "base_url": public_url.public_base_url(request, db),
        "detected_base_url": public_url.detected_base_url(request),
        "current_path": request.url.path,
        # The request's own session, for the few template helpers that have to
        # look something up -- resolving a task's notebook link needs to know
        # whether that notebook is backed up, and the answer changes whenever
        # somebody unticks a folder. Read-only use only.
        "db_session": db,
        # Notes and Digests only exist because of Supernote, and a navigation
        # item for a service nobody has connected reads as a feature that is
        # broken rather than one that is unused. Both pages still work if
        # somebody has bookmarked them; only the link goes.
        "has_supernote": _supernote_connected(db),
        "now": dt.datetime.now(dt.timezone.utc),
    }
    context.update(extra)
    return context


def render(request: Request, db: Session, template: str, **extra: Any):
    return templates.TemplateResponse(
        request, template, build_template_context(request, db, **extra)
    )


# --- Jinja registration -------------------------------------------------------

templates.env.globals["badge_for"] = badge_for


def note_link_for(db, item):
    """Where a task's notebook reference points, or None if nowhere.

    A global rather than something each view passes down: the task list, the
    calendar and the search results all render the same rows, and three places
    remembering to look it up is three places to forget.
    """
    from app.web.note_links import link_for

    try:
        return link_for(db, item)
    except Exception:  # noqa: BLE001 - a decoration must never break a page
        return None


templates.env.globals["note_link_for"] = note_link_for
templates.env.globals["app_name"] = "Task Hub"
# Stamped onto the CSS and JS URLs so an upgrade cannot be served from a
# browser cache that never revalidated.
templates.env.globals["asset_version"] = ASSET_VERSION
templates.env.filters["fmt_date"] = format_date
templates.env.filters["fmt_time"] = format_time
