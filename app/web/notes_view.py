"""Reading the backed-up Supernote notebooks, and choosing which to back up.

The viewer serves PDFs Task Hub made earlier rather than fetching anything from
Supernote, so the page is fast and works while the tablet, the network and
Supernote's servers are all unavailable -- which is most of what a backup is
for.

Serving a file to a browser is the part worth being careful about. The name on
disk is derived from Supernote's id for the note and never from anything a
person typed, and a request names a database row rather than a path, so there
is no arrangement of characters in a notebook's title that can reach a file
outside the notes directory.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError
from app.crypto import decrypt_json
from app.db import settings_store
from app.db.models import SupernoteNote
from app.db.session import get_db
from app.services.supernote_files import WELL_KNOWN_ROOTS, SupernoteFiles
from app.sync import note_backup
from app.web import deps

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notes")


def _notes(db: Session) -> list[SupernoteNote]:
    return list(
        db.execute(
            select(SupernoteNote).order_by(
                SupernoteNote.folder_path, SupernoteNote.name
            )
        ).scalars()
    )


@router.get("")
@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    """Every backed-up notebook, grouped by the folder it came from."""
    rows = _notes(db)
    kept = [r for r in rows if not r.excluded]
    grouped: dict[str, list[SupernoteNote]] = {}
    for row in kept:
        grouped.setdefault(row.folder_path or "", []).append(row)

    return deps.render(
        request, db, "notes.html",
        grouped=grouped,
        total=len(kept),
        readable=len([r for r in kept if r.pdf_name and not r.error]),
        failed=[r for r in kept if r.error and not note_backup.is_pending(r)],
        pending=[r for r in kept if note_backup.is_pending(r)],
        removed=[r for r in rows if r.excluded],
        enabled=note_backup.backup_enabled(db),
        interval=note_backup.backup_interval(db),
        last_run=note_backup.last_run(db),
        selected=note_backup.selected_folders(db),
    )


@router.get("/{note_row_id}/pdf")
def note_pdf(
    note_row_id: int,
    request: Request,
    download: str = "",
    db: Session = Depends(get_db),
):
    """The stored PDF: shown in the page, or saved to the device.

    ``?download=1`` switches the disposition to ``attachment``, which is what
    makes a phone or tablet save the file into its own downloads rather than
    opening it in a tab. That is the whole difference between reading a
    notebook here and keeping a copy on the device in your hand -- and it is
    worth having, because a backup you cannot take away is only half a backup.

    Addressed by database row rather than by name: the file on disk is named
    from Supernote's id, and nothing a person can type reaches this path.
    """
    row = db.get(SupernoteNote, note_row_id)
    if row is None or not row.pdf_name:
        deps.flash(request, "That note has not been backed up yet.", "error")
        return deps.redirect("/notes")

    path = note_backup.pdf_path(row)
    if not path.exists():
        deps.flash(
            request,
            "The PDF for that note is missing from disk. It will be fetched "
            "again on the next backup.",
            "error",
        )
        return deps.redirect("/notes")

    # A notebook title can contain anything, so it is offered as the download
    # name but never used to find the file.
    safe = "".join(c for c in (row.name or "note") if c.isalnum() or c in " ._-").strip()
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe or "note"}.pdf"',
            # These are the user's own notes; no cache should hold them.
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


@router.get("/{note_row_id}/thumb")
def note_thumb(note_row_id: int, db: Session = Depends(get_db)):
    """The small preview of a notebook's first page.

    Cached hard by the browser but marked private: it is a picture of somebody's
    handwriting, so no shared cache should keep it, while the browser that
    already displayed it may as well not fetch it twice. The image only ever
    changes when the notebook does, and then it is written under the same name.
    """
    from fastapi.responses import Response

    row = db.get(SupernoteNote, note_row_id)
    if row is None or not row.thumb_name:
        return Response(status_code=404)
    path = note_backup.thumb_path(row)
    if path is None or not path.exists():
        return Response(status_code=404)
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/{note_row_id}")
def view(note_row_id: int, request: Request, db: Session = Depends(get_db)):
    """A page around one note, so the reader has a title and a way back."""
    row = db.get(SupernoteNote, note_row_id)
    if row is None:
        deps.flash(request, "That note is not in the backup.", "error")
        return deps.redirect("/notes")
    return deps.render(request, db, "note_view.html", note=row)


@router.post("/{note_row_id}/remove")
def remove(note_row_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete Task Hub's copy of one notebook.

    Only Task Hub's copy. The notebook itself stays on the tablet and in
    Supernote's cloud, untouched -- this connector has never had a way to
    delete anything there and this route is not the exception.
    """
    row = db.get(SupernoteNote, note_row_id)
    if row is None:
        deps.flash(request, "That note is not in the backup.", "error")
        return deps.redirect("/notes")

    name = row.name
    note_backup.remove_copy(db, row)
    db.commit()
    deps.flash(
        request,
        f"Removed the copy of {name!r} from Task Hub. It is untouched on your "
        "tablet, and it will not be fetched again unless you restore it.",
        "success",
    )
    return deps.redirect("/notes")


