"""Example application frames for the frame-level exposure check (stdlib only).

Four 8-bit RGB frames, modeled on the session views of the webapp mockup (``webapp/``), of the
kind a custom application might show during photostimulation:

- ``F1_morning_text``: a text slide in bold full-code blue (0,0,255) on black. Built to the
  composition guidance: one primary, a calibrated code, a black background, hard pixel edges.
- ``F2_orb``: the "Light Only" orb as a blue disc the height of the frame whose code steps down
  through calibrated codes (255, 192, 128, 64) toward its edge, on black. Built to the guidance.
- ``F3_night_text``: the same text slide in the webapp's night colors, red-orange (255,92,56) text
  on a dark red (20,5,7) background. Not built to the guidance: mixed channels, and a background
  whose codes lie below the lowest calibrated code.
- ``F4_red_orb``: the orb in red (255, 192, 128, 64 toward the edge) on black, at the night level.
  Built to the guidance.

Frames are rendered deterministically (a 5x7 pixel font, integer geometry), so the same frame,
pixel for pixel, can be rebuilt from this module; ``frame_hash`` matches the host's ``image_hash``.
Each frame also names the uniform full-frame colors captured with it as same-session references.

Write the frames as binary PPM (the format ``chronolume_host`` loads with ``image``)::

    python -m uprtek.frames --out data/uprtek/frames
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

FRAME_W = 1920
FRAME_H = 1080

# Webapp theme colors (webapp/css/styles.css): --bg and --stim-text of the morning and night themes.
WEBAPP_NIGHT_BG = (20, 5, 7)
WEBAPP_NIGHT_TEXT = (255, 92, 56)
BLACK = (0, 0, 0)
BLUE = (0, 0, 255)

GLYPH_W, GLYPH_H = 5, 7
TEXT_SCALE = 16
TEXT_BOLD = 8  # extra stroke width in pixels: each glyph pixel is drawn (scale + bold) square
CHAR_ADVANCE = GLYPH_W + 1
LINE_ADVANCE = GLYPH_H + 5

_GLYPHS = {
    "A": (" ### ", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"),
    "B": ("#### ", "#   #", "#   #", "#### ", "#   #", "#   #", "#### "),
    "C": (" ### ", "#   #", "#    ", "#    ", "#    ", "#   #", " ### "),
    "D": ("#### ", "#   #", "#   #", "#   #", "#   #", "#   #", "#### "),
    "E": ("#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#####"),
    "F": ("#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#    "),
    "G": (" ### ", "#   #", "#    ", "# ###", "#   #", "#   #", " ####"),
    "H": ("#   #", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"),
    "I": (" ### ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "),
    "J": ("  ###", "   # ", "   # ", "   # ", "   # ", "#  # ", " ##  "),
    "K": ("#   #", "#  # ", "# #  ", "##   ", "# #  ", "#  # ", "#   #"),
    "L": ("#    ", "#    ", "#    ", "#    ", "#    ", "#    ", "#####"),
    "M": ("#   #", "## ##", "# # #", "# # #", "#   #", "#   #", "#   #"),
    "N": ("#   #", "#   #", "##  #", "# # #", "#  ##", "#   #", "#   #"),
    "O": (" ### ", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "),
    "P": ("#### ", "#   #", "#   #", "#### ", "#    ", "#    ", "#    "),
    "Q": (" ### ", "#   #", "#   #", "#   #", "# # #", "#  # ", " ## #"),
    "R": ("#### ", "#   #", "#   #", "#### ", "# #  ", "#  # ", "#   #"),
    "S": (" ####", "#    ", "#    ", " ### ", "    #", "    #", "#### "),
    "T": ("#####", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  "),
    "U": ("#   #", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "),
    "V": ("#   #", "#   #", "#   #", "#   #", "#   #", " # # ", "  #  "),
    "W": ("#   #", "#   #", "#   #", "# # #", "# # #", "# # #", " # # "),
    "X": ("#   #", "#   #", " # # ", "  #  ", " # # ", "#   #", "#   #"),
    "Y": ("#   #", "#   #", " # # ", "  #  ", "  #  ", "  #  ", "  #  "),
    "Z": ("#####", "    #", "   # ", "  #  ", " #   ", "#    ", "#####"),
    ".": ("     ", "     ", "     ", "     ", "     ", " ##  ", " ##  "),
    ",": ("     ", "     ", "     ", "     ", " ##  ", "  #  ", " #   "),
    " ": ("     ",) * 7,
}

# Orb: disc radius in pixels and its rings (outer radius as a fraction of the disc, blue code).
ORB_RADIUS = 540
ORB_RINGS = ((1.0, 64), (0.85, 128), (0.7, 192), (0.5, 255))


class Canvas:
    def __init__(self, w: int, h: int, background: tuple[int, int, int]) -> None:
        self.w, self.h = w, h
        self.px = bytearray(bytes(background) * (w * h))

    def fill_span(self, y: int, x0: int, x1: int, rgb: tuple[int, int, int]) -> None:
        """Pixels x0 <= x < x1 of row y."""
        x0, x1 = max(0, x0), min(self.w, x1)
        if 0 <= y < self.h and x1 > x0:
            i = (y * self.w + x0) * 3
            self.px[i:i + (x1 - x0) * 3] = bytes(rgb) * (x1 - x0)

    def fill_rect(self, x: int, y: int, w: int, h: int, rgb: tuple[int, int, int]) -> None:
        for yy in range(y, y + h):
            self.fill_span(yy, x, x + w, rgb)


def text_lines(canvas: Canvas, lines: tuple[str, ...], rgb: tuple[int, int, int], scale: int = TEXT_SCALE,
               bold: int = TEXT_BOLD) -> None:
    """Centered block of lines in the pixel font, each glyph pixel a (scale + bold) square on a
    scale grid."""
    block_h = (len(lines) * LINE_ADVANCE - (LINE_ADVANCE - GLYPH_H)) * scale + bold
    top = (canvas.h - block_h) // 2
    for n, line in enumerate(lines):
        width = (len(line) * CHAR_ADVANCE - 1) * scale + bold
        left = (canvas.w - width) // 2
        y0 = top + n * LINE_ADVANCE * scale
        for i, ch in enumerate(line):
            glyph = _GLYPHS[ch]
            x0 = left + i * CHAR_ADVANCE * scale
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == "#":
                        canvas.fill_rect(x0 + gx * scale, y0 + gy * scale, scale + bold, scale + bold, rgb)


def disc(canvas: Canvas, radius: float, rgb: tuple[int, int, int]) -> None:
    """Pixels whose centers lie within ``radius`` of the frame center."""
    cx, cy = canvas.w / 2.0, canvas.h / 2.0
    for y in range(canvas.h):
        dy = y + 0.5 - cy
        if abs(dy) > radius:
            continue
        hw = math.sqrt(radius * radius - dy * dy)
        canvas.fill_span(y, math.ceil(cx - hw - 0.5), math.floor(cx + hw - 0.5) + 1, rgb)


SLIDE_TEXT = ("THE PYRAMIDS WERE", "ALREADY ANCIENT", "TO CLEOPATRA.")


def _morning_text(w: int, h: int) -> Canvas:
    c = Canvas(w, h, BLACK)
    text_lines(c, SLIDE_TEXT, BLUE)
    return c


def _orb_of(channel: int) -> Callable[[int, int], Canvas]:
    def draw(w: int, h: int) -> Canvas:
        c = Canvas(w, h, BLACK)
        for frac, code in ORB_RINGS:
            rgb = [0, 0, 0]
            rgb[channel] = code
            disc(c, frac * ORB_RADIUS, tuple(rgb))
        return c
    return draw


def _night_text(w: int, h: int) -> Canvas:
    c = Canvas(w, h, WEBAPP_NIGHT_BG)
    text_lines(c, SLIDE_TEXT, WEBAPP_NIGHT_TEXT)
    return c


@dataclass(frozen=True)
class FrameSpec:
    name: str
    title: str
    webapp_view: str
    composed_to_guidance: bool
    level: int  # hardware brightness the frame is shown at (the webapp mode's level)
    nominal_rgb: tuple[int, int, int]  # dominant lit color, for the spectral peak search
    references: tuple[tuple[int, int, int], ...]  # uniform full-frame colors captured with the frame
    draw: Callable[[int, int], Canvas]


FRAMES = (
    FrameSpec("F1_morning_text", "Morning text slide", "World History slide, morning theme", True, 8,
              BLUE, (BLUE,), _morning_text),
    FrameSpec("F2_orb", "Light-only orb", "Light Only orb, morning theme", True, 8,
              BLUE, tuple((0, 0, code) for _, code in ORB_RINGS[::-1]), _orb_of(2)),
    FrameSpec("F3_night_text", "Night text slide (webapp colors)", "World History slide, night theme", False, 5,
              WEBAPP_NIGHT_TEXT, (WEBAPP_NIGHT_TEXT, WEBAPP_NIGHT_BG), _night_text),
    FrameSpec("F4_red_orb", "Red light-only orb", "Light Only orb, night theme", True, 5,
              (255, 0, 0), tuple((code, 0, 0) for _, code in ORB_RINGS[::-1]), _orb_of(0)),
)
BY_NAME = {f.name: f for f in FRAMES}


def render(name: str, w: int = FRAME_W, h: int = FRAME_H) -> bytes:
    """The frame's pixels, 8-bit RGB, top row first."""
    return bytes(BY_NAME[name].draw(w, h).px)


