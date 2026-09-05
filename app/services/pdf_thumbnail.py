"""A small preview image of a PDF's first page, without a PDF library.

Supernote's converter writes each page as one plain image inside the PDF --
1920x2560, eight bits per component, grey or RGB, zlib-compressed and nothing
more. That is the simplest shape a PDF image can take, and it means the first
page can be recovered with the standard library alone.

Doing it this way rather than the obvious alternatives matters:

* **Not by asking Supernote for a PNG.** Their ``/file/note/to/png`` endpoint
  works and would give a perfect thumbnail, but it is a second full render of
  the notebook on their servers, for an API they never published. Halving that
  cost was a deliberate decision everywhere else in the backup; spending it
  again on a decoration would undo it.
* **Not by adding a PDF renderer.** Pillow, pypdfium or mupdf would each work
  and each would be a large dependency carried in every image forever, for one
  small picture.

Anything unexpected produces no thumbnail rather than an error. A preview is a
convenience: a notebook with no picture beside it is mildly plainer, while a
backup that fails because a picture could not be made would be a real fault.
"""

from __future__ import annotations

import logging
import re
import struct
import zlib

logger = logging.getLogger(__name__)

#: Widest edge of the generated preview. Small on purpose: it sits under a file
#: name in a list, and a page of a hundred of them should stay light.
THUMB_WIDTH = 240

#: Samples averaged per output pixel, per axis. Plain nearest-neighbour drops
#: thin pen strokes entirely, which for a page of handwriting means a preview
#: that looks blank -- worse than none at all.
SAMPLES = 3

#: The first image object in the file, with its dictionary. Supernote writes
#: pages in order, so the first is page one.
_IMAGE = re.compile(
    rb"/Subtype\s*/Image(?P<dict>[^>]{0,400})>>\s*stream\r?\n", re.S
)


def _first_image(pdf: bytes) -> tuple[dict, bytes] | None:
    """The first embedded page image: its parameters and its raw stream."""
    match = _IMAGE.search(pdf)
    if match is None:
        return None

    header = match.group("dict")
    start = match.end()
    end = pdf.find(b"endstream", start)
    if end == -1:
        return None

    def number(name: bytes) -> int | None:
        found = re.search(name + rb"\s+(\d+)", header)
        return int(found.group(1)) if found else None

    width, height = number(rb"/Width"), number(rb"/Height")
    depth = number(rb"/BitsPerComponent")
    if not width or not height or depth != 8:
        return None

    if b"/DeviceRGB" in header:
        channels = 3
    elif b"/DeviceGray" in header:
        channels = 1
    else:
        # CMYK, indexed palettes and the rest are possible in principle and
        # have never been seen from this converter. Declining is better than
        # guessing at colours.
        return None

    if b"/DCTDecode" in header or b"/JPXDecode" in header:
        return None  # A real JPEG; decoding one by hand is a different project.
    if b"/FlateDecode" not in header:
        return None
    if b"/DecodeParms" in header or b"/Predictor" in header:
        # Predictor-encoded rows need un-filtering before the bytes mean
        # anything. Not seen from this converter, and worth refusing rather
        # than rendering noise.
        return None

    return (
        {"width": width, "height": height, "channels": channels},
        pdf[start:end],
    )


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def thumbnail(pdf: bytes, width: int = THUMB_WIDTH) -> bytes | None:
    """A small PNG of the PDF's first page, or None if it cannot be made."""
    try:
        found = _first_image(pdf)
        if found is None:
            return None
        meta, stream = found
        try:
            pixels = zlib.decompress(stream)
        except zlib.error:
            # Streams are sometimes padded; decompressobj tolerates the tail
            # that decompress() refuses outright.
            try:
                pixels = zlib.decompressobj().decompress(stream)
            except zlib.error:
                return None

        source_w, source_h = meta["width"], meta["height"]
        channels = meta["channels"]
        if len(pixels) < source_w * source_h * channels:
            return None

        out_w = max(1, min(width, source_w))
        out_h = max(1, round(source_h * out_w / source_w))
        box_x = source_w / out_w
        box_y = source_h / out_h

        rows = bytearray()
        for oy in range(out_h):
            rows.append(0)  # PNG filter type 0
            for ox in range(out_w):
                r = g = b = 0
                for sy in range(SAMPLES):
                    py = int((oy + (sy + 0.5) / SAMPLES) * box_y)
                    if py >= source_h:
                        py = source_h - 1
                    row_start = py * source_w * channels
                    for sx in range(SAMPLES):
                        px = int((ox + (sx + 0.5) / SAMPLES) * box_x)
                        if px >= source_w:
                            px = source_w - 1
                        i = row_start + px * channels
                        if channels == 1:
                            value = pixels[i]
                            r += value
                            g += value
                            b += value
                        else:
                            r += pixels[i]
                            g += pixels[i + 1]
                            b += pixels[i + 2]
                total = SAMPLES * SAMPLES
                rows.extend((r // total, g // total, b // total))

        header = struct.pack(">IIBBBBB", out_w, out_h, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + _chunk(b"IEND", b"")
        )
    except Exception:  # noqa: BLE001
        # A preview is a decoration. Nothing about failing to draw one should
        # ever be able to fail a backup.
        logger.debug("Could not build a preview for a notebook", exc_info=True)
        return None
