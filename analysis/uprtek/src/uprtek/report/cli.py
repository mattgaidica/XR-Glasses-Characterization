"""chronolume-uprtek: analyze UPRtek sessions (64-bit Python with the ``analysis`` extra)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from uprtek.report.analyze import analyze_session
from uprtek.report.linearity import run_linearity


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chronolume-uprtek", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="analyze every capture in a session folder")
    a.add_argument("session_dir", type=Path)
    a.add_argument("--output", type=Path, help="default: <session>/analysis")

    lin = sub.add_parser("linearity",
                         help="gamma fit vs blue code, step ratios vs brightness, or a line vs duty cycle")
    lin.add_argument("analysis_dir", type=Path, help="folder containing per-capture summary.json files")
    lin.add_argument("--x", required=True, choices=["blue_code", "brightness", "duty"])
    lin.add_argument("--y", default="irradiance_380_500_W_m2")
    lin.add_argument("--output", type=Path, help="default: the analysis folder")

    m = sub.add_parser("model", help="settings-to-exposure model: fit, validate, predict")
    msub = m.add_subparsers(dest="model_cmd", required=True)
    mf = msub.add_parser("fit", help="fit a calibration package from model-capture sessions")
    mf.add_argument("--calibration", type=Path, nargs="+", required=True,
                    help="primaries and mixtures stage session folders")
    mf.add_argument("--reference", type=Path, nargs="*", default=[],
                    help="color_set session folders (full-code primaries at every brightness level)")
    mf.add_argument("--calibration-id")
    mf.add_argument("--output", type=Path, required=True, help="calibration package JSON")
    mv = msub.add_parser("validate", help="evaluate a package on independent validation sessions")
    mv.add_argument("package", type=Path)
    mv.add_argument("--validation", type=Path, nargs="+", required=True)
    mv.add_argument("--output", type=Path, required=True, help="validation report JSON")
    mp = msub.add_parser("predict", help="estimate exposure for one uniform color")
    mp.add_argument("package", type=Path)
    mp.add_argument("--rgb", required=True, help="R,G,B")
    mp.add_argument("--level", type=int, required=True)
    mp.add_argument("--duty", type=float, required=True)

    args = p.parse_args(argv)
    if args.cmd == "model":
        return _model(args)
    if args.cmd == "analyze":
        summaries = analyze_session(args.session_dir, args.output)
        out = args.output or args.session_dir / "analysis"
        for s in summaries:
            flags = " ".join(s["qc_flags"]) or "-"
            peak = s["peak_wavelength_nm"]
            print(
                f"{s['label']:<10} peak={peak if peak is None else f'{peak:.0f}'} nm  "
                f"E380-780={s['irradiance_380_780_W_m2']:.4g} W/m2  "
                f"lux={s['photopic_illuminance_lux']:.4g} (meter {s['instrument_lux']})  {flags}"
            )
        print(f"wrote {out}")
        return 0
    fit = run_linearity(args.analysis_dir, args.output or args.analysis_dir, args.x, args.y)
    if fit.model == "power_law":
        print(f"blue_code: gamma={fit.gamma:.4f} y(255)={fit.amplitude_at_max:.6g} "
              f"R2(log)={fit.r_squared_log:.5f} n={len(fit.x)}")
        for label, x, dev in zip(fit.labels, fit.x, fit.percent_deviation_from_fit):
            print(f"  {label:<10} B={x:.0f}  deviation from fit {dev:+.1f} %")
    elif fit.model == "linear":
        print(f"duty: y = {fit.slope:.6g} * duty + {fit.intercept:.6g}  R2={fit.r_squared:.5f} n={len(fit.x)}")
        for label, x, y, frac in zip(fit.labels, fit.x, fit.y, fit.fraction_of_max):
            print(f"  {label:<20} duty={x:.0f}  y={y:.6g}  fraction of max {frac:.3f}")
    else:
        print(f"brightness: n={len(fit.x)}")
        for label, x, y, frac in zip(fit.labels, fit.x, fit.y, fit.fraction_of_max):
            print(f"  {label:<10} level={x:.0f}  y={y:.6g}  fraction of max {frac:.3f}")
        for s in fit.steps:
            print(f"  step {s['from_level']:.0f} -> {s['to_level']:.0f}: x{s['ratio']:.3f}")
    return 0


def _model(args: argparse.Namespace) -> int:
    from uprtek.model.estimator import estimate, load_package
    from uprtek.model.fit import fit_package, validate_package

    if args.model_cmd == "fit":
        pkg = fit_package(args.calibration, args.reference, calibration_id=args.calibration_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
        sel = pkg["selection"]
        for name, m in sel["candidates"].items():
            print(f"{name:<14} {m['method']}: EDI MAPE {m['edi'].get('mape', float('nan')):.2f}%  "
                  f"lux MAPE {m['lux'].get('mape', float('nan')):.2f}%  failed {m['n_failed']}  "
                  f"unsupported {m['n_unsupported']}")
        print(f"chosen {sel['chosen']} (targets {'met' if sel['meets_targets'] else 'NOT met'}); wrote {args.output}")
        for w in pkg["warnings"]:
            print(f"WARNING: {w}")
        return 0
    if args.model_cmd == "validate":
        pkg = load_package(args.package)
        report = validate_package(pkg, args.validation)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        pkg["validation"] = {k: report[k] for k in ("sessions", "metrics", "meets_targets", "created",
                                                    "validation_duties", "duty_heldout")}
        args.package.write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
        mt = report["metrics"]
        print(f"{report['model']['name']}: EDI MAPE {mt['edi'].get('mape', float('nan')):.2f}% "
              f"(P95 {mt['edi'].get('p95_ape', float('nan')):.2f}%), lux MAPE {mt['lux'].get('mape', float('nan')):.2f}% "
              f"(P95 {mt['lux'].get('p95_ape', float('nan')):.2f}%), failed {mt['n_failed']}, "
              f"predicted below range {mt['n_pred_below_range']}; duties {report['validation_duties']} "
              f"(held out: {report['duty_heldout']}); targets {'met' if report['meets_targets'] else 'NOT met'}; "
              f"wrote {args.output}")
        return 0
    rgb = [int(v) for v in args.rgb.split(",")]
    print(json.dumps(estimate(load_package(args.package), rgb, args.level, args.duty), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
