"""Connecting Obsidian: sign in once, then link as many vaults as you like.

Obsidian does not fit either of the shapes the other services use. There is no
OAuth to redirect through, and unlike Apple or Things it is not enough to store
a password: the account has to be signed in to Obsidian's own client, and then
vaults have to be picked from the ones that account can see.

**One sign-in, several vaults.** Obsidian's client holds a single session, but
that session can sync any number of vaults -- it keeps their state separately,
in ``sync/<vault id>/`` beneath one shared login. So the session is signed in
once, and each vault linked from it becomes an account of its own with its own
slot, its own folder mappings and its own sync process. That is what lets a work
vault and a personal vault be mapped to different collections, enabled and
disabled separately, and disconnected one at a time.

The password is used once and not kept. Obsidian's client holds a session of its
own afterwards, in the data volume, so a restart does not sign the user out.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError
from app.crypto import decrypt_json, encrypt_json
from app.db import settings_store
from app.db.models import Account, AccountStatus, ServiceKind
from app.db.session import get_db
from app.services import obsidian_cli as cli
from app.web import deps

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/obsidian")

BACK = "/services/obsidian"

#: The signed-in address, kept here rather than on an account because the
#: sign-in exists before any vault has been chosen and outlives all of them.
EMAIL_KEY = "obsidian_email"

#: The same ceiling every other service has.
MAX_VAULTS = 10


def accounts_for(db: Session) -> list[Account]:
    """Every linked vault, in slot order."""
    return list(
        db.execute(
            select(Account)
            .where(Account.service == ServiceKind.OBSIDIAN)
            .order_by(Account.slot)
        ).scalars()
    )


def account_for(db: Session) -> Account | None:
    """The first linked vault. Kept for callers that only need any one."""
    accounts = accounts_for(db)
    return accounts[0] if accounts else None


def account_for_slot(db: Session, slot: int) -> Account | None:
    return db.execute(
        select(Account).where(
            Account.service == ServiceKind.OBSIDIAN, Account.slot == slot
        )
    ).scalar_one_or_none()


def linked_vault(account: Account | None) -> dict:
    """Which vault this account is syncing, as saved when it was chosen."""
    if account is None or not account.credentials:
        return {}
    try:
        return decrypt_json(account.credentials) or {}
    except Exception:       # noqa: BLE001 -- a key change must not break the page
        return {}


def _free_slot(db: Session) -> int | None:
    used = {a.slot for a in accounts_for(db)}
    return next((n for n in range(1, MAX_VAULTS + 1) if n not in used), None)


def state(db: Session) -> dict:
    """Everything the page needs to know about where setup has got to.

    Deliberately assembled in one place: the template shows one of three very
    different things depending on this, and working it out in the template
    instead would put the logic somewhere it cannot be read or tested.
    """
    accounts = accounts_for(db)

    if not cli.available():
        return {"step": "unavailable", "accounts": accounts, "linked": []}

    if not cli.logged_in():
        return {"step": "login", "accounts": accounts, "linked": []}

    from app.services.obsidian_sync import manager as sync_manager

    linked = []
    for account in accounts:
        vault = linked_vault(account)
        name = vault.get("name", "")
        path = cli.vault_path(name)
        linked.append({
            "account": account,
            "paused": not account.enabled,
            "write_back": bool(vault.get("write_back")),
            "write_folders": set(vault.get("write_folders") or []),
            "skip_folders": set(cli.excluded_folders(vault.get("vault_id") or "")),
            "vault": vault,
            "path": str(path),
            "downloaded": path.exists() and any(path.iterdir()),
            # Whether this vault is being kept current, rather than being a
            # copy taken once and quietly ageing.
            "live": sync_manager.status(name),
        })

    # The remote list is always offered, not only when nothing is linked yet:
    # a second vault should be addable later without disconnecting the first.
    vaults, result = cli.list_vaults()
    already = {linked_vault(a).get("vault_id") for a in accounts}

    return {
        "step": "linked",
        "accounts": accounts,
        "linked": linked,
        "email": settings_store.get(db, EMAIL_KEY) or "",
        "vaults": vaults,
        "linked_ids": already,
        "addable": [v for v in vaults if v.vault_id not in already],
        "slots_left": MAX_VAULTS - len(accounts),
        "error": None if result.ok else result.message,
    }


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    mfa: str = Form(""),
    db: Session = Depends(get_db),
):
    email = email.strip()
    if not email or not password:
        deps.flash(request, "Enter your Obsidian email and password.", "error")
        return deps.redirect(BACK)

    result = cli.login(email, password, mfa)
    if not result.ok:
        # The client's own words, which are more useful than anything invented
        # here -- it distinguishes a wrong password from a missing 2FA code.
        deps.flash(request, result.message, "error")
        return deps.redirect(BACK)

    # No account yet. Signing in is not the same as linking a vault, and
    # creating an account here would make an empty one that syncs nothing.
    settings_store.set_value(db, EMAIL_KEY, email)
    db.commit()

    deps.flash(
        request,
        f"Signed in to Obsidian as {email}. Now choose which vaults to sync.",
        "success",
    )
    return deps.redirect(BACK)


@router.post("/vaults")
def choose_vaults(
    request: Request,
    vault_id: list[str] = Form(default=[]),
    encryption_password: str = Form(""),
    db: Session = Depends(get_db),
):
    """Link one or more vaults, each as its own slot."""
    if not cli.logged_in():
        deps.flash(request, "Sign in to Obsidian first.", "error")
        return deps.redirect(BACK)

    wanted = [v for v in vault_id if v.strip()]
    if not wanted:
        deps.flash(request, "Choose at least one vault to sync.", "error")
        return deps.redirect(BACK)

    remote, _ = cli.list_vaults()
    names = {v.vault_id: v.name for v in remote}
    email = settings_store.get(db, EMAIL_KEY) or ""
    already = {linked_vault(a).get("vault_id") for a in accounts_for(db)}

    linked: list[str] = []
    problems: list[str] = []

    for chosen in wanted:
        if chosen in already:
            continue
        slot = _free_slot(db)
        if slot is None:
            problems.append(
                f"No slots left — Task Hub syncs up to {MAX_VAULTS} vaults."
            )
            break

        name = names.get(chosen, chosen)
        result = cli.setup(chosen, name, encryption_password)
        if not result.ok:
            # The client says "Password not provided.", which is true and means
            # nothing to someone who has just typed their account password in
            # the box above. It is asking for a different password entirely.
            hint = ""
            if "password" in result.message.lower():
                hint = (
                    " This vault has end-to-end encryption turned on, so it "
                    "needs its own encryption password — the one you set in "
                    "Obsidian when you enabled encryption, not your account "
                    "password. Enter it in the box and tick only this vault."
                )
            problems.append(f"{name}: {result.message}{hint}")
            continue

        account = Account(service=ServiceKind.OBSIDIAN, slot=slot)
        account.label = name
        account.remote_identity = email
        account.status = AccountStatus.CONNECTED
        account.status_detail = None
        account.credentials = encrypt_json({"vault_id": chosen, "name": name})
        db.add(account)
        db.commit()
        already.add(chosen)
        linked.append(name)

        # Download it and read its folders straight away, so the mapping table
        # is populated when the page comes back rather than after a step nobody
        # would think to look for.
        cli.sync_once(name)
        try:
            from app.sync.engine import refresh_remote_lists

            refresh_remote_lists(db, account)
        except ConnectorError as exc:
            logger.warning("Linked %s but could not read its folders: %s", name, exc)

    # One call at the end rather than one per vault: it reconciles the whole
    # set, so doing it repeatedly would only restart what is already running.
    from app.services.obsidian_sync import apply_obsidian_sync_settings

    apply_obsidian_sync_settings()

    for problem in problems[:3]:
        deps.flash(request, problem, "error")
    if linked:
        joined = ", ".join(f"“{n}”" for n in linked)
        deps.flash(
            request,
            f"Linked {joined}. Read-only: Task Hub cannot change {'them' if len(linked) > 1 else 'it'}.",
            "success",
        )
    return deps.redirect(BACK)


@router.post("/{slot}/discover")
def discover(slot: int, request: Request, db: Session = Depends(get_db)):
    """Read a vault's folders so they can be mapped to collections.

    Obsidian has no lists of its own -- it has files, folders and tags -- so
    what is offered here is a choice: the whole vault, and each top-level
    folder. Every one is read-only, which is what stops the mapping table
    offering a write-back tick that could never do anything.
    """
    account = account_for_slot(db, slot)
    if account is None:
        deps.flash(request, "That vault is not linked.", "error")
        return deps.redirect(BACK)

    try:
        from app.sync.engine import refresh_remote_lists

        found = refresh_remote_lists(db, account)
    except ConnectorError as exc:
        deps.flash(request, f"Could not read the vault: {exc}", "error")
        return deps.redirect(BACK)

    deps.flash(
        request,
        f"Found {len(found)} place(s) in “{account.label}” to read tasks from.",
        "success",
    )
    return deps.redirect(BACK)


@router.post("/{slot}/sync")
def sync_now(slot: int, request: Request, db: Session = Depends(get_db)):
    account = account_for_slot(db, slot)
    vault = linked_vault(account)
    if not vault.get("name"):
        deps.flash(request, "That vault is not linked.", "error")
        return deps.redirect(BACK)

    name = vault["name"]

    # Continuous sync holds the vault's lock, so its child is paused for the
    # duration rather than left to collide with a second client on the same
    # directory. Only this vault's child: the others are unaffected.
    from app.services.obsidian_sync import apply_obsidian_sync_settings
    from app.services.obsidian_sync import manager as sync_manager

    was_running = sync_manager.status(name).running
    if was_running:
        sync_manager.apply([
            v for v in sync_manager.statuses() if v != name
        ])
    try:
        result = cli.sync_once(name)
    finally:
        if was_running:
            apply_obsidian_sync_settings()

    if not result.ok:
        deps.flash(request, result.message, "error")
    else:
        deps.flash(request, f"Downloaded the latest of “{name}”.", "success")
    return deps.redirect(BACK)


@router.post("/{slot}/pause")
def pause_vault(slot: int, request: Request, db: Session = Depends(get_db)):
    """Stop syncing a vault without throwing away how it is set up.

    This exists because unlinking was the only way to say "not this one", and
    unlinking deletes the account -- which takes its folder mappings with it.
    Someone deciding they do not want a vault synced loses the work of wiring
    it up, and re-linking hands them a blank one. Pausing keeps every mapping
    exactly where it is and simply stops the vault being read.
    """
    account = account_for_slot(db, slot)
    if account is None:
        return deps.redirect(BACK)

    account.enabled = not account.enabled
    db.commit()

    # Reconciled after the change: a paused vault's continuous sync is stopped
    # too, so a vault nobody is reading is not still being downloaded.
    from app.services.obsidian_sync import apply_obsidian_sync_settings

    apply_obsidian_sync_settings()

    if account.enabled:
        deps.flash(request, f"Syncing “{account.label}” again.", "success")
    else:
        deps.flash(
            request,
            f"Paused “{account.label}”. Its folder mappings are kept, so "
            "resuming picks up exactly where it left off.",
            "success",
        )
    return deps.redirect(BACK)


@router.post("/{slot}/write-back")
def set_write_back(
    slot: int,
    request: Request,
    enabled: str = Form(""),
    confirm: str = Form(""),
    folders: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Turn write-back on or off for one vault, and choose which folders.

    Enabling means changing Obsidian's own sync mode from ``mirror-remote`` to
    ``bidirectional``. That is the real switch: while the client is in
    mirror-remote it reverts anything written locally, so leaving it there and
    flipping a flag here would produce writes that silently vanish. Doing it
    this way round also means turning write-back *off* restores the guarantee
    rather than merely promising it.
    """
    account = account_for_slot(db, slot)
    if account is None:
        return deps.redirect(BACK)

    vault = linked_vault(account)
    name = vault.get("name") or ""
    want = enabled == "1"

    if want and confirm != "1":
        deps.flash(request, "Tick the box confirming you understand, first.", "error")
        return deps.redirect(BACK)

    mode = "bidirectional" if want else "mirror-remote"
    result = cli.set_sync_mode(cli.vault_path(name), mode)
    if not result.ok:
        deps.flash(
            request,
            f"Could not change Obsidian's sync mode for “{name}”: {result.message}. "
            "Nothing was changed.",
            "error",
        )
        return deps.redirect(BACK)

    vault["write_back"] = want
    vault["write_folders"] = sorted(f for f in folders if f) if want else []
    account.credentials = encrypt_json(vault)
    db.commit()

    if want:
        where = (
            "the whole vault" if not vault["write_folders"]
            else ", ".join(f.replace("folder:", "") or "the vault root"
                           for f in vault["write_folders"])
        )
        deps.flash(
            request,
            f"Task Hub can now tick tasks off in “{name}” ({where}). It changes "
            "nothing else in your notes, and stops after "
            "20 changes in one pass.",
            "warning",
        )
    else:
        deps.flash(
            request,
            f"“{name}” is read-only again. Obsidian's client is back in "
            "mirror-remote mode, so it would revert any local change.",
            "success",
        )
    return deps.redirect(BACK)


