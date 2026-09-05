"""Backing up chosen Supernote folders as PDFs Task Hub can show.

This is a backup, not a sync. The folders it reads are never written to and
nothing here travels back to the tablet: the only thing Task Hub does is ask
Supernote to render a notebook and keep the result. That asymmetry is
deliberate and worth preserving -- a bug in a backup loses nothing, a bug in a
write loses somebody's notes.

It runs on its own clock, far slower than the task sync, and every decision in
this module is about not being a nuisance on somebody else's server:

* **Only changed notes are converted.** Supernote reports an md5 for every
  file, so a notebook opened but not written to is recognised and skipped.
  Rendering is done by Ratta's machines, at Ratta's expense, on an API they
  never published; re-rendering unchanged notebooks on a timer is the surest
  way to have that access taken away.
* **One request per note.** ``pageNoList: []`` converts the whole notebook, so
  there is no separate call to count pages first.
* **A pause between conversions**, and a cap on how many run in one pass, so a
  first backup of a large account spreads over several runs rather than
  arriving as a burst.
* **The floor on the interval is thirty minutes**, enforced where the schedule
  is read rather than trusted to the form.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.connectors.base import ConnectorAuthError, ConnectorError
from app.crypto import decrypt_json
from app.db import settings_store
from app.db.models import Account, AccountStatus, ServiceKind, SupernoteNote, utcnow
from app.db.session import session_scope
from app.services.pdf_thumbnail import thumbnail
from app.services.supernote_files import SupernoteFiles

logger = logging.getLogger(__name__)

#: Where the PDFs live. Inside the data directory, so the existing backup and
#: restore in app.web.backup carries them without being told about them.
NOTES_DIR = DATA_DIR / "notes"

#: Seconds between conversions. Not a rate limit anyone imposed -- a courtesy,
#: because each one is real work on hardware we do not pay for.
PAUSE_BETWEEN_CONVERSIONS = 2.0

#: What Supernote says when it has accepted a notebook but not finished with
#: it. Large planners come back this way: the conversion is queued and the file
#: appears on a later pass. It is recorded like a failure so the note is
#: retried, but it is not one, and the pages say so rather than showing a
#: perfectly healthy notebook as broken.
PENDING_PHRASES = ("being converted", "is converting")


def is_pending(note: SupernoteNote) -> bool:
    """Whether this note is queued at Supernote rather than failed."""
    message = (note.error or "").lower()
    return any(phrase in message for phrase in PENDING_PHRASES)


#: Most notebooks converted in a single pass. A first backup of a large account
#: therefore spreads over several runs instead of arriving as one long burst,
#: and a failure mid-way costs one pass rather than the lot.
MAX_PER_RUN = 25


class BackupResult:
    """What one pass did, for the page and the log."""

    def __init__(self) -> None:
        self.checked = 0
        self.converted = 0
        self.unchanged = 0
        self.removed = 0
        self.deferred = 0
        self.excluded = 0
        self.errors: list[str] = []

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"BackupResult(checked={self.checked}, converted={self.converted}, "
            f"unchanged={self.unchanged}, removed={self.removed}, "
            f"deferred={self.deferred}, excluded={self.excluded}, "
            f"errors={len(self.errors)})"
        )


# --- Settings -----------------------------------------------------------------

def selected_folders(session: Session) -> list[str]:
    """The folder ids the user ticked, as a list."""
    raw = settings_store.get(session, settings_store.SUPERNOTE_BACKUP_FOLDERS) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def set_selected_folders(session: Session, folder_ids: list[str]) -> None:
    cleaned = sorted({str(f).strip() for f in folder_ids if str(f).strip()})
    settings_store.set_value(session, settings_store.SUPERNOTE_BACKUP_FOLDERS, ",".join(cleaned))


def backup_interval(session: Session) -> int:
    """Minutes between passes, never below the floor.

    Clamped here rather than only in the form, so a value written directly into
    the database -- or left behind by an older version -- still cannot turn this
    into something that hammers Supernote.
    """
    raw = settings_store.get(session, settings_store.SUPERNOTE_BACKUP_INTERVAL_MINUTES)
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        minutes = settings_store.DEFAULT_NOTE_BACKUP_INTERVAL_MINUTES
    return max(minutes, settings_store.MIN_NOTE_BACKUP_INTERVAL_MINUTES)


def backup_enabled(session: Session) -> bool:
    return settings_store.get_bool(session, settings_store.SUPERNOTE_BACKUP_ENABLED)


# --- Storage ------------------------------------------------------------------

def pdf_path(note: SupernoteNote):
    """Where one note's PDF lives on disk.

    Named from the note's id rather than its title. A notebook can be called
    anything at all, including things that are not safe as a file name, and a
    name that changes on the tablet must not orphan the file it refers to.
    """
    return NOTES_DIR / (note.pdf_name or f"{note.note_id}.pdf")


def _store_pdf(note_id: str, content: bytes) -> tuple[str, int]:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{note_id}.pdf"
    target = NOTES_DIR / name
    # Written beside and moved into place, so an interrupted download cannot
    # leave a half-written PDF that a reader will refuse to open.
    staging = target.with_suffix(".part")
    staging.write_bytes(content)
    staging.replace(target)
    return name, len(content)


def thumb_path(note: SupernoteNote):
    """Where one note's preview image lives, or None if it has none."""
    return NOTES_DIR / note.thumb_name if note.thumb_name else None


