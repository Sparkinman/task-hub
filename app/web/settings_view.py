"""Application settings: region, sync cadence, appearance, backups and the password."""

from __future__ import annotations

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
    )


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
