"""Frame-level check: predicted versus measured exposure for the example application frames.

Predictions come from the calibration package when one is given (``--package``; its model, with
the code response at each level and the mixture interaction). Otherwise they come from the
characterization (color_set) sessions alone, through the factorized form of
``uprtek.model.forward``: each primary's code response at the base level and duty (its code ladder
over full code, applied at every level, which the model sessions showed to overestimate low codes
at levels below 8), its full-code output at every level, and its duty series, without a mixture
interaction. A frame is predicted as the pixel-weighted sum of its colors' uniform-color
predictions, and the optics are assumed to weight every pixel equally. Three predictions are
reported per frame:

- ``estimate``: the pixel-weighted sum, when every color is inside the calibrated code range;
- ``bounds``: with codes between 0 and a channel's lowest calibrated code contributing nothing
  (lower) or as much as that lowest code (upper); the code response rises with code, so the
  pixel-weighted sum lies between them;
- ``mean_rgb``: the uniform-color prediction of the frame's mean code, for comparison only.

Measured values come from frame sessions (``uprtek.frame_capture``). Every value is
display-attributable: the capture minus the mean of the black captures of its block. Each frame is
also compared with the pixel-weighted sum of the uniform references measured in the same block,
which tests spatial additivity without any model.

With ``--grid`` (a ``uprtek.grid_check`` report), a spatially weighted alternative is reported
alongside (``uprtek.model.spatial``; not validated): ``predicted.spatial`` weights each color's
uniform prediction by the fitted probe weight map over its pixels and by the lit-area gain, and
``measured.reference_spatial`` does the same with the same-block references, without any model.

    python -m uprtek.model.frame_check --characterization S1 S2 S3 [--package calibration.json]
        [--frames F1 F2 F3] [--grid grid_check.json] --out report.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

import numpy as np

from uprtek import __version__
from uprtek.frames import BY_NAME, FRAME_H, FRAME_W, FRAMES, describe, render
from uprtek.model import forward, spatial
from uprtek.model.fit import PHOTOPIC_MIN_LUX, _summaries
from uprtek.rawio import read_session

REPORT_SCHEMA = "chronolume-frame-check/1"
OUTPUTS = {"melanopic_edi": ("edi", "melanopic_EDI_lux"), "photopic": ("lux", "photopic_illuminance_lux")}
LADDER = re.compile(r"^([RGB])(\d{3})$")
LEVEL = re.compile(r"^([RGB])255_br(\d)$")
DUTY = re.compile(r"^([RGB])255_d(\d+)$")


def _value(s: dict[str, Any], key: str) -> float:
    return float(s.get(key) or 0.0)


def characterization_params(session_dirs: list[Path]) -> dict[str, Any]:
    """Factorized per-primary parameters, in ``forward``'s format, from color_set sessions: session-
    mean display-attributable outputs (capture minus the session's black ``K``) of the quantitative
    captures; a capture label is used only if it is quantitative in every session."""
    per: dict[str, list[dict[str, Any]]] = {}
    base_level = base_duty = None
    for d in session_dirs:
        sums = _summaries(Path(d))
        k = sums.get("K")
        if k is None:
            raise ValueError(f"{d}: no black capture K")
        for label, s in sums.items():
            if label == "K":
                continue
            per.setdefault(label, []).append({
                "quantitative": bool(s.get("quantitative")),
                **{attr: _value(s, key) - _value(k, key) for attr, key in OUTPUTS.values()},
                "level": s.get("brightness_level"), "duty": s.get("duty_cycle")})
        b = sums.get("B255")
        if b is not None:
            base_level, base_duty = b.get("brightness_level"), b.get("duty_cycle")
    n = len(session_dirs)
    usable = {lab: {attr: float(np.mean([r[attr] for r in rows])) for attr, _ in OUTPUTS.values()}
              for lab, rows in per.items() if len(rows) == n and all(r["quantitative"] for r in rows)}
    params: dict[str, Any] = {}
    for out, (attr, _) in OUTPUTS.items():
        prims = {}
        for c in forward.CHANNEL_INDEX:
            full = usable.get(f"{c}255", {}).get(attr)
            if not full or full <= 0:
                continue
            codes = sorted(int(m.group(2)) for lab in usable if (m := LADDER.match(lab)) and m.group(1) == c
                           and 0 < int(m.group(2)) < 255 and usable[lab][attr] > 0)
            levels = {m.group(2): usable[lab][attr] for lab in usable
                      if (m := LEVEL.match(lab)) and m.group(1) == c and usable[lab][attr] > 0}
            duties = {int(m.group(2)): usable[lab][attr] / full for lab in usable
                      if (m := DUTY.match(lab)) and m.group(1) == c and usable[lab][attr] > 0}
            duties[int(base_duty)] = 1.0
            prims[c] = {
                "codes": [0] + codes + [255],
                "a": [0.0] + [usable[f"{c}{k:03d}"][attr] / full for k in codes] + [1.0],
                "levels": dict(sorted(levels.items(), key=lambda kv: int(kv[0]))),
                "duties": sorted(duties),
                "q": [duties[d] for d in sorted(duties)],
                "base_duty": int(base_duty),
            }
        params[out] = {"primaries": prims, "interaction": None}
    return {"outputs": params, "base_level": base_level, "base_duty": base_duty,
            "sessions": [str(d) for d in session_dirs]}


def _channel_bounds(prim: dict[str, Any], code: int, level: int, duty: float,
                    kind: str = "factorized") -> tuple[float, float] | None:
    """(lower, upper) of one channel's output; equal inside the calibrated code range."""
    v = forward.primary_output(prim, code, level, duty, kind)
    if v is not None:
        return v, v
    k1 = forward.lowest_code(prim, level)
    if 0 < code < k1:
        hi = forward.primary_output(prim, k1, level, duty, kind)
        return (0.0, hi) if hi is not None else None
    return None


def predict_frame(params: dict[str, Any], colors: list[tuple[tuple[int, int, int], float]], level: int,
                  duty: float, kind: str = "factorized", interaction: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    mean_rgb = [sum(f * rgb[i] for rgb, f in colors) for i in range(3)]
    for name, (attr, _) in OUTPUTS.items():
        p = params["outputs"][name]
        est: float | None = 0.0
        lo = hi = 0.0
        unsupported_fraction = 0.0
        for rgb, f in colors:
            v = forward.output(p, rgb, level, duty, kind, interaction)
            if v is None:
                est = None
                unsupported_fraction += f
            elif est is not None:
                est += f * v
            for c, i in forward.CHANNEL_INDEX.items():
                if rgb[i] == 0:
                    continue
                b = _channel_bounds(p["primaries"][c], rgb[i], level, duty, kind) if c in p["primaries"] else None
                if b is None:
                    lo = hi = float("nan")
                    break
                lo += f * b[0]
                hi += f * b[1]
        naive = forward.output(p, [int(round(v)) for v in mean_rgb], level, duty, kind, interaction)
        out[attr] = {"estimate": est, "lower": None if np.isnan(lo) else lo, "upper": None if np.isnan(hi) else hi,
                     "mean_rgb": naive, "unsupported_fraction": unsupported_fraction}
    out["mean_rgb_code"] = mean_rgb
    for k in ("estimate", "lower", "upper", "mean_rgb"):
        e, x = out["edi"][k], out["lux"][k]
        out.setdefault("der", {})[k] = e / x if e is not None and x and x >= PHOTOPIC_MIN_LUX else None
    return out


def measured_frames(session_dir: Path) -> dict[str, dict[str, Any]]:
    """Per frame: display-attributable frame and reference values, and the pixel-weighted sum of
    the references, from one frame session."""
    session = read_session(session_dir)
    sums = _summaries(Path(session_dir))
    frames = {f["name"]: f for f in session["frames"]}
    result = {}
    for blk in session["blocks"]:
        name = blk["frame"]
        caps = [c for c in session["captures"] if c["block_index"] == blk["index"]]
        blacks = [sums[c["label"]] for c in caps if c["role"].startswith("black") and c["label"] in sums]
        bg = {attr: float(np.mean([_value(s, key) for s in blacks])) if blacks else 0.0
              for attr, key in OUTPUTS.values()}
        drift = {attr: abs(_value(blacks[-1], key) - _value(blacks[0], key)) if len(blacks) > 1 else None
                 for attr, key in OUTPUTS.values()}

        def net(label: str) -> dict[str, Any] | None:
            s = sums.get(label)
            if s is None:
                return None
            return {**{attr: _value(s, key) - bg[attr] for attr, key in OUTPUTS.values()},
                    "lux_gross": _value(s, "photopic_illuminance_lux"),
                    "quantitative": bool(s.get("quantitative")), "flags": s.get("qc_flags", [])}

        frame = net(name)
        refs = {}
        for c in caps:
            if c["role"] == "reference":
                refs[",".join(str(v) for v in c["rgb"])] = net(c["label"])
        weighted: dict[str, float | None] = {}
        for attr, _ in OUTPUTS.values():
            total = 0.0
            for col in frames[name]["colors"]:
                if col["rgb"] == [0, 0, 0]:
                    continue
                r = refs.get(",".join(str(v) for v in col["rgb"]))
                if r is None:
                    total = None
                    break
                total += col["fraction"] * r[attr]
            weighted[attr] = total
        result[name] = {"session": str(session_dir), "session_index": session.get("session_index"),
                        "level": blk["level"], "duty": blk["duty"], "background": bg, "background_drift": drift,
                        "frame": frame, "references": refs, "reference_weighted": weighted}
    return result


def _pct(pred: float | None, meas: float | None) -> float | None:
    if pred is None or meas is None or meas <= 0:
        return None
    return 100.0 * (pred - meas) / meas


def spatial_frame(fit: dict[str, Any], params: dict[str, Any], pixels: bytes, w: int, h: int, level: int,
                  duty: float, kind: str, interaction: bool, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Spatially weighted alternative for one frame: the model's uniform predictions and, per frame
    session, the same-block references, each weighted by the fitted map and the lit-area gain."""
    weights, fractions = spatial.color_weights(pixels, w, h, fit["shape"])
    loads = {rgb: spatial.drive(params["outputs"]["photopic"], rgb, level) for rgb in weights}
    load_frame = sum(loads[rgb] * f for rgb, f in fractions.items())
    pred: dict[str, Any] = {"load": load_frame}
    meas: dict[str, Any] = {}
    for name, (attr, _) in OUTPUTS.items():
        g = (fit["gain"] or {}).get(attr)
        alpha = g["alpha"] if g else 0.0
        uniform = {rgb: forward.output(params["outputs"][name], rgb, level, duty, kind, interaction)
                   for rgb in weights if any(rgb)}
        pred[attr] = spatial.frame_sum(uniform, weights, loads, load_frame, alpha)
        per = []
        for r in rows:
            refs = {tuple(int(v) for v in k.split(",")): (v[attr] if v else None) for k, v in r["references"].items()}
            s = spatial.frame_sum(refs, weights, loads, load_frame, alpha)
            if s is not None:
                per.append(s)
        meas[attr] = float(np.mean(per)) if per else None
    return {"predicted": pred, "reference": meas}


def frame_report(characterization: list[Path], frame_sessions: list[Path] | None = None,
                 package: dict[str, Any] | None = None, grid: dict[str, Any] | None = None) -> dict[str, Any]:
    params = characterization_params(characterization)
    kind, interaction = "factorized", False
    if package:
        params = {**params, "outputs": package["outputs"], "calibration_id": package.get("calibration_id")}
        kind, interaction = package["model"]["kind"], bool(package["model"]["interaction"])
    frame_sessions = frame_sessions or []
    measured = [measured_frames(Path(d)) for d in frame_sessions]
    duty = int(params["base_duty"])
    fits: dict[tuple[int, int], dict[str, Any]] = {}
    records = []
    for spec in FRAMES:
        sess_frames = [read_session(Path(d))["frames"] for d in frame_sessions]
        desc = next((f for fs in sess_frames for f in fs if f["name"] == spec.name), None)
        if desc is None:
            desc = describe(spec, render(spec.name, FRAME_W, FRAME_H), FRAME_W, FRAME_H)
        colors = [(tuple(c["rgb"]), c["fraction"]) for c in desc["colors"]]
        level = spec.level
        pred = predict_frame(params, colors, level, duty, kind, interaction)
        rows = [m[spec.name] for m in measured if spec.name in m]
        sp = None
        if grid is not None:
            w, h = int(desc["width"]), int(desc["height"])
            if (w, h) not in fits:
                fits[(w, h)] = spatial.fit_spatial(grid, w, h)
            sp = spatial_frame(fits[(w, h)], params, render(spec.name, w, h), w, h, level, duty, kind,
                               interaction, rows)
            pred["spatial"] = sp["predicted"]
        meas: dict[str, Any] = {}
        for attr, _ in OUTPUTS.values():
            vals = [r["frame"][attr] for r in rows if r["frame"] and r["frame"]["quantitative"]]
            wts = [r["reference_weighted"][attr] for r in rows if r["reference_weighted"][attr] is not None]
            meas[attr] = {"frame": float(np.mean(vals)) if vals else None,
                          "frame_range": [float(min(vals)), float(max(vals))] if vals else None,
                          "reference_weighted": float(np.mean(wts)) if wts else None, "n_sessions": len(vals)}
            if sp is not None:
                meas[attr]["reference_spatial"] = sp["reference"][attr]
        e, x = meas["edi"]["frame"], meas["lux"]["frame"]
        meas["der"] = {"frame": e / x if e is not None and x and x >= PHOTOPIC_MIN_LUX else None}
        gross = [r["frame"]["lux_gross"] for r in rows if r["frame"]]
        errors = {attr: {k: _pct(pred[attr][k], meas[attr]["frame"]) for k in ("estimate", "lower", "upper", "mean_rgb")}
                  | {"reference_weighted": _pct(meas[attr]["reference_weighted"], meas[attr]["frame"])}
                  for attr in ("edi", "lux")}
        if sp is not None:
            for attr in ("edi", "lux"):
                errors[attr]["spatial"] = _pct(sp["predicted"][attr], meas[attr]["frame"])
                errors[attr]["reference_spatial"] = _pct(sp["reference"][attr], meas[attr]["frame"])
        records.append({
            "name": spec.name, "title": spec.title, "webapp_view": spec.webapp_view,
            "composed_to_guidance": spec.composed_to_guidance, "level": level, "duty": duty,
            "width": desc["width"], "height": desc["height"], "hash": desc["hash"],
            "lit_fraction": desc["lit_fraction"], "colors": desc["colors"],
            "predicted": pred, "measured": meas, "errors_pct": errors,
            "frame_lux_gross": gross, "below_meter_range": bool(gross) and min(gross) < PHOTOPIC_MIN_LUX,
            "sessions": [r["session"] for r in rows],
        })
    return {"schema": REPORT_SCHEMA, "created": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "analysis_version": __version__, "characterization": params, "frame_sessions": [str(d) for d in frame_sessions],
            "prediction_source": "package" if package else "characterization",
            "model": {"kind": kind, "interaction": interaction},
            "assumptions": ["pixel-weighted sum of uniform-color predictions (equal spatial weighting)",
                            "the package's model and mixture interaction" if package else
                            "no mixture interaction term (no mixture calibration); the base-level code "
                            "response is applied at every level",
                            "codes below a channel's lowest calibrated code are bounded, not interpolated"],
            "spatial": ({f"{w}x{h}": f for (w, h), f in fits.items()} if grid is not None else None),
            "frames": records}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--characterization", nargs="+", type=Path, required=True, help="color_set session folders")
    p.add_argument("--package", type=Path, help="calibration package (chronolume-uprtek model fit)")
    p.add_argument("--frames", nargs="*", type=Path, default=[], help="frame session folders")
    p.add_argument("--grid", type=Path, help="spatial grid report (chronolume-uprtek-grid-check --out)")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    package = json.loads(args.package.read_text(encoding="utf-8")) if args.package else None
    grid = json.loads(args.grid.read_text(encoding="utf-8")) if args.grid else None
    report = frame_report(args.characterization, args.frames, package, grid)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for r in report["frames"]:
        pe, pl = r["predicted"]["edi"], r["predicted"]["lux"]
        fmt = lambda v: "-" if v is None else f"{v:.3g}"
        print(f"{r['name']:<16} level {r['level']} lit {100 * r['lit_fraction']:5.1f}%  predicted EDI {fmt(pe['estimate'])} "
              f"[{fmt(pe['lower'])}, {fmt(pe['upper'])}] lx, photopic {fmt(pl['estimate'])} [{fmt(pl['lower'])}, "
              f"{fmt(pl['upper'])}] lx; measured EDI {fmt(r['measured']['edi']['frame'])}, "
              f"photopic {fmt(r['measured']['lux']['frame'])}")
        if "spatial" in r["predicted"]:
            ps, m = r["predicted"]["spatial"], r["measured"]
            print(f"{'':<16} spatial (load {ps['load']:.3f}): predicted EDI {fmt(ps['edi'])}, photopic {fmt(ps['lux'])}; "
                  f"references EDI {fmt(m['edi'].get('reference_spatial'))}, "
                  f"photopic {fmt(m['lux'].get('reference_spatial'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
