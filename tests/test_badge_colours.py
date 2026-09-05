"""Every service colour must exist in the stylesheet, in both forms.

A colour is named in two places -- the badge on a task, and the round icon on
the services list and the overview -- and each reads a different CSS class. Name
one that has no rule and nothing breaks loudly: the badge renders with no
colour, or the icon renders with no background at all, and it looks like a
service that has simply not been given a colour yet.

That is exactly how CalDAV lost its colour. Amber was added to the badges and
never to the icons, and nobody noticed until somebody said "there is no colour
indicator for CalDAV". Then adding Microsoft and Things 3 repeated it in the
same afternoon, which is a good sign the pairing needs a test rather than
remembering.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "app" / "static" / "css" / "app.css").read_text()

badge_classes = set(re.findall(r"\.badge-([a-z]+)\s*\{", CSS))
icon_classes = set(re.findall(r"\.service-icon\.([a-z]+)\s*\{", CSS))
variables = set(re.findall(r"--badge-([a-z]+)\s*:", CSS))

from app.web.deps import DEFAULT_BADGE, SERVICE_BADGES  # noqa: E402
from app.web.services_view import SERVICE_CATALOGUE  # noqa: E402

in_use = {entry["colour"] for entry in SERVICE_BADGES.values()}
in_use.add(DEFAULT_BADGE["colour"])
in_use |= {definition.colour for definition in SERVICE_CATALOGUE}

print(f"\n{len(in_use)} colours are named by services: {', '.join(sorted(in_use))}")

print("\nEach one has a badge class, or a task's badge renders colourless")
for colour in sorted(in_use):
    check(f".badge-{colour}", colour in badge_classes)

print("\nEach one has a service-icon class, or the round icon has no background")
for colour in sorted(in_use):
    check(f".service-icon.{colour}", colour in icon_classes)

print("\nAnd each is backed by a variable, so dark mode has a value too")
for colour in sorted(in_use):
    check(f"--badge-{colour}", colour in variables)

print("\nThe dark palettes define every colour the light one does")
# Two dark blocks: the prefers-color-scheme default and the explicit override.
# A colour added to one and not the others is invisible in whichever theme was
# missed, which is the sort of thing nobody notices until they switch.
blocks = re.findall(r"\{([^{}]*--badge-[a-z]+[^{}]*)\}", CSS)
palettes = [set(re.findall(r"--badge-([a-z]+)\s*:", block)) for block in blocks]
palettes = [p for p in palettes if len(p) > 3]  # ignore incidental single uses
check("more than one palette is defined", len(palettes) >= 2, str(len(palettes)))
if palettes:
    complete = palettes[0]
    for index, palette in enumerate(palettes[1:], start=1):
        missing = complete - palette
        check(f"palette {index} defines every colour", not missing, str(sorted(missing)))

if _failures:
    print(f"\n{len(_failures)} check(s) failed.")
    sys.exit(1)
print("\nAll badge colour checks passed.")
