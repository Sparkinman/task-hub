"""The Digests page: read Supernote's highlights, and add to them.

Reading comes from Task Hub's own copy, so the page works while Supernote is
unreachable. Writing goes straight to Supernote and then refreshes the copy,
because a digest that appeared here but not on the tablet would be worse than
one that failed outright -- the whole point is that it turns up in the Digest
app next time somebody opens it.

The PDF is generated here rather than fetched. A digest is a paragraph of text
and a source reference, not a file, so nothing exists to download until Task
Hub sets one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError
from app.db import settings_store
from app.db.models import SupernoteDigestItem
from app.db.session import get_db
from app.services.supernote_digest import SOURCE_TYPES
from app.services.text_pdf import Block, build
from app.sync import digest_sync
from app.web import deps

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/digests")

#: The stand-in name for digests filed in no library. Supernote allows this and
#: the tablet shows them in its "all" view, so they must not simply vanish.
UNFILED = "Unfiled"


def _grouped(db: Session) -> dict[str, list[SupernoteDigestItem]]:
    rows = list(
        db.execute(
            select(SupernoteDigestItem).order_by(
                SupernoteDigestItem.library_name,
                SupernoteDigestItem.remote_created_at.desc(),
            )
        ).scalars()
    )
    grouped: dict[str, list[SupernoteDigestItem]] = {}
    for row in rows:
        grouped.setdefault(row.library_name or UNFILED, []).append(row)
    return grouped


@router.get("")
@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    grouped = _grouped(db)
    return deps.render(
        request, db, "digests.html",
        grouped=grouped,
        total=sum(len(v) for v in grouped.values()),
        enabled=digest_sync.digest_enabled(db),
        selected=digest_sync.selected_libraries(db),
        source_types=SOURCE_TYPES,
        has_account=digest_sync.account(db) is not None,
    )


# --- The PDF ------------------------------------------------------------------

def _pdf_for(rows: list[SupernoteDigestItem], title: str) -> bytes:
    """Set one library's digests as a readable document."""
    blocks = [
        Block(title, size=20, bold=True, space_after=2),
        Block(f"{len(rows)} digest{'' if len(rows) == 1 else 's'} from Supernote",
              size=9.5, grey=True, space_after=20),
    ]
    for row in rows:
        # The passage itself leads, because that is what somebody is reading
        # for. Where it came from follows in grey, quiet enough to skip.
        blocks.append(Block(row.content or "(empty)", size=11.5, space_after=4))

        where = []
        if row.source_path:
            where.append(row.source_path.rsplit("/", 1)[-1])
        if row.page_number:
            where.append(f"page {row.page_number}")
        kind = SOURCE_TYPES.get(row.source_type)
        if kind:
            where.append(kind.lower())
        if where:
            blocks.append(Block(" · ".join(where), size=8.5, grey=True, space_after=2))

        if row.comment:
            blocks.append(Block(f"Note: {row.comment}", size=10, space_after=2))
        if row.has_handwriting:
            blocks.append(Block(
                "This digest has a handwritten comment, which is on the tablet "
                "and not included here.", size=8.5, grey=True, space_after=2))
        blocks.append(Block("", size=6, space_after=10))
    return build(title, blocks)


@router.get("/library/{name}/pdf")
def library_pdf(name: str, download: str = "", db: Session = Depends(get_db)):
    """Every digest in one library, as a PDF.

    Rendered per request rather than stored. These are a few kilobytes of text
    and setting one takes milliseconds, so a cache would be a stale copy to
    manage for no gain.
    """
    grouped = _grouped(db)
    rows = grouped.get(name)
    if not rows:
        return Response(status_code=404)

    safe = "".join(c for c in name if c.isalnum() or c in " ._-").strip() or "digests"
    disposition = "attachment" if download else "inline"
    return Response(
        content=_pdf_for(rows, name),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe}.pdf"',
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


