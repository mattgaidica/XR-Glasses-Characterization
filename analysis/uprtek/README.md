# Chronolume UPRtek capture and analysis

Automated spectral sweeps of the VITURE glasses with a UPRtek MK350S (Premium) on Windows, plus offline analysis.
This package is **independent of `analysis/hosi/`**: it imports nothing from HOSI and HOSI imports nothing from it. The CIE tables in `src/uprtek/data/cie/` are unchanged copies.

## What differs from HOSI

| | HOSI (`analysis/hosi`) | UPRtek (`analysis/uprtek`) |
|---|---|---|
| Raw data | C12880MA counts, per-row darks | Factory-calibrated spectral irradiance, 380–780 nm at 1 nm |
| Quantity | Radiance through lens/tube | Cosine-corrected **irradiance** at the probe plane |
| Absolute scale | `radSens` + transfer calibration | Instrument factory calibration |
| Corneal irradiance / α-opic | Needs an FOV assumption | Direct, with the probe at the eye position |
| Dark | Shutter dark at matching integration time | `mk_Msr_Dark` with the sensor capped |

`mk_GetSpectrum` returns **mW·m⁻²·nm⁻¹**. Every capture is checked by integrating the spectrum against V(λ) and comparing with the meter's own lux (`computed_to_instrument_lux_ratio`; mismatch > 3 % → `UNIT_CHECK_MISMATCH`, not quantitative).

## Setup (once)

The vendor `mkusb.dll` is 32-bit, so capture runs under 32-bit Python; analysis runs under 64-bit Python. From the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e "analysis/uprtek[analysis,dev]"