def _make_thumbnail(note_id: str, pdf: bytes) -> tuple[str, int] | None:
    """Draw the preview from a PDF already in hand.

    Costs Supernote nothing: the picture comes out of the file Task Hub just
    downloaded, not from a second render on their servers.
    """
    png = thumbnail(pdf)
    if not png:
        return None
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{note_id}.thumb.png"
    (NOTES_DIR / name).write_bytes(png)
    return name, len(png)


def fill_missing_thumbnails() -> int:
    """Draw previews for notebooks backed up before previews existed.

    Reads the stored PDFs and touches the network not at all, so it is safe to
    run at the end of every pass however often that is.
    """
    made = 0
    with session_scope() as session:
        rows = session.execute(
            select(SupernoteNote).where(
                SupernoteNote.pdf_name.isnot(None),
                SupernoteNote.thumb_name.is_(None),
                SupernoteNote.excluded.is_(False),
            )
        ).scalars().all()
        for row in rows:
            path = pdf_path(row)
            if not path.exists():
                continue
            try:
                result = _make_thumbnail(row.note_id, path.read_bytes())
            except OSError:
                continue
            if result:
                row.thumb_name = result[0]
                made += 1
        session.commit()
    return made


def _discard_pdf(note: SupernoteNote) -> None:
    try:
        for path in (pdf_path(note), thumb_path(note)):
            if path is not None and path.exists():
                path.unlink()
    except OSError as exc:  # pragma: no cover - filesystem edge
        logger.warning("Could not remove %s: %s", note.pdf_name, exc)


# --- The pass -----------------------------------------------------------------

def supernote_account(session: Session) -> Account | None:
    return session.execute(
        select(Account).where(
            Account.service == ServiceKind.SUPERNOTE,
            Account.enabled.is_(True),
        ).order_by(Account.slot)
    ).scalars().first()


