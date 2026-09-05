"""Typeset plain text into a PDF, with no dependencies.

A Supernote notebook arrives as a PDF because Supernote renders it. A digest
does not: it is a paragraph of text, a source reference and sometimes a
comment, so if it is to be readable in a PDF viewer, Task Hub has to set it.

PDF makes that unusually easy for this one case. The format has fourteen fonts
every reader is required to provide, so a document using Helvetica needs no
font embedding at all -- and without embedding, a text-only PDF is a few
hundred bytes of markup around the words. That is a much better trade than
carrying ReportLab in the image for one feature.

Images are handled too, because a digest can carry a handwritten note and that
note is the half somebody wrote themselves. A PDF image is raw samples with its
own compression, which is why :class:`Image` takes greyscale pixels rather than
a PNG -- the two use the same Flate compression, so encoding a PNG and unpicking
it again would be work for nothing.

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

#: The Supernote screens these drawings come off are around 226 dots to the
#: inch. Placing them at that density rather than filling the column keeps
#: handwriting close to its size on the tablet, so a short note reads as a
#: short note instead of being blown up to the width of the page.
SCREEN_DPI = 226.0


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


class Image:
    """A greyscale picture to place in the flow, at one byte per pixel.

    Sized from the screen it was drawn on rather than stretched to the column,
    and shrunk to fit when that would be too big for the page.
    """

    def __init__(self, pixels: bytes, width: int, height: int,
                 space_after: float = 10.0, dpi: float = SCREEN_DPI):
        self.pixels = pixels
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.space_after = space_after
        self.dpi = dpi or SCREEN_DPI

    def placed(self, column: float, page: float) -> tuple[float, float]:
        """How big to draw it: natural size, reduced to fit the page."""
        draw_w = self.width * 72.0 / self.dpi
        draw_h = self.height * 72.0 / self.dpi
        shrink = min(1.0, column / draw_w, page / draw_h)
        return draw_w * shrink, draw_h * shrink


def build(title: str, blocks: list) -> bytes:
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

    tall = PAGE_H - 2 * MARGIN
    images: list[Image] = []

    for block in blocks:
        if isinstance(block, Image):
            draw_w, draw_h = block.placed(usable, tall)
            # Never split a drawing across a page break: half a signature on
            # each of two pages is worse than a page with a gap at the bottom.
            if y - draw_h < MARGIN and current:
                newpage()
            images.append(block)
            current.append(("image", MARGIN, y - draw_h, draw_w, draw_h,
                            len(images)))
            y -= draw_h + block.space_after
            continue

        leading = block.size * 1.35
        for line in wrap(block.text, block.size, usable):
            if y - leading < MARGIN:
                newpage()
            current.append(("text", MARGIN, y - block.size, line, block.size,
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

    # One object per drawing, shared by whichever page uses it.
    image_ids: list[int] = []
    for picture in images:
        data = zlib.compress(picture.pixels, 6)
        image_ids.append(add(
            b"<< /Type /XObject /Subtype /Image "
            + f"/Width {picture.width} /Height {picture.height} ".encode()
            + b"/ColorSpace /DeviceGray /BitsPerComponent 8 "
            + b"/Filter /FlateDecode /Length " + str(len(data)).encode()
            + b" >>\nstream\n" + data + b"\nendstream"
        ))

    page_ids: list[int] = []
    content_ids: list[int] = []
    used: list[list[int]] = []
    for page in pages:
        parts = []
        drawn: list[int] = []
        for entry in page:
            if entry[0] == "image":
                _, x, y_pos, draw_w, draw_h, number = entry
                drawn.append(number)
                # The unit square is the image, so the matrix is its size.
                parts.append(
                    f"q {draw_w:.2f} 0 0 {draw_h:.2f} {x:.1f} {y_pos:.1f} cm "
                    f"/Im{number} Do Q"
                )
                continue
            _, x, y_pos, line, size, bold, grey = entry
            font = "F2" if bold else "F1"
            colour = "0.42 0.45 0.5" if grey else "0.10 0.11 0.13"
            parts.append(
                f"BT /{font} {size:.1f} Tf {colour} rg "
                f"1 0 0 1 {x:.1f} {y_pos:.1f} Tm ({_escape(line)}) Tj ET"
            )
        used.append(drawn)
        stream = zlib.compress("\n".join(parts).encode("latin-1", "replace"))
        content_ids.append(add(
            b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\n"
            b"stream\n" + stream + b"\nendstream"
        ))

    pages_id = len(objects) + len(pages) + 1
    for content_id, drawn in zip(content_ids, used):
        xobjects = ""
        if drawn:
            entries = " ".join(f"/Im{n} {image_ids[n - 1]} 0 R" for n in drawn)
            xobjects = f"/XObject << {entries} >> "
        page_ids.append(add(
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
            f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> "
            f"{xobjects}>> "
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
