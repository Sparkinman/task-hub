"""Management UI for the built-in Radicale CalDAV server.

Creating collections, renaming them, changing the CalDAV password and finding
the URL to type into a phone are all done here. None of it requires a terminal,
which is the point: Radicale is normally administered by editing an htpasswd
file and making directories by hand, and neither is acceptable for this project.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import decrypt_json, encrypt_json, hash_password
from app.db import settings_store
from app.db.models import (
    CollectionKind,
    ListMapping,
    RadicaleCollection,
    RemoteList,
    SyncGroup,
)
from app.db.session import get_db
from app.radicale_embed import set_radicale_user
from app.services.caldav_client import (
    CalDAVError,
    RadicaleClient,
    slugify_collection_id,
)
from app.web import deps
from app.web import public_url as _public_url
from app.web.setup import MIN_PASSWORD_LENGTH, RADICALE_PASSWORD_KEY

router = APIRouter(prefix="/radicale-admin")


def get_radicale_credentials(db: Session) -> tuple[str, str] | None:
    """The stored CalDAV username and password, or None if not set up."""
    username = settings_store.get(db, settings_store.RADICALE_USERNAME)
    if not username:
        return None
    password = decrypt_json(settings_store.get(db, RADICALE_PASSWORD_KEY)).get("password")
    if not password:
        return None
    return username, password


def get_radicale_client(db: Session) -> RadicaleClient | None:
    """A connected client for the configured Radicale account.

    Returns None rather than raising when Radicale has not been configured yet,
    so callers can render a "not set up" state instead of an error page.
    """
    credentials = get_radicale_credentials(db)
    if credentials is None:
        return None
    return RadicaleClient(*credentials)


def public_base_url(request: Request, db: Session) -> str:
    """The address CalDAV clients should be given.

    Derived from the request rather than configured, so the address shown is
    one the visiting device demonstrably just used.
    """
    return _public_url.public_base_url(request, db)


def sync_collection_cache(db: Session, client: RadicaleClient) -> list:
    """Refresh the cached collection list from the live CalDAV server.

    Radicale is authoritative -- a collection could have been created by an
    external CalDAV client, or removed from disk -- so the cache is rebuilt from
    what the server actually reports rather than trusted on its own.
    """
    live = client.list_collections(with_counts=True)
    username = client.username
    live_ids = {c.collection_id for c in live}

    known = {
        row.collection_id: row
        for row in db.execute(
            select(RadicaleCollection).where(
                RadicaleCollection.radicale_user == username
            )
        ).scalars()
    }

    for info in live:
        row = known.get(info.collection_id)
        if row is None:
            db.add(
                RadicaleCollection(
                    radicale_user=username,
                    collection_id=info.collection_id,
                    display_name=info.display_name,
                    kind=info.kind,
                    colour=info.colour,
                )
            )
        else:
            row.display_name = info.display_name
            row.kind = info.kind
            row.colour = info.colour or row.colour

    for collection_id, row in known.items():
        if collection_id not in live_ids:
            db.delete(row)

    db.commit()
    return live


@router.get("")
@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    client = get_radicale_client(db)
    if client is None:
        return deps.render(
            request, db, "radicale_admin.html",
            configured=False, collections=[], caldav_base="", base_mismatch=None,
            radicale_web_url=f"{RADICALE_MOUNT_PATH}/.web/",
        )

    errors: list[str] = []
    collections = []
    try:
        collections = sync_collection_cache(db, client)
    except CalDAVError as exc:
        errors.append(str(exc))

    from app.config import RADICALE_MOUNT_PATH

    # The address shown here is the one people type into a phone, so normally it
    # is simply the address this page was loaded from -- which is known to work,
    # because it just did. Only an override can make it something else, and an
    # override left behind by an earlier deployment produces an address that
    # fails silently on every device that copies it. Say so where it is read.
    base_mismatch = _public_url.override_conflict(request, db)

    # A loopback address is right for the machine running Task Hub and useless
    # from anywhere else, which is worth flagging even when nothing is wrong.
    caldav_host = urlsplit(public_base_url(request, db)).hostname or ""
    base_is_loopback = caldav_host.lower() in {"localhost", "127.0.0.1", "::1"}

    return deps.render(
        request, db, "radicale_admin.html",
        configured=True,
        collections=collections,
        radicale_username=client.username,
        caldav_base=f"{public_base_url(request, db)}{RADICALE_MOUNT_PATH}/",
        caldav_discovery=f"{public_base_url(request, db)}{RADICALE_MOUNT_PATH}/{client.username}/",
        # Root-relative on purpose. The configured base URL is whatever was
        # set for OAuth redirects -- often "localhost", which resolves to the
        # visiting device rather than the server and simply times out. A
        # relative link always resolves against the host actually in use.
        radicale_web_url=f"{RADICALE_MOUNT_PATH}/.web/",
        errors=errors,
        base_mismatch=base_mismatch,
        base_is_loopback=base_is_loopback,
    )


@router.post("/collections/create")
def create_collection(
    request: Request,
    display_name: str = Form(...),
    kind: str = Form("tasks"),
    colour: str = Form("#2563eb"),
    db: Session = Depends(get_db),
):
    client = get_radicale_client(db)
    if client is None:
        deps.flash(request, "Radicale is not configured yet.", "error")
        return deps.redirect("/radicale-admin")

    display_name = display_name.strip()
    if not display_name:
        deps.flash(request, "Give the collection a name.", "error")
        return deps.redirect("/radicale-admin")

    try:
        collection_kind = CollectionKind(kind)
    except ValueError:
        deps.flash(request, "Choose whether this holds tasks or calendar events.", "error")
        return deps.redirect("/radicale-admin")

    collection_id = slugify_collection_id(display_name)
    try:
        existing = {c.collection_id for c in client.list_collections()}
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/radicale-admin")

    if collection_id in existing:
        # Two collections named "Work" would collide on one URL; disambiguate
        # rather than silently overwriting the first.
        suffix = 2
        while f"{collection_id}-{suffix}" in existing:
            suffix += 1
        collection_id = f"{collection_id}-{suffix}"

    try:
        client.create_collection(collection_id, display_name, collection_kind, colour)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/radicale-admin")

    db.add(
        RadicaleCollection(
            radicale_user=client.username,
            collection_id=collection_id,
            display_name=display_name,
            kind=collection_kind,
            colour=colour,
        )
    )
    db.commit()
    deps.flash(request, f"Created {display_name!r}.", "success")
    return deps.redirect("/radicale-admin")


def _drop_mismatched_mappings(
    db: Session, collection: RadicaleCollection, kind: CollectionKind
) -> int:
    """Remove list links that the collection's new type has invalidated.

    A collection that has become a calendar cannot go on feeding a Google task
    list. Clearing those links here rather than refusing the type change keeps
    the user out of a dead end where the only way forward is to guess which
    other page holds the thing blocking them.
    """
    groups = db.execute(
        select(SyncGroup).where(SyncGroup.radicale_collection_id == collection.id)
    ).scalars().all()
    removed = 0
    for group in groups:
        mappings = db.execute(
            select(ListMapping).where(ListMapping.sync_group_id == group.id)
        ).scalars().all()
        for mapping in mappings:
            remote_list = db.get(RemoteList, mapping.remote_list_id)
            if remote_list is None or remote_list.kind != kind:
                db.delete(mapping)
                removed += 1
        group.kind = kind
    return removed


@router.post("/collections/{collection_id}/rename")
def rename_collection(
    request: Request,
    collection_id: str,
    display_name: str = Form(...),
    colour: str = Form(""),
    kind: str = Form(""),
    db: Session = Depends(get_db),
):
    """Edit one collection's name, colour and type.

    The type is validated before anything is written, so a refused type change
    leaves the name and colour untouched too. A half-applied edit would be
    worse than none: the user would have to work out which half landed.
    """
    client = get_radicale_client(db)
    if client is None:
        return deps.redirect("/radicale-admin")

    display_name = display_name.strip()
    if not display_name:
        deps.flash(request, "The name cannot be empty.", "error")
        return deps.redirect("/radicale-admin")

    row = db.execute(
        select(RadicaleCollection).where(
            RadicaleCollection.radicale_user == client.username,
            RadicaleCollection.collection_id == collection_id,
        )
    ).scalar_one_or_none()

    new_kind: CollectionKind | None = None
    if kind:
        try:
            new_kind = CollectionKind(kind)
        except ValueError:
            deps.flash(request, "Choose either tasks or calendar events.", "error")
            return deps.redirect("/radicale-admin")

    changing_kind = (
        new_kind is not None and row is not None and row.kind != new_kind
    )

    if changing_kind:
        # A to-do and a calendar event are different objects, so the items
        # already in the collection could not survive being reinterpreted as
        # the other. Rather than convert or destroy them, the change is only
        # offered on a collection that has nothing to lose.
        try:
            held = client.count_records(collection_id)
        except CalDAVError as exc:
            deps.flash(request, str(exc), "error")
            return deps.redirect("/radicale-admin")
        if held:
            deps.flash(
                request,
                f"{row.display_name!r} still holds {held} item(s), so its type "
                "cannot be changed. Empty it first, or make a new collection of "
                "the type you want.",
                "error",
            )
            return deps.redirect("/radicale-admin")

    try:
        client.rename_collection(collection_id, display_name, colour or None)
        if changing_kind:
            client.set_collection_kind(collection_id, new_kind)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/radicale-admin")

    dropped = 0
    if row is not None:
        row.display_name = display_name
        if colour:
            row.colour = colour
        if changing_kind:
            row.kind = new_kind
            dropped = _drop_mismatched_mappings(db, row, new_kind)
        db.commit()

    if changing_kind:
        message = f"{display_name!r} now holds {new_kind.value}."
        if dropped:
            message += (
                f" {dropped} service list link(s) no longer matched its type and "
                "were removed."
            )
        deps.flash(request, message, "success")
    else:
        deps.flash(request, "Collection updated.", "success")
    return deps.redirect("/radicale-admin")


@router.post("/collections/{collection_id}/delete")
def delete_collection(
    request: Request,
    collection_id: str,
    confirm_name: str = Form(""),
    db: Session = Depends(get_db),
):
    client = get_radicale_client(db)
    if client is None:
        return deps.redirect("/radicale-admin")

    row = db.execute(
        select(RadicaleCollection).where(
            RadicaleCollection.radicale_user == client.username,
            RadicaleCollection.collection_id == collection_id,
        )
    ).scalar_one_or_none()
    expected = row.display_name if row else collection_id

    # Deleting a collection destroys every task in it and cannot be undone, so
    # it takes a typed confirmation rather than a single click.
    if confirm_name.strip() != expected:
        deps.flash(
            request,
            f"Type the collection name exactly ({expected!r}) to confirm deletion.",
            "error",
        )
        return deps.redirect("/radicale-admin")

    try:
        client.delete_collection(collection_id)
    except CalDAVError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/radicale-admin")

    if row is not None:
        db.delete(row)
        db.commit()

    deps.flash(request, f"Deleted {expected!r}.", "success")
    return deps.redirect("/radicale-admin")


@router.post("/password")
def change_password(
    request: Request,
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    username = settings_store.get(db, settings_store.RADICALE_USERNAME)
    if not username:
        return deps.redirect("/radicale-admin")

    if len(new_password) < MIN_PASSWORD_LENGTH:
        deps.flash(
            request,
            f"The CalDAV password must be at least {MIN_PASSWORD_LENGTH} characters.",
            "error",
        )
        return deps.redirect("/radicale-admin")
    if new_password != new_password_confirm:
        deps.flash(request, "The two passwords do not match.", "error")
        return deps.redirect("/radicale-admin")

    try:
        password_hash = hash_password(new_password)
    except ValueError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect("/radicale-admin")

    set_radicale_user(username, password_hash)
    settings_store.set_value(
        db, RADICALE_PASSWORD_KEY, encrypt_json({"password": new_password})
    )
    db.commit()

    deps.flash(
        request,
        "CalDAV password changed. Any phone or laptop syncing with the old "
        "password will need updating.",
        "success",
    )
    return deps.redirect("/radicale-admin")
