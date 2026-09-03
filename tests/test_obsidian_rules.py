"""Changing what counts as a task must never delete anything.

A vault's task rule is the vault's decision. Someone sets a Tasks-plugin global
filter, or renames it from #task to #todo, and every line that no longer matches
disappears from the next pull. That is indistinguishable, to the sync engine,
from the user having deleted those tasks -- and acting on it would tombstone
them out of Todoist, Google and everywhere else at once.

These tests hold the connector to reporting such a pass as incremental, which is
what stops absence being read as deletion for the one pass where absence only
means "the rule changed".
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from app.connectors.obsidian import ObsidianConnector
from app.db.models import CollectionKind

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def build_vault(tmp: Path, global_filter: str | None) -> None:
    """A vault with three dated lines: two tagged #task, one tagged #todo."""
    (tmp / "Notes").mkdir(parents=True, exist_ok=True)
    (tmp / "Notes" / "Work.md").write_text(
        "- [ ] #task First 📅 2026-09-15\n"
        "- [ ] #task Second 📅 2026-09-16\n"
        "- [ ] #todo Third 📅 2026-09-17\n",
        encoding="utf-8",
    )
    plugins = tmp / ".obsidian" / "plugins" / "obsidian-tasks-plugin"
    if global_filter is None:
        import shutil
        shutil.rmtree(plugins, ignore_errors=True)
        return
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "data.json").write_text(
        '{"globalFilter": "%s"}' % global_filter, encoding="utf-8"
    )


class Vault(ObsidianConnector):
    """The connector, pointed at a directory instead of a downloaded vault."""

    def __init__(self, path: Path):
        super().__init__(account_id=0, credentials={"name": "Test"}, sync_state={})
        self._path = path

    @property
    def root(self):
        return self._path

    def _require_vault(self):
        return self._path


def pull(tmp: Path, state):
    """One pull, from a freshly built connector.

    Production builds a connector per sync run, and the settings cache lives on
    the instance -- so reusing one here would test a cache that never exists in
    the real thing, and would miss the rule change entirely.
    """
    return Vault(tmp).pull("vault:", CollectionKind.TASKS, state=state)


with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)

    print("A filter of #task")
    build_vault(tmp, "#task")
    first = pull(tmp, None)
    titles = sorted(i.record.title for i in first.items)
    check("only the tagged lines are tasks", len(first.items) == 2, str(titles))
    check("the first pull is complete, so deletions can be detected",
          not first.incremental)
    check("it remembers the rule it used", bool(first.sync_state.get("rules")))

    print("\nThe filter changes to #todo")
    build_vault(tmp, "#todo")
    second = pull(tmp, first.sync_state)
    titles = sorted(i.record.title for i in second.items)
    check("a different set of lines now qualifies", len(second.items) == 1, str(titles))
    check("THE PASS IS INCREMENTAL, so the two that vanished are not deleted",
          second.incremental,
          "without this the engine tombstones them out of every other service")
    check("and it says why", any("changed" in e for e in second.errors),
          str(second.errors))

    print("\nThe pass after that")
    third = pull(tmp, second.sync_state)
    check("compares like with like again", not third.incremental)
    check("so genuine deletions still propagate afterwards", len(third.items) == 1)

    print("\nA filter being removed entirely counts as a change too")
    build_vault(tmp, None)
    fourth = pull(tmp, third.sync_state)
    check("removing the filter is treated as a rule change", fourth.incremental)
    check("and now the date rule applies to every line", len(fourth.items) == 3,
          str(sorted(i.record.title for i in fourth.items)))

    print("\nAn unchanged rule is not mistaken for a change")
    fifth = pull(tmp, fourth.sync_state)
    check("a steady vault stays complete", not fifth.incremental)
    check("  ...so ordinary deletions are still noticed", len(fifth.items) == 3)

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All Obsidian rule-change tests passed.")
