"""Settings-to-exposure estimator (stdlib only): calibration package + display state -> exposure.

    pkg = load_package("calibration.json")
    estimate(pkg, rgb=(0, 0, 192), brightness_level=4, duty_setting=42)

returns melanopic EDI and photopic illuminance (lx), melanopic DER, a ``status`` (ok, low_signal,
unsupported, configuration_mismatch, unavailable), a ``support`` (measured, interpolated,
extrapolated, unsupported) and ``targets_met`` (whether the package's model met its accuracy
targets; a warning is added when it did not). The only extrapolation is in code: a code below the
lowest code node at its level follows the lowest power-law segment toward code 0 (support
``extrapolated``, with a warning). Nothing is clamped into range: levels or duty cycles outside the
calibration, full white above the package's white limit, and any multi-channel stimulus above its
multi-channel limit or below the lowest level the mixtures were calibrated at are unsupported.

The estimator covers uniform full-frame colors only. A pixel-weighted estimate for rendered content
is in ``uprtek.model.experimental`` and is not validated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from uprtek.model import forward

PACKAGE_SCHEMA = "chronolume-exposure-calibration/1"
CONFIG_KEYS = ("device", "firmware", "product_id", "film", "display_mode")
SUPPORT_ORDER = ("measured", "interpolated", "extrapolated", "unsupported")
CODE_EXTRAPOLATED = "a code is below the lowest code node at this level (code response extrapolated)"
TARGETS_NOT_MET = "the package's model did not meet its accuracy targets"


def load_package(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def targets_met(package: dict[str, Any] | None) -> bool | None:
    """Validation result when the package has one, else the cross-validation selection result."""
    if not package:
        return None
    v = package.get("validation") or {}
    if "meets_targets" in v:
        return bool(v["meets_targets"])
    sel = package.get("selection") or {}
    return bool(sel["meets_targets"]) if "meets_targets" in sel else None


def _response(package: dict[str, Any] | None, status: str, support: str,
              warnings: list[str] | None = None, edi: float | None = None, lux: float | None = None,
              der: float | None = None) -> dict[str, Any]:
    error_ref = None
    if package:
        v = package.get("validation") or {}
        error_ref = v.get("metrics") or (package.get("selection") or {}).get("candidates", {}).get(
            (package.get("model") or {}).get("name"))
        if error_ref:
            error_ref = {k: error_ref[k] for k in ("edi", "lux", "der") if k in error_ref}
    met = targets_met(package)
    warnings = list(warnings or [])
    if met is False:
        warnings.append(TARGETS_NOT_MET)
    return {
        "melanopic_edi_lx": edi,
        "photopic_lux": lux,
        "melanopic_der": der,
        "status": status,
        "support": support,
        "targets_met": met,
        "empirical_error_reference": error_ref,
        "warnings": warnings,
        "calibration_id": package.get("calibration_id") if package else None,
    }


def multi_channel_level_ok(validity: dict[str, Any], rgb: Iterable[int], level: int) -> bool:
    """False for a multi-channel stimulus below the lowest level the mixtures were calibrated at."""
    lo = validity.get("multi_channel_min_level")
    return lo is None or sum(1 for v in rgb if v > 0) < 2 or int(level) >= lo


def _mismatch(package: dict[str, Any], configuration: dict[str, Any] | None) -> list[str]:
    if not configuration:
        return []
    ref = package.get("configuration") or {}
    return [f"{k}: package {ref.get(k)!r}, given {configuration[k]!r}"
            for k in CONFIG_KEYS if k in configuration and ref.get(k) is not None
            and str(configuration[k]) != str(ref.get(k))]


def estimate(package: dict[str, Any] | None, rgb: Iterable[int], brightness_level: int, duty_setting: float,
             *, configuration: dict[str, Any] | None = None,
             stimulus_geometry: str = "uniform_full_frame") -> dict[str, Any]:
    if not package or package.get("schema") != PACKAGE_SCHEMA:
        return _response(package, "unavailable", "unsupported", ["no valid calibration package"])
    # Step 0: the device configuration must match the one the package was calibrated in.
    diffs = _mismatch(package, configuration)
    if diffs:
        return _response(package, "configuration_mismatch", "unsupported", diffs)
    validity = package["validity"]
    if stimulus_geometry != validity.get("stimulus_geometry", "uniform_full_frame"):
        return _response(package, "unsupported", "unsupported", [f"geometry {stimulus_geometry!r} not calibrated"])
    rgb = tuple(int(v) for v in rgb)
    level = int(brightness_level)
    if len(rgb) != 3 or not all(0 <= v <= 255 for v in rgb) or not 0 <= level <= 8 or not 0 <= duty_setting <= 100:
        return _response(package, "unsupported", "unsupported", ["input outside the device ranges"])
    if rgb[0] == rgb[1] == rgb[2] > 0 and level > validity["white_max_level"]:
        return _response(package, "unsupported", "unsupported",
                         [f"white above brightness {validity['white_max_level']} is a known failure state"])
    if sum(1 for v in rgb if v > 0) > 1 and level > validity["multi_channel_max_level"]:
        return _response(package, "unsupported", "unsupported",
                         [f"multi-channel stimuli are calibrated up to brightness {validity['multi_channel_max_level']}"])
    if not multi_channel_level_ok(validity, rgb, level):
        return _response(package, "unsupported", "unsupported",
                         [f"multi-channel stimuli are calibrated from brightness "
                          f"{validity['multi_channel_min_level']} (the mixture interaction is not extrapolated)"])
    model = package["model"]
    outs = package["outputs"]
    edi = forward.output(outs["melanopic_edi"], rgb, level, duty_setting, model["kind"], model["interaction"])
    lux = forward.output(outs["photopic"], rgb, level, duty_setting, model["kind"], model["interaction"])
    if lux is None:
        return _response(package, "unsupported", "unsupported",
                         ["setting outside the calibrated domain (code below the lowest calibrated code, "
                          "or level or duty cycle not calibrated)"])
    key = "{},{},{},{},{}".format(*rgb, level, int(duty_setting))
    measured = float(duty_setting).is_integer() and key in set(package.get("calibration_settings", []))
    support = "measured" if measured else "interpolated"
    notes = []
    if code_extrapolated(outs, rgb, level):
        support = "extrapolated"
        notes.append(CODE_EXTRAPOLATED)
    # Step 6, per output: melanopic parameters exist only where the calibration's melanopic signal
    # passed the eligibility test, so the melanopic domain can be smaller than the photopic one.
    if edi is None:
        return _response(package, "low_signal", support,
                         notes + ["melanopic signal below the calibrated melanopic range for this setting; "
                                  "melanopic EDI and DER are not returned"], None, lux)
    if lux < validity["photopic_min_lux"]:
        return _response(package, "low_signal", support,
                         notes + [f"predicted photopic illuminance below {validity['photopic_min_lux']:g} lx "
                                  "(meter range); values are approximate and DER is not returned"], edi, lux)
    der = edi / lux if lux >= validity["der_min_photopic_lux"] else None
    return _response(package, "ok", support, notes, edi, lux, der)


def code_extrapolated(outputs: dict[str, Any], rgb: Iterable[int], level: int) -> bool:
    """True when a driven channel's code is below the lowest code node at ``level`` for either output."""
    for out in outputs.values():
        for c, i in forward.CHANNEL_INDEX.items():
            prim = out["primaries"].get(c)
            k = list(rgb)[i]
            if prim and prim.get("code_nodes") and 0 < k < 255 and k < forward.lowest_code(prim, level):
                return True
    return False
