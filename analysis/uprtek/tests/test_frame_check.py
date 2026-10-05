import numpy as np
import pytest

pytest.importorskip("numpy")

from uprtek.model import spatial
from uprtek.model.frame_check import predict_frame, spatial_frame

# Synthetic factorized parameters: a(k) = (k/255)^2, codes from 64; output 100 lx at full code.
CODES = [0, 64, 128, 192, 255]
A = [0.0] + [(k / 255) ** 2 for k in CODES[1:]]


def _params():
    prim = {"codes": CODES, "a": A, "levels": {"5": 50.0, "8": 100.0}, "duties": [98], "q": [1.0],
            "base_duty": 98}
    out = {"primaries": {c: dict(prim) for c in "RGB"}, "interaction": None}
    return {"outputs": {"melanopic_edi": out, "photopic": out}}


def test_frame_is_pixel_weighted_sum_of_supported_colors():
    p = predict_frame(_params(), [((0, 0, 0), 0.75), ((0, 0, 255), 0.25)], 8, 98)
    assert p["lux"]["estimate"] == pytest.approx(25.0)
    assert p["lux"]["lower"] == p["lux"]["upper"] == pytest.approx(25.0)
    # the mean code (64) gives a(64) * 100 = 6.3 lx, not 25 lx
    assert p["lux"]["mean_rgb"] == pytest.approx(100 * (64 / 255) ** 2)
    assert p["der"]["estimate"] == pytest.approx(1.0)


def test_codes_below_the_lowest_calibrated_code_are_bounded():
    p = predict_frame(_params(), [((20, 5, 7), 0.9), ((255, 0, 0), 0.1)], 5, 98)
    assert p["lux"]["estimate"] is None and p["lux"]["unsupported_fraction"] == pytest.approx(0.9)
    full = 50.0
    assert p["lux"]["lower"] == pytest.approx(0.1 * full)
    assert p["lux"]["upper"] == pytest.approx(0.1 * full + 0.9 * 3 * full * (64 / 255) ** 2)


def test_unsupported_level_gives_no_bounds():
    p = predict_frame(_params(), [((0, 0, 255), 1.0)], 3, 98)
    assert p["lux"]["estimate"] is None and p["lux"]["lower"] is None and p["lux"]["upper"] is None


W, H = 90, 60


def _grid(relative, alpha):
    """A grid_check-like report whose cell sums and white/primary ratio follow G = load^-alpha."""
    s_prim, kappa, s_white = (1 / 9) ** -alpha, 3.0 ** -alpha, 9.0 ** alpha
    rel = {c: float(relative[i][j]) for i, row in enumerate(spatial.LAYOUT) for j, c in enumerate(row)}
    colors = {}
    for c, s, full in (("R255", s_prim, 10.0), ("G255", s_prim, 20.0), ("B255", s_prim, 30.0),
                       ("W255", s_white, 60.0 * kappa)):
        colors[c] = {"mean": {a: {"relative": {k: v * s for k, v in rel.items()}, "sum_over_full": s}
                              for a in ("edi", "lux")},
                     "sessions": [{"session_index": 1, "level": 7, "duty": 98, "full": {"edi": full, "lux": full}}]}
    return {"colors": colors, "grid_sessions": ["g1"]}


def test_spatial_fit_recovers_gain_and_shape():
    truth = {"x0": 0.5 * W, "y0": 0.55 * H, "sx": 0.25 * W, "sy": 0.3 * H, "amp": 3.0}
    cells = spatial.shape_cells(truth, W, H)
    fit = spatial.fit_spatial(_grid(cells, 0.08), W, H)
    assert fit["gain"]["lux"]["alpha"] == pytest.approx(0.08, abs=1e-9)
    assert max(abs(r) for r in fit["gain"]["lux"]["residual_pct"]) < 1e-6
    assert np.allclose(fit["fitted_relative"], cells / cells.mean(), atol=0.01)
    assert spatial.weight_map(fit["shape"], W, H).mean() == pytest.approx(1.0)
    assert fit["grid_level"] == [7] and fit["grid_duty"] == [98]


def test_flat_grid_reduces_to_the_equal_weighting_estimate():
    fit = spatial.fit_spatial(_grid(np.ones((3, 3)), 0.0), W, H)
    assert fit["gain"]["lux"]["alpha"] == pytest.approx(0.0, abs=1e-12)
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[: H // 2, :, 2] = 255
    img[H // 2:, : W // 3, 0] = 128
    pixels = img.tobytes()
    weights, fractions = spatial.color_weights(pixels, W, H, fit["shape"])
    colors = list(fractions.items())
    eq = predict_frame(_params(), colors, 8, 98)
    sp = spatial_frame(fit, _params(), pixels, W, H, 8, 98, "factorized", False, [])
    assert sp["predicted"]["lux"] == pytest.approx(eq["lux"]["estimate"], rel=1e-6)
    assert sp["predicted"]["load"] == pytest.approx(0.5 + (1 / 6) * (128 / 255) ** 2)
    assert sp["reference"]["lux"] is None


def test_lit_area_gain_raises_a_partly_lit_frame():
    fit = spatial.fit_spatial(_grid(np.ones((3, 3)), 0.1), W, H)
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[: H // 3, : W // 3, 2] = 255  # one ninth lit at full code: load 1/9
    pixels = img.tobytes()
    ref = {"session": "s", "references": {"0,0,255": {"edi": 90.0, "lux": 9.0}}}
    sp = spatial_frame(fit, _params(), pixels, W, H, 8, 98, "factorized", False, [ref])
    assert sp["predicted"]["load"] == pytest.approx(1 / 9)
    assert sp["predicted"]["lux"] == pytest.approx(100.0 / 9 * (1 / 9) ** -0.1, rel=1e-6)
    assert sp["reference"]["lux"] == pytest.approx(9.0 / 9 * (1 / 9) ** -0.1, rel=1e-6)
