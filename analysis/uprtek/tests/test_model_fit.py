import copy
import json

import pytest

pytest.importorskip("numpy")

from uprtek.model import estimator, experimental, forward
from uprtek.model.capture import load_grid, stage_settings
from uprtek.model.fit import (BACKGROUND_REL_FLOOR, INTERACTION_RIDGE, MELANOPIC_MIN_EDI, SIGNAL_TO_NOISE,
                              Setting, average, background_noise, composition, fallback_candidate,
                              fit_candidate, fit_package, load_rows, meets_targets, metrics, session_anchor,
                              validate_package)

# Synthetic device: a_c(k, L) = (k/255)^gamma(L) with gamma steeper at lower levels, b(L) = (L+1)/9,
# q(D) = D/98, per-channel scales at code 255, level 8, duty 98; mixtures lose 10 % per unit of extra
# demand. Black adds a floor. A power law is exact on the code nodes' power-law segments (and their
# downward extension), and gamma linear in L makes a_c exactly log-linear in level between ladder levels.
SCALE = {"edi": (0.5, 20.0, 200.0), "lux": (30.0, 200.0, 20.0)}
FLOOR = {"edi": 0.02, "lux": 0.05}
ATTENUATION = 0.10
CONFIG = {"device": "Fake Luma Ultra", "firmware": "fw1", "product_id": "4356", "film": "0.000000",
          "display_mode": "50"}


def gamma(level):
    return 2.0 + 0.15 * (8 - level)


def truth(rgb, level, duty, attr):
    total = sum(SCALE[attr][i] * (k / 255) ** gamma(level) * (level + 1) / 9 * duty / 98
                for i, k in enumerate(rgb))
    return total * (1 - ATTENUATION * forward.extra_demand(rgb))


def _write_session(root, name, captures, *, stage=None, session_index=None):
    d = root / name
    (d / "raw").mkdir(parents=True)
    session = {"session_index": session_index, "model_stage": stage, "host": CONFIG,
               "measure_options": {"eye": "both"}, "wear_detection": "disabled",
               "grid": {"version": "v1"} if stage else None,
               "duty_acceptance": {"duties": [98, 42, 30]} if stage else None}
    (d / "session.json").write_text(json.dumps(session))
    for label, rgb, level, duty, role, block in captures:
        meta = {"label": label, "rgb": list(rgb), "role": role, "block_index": block,
                "host_state": {"brightness": str(level), "duty_cycle": str(duty)}}
        (d / "raw" / f"{label}.json").write_text(json.dumps(meta))
        (d / "raw" / f"{label}.csv").write_text("wavelength_nm,rep0\n380,0\n")
        lux = truth(rgb, level, duty, "lux") + FLOOR["lux"]
        summary = {"label": label, "melanopic_EDI_lux": truth(rgb, level, duty, "edi") + FLOOR["edi"],
                   "photopic_illuminance_lux": lux, "quantitative": True}
        (d / "analysis" / label).mkdir(parents=True)
        (d / "analysis" / label / "summary.json").write_text(json.dumps(summary))
    (d / "analysis" / "summary.csv").write_text("label\n")
    return d


def _model_session(root, stage, index):
    caps, blocks = [], {}
    for s in stage_settings(load_grid(), stage):
        b = blocks.setdefault((s.level, s.duty), len(blocks))
        caps.append((s.label, s.rgb, s.level, s.duty, "stimulus", b))
    for (level, duty), b in blocks.items():
        caps.append((f"K_L{level}_D{duty}_start", (0, 0, 0), level, duty, "black_start", b))
        caps.append((f"K_L{level}_D{duty}_end", (0, 0, 0), level, duty, "black_end", b))
    return _write_session(root, f"model_{stage}_s{index}", caps, stage=stage, session_index=index)


def _reference_session(root):
    caps = [("K", (0, 0, 0), 8, 98, "stimulus", None)]
    for c, rgb in (("R", (255, 0, 0)), ("G", (0, 255, 0)), ("B", (0, 0, 255))):
        caps += [(f"{c}255_br{n}", rgb, n, 98, "stimulus", None) for n in range(9)]
    return _write_session(root, "color_set", caps)


