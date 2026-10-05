"""Analyze a UPRtek session: per-capture spectrum.csv / summary.json / plots and a session table."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from uprtek.rawio import RawCapture, list_captures, read_capture, read_session
from uprtek.report.constants import (
    AMBIENT_FRACTION_LIMIT,
    INSTRUMENT_TO_W_M2_NM,
    REPEAT_CV_LIMIT_PERCENT,
    UNIT_CHECK_MIN_LUX,
    UNIT_CHECK_TOLERANCE,
    W_M2_TO_UW_CM2,
)
from uprtek.report.plots import plot_normalized_spectrum, plot_spectrum
from uprtek.report.spectral import (
    alphaopic,
    band_integral,
    characterize,
    delta_lambda,
    irradiance,
    photon_spectrum,
    photopic_lux,
)

BLACK_LABEL = "K"

TABLE_COLUMNS = [
    "label",
    "rgb",
    "eye",
    "brightness_level",
    "duty_cycle",
    "quantitative",
    "qc_flags",
    "peak_wavelength_nm",
    "measured_fwhm_nm",
    "spectral_centroid_nm",
    "irradiance_380_500_W_m2",
    "irradiance_380_780_W_m2",
    "irradiance_430_470_uW_cm2",
    "offband_fraction_500_780",
    "photon_irradiance_photons_cm2_s",
    "photopic_illuminance_lux",
    "instrument_lux",
    "melanopic_EDI_lux",
    "s_cone_opic_EDI_lux",
    "repeat_cv_percent",
]


def to_w_m2_nm(raw: RawCapture) -> np.ndarray:
    return np.asarray(raw.repeats, dtype=float) * INSTRUMENT_TO_W_M2_NM


def analyze_session(session_dir: str | Path, output_dir: str | Path | None = None) -> list[dict[str, Any]]:
    session_dir = Path(session_dir)
    output_dir = Path(output_dir) if output_dir else session_dir / "analysis"
    session = read_session(session_dir)
    captures = [read_capture(p) for p in list_captures(session_dir)]
    if not captures:
        raise FileNotFoundError(f"no captures in {session_dir / 'raw'}")

    black = next((c for c in captures if c.label == BLACK_LABEL), None)
    black_mean = to_w_m2_nm(black).mean(axis=0) if black is not None else None

    summaries = []
    for cap in captures:
        summary = analyze_capture(
            cap,
            output_dir / cap.label,
            session=session,
            black_mean=None if cap is black else black_mean,
        )
        summaries.append(summary)
    write_table(output_dir / "summary.csv", summaries)
    return summaries


def analyze_capture(
    cap: RawCapture,
    dest: Path,
    *,
    session: dict[str, Any] | None = None,
    black_mean: np.ndarray | None = None,
) -> dict[str, Any]:
    session = session or {}
    meta = cap.meta
    dest.mkdir(parents=True, exist_ok=True)
    wl = np.asarray(cap.wavelength_nm, dtype=float)
    reps = to_w_m2_nm(cap)
    e = reps.mean(axis=0)
    e_std = reps.std(axis=0, ddof=1) if len(reps) > 1 else np.zeros_like(e)
    dlam = delta_lambda(wl)
    rgb = tuple(meta["rgb"]) if meta.get("rgb") else None
    host_state = meta.get("host_state") or {}

    flags: list[str] = list(meta.get("flags") or [])

    per_rep_total = [band_integral(wl, np.maximum(0.0, r), dlam, 380.0, 780.0) for r in reps]
    mean_total = float(np.mean(per_rep_total))
    cv = (
        float(100.0 * np.std(per_rep_total, ddof=1) / mean_total)
        if len(per_rep_total) > 1 and mean_total > 0
        else float("nan")
    )
    if math.isfinite(cv) and cv > REPEAT_CV_LIMIT_PERCENT:
        flags.append("REPEAT_CV_HIGH")

    repeats_meta = meta.get("repeats") or []
    inst_lux = [r.get("instrument", {}).get("lux") for r in repeats_meta]
    inst_lux = [v for v in inst_lux if v is not None]
    instrument_lux = float(np.mean(inst_lux)) if inst_lux else None
    rep_lux = [photopic_lux(wl, r) for r in reps]
    computed_lux = float(np.mean(rep_lux))
    lux_ratio = None
    if instrument_lux is not None and instrument_lux >= UNIT_CHECK_MIN_LUX:
        lux_ratio = computed_lux / instrument_lux
        if abs(lux_ratio - 1.0) > UNIT_CHECK_TOLERANCE:
            flags.append("UNIT_CHECK_MISMATCH")
    strengths = [r.get("light_strength") for r in repeats_meta]
    if strengths and all(s == 0 for s in strengths):
        flags.append("LOW_LIGHT")

    char = characterize(wl, e, rgb)
    irr = irradiance(wl, e)
    alpha = alphaopic(wl, e, irr.photopic_illuminance_lux)
    is_black = rgb == (0, 0, 0)
    if not is_black:
        if not char.fwhm_resolved:
            flags.append("FWHM_UNRESOLVED")
        if not char.peak_in_search_range:
            flags.append("PEAK_OUTSIDE_SEARCH")

    increment = None
    if black_mean is not None and black_mean.shape == e.shape:
        increment = e - black_mean
        black_vis = band_integral(wl, np.maximum(0.0, black_mean), dlam, 380.0, 780.0)
        if irr.irradiance_380_780_W_m2 > 0 and black_vis > AMBIENT_FRACTION_LIMIT * irr.irradiance_380_780_W_m2:
            flags.append("AMBIENT_PRESENT")

    disqualifying = {"OUT_OF_RANGE", "OVER_EXPOSED", "READBACK_MISMATCH", "UNIT_CHECK_MISMATCH",
                     "NO_DISPLAY_LIGHT", "REPEAT_OUTLIER", "DUTY_MISMATCH", "ANCHOR_DRIFT"}
    quantitative = not (disqualifying & set(flags))
    meter = session.get("meter") or meta.get("meter") or {}

    summary: dict[str, Any] = {
        "source": str(cap.csv_path),
        "instrument": "UPRtek",
        "meter_model": meter.get("model"),
        "meter_optical_sn": meter.get("optical_sn"),
        "meter_fw": meter.get("fw_version"),
        "label": cap.label,
        "device": host_state.get("device"),
        "backend": host_state.get("backend"),
        "eye": meta.get("eye"),
        "rgb": list(rgb) if rgb else None,
        "brightness_level": _int_or_none(host_state.get("brightness")),
        "requested_brightness": meta.get("requested_brightness"),
        "brightness_source": meta.get("brightness_source"),
        "duty_cycle": _int_or_none(host_state.get("duty_cycle")),
        "requested_duty_cycle": meta.get("requested_duty_cycle"),
        "film": _float_or_none(host_state.get("film")),
        "display_mode": host_state.get("display_mode"),
        "calibration_version": host_state.get("calibration"),
        "readback": meta.get("readback"),
        "absolute_scale": "instrument_factory_calibration",
        "certainty": "directly_measured",
        "exposure_plane": "corneal-plane irradiance (cosine-corrected probe at eye position)",
        "spectrum_unit_factor_to_W_m2_nm": INSTRUMENT_TO_W_M2_NM,
        "n_repeats": int(len(reps)),
        "repeat_irradiance_380_780_W_m2": per_rep_total,
        "repeat_cv_percent": cv,
        "exposure_time_raw": [r.get("instrument", {}).get("exposure_time_raw") for r in repeats_meta],
        "light_strength": strengths,
        "instrument_lux": instrument_lux,
        "instrument_cct_K": _mean([r.get("instrument", {}).get("cct_K") for r in repeats_meta]),
        "instrument_cie1931_x": _mean([r.get("instrument", {}).get("cie1931_x") for r in repeats_meta]),
        "instrument_cie1931_y": _mean([r.get("instrument", {}).get("cie1931_y") for r in repeats_meta]),
        "computed_to_instrument_lux_ratio": lux_ratio,
        "peak_wavelength_nm": char.peak_wavelength_nm,
        "measured_fwhm_nm": char.measured_fwhm_nm,
        "lambda_half_left_nm": char.lambda_half_left_nm,
        "lambda_half_right_nm": char.lambda_half_right_nm,
        "spectral_centroid_nm": char.spectral_centroid_nm,
        "global_peak_wavelength_nm": char.global_peak_wavelength_nm,
        **irr.__dict__,
        "s_cone_opic_EDI_lux": alpha.edi_lux["s_cone"],
        "m_cone_opic_EDI_lux": alpha.edi_lux["m_cone"],
        "l_cone_opic_EDI_lux": alpha.edi_lux["l_cone"],
        "rhodopic_EDI_lux": alpha.edi_lux["rhodopic"],
        "melanopic_EDI_lux": alpha.edi_lux["melanopic"],
        "s_cone_opic_DER": alpha.der["s_cone"],
        "m_cone_opic_DER": alpha.der["m_cone"],
        "l_cone_opic_DER": alpha.der["l_cone"],
        "rhodopic_DER": alpha.der["rhodopic"],
        "melanopic_DER": alpha.der["melanopic"],
        "qc_flags": flags,
        "quantitative": quantitative,
    }
    if increment is not None:
        summary["display_increment_irradiance_380_500_W_m2"] = band_integral(wl, increment, dlam, 380.0, 500.0)
        summary["display_increment_irradiance_380_780_W_m2"] = band_integral(wl, increment, dlam, 380.0, 780.0)

    write_spectrum_csv(dest / "spectrum.csv", wl, e, e_std, increment)
    (dest / "summary.json").write_text(json.dumps(_json_safe(summary), indent=2) + "\n", encoding="utf-8")
    if not is_black and np.any(e > 0):
        title = (f"{cap.label}  rgb={list(rgb) if rgb else '-'}  bri={summary['brightness_level']}"
                 f"  duty={summary['duty_cycle']}")
        plot_spectrum(dest / "spectrum.png", wl, e, title=title)
        plot_normalized_spectrum(dest / "spectrum_normalized.png", wl, e, title=title)
    return summary


def write_spectrum_csv(
    path: Path, wl: np.ndarray, e: np.ndarray, e_std: np.ndarray, increment: np.ndarray | None
) -> None:
    n = photon_spectrum(wl, np.maximum(0.0, e))
    header = [
        "wavelength_nm",
        "spectral_irradiance_W_m2_nm",
        "spectral_irradiance_std_W_m2_nm",
        "spectral_irradiance_uW_cm2_nm",
        "photon_irradiance_photons_m2_s_nm",
    ]
    if increment is not None:
        header.append("display_increment_W_m2_nm")
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for i in range(len(wl)):
            row = [f"{wl[i]:g}", f"{e[i]:.8e}", f"{e_std[i]:.8e}", f"{e[i] * W_M2_TO_UW_CM2:.8e}", f"{n[i]:.8e}"]
            if increment is not None:
                row.append(f"{increment[i]:.8e}")
            w.writerow(row)


def write_table(path: Path, summaries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(TABLE_COLUMNS)
        for s in summaries:
            row = []
            for col in TABLE_COLUMNS:
                v = s.get(col)
                if isinstance(v, list):
                    v = " ".join(str(x) for x in v)
                elif isinstance(v, float):
                    v = "" if not math.isfinite(v) else f"{v:.6g}"
                row.append("" if v is None else v)
            w.writerow(row)


def _mean(values: list[Any]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return float(np.mean(vals)) if vals else None


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj
