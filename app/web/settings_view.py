"""Application settings: region, sync cadence, appearance, backups and the password."""

from __future__ import annotations

import datetime as dt
import json
import logging
import tempfile
import zoneinfo
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.crypto import decrypt_json, encrypt_json, hash_password, verify_password
from app.db import settings_store
from app.db.session import get_db
from app.services import mail_providers
from app.web import backup, deps, maintenance
from app.web.setup import MIN_PASSWORD_LENGTH, _timezones

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings")

#: Refused above this size. A real backup of a large installation is tens of
#: megabytes; anything approaching a gigabyte is a mistaken upload, and finding
#: that out after filling the disk of a Raspberry Pi is not the way to find out.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


@router.get("")
@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    # Only scanned when the panel is going to be shown. The queries are cheap,
    # but there is no reason to run them for someone who cannot see the result.
    findings = maintenance.scan(db) if settings_store.is_advanced(db) else []
    from app.services.tunnel import manager as tunnel_manager

    return deps.render(
        request, db, "settings.html",
        findings=findings,
        tunnel=tunnel_manager.status(),
        tunnel_available=tunnel_manager.available(),
        tunnel_has_token=bool(
            decrypt_json(settings_store.get(db, settings_store.TUNNEL_TOKEN)).get("token")
        ),
        timezones=_timezones(),
        minimum_interval=settings_store.MIN_SYNC_INTERVAL_MINUTES,
        sync_interval=settings_store.get_sync_interval(db),
        sync_enabled=settings_store.get_bool(db, settings_store.SYNC_ENABLED),
        backup_sizes={
            name: backup.human_size(size)
            for name, size in backup.size_breakdown().items()
        },
        restart_supported=backup.restart_supported(),
        rollback_bytes=backup.rollback_size(),
        rollback_size=backup.human_size(backup.rollback_size()),
        mail={
            "host": settings_store.get(db, settings_store.SMTP_HOST) or "",
            "port": settings_store.get(db, settings_store.SMTP_PORT) or "587",
            "security": settings_store.get(db, settings_store.SMTP_SECURITY) or "starttls",
            "username": settings_store.get(db, settings_store.SMTP_USERNAME) or "",
            "from_address": settings_store.get(db, settings_store.SMTP_FROM) or "",
            # Never the password itself, only whether one is saved, so the page
            # can say "leave blank to keep it" without putting a secret in HTML.
            "has_password": bool(
                decrypt_json(
                    settings_store.get(db, settings_store.SMTP_PASSWORD)
                ).get("password")
            ),
            # Where a test goes if the box is left as it is: the summary's own
            # recipient, so the button proves the thing that happens each
            # morning rather than something adjacent to it.
            "test_to": (
                (settings_store.get(db, settings_store.DIGEST_TO) or "").strip()
                or (settings_store.get(db, settings_store.SMTP_FROM) or "").strip()
            ),
            "last_test": _last_mail_test(db),
            "providers": mail_providers.PROVIDERS,
        },
        digest={
            "enabled": settings_store.get_bool(db, settings_store.DIGEST_ENABLED),
            "time": settings_store.get(db, settings_store.DIGEST_TIME) or "07:00",
            "to": settings_store.get(db, settings_store.DIGEST_TO) or "",
            "days": settings_store.digest_days(db),
            "day_codes": settings_store.DIGEST_DAY_CODES,
            "day_names": settings_store.DIGEST_DAY_NAMES,
            "days_phrase": _days_phrase(settings_store.digest_days(db)),
            "when_empty": settings_store.get_bool(db, settings_store.DIGEST_WHEN_EMPTY),
            "next_run": _digest_next_run(),
        },
    )


def _digest_next_run():
    from app.sync.scheduler import digest_next_run_time

    return digest_next_run_time()


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")


def _days_phrase(days: list[str]) -> str:
    """The chosen days as a sentence: "every day", "weekdays", or a list."""
    chosen = [code for code in settings_store.DIGEST_DAY_CODES if code in set(days)]
    if len(chosen) == len(settings_store.DIGEST_DAY_CODES):
        return "every day"
    if chosen == list(WEEKDAYS):
        return "Monday to Friday"
    names = [settings_store.DIGEST_DAY_NAMES[code] for code in chosen]
    if len(names) == 1:
        return f"every {names[0]}"
    return " and ".join([", ".join(names[:-1]), names[-1]])


@router.post("/general")
def save_general(
    request: Request,
    timezone: str = Form(...),
    date_format: str = Form("YYYY-MM-DD"),
    time_format: str = Form("24h"),
    week_start: str = Form("monday"),
    theme: str = Form("system"),
    advanced_mode: str = Form(""),
    base_url_override: str = Form(""),
    db: Session = Depends(get_db),
):
    if timezone not in zoneinfo.available_timezones():
        deps.flash(request, f"{timezone!r} is not a recognised timezone.", "error")
        return deps.redirect("/settings")

    override = base_url_override.strip().rstrip("/")
    if override and not override.startswith(("http://", "https://")):
        deps.flash(
            request,
            "The address must start with http:// or https://",
            "error",
        )
        return deps.redirect("/settings")

    settings_store.set_many(
        db,
        {
            settings_store.TIMEZONE: timezone,
            settings_store.DATE_FORMAT: date_format,
            settings_store.TIME_FORMAT: time_format,
            settings_store.WEEK_START: week_start,
            settings_store.THEME: theme,
            settings_store.ADVANCED_MODE: "1" if advanced_mode == "1" else "0",
            settings_store.BASE_URL_OVERRIDE: override,
        },
    )
    db.commit()
    deps.flash(request, "Settings saved.", "success")
    return deps.redirect("/settings")


@router.post("/sync")
def save_sync(
    request: Request,
    interval_minutes: str = Form("15"),
    sync_enabled: str = Form(""),
    db: Session = Depends(get_db),
):
    minimum = settings_store.MIN_SYNC_INTERVAL_MINUTES
    try:
        requested = int(interval_minutes)
    except ValueError:
        deps.flash(request, "Enter the interval as a whole number of minutes.", "error")
        return deps.redirect("/settings")

    if requested < minimum:
        deps.flash(
            request,
            f"The sync interval cannot be shorter than {minimum} minutes. Syncing "
            "more often gets Task Hub rate-limited, which makes it slower overall.",
            "error",
        )
        return deps.redirect("/settings")

    stored = settings_store.set_sync_interval(db, requested)
    settings_store.set_bool(db, settings_store.SYNC_ENABLED, sync_enabled == "1")
    db.commit()

    # Apply immediately rather than at the next restart, so the setting the user
    # just saved is the one actually running.
    from app.sync.scheduler import reschedule

    reschedule()

    deps.flash(request, f"Syncing every {stored} minutes.", "success")
    return deps.redirect("/settings")


@router.post("/repair/{key}")
def apply_repair(key: str, request: Request, db: Session = Depends(get_db)):
    """Run one named repair.

    Gated on advanced mode server-side as well as in the template: hiding a
    button is a courtesy, not a control, and these delete rows.
    """
    if not settings_store.is_advanced(db):
        deps.flash(request, "Turn on Advanced mode to use the repair tools.", "error")
        return deps.redirect("/settings")

    entry = maintenance.CHECKS.get(key)
    if entry is None:
        deps.flash(request, "Unknown repair.", "error")
        return deps.redirect("/settings")

    removed = maintenance.repair(db, key)
    if removed:
        deps.flash(request, f"Removed {removed} row(s): {entry[0].lower()}.", "success")
    else:
        deps.flash(request, "Nothing to remove — it had already been dealt with.", "info")
    return deps.redirect("/settings")


# --- Remote access -----------------------------------------------------------


