"""Decoding Supernote's ``.mark`` handwriting into an image.

A digest can carry a handwritten note as well as the passage it was taken from,
and Task Hub was showing only the passage -- saying "this has handwriting on
the tablet" and stopping there, which is a poor answer when the handwriting is
the part somebody wrote themselves.

The file is Supernote's own container: a header of ``<KEY:value>`` blocks, one
or more layers, and a footer naming where each lives. The pen strokes are a
single ``MAINLAYER`` bitmap compressed with ``RATTA_RLE``, their own run-length
encoding, which is simple enough to decode here:

* Bytes come in pairs of ``(colour, length)``.
* A length under ``0x80`` means that many pixels plus one.
* A length with the high bit set is the low seven bits of a longer run, and it
  is held until the next pair: if that pair repeats the colour, the two combine
  into one long run; otherwise the held run is emitted on its own.
* ``0xFF`` is the special "to the end of the line" length.

Ratta's own converter is used everywhere else in Task Hub, deliberately,
because it always understands the current format. It cannot be used here: those
endpoints take a file id from the cloud file tree, and a digest's handwriting
is not a file in that tree.

Anything unexpected returns None. A missing picture beside a digest is a
disappointment; a backup that fails because a picture could not be drawn is a
fault.
"""

from __future__ import annotations

import logging
import re
import struct
import zlib

logger = logging.getLogger(__name__)

#: What each colour code paints, as an 8-bit grey level. Supernote's palette is
#: black, two greys and the background, with a second set of codes for the
#: marker pen that draws in the same shades.
_GREYS = {
    0x61: 0x00,  # black
    0x62: 0xFF,  # background
    0x63: 0x9D,  # dark grey
    0x64: 0xC9,  # grey
    0x65: 0xFF,  # white
    0x66: 0x00,  # marker black
    0x67: 0x9D,  # marker dark grey
    0x68: 0xC9,  # marker grey
}
_BACKGROUND = 0xFF

#: Page sizes these files are written at. The header names the device, but not
#: every model is listed there, so the decoder simply checks which size the
#: decoded run length actually fits.
_SIZES = ((1920, 2560), (1404, 1872), (1404, 1874))

_FIELD = re.compile(rb"<([A-Z_0-9]+):([^>]*)>")


def _fields(data: bytes) -> dict[str, str]:
    return {
        m.group(1).decode("ascii", "replace"): m.group(2).decode("ascii", "replace")
        for m in _FIELD.finditer(data)
    }


def _bitmap_addresses(data: bytes) -> list[int]:
    """Every address a layer says its bitmap starts at, in file order.

    The layer headers are not at a fixed place. In a small file they sit near
    the front; in a larger one they sit after the bitmap they describe, which
    can be hundreds of kilobytes in. An earlier version read only the first
    64KB and so found nothing at all in the bigger of two real files.

    Several are returned rather than one because a file may carry more than one
    layer, and the caller decides by which of them has ink on it.
    """
    seen: list[int] = []
    for match in _FIELD.finditer(data):
        if match.group(1) != b"LAYERBITMAP":
            continue
        try:
            at = int(match.group(2))
        except ValueError:
            continue
        if 0 < at < len(data) and at not in seen:
            seen.append(at)
    return seen


def decode_rle(payload: bytes, expected: int) -> bytearray | None:
    """Expand a RATTA_RLE run into one grey byte per pixel."""
    out = bytearray()
    holder: tuple[int, int] | None = None
    index = 0

    while index + 1 < len(payload) and len(out) < expected:
        colour, length = payload[index], payload[index + 1]
        index += 2
        emitted = False

        if holder is not None:
            held_colour, held_length = holder
            holder = None
            if colour == held_colour:
                # The held run and this one are the same colour, so they are
                # two halves of one long run rather than two runs.
                run = 1 + length + (((held_length & 0x7F) + 1) << 7)
                out += bytes([_GREYS.get(colour, _BACKGROUND)]) * run
                emitted = True
            else:
                run = ((held_length & 0x7F) + 1) << 7
                out += bytes([_GREYS.get(held_colour, _BACKGROUND)]) * run

        if emitted:
            continue
        if length == 0xFF:
            # Their "rest of this stretch" marker.
            out += bytes([_GREYS.get(colour, _BACKGROUND)]) * (1 << 14)
        elif length & 0x80:
            holder = (colour, length)
        else:
            out += bytes([_GREYS.get(colour, _BACKGROUND)]) * (length + 1)

    if holder is not None:
        run = ((holder[1] & 0x7F) + 1) << 7
        out += bytes([_GREYS.get(holder[0], _BACKGROUND)]) * run

    if not out:
        return None
    # A short read is padded rather than refused: a stroke that stops early is
    # still worth showing, and the alternative is no picture at all.
    if len(out) < expected:
        out += bytes([_BACKGROUND]) * (expected - len(out))
    return out[:expected]


