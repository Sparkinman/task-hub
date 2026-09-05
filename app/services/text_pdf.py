"""Typeset plain text into a PDF, with no dependencies.

A Supernote notebook arrives as a PDF because Supernote renders it. A digest
does not: it is a paragraph of text, a source reference and sometimes a
comment, so if it is to be readable in a PDF viewer, Task Hub has to set it.

PDF makes that unusually easy for this one case. The format has fourteen fonts
every reader is required to provide, so a document using Helvetica needs no
font embedding at all -- and without embedding, a text-only PDF is a few
hundred bytes of markup around the words. That is a much better trade than
carrying ReportLab in the image for one feature.

The deliberate limitation is character set. The built-in fonts are single-byte,
so this handles Latin text and transliterates or drops the rest rather than
emitting mojibake. Anything beyond that would mean embedding a font, which is
the whole dependency this avoids.
"""

from __future__ import annotations

import unicodedata
import zlib

#: Helvetica's advance widths, in units of 1/1000 em, for the printable ASCII
#: range. Needed for wrapping: without real widths, lines set with a fixed
#: guess are either ragged or overflow the page.
_WIDTHS = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667,
    "'": 191, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
    ".": 278, "/": 278, "0": 556, "1": 556, "2": 556, "3": 556, "4": 556,
    "5": 556, "6": 556, "7": 556, "8": 556, "9": 556, ":": 278, ";": 278,
    "<": 584, "=": 584, ">": 584, "?": 556, "@": 1015, "[": 278, "\\": 278,
    "]": 278, "^": 469, "_": 556, "`": 333, "{": 334, "|": 260, "}": 334,
    "~": 584,
}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _WIDTHS[_c] = {"I": 278, "J": 500, "L": 556, "M": 833, "W": 944}.get(_c, 722)
for _c in "abcdefghijklmnopqrstuvwxyz":
    _WIDTHS[_c] = {"f": 278, "i": 222, "j": 222, "l": 222, "m": 833,
                   "r": 333, "t": 278, "w": 722}.get(_c, 556)

#: A4, in points.
PAGE_W, PAGE_H = 595.28, 841.89
MARGIN = 56.0


def _latin(text: str) -> str:
    """Reduce text to what a built-in PDF font can actually show.

    Curly quotes and dashes are replaced with their straight equivalents rather
    than dropped, because a Bible passage full of typographic quotes would
    otherwise arrive full of gaps. Anything with no Latin equivalent is
    replaced with a question mark, which is honest about having lost something.
    """
    swaps = {"‘": "'", "’": "'", "“": '"', "”": '"',
             "–": "-", "—": "-", "…": "...", " ": " ",
             "•": "-", "·": "-", "′": "'", "″": '"',
             "\t": " "}
    out = []
    for char in text:
        # Line breaks survive. The wrapper splits on them to keep the author's
        # own paragraphing, and folding them here -- which an earlier version
        # did -- turned every multi-line digest into one run of text punctuated
        # by question marks.
        if char == "\n":
            out.append("\n")
            continue
        if char in swaps:
            out.append(swaps[char])
            continue
        if 32 <= ord(char) <= 126:
            out.append(char)
            continue
        # Strip accents where that leaves something readable.
        folded = unicodedata.normalize("NFKD", char)
        plain = "".join(c for c in folded if 32 <= ord(c) <= 126)
        out.append(plain if plain else "?")
    return "".join(out)


def width_of(text: str, size: float) -> float:
    return sum(_WIDTHS.get(c, 556) for c in text) * size / 1000.0


def _break_long(word: str, size: float, limit: float) -> list[str]:
    """Split a run too long for one line, at whatever character fits.

    A URL, a long identifier or a word in a language that does not space its
    words has no break to wrap at, and left alone it runs off the right edge of
    the page -- silently, because nothing measures the result.
    """
    pieces: list[str] = []
    while width_of(word, size) > limit and len(word) > 1:
        cut = len(word)
        while cut > 1 and width_of(word[:cut], size) > limit:
            cut -= 1
        pieces.append(word[:cut])
        word = word[cut:]
    if word:
        pieces.append(word)
    return pieces


def wrap(text: str, size: float, limit: float) -> list[str]:
    """Break text into lines that fit, keeping the author's own line breaks."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue

        current = ""
        for word in words:
            candidate = f"{current} {word}" if current else word
            if width_of(candidate, size) <= limit:
                current = candidate
                continue
            if current:
                lines.append(current)
            # Every word is measured on its own, including the first in a
            # paragraph -- an earlier version only broke words that followed
            # another, so a paragraph that was one long run was never broken
            # at all.
            pieces = _break_long(word, size, limit)
            lines.extend(pieces[:-1])
            current = pieces[-1] if pieces else ""
        if current:
            lines.append(current)
    return lines


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


class Block:
    """One piece of a document: a heading, a paragraph, or a spacer."""

    def __init__(self, text: str, size: float = 11.0, bold: bool = False,
                 space_after: float = 6.0, grey: bool = False):
        self.text = _latin(text)
        self.size = size
        self.bold = bold
        self.space_after = space_after
        self.grey = grey


def build(title: str, blocks: list[Block]) -> bytes:
    """Lay the blocks out over as many pages as they need, and write the PDF."""
    usable = PAGE_W - 2 * MARGIN
    pages: list[list[tuple]] = []
    current: list[tuple] = []
    y = PAGE_H - MARGIN

    def newpage():
        nonlocal current, y
        if current:
            pages.append(current)
        current = []
        y = PAGE_H - MARGIN

    for block in blocks:
        leading = block.size * 1.35
        for line in wrap(block.text, block.size, usable):
            if y - leading < MARGIN:
                newpage()
            current.append((MARGIN, y - block.size, line, block.size,
                            block.bold, block.grey))
            y -= leading
        y -= block.space_after
    newpage()
    if not pages:
        pages = [[]]

    # --- Assemble the file ---------------------------------------------------
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # object numbers are 1-based

    font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                       b"/Encoding /WinAnsiEncoding >>")
    font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                    b"/Encoding /WinAnsiEncoding >>")

    page_ids: list[int] = []
    content_ids: list[int] = []
    for page in pages:
        parts = []
        for x, y_pos, line, size, bold, grey in page:
            font = "F2" if bold else "F1"
            colour = "0.42 0.45 0.5" if grey else "0.10 0.11 0.13"
            parts.append(
                f"BT /{font} {size:.1f} Tf {colour} rg "
                f"1 0 0 1 {x:.1f} {y_pos:.1f} Tm ({_escape(line)}) Tj ET"
            )
        stream = zlib.compress("\n".join(parts).encode("latin-1", "replace"))
        content_ids.append(add(
            b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\n"
            b"stream\n" + stream + b"\nendstream"
        ))

    pages_id = len(objects) + len(pages) + 1
    for content_id in content_ids:
        page_ids.append(add(
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
            f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode()
        ))

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())
    info_id = add(
        b"<< /Title (" + _escape(_latin(title)).encode("latin-1", "replace")
        + b") /Producer (Task Hub) >>"
    )
    catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R "
        f"/Info {info_id} 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)