py -3.12-32 -m venv .venv32
.\.venv32\Scripts\python -m pip install -e analysis/uprtek
```

Requires uSpectrum installed (`C:\Program Files (x86)\uSpectrum`), which provides `mkusb.dll` and the SiUSBXp driver. Override the DLL folder with `UPRTEK_MKUSB_DIR` or `--mkusb-dir`. **Close uSpectrum** before capturing; the meter allows one client.

Check the meter:

```powershell
.\.venv32\Scripts\python -m uprtek.probe
```

## Run a sweep

1. Glasses connected, desktop **extended** (Win+P → Extend). HDR, Night light, and Auto color management off for the glasses display. Dark room.
2. Start the host with the control port, fullscreen on the glasses monitor (indices are printed at startup):

   ```powershell
   .\build\apps\chronolume_host\Release\chronolume_host.exe --device viture --monitor 1 --control-port 7777
   ```

   Press `P` to fullscreen (or the sweep warns you).
3. Position the probe at the eye position and run:

   ```powershell
   .\.venv32\Scripts\python -m uprtek.sweep --sequence hosi_set --calibration-version uprtek_v001 --repeats 3
   ```

   For all channels alike (red, green, white and blue ladders and brightness sweeps), use `--sequence color_set --brightness 8`; `--brightness-levels 0,4,8` shortens it.

   The sweep asks you to cap the sensor for dark calibration, then uncap. The glasses show `(0,0,255)` during dark calibration so they do not sit dark through the prompts; `session.json` records how long `mk_Msr_Dark` itself took (`dark.duration_s`). It then presents each stimulus, waits for the host to confirm the frame was presented (with framebuffer readback), dwells `--settle-s`, and captures `--repeats` spectra. Every capture is also written into the host session log as a `mark` event.

Sequences (labels match the HOSI file names):

| `--sequence` | Steps |
|---|---|
| `hosi_set` (default) | `R255 G255 B255 W255`, blue ladder `B192…B016`, `K`, then `B255_br8…B255_br0` |
| `color_set` | `R255 G255 B255 W255`, blue ladder `B192…B016`, `K`, ladders `R/G/W 192 128 064`, then `R255 G255 B255 W255` at each of `--brightness-levels` (`R255_br8 … W255_br0`; no `W255_br8`, see *White at brightness 8*), then the duty block: `R255_d42 G255_d42 B255_d42 W255_br7_d42`, then the same at duty 30 (every duty preset below the base; see *Duty cycle*) |
| `primaries` | `R255 G255 B255 W255 K` |
| `blue_ladder` | `B016 B032 B064 B128 B192 B255` at the current brightness |
| `brightness_sweep` | `B255` at `--brightness-levels` (default `0-8`), highest first |
| `repeatability` | `B128 B255` (Gate 2: rerun after 20 min) |

`K` is subtracted from every capture in analysis and the dark reading drifts upward after dark calibration (see below), so it is taken right after the dimmest blue codes rather than first; a long black stretch was also followed by blanking on the next capture. Brightness sweeps run highest level first, so from base brightness 8 the first group and the level-8 anchors need no change. `color_set` characterizes red, green and white like blue, which the dose table needs (red, green and blue at level 8, white at its maximum of 7); the gamma and brightness fits in `uprtek-report` still use only the pure-blue points.

Useful options: `--eye left|right|both`, `--exposure auto|<ms>` (manual, at most 65 ms), `--max-exposure-ms N` (auto-exposure ceiling, see below), `--priming N` (discarded captures after each stimulus change, default 1; `0` saves time; a priming capture already at the exposure ceiling is kept as repeat 0, marked `kept_as_rep0` / `from_priming`), `--max-retries N` / `--retry-wait-s S` (re-capture when the display reads dark, default 5 / 2 s, see below), `--capture-retries N` (whole-capture re-runs, default 3), `--anchor-every N` (reference capture interval, default 5; `0` disables), `--duty 98|42|30|keep` (duty cycle held for the sweep, default 98; see *Duty cycle*), `--brightness N|keep` (hardware brightness for steps without their own, i.e. primaries, ladders and `K`, default 8; those steps return to it after a brightness sweep), `--brightness-change-rgb R,G,B` (shown while brightness changes, default `0,0,0`, see below), `--warmup-s 600` (Gate 2 warm-up on `(0,0,255)`), `--manual-brightness` (prompts you if the host cannot set brightness; captures are flagged `BRIGHTNESS_MANUAL`), `--allow-mock` (testing without glasses), `--skip-dark`.
Brightness is restored and the display set to black at the end, including on Ctrl-C.

### Low light: exposure

`mk_Msr_Capture` and `mk_Msr_SetMaxExpTime` take **microseconds** (the vendor guide says ms), and the reported `exposure_time_raw` is in µs. Manual exposure is therefore capped at 65 ms by the DLL's 16-bit argument, below the 500 ms auto-exposure ceiling, so it does not help with dim stimuli. Raise the auto-exposure ceiling instead, e.g. `--max-exposure-ms 5000` (verified on MK350X FW 1.1.1.B4: 5 s exposures, light strength 0). The ceiling persists on the meter, so the sweep and probe set it at the start and always restore 500 ms at the end; each capture records `max_exposure_ms`, and `range_cause` `low` means auto-exposure hit that ceiling. The sweep runs dark calibration after setting the ceiling. Check that the dark correction holds at long exposures before trusting dim readings: `.\.venv32\Scripts\python -m uprtek.probe --dark --max-exposure-ms 5000` with the sensor left capped should read close to 0 lux.

Output:

```text
data/uprtek/session_YYYYMMDD_HHMMSS/
  session.json        meter identity, host/device, options, dark time and duration, anchors, capture list, status
  raw/B255.csv        wavelength_nm, rep0, rep1, …  (mW/m²/nm, as returned by the meter)
  raw/B255.json       RGB, eye, brightness (+ source), duty cycle, film, display mode,
                      calibration version, host frame + readback, per-repeat meter readings
