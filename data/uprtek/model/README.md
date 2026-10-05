# Developer usage guide: settings-to-exposure calibration

`calibration.json` is the calibration package (`luma_ultra_v1`) of the settings-to-exposure model in the validation manuscript (`manuscripts/validation/`, Section 2.5 and Appendix A). `validation.json` is its held-out validation report. The package predicts corneal-plane melanopic EDI, photopic illuminance and melanopic DER for a **uniform full-frame** color at a hardware brightness level and duty cycle on the VITURE Luma Ultra, in the configuration it records (device, firmware, product id, film state, display mode).

## Predicting a uniform stimulus

Load the full-precision package; do not transcribe the rounded parameter table in the manuscript.

```python
from uprtek.model.estimator import load_package, estimate

pkg = load_package("data/uprtek/model/calibration.json")
r = estimate(pkg, rgb=(0, 0, 192), brightness_level=4, duty_setting=42,
             configuration={"firmware": "...", "film": "0.000000"})
```

The estimator is stdlib only (`analysis/uprtek/src/uprtek/model/estimator.py`); Appendix A of the manuscript specifies the same algorithm for reimplementation, and an implementation should reproduce its reference cases. Values are display-attributable and exclude ambient light.

Check `status` and `support` before using a value:

| `status` | Meaning | Use |
|---|---|---|
| `ok` | Within the calibrated domain | Use the values |
| `low_signal` | Predicted photopic below 1 lx (both values, no DER), or melanopic outside its calibrated range (photopic only) | Use what is returned, with care |
| `unsupported` | Outside the calibrated domain | No value; choose another setting |
| `configuration_mismatch` | Passed configuration differs from the package's | No value; recalibrate or restore the configuration |

`support` is `measured`, `interpolated` or `extrapolated`. The only extrapolation is in code: a code below a channel's lowest code node at that level (code 64 for every channel) extends the lowest segment toward 0 and is flagged.

## Calibrated domain

- Brightness levels 0–8 for a full-code primary; codes below 255 only at levels 2–8.
- Duty cycles 30–98 (the device presets are 30, 42 and 98).
- Codes 64–255 per channel, plus 0 (off). Lower codes are extrapolated.
- Two- and three-channel colors, including white, only at levels 4–7. White is never shown above level 7: at level 8 it collapses the display and drops the USB control link.

## Composing rendered frames

The package covers uniform full-frame stimuli only. A rendered frame is outside the validated domain even when every color in it is supported.

- **Do not predict a frame from its mean color.** The code response is nonlinear; the mean color underestimated a blue orb's melanopic EDI 2.7-fold. The color histogram is the minimum input.
- **Sum over the histogram, but expect layout to matter.** The pixel-weighted sum of uniform predictions (`uprtek.model.experimental.estimate_frame_experimental`) read about a third low on the example frames, because the probe weights the center of the field and a small lit area gives more light per pixel than a full field.
- **Keep each color within the model's support.** Use a true black (0, 0, 0) background, pure primaries at codes 64–255 for light-carrying elements, and mixtures or white only at levels 4–7. Dark non-black backgrounds fall below the lowest calibrated code and can only be extrapolated.
- **Sparse content gives little light.** Bold blue text on black (12 % lit) was predicted at about a tenth of full-field blue; a frame meant to deliver a melanopic dose should light a large area.
- **Measure frames meant as stimuli.** The spatially weighted estimate (`uprtek.model.spatial`, `chronolume-uprtek-frame-check --grid`) is experimental: it was designed after the frames were measured, describes the phantom and probe rather than a wearer's eye, and is not validated. Until it is, measure such a frame on the rig.

Commands to refit, validate and check frames are in `analysis/uprtek/README.md` (Exposure model, Application frames, Spatial grid check).
