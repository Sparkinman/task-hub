"""Mirroring Supernote digests into Task Hub, and writing new ones back.

Two-way, unlike the notebook backup. Reading keeps a local copy so the page is
readable and searchable while Supernote is unreachable; writing lets a passage
typed on a laptop appear on the tablet.

Supernote is the authority. A digest edited in both places since the last pass
resolves in Supernote's favour, because the tablet is where these are actually
made and a web page is the convenience. Nothing here merges text.

Cheap by construction: one request lists every digest on an account -- there
are tens of these, not thousands -- so there is no per-library fetch and no
paging to manage beyond the first page. It rides the note backup's schedule
rather than adding a third clock.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorAuthError, ConnectorError
from app.crypto import decrypt_json
from app.db import settings_store
from app.db.models import Account, AccountStatus, ServiceKind, SupernoteDigestItem
from app.db.session import session_scope
from app.services.supernote_digest import SupernoteDigests
from app.services.supernote_mark import to_png
from app.sync.note_backup import NOTES_DIR

logger = logging.getLogger(__name__)

#: Stands for "not in any library" in the chooser.
#:
#: Supernote lets a digest belong to no library, and the tablet shows those in
#: its all view. Without a way to tick them they could only be included by
#: ticking nothing at all, which is the opposite of what somebody choosing
#: carefully would expect -- and it silently dropped two highlights on the
#: account this was built against. Cannot collide with a real one: Supernote's
#: are 32-character hex.
UNFILED_UID = "__unfiled__"

#: Decoded handwriting is kept beside the notebook PDFs, so whatever backs up or
#: restores that directory carries it without being told about it.
HANDWRITING_DIR = NOTES_DIR


class DigestResult:
    def __init__(self) -> None:
        self.added = 0
        self.updated = 0
        self.removed = 0
        self.errors: list[str] = []

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (f"DigestResult(added={self.added}, updated={self.updated}, "
                f"removed={self.removed}, errors={len(self.errors)})")


# --- Settings -----------------------------------------------------------------

def selected_libraries(session: Session) -> list[str]:
    """The library unique-ids the user chose to mirror."""
    raw = settings_store.get(session, settings_store.SUPERNOTE_DIGEST_LIBRARIES) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def set_selected_libraries(session: Session, uids: list[str]) -> None:
    cleaned = sorted({str(u).strip() for u in uids if str(u).strip()})
    settings_store.set_value(
        session, settings_store.SUPERNOTE_DIGEST_LIBRARIES, ",".join(cleaned)
    )


def remember_libraries(session: Session, libraries: dict[str, str]) -> None:
    """Keep the account's libraries so a menu can be drawn without a request."""
    settings_store.set_value(
        session,
        settings_store.SUPERNOTE_DIGEST_LIBRARY_CACHE,
        json.dumps([{"uid": uid, "name": name}
                    for uid, name in sorted(libraries.items(), key=lambda p: p[1])]),
    )


def known_libraries(session: Session) -> list[dict]:
    """Every library seen on the account at the last sync.

    Used for the "file this under" menu when adding a digest. Built from the
    whole account rather than from what has been mirrored: somebody mirroring
    one library can still file a new note into another, and an earlier version
    offered only the libraries already on the page -- so a menu that should
    have listed six listed two.
    """
    raw = settings_store.get(session, settings_store.SUPERNOTE_DIGEST_LIBRARY_CACHE)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("uid")]


def digest_enabled(session: Session) -> bool:
    return settings_store.get_bool(session, settings_store.SUPERNOTE_DIGEST_ENABLED)


def account(session: Session) -> Account | None:
    return session.execute(
        select(Account).where(
            Account.service == ServiceKind.SUPERNOTE, Account.enabled.is_(True)
        ).order_by(Account.slot)
    ).scalars().first()


def client_for(session: Session) -> SupernoteDigests | None:
    """A live client for the connected account, or None if there is not one."""
    row = account(session)
    if row is None or row.status == AccountStatus.NEEDS_AUTH:
        return None
    token = (decrypt_json(row.credentials) or {}).get("token") or ""
    if not token:
        return None
    return SupernoteDigests(token)


# --- The pass -----------------------------------------------------------------

