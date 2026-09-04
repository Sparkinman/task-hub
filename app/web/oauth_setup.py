"""Connecting Todoist and TickTick accounts, entirely through the web page.

Google has its own module because its console, its scopes and its seven-day
testing-mode expiry all need explaining in Google's own terms. Todoist and
TickTick are close enough to each other that one module serves both: register an
app, paste a Client ID and Secret, click Connect, approve, come back.

Everything that genuinely differs between the two lives in :class:`OAuthService`
at the top of this file, so adding a fourth OAuth service later means adding one
entry rather than another copy of the flow.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors import microsoft as microsoft_api
from app.connectors import ticktick as ticktick_api
from app.connectors import todoist as todoist_api
from app.connectors.base import ConnectorError
from app.crypto import decrypt_json, encrypt_json
from app.db import settings_store
from app.db.models import Account, AccountStatus, CollectionKind, RemoteList, ServiceKind
from app.db.session import get_db
from app.web import deps
from app.web.disconnect import disconnect_accounts, wants_cleanup
from app.web.forwarded import LOOPBACK_HOSTS, is_bare_ip

router = APIRouter()

#: How many accounts of one service may be connected. Matches the Google page.
MAX_SLOTS = 10


@dataclass(frozen=True)
class OAuthService:
    """Everything that differs between one OAuth service and another."""

    kind: ServiceKind
    name: str
    #: Where the settings row holding the client id and secret is stored.
    settings_key: str
    #: Page where the user registers an application and reads off the two values.
    console_url: str
    console_name: str
    authorization_url: Callable[[str, str, str], str]
    exchange_code: Callable[[str, str, str, str], dict]
    #: Path the service must be told to redirect back to.
    callback_path: str
    #: Some consoles reject a bare IP or a http:// URL; say so before the user
    #: discovers it the hard way at the end of the flow. Both rules exempt
    #: loopback, which every console accepts precisely so that software running
    #: on somebody's own machine can be connected at all.
    requires_https: bool = False
    #: Whether a bare IP address is refused and a name is required. A separate
    #: rule from HTTPS, and services differ on which they apply: TickTick wants
    #: a name but not HTTPS, Microsoft wants HTTPS but not a name, Google wants
    #: both, and Todoist wants neither.
    requires_hostname: bool = False

    #: Whether this service also offers a personal token that can simply be
    #: pasted, skipping app registration entirely.
    personal_token_url: str = ""

    #: Whether to offer pasting the redirected URL back by hand. Needed when the
    #: service may not be able to reach this instance to deliver the code.
    allows_paste_back: bool = False


SERVICES: dict[str, OAuthService] = {
    ServiceKind.TODOIST.value: OAuthService(
        kind=ServiceKind.TODOIST,
        name="Todoist",
        settings_key="todoist_client_credentials",
        console_url="https://developer.todoist.com/appconsole.html",
        console_name="Todoist App Management console",
        authorization_url=todoist_api.authorization_url,
        exchange_code=todoist_api.exchange_code,
        callback_path="/oauth/todoist/callback",
        personal_token_url="https://app.todoist.com/app/settings/integrations/developer",
    ),
    ServiceKind.TICKTICK.value: OAuthService(
        kind=ServiceKind.TICKTICK,
        name="TickTick",
        settings_key="ticktick_client_credentials",
        console_url=ticktick_api.DEVELOPER_CENTRE,
        console_name="TickTick Developer Center",
        authorization_url=ticktick_api.authorization_url,
        exchange_code=ticktick_api.exchange_code,
        callback_path="/oauth/ticktick/callback",
        # TickTick refuses a bare number but is content with plain http, which
        # is why an sslip.io name gets it working where it will not get Google
        # working. See docs/addresses.md.
        requires_hostname=True,
        allows_paste_back=True,
    ),
    ServiceKind.MICROSOFT.value: OAuthService(
        kind=ServiceKind.MICROSOFT,
        name="Microsoft",
        settings_key="microsoft_client_credentials",
        console_url="https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
        console_name="Azure app registrations",
        authorization_url=microsoft_api.authorization_url,
        exchange_code=microsoft_api.exchange_code,
        callback_path="/oauth/microsoft/callback",
        # Azure accepts http://localhost for a Web platform, and nothing else
        # over plain http. The loopback exemption below is what makes those two
        # statements compatible.
        requires_https=True,
        allows_paste_back=True,
    ),
}


def service_for(service_key: str) -> OAuthService | None:
    return SERVICES.get(service_key)


def client_credentials_for(db: Session, kind: ServiceKind) -> tuple[str, str]:
    """The saved Client ID and Secret, or a pair of empty strings."""
    service = SERVICES.get(kind.value)
    if service is None:
        return "", ""
    payload = decrypt_json(settings_store.get(db, service.settings_key))
    return payload.get("client_id", ""), payload.get("client_secret", "")


def has_credentials(db: Session, kind: ServiceKind) -> bool:
    client_id, client_secret = client_credentials_for(db, kind)
    return bool(client_id and client_secret)


def redirect_uri_for(request: Request, service: OAuthService) -> str:
    """The callback address, taken from the address this page was loaded with.

    Deliberately derived from the live request rather than from the configured
    base URL: whatever the user typed into their browser is what the service
    will be asked to send them back to, and the two disagreeing is the single
    most common cause of a failed connection.
    """
    base = str(request.base_url).rstrip("/")
    return f"{base}{service.callback_path}"


def redirect_uri_problem(uri: str, service: OAuthService) -> str | None:
    """Warn about a redirect URI the service will reject, before it does.

    Every console is fussy in its own way, and the error arrives only at the
    very end of the flow, by which point it is hard to tell which of half a
    dozen fields was wrong. The rules encoded here are the ones written down in
    docs/addresses.md, and the two files are meant to agree.

    Loopback is tested first and exempted from everything, because every console
    carves out exactly that exception -- it exists so that software running on
    somebody's own machine can be connected at all. Testing it after the HTTPS
    rule would tell a user that ``http://localhost`` is rejected by a service
    that in fact accepts it, which is worse than saying nothing: it would send
    them off to fix an address that already worked.
    """
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    if not host:
        return "Task Hub cannot work out its own address. Set one under Settings."

    if host in LOOPBACK_HOSTS:
        return (
            f"This redirect URI points at localhost. That works while you use "
            f"Task Hub on this machine, and {service.name} accepts it, but it "
            "will send other devices back to themselves. Set your real address "
            "under Settings before connecting from a phone."
        )

    if service.requires_https and parts.scheme != "https":
        return (
            f"{service.name} will reject this address because it is not HTTPS. "
            "Only localhost may use plain http. Reach Task Hub over HTTPS -- a "
            "Tailscale or Cloudflare address gives you one -- or borrow "
            "localhost with an SSH port forward just long enough to connect."
        )

    if service.requires_hostname and is_bare_ip(host):
        return (
            f"{service.name} will reject this address because it is a bare IP "
            "address, and it wants a name. sslip.io provides one free with "
            f"nothing to install: {host.replace('.', '-')}.sslip.io resolves "
            f"straight back to {host}."
        )
    return None


def _free_slots(db: Session, kind: ServiceKind) -> list[int]:
    taken = {
        account.slot
        for account in db.execute(
            select(Account).where(Account.service == kind)
        ).scalars()
    }
    return [slot for slot in range(1, MAX_SLOTS + 1) if slot not in taken]


# --- Client credentials -------------------------------------------------------


@router.post("/services/{service_key}/credentials")
def save_credentials(
    service_key: str,
    request: Request,
    client_id: str = Form(""),
    client_secret: str = Form(""),
    db: Session = Depends(get_db),
):
    service = service_for(service_key)
    if service is None:
        return deps.redirect("/services")

    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id or not client_secret:
        deps.flash(request, "Both the Client ID and the Client Secret are needed.", "error")
        return deps.redirect(f"/services/{service_key}")

    settings_store.set_value(
        db,
        service.settings_key,
        encrypt_json({"client_id": client_id, "client_secret": client_secret}),
    )
    db.commit()
    deps.flash(
        request,
        f"Saved. You can now connect a {service.name} account below.",
        "success",
    )
    return deps.redirect(f"/services/{service_key}")


@router.post("/services/{service_key}/credentials/clear")
def clear_credentials(service_key: str, request: Request, db: Session = Depends(get_db)):
    service = service_for(service_key)
    if service is None:
        return deps.redirect("/services")
    settings_store.set_value(db, service.settings_key, None)
    db.commit()
    deps.flash(
        request,
        f"Removed the {service.name} application details. Accounts already "
        "connected will stop syncing until you add them again.",
        "info",
    )
    return deps.redirect(f"/services/{service_key}")


# --- The OAuth dance ----------------------------------------------------------


@router.get("/services/{service_key}/connect/{slot}")
def start_oauth(
    service_key: str, slot: int, request: Request, db: Session = Depends(get_db)
):
    service = service_for(service_key)
    if service is None:
        return deps.redirect("/services")

    client_id, client_secret = client_credentials_for(db, service.kind)
    if not client_id or not client_secret:
        deps.flash(
            request,
            f"Add your {service.name} Client ID and Client Secret first.",
            "error",
        )
        return deps.redirect(f"/services/{service_key}")

    if slot < 1 or slot > MAX_SLOTS:
        return deps.redirect(f"/services/{service_key}")

    # The state parameter is both CSRF protection and the only way the callback
    # can tell which service and slot it belongs to -- the services send back
    # nothing else of ours.
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["oauth_service"] = service_key
    request.session["oauth_slot"] = slot

    return deps.redirect(
        service.authorization_url(
            client_id, redirect_uri_for(request, service), state
        )
    )


def _handle_callback(
    service: OAuthService,
    request: Request,
    db: Session,
    code: str | None,
    state: str | None,
    error: str | None,
):
    page = f"/services/{service.kind.value}"

    if error:
        deps.flash(request, f"{service.name} refused the connection: {error}", "error")
        return deps.redirect(page)

    expected = request.session.pop("oauth_state", None)
    slot = request.session.pop("oauth_slot", None)
    request.session.pop("oauth_service", None)

    if not state or not expected or state != expected:
        deps.flash(
            request,
            "That sign-in did not match the one this page started. Nothing was "
            "changed; please try connecting again.",
            "error",
        )
        return deps.redirect(page)
    if not code:
        deps.flash(request, f"{service.name} sent no authorization code.", "error")
        return deps.redirect(page)

    client_id, client_secret = client_credentials_for(db, service.kind)
    try:
        credentials = service.exchange_code(
            client_id, client_secret, code, redirect_uri_for(request, service)
        )
    except ConnectorError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(page)

    slot = int(slot or 1)
    account = db.execute(
        select(Account).where(
            Account.service == service.kind, Account.slot == slot
        )
    ).scalar_one_or_none()
    if account is None:
        account = Account(
            service=service.kind,
            slot=slot,
            label=f"{service.name} {slot}",
        )
        db.add(account)

    account.credentials = encrypt_json(credentials)
    account.enabled = True
    account.status = AccountStatus.CONNECTED
    account.status_detail = None

    # Prove the token works now, while the user is still here and can act on a
    # problem, rather than at the next scheduled sync in the middle of the night.
    db.flush()
    try:
        from app.sync.engine import build_connector

        connector = build_connector(db, account)
        account.remote_identity = connector.verify()
        connector.close()
    except ConnectorError as exc:
        account.status = AccountStatus.ERROR
        account.status_detail = str(exc)
        db.commit()
        deps.flash(
            request,
            f"Connected, but {service.name} would not answer: {exc}",
            "warning",
        )
        return deps.redirect(page)

    db.commit()
    deps.flash(
        request,
        f"{service.name} connected as {account.remote_identity}. Press "
        "“Refresh lists” to fetch its lists.",
        "success",
    )
    return deps.redirect(page)


@router.get("/oauth/todoist/callback")
def todoist_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    return _handle_callback(
        SERVICES[ServiceKind.TODOIST.value], request, db, code, state, error
    )


@router.get("/oauth/ticktick/callback")
def ticktick_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    return _handle_callback(
        SERVICES[ServiceKind.TICKTICK.value], request, db, code, state, error
    )


# --- The simple path: a personal token -----------------------------------------


@router.get("/oauth/microsoft/callback")
def microsoft_callback(
    request: Request, code: str = "", state: str = "", error: str = "",
    db: Session = Depends(get_db),
):
    return _handle_callback(
        SERVICES[ServiceKind.MICROSOFT.value], request, code, state, error, db
    )


@router.post("/services/{service_key}/token")
def save_personal_token(
    service_key: str,
    request: Request,
    api_token: str = Form(""),
    label: str = Form(""),
    slot: int = Form(0),
    db: Session = Depends(get_db),
):
    """Connect an account from a token the user pasted.

    Todoist hands out a personal API token from its own settings that never
    expires and needs no application registered anywhere. For a hub with one
    owner that achieves everything OAuth would, without the redirect URI, the
    client secret or the hourly refresh -- so it is the path offered first.
    """
    service = service_for(service_key)
    if service is None or not service.personal_token_url:
        return deps.redirect("/services")

    page = f"/services/{service_key}"
    api_token = api_token.strip()
    if not api_token:
        deps.flash(request, "Paste the API token first.", "error")
        return deps.redirect(page)

    free = _free_slots(db, service.kind)
    slot = int(slot) or (free[0] if free else 0)
    if not slot:
        deps.flash(
            request,
            f"All {MAX_SLOTS} {service.name} slots are in use. Disconnect one first.",
            "error",
        )
        return deps.redirect(page)

    account = db.execute(
        select(Account).where(Account.service == service.kind, Account.slot == slot)
    ).scalar_one_or_none()
    if account is None:
        account = Account(service=service.kind, slot=slot)
        db.add(account)

    account.label = label.strip() or account.label or f"{service.name} {slot}"
    account.credentials = encrypt_json({"api_token": api_token})
    account.enabled = True
    account.status = AccountStatus.CONNECTED
    account.status_detail = None
    db.flush()

    # Check it now, while the user is still here. A token pasted with a stray
    # space looks identical to a good one until the next scheduled sync.
    try:
        from app.sync.engine import build_connector

        connector = build_connector(db, account)
        account.remote_identity = connector.verify()
        connector.close()
    except ConnectorError as exc:
        db.rollback()
        deps.flash(request, f"{service.name} rejected that token: {exc}", "error")
        return deps.redirect(page)

    db.commit()
    deps.flash(
        request,
        f"{service.name} connected as {account.remote_identity}. Press "
        "\u201cRefresh lists\u201d to fetch its lists.",
        "success",
    )
    return deps.redirect(page)


# --- The fallback: paste the redirected address back ---------------------------


def _code_from_pasted(text: str) -> str | None:
    """Pull the authorization code out of whatever the user pasted.

    They may paste the whole redirected address, the query string alone, or just
    the code. All three are accepted, because telling someone to paste "only the
    part after code=" is exactly the instruction people get wrong.
    """
    text = (text or "").strip()
    if not text:
        return None
    if "code=" in text:
        query = urlsplit(text).query or text.split("?", 1)[-1]
        values = parse_qs(query).get("code")
        if values:
            return values[0].strip()
    if "://" in text or "&" in text or "?" in text:
        return None
    return text


@router.post("/services/{service_key}/paste")
def paste_authorization(
    service_key: str,
    request: Request,
    pasted: str = Form(""),
    db: Session = Depends(get_db),
):
    """Finish a connection by hand when the redirect could not come back.

    TickTick will only redirect to the one address registered with it, and that
    address is often not the one the browser is actually using -- a phone on the
    LAN, an instance behind a different name. When the redirect lands somewhere
    that cannot deliver the code, the address bar still contains it, and pasting
    that whole address here completes the connection.
    """
    service = service_for(service_key)
    if service is None or not service.allows_paste_back:
        return deps.redirect("/services")

    page = f"/services/{service_key}"
    code = _code_from_pasted(pasted)
    if not code:
        deps.flash(
            request,
            "No authorization code found in that. Paste the whole address from "
            "the browser's address bar after approving, including the part "
            "beginning with ?code=",
            "error",
        )
        return deps.redirect(page)

    slot = request.session.pop("oauth_slot", None)
    request.session.pop("oauth_state", None)
    request.session.pop("oauth_service", None)

    client_id, client_secret = client_credentials_for(db, service.kind)
    try:
        credentials = service.exchange_code(
            client_id, client_secret, code, redirect_uri_for(request, service)
        )
    except ConnectorError as exc:
        deps.flash(request, str(exc), "error")
        return deps.redirect(page)

    free = _free_slots(db, service.kind)
    slot = int(slot or (free[0] if free else 1))
    account = db.execute(
        select(Account).where(Account.service == service.kind, Account.slot == slot)
    ).scalar_one_or_none()
    if account is None:
        account = Account(
            service=service.kind, slot=slot, label=f"{service.name} {slot}"
        )
        db.add(account)

    account.credentials = encrypt_json(credentials)
    account.enabled = True
    account.status = AccountStatus.CONNECTED
    account.status_detail = None
    db.flush()

    try:
        from app.sync.engine import build_connector

        connector = build_connector(db, account)
        account.remote_identity = connector.verify()
        connector.close()
    except ConnectorError as exc:
        account.status = AccountStatus.ERROR
        account.status_detail = str(exc)
        db.commit()
        deps.flash(request, f"Connected, but {service.name} would not answer: {exc}", "warning")
        return deps.redirect(page)

    db.commit()
    deps.flash(
        request,
        f"{service.name} connected. Press \u201cRefresh lists\u201d to fetch its lists.",
        "success",
    )
    return deps.redirect(page)


# --- Account housekeeping -----------------------------------------------------


@router.post("/services/{service_key}/{slot}/disconnect")
def disconnect(
    service_key: str, slot: int, request: Request,
    remove_items: str = Form(""),
    db: Session = Depends(get_db),
):
    service = service_for(service_key)
    if service is None:
        return deps.redirect("/services")

    account = db.execute(
        select(Account).where(Account.service == service.kind, Account.slot == slot)
    ).scalar_one_or_none()
    if account is None:
        return deps.redirect(f"/services/{service_key}")

    disconnect_accounts(
        request, db, [account], wants_cleanup(remove_items),
        note=f"Nothing was deleted from {service.name} itself.",
    )
    return deps.redirect(f"/services/{service_key}")


@router.post("/services/{service_key}/{slot}/discover")
def discover_lists(
    service_key: str, slot: int, request: Request, db: Session = Depends(get_db)
):
    """Fetch this account's lists so they can be mapped to collections."""
    service = service_for(service_key)
    if service is None:
        return deps.redirect("/services")

    account = db.execute(
        select(Account).where(Account.service == service.kind, Account.slot == slot)
    ).scalar_one_or_none()
    if account is None:
        return deps.redirect(f"/services/{service_key}")

    try:
        # The reconciliation lives in the sync layer so that every service's
        # setup page treats a vanished list identically.
        from app.sync.engine import refresh_remote_lists

        remote_lists = refresh_remote_lists(db, account)
    except ConnectorError as exc:
        account.status = AccountStatus.ERROR
        account.status_detail = str(exc)
        db.commit()
        deps.flash(request, f"Could not read the lists: {exc}", "error")
        return deps.redirect(f"/services/{service_key}")

    deps.flash(
        request,
        f"Found {len(remote_lists)} list(s) in {account.label}.",
        "success",
    )
    return deps.redirect(f"/services/{service_key}")