@pytest.fixture(scope="module")
def package_and_report(tmp_path_factory):
    root = tmp_path_factory.mktemp("model")
    cal = [_model_session(root, stage, i) for stage in ("primaries", "ladders", "mixtures") for i in (1, 2)]
    ref = [_reference_session(root)]
    val = [_model_session(root, "validation", 1)]
    pkg = fit_package(cal, ref, calibration_id="test-cal")
    report = validate_package(pkg, val)
    pkg["validation"] = {k: report[k] for k in ("sessions", "metrics", "meets_targets", "created",
                                                "validation_duties", "duty_heldout")}
    return pkg, report


def test_fit_recovers_factors_and_selects_interaction(package_and_report):
    pkg, _ = package_and_report
    sel = pkg["selection"]
    assert sel["chosen"] == "factorized+H" and sel["meets_targets"]
    assert sel["candidates"]["factorized"]["n_unsupported"] == 0
    assert sel["candidates"]["factorized"]["method"].startswith("leave-one-session-out")
    assert sel["candidates"]["factorized"]["edi"]["mape"] > 1.0  # mixtures miss without H
    blue = pkg["outputs"]["melanopic_edi"]["primaries"]["B"]
    assert blue["levels"]["8"] == pytest.approx(200.0, rel=1e-6)
    assert blue["levels"]["2"] == pytest.approx(200.0 * 3 / 9, rel=1e-6)  # from the reference session
    assert dict(zip(blue["duties"], blue["q"]))[42] == pytest.approx(42 / 98, rel=1e-6)
    assert set(blue["code_nodes"]) == {"2", "4", "5", "7", "8"} and blue["min_code"] == 64
    for lv, node in blue["code_nodes"].items():
        assert node["codes"][0] == 0 and node["codes"][-1] == 255
        for k, a in zip(node["codes"][1:-1], node["a"][1:-1]):
            assert a == pytest.approx((k / 255) ** gamma(int(lv)), rel=1e-6)
    assert blue["code_nodes"]["8"]["codes"] == [0, 64, 128, 192, 255]
    assert blue["code_nodes"]["4"]["codes"] == [0, 128, 192, 255]  # code 64 is below the meter range there
    assert forward.code_response(blue, 96, 3) == pytest.approx((96 / 255) ** gamma(3), rel=1e-6)
    assert forward.code_response(blue, 32, 4) == pytest.approx((32 / 255) ** gamma(4), rel=1e-6)  # extended
    assert forward.code_response(blue, 128, 1) is None  # below the ladder levels
    assert pkg["outputs"]["photopic"]["interaction"][0] == pytest.approx(ATTENUATION, abs=0.02)
    assert pkg["configuration"]["firmware"] == "fw1" and not pkg["warnings"]


def test_validation_on_unseen_codes_and_levels(package_and_report):
    _, report = package_and_report
    m = report["metrics"]
    assert m["n_unsupported"] == 0 and m["n_failed"] == 0
    assert m["n_settings"] == 48 and report["n_non_quantitative"] == 0
    assert m["lux"]["n_ineligible"] > 0 and m["lux"]["n"] == m["lux"]["n_eligible"]
    assert m["edi"]["mape"] < 1.0 and m["lux"]["mape"] < 1.0 and m["der"]["n"] > 0
    assert m["lux"]["mae_all"] is not None and m["lux"]["n_all"] == 48
    assert set(m["by_group"]) == {"all", "primary", "mixture"}
    assert m["by_group"]["mixture"]["lux"]["n"] > 0 and "RG" in m["strata"]["composition"]
    assert report["meets_targets"]
    assert not report["duty_heldout"] and report["validation_duties"] == [42, 98]
    assert len(report["predictions"]) == m["n_settings"]
    assert {"eligible_edi", "eligible_lux", "composition"} <= set(report["predictions"][0])


def _setting(edi, lux, lux_raw, *, edi_noise=0.0, lux_noise=0.0, rgb=(0, 0, 255)):
    return Setting(rgb, 6, 42, edi, lux, lux_raw, 1, ("s",), edi_noise, lux_noise)


