"""Noticing that Task Hub moved after a service was connected.

The failure this guards against is slow. Connecting Google over an SSH port
forward and later reaching Task Hub through a Cloudflare tunnel breaks nothing
at the time -- renewing a token never sends an address -- so everything syncs
for weeks. It breaks the next reconnection, which arrives as
``redirect_uri_mismatch`` at the end of a sign-in, pointing at Google rather
than at the move.

Two ways to get this wrong, and both are tested here. Warning when nothing
changed trains the user to ignore the banner, so a differently-spelled version
of the same address must stay silent. Warning without checking whether the new
address is one the console would even accept sends the user off to paste
``http://192.168.1.50:8080/...`` into Google, which refuses it -- so that case
has to say something different.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

from fastapi import Request

from app.db.models import Account, AccountStatus, ServiceKind
from app.db.session import init_db, session_scope
from app.web.redirect_drift import drift_in

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def request_at(base: str) -> Request:
    """A request that arrived on ``base``, which is all the drift check reads."""
    parts = urlsplit(base)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": parts.scheme,
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", parts.netloc.encode())],
            "server": (parts.hostname, port),
            "client": ("127.0.0.1", 40000),
        }
    )


init_db()


def account_connected_at(session, kind: ServiceKind, slot: int, uri: str | None):
    account = Account(
        service=kind,
        slot=slot,
        label=f"{kind.value} drift test",
        status=AccountStatus.CONNECTED,
        remote_identity="someone@example.com",
        connected_redirect_uri=uri,
    )
    session.add(account)
    session.flush()
    return account


print("\nAn account connected at the address in use is not drift")
with session_scope() as session:
    account = account_connected_at(
        session, ServiceKind.GOOGLE, 91,
        "https://tasks.example.com/oauth/google/callback",
    )
    found = drift_in(request_at("https://tasks.example.com"), [account])
    check("same address is silent", found == [], repr(found))

    # The upgrade case: every account that existed before the column did has no
    # recorded address. Guessing one would warn about a move that may never have
    # happened, on every install, immediately after an update.
    legacy = account_connected_at(session, ServiceKind.GOOGLE, 92, None)
    check(
        "no recorded address is silent",
        drift_in(request_at("https://tasks.example.com"), [legacy]) == [],
    )

    blank = account_connected_at(session, ServiceKind.GOOGLE, 93, "")
    check(
        "an empty recorded address is silent",
        drift_in(request_at("https://tasks.example.com"), [blank]) == [],
    )
    session.rollback()

print("\nSpelling differences a console ignores are not drift either")
with session_scope() as session:
    for stored, now, why in [
        ("https://Tasks.Example.com/oauth/google/callback",
         "https://tasks.example.com", "host case"),
        ("https://tasks.example.com/oauth/google/callback/",
         "https://tasks.example.com", "trailing slash"),
        ("HTTPS://tasks.example.com/oauth/google/callback",
         "https://tasks.example.com", "scheme case"),
    ]:
        account = account_connected_at(session, ServiceKind.GOOGLE, 94, stored)
        found = drift_in(request_at(now), [account])
        check(f"{why} is not a move", found == [], repr(found))
        session.rollback()

print("\nDifferences a console does care about are drift")
with session_scope() as session:
    for stored, now, why in [
        ("https://tasks.example.com/oauth/google/callback",
         "https://other.example.com", "a different host"),
        ("http://localhost:8080/oauth/google/callback",
         "http://localhost:9090", "a different port"),
        ("http://tasks.example.com/oauth/google/callback",
         "https://tasks.example.com", "http versus https"),
    ]:
        account = account_connected_at(session, ServiceKind.GOOGLE, 95, stored)
        found = drift_in(request_at(now), [account])
        check(f"{why} is reported", len(found) == 1, repr(found))
        session.rollback()

print("\nThe warning carries what the user has to paste")
with session_scope() as session:
    account = account_connected_at(
        session, ServiceKind.GOOGLE, 96,
        "http://localhost:8080/oauth/google/callback",
    )
    found = drift_in(request_at("https://tasks.example.com"), [account])
    check("one account drifted", len(found) == 1)
    if found:
        drift = found[0]
        check(
            "the new address is the full callback URI",
            drift.current_uri == "https://tasks.example.com/oauth/google/callback",
            drift.current_uri,
        )
        check(
            "the old address is preserved as recorded",
            drift.connected_uri == "http://localhost:8080/oauth/google/callback",
            drift.connected_uri,
        )
        check("the console is named", bool(drift.console_name), drift.console_name)
        check(
            "an address Google accepts raises no objection",
            drift.current_uri_problem is None,
            str(drift.current_uri_problem),
        )
    session.rollback()

print("\nMoving to an address the console would refuse says so instead")
with session_scope() as session:
    # Connected over a port forward, now being used on the LAN. Telling this
    # user to add http://192.168.1.50:8080/... to Google Cloud Console would
    # send them to a form that refuses it, twice: not HTTPS, and a bare number.
    account = account_connected_at(
        session, ServiceKind.GOOGLE, 97,
        "http://localhost:8080/oauth/google/callback",
    )
    found = drift_in(request_at("http://192.168.1.50:8080"), [account])
    check("the move is still reported", len(found) == 1, repr(found))
    if found:
        check(
            "and Google's objection is carried with it",
            bool(found[0].current_uri_problem),
            str(found[0].current_uri_problem),
        )
    session.rollback()

print("\nMoving to localhost is a move, not a problem")
with session_scope() as session:
    # Every console accepts localhost. Task Hub warns about it as advice for
    # people setting up a phone, and treating that advice as a refusal would
    # tell the user the address cannot be registered when it is the one address
    # that always can be.
    account = account_connected_at(
        session, ServiceKind.GOOGLE, 98,
        "https://tasks.example.com/oauth/google/callback",
    )
    found = drift_in(request_at("http://localhost:8080"), [account])
    check("the move is reported", len(found) == 1, repr(found))
    if found:
        check(
            "with nothing said against localhost",
            found[0].current_uri_problem is None,
            str(found[0].current_uri_problem),
        )
    session.rollback()

print("\nEvery OAuth service is understood, not just Google")
with session_scope() as session:
    for slot, kind in enumerate(
        (ServiceKind.TODOIST, ServiceKind.TICKTICK, ServiceKind.MICROSOFT),
        start=80,
    ):
        account = account_connected_at(
            session, kind, slot,
            f"http://localhost:8080/oauth/{kind.value}/callback",
        )
        found = drift_in(request_at("https://tasks.example.com"), [account])
        check(f"{kind.value} drift is reported", len(found) == 1, repr(found))
        if found:
            check(
                f"{kind.value} names its own console",
                bool(found[0].console_name) and bool(found[0].console_url),
                repr(found[0]),
            )
            check(
                f"{kind.value} points at its own callback path",
                found[0].current_uri.endswith(f"/oauth/{kind.value}/callback"),
                found[0].current_uri,
            )
        session.rollback()

print("\nServices that never use a redirect are ignored")
with session_scope() as session:
    # Apple, CalDAV, Things and Obsidian sign in with a password or a file path.
    # There is no console holding an address, so there is nothing to warn about
    # even if somebody contrived to record one.
    account = account_connected_at(
        session, ServiceKind.APPLE, 99, "http://localhost:8080/whatever",
    )
    found = drift_in(request_at("https://tasks.example.com"), [account])
    check("a password service is silent", found == [], repr(found))
    session.rollback()

print("\nThe banner renders, and says the right thing in each case")
# The logic being right is no use if the partial cannot render it. This is the
# failure that reaches somebody as "Internal Server Error" on the overview page,
# and it costs a millisecond to rule out.
from jinja2 import Environment, FileSystemLoader  # noqa: E402

from app.web.redirect_drift import RedirectDrift  # noqa: E402

_template = Environment(
    loader=FileSystemLoader("app/templates")
).get_template("partials/redirect_drift.html")

check("silent with an empty list", _template.render(drift=[]).strip() == "")
check(
    "silent on a page that never passes it at all",
    _template.render().strip() == "",
)

_addable = RedirectDrift(
    account_id=1, service_key="google", service_name="Google", slot=1,
    identity="someone@example.com",
    connected_uri="http://localhost:8080/oauth/google/callback",
    current_uri="https://tasks.example.com/oauth/google/callback",
    console_name="Google Cloud Console",
    console_url="https://console.cloud.google.com/apis/credentials",
)
_rendered = _template.render(drift=[_addable])
check("the old address is shown", _addable.connected_uri in _rendered)
check("the new address is offered to copy", _addable.current_uri in _rendered)
check("the console is linked", _addable.console_url in _rendered)
check("it says to keep both", "alongside the one already there" in _rendered)

_refused = RedirectDrift(
    account_id=2, service_key="google", service_name="Google", slot=2,
    identity="",
    connected_uri="http://localhost:8080/oauth/google/callback",
    current_uri="http://192.168.1.50:8080/oauth/google/callback",
    console_name="Google Cloud Console",
    console_url="https://console.cloud.google.com/apis/credentials",
    current_uri_problem="Google will reject this address because it is a raw IP address.",
)
_rendered = _template.render(drift=[_refused])
check("a refused address is not offered for pasting", "Copy" not in _rendered)
check(
    "and the reason is given instead",
    "would not help" in _rendered and "raw IP address" in _rendered,
)

if _failures:
    print(f"\n{len(_failures)} check(s) failed.")
    sys.exit(1)
print("\nAll redirect drift checks passed.")
