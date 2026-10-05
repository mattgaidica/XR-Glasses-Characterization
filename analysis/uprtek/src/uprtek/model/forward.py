"""Forward model evaluation (stdlib only): calibration-package parameters -> predicted output.

Shared by the fitting code and the estimator so a prediction is computed one way. A later browser
port reproduces these functions from the same package.

Per output ("melanopic_edi", "photopic"), each primary c has

- ``code_nodes``: code response a(k, L) measured at each level where codes below 255 were
  measured ("ladder levels"): ``{level: {"codes": [0, ..., 255], "a": [0, ..., 1]}}``, with
  power-law segments between codes (``code_interp``) and log-linear in level between ladder
  levels. Below a level's lowest node code the lowest segment is extended toward code 0 (an
  extrapolation; ``min_code`` is the lowest node code at any level). Unsupported outside the
  ladder levels.
- ``codes`` / ``a``: calibrated codes and the code response at the highest ladder level. Packages
  without ``code_nodes`` use them at every level.
- ``levels``: output at code 255 and the base duty for each supported hardware level (lx).
- ``duties`` / ``q``: duty response normalized to 1 at the base duty; linear between duties.
- ``crossed`` (crossed models only): measured output at each grid level and duty, by code.

factorized: f_c(k, L, D) = a_c(k, L) * b_c(L) * q_c(D),  a_c(k, L) from the code nodes
crossed:    f_c(k, L, D) interpolated in code, then duty, at grid level L; at other levels, the
            nearest grid level scaled by b_c(L) / b_c(L*).

Mixtures add the primaries, times an attenuation that vanishes for single-primary stimuli
(``interaction_form``):
    total:   h = 1 - E * g,  E = sum(k_c / 255) - max(k_c / 255), applied to the sum
    channel: h_c = 1 - O_c * g,  O_c = sum over the other channels of k / 255, applied to each f_c
    g = beta0 + betaL * L / 8 + betaD * D / 100
Predictions are clamped at zero.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from typing import Any

CHANNEL_INDEX = {"R": 0, "G": 1, "B": 2}
MAX_LEVEL = 8.0


def interp(x: float, xs: list[float], ys: list[float]) -> float | None:
    """Linear interpolation inside [xs[0], xs[-1]]; None outside (no extrapolation)."""
    if not xs or x < xs[0] or x > xs[-1]:
        return None
    if len(xs) == 1:
        return ys[0]
    i = min(max(bisect_right(xs, x) - 1, 0), len(xs) - 2)
    x0, x1 = xs[i], xs[i + 1]
    t = 0.0 if x1 == x0 else (x - x0) / (x1 - x0)
    return ys[i] + t * (ys[i + 1] - ys[i])


def code_interp(code: float, codes: list[float], vals: list[float]) -> float | None:
    """Code response between measured codes: power-law (log-log) segments where both ends are
    positive, linear otherwise. ``codes`` starts at 0 with value 0. Code 0 is a defined endpoint;
    codes between 0 and the first measured code, and above the last, are unsupported (None)."""
    if code < 0 or code > codes[-1]:
        return None
    if code == 0:
        return 0.0
    pts = [(c, v) for c, v in zip(codes, vals) if c > 0]
    if not pts or code < pts[0][0]:
        return None
    for (c0, v0), (c1, v1) in zip(pts, pts[1:]):
        if c0 <= code <= c1:
            if v0 > 0 and v1 > 0:
                return v0 * (v1 / v0) ** (math.log(code / c0) / math.log(c1 / c0))
            return interp(code, [c0, c1], [v0, v1])
    return pts[-1][1] if code == pts[-1][0] else None


def _bracket(levels: list[int], level: int) -> list[int] | None:
    """The ladder levels bracketing ``level`` (one if it is a ladder level); None outside them."""
    if not levels or level < levels[0] or level > levels[-1]:
        return None
    if level in levels:
        return [level]
    i = bisect_right(levels, level)
    return [levels[i - 1], levels[i]]


def lowest_code(prim: dict[str, Any], level: int | None = None) -> int:
    """The lowest calibrated nonzero code of a primary; with ``level``, the lowest code supported
    at that level (the larger of the bracketing ladder levels' lowest node codes)."""
    nodes = prim.get("code_nodes")
    if nodes and level is not None:
        br = _bracket(sorted(int(l) for l in nodes), int(level))
        if br:
            return max(int(nodes[str(L)]["codes"][1]) for L in br)
    return int(prim["min_code"]) if prim.get("min_code") else int(prim["codes"][1])


def _node_response(node: dict[str, Any], code: int) -> float | None:
    """One ladder level's code response; below its lowest node code, the lowest power-law segment
    (lowest node to the next, or to code 255) extended toward code 0."""
    codes, vals = [float(c) for c in node["codes"]], list(node["a"])
    if 0 < code < codes[1] and len(codes) >= 3 and vals[1] > 0 and vals[2] > 0:
        c0, v0, c1, v1 = codes[1], vals[1], codes[2], vals[2]
        return v0 * (code / c0) ** (math.log(v1 / v0) / math.log(c1 / c0))
    return code_interp(float(code), codes, vals)


def code_response(prim: dict[str, Any], code: int, level: int) -> float | None:
    """a_c(k, L), normalized to 1 at code 255; None outside the calibrated code or level range."""
    if code == 255:
        return 1.0
    nodes = prim.get("code_nodes")
    if not nodes:
        return code_interp(float(code), [float(c) for c in prim["codes"]], list(prim["a"]))
    if code == 0:
        return 0.0
    br = _bracket(sorted(int(l) for l in nodes), int(level))
    if br is None:
        return None
    vals = []
    for L in br:
        v = _node_response(nodes[str(L)], code)
        if v is None or v <= 0:
            return None
        vals.append(v)
    if len(br) == 1:
        return vals[0]
    t = (level - br[0]) / (br[1] - br[0])
    return vals[0] ** (1.0 - t) * vals[1] ** t


def extra_demand(rgb: tuple[int, int, int] | list[int]) -> float:
    k = [v / 255.0 for v in rgb]
    return sum(k) - max(k)


def other_demand(rgb: tuple[int, int, int] | list[int], i: int) -> float:
    """O_c: drive of the channels other than channel index ``i``."""
    return sum(v / 255.0 for j, v in enumerate(rgb) if j != i)


def attenuation(beta: list[float], level: int, duty: float) -> float:
    """g = beta0 + betaL * L / 8 + betaD * D / 100."""
    return beta[0] + beta[1] * level / MAX_LEVEL + beta[2] * duty / 100.0


def channel_outputs(params: dict[str, Any], rgb: tuple[int, int, int] | list[int], level: int, duty: float,
                    kind: str) -> dict[int, float] | None:
    """Each driven channel's own output (no interaction), by channel index; None when unsupported."""
    out = {}
    for c, i in CHANNEL_INDEX.items():
        if rgb[i] == 0:
            continue
        prim = params["primaries"].get(c)
        if prim is None:
            return None
        v = primary_output(prim, int(rgb[i]), int(level), float(duty), kind)
        if v is None:
            return None
        out[i] = v
    return out


def _crossed_at_level(prim: dict[str, Any], code: int, level: int, duty: float) -> float | None:
    nodes = prim["crossed"].get(str(level))
    if not nodes:
        return None
    duties = sorted(float(d) for d in nodes)
    if not duties or duty < duties[0] or duty > duties[-1]:
        return None
    # Only the bracketing duty nodes enter the interpolation; others may lack this code.
    i = min(max(bisect_right(duties, duty) - 1, 0), len(duties) - 1)
    duties = duties[i:i + 2] if duties[i] != duty else [duties[i]]
    vals = []
    for d in duties:
        node = nodes[f"{d:g}"]
        v = code_interp(float(code), [0.0] + [float(c) for c in node["codes"]], [0.0] + list(node["values"]))
        if v is None:
            return None
        vals.append(v)
    return interp(duty, duties, vals)


def primary_output(prim: dict[str, Any], code: int, level: int, duty: float, kind: str) -> float | None:
    """One primary's output, or None when (code, level, duty) is outside its supported domain."""
    if code == 0:
        return 0.0
    b = prim["levels"].get(str(level))
    if kind == "crossed" and prim.get("crossed"):
        v = _crossed_at_level(prim, code, level, duty)
        if v is not None:
            return v
        grid_levels = sorted(int(l) for l in prim["crossed"])
        if b is None or not grid_levels:
            return None
        nearest = min(grid_levels, key=lambda g: (abs(g - level), -g))
        b_ref = prim["levels"].get(str(nearest))
        v = _crossed_at_level(prim, code, nearest, duty)
        if v is None or not b_ref:
            return None
        return v * b / b_ref
    if b is None:
        return None
    a = code_response(prim, code, level)
    q = interp(duty, [float(d) for d in prim["duties"]], list(prim["q"]))
    if a is None or q is None:
        return None
    return a * b * q


def output(params: dict[str, Any], rgb: tuple[int, int, int] | list[int], level: int, duty: float,
           kind: str, interaction: bool) -> float | None:
    """Predicted output for one uniform RGB stimulus, or None when unsupported."""
    parts = channel_outputs(params, rgb, level, duty, kind)
    if parts is None:
        return None
    total = sum(parts.values())
    beta = params.get("interaction") if interaction else None
    if beta:
        g = attenuation(beta, level, duty)
        if params.get("interaction_form") == "channel":
            total = sum(v * (1.0 - other_demand(rgb, i) * g) for i, v in parts.items())
        else:
            total *= 1.0 - extra_demand(rgb) * g
    return max(0.0, total)
