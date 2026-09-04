"""Each OAuth callback route must hand its arguments over correctly.

The Microsoft callback passed them positionally in a different order from the
other two, so the database session arrived where the error message was expected.
A session object is truthy, so every Microsoft connection was refused at the
first line -- before the authorization code was looked at -- and the refusal
said "Microsoft refused the connection: <sqlalchemy.orm.session.Session object>"
to a log nobody was reading. From the outside it was a sign-in that returned to
the setup page having done nothing at all, identically, every time.

It had never worked. Not once, for anybody.

Nothing caught it because nothing tested how the routes call the handler: the
handler's own logic was fine, the routes were declared correctly, and the join
between them was where the fault lived. The arguments are keyword-only now, so
the same mistake cannot be made again -- and this checks the wiring rather than
trusting that.
"""

from __future__ import annotations

import sys

from app.db.models import ServiceKind
from app.web import oauth_setup

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


ROUTES = {
    ServiceKind.TODOIST: oauth_setup.todoist_callback,
    ServiceKind.TICKTICK: oauth_setup.ticktick_callback,
    ServiceKind.MICROSOFT: oauth_setup.microsoft_callback,
}

SENTINEL_DB = object()

print("\nEvery callback route hands over what it was given, unshuffled")
for kind, route in ROUTES.items():
    captured = {}

    def spy(service, request, *, db, code, state, error):
        captured.update(service=service, request=request, db=db,
                        code=code, state=state, error=error)
        return "ok"

    original = oauth_setup._handle_callback
    oauth_setup._handle_callback = spy
    try:
        route(request="REQUEST", code="THE-CODE", state="THE-STATE",
              error="THE-ERROR", db=SENTINEL_DB)
    finally:
        oauth_setup._handle_callback = original

    check(f"{kind.value}: the service is its own",
          captured.get("service") is oauth_setup.SERVICES[kind.value],
          str(captured.get("service")))
    check(f"{kind.value}: the code arrives as the code",
          captured.get("code") == "THE-CODE", repr(captured.get("code")))
    check(f"{kind.value}: the state arrives as the state",
          captured.get("state") == "THE-STATE", repr(captured.get("state")))
    # The one that was wrong. A database session here is truthy, and the handler
    # treats any truthy error as the service having refused the sign-in.
    check(f"{kind.value}: the error is a message, not the database session",
          captured.get("error") == "THE-ERROR", repr(captured.get("error")))
    check(f"{kind.value}: the session arrives as the session",
          captured.get("db") is SENTINEL_DB, repr(captured.get("db")))

print("\nAnd the handler refuses to be called positionally at all")
try:
    oauth_setup._handle_callback(
        oauth_setup.SERVICES["todoist"], "REQUEST", SENTINEL_DB, "c", "s", None)
    check("positional arguments are rejected", False,
          "the signature still accepts the shape that caused this")
except TypeError:
    check("positional arguments are rejected", True)

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All OAuth callback wiring tests passed.")
