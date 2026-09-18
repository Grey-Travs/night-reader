"""Draw Night Reader's home-screen icon, with no image library.

Why this exists rather than a checked-in binary someone once exported: the icon is four
files at three sizes plus a favicon, and every one of them has to agree. Regenerating
them from one description is the only way that stays true after the first tweak.

Pillow is not a dependency of this project and is not worth becoming one for a script
that runs about twice, so the PNG is encoded here directly -- it is a length-prefixed
chunk format over a zlib stream, which is about thirty lines.

The artwork is a crescent moon over two lines of text: night, and reading. It is drawn
from signed distance fields and supersampled, so the curve stays clean at 192px where a
naive rasteriser goes visibly jagged.

    python tools/make_icons.py

Writes into ``web/public/``, which Vite copies to the site root verbatim.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

PUBLIC = Path(__file__).resolve().parent.parent / "web" / "public"

# Straight from web/src/index.css, so the icon cannot drift away from the app it opens.
PAGE = (0x18, 0x15, 0x20)      # --page, the dark surface
ACCENT = (0xD9, 0xA8, 0x5C)    # --accent, the warm gold
INK = (0xEC, 0xE6, 0xDF)       # --ink, for the text lines

# 3x3 subsamples per pixel. Enough that the crescent's inner curve reads as smooth at the
# smallest size we emit; more is slower for no visible gain.
SS = 3


# ---------------------------------------------------------------------------
# PNG, by hand
# ---------------------------------------------------------------------------


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: Path, size: int, rows: list[bytearray]) -> None:
    """8-bit RGBA, filter type 0 on every scanline (no prediction, just clarity)."""
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, 9))
           + _chunk(b"IEND", b""))
    path.write_bytes(png)


# ---------------------------------------------------------------------------
# Shapes, as signed distances in a 0..1 square (negative = inside)
# ---------------------------------------------------------------------------


def _circle(x: float, y: float, cx: float, cy: float, r: float) -> float:
    return math.hypot(x - cx, y - cy) - r


def _rounded_square(x: float, y: float, radius: float) -> float:
    """Distance to a full-canvas square with rounded corners."""
    dx, dy = abs(x - 0.5) - (0.5 - radius), abs(y - 0.5) - (0.5 - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    return outside + min(max(dx, dy), 0.0) - radius


def _capsule(x: float, y: float, x0: float, x1: float, cy: float, half: float) -> float:
    """A horizontal line with round caps -- one line of text."""
    return math.hypot(x - min(max(x, x0), x1), y - cy) - half


def _mark(x: float, y: float) -> float:
    """The crescent and the two text lines, in the mark's own 0..1 box.

    The crescent is one circle with a second subtracted from its upper right, which is
    the whole trick: `max(outer, -cut)` is the intersection of "inside the disc" and
    "outside the bite", and gives a real crescent rather than a filled circle with a
    lighter one drawn over it. That matters because the icon must work on a transparent
    background, where overdrawing would leave the bite opaque.
    """
    outer = _circle(x, y, 0.465, 0.395, 0.290)
    cut = _circle(x, y, 0.605, 0.300, 0.263)
    crescent = max(outer, -cut)
    line_a = _capsule(x, y, 0.245, 0.755, 0.760, 0.0295)
    line_b = _capsule(x, y, 0.245, 0.635, 0.880, 0.0295)
    return min(crescent, line_a, line_b)


def _coverage(distance: float, px: float) -> float:
    """Antialias by treating the distance as a soft edge one pixel wide."""
    return min(max(0.5 - distance / px, 0.0), 1.0)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(size: int, *, maskable: bool, opaque: bool, mark_scale: float,
           corner: float = 0.22) -> list[bytearray]:
    """One icon.

    ``maskable`` fills the whole square, because Android crops a maskable icon to
    whatever shape the launcher uses and a rounded icon would get its corners clipped
    twice. ``mark_scale`` shrinks the artwork into the safe zone for that same reason.
    """
    px = 1.0 / size          # one output pixel, in unit coordinates
    step = 1.0 / (size * SS)
    half = step / 2.0
    rows: list[bytearray] = []
    inv = 1.0 / (SS * SS)

    for py in range(size):
        row = bytearray(size * 4)
        for pxi in range(size):
            bg_cov = 0.0
            mark_cov = 0.0
            ink_cov = 0.0
            for sy in range(SS):
                y = (py * SS + sy) * step + half
                for sx in range(SS):
                    x = (pxi * SS + sx) * step + half
                    if maskable or opaque:
                        bg = 1.0
                    else:
                        bg = _coverage(_rounded_square(x, y, corner), px)
                    if bg <= 0.0:
                        continue
                    bg_cov += bg
                    # Into the mark's own coordinate box.
                    mx = (x - 0.5) / mark_scale + 0.5
                    my = (y - 0.5) / mark_scale + 0.5
                    if not (0.0 <= mx <= 1.0 and 0.0 <= my <= 1.0):
                        continue
                    moon = _coverage(_circle(mx, my, 0.465, 0.395, 0.290), px / mark_scale)
                    bite = _coverage(-_circle(mx, my, 0.605, 0.300, 0.263), px / mark_scale)
                    mark_cov += min(moon, bite) * bg
                    lines = _coverage(
                        min(_capsule(mx, my, 0.245, 0.755, 0.760, 0.0295),
                            _capsule(mx, my, 0.245, 0.635, 0.880, 0.0295)),
                        px / mark_scale)
                    ink_cov += lines * bg

            a = bg_cov * inv
            if a <= 0.0:
                continue
            moon_a = min(mark_cov * inv / a, 1.0) if a else 0.0
            ink_a = min(ink_cov * inv / a, 1.0) if a else 0.0
            # Paint the moon, then the text lines, both over the page colour.
            r, g, b = PAGE
            r = r + (ACCENT[0] - r) * moon_a
            g = g + (ACCENT[1] - g) * moon_a
            b = b + (ACCENT[2] - b) * moon_a
            r = r + (INK[0] - r) * ink_a
            g = g + (INK[1] - g) * ink_a
            b = b + (INK[2] - b) * ink_a
            o = pxi * 4
            row[o] = int(r + 0.5)
            row[o + 1] = int(g + 0.5)
            row[o + 2] = int(b + 0.5)
            row[o + 3] = int(a * 255 + 0.5)
        rows.append(row)
    return rows


FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" rx="22" fill="#181520"/>
  <path d="M60.5 17.6a29 29 0 1 0 0 48.5 26.3 26.3 0 1 1 0-48.5Z" fill="#D9A85C"/>
  <g stroke="#ECE6DF" stroke-width="5.9" stroke-linecap="round">
    <path d="M24.5 76h51"/>
    <path d="M24.5 88h39"/>
  </g>
</svg>
"""


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    jobs = [
        # The everyday icon: rounded square, artwork filling most of it.
        ("icon-192.png", 192, dict(maskable=False, opaque=False, mark_scale=0.70)),
        ("icon-512.png", 512, dict(maskable=False, opaque=False, mark_scale=0.70)),
        # Maskable: full bleed, artwork pulled into the safe circle so a launcher that
        # crops to a circle does not cut the moon.
        ("icon-maskable-512.png", 512,
         dict(maskable=True, opaque=True, mark_scale=0.56)),
        # iOS masks the corners itself and dislikes transparency, so: opaque square.
        ("apple-touch-icon.png", 180, dict(maskable=False, opaque=True, mark_scale=0.68)),
    ]
    for name, size, opts in jobs:
        write_png(PUBLIC / name, size, render(size, **opts))
        print(f"wrote {name} ({size}x{size})")
    (PUBLIC / "favicon.svg").write_text(FAVICON, encoding="utf-8")
    print("wrote favicon.svg")


if __name__ == "__main__":
    main()