def apply_tunnel_settings() -> None:
    """Bring the tunnel into line with what is stored. Safe to call any time."""
    from app.db.session import session_scope
    from app.services.tunnel import manager

    with session_scope() as db:
        enabled = settings_store.get_bool(db, settings_store.TUNNEL_ENABLED)
        token = decrypt_json(settings_store.get(db, settings_store.TUNNEL_TOKEN)).get("token")
    manager.apply(enabled, token)


@router.post("/tunnel")
def save_tunnel(
    request: Request,
    tunnel_enabled: str = Form(""),
    tunnel_token: str = Form(""),
    db: Session = Depends(get_db),
):
    """Turn remote access on or off, and store the token."""
    from app.services.tunnel import looks_like_token, manager

    enabled = tunnel_enabled == "1"
    typed = tunnel_token.strip()

    stored = decrypt_json(settings_store.get(db, settings_store.TUNNEL_TOKEN)).get("token")
    # An empty box means "leave the saved token alone", so the field can stay
    # blank on the page rather than echoing a secret back into the HTML.
    token = typed or stored

    if enabled and not token:
        deps.flash(request, "Paste your Cloudflare tunnel token to switch this on.", "error")
        return deps.redirect("/settings")

    if typed:
        ok, problem = looks_like_token(typed)
        if not ok:
            deps.flash(request, problem, "error")
            return deps.redirect("/settings")
        settings_store.set_value(
            db, settings_store.TUNNEL_TOKEN, encrypt_json({"token": typed})
        )

    settings_store.set_bool(db, settings_store.TUNNEL_ENABLED, enabled)
    db.commit()

    manager.apply(enabled, token)
    deps.flash(
        request,
        "Remote access starting — it takes a few seconds to connect."
        if enabled else "Remote access switched off.",
        "success" if enabled else "info",
    )
    return deps.redirect("/settings")


@router.post("/tunnel/forget")
def forget_tunnel(request: Request, db: Session = Depends(get_db)):
    """Switch remote access off and delete the stored token."""
    from app.services.tunnel import manager

    settings_store.set_bool(db, settings_store.TUNNEL_ENABLED, False)
    settings_store.set_value(db, settings_store.TUNNEL_TOKEN, None)
    db.commit()
    manager.stop()
    deps.flash(request, "Remote access switched off and the token deleted.", "success")
    return deps.redirect("/settings")


@router.get("/tunnel/status")
def tunnel_status(db: Session = Depends(get_db)):
    """Live status, polled by the Settings page while it connects."""
    from fastapi.responses import JSONResponse

    from app.services.tunnel import manager

    status = manager.status()
    if status.healthy:
        summary = f"Connected — {status.connections} link(s) to Cloudflare"
    elif status.enabled and status.running:
        summary = "Connecting…"
    elif status.enabled:
        summary = status.last_error or "Not connected"
    else:
        summary = "Off"

    return JSONResponse(
        {
            "enabled": status.enabled,
            "running": status.running,
            "healthy": status.healthy,
            "connections": status.connections,
            "summary": summary,
            "error": status.last_error,
            "log": status.log[-12:],
        }
    )


