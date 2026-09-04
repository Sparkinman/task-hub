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
from app.services import mail_providers
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
    # Last, and skippable in one click. Nothing else in Task Hub depends on
    # email, and somebody who has just got their tasks syncing should not be
    # made to find SMTP settings before they can use it.
    Step("email", "Email", "Optional: a daily summary of what is due."),
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
    """Render one wizard step, with the shared wizard chrome around it.

    Every step goes through here so that the progress indicator, the step
    ordering and the error presentation are defined once. A step that rendered
    itself would drift from the others the first time one of them changed.
    """
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
    """Create the sign-in account, or explain why the details were refused.

    This is the only account Task Hub will ever have, and there is no password
    reset -- no mail server exists to send one through -- so the rules are
    stricter here than they would otherwise be and the wizard says so before
    the choice is made rather than after.
    """
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
    """Save the regional preferences.

    The timezone matters more than the formats: it decides what "due today"
    means, and getting it wrong makes every date look off by a day near
    midnight rather than obviously broken.
    """
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
    """Create the CalDAV account that external clients sign in with.

    Deliberately separate from the web password. It is typed into phones,
    stored by them, and sent on every sync, so it lives a very different life
    from the one used to open this page -- and one being exposed should not
    give away the other.
    """
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
    """Create the first collections, so there is somewhere for tasks to land.

    A Task Hub with no collections is not usable, and asking someone to work
    out that they need to create one before connecting anything is a poor
    first five minutes. Two sensible defaults are offered and can be renamed
    later.
    """
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
    _advance(db, "email")
    db.commit()
    return deps.redirect("/setup/email")


def _finish(request: Request, db: Session):
    """Close the wizard, or send the user back for a sign-in account.

    Refuse to finish without an account to sign in with. Marking setup complete
    closes the wizard for good, and there is no password reset, so completing it
    with no user would lock everybody out of an installation permanently, with
    no way back in short of deleting the data volume. Following the wizard
    normally cannot reach this state, because a rejected account step re-renders
    rather than advancing; a reload, a stale form or a resubmitted step can.
    """
    if db.execute(select(User)).scalars().first() is None:
        return _render_step(
            request, db, "account",
            errors=[
                "No sign-in account has been created yet, and setup cannot "
                "finish without one -- there is no way to add it afterwards. "
                "Please choose a username and password."
            ],
        )

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


def _email_context(db: Session) -> dict:
    """Whatever is already saved, so a re-render never empties the form."""
    from app.crypto import decrypt_json

    return {
        "mail": {
            "host": settings_store.get(db, settings_store.SMTP_HOST) or "",
            "port": settings_store.get(db, settings_store.SMTP_PORT) or "587",
            "security": settings_store.get(db, settings_store.SMTP_SECURITY) or "starttls",
            "username": settings_store.get(db, settings_store.SMTP_USERNAME) or "",
            "from_address": settings_store.get(db, settings_store.SMTP_FROM) or "",
            "has_password": bool(
                decrypt_json(
                    settings_store.get(db, settings_store.SMTP_PASSWORD)
                ).get("password")
            ),
            "providers": mail_providers.PROVIDERS,
        },
        "digest": {
            "to": settings_store.get(db, settings_store.DIGEST_TO) or "",
            "time": settings_store.get(db, settings_store.DIGEST_TIME) or "07:00",
            "days": settings_store.digest_days(db),
            "day_codes": settings_store.DIGEST_DAY_CODES,
            "day_names": settings_store.DIGEST_DAY_NAMES,
            "when_empty": settings_store.get_bool(db, settings_store.DIGEST_WHEN_EMPTY),
        },
    }


@router.get("/email")
def email_step(request: Request, db: Session = Depends(get_db)):
    if (target := _guard(db, "email")):
        return deps.redirect(target)
    return _render_step(request, db, "email", **_email_context(db))