def histogram(pixels: bytes) -> list[tuple[tuple[int, int, int], int]]:
    """(rgb, pixel count) for every color in the frame, most frequent first."""
    counts = Counter(zip(pixels[0::3], pixels[1::3], pixels[2::3]))
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def frame_hash(pixels: bytes) -> str:
    """FNV-1a 64 of the pixel bytes as 16 hex digits, as the host reports ``image_hash``."""
    h = 14695981039346656037
    for b in pixels:
        h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def write_ppm(path: Path, w: int, h: int, pixels: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"P6\n{w} {h}\n255\n".encode("ascii") + pixels)


def read_ppm(path: Path) -> tuple[int, int, bytes]:
    data = Path(path).read_bytes()
    tokens: list[bytes] = []
    i = 0
    while len(tokens) < 4:
        while data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            while data[i:i + 1] != b"\n":
                i += 1
            continue
        j = i
        while not data[j:j + 1].isspace():
            j += 1
        tokens.append(data[i:j])
        i = j
    if tokens[0] != b"P6" or tokens[3] != b"255":
        raise ValueError(f"{path}: not an 8-bit binary PPM")
    w, h = int(tokens[1]), int(tokens[2])
    return w, h, data[i + 1:i + 1 + w * h * 3]


def describe(spec: FrameSpec, pixels: bytes, w: int, h: int) -> dict:
    n = w * h
    hist = histogram(pixels)
    return {
        "name": spec.name,
        "title": spec.title,
        "webapp_view": spec.webapp_view,
        "composed_to_guidance": spec.composed_to_guidance,
        "level": spec.level,
        "nominal_rgb": list(spec.nominal_rgb),
        "references": [list(r) for r in spec.references],
        "width": w,
        "height": h,
        "hash": frame_hash(pixels),
        "lit_fraction": sum(c for rgb, c in hist if rgb != BLACK) / n,
        "colors": [{"rgb": list(rgb), "pixels": c, "fraction": c / n} for rgb, c in hist],
    }


def write_frames(out: Path, w: int = FRAME_W, h: int = FRAME_H) -> list[dict]:
    """Write every frame as ``<name>.ppm`` plus ``frames.json`` (histograms and hashes)."""
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for spec in FRAMES:
        px = render(spec.name, w, h)
        write_ppm(out / f"{spec.name}.ppm", w, h, px)
        records.append({**describe(spec, px, w, h), "file": f"{spec.name}.ppm"})
    (out / "frames.json").write_text(json.dumps({"schema": "chronolume-frames/1", "frames": records}, indent=1) + "\n",
                                     encoding="utf-8")
    return records


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=Path("data/uprtek/frames"))
    p.add_argument("--width", type=int, default=FRAME_W, help="framebuffer width of the glasses display")
    p.add_argument("--height", type=int, default=FRAME_H)
    args = p.parse_args(argv)
    for r in write_frames(args.out, args.width, args.height):
        print(f"{r['file']}: {r['width']}x{r['height']} hash {r['hash']} lit {100 * r['lit_fraction']:.1f}% "
              f"colors {len(r['colors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