@router.post("/password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    user = deps.get_current_user(request, db)
    if user is None:
        return deps.redirect("/login")

    if not verify_password(current_password, user.password_hash):
        deps.flash(request, "Your current password is not correct.", "error")
        return deps.redirect("/settings")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        deps.flash(
            request,
            f"The new password must be at least {MIN_PASSWORD_LENGTH} characters.",
            "error",
        )
        return deps.redirect("/settings")
    if new_password != new_password_confirm:
        deps.flash(request, "The two new passwords do not match.", "error")
        return deps.redirect("/settings")

    try:
        user.password_hash = hash_password(new_password)
    except ValueError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/settings")

    db.commit()
    deps.flash(request, "Password changed.", "success")
    return deps.redirect("/settings")


# --- Backup, restore and restart ----------------------------------------------
#
# These exist so that operating Task Hub never requires a terminal. Everything
# here was previously a docker command in the install guides, which meant that
# in practice backups did not happen -- and the thing not being backed up is
# the set of service logins, which cannot be recovered any other way.


@router.get("/backup/download")
def download_backup(request: Request, db: Session = Depends(get_db)):
    """Send the whole installation as a single file the browser saves.

    Written to a temporary file rather than streamed, because the archive has
    to be complete before it can be trusted: an error half way through a
    streamed response arrives as a truncated download that looks successful.
    The temporary file is deleted once the browser has it, whether or not the
    transfer finished.
    """
    handle = tempfile.NamedTemporaryFile(
        prefix="taskhub-backup-", suffix=".tar.gz", delete=False
    )
    handle.close()
    archive = Path(handle.name)

    try:
        backup.write_backup(archive)
    except Exception as exc:
        archive.unlink(missing_ok=True)
        logger.exception("Backup failed")
        deps.flash(request, f"The backup could not be created: {exc}", "error")
        return deps.redirect("/settings")

    return FileResponse(
        archive,
        media_type="application/gzip",
        filename=backup.suggested_filename(),
        background=BackgroundTask(archive.unlink, missing_ok=True),
    )


@router.post("/backup/restore")
async def restore_backup(
    request: Request,
    archive: UploadFile,
    confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    """Replace everything with the contents of an uploaded backup.

    This throws away the current installation, so it asks for the word RESTORE
    to be typed rather than relying on a button being clicked deliberately.
    What it replaces includes the encryption key, which means every service
    login afterwards is the one from the backup, not the one from now.
    """
    if confirm.strip().upper() != "RESTORE":
        deps.flash(
            request,
            "Type RESTORE in the confirmation box to replace your data. "
            "Nothing has been changed.",
            "error",
        )
        return deps.redirect("/settings")

    handle = tempfile.NamedTemporaryFile(
        prefix="taskhub-upload-", suffix=".tar.gz", delete=False
    )
    upload = Path(handle.name)
    written = 0
    try:
        while chunk := await archive.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                raise backup.BackupError(
                    "That file is larger than 2 GB, which no Task Hub backup "
                    "is. Nothing has been changed."
                )
            handle.write(chunk)
        handle.close()

        if written == 0:
            raise backup.BackupError("No file was chosen. Nothing has been changed.")

        # Checked before anything is touched, so an unusable archive costs the
        # user an error message rather than their installation.
        backup.inspect_archive(upload)
        backup.close_database()
        backup.restore_archive(upload)
    except backup.BackupError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/settings")
    except Exception as exc:
        logger.exception("Restore failed")
        deps.flash(
            request,
            f"The restore failed and your existing data has been left alone: {exc}",
            "error",
        )
        return deps.redirect("/settings")
    finally:
        handle.close()
        upload.unlink(missing_ok=True)

    # The database on disk is now the restored one, but this process is still
    # holding the old one's state, so it has to start again to see it.
    backup.request_restart()
    return deps.render(
        request, db, "restarting.html",
        heading="Restoring your backup",
        detail=(
            "Your data has been replaced and Task Hub is restarting to load it. "
            "You will need to sign in with the password from the backup, not the "
            "one you were using a moment ago."
        ),
    )


@router.post("/backup/discard-rollback")
def discard_rollback(request: Request, db: Session = Depends(get_db)):
    """Delete the copy of the data that a previous restore set aside."""
    removed = backup.discard_rollbacks()
    if removed:
        deps.flash(request, "The data set aside by the last restore has been deleted.", "success")
    else:
        deps.flash(request, "There was nothing set aside to delete.", "info")
    return deps.redirect("/settings")


@router.post("/restart")
def restart(request: Request, db: Session = Depends(get_db)):
    """Stop the application so that Docker starts it again.

    A container has no other way to restart itself. Outside Docker this would
    simply stop, so the button is not offered there and the request is refused
    if it arrives anyway.
    """
    if not backup.restart_supported():
        deps.flash(
            request,
            "Restarting from this page only works when Task Hub is running "
            "under Docker, because it works by stopping and letting Docker "
            "start it again. Outside Docker it would just stop.",
            "error",
        )
        return deps.redirect("/settings")

    backup.request_restart()
    return deps.render(
        request, db, "restarting.html",
        heading="Restarting Task Hub",
        detail="This normally takes about fifteen seconds.",
    )


# --- Outgoing mail and the daily summary --------------------------------------


@router.post("/mail")
def save_mail(
    request: Request,
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_security: str = Form("starttls"),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    db: Session = Depends(get_db),
):
    """Store the mail server details.

    The password box empty means "keep the saved one", exactly as the tunnel
    token does, so the page never has to echo a secret back into its own HTML
    to let somebody change the port.
    """
    from app.services.mailer import SECURITIES

    security = smtp_security.strip().lower()
    if security not in SECURITIES:
        security = "starttls"

    try:
        port = int(smtp_port.strip() or "587")
    except ValueError:
        deps.flash(request, "The port has to be a number, usually 587 or 465.", "error")
        return deps.redirect("/settings")
    if not 1 <= port <= 65535:
        deps.flash(request, "That is not a usable port number.", "error")
        return deps.redirect("/settings")

    from app.services.mail_providers import correct_host

    host, correction = correct_host(smtp_host)

    settings_store.set_value(db, settings_store.SMTP_HOST, host)
    settings_store.set_value(db, settings_store.SMTP_PORT, str(port))
    settings_store.set_value(db, settings_store.SMTP_SECURITY, security)
    settings_store.set_value(db, settings_store.SMTP_USERNAME, smtp_username.strip())
    settings_store.set_value(db, settings_store.SMTP_FROM, smtp_from.strip())
    if smtp_password.strip():
        settings_store.set_value(
            db, settings_store.SMTP_PASSWORD,
            encrypt_json({"password": mail_providers.clean_password(smtp_password)}),
        )
    db.commit()

    if correction:
        deps.flash(request, correction + " Send a test to check it.", "success")
    else:
        deps.flash(request, "Mail server saved. Send a test to check it.", "success")
    return deps.redirect("/settings")


@router.post("/mail/test")
def test_mail(
    request: Request,
    test_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Prove the settings work, and remember the answer.

    The address defaults to the summary's own recipient, so pressing the button
    without touching anything proves the thing that will actually happen every
    morning. The outcome is recorded and shown beside the button afterwards:
    a flash message is gone on the next page load, and "is my email working?"
    is a question people come back to the settings page to ask.
    """
    from app.services.digest import mail_settings
    from app.services.mailer import MailError, send

    to = test_to.strip() or (settings_store.get(db, settings_store.DIGEST_TO) or "").strip()
    to = to or (settings_store.get(db, settings_store.SMTP_FROM) or "").strip()
    if not to:
        deps.flash(request, "Type an address to send the test to.", "error")
        return deps.redirect("/settings")

    try:
        send(
            mail_settings(db), to,
            "Task Hub test message",
            "This is Task Hub checking that it can send you email.\n\n"
            "If you are reading it, the daily summary will arrive too.\n\n"
            "— Task Hub\n",
        )
    except MailError as exc:
        _record_mail_test(db, ok=False, to=to, detail=str(exc))
        deps.flash(request, str(exc), "error")
        return deps.redirect("/settings")

    _record_mail_test(db, ok=True, to=to, detail="")
    # "Sent" means the mail server accepted it, which is not the same as
    # delivered: a wrong-but-real domain accepts the message and bounces it
    # minutes later, long after this page has said it worked.
    warning = mail_providers.suggest_address(to)
    if warning:
        deps.flash(request, warning, "error")
    deps.flash(
        request,
        f"Test message sent to {to}. If it does not appear within a minute or "
        "two, check the spam folder — a sender that has never written to you "
        "before is exactly what a spam filter looks for.",
        "success",
    )
    return deps.redirect("/settings")


def _record_mail_test(db: Session, *, ok: bool, to: str, detail: str) -> None:
    """Remember how the last test went, so the page can say so."""
    settings_store.set_value(
        db, settings_store.SMTP_TEST_RESULT,
        json.dumps({
            "ok": ok,
            "to": to,
            "detail": detail,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }),
    )
    db.commit()


def _last_mail_test(db: Session) -> dict | None:
    raw = settings_store.get(db, settings_store.SMTP_TEST_RESULT) or ""
    if not raw:
        return None
    try:
        saved = json.loads(raw)
    except ValueError:
        return None
    when = saved.get("at", "")
    try:
        moment = dt.datetime.fromisoformat(when).astimezone(
            deps.resolve_tz(settings_store.get(db, settings_store.TIMEZONE))
        )
        when = moment.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        pass
    return {
        "ok": bool(saved.get("ok")),
        "to": saved.get("to", ""),
        "detail": saved.get("detail", ""),
        "when": when,
    }


@router.post("/digest")
def save_digest(
    request: Request,
    digest_enabled: str = Form(""),
    digest_time: str = Form("07:00"),
    digest_to: str = Form(""),
    digest_days: list[str] = Form(default=[]),
    digest_when_empty: str = Form(""),
    db: Session = Depends(get_db),
):
    """Turn the daily summary on or off, and choose when and how often."""
    from app.sync.scheduler import reschedule_digest

    enabled = digest_enabled == "1"
    when = digest_time.strip() or "07:00"
    try:
        hour, _, minute = when.partition(":")
        hour, minute = int(hour), int(minute or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        deps.flash(request, "Give the time as HH:MM, like 07:00.", "error")
        return deps.redirect("/settings")

    # Week order, whatever order the boxes were submitted in, and only real day
    # codes -- this string goes straight into a cron expression.
    days = [
        code for code in settings_store.DIGEST_DAY_CODES
        if code in {value.strip().lower() for value in digest_days}
    ]
    if enabled and not days:
        deps.flash(
            request,
            "Choose at least one day for the summary, or switch it off.",
            "error",
        )
        return deps.redirect("/settings")

    recipient = digest_to.strip()
    if enabled and not recipient:
        deps.flash(request, "Add an address to send the summary to.", "error")
        return deps.redirect("/settings")
    if enabled and not (settings_store.get(db, settings_store.SMTP_HOST) or "").strip():
        deps.flash(
            request,
            "Set up the mail server above before switching the summary on.",
            "error",
        )
        return deps.redirect("/settings")

    settings_store.set_bool(db, settings_store.DIGEST_ENABLED, enabled)
    settings_store.set_value(db, settings_store.DIGEST_TIME, f"{hour:02d}:{minute:02d}")
    settings_store.set_value(db, settings_store.DIGEST_TO, recipient)
    settings_store.set_value(
        db, settings_store.DIGEST_DAYS,
        ",".join(days or settings_store.DIGEST_DAY_CODES),
    )
    settings_store.set_bool(
        db, settings_store.DIGEST_WHEN_EMPTY, digest_when_empty == "1"
    )
    db.commit()

    reschedule_digest()
    warning = mail_providers.suggest_address(recipient)
    if warning:
        deps.flash(request, warning, "error")
    deps.flash(
        request,
        f"Daily summary {'on' if enabled else 'off'}."
        + (f" Sending at {hour:02d}:{minute:02d}, {_days_phrase(days)}."
           if enabled else ""),
        "success",
    )
    return deps.redirect("/settings")


@router.post("/digest/send")
def send_digest_now(request: Request, db: Session = Depends(get_db)):
    """Send today's summary immediately, even on a quiet day."""
    from app.services.digest import send_digest
    from app.services.mailer import MailError

    try:
        outcome = send_digest(db, force=True)
    except MailError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/settings")
    deps.flash(request, outcome, "success")
    return deps.redirect("/settings")
