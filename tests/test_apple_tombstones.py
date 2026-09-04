"""Apple's "these reminders have moved" placeholders must not look like lists.

When an Apple ID's Reminders are upgraded, the real reminders move to a store
CalDAV cannot reach and Apple leaves the old list behind with a warning sign in
its name and two placeholder items inside it. Offering that to be mapped is
worse than showing nothing: it looks like a working list, and syncing it copies
two meaningless sentences into every other service the user has connected.

The strings below are not invented. They are exactly what a real upgraded iCloud
account returned:

    'Family ⚠️'      -> 'Where are my reminders?'
                        'The creator of this list has upgraded these reminders.'
    'Reminders ⚠️'   -> the same two

The test that matters most here is the one that keeps a list. Hiding somebody's
real reminders because they happened to put a warning sign in the name would be
a far worse fault than showing a dead list, so the marker alone is never enough.
"""

from __future__ import annotations

import sys

from app.connectors.base import PullResult, RemoteItem
from app.connectors.caldav_remote import AppleConnector
from app.db.models import CollectionKind
from app.services.ical_model import CanonicalRecord

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def connector_returning(titles: list[str] | None, raises: bool = False):
    """An Apple connector whose pull returns tasks with these titles."""
    apple = AppleConnector(
        account_id=0, credentials={"username": "u", "password": "p"}
    )
    calls: list[str] = []

    def fake_pull(remote_list_id, kind, since=None, state=None):
        calls.append(remote_list_id)
        if raises:
            raise RuntimeError("the server said no")
        return PullResult(
            items=[
                RemoteItem(
                    remote_id=f"item-{n}",
                    record=CanonicalRecord(
                        uid="", kind=CollectionKind.TASKS, title=title
                    ),
                    fields_present=frozenset(),
                )
                for n, title in enumerate(titles or [])
            ],
            incremental=False,
        )

    apple.pull = fake_pull  # type: ignore[method-assign]
    return apple, calls


REAL_PLACEHOLDERS = [
    "Where are my reminders?",
    "The creator of this list has upgraded these reminders.",
]

print("\nA marked list holding only Apple's placeholders is a headstone")
apple, calls = connector_returning(REAL_PLACEHOLDERS)
check("the pair a real upgraded account returned is recognised",
      apple._is_upgrade_tombstone("url", "Reminders ⚠️"))
check("recognised in the other order too",
      connector_returning(list(reversed(REAL_PLACEHOLDERS)))[0]
      ._is_upgrade_tombstone("url", "Family ⚠️"))
apple, _ = connector_returning([t.upper() for t in REAL_PLACEHOLDERS])
check("matched whatever the casing", apple._is_upgrade_tombstone("url", "X ⚠️"))
apple, _ = connector_returning([f"  {REAL_PLACEHOLDERS[0]}  "])
check("matched with surrounding whitespace",
      apple._is_upgrade_tombstone("url", "X ⚠️"))

print("\nAnything that might be somebody's real list is kept")
apple, _ = connector_returning(REAL_PLACEHOLDERS + ["Buy milk"])
check("a real task among the placeholders keeps the list",
      not apple._is_upgrade_tombstone("url", "Shopping ⚠️"),
      "a list with real tasks in it was about to be hidden")

apple, _ = connector_returning([])
check("an empty marked list is kept, not assumed dead",
      not apple._is_upgrade_tombstone("url", "Empty ⚠️"))

apple, _ = connector_returning(["Urgent things"])
check("a list the user marked themselves is kept",
      not apple._is_upgrade_tombstone("url", "Urgent ⚠️"))

apple, _ = connector_returning(None, raises=True)
check("a list that cannot be read is kept rather than hidden",
      not apple._is_upgrade_tombstone("url", "Unreadable ⚠️"),
      "an unreadable list must never be silently dropped")

print("\nAn unmarked list is never even opened")
apple, calls = connector_returning(REAL_PLACEHOLDERS)
result = apple._is_upgrade_tombstone("url", "Reminders")
check("a name without the warning sign is not a tombstone", not result)
check("and no request was made to find that out", calls == [],
      f"it fetched {calls}")

print("\nThe marker is matched on the warning sign itself")
apple, _ = connector_returning(REAL_PLACEHOLDERS)
check("the bare sign counts, with or without the emoji variation selector",
      apple._is_upgrade_tombstone("url", "Reminders ⚠"))

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All Apple tombstone tests passed.")
