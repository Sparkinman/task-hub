"""Noticing that Task Hub has moved since a service was connected.

Moving Task Hub -- from a LAN address to a Tailscale name, from localhost to a
Cloudflare tunnel -- breaks nothing that is already connected. Refreshing a
token never sends a redirect address, so a connection made months ago at
``http://localhost:8080`` keeps syncing happily from anywhere.

What it breaks is the *next* connection. Reconnecting a revoked account, adding
a second one, or renewing TickTick -- which has no refresh at all and must be
reconnected when its token expires -- sends the address in use at that moment,
and the console still has only the old one. The result is a
``redirect_uri_mismatch`` at the very end of a sign-in, weeks after the move
that caused it, blaming the service rather than the change.

So the address each account was connected at is recorded, and this module
compares it to the address in use now.

**Only accounts that actually have to be reconnected are reported.** A working
account is not affected by the address at all -- its tokens refresh without one
-- so warning about it says "something is wrong" when nothing is, and says it
on every visit from a second address. Reaching Task Hub over a tunnel and over
a port forward is normal and supported; doing so is not a fault and is not
reported as one. The warning appears when an account is in ``NEEDS_AUTH`` and
therefore has to go through sign-in again, which is the only moment the
redirect address is sent and the only moment a stale console entry can bite.

One warning is produced per console entry rather than per account, because that
is the unit of work: every slot of a service shares one OAuth client, so a
single paste fixes all of them. Repeating an identical instruction once per
account made a one-line job look like several.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Account, AccountStatus, ServiceKind


@dataclass(frozen=True)
class RedirectDrift:
    """One account connected at an address that is no longer the one in use."""

    #: Every account this one console entry covers. They share an OAuth client,
    #: so they share the fix.
    account_ids: tuple[int, ...]
    slots: tuple[int, ...]
    identities: tuple[str, ...]
    service_key: str
    service_name: str
    #: The address the account was connected at, and the one its console holds.
    connected_uri: str
    #: The address a reconnection would send instead.
    current_uri: str
    console_name: str
    console_url: str
    #: Set when the console would refuse ``current_uri`` outright. Then adding it
    #: is not the fix and telling the user to add it would waste their evening:
    #: they need a different address altogether before reconnecting.
    current_uri_problem: str | None = None


def _normalised(uri: str) -> str:
    """Compare addresses the way a console does, minus what it does not care about.

    Host and scheme are case-insensitive and a trailing slash is not a
    difference, so treating either as drift would warn about a move that never
    happened. Everything else -- the port, the path, http versus https -- is
    compared exactly, because to a console those are different addresses.
    """
    parts = urlsplit(uri.strip())
    host = (parts.hostname or "").lower()
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit(
        (parts.scheme.lower(), host, parts.path.rstrip("/"), parts.query, "")
    )


def current_redirect_uri(request: Request, service_key: str) -> str:
    """What a connection started right now would send, or "" for other services.

    Imported here rather than at module scope: both setup modules reach back
    into the wider web package, and importing them at the top of a module that
    they may themselves pull in is how an import cycle starts.
    """
    if service_key == ServiceKind.GOOGLE.value:
        from app.web.google_setup import redirect_uri_for as google_redirect_uri

        return google_redirect_uri(request)

    from app.web.oauth_setup import SERVICES
    from app.web.oauth_setup import redirect_uri_for as oauth_redirect_uri

    service = SERVICES.get(service_key)
    return oauth_redirect_uri(request, service) if service else ""


def _console(service_key: str) -> tuple[str, str, str]:
    """Human name, console name and console address for a service."""
    if service_key == ServiceKind.GOOGLE.value:
        return (
            "Google",
            "Google Cloud Console",
            "https://console.cloud.google.com/apis/credentials",
        )
    from app.web.oauth_setup import SERVICES

    service = SERVICES.get(service_key)
    if service is None:
        return service_key.title(), "", ""
    return service.name, service.console_name, service.console_url


def _problem_with(service_key: str, uri: str) -> str | None:
    """Why the console would refuse this address, if it would."""
    if service_key == ServiceKind.GOOGLE.value:
        from app.web.google_setup import redirect_uri_problem as google_problem

        return google_problem(uri)

    from app.web.oauth_setup import SERVICES
    from app.web.oauth_setup import redirect_uri_problem as oauth_problem

    service = SERVICES.get(service_key)
    if service is None:
        return None
    problem = oauth_problem(uri, service)
    # The loopback message is advice about phones, not a refusal -- every
    # console accepts localhost. Treating it as a refusal here would tell the
    # user that the address they are being asked to register cannot be
    # registered, which is the opposite of true.
    if problem and urlsplit(uri).hostname in {"localhost", "127.0.0.1", "::1"}:
        return None
    return problem


def drift_in(request: Request, accounts: list[Account]) -> list[RedirectDrift]:
    """The accounts here that must reconnect, and cannot from this address.

    Grouped by the console entry that would fix them, because one paste fixes
    every slot of a service at once.
    """
    #: (service, connected address, current address) -> the accounts sharing it
    groups: dict[tuple[str, str, str], list[Account]] = {}
    for account in accounts:
        # A working account is unaffected: refreshing a token never sends a
        # redirect address, so the one in the console is irrelevant until the
        # account has to sign in again. Reporting it would call an ordinary
        # second address -- a tunnel alongside a port forward -- a fault.
        if account.status != AccountStatus.NEEDS_AUTH:
            continue

        connected = (account.connected_redirect_uri or "").strip()
        if not connected:
            continue  # Connected before this was recorded, or without a redirect.

        service_key = account.service.value
        current = current_redirect_uri(request, service_key)
        if not current or _normalised(current) == _normalised(connected):
            continue

        groups.setdefault((service_key, connected, current), []).append(account)

    found: list[RedirectDrift] = []
    for (service_key, connected, current), members in groups.items():
        name, console_name, console_url = _console(service_key)
        members.sort(key=lambda a: a.slot)
        found.append(
            RedirectDrift(
                account_ids=tuple(a.id for a in members),
                slots=tuple(a.slot for a in members),
                identities=tuple(a.remote_identity or "" for a in members),
                service_key=service_key,
                service_name=name,
                connected_uri=connected,
                current_uri=current,
                console_name=console_name,
                console_url=console_url,
                current_uri_problem=_problem_with(service_key, current),
            )
        )
    return found


def all_drift(request: Request, db: Session) -> list[RedirectDrift]:
    """Every account that needs reconnecting and cannot, for the overview page.

    Usually empty, and meant to be: it speaks only when an account has already
    stopped working and this address would stop it being fixed.
    """
    accounts = list(
        db.execute(
            select(Account).where(Account.connected_redirect_uri.isnot(None))
        ).scalars()
    )
    return drift_in(request, accounts)
