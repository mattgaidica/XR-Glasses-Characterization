"""Spatial grid patterns: one cell of a 3x3 grid lit at a time (stdlib only).

The framebuffer is split into a 3x3 grid (integer thirds; 640x360 cells at 1920x1080). Each pattern
lights one cell at full code in one of R, G, B, or W on black, at brightness ``GRID_LEVEL``. With the
same block's full-field capture, the nine cells give the probe's spatial weight map (cell over full
field; 1/9 each if every pixel counts equally) and an additivity check (sum of the nine over the full
field), the two candidate explanations for the application frames reading above the pixel-weighted
sum of their uniform references.

Patterns reuse the frame machinery (``uprtek.frames``): deterministic pixels, ``frame_hash`` matching
the host's ``image_hash``, and binary PPM files the host loads with ``image``.

    python -m uprtek.grid_frames --out data/uprtek/grid_frames
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from uprtek.frames import BLACK, FRAME_H, FRAME_W, Canvas, FrameSpec, write_ppm

GRID_LEVEL = 7  # the highest brightness allowed for white (sequences.WHITE_MAX_BRIGHTNESS)

# (name, row, column), center first.
CELLS = (("C", 1, 1), ("TL", 0, 0), ("T", 0, 1), ("TR", 0, 2), ("L", 1, 0), ("R", 1, 2),
         ("BL", 2, 0), ("B", 2, 1), ("BR", 2, 2))
COLORS = (("R255", (255, 0, 0)), ("G255", (0, 255, 0)), ("B255", (0, 0, 255)), ("W255", (255, 255, 255)))
COLOR_RGB = dict(COLORS)


def cell_rect(row: int, col: int, w: int, h: int) -> tuple[int, int, int, int]:
    """(x, y, width, height) of a grid cell; edges at integer thirds, so the nine cells tile the frame."""
    x0, x1 = col * w // 3, (col + 1) * w // 3
    y0, y1 = row * h // 3, (row + 1) * h // 3
    return x0, y0, x1 - x0, y1 - y0


def pattern_name(color: str, cell: str) -> str:
    return f"G_{color}_{cell}"


def _cell_of(rgb: tuple[int, int, int], row: int, col: int):
    def draw(w: int, h: int) -> Canvas:
        c = Canvas(w, h, BLACK)
        c.fill_rect(*cell_rect(row, col, w, h), rgb)
        return c
    return draw


PATTERNS = tuple(
    FrameSpec(pattern_name(color, cell), f"{color} cell {cell}", "", True, GRID_LEVEL, rgb, (rgb,),
              _cell_of(rgb, row, col))
    for color, rgb in COLORS for cell, row, col in CELLS
)
BY_NAME = {p.name: p for p in PATTERNS}


def color_patterns(color: str) -> list[FrameSpec]:
    return [BY_NAME[pattern_name(color, cell)] for cell, _, _ in CELLS]


def render(name: str, w: int = FRAME_W, h: int = FRAME_H) -> bytes:
    """The pattern's pixels, 8-bit RGB, top row first."""
    return bytes(BY_NAME[name].draw(w, h).px)


_FNV_PRIME = 1099511628211
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def pattern_hash(name: str, w: int = FRAME_W, h: int = FRAME_H) -> str:
    """``frames.frame_hash`` of the rendered pattern, computed from its geometry: FNV-1a over a run of
    n zero bytes is a multiplication by prime**n, so only the lit cell is hashed byte by byte."""
    spec = BY_NAME[name]
    cell = next((r, c) for n, r, c in CELLS if pattern_name(_color_of(spec), n) == name)
    x, y, cw, ch = cell_rect(*cell, w, h)
    span = bytes(spec.nominal_rgb) * cw
    hv, zeros = 14695981039346656037, (y * w + x) * 3
    for row in range(ch):
        hv = (hv * pow(_FNV_PRIME, zeros, 1 << 64)) & _FNV_MASK
        for b in span:
            hv = ((hv ^ b) * _FNV_PRIME) & _FNV_MASK
        zeros = (w - cw) * 3
    zeros = ((h - y - ch) * w + (w - x - cw)) * 3
    hv = (hv * pow(_FNV_PRIME, zeros, 1 << 64)) & _FNV_MASK
    return f"{hv:016x}"


def _color_of(spec: FrameSpec) -> str:
    return next(c for c, rgb in COLORS if rgb == spec.nominal_rgb)


def describe_pattern(name: str, w: int = FRAME_W, h: int = FRAME_H) -> dict:
    """``frames.describe`` of the rendered pattern, computed from its geometry."""
    spec = BY_NAME[name]
    cell = next((r, c) for n, r, c in CELLS if pattern_name(_color_of(spec), n) == name)
    _, _, cw, ch = cell_rect(*cell, w, h)
    n, lit = w * h, cw * ch
    colors = sorted([(BLACK, n - lit), (spec.nominal_rgb, lit)], key=lambda kv: (-kv[1], kv[0]))
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
        "hash": pattern_hash(name, w, h),
        "lit_fraction": lit / n,
        "colors": [{"rgb": list(rgb), "pixels": c, "fraction": c / n} for rgb, c in colors],
    }


def write_patterns(out: Path, w: int = FRAME_W, h: int = FRAME_H) -> list[dict]:
    """Write every pattern as ``<name>.ppm`` plus ``grid.json`` (histograms and hashes)."""
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for spec in PATTERNS:
        write_ppm(out / f"{spec.name}.ppm", w, h, render(spec.name, w, h))
        records.append({**describe_pattern(spec.name, w, h), "file": f"{spec.name}.ppm"})
    (out / "grid.json").write_text(json.dumps({"schema": "chronolume-grid-frames/1", "frames": records}, indent=1)
                                   + "\n", encoding="utf-8")
    return records


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=Path("data/uprtek/grid_frames"))
    p.add_argument("--width", type=int, default=FRAME_W, help="framebuffer width of the glasses display")
    p.add_argument("--height", type=int, default=FRAME_H)
    args = p.parse_args(argv)
    for r in write_patterns(args.out, args.width, args.height):
        print(f"{r['file']}: {r['width']}x{r['height']} hash {r['hash']} lit {100 * r['lit_fraction']:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
