import json
import math

import pytest

pytest.importorskip("numpy")
pytest.importorskip("matplotlib")

import numpy as np

from uprtek.rawio import write_capture
from uprtek.report.analyze import analyze_session
from uprtek.report.linearity import run_linearity
from uprtek.report.spectral import photopic_lux

WL = [float(w) for w in range(380, 781)]


def _spectrum_mw(peak_mw, center=450.0, floor=0.0):
    return [floor + peak_mw * math.exp(-0.5 * ((w - center) / 9.0) ** 2) for w in WL]


def _write(tmp_path, label, rgb, spectra, *, lux_scale=1.0, brightness=None, flags=None, duty=98):
    reps = []
    for s in spectra:
        lux = photopic_lux(np.array(WL), np.array(s) * 1e-3) * lux_scale
        reps.append({"rep": len(reps), "light_strength": 1, "instrument": {"lux": lux}})
    meta = {
        "rgb": list(rgb),
        "eye": "both",
        "requested_brightness": brightness,
        "host_state": {"brightness": str(brightness if brightness is not None else 5),
                       "duty_cycle": str(duty), "film": "0", "calibration": "cal_test",
                       "backend": "viture 2.4.0", "device": "Luma Ultra"},
        "readback": {"left": list(rgb), "right": list(rgb)},
        "repeats": reps,
        "flags": flags or [],
    }
    write_capture(tmp_path, label, WL, spectra, meta)


def test_analyze_session_outputs_and_metrics(tmp_path):
    _write(tmp_path, "K", (0, 0, 0), [[0.001] * len(WL)] * 2)
    _write(tmp_path, "B255", (0, 0, 255), [_spectrum_mw(10.0), _spectrum_mw(10.2)])
    summaries = {s["label"]: s for s in analyze_session(tmp_path)}
    b = summaries["B255"]
    assert b["peak_wavelength_nm"] == 450
    assert b["measured_fwhm_nm"] == pytest.approx(21.2, abs=0.2)
    assert b["quantitative"] is True
    assert "UNIT_CHECK_MISMATCH" not in b["qc_flags"]
    assert b["computed_to_instrument_lux_ratio"] == pytest.approx(1.0, abs=1e-6)
    assert b["irradiance_380_500_W_m2"] == pytest.approx(10.1e-3 * 9.0 * math.sqrt(2 * math.pi), rel=1e-3)
    assert b["repeat_cv_percent"] < 5
    assert "display_increment_irradiance_380_500_W_m2" in b
    out = tmp_path / "analysis"
    assert (out / "B255" / "spectrum.csv").exists()
    assert (out / "B255" / "spectrum.png").exists()
    assert (out / "summary.csv").read_text().startswith("label,")
    saved = json.loads((out / "B255" / "summary.json").read_text())
    assert saved["absolute_scale"] == "instrument_factory_calibration"


def test_unit_mismatch_is_flagged_and_not_quantitative(tmp_path):
    _write(tmp_path, "B255", (0, 0, 255), [_spectrum_mw(10.0)], lux_scale=2.0)
    (s,) = analyze_session(tmp_path)
    assert "UNIT_CHECK_MISMATCH" in s["qc_flags"]
    assert s["quantitative"] is False


def test_blue_code_gamma_fit(tmp_path):
    for b in (64, 128, 255):
        _write(tmp_path, f"B{b:03d}", (0, 0, b), [_spectrum_mw(10.0 * (b / 255) ** 2.2)])
    _write(tmp_path, "B255_br3", (0, 0, 255), [_spectrum_mw(3.0)], brightness=3)
    analyze_session(tmp_path)
    fit = run_linearity(tmp_path / "analysis", tmp_path / "analysis", "blue_code", "irradiance_380_500_W_m2")
    assert fit.model == "power_law"
    assert fit.labels == ["B064", "B128", "B255"]
    assert fit.gamma == pytest.approx(2.2, abs=1e-3)
    assert fit.r_squared_log == pytest.approx(1.0, abs=1e-6)
    assert max(abs(d) for d in fit.percent_deviation_from_fit) < 0.1
    assert (tmp_path / "analysis" / "linearity_blue_code.png").exists()


def test_brightness_steps(tmp_path):
    for level, peak in ((1, 1.0), (3, 2.0), (4, 2.5)):  # level 2 missing on purpose
        _write(tmp_path, f"B255_br{level}", (0, 0, 255), [_spectrum_mw(peak)], brightness=level)
    analyze_session(tmp_path)
    fit = run_linearity(tmp_path / "analysis", tmp_path / "analysis", "brightness", "irradiance_380_500_W_m2")
    assert fit.model == "steps"
    assert [(s["from_level"], s["to_level"]) for s in fit.steps] == [(1, 3), (3, 4)]
    assert [s["ratio"] for s in fit.steps] == pytest.approx([2.0, 1.25], rel=1e-6)
    assert fit.fraction_of_max == pytest.approx([0.4, 0.8, 1.0], rel=1e-6)
    assert (tmp_path / "analysis" / "linearity_brightness.png").exists()


def test_duty_response_and_duty_block_kept_out_of_brightness(tmp_path):
    _write(tmp_path, "B255_br8", (0, 0, 255), [_spectrum_mw(10.0)], brightness=8, duty=98)
    _write(tmp_path, "B255_br4", (0, 0, 255), [_spectrum_mw(4.0)], brightness=4, duty=98)
    for d in (30, 42):
        _write(tmp_path, f"B255_d{d}", (0, 0, 255), [_spectrum_mw(10.0 * d / 98)], brightness=8, duty=d)
    analyze_session(tmp_path)
    fit = run_linearity(tmp_path / "analysis", tmp_path / "analysis", "duty", "irradiance_380_500_W_m2")
    assert fit.model == "linear"
    assert fit.x == [30.0, 42.0, 98.0]
    assert fit.fraction_of_max == pytest.approx([30 / 98, 42 / 98, 1.0], rel=1e-6)
    assert fit.intercept == pytest.approx(0.0, abs=1e-9)
    assert (tmp_path / "analysis" / "linearity_duty.png").exists()
    steps = run_linearity(tmp_path / "analysis", tmp_path / "analysis", "brightness", "irradiance_380_500_W_m2")
    assert steps.labels == ["B255_br4", "B255_br8"]