def _png(pixels: bytes, width: int, height: int, scale: int = 1) -> bytes:
    """A greyscale PNG, optionally reduced by whole-pixel averaging."""
    if scale > 1:
        out_w, out_h = width // scale, height // scale
        reduced = bytearray()
        for y in range(out_h):
            reduced.append(0)
            base = y * scale * width
            for x in range(out_w):
                total = 0
                for dy in range(scale):
                    row = base + dy * width + x * scale
                    total += sum(pixels[row:row + scale])
                reduced.append(total // (scale * scale))
        rows, width, height = bytes(reduced), out_w, out_h
    else:
        rows = bytearray()
        for y in range(height):
            rows.append(0)
            rows += pixels[y * width:(y + 1) * width]
        rows = bytes(rows)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit grey
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def _trim(pixels: bytearray, width: int, height: int) -> tuple[bytes, int, int]:
    """Crop away the blank margin around the writing.

    A mark file is a whole page, and handwriting on it is usually a few lines in
    one corner. Shown uncropped it is a postage stamp in an ocean of white, so
    the empty edges go.
    """
    top, bottom = 0, height - 1
    while top < height and all(
        pixels[top * width + x] >= 0xF0 for x in range(0, width, 4)
    ):
        top += 1
    while bottom > top and all(
        pixels[bottom * width + x] >= 0xF0 for x in range(0, width, 4)
    ):
        bottom -= 1
    left, right = 0, width - 1
    while left < width and all(
        pixels[y * width + left] >= 0xF0 for y in range(top, bottom + 1, 4)
    ):
        left += 1
    while right > left and all(
        pixels[y * width + right] >= 0xF0 for y in range(top, bottom + 1, 4)
    ):
        right -= 1

    if right <= left or bottom <= top:
        return bytes(pixels), width, height

    # A little air around the writing, so it does not sit against the edge.
    margin = 24
    top, left = max(0, top - margin), max(0, left - margin)
    bottom, right = min(height - 1, bottom + margin), min(width - 1, right + margin)

    new_w, new_h = right - left + 1, bottom - top + 1
    cropped = bytearray()
    for y in range(top, bottom + 1):
        cropped += pixels[y * width + left:y * width + right + 1]
    return bytes(cropped), new_w, new_h


def to_image(data: bytes) -> tuple[bytes, int, int] | None:
    """The handwriting as greyscale pixels, cropped: (pixels, width, height).

    Kept separate from :func:`to_png` because the PDF needs the pixels rather
    than a PNG -- a PDF image is raw samples with its own compression, so
    encoding a PNG only to unpick it again would be wasted work.
    """
    try:
        if b"SN_FILE" not in data[:64]:
            return None
        fields = _fields(data)
        if fields.get("LAYERPROTOCOL") not in (None, "RATTA_RLE"):
            return None

        for start in _bitmap_addresses(data):
            # The bitmap is length-prefixed: four bytes, then the run.
            size = struct.unpack("<I", data[start:start + 4])[0]
            payload = data[start + 4:start + 4 + size]
            if not payload:
                continue

            for width, height in _SIZES:
                pixels = decode_rle(payload, width * height)
                if pixels is None:
                    continue
                ink = sum(1 for i in range(0, len(pixels), 17) if pixels[i] < 0xF0)
                if ink == 0:
                    # A blank page: either the wrong size for this device, or a
                    # layer nobody drew on. Either way, keep looking.
                    continue
                return _trim(pixels, width, height)
        return None
    except Exception:  # noqa: BLE001
        logger.debug("Could not decode a mark file", exc_info=True)
        return None


def read_png(data: bytes) -> tuple[bytes, int, int] | None:
    """Read back a greyscale PNG written by :func:`_png`.

    Only this module's own output is handled, which is why it is fifteen lines
    rather than a decoder: 8-bit grey, no interlacing, and every row filtered
    with 0. The PDF needs raw samples, and the alternative to reading them out
    of the saved picture is decoding the ``.mark`` file again on every request.
    """
    try:
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        width = height = 0
        body = bytearray()
        at = 8
        while at + 8 <= len(data):
            size = struct.unpack(">I", data[at:at + 4])[0]
            kind = data[at + 4:at + 8]
            payload = data[at + 8:at + 8 + size]
            if kind == b"IHDR":
                width, height, depth, colour = struct.unpack(">IIBB", payload[:10])
                if depth != 8 or colour != 0:
                    return None
            elif kind == b"IDAT":
                body += payload
            elif kind == b"IEND":
                break
            at += size + 12
        if not width or not height:
            return None

        rows = zlib.decompress(bytes(body))
        stride = width + 1
        if len(rows) < stride * height:
            return None
        pixels = bytearray()
        for y in range(height):
            start = y * stride
            if rows[start] != 0:
                return None  # A filtered row: not something this module wrote.
            pixels += rows[start + 1:start + stride]
        return bytes(pixels), width, height
    except Exception:  # noqa: BLE001
        logger.debug("Could not read back a mark image", exc_info=True)
        return None


def to_png(data: bytes, scale: int = 2) -> bytes | None:
    """A PNG of the handwriting in a ``.mark`` file, or None if unreadable."""
    found = to_image(data)
    if found is None:
        return None
    pixels, width, height = found
    try:
        return _png(pixels, width, height, scale=scale)
    except Exception:  # noqa: BLE001
        logger.debug("Could not encode a mark image", exc_info=True)
        return None