def test_eligibility_is_measurement_based_and_per_output():
    assert _setting(5.0, 5.0, 5.2).eligible("edi") and _setting(5.0, 5.0, 5.2).eligible("lux")
    below = _setting(0.5, 0.5, 0.6)  # gross photopic below the meter's range: neither output
    assert not below.in_range and not below.eligible("edi") and not below.eligible("lux")
    weak_mel = _setting(0.2, 30.0, 30.1, edi_noise=0.1)  # melanopic net under 3x background noise
    assert weak_mel.eligible("lux") and not weak_mel.eligible("edi")
    assert not _setting(-0.1, 30.0, 30.1).eligible("edi")  # log fit needs a positive target
    floor = _setting(MELANOPIC_MIN_EDI * 0.6, 30.0, 30.1)  # melanopic at the meter floor: fitted, not scored
    assert not floor.eligible("edi") and floor.eligible("edi", floor=False) and floor.eligible("lux")
    assert background_noise([1.0, 1.4]) == pytest.approx(0.4)
    assert background_noise([1.0]) == pytest.approx(BACKGROUND_REL_FLOOR * 1.0)


def test_underprediction_counts_and_failures_stay_in_metrics():
    s = _setting(10.0, 10.0, 10.1)
    m = metrics([(s, {"edi": 9.0, "lux": 0.5}), (_setting(0.4, 0.4, 0.5), {"edi": 0.3, "lux": 0.3})])
    assert m["lux"]["n"] == 1 and m["lux"]["mape"] == pytest.approx(95.0)  # predicted < 1 lx still scored
    assert m["n_pred_below_range"] == 1 and m["lux"]["n_ineligible"] == 1 and m["lux"]["n_all"] == 2
    m = metrics([(s, {"edi": None, "lux": None})])
    assert m["n_failed"] == 1 and m["edi"]["n_failed"] == 1 and not meets_targets(m)
    assert composition((192, 96, 0)) == "RG" and composition((64, 64, 64)) == "gray"
    assert composition((192, 96, 80)) == "RGB_unequal" and composition((0, 0, 0)) == "black"


def test_package_documents_fit_and_cv(package_and_report):
    pkg, _ = package_and_report
    fit = pkg["fit"]
    assert fit["interaction"]["ridge_lambda"] == INTERACTION_RIDGE
    assert fit["eligibility"]["signal_to_noise"] == SIGNAL_TO_NOISE
    assert fit["eligibility"]["melanopic_min_edi"] == MELANOPIC_MIN_EDI
    assert "refit" in fit["cross_validation"]
    assert pkg["selection"]["anchor_normalized"] is None  # the synthetic sessions have no anchors
    assert set(pkg["selection"]["candidates"]) == {"factorized", "factorized+H", "factorized+Hc",
                                                   "factorized+Hc+Rder", "crossed", "crossed+H", "crossed+Hc"}
    assert pkg["model"]["melanopic_from_photopic"] == []
    assert "melanopic_from_photopic" in fit


def test_red_melanopic_from_photopic_and_der(package_and_report):
    pkg, _ = package_and_report
    rder, hc = pkg["selection"]["candidates"]["factorized+Hc+Rder"], pkg["selection"]["candidates"]["factorized+Hc"]
    assert rder["n_failed"] == 0  # the synthetic DER is exact, so only the interaction form limits it
    assert rder["edi"]["mape"] == pytest.approx(hc["edi"]["mape"], rel=1e-6)
    rows = [r for d in pkg["training_sessions"] for r in load_rows(d)]
    ref = average([r for d in pkg["reference_sessions"] for r in load_rows(d, reference=True)])
    out = fit_candidate(average(rows), ref, "factorized", "channel", ("R",))
    red_mel, red_lux = out["melanopic_edi"]["primaries"]["R"], out["photopic"]["primaries"]["R"]
    der = SCALE["edi"][0] / SCALE["lux"][0]
    assert red_mel["melanopic_der"] == pytest.approx(der, rel=1e-6)
    assert red_mel["code_nodes"] == red_lux["code_nodes"] and red_mel["q"] == red_lux["q"]
    for L, v in red_lux["levels"].items():
        assert red_mel["levels"][L] == pytest.approx(v * der, rel=1e-9)
    assert "melanopic_der" not in out["melanopic_edi"]["primaries"]["B"]  # other channels keep their own fit
    assert out["melanopic_edi"]["interaction"] is not None
    assert forward.output(out["melanopic_edi"], (128, 0, 64), 6, 42, "factorized", True) == pytest.approx(
        truth((128, 0, 64), 6, 42, "edi"), rel=0.02)


def test_fallback_prefers_no_failed_predictions():
    def m(failed, edi, lux):
        return {"n_failed": failed, "edi": {"n": 1, "mape": edi}, "lux": {"n": 1, "mape": lux}}
    sel = {"factorized+Hc": m(0, 6.4, 4.7), "crossed+H": m(188, 2.8, 2.7), "factorized": m(0, 7.8, 6.0)}
    assert fallback_candidate(sel) == "factorized+Hc"