def run_backup(force: bool = False) -> BackupResult:
    """One pass: walk the chosen folders, convert what changed, tidy up.

    ``force`` only bypasses the enabled switch, for the "Back up now" button.
    It never bypasses the md5 check, because that check is what keeps this
    polite -- a button that reconverted everything would be one bored click
    away from a very rude afternoon.
    """
    result = BackupResult()

    with session_scope() as session:
        if not force and not backup_enabled(session):
            return result
        account = supernote_account(session)
        if account is None or account.status == AccountStatus.NEEDS_AUTH:
            result.errors.append(
                "No connected Supernote account, so there is nothing to back up."
            )
            return result
        folders = selected_folders(session)
        if not folders:
            return result
        token = (decrypt_json(account.credentials) or {}).get("token") or ""
        account_id = account.id

    try:
        files = SupernoteFiles(token)
    except ConnectorAuthError as exc:
        result.errors.append(str(exc))
        return result

    try:
        wanted: dict[str, tuple] = {}
        for folder_id in folders:
            try:
                for entry, path in files.notes_under(folder_id):
                    wanted[entry.id] = (entry, path, folder_id)
            except ConnectorAuthError as exc:
                result.errors.append(str(exc))
                _mark_needs_auth(account_id)
                return result
            except ConnectorError as exc:
                result.errors.append(f"Could not read a chosen folder: {exc}")

        result.checked = len(wanted)
        _forget_unwanted(account_id, set(wanted), result)

        converted = 0
        for note_id, (entry, path, folder_id) in sorted(
            wanted.items(), key=lambda kv: kv[1][0].updated_at, reverse=True
        ):
            if converted >= MAX_PER_RUN:
                result.deferred += 1
                continue
            if _is_excluded(account_id, entry.id):
                # Deleted from Task Hub on purpose. Still in a chosen folder, so
                # it is seen every pass -- and must be left alone every pass.
                result.excluded += 1
                continue
            if _is_current(account_id, entry):
                result.unchanged += 1
                _refresh_placement(account_id, entry, path, folder_id)
                continue

            try:
                content = files.note_pdf(entry.id)
            except ConnectorAuthError as exc:
                result.errors.append(str(exc))
                _mark_needs_auth(account_id)
                break
            except ConnectorError as exc:
                # One unconvertible notebook must not stop the rest. The reason
                # is kept against the row so the page can say why it is missing
                # instead of quietly never showing it.
                _record_failure(account_id, entry, path, folder_id, str(exc))
                result.errors.append(f"{entry.name}: {exc}")
                continue

            name, size = _store_pdf(entry.id, content)
            preview = _make_thumbnail(entry.id, content)
            _record_success(account_id, entry, path, folder_id, name, size,
                            preview[0] if preview else None)
            converted += 1
            result.converted += 1
            time.sleep(PAUSE_BETWEEN_CONVERSIONS)
    finally:
        files.close()

    # Costs nothing and no network: fills in previews for anything backed up
    # before they existed, or where the draw failed last time.
    fill_missing_thumbnails()

    # Digests ride this schedule rather than having a clock of their own: they
    # come from the same account over one cheap request, and a third timer
    # would be a third thing to explain.
    try:
        from app.sync.digest_sync import run_sync as sync_digests

        digests = sync_digests()
        if digests.added or digests.updated or digests.removed:
            logger.info("Supernote digests: %r", digests)
    except Exception:  # noqa: BLE001 - never fatal to a backup
        logger.debug("Digest sync failed", exc_info=True)

    notify_expiring_sessions()

    _remember_run(result)
    return result


# --- Database bookkeeping -----------------------------------------------------

def _row_for(session: Session, account_id: int, note_id: str) -> SupernoteNote | None:
    return session.execute(
        select(SupernoteNote).where(
            SupernoteNote.account_id == account_id,
            SupernoteNote.note_id == note_id,
        )
    ).scalar_one_or_none()


def _is_current(account_id: int, entry) -> bool:
    """Whether the stored PDF was made from exactly this version of the note."""
    with session_scope() as session:
        row = _row_for(session, account_id, entry.id)
        if row is None or not row.pdf_name or row.error:
            return False
        if entry.md5 and row.source_md5 != entry.md5:
            return False
        if not entry.md5 and row.source_updated_at:
            # No md5 to compare -- fall back to the timestamp rather than
            # assuming unchanged, because assuming wrongly means a stale PDF
            # forever.
            stored = int(row.source_updated_at.timestamp() * 1000)
            if entry.updated_at > stored:
                return False
        return pdf_path(row).exists()


def _is_excluded(account_id: int, note_id: str) -> bool:
    with session_scope() as session:
        row = _row_for(session, account_id, note_id)
        return bool(row and row.excluded)


def remove_copy(session: Session, row: SupernoteNote) -> None:
    """Delete Task Hub's copy of one notebook and remember the decision.

    The row survives with ``excluded`` set. Deleting it outright would leave no
    trace of the choice, and the notebook is still in a folder marked for
    backup, so the next pass would download it again -- which reads as a delete
    button that does not work.
    """
    _discard_pdf(row)
    row.pdf_name = None
    row.thumb_name = None
    row.pdf_size = 0
    row.converted_at = None
    row.source_md5 = ""
    row.error = None
    row.excluded = True


def restore_copy(session: Session, row: SupernoteNote) -> None:
    """Allow a previously deleted notebook to be fetched again."""
    row.excluded = False
    row.source_md5 = ""  # Forces the next pass to convert it.


