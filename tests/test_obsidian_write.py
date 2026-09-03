"""Tests for writing a completion back into a markdown line.

This is the only code in Task Hub that modifies a file in someone's Obsidian
vault, so the tests are almost entirely about what it must *not* disturb. The
Tasks plugin parses a line backwards from the end and stops at the first thing
it does not recognise, which means a line rebuilt from parsed fields silently
loses whatever the parser did not understand -- a wikilink, a footnote, a block
reference, another plugin's inline field. Patching spans avoids that, and these
tests are what hold it to it.
"""

from __future__ import annotations

import datetime as dt
import sys

from app.services.obsidian_md import (
    parse_line, rewrite_completion, verify_only_completion_changed,
)

DONE = dt.date(2026, 9, 10)
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def tick(line: str, completed: bool = True, on: dt.date = DONE) -> str:
    task = parse_line(line, 0)
    assert task is not None, line
    return rewrite_completion(task, completed, on)


print("Completing a task")

out = tick("- [ ] #task Buy milk 📅 2026-09-12")
check("the box is ticked", out.startswith("- [x] "), out)
check("a done date is appended", "✅ 2026-09-10" in out, out)
check("the due date is untouched", "📅 2026-09-12" in out, out)
check("the tag survives", "#task" in out, out)

print("\nWhat must survive untouched")

cases = [
    ("a block reference",
     "- [ ] #task Call [[Bob Smith]] 📅 2026-09-12 ^a1b2c3", "^a1b2c3"),
    ("a wikilink",
     "- [ ] #task Read [[Some Note|the note]] 📅 2026-09-12", "[[Some Note|the note]]"),
    ("nested indentation",
     "    - [ ] #task Indented 📅 2026-09-12", "    - [x]"),
    ("an asterisk bullet",
     "* [ ] #task Starred 📅 2026-09-12", "* [x]"),
    ("another plugin's inline field",
     "- [ ] #task Thing [effort:: 3h] 📅 2026-09-12", "[effort:: 3h]"),
    ("a trailing tag after the metadata",
     "- [ ] #task Thing 📅 2026-09-12 #home", "#home"),
    ("emphasis and code",
     "- [ ] #task Fix `parse()` in **core** 📅 2026-09-12", "`parse()` in **core**"),
    ("a priority emoji",
     "- [ ] #task Urgent ⏫ 📅 2026-09-12", "⏫"),
    ("a recurrence rule",
     "- [ ] #task Weekly 🔁 every week 📅 2026-09-12", "🔁 every week"),
]
for name, line, must_keep in cases:
    out = tick(line)
    check(f"{name} survives", must_keep in out, f"{line!r} -> {out!r}")
    check(f"  ...and {name} line still verifies",
          verify_only_completion_changed(line, out) is None,
          str(verify_only_completion_changed(line, out)))

print("\nThe done date")

out = tick("- [ ] #task Already done ✅ 2026-01-01 📅 2026-09-12")
check("an existing done date is replaced, not duplicated",
      out.count("✅") == 1 and "2026-09-10" in out and "2026-01-01" not in out, out)

out = tick("- [ ] #task Dataview [due:: 2026-09-12]")
check("a dataview vault gets dataview syntax",
      "[completion:: 2026-09-10]" in out and "✅" not in out, out)

print("\nUn-completing")

out = tick("- [x] #task Was done ✅ 2026-09-10 📅 2026-09-12", completed=False)
check("the box is cleared", out.startswith("- [ ] "), out)
check("the done date is removed", "✅" not in out, out)
check("the due date survives", "📅 2026-09-12" in out, out)
check("no double spaces are left behind", "  " not in out.strip(), repr(out))

print("\nThe verifier catches damage")

original = "- [ ] #task Call [[Bob]] 📅 2026-09-12 ^a1b2c3"
check("a clean patch passes",
      verify_only_completion_changed(original, tick(original)) is None)
check("a lost block reference is caught",
      verify_only_completion_changed(original, "- [x] #task Call [[Bob]] 📅 2026-09-12 ✅ 2026-09-10") is not None)
check("a changed description is caught",
      verify_only_completion_changed(original, "- [x] #task Call [[Bobby]] 📅 2026-09-12 ^a1b2c3") is not None)
check("a lost due date is caught",
      verify_only_completion_changed(original, "- [x] #task Call [[Bob]] ^a1b2c3") is not None)
check("a line that stops being a task is caught",
      verify_only_completion_changed(original, "just some text") is not None)

print("\nRound trip")

line = "- [ ] #task Call [[Bob]] ⏫ 🔁 every week 📅 2026-09-12 #home ^a1b2c3"
done = tick(line)
back = tick(done, completed=False)
check("completing then un-completing returns the original line",
      back == line, f"{line!r} -> {done!r} -> {back!r}")

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All Obsidian write tests passed.")
