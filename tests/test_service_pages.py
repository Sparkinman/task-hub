"""Every service page must be able to build its own context.

This exists because the Apple page was broken twice in one afternoon, both times
by a change that looked obviously correct and was never rendered before being
shipped. The pages are behind a login, so a quick look in a browser is not the
casual check it sounds like, and the failures were both the kind a reader's eye
slides over: a name used in a helper that does not receive it, and a template
variable added to the markup before the view supplied it.

Neither needed a browser to catch. Building the context for every service in the
catalogue is enough, and it is fast, so there is no excuse for not doing it on
every run.

What this deliberately does not do is assert what the pages say. It asserts that
they can be assembled at all, for every service, whether or not an account is
connected -- which is the failure that reaches somebody as "Internal Server
Error" with nothing else to go on.
"""

from __future__ import annotations

import sys

from app.db.models import (
    Account, AccountStatus, CollectionKind, RemoteList, ServiceKind,
)
from app.db.session import init_db, session_scope
from app.web.services_view import (
    EMPTY_LIST_NOTES, SERVICE_CATALOGUE, _mapping_context,
)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


init_db()

print("\nEvery service builds a page context with no accounts connected")
# The empty case is not the trivial one. A page with nothing on it still has to
# assemble, and deriving anything from "the first account" is exactly where that
# breaks.
with session_scope() as session:
    for definition in SERVICE_CATALOGUE:
        try:
            context = _mapping_context(session, [])
            check(f"{definition.name} with no accounts", isinstance(context, dict))
        except Exception as exc:  # noqa: BLE001
            check(f"{definition.name} with no accounts", False, repr(exc))

print("\nAnd with an account connected")
with session_scope() as session:
    for slot, definition in enumerate(SERVICE_CATALOGUE, start=1):
        kind = ServiceKind(definition.key)
        account = Account(
            service=kind, slot=90 + slot, label=f"{definition.name} test",
            status=AccountStatus.CONNECTED,
        )
        session.add(account)
        session.flush()
        # A list of each kind the service claims, so the mapping table is built
        # rather than skipped as empty.
        for collection_kind in (CollectionKind.TASKS, CollectionKind.CALENDAR):
            session.add(RemoteList(
                account_id=account.id,
                remote_id=f"probe-{definition.key}-{collection_kind.value}",
                name=f"Probe {collection_kind.value}", kind=collection_kind,
            ))
        session.flush()

        try:
            context = _mapping_context(session, [account])
            missing = [
                key for key in ("dropdowns", "collections", "kind_enabled",
                                "empty_notes", "lists_by_account")
                if key not in context
            ]
            check(f"{definition.name} with an account",
                  not missing, f"context missing {missing}")
        except Exception as exc:  # noqa: BLE001
            check(f"{definition.name} with an account", False, repr(exc))
        session.rollback()

print("\nThe notes that explain an empty section name a real service and kind")
for key, notes in EMPTY_LIST_NOTES.items():
    check(f"{key} is a service in the catalogue",
          any(d.key == key for d in SERVICE_CATALOGUE), key)
    for note_key, text in notes.items():
        base = note_key.removesuffix("_alternative")
        check(f"{key}.{note_key} names a real collection kind",
              base in {k.value for k in CollectionKind}, base)
        check(f"{key}.{note_key} actually says something", len(text.strip()) > 40)

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All service page tests passed.")