```

## Analyze

```powershell
.\.venv\Scripts\chronolume-uprtek analyze data/uprtek/session_YYYYMMDD_HHMMSS
.\.venv\Scripts\chronolume-uprtek linearity data/uprtek/session_.../analysis --x blue_code
.\.venv\Scripts\chronolume-uprtek linearity data/uprtek/session_.../analysis --x brightness
.\.venv\Scripts\chronolume-uprtek linearity data/uprtek/session_.../analysis --x duty
```

`linearity --x duty` fits a line to B255 at brightness 8 against duty cycle (the duty block plus the base-duty `B255`, duplicates averaged) and reports each duty as a fraction of the highest.

`linearity --x blue_code` fits a power law `y = y(255)·(B/255)^gamma` in log-log space to the quantitative blue-ladder captures and reports gamma, R² (log space) and each point's deviation from the fit. `linearity --x brightness` reports, for the `B255_br*` captures, the ratio between consecutive measured brightness levels (a disqualified level is skipped, and the step shows which levels it spans) and each level as a fraction of the brightest. Both write `linearity_<x>.json` and `.png` in the analysis folder. Both use pure-blue captures only; red, green and white captures from `color_set` are analyzed per capture but not yet fitted.

Per capture (`analysis/<label>/`): `spectrum.csv` (W/m²/nm, std across repeats, µW/cm²/nm, photons, and the increment over `K` when present), `summary.json`, `spectrum.png`, `spectrum_normalized.png`. A session table is written to `analysis/summary.csv`.

`summary.json` uses the HOSI key names where the quantity is the same (`peak_wavelength_nm`, `measured_fwhm_nm`, `spectral_centroid_nm`, `offband_fraction_500_780`, `irradiance_430_470_uW_cm2`, `photon_irradiance_photons_cm2_s`, `melanopic_EDI_lux`, …). Band integrals are irradiance (`irradiance_380_500_W_m2`, …), not radiance. `repeat_cv_percent` is the Gate 2 repeatability across repeats.

QC flags: `OUT_OF_RANGE`, `READBACK_MISMATCH`, `UNIT_CHECK_MISMATCH`, `NO_DISPLAY_LIGHT`, `REPEAT_OUTLIER`, `DUTY_MISMATCH`, `ANCHOR_DRIFT` (these make a capture non-quantitative), `LOW_LIGHT`, `REPEAT_CV_HIGH` (> 5 %), `WEAR_NOT_WORN` (the host reported the wear sensor as not worn at a repeat), `AMBIENT_PRESENT` (black > 1 % of the measurement), `FWHM_UNRESOLVED`, `PEAK_OUTSIDE_SEARCH`, `BRIGHTNESS_MANUAL`.

`OUT_OF_RANGE` means the meter returned light strength 2. The vendor guide calls that over exposure, but the MK350X (FW 1.1.1.B4) also returns it when the signal is too weak, so each repeat records a `range_cause`: `low` (auto-exposure pinned at its maximum), `saturated` (exposure ≤ 10 ms with ≥ 1 lux), or `unknown` (in darkness auto-exposure can also drop to a few ms, reading well under 1 lux).

**Blank display.** The glasses can blank intermittently during a sweep (even right after waking them). Turn off Wearer Detection on a phantom (see `apps/chronolume_host/README.md`); it was on for all sessions before 2026-10-03, off since. When the display blanks, the meter then reads its own dark noise, usually **without** light strength 2: a spectrum peaking above ~700 nm at a few tenths of a lux, typically at the exposure ceiling. Auto-exposure itself recovers correctly from long exposures (checked by switching black → B255 with a 5 s ceiling). The display primaries peak at 459, 539 and 631 nm, so for any non-black stimulus a peak ≥ 700 nm means no display light: the sweep discards that capture, waits `--retry-wait-s`, and re-captures, up to `--max-retries` times per repeat. Discarded attempts are kept under `rejected` in the raw JSON (with the host's `wear` value) and every attempt is a `mark` event. If the retries run out the capture is flagged `NO_DISPLAY_LIGHT`. A display that turns on part-way through a long exposure gives a low, blue-shifted reading that the peak test cannot see; `REPEAT_OUTLIER` (a repeat > 30 % from the capture median, checked when the median is ≥ 0.1 lux) catches it. `--priming` discarded captures (default 1) are recorded under `priming`.

**Stimulus visibility.** Before any capture (steps, re-runs and anchors) the host's framebuffer readback must match the requested RGB on a real framebuffer. A minimized stimulus window reads back 0×0 and zeros, which would otherwise pass for black; the glasses then show the desktop, and every stimulus measures the same. On a failed readback the sweep sends `fullscreen on`, then prompts you twice, then stops with an error. Failed readbacks that were fixed are kept under `readback_fixes`. The host no longer minimizes its fullscreen window when it loses focus, so this should be rare.

**Brightness changes on black.** Every brightness change (base level, brightness steps, anchors) is made with the display showing black; the stimulus is then re-presented and its readback checked before measuring. Raising brightness to 8 while full white was showing at duty 98 dropped the glasses' USB control link twice (SDK error -3, and every later command failed until the host was restarted). `--brightness-change-rgb 0,0,16` shows a dim blue instead, to keep the panel lit; it has not yet been tested against that USB drop. The sweep asks the host for the current level first and skips the change (and `--brightness-settle-s`) when the glasses already report it.

**White at brightness 8.** Full white `(255,255,255)` at brightness 8, duty 98, makes the Luma Ultra's output collapse (the next blue anchor read 0.47× and 0.09× of the reference in two sweeps on 2026-10-03) and then drops its USB control link (`get_brightness_level` returns -7, `set_duty_cycle` -3) until the host is restarted. It happens even when brightness is changed on black first. It is not specific to Windows or to duty 98: on macOS (firmware 12.0.01.101) at duty 90 on 2026-10-04, stepping white from 7 to 8 by hand returned SDK error -3, and an earlier attempt showed white at 8 briefly before the glasses' display dropped off the system. Red, green and blue alone are fine at 8, and white at 7 is fine. The sweep therefore never shows white (`r = g = b > 0`) above `WHITE_MAX_BRIGHTNESS` = 7: white steps that follow the base level (`W255`, the `W` ladder) run at 7 when the base is 8 and the next step returns to the base; with `--brightness keep` at 8 the same cap applies; sequences that ask for white above 7 explicitly are refused before anything is shown; and `color_set` has no `W255_br8`. Capped captures carry `white_brightness_capped: true` in their raw JSON, and the console line shows `(white capped at 7)`. Whether lower white codes (`W192`, …) are safe at 8 has not been tested. The host itself does not enforce the limit, so do not show white at 8 by hand.

**Re-running captures.** A capture that ends with `NO_DISPLAY_LIGHT` or `REPEAT_OUTLIER` is re-run in full (all repeats) up to `--capture-retries` times (default 3). Both triggers come from the capture's own readings, never from comparison with an expected value, so re-running does not steer results. Every discarded attempt is kept, with its spectra, under `discarded_attempts` in the raw JSON, and `capture_attempts` records how many were needed. If the last attempt still fails, the flag stays and the capture is not quantitative.

**Anchors.** A display that stays dim for a whole capture gives repeats that agree with each other, so the outlier check cannot see it. The sweep therefore captures an anchor, (0,0,255) at brightness 8, before the first step and after every `--anchor-every` steps (default 5; `0` disables). The first anchor is the reference; it must be clean or the sweep prompts you to check the glasses (three tries, then it stops). A later anchor more than 10 % from the reference is captured once more; if it is still off, the sweep pauses so you can wake the glasses or check the probe, then re-runs the steps since the previous anchor (twice at most). If it is still off after that, those captures are flagged `ANCHOR_DRIFT` and the sweep continues. Anchors are written to `anchors/raw/` (not `raw/`, so analysis ignores them) and summarised under `anchors` in `session.json`; block re-runs are listed under `block_reruns`. With `--brightness keep` brightness is restored after each anchor; otherwise the next step sets its own level (or the base level).

**Duty cycle.** The Luma Ultra supports only the manufacturer's SDK duty presets 98, 42 and 30 (`DUTY_PRESETS` in `sequences.py`); 98 is the base duty. `--duty` takes one of them (default 98) or `keep`, which leaves the glasses alone. When the sequence has steps with their own duty (`color_set`), the sweep first runs a duty check: it requests each preset with the display black and reads it back, and stops if any is not accepted. The result (every requested and reported value) is stored as `duty_acceptance` in `session.json`, and `color_set` builds its duty block from the presets below the base. The base duty is applied before the first capture and must read back, or the sweep stops. Duty is changed only on black, followed by `--brightness-settle-s`, and the steps after the duty block return to the base duty. The host re-reads duty after every brightness change; if it moved, the sweep re-applies it and records the old value as `duty_reapplied_from`. A capture whose host state does not show the requested duty (`requested_duty_cycle` in the raw JSON) is flagged `DUTY_MISMATCH`. The initial duty is restored at the end. Duty is a real intensity knob: at B255 and brightness 8, duty 30 gave ~41 % of the duty-90 output and duty 98 ~110 %. The host keys `,` / `.` (−10 / +10) and `9` (98) set duty by hand, e.g. for a photodiode check of the emitted waveform.

**Dark drift at long exposures.** With a 5 s ceiling the capped/blank reading drifted from 0.02 to ~0.5 lux over ~15 min after dark calibration. Measure dim stimuli soon after the dark step and keep `K` in the sequence so the analysis can subtract it; the built-in sequences take `K` right after the dimmest blue codes. Sessions recorded before this change carry the old name `OVER_EXPOSED`, which is still treated as disqualifying.

## Exposure model

A settings-to-exposure model predicts corneal melanopic EDI, photopic illuminance and melanopic DER for a uniform full-frame color at a brightness level and duty cycle, without measuring it (`docs/chronolume_exposure_model_developer_spec.md`), at the SDK duty presets.

### Capture (32-bit Python)

The grid is `src/uprtek/data/model_grid_v3.json` (schema `chronolume-model-grid/2`; `--grid` selects another, e.g. `model_grid_v2.json`, which keeps level 0 in the mixtures, or `model_grid_v1.json`), in three stages:

| `--stage` | Settings | Sessions |
|---|---|---|
| `primaries` | Code ladder: R, G, B at codes 64, 192, level 8, duty 98. Crossed: R, G at codes 128, 255 × levels 4, 8 × duties 30, 42, 98; B at 128, 255 × level 8 × the same duties, and B255 at level 4 × the same duties (39) | 3 |
| `ladders` | R, G, B at codes 64, 128, 192 × levels 2, 4, 5, 7 × duty 98 (36); code ladders below level 8, added after the primaries sessions showed that the code response depends on level | 3 |
| `mixtures` | 19 two- and three-channel compositions (gray axis, full white, pairs, unequal mixes) × levels 4, 7 × duties 30, 98 (76; level 0 dropped in v3, its mixtures fall near or below the meter's range) | 3 |
| `validation` | R, G, B at codes 96, 160 × levels 3, 6, and 6 new mixtures × levels 5, 6 (inside the mixtures' levels), all × duties 42, 98 (48); never used for fitting | 1 |

v2 replaces v1's primaries grid (codes 64–255 × levels 0, 4, 8 × duties 30, 42, 98: 108 settings), in which most settings were dim enough to sit at the exposure ceiling, retake for `NO_DISPLAY_LIGHT` and fall below the noise of their own black; the session was long enough to drift. v2 keeps every code at the bright state and crosses level and duty only where the signal is well above black (blue at level 4 is weak, so only code 255 runs there). The three v3 primaries sessions (2026-10-05) then showed that the code response is not shared across levels: code 128 gives 25–27 % of full-code output at level 8 but 9–11 % at level 4 (R, G), a power law with exponent about 2.0 at level 8 and 3.2–3.5 at level 4, and the full-code output rises slowly over levels 0–4 and steeply over 5–8 (two dimming regimes). The `ladders` stage measures the code response at levels 2, 4, 5 and 7 so each regime has its own ladders; validation primary levels 3 and 6 stay between ladder levels. The ladders sessions then showed that one power law per level fits level 8 but not the lower levels (code 64 at levels 2 and 4 was under-predicted by 40–72 %), so the code response is fitted as free nodes. The brightness response at the other levels comes from the `--reference` color_set sessions. Mixtures and validation are unchanged from v1. The presets leave no duty cycle between the calibrated ones, so validation tests unseen codes, levels and mixtures at calibrated duty cycles (the report's `duty_heldout` is false). Multi-channel stimuli never run above level 7, and white never above 7; a grid that asks for either is refused.

```powershell
.\.venv32\Scripts\chronolume-uprtek-model-sweep --stage primaries --dry-run
.\.venv32\Scripts\chronolume-uprtek-model-sweep --stage primaries --session-index 1 --seed 1 --calibration-version uprtek_v001 --wear-detection disabled
```

`--dry-run` prints the blocks and setting counts and exits. Capture is a separate entry point (not a `chronolume-uprtek` subcommand) because it runs under 32-bit Python with the meter. Settings are grouped into blocks of one (level, duty); each block starts and ends with black (`K_L4_D42_start`, `K_L4_D42_end`), which the fit subtracts. Block and setting order is a seeded permutation (`--seed`), reversed on even `--session-index` so drift is counterbalanced. The B255 anchor at level 8 and the base duty runs after each block's closing black (`--anchor-every block`, the default), so the hardware state never changes inside a block; `--anchor-every N` runs it every N settings instead, as before. A failed anchor is retaken once; two consecutive failed anchors stop the session. Each anchor sets level 8 and the base duty (duty on black) and checks both by readback before measuring, then restores the level and duty of the block that follows (or of the interrupted block, with `--anchor-every N`) on black and checks them by readback, stopping the session if either does not read back; the anchor record stores `anchor_state` and `restored_state`. Labels look like `C000-000-192_L4_D42`; each raw JSON adds `role`, `block_index`, `acquisition_order`, `model_stage`, `group` and `uniform_full_frame` to the usual state, and `session.json` (schema `chronolume-uprtek-model-session/1`) records the stage, seed, plan, `duty_acceptance`, `reseated` (`--not-reseated` to say otherwise) and `--wear-detection`. Reseat the glasses between sessions of a stage. Sessions are written to `data/uprtek/model_<stage>_<timestamp>/`.

### Fit, validate, predict (64-bit Python)

```powershell
.\.venv\Scripts\chronolume-uprtek model fit --calibration data/uprtek/model_primaries_* data/uprtek/model_ladders_* data/uprtek/model_mixtures_* --reference data/uprtek/session_<color_set> --calibration-id luma_ultra_v1 --output data/uprtek/model/calibration.json
.\.venv\Scripts\chronolume-uprtek model validate data/uprtek/model/calibration.json --validation data/uprtek/model_validation_* --output data/uprtek/model/validation.json
.\.venv\Scripts\chronolume-uprtek model predict data/uprtek/model/calibration.json --rgb 0,0,192 --level 4 --duty 42
```

Sessions without an `analysis/` folder are analyzed first. Outputs are display-attributable: each setting minus the mean of its block's blacks (`K` for `--reference` color_set sessions, which supply the full-code response at every brightness level). **Eligibility** is decided by the measurement alone, per output (`fit.py`): the capture is quantitative, its gross photopic reading is at least 1 lx (`PHOTOPIC_MIN_LUX`), and the output's net value is positive and at least `SIGNAL_TO_NOISE` (3) × its background noise, `max(|K_end − K_start|, BACKGROUND_REL_FLOOR (0.1) × mean K)` (reference color_set sessions have one `K`, so only the floor). For scoring, melanopic EDI must also be at least `MELANOPIC_MIN_EDI` (0.5 lx): red alone reads 0.1–0.3 lx melanopic EDI whatever its code, a meter floor. The same rule without that floor picks the fitting targets, so a dim red can enter the photopic fit but not the melanopic one, and a red at the melanopic floor still has parameters for the mixtures it is part of. In the metrics a prediction never decides eligibility: an eligible setting without a prediction is a failure (`n_failed`, which fails the targets), and a prediction below 1 lx stays in the errors and is counted (`n_pred_below_range`). Percentage errors are over eligible settings; `mae_all` (lx) is over every setting with a prediction; DER uses settings eligible for both outputs. Metrics carry `n_eligible` / `n_ineligible` per output, `by_group` (all, primary, mixture) and strata by group, composition (R, G, B, gray, RG, RB, GB, RGB_unequal), level and duty.

The six candidates are factorized (`a(code, level)·b(level)·q(duty)` per primary, with `a` a free code node at every code below 255 and level where it was measured: power-law segments between codes, log-linear in level between those "ladder levels", and below a level's lowest node the lowest segment extended toward code 0; codes below 255 are unsupported outside the ladder levels) and crossed (grid nodes per level and duty), each without a mixture interaction term, with the total form (`h = 1 − E·g` on the sum, `E` = drive above the strongest channel), and with the per-channel form (`+Hc`: each primary times `1 − O_c·g`, `O_c` = drive of the other channels), `g = β0 + βL·L/8 + βD·D/100` (ridge `INTERACTION_RIDGE` = 1e-3). A seventh, `factorized+Hc+Rder`, is `factorized+Hc` with red's melanopic parameters taken from its photopic ones times red's melanopic DER (geometric mean of melanopic EDI / photopic over red settings eligible for both outputs; code and duty responses shared): red's melanopic signal is at the meter floor, so its own melanopic code nodes drop out of some folds and leave dim-red mixtures unsupported (33 failed predictions on the nine sessions of 2026-10-05). The package records this as `model.melanopic_from_photopic` and each such primary's `melanopic_der`; prediction is unchanged. They are compared by leave-one-session-out cross-validation on the calibration sessions (each fold refits every parameter; reference sessions are fixed inputs), and the simplest one meeting the targets (MAPE ≤ 5 %, P95 ≤ 10 % for EDI and lux, no failed predictions) is chosen; if none does, the one with the fewest failed predictions and then the lowest worst-case MAPE is chosen with `meets_targets: false`. `selection.anchor_normalized` repeats the cross-validation with each calibration session scaled by the mean anchor of all sessions over its own (blue 255 at level 8); it is reported, never used for selection. The package's `fit` block records these rules and constants. `validate` only evaluates the locked package. It writes a report (metrics, candidate comparison, predictions with eligibility flags and composition, `validation_duties`, `duty_heldout`) and embeds a `validation` block in the package. Name that report as `model_report`, and the package as `model_package`, in `manuscripts/validation/data_manifest.json` to fill the manuscript's model section and Appendix A (parameter table, grids, candidate comparison, composition errors, reference estimator cases, the package's SHA-256, and a worked example that the build checks against `uprtek.model.forward`).

From code (stdlib only, no numpy):

```python
from uprtek.model.estimator import load_package, estimate

