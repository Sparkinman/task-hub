"""Renders the setup guides inside the application.

The guides live as Markdown files in ``docs/`` and are rendered on demand. They
are served from inside the app rather than linked out to a website because the
whole point is that a brand-new user can follow them without leaving the page
they are configuring -- and because a self-hosted install should not depend on
some external site still being up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from markdown_it import MarkdownIt
from sqlalchemy.orm import Session

from app.config import DOCS_DIR
from app.db.session import get_db
from app.web import deps

router = APIRouter(prefix="/docs")

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
_md.enable("table")
_md.enable("strikethrough")


def _slug_of(text: str) -> str:
    """A heading's anchor, matching what GitHub would generate for it.

    The install guides are long enough to need links within a page, and they are
    read both here and on GitHub. One slug rule for both means a link written
    once works in both places rather than silently going nowhere in one of them.
    """
    slug = text.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)   # punctuation and dashes-as-dashes go
    return re.sub(r"\s", "-", slug)


def _add_heading_anchors(md: MarkdownIt) -> None:
    """Give every heading an id, taken from its own text."""
    default = md.renderer.rules.get("heading_open")

    def heading_open(tokens, idx, options, env):
        inline = tokens[idx + 1]
        text = "".join(
            child.content for child in (inline.children or []) if child.type == "text"
        ) or inline.content
        tokens[idx].attrSet("id", _slug_of(text))
        if default:
            return default(tokens, idx, options, env)
        return md.renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["heading_open"] = heading_open


_add_heading_anchors(_md)


@dataclass
class DocEntry:
    slug: str
    title: str


def _title_of(path) -> str:
    """Use the document's first heading as its title.

    Taken from the file rather than kept in a table here, so that renaming a
    guide is a one-line edit in the guide itself and cannot leave the index
    disagreeing with the page it points at. Falls back to a tidied filename
    for a document with no heading, which should not happen but should not
    produce a blank entry if it does.
    """
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem.replace("-", " ").replace("_", " ").title()


def list_docs() -> list[DocEntry]:
    """Every guide, in reading order rather than alphabetical order.

    Alphabetical would open the list with the Apple guide, which is neither
    where a new reader should start nor a connector that works yet. The rank
    table below puts the "what is this" page first, then the install guide for
    whichever machine the reader has, then the shared explanation of
    addressing, then the per-service walkthroughs.
    """
    if not DOCS_DIR.exists():
        return []
    entries = [
        DocEntry(slug=path.stem, title=_title_of(path))
        for path in sorted(DOCS_DIR.glob("*.md"))
    ]
    # Reading order rather than alphabetical: what Task Hub is, then how to
    # install it on your own machine, then a service guide, then the guide for
    # connecting your own apps -- which only makes sense once something syncs.
    rank = {
        "getting-started": 0,
        # The install guides sit together, ordered by how likely a reader is to
        # be on that machine rather than alphabetically.
        "install-raspberry-pi": 1,
        "install-nas": 2,
        "install-windows": 3,
        "install-macos": 4,
        "install-linux": 5,
        "addresses": 6,
        "third-party-apps": 9,
    }
    return sorted(entries, key=lambda e: (rank.get(e.slug, 8), e.title.lower()))


@router.get("")
@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    """The list of guides."""
    return deps.render(request, db, "docs_index.html", docs=list_docs())


@router.get("/{slug}")
def show(slug: str, request: Request, db: Session = Depends(get_db)):
    # Guides link to each other by filename, because that is the form GitHub
    # needs when the same files are browsed there. Accepting it here means one
    # link works in both places instead of one of them quietly going nowhere.
    if slug.endswith(".md"):
        slug = slug[:-3]

    # The slug becomes a filename, so anything but a plain lowercase name is
    # refused rather than sanitised -- no traversal, no surprises.
    if not _SLUG.match(slug):
        deps.flash(request, "No such guide.", "error")
        return deps.redirect("/docs")

    path = (DOCS_DIR / f"{slug}.md").resolve()
    if not path.is_file() or DOCS_DIR.resolve() not in path.parents:
        deps.flash(request, "That guide has not been written yet.", "error")
        return deps.redirect("/docs")

    source = path.read_text(encoding="utf-8")
    return deps.render(
        request, db, "doc_page.html",
        doc_title=_title_of(path),
        doc_html=_md.render(source),
        docs=list_docs(),
        slug=slug,
    )
