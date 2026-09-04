"""The address rules each OAuth console applies, as a table.

Task Hub builds its redirect URI from the address the browser actually used, so
the same image works unchanged over an SSH port forward, a LAN address,
Tailscale, a Cloudflare tunnel or somebody's own domain. Building the right URI
is only half the job: each console then refuses some of those shapes, and the
refusal arrives as an opaque error at the very end of the flow, long after the
mistake was made.

This is the table from docs/addresses.md, executable. If the two ever disagree,
one of them is lying to somebody who is about to spend an evening on it.

The rules are not the same shape for every service, which is the whole reason
they are worth testing rather than assuming: TickTick wants a name but tolerates
plain http, Microsoft wants https but tolerates a bare number, Google wants
both, and Todoist minds neither.
"""

from __future__ import annotations

import sys

from app.web.google_setup import redirect_uri_problem as google_problem
from app.web.oauth_setup import SERVICES, redirect_uri_problem as oauth_problem

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


#: base address -> which services will refuse it. Taken straight from the table
#: in docs/addresses.md. Loopback is a special case handled separately: every
#: console accepts it, and Task Hub warns about it only as advice about phones.
ACCEPTS = {
    "http://192.168.1.50:8080": {
        "Google": False, "Microsoft": False, "Todoist": True, "TickTick": False,
    },
    "http://192-168-1-50.sslip.io:8080": {
        "Google": False, "Microsoft": False, "Todoist": True, "TickTick": True,
    },
    "https://name.tailnet.ts.net": {
        "Google": True, "Microsoft": True, "Todoist": True, "TickTick": True,
    },
    "https://tasks.example.com": {
        "Google": True, "Microsoft": True, "Todoist": True, "TickTick": True,
    },
    # A Cloudflare tunnel is a name over HTTPS, so every console takes it. It is
    # listed separately because it is the path most people will actually use.
    "https://sudden-words-1234.trycloudflare.com": {
        "Google": True, "Microsoft": True, "Todoist": True, "TickTick": True,
    },
}


def accepted_by(service_name: str, base: str) -> bool:
    """Whether Task Hub thinks this service would accept this address."""
    if service_name == "Google":
        return google_problem(f"{base}/oauth/google/callback") is None
    service = next(s for s in SERVICES.values() if s.name == service_name)
    return oauth_problem(f"{base}{service.callback_path}", service) is None


print("\nThe table from docs/addresses.md, checked against the code")
for base, expectations in ACCEPTS.items():
    print(f"\n  {base}")
    for service_name, should_accept in expectations.items():
        actual = accepted_by(service_name, base)
        check(
            f"{service_name} {'accepts' if should_accept else 'refuses'} it",
            actual == should_accept,
            f"the code says it would {'accept' if actual else 'refuse'} it",
        )

print("\nLoopback is exempt everywhere, because every console carves it out")
for service_name in ("Google", "Microsoft", "Todoist", "TickTick"):
    # Google returns no warning at all; the others return advice about phones
    # rather than a refusal. What must never happen is a message claiming the
    # service will reject an address it in fact accepts -- that would send
    # somebody off to fix a setup that already worked.
    if service_name == "Google":
        check("Google accepts http://localhost",
              accepted_by("Google", "http://localhost:8080"))
        continue
    service = next(s for s in SERVICES.values() if s.name == service_name)
    message = oauth_problem(
        f"http://localhost:8080{service.callback_path}", service) or ""
    check(f"{service_name}'s localhost note does not claim a rejection",
          "reject" not in message.lower(),
          message)
    check(f"{service_name}'s localhost note says it is accepted",
          "accepts it" in message, message)

print("\nA bare IP is named as such, and a way out is offered")
service = next(s for s in SERVICES.values() if s.name == "TickTick")
message = oauth_problem(
    f"http://192.168.1.50:8080{service.callback_path}", service) or ""
check("TickTick's message offers the sslip.io name for that exact address",
      "192-168-1-50.sslip.io" in message, message)

print("\nAn address Task Hub cannot work out at all is reported, not ignored")
for service in SERVICES.values():
    check(f"{service.name} reports an empty host",
          oauth_problem("http:///oauth/x/callback", service) is not None)

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All redirect URI rules match the documentation.")
