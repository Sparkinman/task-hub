"""Why does a second sync pass write anything at all?

A small live run whose only job is to answer that. It builds the same throwaway
containers as :mod:`tests.stress_live`, seeds a handful of items, runs two
passes, and records for the second pass exactly which fields the merge accepted
as changed and which services were then written to -- with the before and after
values, so the cause is visible rather than inferred.

Deliberately tiny. The question is what changes, not how fast.

    docker compose cp tests taskhub:/app/
    docker compose exec -e PYTHONPATH=/app -e TASKHUB_DATA_DIR=/tmp/diag \\
        -w /app taskhub python -m tests.diagnose_rewrite
"""

from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict

os.environ.setdefault("STRESS_TASKS", "8")
os.environ.setdefault("STRESS_EVENTS", "8")

import tests.stress_live as SL  # noqa: E402  (prepares the isolated data dir)

from app.db.models import CollectionKind, ServiceKind  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.sync import engine as engine_module  # noqa: E402
from app.sync.engine import SyncEngine  # noqa: E402

#: Field changes the merge accepted, per pass: (pass, service, field) -> samples
ACCEPTED: dict[tuple[int, str, str], list[str]] = defaultdict(list)
#: Pushes actually issued, per pass: (pass, service, action) -> count
PUSHES: Counter[tuple[int, str, str]] = Counter()

CURRENT_PASS = 0


def instrument() -> None:
    original_merge = engine_module.merge_remote
    original_push = SyncEngine._push_one

    def merge_spy(canonical, remote, source, provenance, now=None, baseline=None):
        result = original_merge(canonical, remote, source, provenance, now, baseline)
        for change in result.changes:
            key = (CURRENT_PASS, source.value, change.field)
            if len(ACCEPTED[key]) < 4:
                ACCEPTED[key].append(f"{change.old!r} -> {change.new!r}")
        return result

    def push_spy(self, group, part, item, record, stats):
        before = stats.pushed
        had_link = bool(self._links_in_list(item.id, part.account.id, part.remote_list.id))
        original_push(self, group, part, item, record, stats)
        if stats.pushed > before:
            PUSHES[(CURRENT_PASS, part.account.service.value,
                    "update" if had_link else "create")] += 1

    engine_module.merge_remote = merge_spy
    SyncEngine._push_one = push_spy


def run_pass(index: int):
    global CURRENT_PASS
    CURRENT_PASS = index
    with session_scope() as session:
        return SyncEngine(session).run_sync(trigger="diagnose")


def main() -> int:
    print("Second-pass rewrite diagnosis")
    print("=" * 74)

    engine_module.ensure_radicale_account = lambda session: None
    instrument()

    try:
        with session_scope() as session:
            made = SL.create_containers(session)
            SL.wire_groups(session, made)

        with session_scope() as session:
            _, radicale = SL.connector_for(session, ServiceKind.RADICALE)
            SL.seed_caldav(radicale.client, made["radicale_tasks"][1])
            SL.seed_events(radicale.client, made["radicale_calendar"][1])

        for index in (1, 2, 3):
            run = run_pass(index)
            print(f"\nPass {index}: pulled {run.items_pulled}, pushed "
                  f"{run.items_pushed}, errors {run.errors}")
            pushes = {k[1:]: v for k, v in PUSHES.items() if k[0] == index}
            for (service, action), count in sorted(pushes.items()):
                print(f"    push  {service:10} {action:7} {count}")
            changes = {k[1:]: v for k, v in ACCEPTED.items() if k[0] == index}
            if changes:
                print("    merge accepted these field changes:")
                for (service, field_name), samples in sorted(changes.items()):
                    total = sum(1 for k in ACCEPTED if k[0] == index
                                and k[1] == service and k[2] == field_name)
                    print(f"      {service:10} {field_name:10} "
                          f"({total} distinct key) e.g. {samples[0]}")
                    for sample in samples[1:3]:
                        print(f"      {'':10} {'':10}      {sample}")
            else:
                print("    merge accepted no field changes")

    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    finally:
        print("\n" + "=" * 74)
        print("Teardown")
        SL.teardown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
