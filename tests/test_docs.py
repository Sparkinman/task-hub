"""The guides: every one renders, every internal link goes somewhere.

Two failures this catches, both of which have happened. A guide that links to
`docs/thing.md` when the file is `thing3.md` looks perfect in review and dead
ends the reader; and a service whose `docs_slug` names a guide that was never
written sends somebody who pressed "Setup guide" to an error page.

It deliberately says nothing about the prose. What it asserts is that the
plumbing between the catalogue, the files and the links is intact.
"""

from __future__ import annotations

import re
import sys

from app.config import DOCS_DIR
from app.web.docs_view import _md, _title_of, list_docs
from app.web.services_view import SERVICE_CATALOGUE

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


DOCS = sorted(DOCS_DIR.glob("*.md"))
SLUGS = {path.stem for path in DOCS}

print("\nEvery guide is a real document")
check("there are guides at all", len(DOCS) > 5, str(len(DOCS)))
for path in DOCS:
    source = path.read_text(encoding="utf-8")
    ok = source.lstrip().startswith("# ") and len(source) > 400
    check(f"{path.name} has a heading and some substance", ok, f"{len(source)} bytes")

print("\nEvery guide renders")
for path in DOCS:
    try:
        html = _md.render(path.read_text(encoding="utf-8"))
        check(f"{path.name} renders", len(html) > 200, f"{len(html)} bytes")
    except Exception as exc:  # noqa: BLE001
        check(f"{path.name} renders", False, repr(exc))

print("\nEvery link between guides points at a guide that exists")
LINK = re.compile(r"\]\(([a-z0-9_-]+)\.md(#[^)]*)?\)")
for path in DOCS:
    source = path.read_text(encoding="utf-8")
    for match in LINK.finditer(source):
        target = match.group(1)
        check(f"{path.name} -> {target}.md", target in SLUGS,
              f"no docs/{target}.md")

print("\nEvery service names a guide that exists")
for definition in SERVICE_CATALOGUE:
    check(f"{definition.name} -> {definition.docs_slug}.md",
          definition.docs_slug in SLUGS, definition.docs_slug)

print("\nThe index is ordered for a reader, not alphabetically")
order = [entry.slug for entry in list_docs()]
check("every guide is listed", len(order) == len(DOCS), f"{len(order)} of {len(DOCS)}")
check("the introduction is first", order[0] == "getting-started", order[:3])
check("what-works comes before the install guides",
      order.index("compatibility") < order.index("install-raspberry-pi"), order[:4])
check("connecting your own apps comes last", order[-1] == "third-party-apps",
      order[-3:])
check("titles come from the documents themselves",
      _title_of(DOCS_DIR / "compatibility.md") == "What works with Task Hub")

print("\nThe compatibility page covers every service in the catalogue")
compat = (DOCS_DIR / "compatibility.md").read_text(encoding="utf-8")
for definition in SERVICE_CATALOGUE:
    check(f"{definition.name} appears on it", definition.name in compat)

print("\nAnd labels each row rather than leaving the reader to guess")
for label in ("Verified", "Should work", "Won't work"):
    check(f"{label!r} is used", label in compat)

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All documentation tests passed.")
