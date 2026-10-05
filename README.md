# XR glasses characterization

Pipeline for controlled ocular photostimulation on a consumer see-through XR display (VITURE Luma Ultra): the stimulus application, the UPRtek acquisition and settings-to-exposure model, the calibration package, and the measurement data.

The validation manuscript and the scripts that rebuild it are not in this repository. Neither are the lab documents or the HOSI spectrometer pipeline.

## What is here

| Path | What it is |
|---|---|
| `apps/chronolume_host/` | Stimulus application. Build instructions are in its README. |
| `analysis/uprtek/` | UPRtek MK350S acquisition and the settings-to-exposure model. |
| `data/uprtek/model/` | Calibration package (`calibration.json`), validation report, and the [developer usage guide](data/uprtek/model/README.md). |
| `data/uprtek/` | The characterization, model, frame, and grid sessions. |
| `data/sessions/` | Host event logs for the characterization sessions. |
| `3d/` | Meter model (STEP). Added separately; this packaging script does not create it. |

`vendor/viture/` (the VITURE SDK) is not included. The host application links against it; see `apps/chronolume_host/README.md` and [VITURE](https://www.viture.com).

The mixture sessions `model_mixtures_s1_20261005_115533` and `model_mixtures_s2_20261005_120222` are included because `calibration.json` lists them in `training_sessions`. Earlier sessions from 2026-10-02 and 2026-10-03 are not: nothing in the manifest or the calibration package uses them.

## Run the analysis

Requires Python 3.11+.

```bash
python3 -m pip install -e "analysis/uprtek[analysis,dev]"
cd analysis/uprtek && python3 -m pytest
```

Predicting a uniform stimulus from the calibration package is described in `data/uprtek/model/README.md`.

## License

MIT. See `LICENSE`. The CIE tables under `analysis/uprtek/src/uprtek/data/cie/` stay under their own terms (`ATTRIBUTION.md` in that folder).
