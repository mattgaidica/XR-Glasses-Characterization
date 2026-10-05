"""Spatial weighting and lit-area gain for frame estimates, fitted from a spatial grid report.

Not validated: an alternative to the equal-weighting frame estimate, reported alongside it. Two
terms, both from ``uprtek.grid_check`` (single 3x3 cells and full fields at one level and duty):

- ``shape``: the probe's weighting of the frame, w(x, y) = (1 + amp * gaussian(x, y)) / norm with
  mean 1 over the frame, fitted so its cell averages match the mean photopic ``9 x cell / full``
  map of red, green and blue normalized to mean 1 (the map's excess over 1 is the gain, below).
  It describes the phantom and probe, not a wearer's eye.
- ``gain``: output per unit drive as a power of the frame's mean drive, G(load) = load^-alpha with
  G(1) = 1. load = mean over pixels of sum_c a_c(k_c, L) (a_c = photopic code response, 1 at code
  255), so a full-field primary is 1 and full white 3. alpha is the log-space least-squares slope
  through four points: G(1/9) = mean primary cell sum, G(1) = 1, G(3) = full white over the sum of
  full red, green and blue, G(3/9) = white cell sum x G(3).

A frame is then sum over colors of U(color) * S(color) * G(load_frame) / G(load(color)), with
U the uniform-color output (model or measured reference) and S the mean over the frame of w times
the color's pixel mask.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from uprtek.model import forward

LAYOUT = (("TL", "T", "TR"), ("L", "C", "R"), ("BL", "B", "BR"))
PRIMARIES = ("R255", "G255", "B255")
ASSUMPTIONS = [
    "spatial weight: Gaussian plus offset fitted to the grid's pooled red, green and blue photopic cell map "
    "(phantom and probe geometry, not a wearer's eye)",
    "lit-area gain: power law in the frame's mean drive (sum of photopic code responses), from the grid's "
    "cell sums and full white over its primaries, at the grid's level and duty",
    "not validated; reported alongside the equal-weighting estimate",
]


def _gauss_cell_means(lo: float, hi: float, mu: float, sigma: float) -> float:
    """Mean of exp(-((x - mu) / sigma)^2 / 2) over [lo, hi]."""
    s = sigma * math.sqrt(2.0)
    return sigma * math.sqrt(math.pi / 2.0) * (math.erf((hi - mu) / s) - math.erf((lo - mu) / s)) / (hi - lo)


def _edges(n: int) -> list[int]:
    return [0, n // 3, 2 * n // 3, n]


def shape_cells(p: dict[str, float], w: int, h: int) -> np.ndarray:
    """3x3 cell averages of the normalized weight map (integer-third cells, as ``grid_frames``)."""
    xe, ye = _edges(w), _edges(h)
    gx = [_gauss_cell_means(xe[j], xe[j + 1], p["x0"], p["sx"]) for j in range(3)]
    gy = [_gauss_cell_means(ye[i], ye[i + 1], p["y0"], p["sy"]) for i in range(3)]
    cells = np.array([[1.0 + p["amp"] * gy[i] * gx[j] for j in range(3)] for i in range(3)])
    area = np.array([[(ye[i + 1] - ye[i]) * (xe[j + 1] - xe[j]) for j in range(3)] for i in range(3)])
    return cells / (np.sum(cells * area) / (w * h))


def weight_map(p: dict[str, float], w: int, h: int) -> np.ndarray:
    """Per-pixel weight, mean 1 over the frame, rows top first."""
    gx = np.exp(-0.5 * ((np.arange(w) + 0.5 - p["x0"]) / p["sx"]) ** 2)
    gy = np.exp(-0.5 * ((np.arange(h) + 0.5 - p["y0"]) / p["sy"]) ** 2)
    m = 1.0 + p["amp"] * np.outer(gy, gx)
    return m / m.mean()


def fit_shape(target: np.ndarray, w: int, h: int) -> dict[str, float]:
    """Deterministic coarse search, then coordinate refinement, of the Gaussian-plus-offset whose
    cell averages match ``target`` (3x3, normalized here to mean 1)."""
    target = np.asarray(target, dtype=float)
    target = target / target.mean()

    def sse(p: dict[str, float]) -> float:
        return float(np.sum((shape_cells(p, w, h) - target) ** 2))

    best, err = None, math.inf
    for x0 in np.linspace(0.35, 0.65, 7) * w:
        for y0 in np.linspace(0.35, 0.65, 7) * h:
            for sx in np.linspace(0.05, 0.6, 12) * w:
                for sy in np.linspace(0.05, 0.6, 12) * h:
                    for amp in (0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0):
                        p = {"x0": x0, "y0": y0, "sx": sx, "sy": sy, "amp": amp}
                        e = sse(p)
                        if e < err:
                            best, err = p, e
    step = {"x0": 0.02 * w, "y0": 0.02 * h, "sx": 0.02 * w, "sy": 0.02 * h, "amp": 0.25 * best["amp"]}
    for _ in range(8):
        improved = True
        while improved:
            improved = False
            for k in step:
                for d in (-1.0, 1.0):
                    q = {**best, k: max(best[k] + d * step[k], 1e-3)}
                    e = sse(q)
                    if e < err - 1e-12:
                        best, err, improved = q, e, True
        step = {k: v / 2.0 for k, v in step.items()}
    return {**{k: float(v) for k, v in best.items()}, "sse": err}


def _relative_map(grid: dict[str, Any], color: str, attr: str) -> np.ndarray | None:
    rel = ((grid["colors"].get(color) or {}).get("mean") or {}).get(attr, {}).get("relative")
    return None if rel is None else np.array([[rel[c] for c in row] for row in LAYOUT])


def fit_gain(grid: dict[str, Any], attr: str) -> dict[str, Any] | None:
    sums = [((grid["colors"].get(c) or {}).get("mean") or {}).get(attr, {}).get("sum_over_full") for c in PRIMARIES]
    s_white = ((grid["colors"].get("W255") or {}).get("mean") or {}).get(attr, {}).get("sum_over_full")
    if any(v is None for v in sums) or s_white is None:
        return None
    full: dict[Any, dict[str, float]] = {}
    for c in (*PRIMARIES, "W255"):
        for s in grid["colors"][c]["sessions"]:
            if s.get("full") and s["full"].get(attr):
                full.setdefault(s.get("session_index"), {})[c] = s["full"][attr]
    kappas = [f["W255"] / sum(f[c] for c in PRIMARIES) for f in full.values() if len(f) == 4]
    if not kappas:
        return None
    kappa = float(np.mean(kappas))
    pts = [(1.0 / 9.0, float(np.mean(sums))), (1.0, 1.0), (3.0, kappa), (3.0 / 9.0, s_white * kappa)]
    lx, lg = np.log([p[0] for p in pts]), np.log([p[1] for p in pts])
    alpha = float(-np.sum(lx * lg) / np.sum(lx ** 2))
    return {"alpha": alpha, "points": [list(p) for p in pts],
            "residual_pct": [100.0 * (l ** -alpha / g - 1.0) for l, g in pts]}


def fit_spatial(grid: dict[str, Any], w: int, h: int) -> dict[str, Any]:
    maps = [m for m in (_relative_map(grid, c, "lux") for c in PRIMARIES) if m is not None]
    if not maps:
        raise ValueError("grid report has no complete red, green or blue block")
    target = np.mean(maps, axis=0)
    shape = fit_shape(target, w, h)
    levels = sorted({s["level"] for rec in grid["colors"].values() for s in rec["sessions"]})
    duties = sorted({s["duty"] for rec in grid["colors"].values() for s in rec["sessions"]})
    return {"shape": shape, "frame_size": [w, h],
            "target_relative": (target / target.mean()).tolist(),
            "fitted_relative": shape_cells(shape, w, h).tolist(),
            "peak_over_mean": float(weight_map(shape, w, h).max()),
            "gain": {a: fit_gain(grid, a) for a in ("edi", "lux")},
            "grid_level": levels, "grid_duty": duties, "grid_sessions": grid.get("grid_sessions", []),
            "assumptions": ASSUMPTIONS}


def color_weights(pixels: bytes, w: int, h: int, shape: dict[str, float]
                  ) -> tuple[dict[tuple[int, int, int], float], dict[tuple[int, int, int], float]]:
    """Per color: the mean over the frame of the weight map times its pixel mask (S), and its pixel
    fraction."""
    img = np.frombuffer(pixels, dtype=np.uint8).reshape(h, w, 3)
    colors, inv = np.unique(img.reshape(-1, 3), axis=0, return_inverse=True)
    inv = inv.ravel()
    wsum = np.bincount(inv, weights=weight_map(shape, w, h).ravel(), minlength=len(colors)) / (w * h)
    frac = np.bincount(inv, minlength=len(colors)) / (w * h)
    keys = [tuple(int(v) for v in rgb) for rgb in colors]
    return dict(zip(keys, map(float, wsum))), dict(zip(keys, map(float, frac)))


def drive(photopic: dict[str, Any], rgb: tuple[int, int, int], level: int) -> float:
    """Sum of the channels' photopic code responses (1 at code 255); unsupported codes count 0."""
    total = 0.0
    for c, i in forward.CHANNEL_INDEX.items():
        prim = photopic["primaries"].get(c)
        if rgb[i] == 0 or prim is None:
            continue
        a = forward.code_response(prim, int(rgb[i]), int(level))
        total += a if a is not None and a > 0 else 0.0
    return total


def gain_factor(alpha: float, load_frame: float, load_color: float) -> float:
    if load_color <= 0 or load_frame <= 0:
        return 1.0
    return (load_frame / load_color) ** -alpha


def frame_sum(values: dict[tuple[int, int, int], float | None], weights: dict[tuple[int, int, int], float],
              loads: dict[tuple[int, int, int], float], load_frame: float, alpha: float) -> float | None:
    """sum over non-black colors of value * S * G(load_frame) / G(load); None if a value is missing."""
    total = 0.0
    for rgb, s in weights.items():
        if not any(rgb):
            continue
        v = values.get(rgb)
        if v is None:
            return None
        total += v * s * gain_factor(alpha, load_frame, loads[rgb])
    return total