def test_session_anchor_is_the_mean_photopic_reading(tmp_path):
    d = tmp_path / "anchors" / "raw"
    d.mkdir(parents=True)
    for i, vals in enumerate(([29.0, 31.0], [30.0])):
        (d / f"ANCHOR_{i:02d}.json").write_text(json.dumps({"repeats": [{"instrument": {"lux": v}} for v in vals]}))
    assert session_anchor(tmp_path) == pytest.approx(30.0)
    assert session_anchor(tmp_path / "missing") is None


def test_channel_interaction_attenuates_each_channel_by_the_others_drive():
    prim = {"codes": [0, 64, 255], "a": [0.0, 0.1, 1.0], "levels": {"7": 10.0}, "duties": [98], "q": [1.0]}
    params = {"primaries": {"R": dict(prim), "B": {**prim, "levels": {"7": 2.0}}},
              "interaction": [0.2, 0.0, 0.0], "interaction_form": "channel"}
    # R 10 lx with B at 64 (O_R = 64/255); B at code 64 is 2 * 64/255 lx with R at 255 (O_B = 1)
    b64 = forward.code_response(params["primaries"]["B"], 64, 7) * 2.0
    want = 10.0 * (1 - 64 / 255 * 0.2) + b64 * (1 - 1.0 * 0.2)
    assert forward.output(params, (255, 0, 64), 7, 98, "factorized", True) == pytest.approx(want)
    total = {**params, "interaction_form": "total"}
    assert forward.output(total, (255, 0, 64), 7, 98, "factorized", True) == pytest.approx(
        (10.0 + b64) * (1 - 64 / 255 * 0.2))
    assert forward.output(params, (0, 0, 64), 7, 98, "factorized", True) == pytest.approx(b64)  # single channel


def test_estimator_status_and_support(package_and_report):
    pkg, _ = package_and_report
    r = estimator.estimate(pkg, (0, 0, 128), 8, 42)
    assert r["status"] == "ok" and r["support"] == "measured"
    assert r["melanopic_edi_lx"] == pytest.approx(truth((0, 0, 128), 8, 42, "edi"), rel=1e-3)
    r = estimator.estimate(pkg, (0, 0, 192), 4, 42)  # off the v2 primaries grid, inside its ranges
    assert r["status"] == "ok" and r["support"] == "interpolated"
    assert r["melanopic_edi_lx"] == pytest.approx(truth((0, 0, 192), 4, 42, "edi"), rel=1e-3)
    assert r["melanopic_der"] == pytest.approx(r["melanopic_edi_lx"] / r["photopic_lux"])
    assert r["empirical_error_reference"]["edi"]["mape"] < 1.0
    r = estimator.estimate(pkg, (0, 0, 160), 6, 98)
    assert r["status"] == "ok" and r["support"] == "interpolated"
    assert estimator.estimate(pkg, (255, 255, 255), 8, 98)["status"] == "unsupported"
    assert estimator.estimate(pkg, (255, 0, 255), 8, 98)["status"] == "unsupported"
    assert estimator.estimate(pkg, (0, 0, 255), 8, 10)["status"] == "unsupported"  # below measured duty
    low = estimator.estimate(pkg, (64, 0, 0), 2, 30)
    assert low["status"] == "low_signal" and low["melanopic_der"] is None
    assert estimator.estimate(pkg, (64, 0, 0), 0, 30)["status"] == "unsupported"  # below the ladder levels
    assert estimator.estimate(pkg, (255, 0, 0), 0, 30)["status"] in ("ok", "low_signal")
    assert estimator.estimate(pkg, (0, 0, 255), 8, 98, configuration={"firmware": "fw2"})["status"] == \
        "configuration_mismatch"
    assert estimator.estimate(None, (0, 0, 255), 8, 98)["status"] == "unavailable"


