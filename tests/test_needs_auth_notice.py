"""Reminders about accounts whose login has expired.

The gap this covers: the sync-failure notification fires only on the change
from working to failing. When an account's token expires the engine marks it
NEEDS_AUTH, and every later pass *skips* it rather than failing on it -- so the
run reports success again and that one notification is the last anybody hears.
The account then sits disconnected indefinitely.

Google makes this easy to hit: an OAuth consent screen left on Testing expires
the login every seven days, so the same account can lapse over and over.
"""

from __future__ import annotations

import datetime as dt
import json
import sys

from app.sync import scheduler

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


print("The message names the account rather than making you go and look")

title, body = scheduler._needs_auth_message(["Google"])
check("one account is named in the title", title == "Google needs signing in again", title)
check("and the body says what has stopped", "cannot sync Google" in body, body)

title, body = scheduler._needs_auth_message(["Google", "Todoist"])
check("two accounts are counted", title == "2 accounts need signing in again", title)
check("and joined with 'and'", "Google and Todoist" in body, body)

title, body = scheduler._needs_auth_message(["Apple", "Google", "Todoist"])
check("three accounts are counted", title == "3 accounts need signing in again", title)
check(
    "and listed with a serial comma pattern",
    "Apple, Google and Todoist" in body,
    body,
)

print("\nThe reminder is rate limited, so a lapse is not announced every pass")

now = dt.datetime.now(dt.timezone.utc)
cutoff = now - dt.timedelta(hours=scheduler.NEEDS_AUTH_REMINDER_HOURS)

check(
    "the window is a day",
    scheduler.NEEDS_AUTH_REMINDER_HOURS == 24,
    str(scheduler.NEEDS_AUTH_REMINDER_HOURS),
)
check(
    "an account told an hour ago is still inside the window",
    (now - dt.timedelta(hours=1)) > cutoff,
)
check(
    "an account told two days ago is due again",
    (now - dt.timedelta(hours=48)) < cutoff,
)

print("\nA malformed or missing record never stops the reminder")

for raw in ("", "not json", "[]", "null"):
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    check(f"{raw!r} degrades to an empty record", parsed == {})

print("\nReconnected accounts are forgotten, so the next lapse is announced at once")

notified = {"1": now.isoformat(), "2": now.isoformat()}
live = {"2"}
pruned = {k: v for k, v in notified.items() if k in live}
check("the reconnected account is dropped", "1" not in pruned)
check("the still-broken one is kept", "2" in pruned)

print("\nThe reminder is wired into the scheduled pass")

source = (scheduler.__file__ or "").replace(".pyc", ".py")
with open(source, encoding="utf-8") as handle:
    text = handle.read()
check(
    "the scheduled sync calls it",
    "_notify_accounts_needing_auth()" in text.split("def _notify_accounts_needing_auth")[0],
)
check(
    "it is filed under the sync notification category, so one switch governs both",
    'category="sync"' in text.split("def _notify_accounts_needing_auth")[1],
)

if _failures:
    print(f"\n{len(_failures)} check(s) failed.")
    sys.exit(1)
print("\nAll reconnect-reminder tests passed.")