def _refresh_placement(account_id: int, entry, path: str, folder_id: str) -> None:
    """Keep the name and folder current without reconverting."""
    with session_scope() as session:
        row = _row_for(session, account_id, entry.id)
        if row is None:
            return
        row.name = entry.stem
        row.folder_path = path
        row.root_folder_id = folder_id
        session.commit()


def _record_success(account_id, entry, path, folder_id, pdf_name, pdf_size,
                    thumb_name=None) -> None:
    with session_scope() as session:
        row = _row_for(session, account_id, entry.id)
        if row is None:
            row = SupernoteNote(account_id=account_id, note_id=entry.id)
            session.add(row)
        row.root_folder_id = folder_id
        row.name = entry.stem
        row.folder_path = path
        row.source_md5 = entry.md5
        row.source_size = entry.size
        row.source_updated_at = (
            dt.datetime.fromtimestamp(entry.updated_at / 1000, dt.UTC)
            if entry.updated_at else None
        )
        row.pdf_name = pdf_name
        row.pdf_size = pdf_size
        row.thumb_name = thumb_name
        row.converted_at = utcnow()
        row.error = None
        session.commit()


def _record_failure(account_id, entry, path, folder_id, message) -> None:
    with session_scope() as session:
        row = _row_for(session, account_id, entry.id)
        if row is None:
            row = SupernoteNote(account_id=account_id, note_id=entry.id)
            session.add(row)
        row.root_folder_id = folder_id
        row.name = entry.stem
        row.folder_path = path
        row.error = message[:500]
        session.commit()


def _forget_unwanted(account_id: int, keep: set[str], result: BackupResult) -> None:
    """Drop notes that are no longer in any chosen folder, and their PDFs."""
    with session_scope() as session:
        rows = session.execute(
            select(SupernoteNote).where(SupernoteNote.account_id == account_id)
        ).scalars().all()
        for row in rows:
            if row.note_id in keep:
                continue
            _discard_pdf(row)
            session.delete(row)
            result.removed += 1
        session.commit()


def _mark_needs_auth(account_id: int) -> None:
    with session_scope() as session:
        account = session.get(Account, account_id)
        if account is not None:
            account.status = AccountStatus.NEEDS_AUTH
            session.commit()


def _remember_run(result: BackupResult) -> None:
    with session_scope() as session:
        settings_store.set_value(
            session, "supernote_backup_last_run",
            f"{utcnow().isoformat()}|{result.converted}|{result.unchanged}|"
            f"{result.removed}|{len(result.errors)}",
        )


def last_run(session: Session) -> dict | None:
    """When the last pass ran and what it did, for the page."""
    raw = settings_store.get(session, "supernote_backup_last_run") or ""
    parts = raw.split("|")
    if len(parts) != 5:
        return None
    try:
        return {
            "when": dt.datetime.fromisoformat(parts[0]),
            "converted": int(parts[1]),
            "unchanged": int(parts[2]),
            "removed": int(parts[3]),
            "errors": int(parts[4]),
        }
    except (ValueError, TypeError):
        return None


def notify_expiring_sessions() -> None:
    """Warn subscribed devices before a Supernote sign-in runs out.

    There is no way to renew one in the background -- Supernote offers none --
    so the only cure is a person signing in again, and the only kind warning is
    an early one. Sent once a day at most while inside the window, which the
    notification tag arranges: the same tag replaces the previous one rather
    than stacking up another every pass.
    """
    try:
        from app.web.push_view import broadcast
        from app.web.supernote_setup import expiring_accounts

        with session_scope() as session:
            soon = expiring_accounts(session, within_days=7)
        if not soon:
            return

        expired = [row for row in soon if row["expired"]]
        if expired:
            body = ("A Supernote sign-in has expired and syncing has stopped. "
                    "Signing in again takes a minute.")
        else:
            days = min(row["days"] for row in soon)
            body = (f"A Supernote sign-in runs out in {days} day"
                    f"{'' if days == 1 else 's'}. Signing in again takes a minute.")

        broadcast(
            title="Supernote sign-in",
            body=body,
            url="/services/supernote",
            tag="taskhub-supernote-expiry",
            category="expiring",
        )
    except Exception:  # noqa: BLE001 - never fatal
        logger.debug("Could not send an expiry notification", exc_info=True)