@router.post("/{note_row_id}/restore")
def restore(note_row_id: int, request: Request, db: Session = Depends(get_db)):
    """Let a removed notebook be backed up again on the next pass."""
    row = db.get(SupernoteNote, note_row_id)
    if row is None:
        return deps.redirect("/notes")
    note_backup.restore_copy(db, row)
    db.commit()
    deps.flash(
        request,
        f"{row.name!r} will be fetched again on the next backup.",
        "success",
    )
    return deps.redirect("/notes")


@router.post("/removed/forget")
def forget_removed(request: Request, db: Session = Depends(get_db)):
    """Clear the list of removed notebooks.

    They come back on the next backup, because the only thing keeping them away
    was the record being cleared here. Said plainly on the button rather than
    left as a surprise.
    """
    rows = db.execute(
        select(SupernoteNote).where(SupernoteNote.excluded.is_(True))
    ).scalars().all()
    for row in rows:
        db.delete(row)
    db.commit()
    deps.flash(
        request,
        f"Forgot {len(rows)} removed notebook(s). They will be backed up again "
        "on the next pass.",
        "info",
    )
    return deps.redirect("/notes")


# --- Choosing folders ---------------------------------------------------------

def folder_tree(db: Session) -> tuple[list[dict], str | None]:
    """The top two levels of the Supernote folder tree, for the picker.

    Two levels rather than the whole tree: it is enough to choose "Note" or one
    notebook folder inside it, and walking everything would mean a request per
    folder every time the settings page is opened. The backup itself still
    recurses below whatever is chosen.
    """
    account = note_backup.supernote_account(db)
    if account is None:
        return [], "No Supernote account is connected."

    token = (decrypt_json(account.credentials) or {}).get("token") or ""
    try:
        files = SupernoteFiles(token)
    except ConnectorError as exc:
        return [], str(exc)

    try:
        tree: list[dict] = []
        roots = files.folders_in("0")
        # Supernote's own folders first, in their usual order, then anything
        # the user made themselves.
        order = {name.lower(): i for i, name in enumerate(WELL_KNOWN_ROOTS)}
        roots.sort(key=lambda e: (order.get(e.name.lower(), len(order)), e.name.lower()))
        for root in roots:
            try:
                children = files.folders_in(root.id)
            except ConnectorError:
                children = []
            tree.append({
                "id": root.id,
                "name": root.name,
                "children": [{"id": c.id, "name": c.name} for c in
                             sorted(children, key=lambda e: e.name.lower())],
            })
        return tree, None
    except ConnectorError as exc:
        return [], str(exc)
    finally:
        files.close()


@router.post("/folders")
def save_folders(
    request: Request,
    folder: list[str] = Form(default=[]),
    interval: str = Form(""),
    enabled: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save which folders to back up, how often, and whether to do it at all."""
    note_backup.set_selected_folders(db, folder)
    settings_store.set_bool(db, settings_store.SUPERNOTE_BACKUP_ENABLED, bool(enabled))

    try:
        minutes = int(interval)
    except (TypeError, ValueError):
        minutes = settings_store.DEFAULT_NOTE_BACKUP_INTERVAL_MINUTES
    floor = settings_store.MIN_NOTE_BACKUP_INTERVAL_MINUTES
    if minutes < floor:
        deps.flash(
            request,
            f"The note backup runs at most every {floor} minutes. Converting a "
            "notebook is real work on Supernote's servers, and they publish no "
            "API for it, so Task Hub deliberately asks gently.",
            "warning",
        )
        minutes = floor
    settings_store.set_value(
        db, settings_store.SUPERNOTE_BACKUP_INTERVAL_MINUTES, str(minutes)
    )
    db.commit()

    from app.sync import scheduler

    scheduler.reschedule_note_backup()

    if not folder:
        deps.flash(request, "No folders chosen, so nothing will be backed up.", "info")
    else:
        deps.flash(request, "Note backup settings saved.", "success")
    return deps.redirect("/services/supernote")


@router.post("/backup-now")
def backup_now(request: Request, db: Session = Depends(get_db)):
    """Run a pass immediately.

    Bypasses the on/off switch but never the change check: unchanged notebooks
    are still skipped, so this cannot be used to make Supernote render an entire
    account over and over.
    """
    if not note_backup.selected_folders(db):
        deps.flash(request, "Choose at least one folder to back up first.", "error")
        return deps.redirect("/services/supernote")

    try:
        result = note_backup.run_backup(force=True)
    except Exception as exc:  # noqa: BLE001 - surfaced rather than a 500
        logger.exception("Manual note backup failed")
        deps.flash(request, f"The backup could not run: {exc}", "error")
        return deps.redirect("/services/supernote")

    parts = []
    if result.converted:
        parts.append(f"{result.converted} converted")
    if result.unchanged:
        parts.append(f"{result.unchanged} already current")
    if result.removed:
        parts.append(f"{result.removed} removed")
    if result.deferred:
        parts.append(f"{result.deferred} left for the next run")
    message = ", ".join(parts) or "nothing to do"

    if result.errors:
        deps.flash(request, f"Backup finished: {message}. "
                            f"{len(result.errors)} problem(s): {result.errors[0]}", "warning")
    else:
        deps.flash(request, f"Backup finished: {message}.", "success")
    return deps.redirect("/services/supernote")
