"""Generate the PNG app icons from the Task Hub mark, with no dependencies.

An installed web app needs PNG icons: Android's installer wants 192 and 512,
and iOS ignores SVG for the home-screen icon entirely. Task Hub's mark is only
circles, so rasterising it here is a few lines of arithmetic -- which is a much
better trade than adding Pillow or cairosvg to the image for one build step
that runs approximately never.

Run it from the project root when the mark changes:

    python3 scripts/make_icons.py

The output is committed, so a normal build and a normal checkout never need to
run this at all.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "img"

# Straight from app/static/img/favicon.svg, whose viewBox is "336 106 168 168":
# a 168-unit square with the wheel centred at (420, 190).
VIEW = 168.0
CX = CY = 84.0            # centre, in view units
RING_R = 72.0             # ring radius
RING_W = 12.0             # ring stroke width
DOT_R = 7.0               # the eight spoke dots
HUB_R = 40.0              # solid centre
CORE_R = 13.0             # dark hole in the centre

BLUE = (0x37, 0x8A, 0xDD)
NAVY = (0x04, 0x2C, 0x53)

#: Dot centres, taken from the SVG and shifted into view coordinates.
DOTS = [(x - 336.0, y - 106.0) for x, y in (
    (471.7, 211.4), (441.4, 241.7), (398.6, 241.7), (368.3, 211.4),
    (368.3, 168.6), (398.6, 138.3), (441.4, 138.3), (471.7, 168.6),
)]

#: Samples per pixel per axis. Four is enough to make circle edges look smooth
#: at every size an installer asks for.
SUPERSAMPLE = 4


def _colour_at(x: float, y: float) -> tuple[int, int, int]:
    """The mark's colour at one point, in view coordinates."""
    dx, dy = x - CX, y - CY
    distance = (dx * dx + dy * dy) ** 0.5

    if distance <= CORE_R:
        return NAVY
    if distance <= HUB_R:
        return BLUE
    if abs(distance - RING_R) <= RING_W / 2:
        return BLUE
    for dot_x, dot_y in DOTS:
        ddx, ddy = x - dot_x, y - dot_y
        if (ddx * ddx + ddy * ddy) ** 0.5 <= DOT_R:
            return BLUE
    return NAVY


def render_badge(size: int) -> bytes:
    """The mark as a silhouette, for Android's status-bar badge.

    Android ignores the colours in a badge entirely and uses only the alpha
    channel, filling whatever shape it finds. An icon with an opaque background
    -- which every ordinary app icon has -- therefore renders as a solid
    rectangle, which is exactly what the first version of this shipped.

    So this one is white where the mark is and fully transparent everywhere
    else, and drawn a little heavier than the real mark because the badge is
    displayed at around the size of a full stop.
    """
    span = VIEW / (1.0 - 2.0 * 0.06)
    origin = CX - span / 2.0
    step = span / (size * SUPERSAMPLE)

    rows = bytearray()
    for py in range(size):
        rows.append(0)
        for px in range(size):
            covered = 0
            for sy in range(SUPERSAMPLE):
                y = origin + (py * SUPERSAMPLE + sy + 0.5) * step
                for sx in range(SUPERSAMPLE):
                    x = origin + (px * SUPERSAMPLE + sx + 0.5) * step
                    dx, dy = x - CX, y - CY
                    distance = (dx * dx + dy * dy) ** 0.5
                    hit = (
                        distance <= HUB_R
                        or abs(distance - RING_R) <= RING_W / 2
                        or any(
                            ((x - dot_x) ** 2 + (y - dot_y) ** 2) ** 0.5 <= DOT_R
                            for dot_x, dot_y in DOTS
                        )
                    )
                    if hit:
                        covered += 1
            alpha = (covered * 255) // (SUPERSAMPLE * SUPERSAMPLE)
            # White, so a launcher tinting the silhouette gets a clean base.
            rows.extend((255, 255, 255, alpha))
    return bytes(rows)


def render(size: int, inset: float = 0.0) -> bytes:
    """One RGB image, as raw rows.

    ``inset`` shrinks the mark towards the centre, leaving a margin. Android's
    maskable icons may be cropped to a circle by the launcher, so the mark has
    to sit inside the safe zone or the launcher will clip its edges off.
    """
    span = VIEW / (1.0 - 2.0 * inset)
    origin = CX - span / 2.0
    step = span / (size * SUPERSAMPLE)

    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG filter type 0 for this scanline
        for px in range(size):
            r = g = b = 0
            for sy in range(SUPERSAMPLE):
                y = origin + (py * SUPERSAMPLE + sy + 0.5) * step
                for sx in range(SUPERSAMPLE):
                    x = origin + (px * SUPERSAMPLE + sx + 0.5) * step
                    cr, cg, cb = _colour_at(x, y)
                    r += cr
                    g += cg
                    b += cb
            total = SUPERSAMPLE * SUPERSAMPLE
            rows.extend((r // total, g // total, b // total))
    return bytes(rows)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def write_png(path: Path, size: int, inset: float = 0.0, badge: bool = False) -> None:
    raw = render_badge(size) if badge else render(size, inset)
    # Colour type 6 is RGBA, 2 is RGB. The badge needs the alpha channel;
    # nothing else does, and an unnecessary one would only bloat the file.
    header = struct.pack(">IIBBBBB", size, size, 8, 6 if badge else 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    print(f"  {path.name}  {size}x{size}  {len(png):,} bytes")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    print("Writing app icons:")
    write_png(OUT / "icon-192.png", 192)
    write_png(OUT / "icon-512.png", 512)
    # Maskable: the launcher may crop this to any shape, so the mark is inset
    # into the safe zone rather than filling the square.
    write_png(OUT / "icon-maskable-512.png", 512, inset=0.14)
    # iOS uses this one for the home screen and does not read the manifest.
    write_png(OUT / "apple-touch-icon.png", 180)
    # Android's status-bar badge: a silhouette, not an icon.
    write_png(OUT / "badge-96.png", 96, badge=True)
