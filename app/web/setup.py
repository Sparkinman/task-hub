"""First-run setup wizard.

Runs once, before anything else in the application is reachable, and collects
everything Task Hub cannot sensibly guess: who may log in, what timezone dates
should be interpreted in, the CalDAV credentials, the first task and calendar
collections, and how often to sync.

Each step commits as it is completed and the current position is recorded in the
database, so closing the browser halfway through resumes where it left off
rather than starting over. The installation only counts as set up once the final
step runs, so an interrupted wizard never leaves a half-configured system that
believes it is ready.
"""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import hash_password
from app.db import settings_store
from app.db.models import CollectionKind, RadicaleCollection, User
from app.db.session import get_db
from app.radicale_embed import set_radicale_user
from app.services.caldav_client import (
    CalDAVError,
    RadicaleClient,
    slugify_collection_id,
)
from app.web import deps

router = APIRouter(prefix="/setup")

SETUP_STEP_KEY = "setup_step"
#: Radicale password, held encrypted so the app can reach its own CalDAV server
#: for syncing without prompting. Stored via the same Fernet key as every other
#: credential rather than in plaintext beside the htpasswd hash.
RADICALE_PASSWORD_KEY = "radicale_password_enc"


@dataclass(frozen=True)
class Step:
    slug: str
    title: str
    blurb: str


STEPS: tuple[Step, ...] = (
    Step("welcome", "Welcome", "What Task Hub does and what you will need."),
    Step("account", "Sign-in", "Create the username and password for this web page."),
    Step("preferences", "Region", "Your timezone and how dates should be shown."),
    Step("radicale", "CalDAV", "Credentials for the built-in Radicale server."),
    Step("collections", "Collections", "Your first task list and calendar."),
    Step("sync", "Sync", "How often Task Hub checks your connected services."),
)

STEP_INDEX = {step.slug: i for i, step in enumerate(STEPS)}

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
MIN_PASSWORD_LENGTH = 8


# --- Step bookkeeping ---------------------------------------------------------


def _current_step(db: Session) -> str:
    return settings_store.get(db, SETUP_STEP_KEY, "welcome") or "welcome"


def _advance(db: Session, to_slug: str) -> None:
    """Record progress, never moving the marker backwards."""
    current = STEP_INDEX.get(_current_step(db), 0)
    if STEP_INDEX.get(to_slug, 0) > current:
        settings_store.set_value(db, SETUP_STEP_KEY, to_slug)
        db.commit()


def _render_step(request: Request, db: Session, slug: str, **extra):
    index = STEP_INDEX[slug]
    return deps.render(
        request,
        db,
        f"onboarding/{slug}.html",
        steps=STEPS,
        step=STEPS[index],
        step_index=index,
        step_number=index + 1,
        step_total=len(STEPS),
        **extra,
    )


def _guard(db: Session, slug: str) -> str | None:
    """Stop a user skipping ahead by typing a later step's URL.

    Returns the URL of the step they should be on, or None if this one is fine.
    """
    reached = STEP_INDEX.get(_current_step(db), 0)
    if STEP_INDEX[slug] > reached:
        return f"/setup/{STEPS[reached].slug}"
    return None


# --- Entry point --------------------------------------------------------------


@router.get("")
@router.get("/")
def setup_index(db: Session = Depends(get_db)):
    return deps.redirect(f"/setup/{_current_step(db)}")


# --- 1. Welcome ---------------------------------------------------------------


@router.get("/welcome")
def welcome(request: Request, db: Session = Depends(get_db)):
    return _render_step(request, db, "welcome")


@router.post("/welcome")
def welcome_submit(db: Session = Depends(get_db)):
    _advance(db, "account")
    return deps.redirect("/setup/account")


# --- 2. Web sign-in account ---------------------------------------------------


@router.get("/account")
def account(request: Request, db: Session = Depends(get_db)):
    if (target := _guard(db, "account")):
        return deps.redirect(target)
    existing = db.execute(select(User)).scalars().first()
    return _render_step(
        request, db, "account", username=existing.username if existing else ""
    )


