"""A calendar client must be able to find the CalDAV service without a login.

RFC 6764 says a client given only a server name asks for ``/.well-known/caldav``
and follows the redirect. Task Hub mounts Radicale at ``/radicale``, which no
client could guess, so that redirect is the only way an iPhone ever finds it.

This was found by an iPhone failing to add the account. The well-known path was
not a route, so it fell through to the session gate and was answered with a
redirect to the HTML login page -- and iOS, handed a login form where it expected
a DAV collection, reports the account as unusable in a way indistinguishable from
a wrong password. The person adding it retyped the password, reset it, retyped it
again, and the password had been right the whole time.

Two things therefore have to hold, and the second is the one that broke:

- the redirect points at the Radicale mount
- it is answered **without a session**, because a CalDAV client has none and
  never will -- it authenticates against Radicale's htpasswd with HTTP Basic
"""

from __future__ import annotations

import sys

from app.config import RADICALE_MOUNT_PATH
from app.web import wellknown
from app.web.deps import is_public_path

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


print("\nThe discovery paths are served without a login")
for path in ("/.well-known/caldav", "/.well-known/carddav"):
    check(f"{path} is public", is_public_path(path),
          "a client with no session would be sent to the login page")

print("\nAnd the gate has not been widened by accident")
# The prefix match must stop at a separator, or anything merely starting with
# the same letters becomes public too. This is the same fault that once let
# /radicale-admin through on the strength of /radicale.
for path in ("/.well-knownsomething", "/.well-known-admin", "/settings"):
    check(f"{path} is still behind the login", not is_public_path(path))

print("\nEach discovery route redirects to the CalDAV mount")
for name, handler in (("caldav", wellknown.caldav_discovery),
                      ("carddav", wellknown.carddav_discovery)):
    response = handler()
    target = response.headers.get("location", "")
    check(f"{name} redirects to {RADICALE_MOUNT_PATH}/",
          target == f"{RADICALE_MOUNT_PATH}/", f"went to {target!r}")
    check(f"{name} answers 301, which lets a client stop asking",
          response.status_code == 301, str(response.status_code))

print("\nThe routes answer the methods a client actually uses")
# iOS sends PROPFIND at discovery, not GET. A route registered for GET alone
# answers 405 to the request that matters, which is how this looked fixed
# while still being broken.
paths = {}
for route in wellknown.router.routes:
    paths.setdefault(route.path, set()).update(route.methods or set())
for path in ("/.well-known/caldav", "/.well-known/carddav"):
    methods = paths.get(path, set())
    for method in ("GET", "PROPFIND", "OPTIONS"):
        check(f"{path} answers {method}", method in methods,
              f"it answers {sorted(methods)}")

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All CalDAV discovery tests passed.")