pkg = load_package("calibration.json")
estimate(pkg, rgb=(0, 0, 192), brightness_level=4, duty_setting=42,
         configuration={"firmware": "...", "film": "0.000000"})
```

Each result has `melanopic_edi_lx`, `photopic_lux`, `melanopic_der`, a `status`, a `support` (`measured`, `interpolated`, `extrapolated` when a code is below its channel's lowest node at that level, with a warning, `unsupported`), `targets_met` (with a warning when the package's model did not meet its targets), the package's empirical error, and `warnings`. Statuses: `configuration_mismatch` when a passed `configuration` differs from the package's device, firmware, product id, film or display mode (checked first); `unsupported` for codes below 255 at levels outside the ladder levels, levels or duty cycles outside the calibration, white above `white_max_level` and multi-channel stimuli above `multi_channel_max_level` or below `multi_channel_min_level` (the lowest mixture calibration level; the interaction term is not extrapolated) (code 0 is defined); `low_signal` when predicted photopic is below 1 lx (both values returned, no DER) or when only the melanopic prediction is outside its calibrated range (photopic returned, melanopic and DER not); `unavailable` without a valid package; otherwise `ok`. The only extrapolation is the code response below a level's lowest node.

`uprtek.model.experimental.estimate_frame_experimental(pkg, histogram, level, duty)` sums uniform estimates by pixel fraction for rendered frames. It is not validated (no whole-frame power limiting or spatial optical weighting) and always carries that warning.

## Application frames

Four example frames of what a custom app might show during photostimulation (`uprtek.frames`, stdlib only, rendered at the framebuffer size) test whether a frame's exposure can be read off its color histogram:

| Frame | Content | Level | Composed to the guidance |
|---|---|---|---|
| `F1_morning_text` | Bold blue (0,0,255) text on black, like the webapp's morning slides; 12 % lit | 8 | yes |
| `F2_orb` | Light-only orb: a blue disc the height of the frame stepping through codes 255, 192, 128, 64 toward its edge, on black; 44 % lit | 8 | yes |
| `F3_night_text` | The same slide in the webapp's night colors: (255,92,56) text on a (20,5,7) background | 5 | no (background below the lowest calibrated code) |
| `F4_red_orb` | The orb in red on black; 44 % lit | 5 | yes |

```powershell
.\.venv\Scripts\chronolume-uprtek-frames --out data/uprtek/frames/1920x1080
.\.venv32\Scripts\chronolume-uprtek-frame-sweep --dry-run
.\.venv32\Scripts\chronolume-uprtek-frame-sweep --session-index 1 --calibration-version uprtek_v001
.\.venv\Scripts\chronolume-uprtek-frame-check --characterization data/uprtek/session_<color_set> ... --package data/uprtek/model/calibration.json --frames data/uprtek/frames_s* --grid data/uprtek/grid_check.json --out data/uprtek/frames/frame_check.json
```

`--grid` (a `chronolume-uprtek-grid-check` report) adds a spatially weighted alternative, reported alongside the equal-weighting estimate and not validated (`uprtek.model.spatial`): a probe weight map (Gaussian plus offset, mean 1, fitted to the pooled red, green and blue photopic 3x3 map) and a lit-area gain `G(load) = load^-alpha`, with load the frame's mean summed photopic code response (a full-field primary is 1, full white 3) and alpha fitted to the grid's cell sums and full white over its primaries. `predicted.spatial` applies them to the model's uniform predictions, `measured.reference_spatial` to the same-block references (no model); the fitted terms are under `spatial`. On the 2026-10-05 sessions the four frames, 1.4–1.6× their equal-weighting estimates, come within −2 to +5 % (F3 melanopic +15 %, under 1 lx), from the grid alone. The weight map describes the phantom and probe, not a wearer's eye.

`chronolume-uprtek-frame-sweep` (32-bit Python, like the other sweeps) reads the framebuffer size from the host, renders the frames into `--frames-dir/<w>x<h>/` with `frames.json` (histograms and FNV-1a hashes), and sends each with the host's `image` command; a frame is measured only when the host's full-framebuffer readback matches it pixel for pixel (it retries `fullscreen on`, then prompts). Each frame is a block at its own level and the base duty: black, the frame, every non-black color in it as a uniform full-field reference (`F2_orb_U000-000-128`), black, then the B255 anchor, which also restores the next block's level. Even `--session-index` reverses the frame order. Raw JSON stores `stimulus: {kind: "image", path, hash, w, h}` with `rgb` set to the frame's main color for the analysis peak search, and `role`, `block_index`, `frame` and `acquisition_order`; `session.json` uses schema `chronolume-uprtek-frame-session/1`.

`chronolume-uprtek-frame-check` predicts each frame as the sum of uniform-color predictions over its histogram. With `--package` (the manuscript build passes `model_package` when the manifest has one) it uses the package's model, with the code response at the frame's level and the mixture interaction. Without it, it falls back to the characterization (color_set) sessions alone: the factorized forward model built from their display-attributable code ladders, brightness and duty steps, which applies the level-8 code response at every level and so overestimates low codes at the frames' level 5. Colors below the lowest calibrated code are not extrapolated: the frame then gets a lower bound (those pixels at 0) and an upper bound (at the lowest calibrated code) instead of an estimate. The report also gives the naive mean-RGB prediction (the whole frame as one uniform color), and, from frame sessions, the measured frame and the reference-weighted sum (each same-session uniform reference times its pixel fraction), all net of the block's mean black. A measured frame enters the comparison only when its capture is quantitative.

## Spatial grid check

Tests the assumption behind the frame predictions that the probe weights every pixel equally and that the display is additive in lit area. `uprtek.grid_frames` splits the framebuffer into a 3x3 grid (integer thirds) and lights one cell at a time at full code in R, G, B, or W on black, at level 7 (white's limit) and the base duty.

```powershell
.\.venv32\Scripts\chronolume-uprtek-grid-sweep --dry-run
.\.venv32\Scripts\chronolume-uprtek-grid-sweep --session-index 1 --max-exposure-ms 4000 --calibration-version uprtek_v001
.\.venv\Scripts\chronolume-uprtek-grid-check data/uprtek/grid_s* --out data/uprtek/grid_check.json
```

`chronolume-uprtek-grid-sweep` renders the 36 patterns into `--frames-dir/<w>x<h>/` (default `data/uprtek/grid_frames`) and shows them with the host's `image` command under the same pixel-for-pixel readback check as the frame sweep. One block per color: black, the uniform full field (`G_B255_FULL`), the nine cells center first (`G_B255_C`, `G_B255_TL`, …), black, then the B255 anchor. Even `--session-index` reverses the color order. Captures carry `role` (`black_start`, `full`, `cell`, `black_end`), `color` and `cell`; `session.json` uses schema `chronolume-uprtek-grid-session/1`.

`chronolume-uprtek-grid-check` nets every capture by its block's mean black and divides by the block's full field, for melanopic EDI and photopic illuminance: the relative weight `9 x cell / full` is 1.00 for every cell under equal weighting, and `sum of 9 / full` is 1.0 for an additive display (above 1.0 if small lit areas are driven brighter). It analyzes a temporary copy of each session, so nothing is written into the session folders.

## Tests

```powershell
cd analysis/uprtek
..\..\.venv\Scripts\python -m pytest
```