@router.get("/pdf")
def all_pdf(download: str = "", db: Session = Depends(get_db)):
    """Everything, one library after another, in a single document."""
    grouped = _grouped(db)
    if not grouped:
        return Response(status_code=404)

    blocks = [Block("Supernote digests", size=22, bold=True, space_after=20)]
    for name, rows in grouped.items():
        blocks.append(Block(name, size=15, bold=True, space_after=8))
        for row in rows:
            blocks.append(Block(row.content or "(empty)", size=11, space_after=3))
            where = []
            if row.source_path:
                where.append(row.source_path.rsplit("/", 1)[-1])
            if row.page_number:
                where.append(f"page {row.page_number}")
            if where:
                blocks.append(Block(" · ".join(where), size=8.5, grey=True, space_after=10))
            else:
                blocks.append(Block("", size=6, space_after=8))
    disposition = "attachment" if download else "inline"
    return Response(
        content=build("Supernote digests", blocks),
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="digests.pdf"'},
    )


# --- Writing back -------------------------------------------------------------

@router.post("/add")
def add(
    request: Request,
    content: str = Form(""),
    library_uid: str = Form(""),
    db: Session = Depends(get_db),
):
    """Write a new digest to Supernote, then refresh the local copy."""
    content = (content or "").strip()
    if not content:
        deps.flash(request, "Type something to add.", "error")
        return deps.redirect("/digests")

    client = digest_sync.client_for(db)
    if client is None:
        deps.flash(request, "No connected Supernote account.", "error")
        return deps.redirect("/digests")
    try:
        client.create(content, library_uid.strip())
    except ConnectorError as exc:
        deps.flash(request, f"Supernote refused it: {exc}", "error")
        return deps.redirect("/digests")
    finally:
        client.close()

    # Fetched straight back rather than assumed, so the page shows what
    # Supernote actually stored.
    digest_sync.run_sync(force=True)
    deps.flash(request, "Added. It will appear in Digest on your tablet.", "success")
    return deps.redirect("/digests")


@router.post("/{row_id}/edit")
def edit(row_id: int, request: Request, content: str = Form(""),
         db: Session = Depends(get_db)):
    row = db.get(SupernoteDigestItem, row_id)
    if row is None:
        return deps.redirect("/digests")
    client = digest_sync.client_for(db)
    if client is None:
        deps.flash(request, "No connected Supernote account.", "error")
        return deps.redirect("/digests")
    try:
        client.update(row.remote_id, content)
    except ConnectorError as exc:
        deps.flash(request, f"Supernote refused the change: {exc}", "error")
        return deps.redirect("/digests")
    finally:
        client.close()
    digest_sync.run_sync(force=True)
    deps.flash(request, "Saved to Supernote.", "success")
    return deps.redirect("/digests")


@router.post("/{row_id}/delete")
def delete(row_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete on Supernote as well as here -- this one really does remove it."""
    row = db.get(SupernoteDigestItem, row_id)
    if row is None:
        return deps.redirect("/digests")
    client = digest_sync.client_for(db)
    if client is None:
        deps.flash(request, "No connected Supernote account.", "error")
        return deps.redirect("/digests")
    try:
        client.delete(row.remote_id)
    except ConnectorError as exc:
        deps.flash(request, f"Supernote refused: {exc}", "error")
        return deps.redirect("/digests")
    finally:
        client.close()
    db.delete(row)
    db.commit()
    deps.flash(request, "Deleted here and on your tablet.", "success")
    return deps.redirect("/digests")


@router.post("/refresh")
def refresh(request: Request, db: Session = Depends(get_db)):
    result = digest_sync.run_sync(force=True)
    if result.errors:
        deps.flash(request, f"Refreshed with problems: {result.errors[0]}", "warning")
    else:
        deps.flash(
            request,
            f"Refreshed: {result.added} new, {result.updated} changed, "
            f"{result.removed} gone.",
            "success",
        )
    return deps.redirect("/digests")


@router.post("/libraries")
def save_libraries(
    request: Request,
    library: list[str] = Form(default=[]),
    enabled: str = Form(""),
    db: Session = Depends(get_db),
):
    digest_sync.set_selected_libraries(db, library)
    settings_store.set_bool(db, settings_store.SUPERNOTE_DIGEST_ENABLED, bool(enabled))
    db.commit()
    digest_sync.run_sync(force=True)
    deps.flash(request, "Digest settings saved.", "success")
    return deps.redirect("/services/supernote")


def libraries_for_picker(db: Session) -> tuple[list, str | None]:
    """The libraries on the account, for the chooser on the Supernote page."""
    client = digest_sync.client_for(db)
    if client is None:
        return [], None
    try:
        return client.libraries(), None
    except ConnectorError as exc:
        return [], str(exc)
    finally:
        client.close()
