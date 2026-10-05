"""Experimental, not validated: exposure estimates for rendered content (stdlib only).

The calibration package covers uniform full-frame colors. ``estimate_frame_experimental`` sums the
uniform-color estimates of each color in a frame weighted by its pixel fraction. That does not model
whole-frame power limiting (a frame draws a different panel load than any of its colors shown alone)
or the spatial weighting of the optics toward the corneal plane, and no rendered content has been
measured against it. It is kept outside the validated estimator for exploratory use only.
"""

from __future__ import annotations

from typing import Any, Iterable

from uprtek.model.estimator import SUPPORT_ORDER, _response, estimate

EXPERIMENTAL_WARNING = ("not validated: pixel-weighted sum of uniform full-frame estimates; does not model "
                        "whole-frame power limiting or spatial optical weighting")


def estimate_frame_experimental(package: dict[str, Any] | None, histogram: Iterable[tuple[Iterable[int], float]],
                                brightness_level: int, duty_setting: float, *,
                                configuration: dict[str, Any] | None = None) -> dict[str, Any]:
    """``histogram``: (rgb, pixel fraction) pairs summing to 1. The result's ``support`` is at best
    "interpolated" for more than one color, and ``warnings`` always carries EXPERIMENTAL_WARNING."""
    parts = [(tuple(rgb), float(f)) for rgb, f in histogram if f > 0]
    total_f = sum(f for _, f in parts)
    warnings = [EXPERIMENTAL_WARNING]
    if not parts or abs(total_f - 1.0) > 1e-6:
        return _response(package, "unavailable", "unsupported", warnings + ["pixel fractions must sum to 1"])
    edi = lux = 0.0
    support = "measured"
    for rgb, f in parts:
        r = estimate(package, rgb, brightness_level, duty_setting, configuration=configuration)
        if r["status"] in ("unsupported", "configuration_mismatch", "unavailable"):
            return _response(package, r["status"], "unsupported",
                             warnings + [f"{list(rgb)}: {w}" for w in r["warnings"]])
        edi += f * r["melanopic_edi_lx"]
        lux += f * r["photopic_lux"]
        support = max(support, r["support"], key=SUPPORT_ORDER.index)
    support = "interpolated" if len(parts) > 1 else support
    validity = package["validity"]
    if lux < validity["photopic_min_lux"]:
        return _response(package, "low_signal", support,
                         warnings + [f"predicted photopic illuminance below {validity['photopic_min_lux']:g} lx; "
                                     "DER not returned"], edi, lux)
    return _response(package, "ok", support, warnings, edi, lux, edi / lux)
