"""The generic CalDAV service: any server, not just iCloud.

The connector underneath has been syncing a live iCloud account for a while --
Apple *is* this code with a fixed address -- so what is new here is the wiring
that lets somebody point it at their own Nextcloud, and the wiring is where the
mistakes are: an address that is never asked for, a username box prefilled with
a display name that will not sign in, an account that cannot be told apart from
another on a different server.
"""

from __future__ import annotations

import sys

from app.connectors.base import ConnectorError
from app.connectors.caldav_remote import AppleConnector, RemoteCalDAVConnector
from app.db.models import SERVICE_DISPLAY_NAMES, ServiceKind
from app.web.password_setup import SERVICES, normalise_server_url, service_for
from app.web.services_view import SERVICES_BY_KEY

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


CREDS = {"username": "paul", "password": "secret", "url": "https://cloud.example.com"}

print("\nCalDAV is a service in its own right")
definition = SERVICES_BY_KEY.get(ServiceKind.CALDAV.value)
check("it is in the catalogue", definition is not None)
check("it offers both kinds",
      definition.supports_tasks and definition.supports_calendar)
check("it has a setup guide", definition.docs_slug == "caldav")
check("it is not hidden behind a phase that has not shipped",
      definition.phase <= 6, definition.phase)
check("it has its own badge colour, not Apple's",
      definition.colour != SERVICES_BY_KEY[ServiceKind.APPLE.value].colour)
check("and a name of its own",
      SERVICE_DISPLAY_NAMES[ServiceKind.CALDAV.value] == "CalDAV")

print("\nIt asks for a server address, and Apple does not")
caldav_service = service_for(ServiceKind.CALDAV.value)
check("CalDAV is a password service", caldav_service is not None)
check("it asks for the address", caldav_service.needs_url)
check("Apple does not", not service_for(ServiceKind.APPLE.value).needs_url)
check("neither does Things", not service_for(ServiceKind.THINGS3.value).needs_url)
check("every password service has both field labels",
      all(s.user_label and s.secret_label for s in SERVICES.values()))

print("\nWhat somebody types into the address box")
check("a bare hostname becomes https",
      normalise_server_url("cloud.example.com") == "https://cloud.example.com")
check("https is left alone",
      normalise_server_url("https://cloud.example.com") == "https://cloud.example.com")
# Deliberate: a server on your own network, where the connector also relaxes
# its TLS requirement. Silently upgrading it to https would break exactly the
# case the allowance exists for.
check("plain http is left alone, on purpose",
      normalise_server_url("http://192.168.1.42:5232") == "http://192.168.1.42:5232")
check("surrounding space is trimmed",
      normalise_server_url("  https://dav.example.com  ") == "https://dav.example.com")
check("nothing stays nothing", normalise_server_url("") == "")
check("and so does whitespace", normalise_server_url("   ") == "")

print("\nThe connector refuses to run half-configured")
for missing, expected in (
    ({"username": "paul", "password": "p"}, "server address"),
    ({"password": "p", "url": "https://x.example"}, "sign-in"),
    ({"username": "paul", "url": "https://x.example"}, "sign-in"),
):
    try:
        RemoteCalDAVConnector(1, missing)
        check(f"missing {expected} is refused", False, "no error raised")
    except ConnectorError as exc:
        check(f"missing {expected} is refused", expected in str(exc), str(exc))

print("\nAn account is named so two servers can be told apart")
connector = RemoteCalDAVConnector(1, CREDS)
check("the host is part of the name",
      connector.identity() == "paul at cloud.example.com", connector.identity())
apple = AppleConnector(2, {"username": "paul@icloud.com", "password": "x"})
check("Apple stays just the Apple ID",
      apple.identity() == "paul@icloud.com", apple.identity())
check("and Apple still defaults to iCloud without being told",
      "icloud.com" in apple.base_url, apple.base_url)

print("\nThe two services stamp their own origin")
check("a generic account stamps CalDAV",
      RemoteCalDAVConnector.service == ServiceKind.CALDAV)
check("an iCloud account stamps Apple", AppleConnector.service == ServiceKind.APPLE)

print("\nThe engine knows how to build it")
# Imported here rather than at the top: build_connector pulls in every
# connector module, and the point is that the CALDAV branch exists at all.
from app.sync.engine import build_connector  # noqa: E402

source = build_connector.__doc__ or ""
import inspect  # noqa: E402

body = inspect.getsource(build_connector)
check("there is a branch for it", "ServiceKind.CALDAV" in body)
check("and it builds the generic connector, not Apple's",
      "RemoteCalDAVConnector(" in body)

print("\nMail provider settings, since a typed server name is where email fails")
from app.services.mail_providers import (  # noqa: E402
    PROVIDERS, correct_host, suggest_address,
)

check("Gmail is offered with the right server",
      any(p.host == "smtp.gmail.com" for p in PROVIDERS))
check("every provider names a security that exists",
      all(p.security in ("starttls", "ssl", "none") for p in PROVIDERS))
check("every provider says which password to use",
      all(p.username_hint for p in PROVIDERS))

# The real mistake this came from: a name that looks exactly right, does not
# exist, and fails as a connection error pointing at the network instead.
host, note = correct_host("smtp.google.com")
check("smtp.google.com is corrected to Gmail's real server",
      host == "smtp.gmail.com", host)
check("and the correction is explained", "smtp.gmail.com" in note)
check("a correct server is left exactly alone",
      correct_host("smtp.gmail.com") == ("smtp.gmail.com", ""))
check("so is a server nobody here has heard of",
      correct_host("mail.mycompany.example") == ("mail.mycompany.example", ""))
check("case and a stray scheme do not defeat it",
      correct_host("HTTPS://SMTP.GOOGLE.COM/")[0] == "smtp.gmail.com")

print("\nAnd a recipient that will bounce is flagged rather than rewritten")
# @google.com is a real domain with no mailbox for you, so the message is
# accepted and bounces minutes later -- after the page has said it worked.
check("a lookalike domain is questioned",
      "gmail.com" in suggest_address("someone@google.com"))
check("the real thing is not", suggest_address("someone@gmail.com") == "")
check("nor is an ordinary domain", suggest_address("paul@example.org") == "")
check("nothing is not an address", suggest_address("") == "")

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All CalDAV service tests passed.")