@router.post("/email")
def email_submit(
    request: Request,
    skip: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_security: str = Form("starttls"),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    digest_to: str = Form(""),
    digest_time: str = Form("07:00"),
    digest_days: list[str] = Form(default=[]),
    digest_when_empty: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save the mail server and summary, or skip the whole step.

    Skipping is a first-class outcome, not a failure: email is the one part of
    Task Hub that needs an account somewhere else, and making somebody go and
    find SMTP settings before they can use what they just installed is how a
    setup gets abandoned. Everything here is on the Settings page afterwards.
    """
    from app.crypto import encrypt_json
    from app.services.digest import mail_settings
    from app.services.mailer import MailError, send

    if skip == "1":
        return _finish(request, db)

    host = smtp_host.strip()
    sender = smtp_from.strip()
    if not host and not sender and not digest_to.strip():
        # An empty form and "Save" pressed: they meant to skip.
        return _finish(request, db)

    errors: list[str] = []
    if not host:
        errors.append("Enter the mail server, or press Skip to do this later.")
    if not sender:
        errors.append("Enter the address the summary should come from.")

    try:
        port = int(smtp_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        port = 587
        errors.append("The port must be a number between 1 and 65535.")

    when = digest_time.strip() or "07:00"
    try:
        hour, _, minute = when.partition(":")
        hour, minute = int(hour), int(minute or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        hour, minute = 7, 0
        errors.append("Give the time as HH:MM, like 07:00.")

    if errors:
        return _render_step(request, db, "email", errors=errors, **_email_context(db))

    settings_store.set_value(db, settings_store.SMTP_HOST, host)
    settings_store.set_value(db, settings_store.SMTP_PORT, str(port))
    settings_store.set_value(db, settings_store.SMTP_SECURITY, smtp_security)
    settings_store.set_value(db, settings_store.SMTP_USERNAME, smtp_username.strip())
    if smtp_password:
        settings_store.set_value(
            db, settings_store.SMTP_PASSWORD, encrypt_json({"password": smtp_password})
        )
    settings_store.set_value(db, settings_store.SMTP_FROM, sender)

    recipient = digest_to.strip()
    days = [
        code for code in settings_store.DIGEST_DAY_CODES
        if code in {value.strip().lower() for value in digest_days}
    ]
    settings_store.set_value(db, settings_store.DIGEST_TIME, f"{hour:02d}:{minute:02d}")
    settings_store.set_value(db, settings_store.DIGEST_TO, recipient)
    settings_store.set_value(
        db, settings_store.DIGEST_DAYS,
        ",".join(days or settings_store.DIGEST_DAY_CODES),
    )
    settings_store.set_bool(
        db, settings_store.DIGEST_WHEN_EMPTY, digest_when_empty == "1"
    )
    settings_store.set_bool(db, settings_store.DIGEST_ENABLED, bool(recipient))
    db.commit()

    # Prove it now rather than on the first morning it fails to arrive. A
    # failure re-renders this step with the mail server's own complaint, and
    # Skip is still right there.
    try:
        send(
            mail_settings(db), recipient or sender,
            "Task Hub test message",
            "This is Task Hub checking that it can send you email.\n\n"
            "If you are reading it, the daily summary will arrive too.\n\n"
            "— Task Hub\n",
        )
    except MailError as exc:
        settings_store.set_bool(db, settings_store.DIGEST_ENABLED, False)
        db.commit()
        return _render_step(
            request, db, "email",
            errors=[
                str(exc),
                "Your settings have been saved. Fix them and try again, or "
                "press Skip and finish it later under Settings → Email.",
            ],
            **_email_context(db),
        )

    from app.sync.scheduler import reschedule_digest

    reschedule_digest()
    warning = mail_providers.suggest_address(recipient)
    if warning:
        deps.flash(request, warning, "error")
    deps.flash(request, f"Test message sent to {recipient or sender}.", "success")
    return _finish(request, db)