def test_codes_below_the_lowest_node_are_extrapolated_and_flagged(package_and_report):
    pkg, _ = package_and_report
    for rgb in ((0, 0, 32), (0, 0, 63), (192, 96, 48)):
        r = estimator.estimate(pkg, rgb, 6, 42)
        assert r["support"] == "extrapolated" and estimator.CODE_EXTRAPOLATED in r["warnings"]
        assert r["photopic_lux"] == pytest.approx(truth(rgb, 6, 42, "lux"), rel=1e-3)
    r = estimator.estimate(pkg, (0, 0, 128), 6, 42)
    assert r["support"] == "interpolated" and r["melanopic_edi_lx"] is not None
    assert estimator.estimate(pkg, (192, 96, 0), 6, 42)["status"] == "ok"  # code 0 is an endpoint
    assert estimator.estimate(pkg, (0, 0, 32), 1, 42)["status"] == "unsupported"  # below the ladder levels


def test_mixtures_below_their_calibrated_levels_are_unsupported(package_and_report):
    pkg, _ = package_and_report
    assert pkg["validity"]["multi_channel_min_level"] == 4
    r = estimator.estimate(pkg, (192, 96, 0), 3, 42)
    assert r["status"] == "unsupported" and r["melanopic_edi_lx"] is None
    assert estimator.estimate(pkg, (192, 96, 0), 4, 42)["status"] == "ok"
    assert estimator.estimate(pkg, (0, 0, 160), 3, 42)["status"] == "ok"  # single channels are not limited


def test_melanopic_low_signal_is_per_output(package_and_report):
    pkg, _ = package_and_report
    assert estimator.estimate(pkg, (64, 0, 0), 8, 98)["status"] == "ok"
    red = copy.deepcopy(pkg)
    mel = red["outputs"]["melanopic_edi"]["primaries"]["R"]
    del mel["levels"]["8"]  # as if red's melanopic signal at level 8 had been ineligible
    r = estimator.estimate(red, (64, 0, 0), 8, 98)
    assert r["status"] == "low_signal" and r["melanopic_edi_lx"] is None and r["melanopic_der"] is None
    assert r["photopic_lux"] == pytest.approx(truth((64, 0, 0), 8, 98, "lux"), rel=1e-3)


def test_targets_met_flag_and_warning(package_and_report):
    pkg, _ = package_and_report
    r = estimator.estimate(pkg, (0, 0, 192), 4, 42)
    assert r["targets_met"] is True and estimator.TARGETS_NOT_MET not in r["warnings"]
    failed = {**pkg, "validation": {**pkg["validation"], "meets_targets": False}}
    r = estimator.estimate(failed, (0, 0, 192), 4, 42)
    assert r["targets_met"] is False and estimator.TARGETS_NOT_MET in r["warnings"]


def test_experimental_frame_estimate_sums_by_pixel_fraction_and_warns(package_and_report):
    pkg, _ = package_and_report
    assert not hasattr(estimator, "estimate_frame")
    blue = estimator.estimate(pkg, (0, 0, 255), 6, 42)
    frame = experimental.estimate_frame_experimental(pkg, [((0, 0, 255), 0.25), ((0, 0, 0), 0.75)], 6, 42)
    assert frame["melanopic_edi_lx"] == pytest.approx(0.25 * blue["melanopic_edi_lx"])
    assert frame["support"] == "interpolated" and experimental.EXPERIMENTAL_WARNING in frame["warnings"]
    assert experimental.estimate_frame_experimental(pkg, [((0, 0, 255), 0.5)], 6, 42)["status"] == "unavailable"


def test_frame_prediction_uses_the_package_code_response_at_the_frame_level(package_and_report):
    from uprtek.model.frame_check import predict_frame
    pkg, _ = package_and_report
    params = {"outputs": pkg["outputs"]}
    colors = [((128, 0, 0), 0.25), ((0, 0, 192), 0.25), ((0, 0, 0), 0.5)]
    pred = predict_frame(params, colors, 5, 42, "factorized", True)
    want = sum(f * truth(rgb, 5, 42, "lux") for rgb, f in colors)
    assert pred["lux"]["estimate"] == pytest.approx(want, rel=1e-3)
    assert pred["lux"]["lower"] == pytest.approx(want, rel=1e-3)  # every code is calibrated
    low = predict_frame(params, [((32, 0, 0), 1.0)], 5, 42, "factorized", True)  # extended code response
    assert low["lux"]["estimate"] == pytest.approx(truth((32, 0, 0), 5, 42, "lux"), rel=1e-3)
    out = predict_frame(params, [((32, 0, 0), 1.0)], 1, 42, "factorized", True)  # below the ladder levels
    assert out["lux"]["estimate"] is None