@router.post("/{slot}/skip-folders")
def set_skipped_folders(
    slot: int,
    request: Request,
    skip: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Choose which top-level folders of a vault not to download at all.

    Worth having as a setting rather than a one-off: a vault holding
    attachments will pull down hundreds of megabytes Task Hub can never read,
    and the person who notices is whoever runs out of disk.
    """
    account = account_for_slot(db, slot)
    if account is None:
        return deps.redirect(BACK)

    vault = linked_vault(account)
    name = vault.get("name") or ""
    wanted = sorted({f.strip() for f in skip if f.strip()})

    result = cli.set_excluded_folders(cli.vault_path(name), wanted)
    if not result.ok:
        deps.flash(request, f"Could not change what is skipped: {result.message}", "error")
        return deps.redirect(BACK)

    vault["skip_folders"] = wanted
    account.credentials = encrypt_json(vault)
    db.commit()

    if wanted:
        deps.flash(
            request,
            f"Skipping {', '.join(wanted)} in “{name}”. Files already downloaded "
            "are left alone — the folders simply stop being synced.",
            "success",
        )
    else:
        deps.flash(request, f"Syncing all of “{name}” again.", "success")
    return deps.redirect(BACK)


@router.post("/{slot}/disconnect")
def disconnect_vault(
    slot: int,
    request: Request,
    remove_items: str = Form(""),
    db: Session = Depends(get_db),
):
    """Unlink one vault, leaving the sign-in and any other vaults alone."""
    account = account_for_slot(db, slot)
    if account is None:
        return deps.redirect(BACK)

    from app.web.disconnect import disconnect_accounts, wants_cleanup

    disconnect_accounts(
        request, db, [account], wants_cleanup(remove_items),
        note="The downloaded copy of the vault is left on disk.",
    )

    # Reconciled after the account is gone, which stops this vault's child.
    from app.services.obsidian_sync import apply_obsidian_sync_settings

    apply_obsidian_sync_settings()
    return deps.redirect(BACK)


@router.post("/disconnect")
def sign_out(
    request: Request,
    remove_items: str = Form(""),
    db: Session = Depends(get_db),
):
    """Sign out of Obsidian entirely, unlinking every vault.

    The downloaded copies are left in place rather than deleted. Removing a few
    thousand of somebody's notes as a side effect of clicking "sign out" is not
    a thing to do quietly, and it costs nothing to leave them.
    """
    # Stopped first. Signing out from under a running sync would leave child
    # processes retrying against an account that no longer exists.
    from app.services.obsidian_sync import manager as sync_manager

    sync_manager.stop()

    cli.logout()
    settings_store.set_value(db, EMAIL_KEY, None)

    accounts = accounts_for(db)
    if accounts:
        from app.web.disconnect import disconnect_accounts, wants_cleanup

        disconnect_accounts(
            request, db, accounts, wants_cleanup(remove_items),
            note="The downloaded copies of the vaults are left on disk.",
        )
    else:
        db.commit()
        deps.flash(request, "Signed out of Obsidian.", "success")
    return deps.redirect(BACK)