def _moment(epoch_ms: int) -> dt.datetime | None:
    if not epoch_ms:
        return None
    try:
        return dt.datetime.fromtimestamp(epoch_ms / 1000, dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def run_sync(force: bool = False) -> DigestResult:
    """Bring the local copy in line with Supernote."""
    result = DigestResult()

    with session_scope() as session:
        if not force and not digest_enabled(session):
            return result
        row = account(session)
        if row is None or row.status == AccountStatus.NEEDS_AUTH:
            result.errors.append("No connected Supernote account.")
            return result
        wanted = set(selected_libraries(session))
        account_id = row.id
        token = (decrypt_json(row.credentials) or {}).get("token") or ""
        client = client_for(session)

    if client is None:
        result.errors.append("No usable Supernote session.")
        return result

    try:
        libraries = {lib.unique_id: lib.name for lib in client.libraries()}
        digests = client.digests()
    except ConnectorAuthError as exc:
        result.errors.append(str(exc))
        with session_scope() as session:
            acc = session.get(Account, account_id)
            if acc is not None:
                acc.status = AccountStatus.NEEDS_AUTH
                session.commit()
        return result
    except ConnectorError as exc:
        result.errors.append(str(exc))
        return result
    finally:
        client.close()

    # An empty selection means every library, including digests filed in none.
    # "Choose nothing and get nothing" would be a puzzling default for a mirror
    # somebody has just switched on.
    def chosen(digest) -> bool:
        if not wanted:
            return True  # Nothing ticked means everything.
        if digest.library_uid:
            return digest.library_uid in wanted
        return UNFILED_UID in wanted

    with session_scope() as session:
        remember_libraries(session, libraries)
        session.commit()

    keep = [d for d in digests if chosen(d)]
    seen = {d.id for d in keep}

    # A digest's handwritten note is a separate download, so only fetched for
    # the ones that have one and only when it has actually changed.
    handwriting: dict[str, str] = {}
    with session_scope() as session:
        known = {
            row.remote_id: (row.handwriting_md5, row.handwriting_name)
            for row in session.execute(
                select(SupernoteDigestItem).where(
                    SupernoteDigestItem.account_id == account_id
                )
            ).scalars()
        }
    # A second client: the first was closed once the listing was read, and the
    # drawings are fetched separately so a failure there costs a picture rather
    # than the whole pass.
    client = SupernoteDigests(token)
    try:
        for digest in keep:
            if not digest.handwriting:
                continue
            was_md5, was_name = known.get(digest.id, ("", None))
            if was_name and was_md5 == digest.handwriting_md5:
                handwriting[digest.id] = was_name
                if (HANDWRITING_DIR / was_name).exists():
                    continue
            try:
                raw = client.handwriting_bytes(digest.id)
            except ConnectorError as exc:
                result.errors.append(f"handwriting for a digest: {exc}")
                continue
            png = to_png(raw) if raw else None
            if not png:
                continue
            HANDWRITING_DIR.mkdir(parents=True, exist_ok=True)
            name = f"digest-{digest.id}.png"
            (HANDWRITING_DIR / name).write_bytes(png)
            handwriting[digest.id] = name
    finally:
        client.close()

    with session_scope() as session:
        existing = {
            row.remote_id: row
            for row in session.execute(
                select(SupernoteDigestItem).where(
                    SupernoteDigestItem.account_id == account_id
                )
            ).scalars()
        }

        for digest in keep:
            row = existing.get(digest.id)
            if row is None:
                row = SupernoteDigestItem(account_id=account_id, remote_id=digest.id)
                session.add(row)
                result.added += 1
            elif row.remote_md5 != digest.md5 or row.content != digest.content:
                result.updated += 1

            row.library_uid = digest.library_uid
            row.library_name = libraries.get(digest.library_uid, "")
            row.content = digest.content
            row.comment = digest.comment
            row.source_path = digest.source_path
            row.source_type = digest.source_type
            row.page_number = digest.page_number
            row.has_handwriting = bool(digest.handwriting)
            row.handwriting_md5 = digest.handwriting_md5
            # Kept when the decode failed: the flag above still says the tablet
            # has more, which is honest, and a later pass may manage it.
            if digest.id in handwriting:
                row.handwriting_name = handwriting[digest.id]
            row.remote_md5 = digest.md5
            row.remote_created_at = _moment(digest.created_at)
            row.remote_updated_at = _moment(digest.modified_at)

        # Gone from Supernote, or from a library no longer chosen.
        for remote_id, row in existing.items():
            if remote_id not in seen:
                session.delete(row)
                result.removed += 1
        session.commit()

    return result