@router.post("/account")
def account_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    errors: list[str] = []

    if not USERNAME_RE.match(username):
        errors.append(
            "The username must be 3-64 characters, using only letters, numbers, "
            "dots, dashes and underscores."
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password != password_confirm:
        errors.append("The two passwords do not match.")

    if errors:
        return _render_step(request, db, "account", errors=errors, username=username)

    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        return _render_step(request, db, "account", errors=[str(exc)], username=username)

    # The wizard is reachable without a login until setup completes, so re-running
    # this step updates the single account rather than creating a second one.
    user = db.execute(select(User)).scalars().first()
    if user is None:
        user = User(username=username, password_hash=password_hash, is_admin=True)
        db.add(user)
    else:
        user.username = username
        user.password_hash = password_hash
    db.commit()
    db.refresh(user)

    # Log them in straight away. From here on the wizard requires a session,
    # which closes the window where an unattended setup page is world-writable.
    deps.login_user(request, user)
    _advance(db, "preferences")
    return deps.redirect("/setup/preferences")


# --- 3. Regional preferences --------------------------------------------------


def _timezones() -> list[str]:
    """Every IANA zone, with the common ones surfaced at the top."""
    common = [
        "UTC",
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "America/Toronto",
        "Europe/London",
        "Europe/Dublin",
        "Europe/Paris",
        "Europe/Berlin",
        "Europe/Madrid",
        "Europe/Rome",
        "Australia/Sydney",
        "Australia/Melbourne",
        "Asia/Tokyo",
        "Asia/Singapore",
        "Asia/Kolkata",
        "Pacific/Auckland",
    ]
    everything = sorted(zoneinfo.available_timezones())
    rest = [tz for tz in everything if tz not in common]
    return common + rest


@router.get("/preferences")
def preferences(request: Request, db: Session = Depends(get_db)):
    if (target := _guard(db, "preferences")):
        return deps.redirect(target)
    return _render_step(
        request,
        db,
        "preferences",
        timezones=_timezones(),
        selected_tz=settings_store.get_timezone(db),
    )


@router.post("/preferences")
def preferences_submit(
    request: Request,
    timezone: str = Form(...),
    date_format: str = Form("YYYY-MM-DD"),
    time_format: str = Form("24h"),
    week_start: str = Form("monday"),
    db: Session = Depends(get_db),
):
    if timezone not in zoneinfo.available_timezones():
        return _render_step(
            request,
            db,
            "preferences",
            errors=[f"{timezone!r} is not a recognised timezone."],
            timezones=_timezones(),
            selected_tz=settings_store.get_timezone(db),
        )

    settings_store.set_many(
        db,
        {
            settings_store.TIMEZONE: timezone,
            settings_store.DATE_FORMAT: date_format,
            settings_store.TIME_FORMAT: time_format,
            settings_store.WEEK_START: week_start,
        },
    )
    db.commit()
    _advance(db, "radicale")
    return deps.redirect("/setup/radicale")


# --- 4. Radicale credentials --------------------------------------------------


@router.get("/radicale")
def radicale_step(request: Request, db: Session = Depends(get_db)):
    if (target := _guard(db, "radicale")):
        return deps.redirect(target)
    suggested = settings_store.get(db, settings_store.RADICALE_USERNAME) or ""
    if not suggested:
        user = db.execute(select(User)).scalars().first()
        suggested = user.username if user else ""
    return _render_step(request, db, "radicale", radicale_username=suggested)


@router.post("/radicale")
def radicale_submit(
    request: Request,
    radicale_username: str = Form(...),
    radicale_password: str = Form(...),
    radicale_password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.crypto import encrypt_json

    radicale_username = radicale_username.strip()
    errors: list[str] = []

    if not USERNAME_RE.match(radicale_username):
        errors.append(
            "The CalDAV username must be 3-64 characters, using only letters, "
            "numbers, dots, dashes and underscores."
        )
    if len(radicale_password) < MIN_PASSWORD_LENGTH:
        errors.append(
            f"The CalDAV password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if radicale_password != radicale_password_confirm:
        errors.append("The two CalDAV passwords do not match.")

    if errors:
        return _render_step(
            request, db, "radicale",
            errors=errors, radicale_username=radicale_username,
        )

    try:
        password_hash = hash_password(radicale_password)
    except ValueError as exc:
        return _render_step(
            request, db, "radicale",
            errors=[str(exc)], radicale_username=radicale_username,
        )

    set_radicale_user(radicale_username, password_hash)
    settings_store.set_value(db, settings_store.RADICALE_USERNAME, radicale_username)
    settings_store.set_value(
        db, RADICALE_PASSWORD_KEY, encrypt_json({"password": radicale_password})
    )
    db.commit()

    # Prove the credentials work now rather than letting the failure surface
    # later as an unexplained empty task list.
    client = RadicaleClient(radicale_username, radicale_password)
    try:
        client.check_connection()
    except CalDAVError as exc:
        return _render_step(
            request, db, "radicale",
            errors=[f"Saved, but connecting to Radicale failed: {exc}"],
            radicale_username=radicale_username,
        )

    _advance(db, "collections")
    return deps.redirect("/setup/collections")


# --- 5. Initial collections ---------------------------------------------------


@router.get("/collections")
def collections_step(request: Request, db: Session = Depends(get_db)):
    if (target := _guard(db, "collections")):
        return deps.redirect(target)
    return _render_step(
        request, db, "collections",
        task_list_name="Tasks", calendar_name="Calendar",
    )


@router.post("/collections")
def collections_submit(
    request: Request,
    task_list_name: str = Form("Tasks"),
    calendar_name: str = Form("Calendar"),
    db: Session = Depends(get_db),
):
    from app.web.radicale_admin import get_radicale_client

    task_list_name = task_list_name.strip() or "Tasks"
    calendar_name = calendar_name.strip() or "Calendar"

    client = get_radicale_client(db)
    if client is None:
        return _render_step(
            request, db, "collections",
            errors=["The Radicale credentials are missing. Please redo the previous step."],
            task_list_name=task_list_name, calendar_name=calendar_name,
        )

    username = settings_store.get(db, settings_store.RADICALE_USERNAME) or ""
    wanted = [
        (task_list_name, CollectionKind.TASKS, "#2563eb"),
        (calendar_name, CollectionKind.CALENDAR, "#7c3aed"),
    ]

    try:
        existing = {c.collection_id for c in client.list_collections()}
    except CalDAVError as exc:
        return _render_step(
            request, db, "collections",
            errors=[str(exc)],
            task_list_name=task_list_name, calendar_name=calendar_name,
        )

    for display_name, kind, colour in wanted:
        collection_id = slugify_collection_id(display_name)
        # Re-running this step must not fail on collections already created.
        if collection_id not in existing:
            try:
                client.create_collection(collection_id, display_name, kind, colour)
            except CalDAVError as exc:
                return _render_step(
                    request, db, "collections",
                    errors=[f"Could not create {display_name!r}: {exc}"],
                    task_list_name=task_list_name, calendar_name=calendar_name,
                )

        known = db.execute(
            select(RadicaleCollection).where(
                RadicaleCollection.radicale_user == username,
                RadicaleCollection.collection_id == collection_id,
            )
        ).scalar_one_or_none()
        if known is None:
            db.add(
                RadicaleCollection(
                    radicale_user=username,
                    collection_id=collection_id,
                    display_name=display_name,
                    kind=kind,
                    colour=colour,
                )
            )
    db.commit()

    _advance(db, "sync")
    return deps.redirect("/setup/sync")


# --- 6. Sync interval, then finish -------------------------------------------


@router.get("/sync")
def sync_step(request: Request, db: Session = Depends(get_db)):
    if (target := _guard(db, "sync")):
        return deps.redirect(target)
    return _render_step(
        request, db, "sync",
        interval=settings_store.get_sync_interval(db),
        minimum=settings_store.MIN_SYNC_INTERVAL_MINUTES,
    )


@router.post("/sync")
def sync_submit(
    request: Request,
    interval_minutes: str = Form("15"),
    sync_enabled: str = Form("1"),
    db: Session = Depends(get_db),
):
    minimum = settings_store.MIN_SYNC_INTERVAL_MINUTES
    try:
        requested = int(interval_minutes)
    except ValueError:
        return _render_step(
            request, db, "sync",
            errors=["Enter the sync interval as a whole number of minutes."],
            interval=settings_store.get_sync_interval(db), minimum=minimum,
        )

    if requested < minimum:
        # Below this the upstream APIs start returning HTTP 429, and the
        # resulting backoff makes syncing slower than a polite interval would.
        return _render_step(
            request, db, "sync",
            errors=[
                f"The sync interval cannot be shorter than {minimum} minutes. "
                "Syncing more often than that gets Task Hub rate-limited by the "
                "services, which makes syncing slower rather than faster."
            ],
            interval=requested, minimum=minimum,
        )

    settings_store.set_sync_interval(db, requested)
    settings_store.set_bool(db, settings_store.SYNC_ENABLED, sync_enabled == "1")

    # Everything needed is now in place; the application opens up.
    settings_store.set_bool(db, settings_store.ONBOARDING_COMPLETE, True)
    settings_store.set_value(db, SETUP_STEP_KEY, "done")
    db.commit()

    deps.flash(
        request,
        "Setup complete. Connect your first service to start syncing.",
        "success",
    )
    return deps.redirect("/")
