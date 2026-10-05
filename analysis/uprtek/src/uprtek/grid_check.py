"""Spatial grid check: the probe's 3x3 weight map and the display's additivity, from grid sessions.

Per color block of a ``uprtek.grid_capture`` session, every capture is made display-attributable
(capture minus the mean of the block's two blacks), then divided by the block's full field:

- ``weights``: cell / full field. 1/9 for every cell if the optics weight every pixel equally.
- ``relative``: 9 x weight, so 1.00 means equal weighting.
- ``sum_over_full``: sum of the nine cells / full field. 1.0 if the display is additive; above 1.0
  if small lit areas are driven brighter than the same pixels in a full field.

Both melanopic EDI and photopic illuminance are reported. Sessions are analyzed from a temporary
copy, so nothing is written into the session folders.

    python -m uprtek.grid_check S1 [S2 ...] --out report.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from uprtek import __version__
from uprtek.grid_frames import CELLS, COLORS
from uprtek.model.fit import _summaries
from uprtek.rawio import read_session

REPORT_SCHEMA = "chronolume-grid-check/1"
OUTPUTS = {"edi": "melanopic_EDI_lux", "lux": "photopic_illuminance_lux"}
CELL_NAMES = [c for c, _, _ in CELLS]
LAYOUT = (("TL", "T", "TR"), ("L", "C", "R"), ("BL", "B", "BR"))


def grid_metrics(full: float | None, cells: dict[str, float | None]) -> dict[str, Any]:
    """Weight map and additivity of one block from display-attributable values."""
    if full is None or full <= 0 or any(cells.get(c) is None for c in CELL_NAMES):
        return {"weights": None, "relative": None, "sum_over_full": None}
    weights = {c: cells[c] / full for c in CELL_NAMES}
    return {"weights": weights, "relative": {c: 9.0 * w for c, w in weights.items()},
            "sum_over_full": sum(weights.values())}


def measured_grid(session_dir: Path) -> dict[str, dict[str, Any]]:
    """Per color: background, net full field and cells, and their metrics, from one grid session
    (analyzed in place; ``grid_report`` passes a temporary copy)."""
    session = read_session(session_dir)
    sums = _summaries(Path(session_dir))
    result = {}
    for blk in session["blocks"]:
        caps = [c for c in session["captures"] if c["block_index"] == blk["index"]]
        blacks = [sums[c["label"]] for c in caps if c["role"].startswith("black") and c["label"] in sums]
        bg = {a: float(np.mean([float(s.get(k) or 0.0) for s in blacks])) if blacks else 0.0
              for a, k in OUTPUTS.items()}
        drift = {a: abs(float(blacks[-1].get(k) or 0.0) - float(blacks[0].get(k) or 0.0)) if len(blacks) > 1 else None
                 for a, k in OUTPUTS.items()}

        def net(label: str) -> dict[str, Any] | None:
            s = sums.get(label)
            if s is None:
                return None
            return {**{a: float(s.get(k) or 0.0) - bg[a] for a, k in OUTPUTS.items()},
                    "lux_gross": float(s.get("photopic_illuminance_lux") or 0.0),
                    "quantitative": bool(s.get("quantitative")), "flags": s.get("qc_flags", [])}

        full = next((net(c["label"]) for c in caps if c["role"] == "full"), None)
        cells = {c["cell"]: net(c["label"]) for c in caps if c["role"] == "cell"}
        metrics = {a: grid_metrics(full[a] if full else None,
                                   {k: (v[a] if v else None) for k, v in cells.items()})
                   for a in OUTPUTS}
        result[blk["color"]] = {"session": str(session_dir), "session_index": session.get("session_index"),
                                "level": blk["level"], "duty": blk["duty"], "background": bg,
                                "background_drift": drift, "full": full, "cells": cells, "metrics": metrics}
    return result


def grid_report(session_dirs: list[Path]) -> dict[str, Any]:
    per_session = []
    with tempfile.TemporaryDirectory(prefix="grid_check_") as tmp:
        for d in session_dirs:
            d = Path(d)
            copy = Path(tmp) / d.name
            shutil.copytree(d, copy, ignore=shutil.ignore_patterns("analysis"))
            m = measured_grid(copy)
            for rec in m.values():
                rec["session"] = str(d)
            per_session.append(m)
    colors = {}
    for color, _ in COLORS:
        rows = [m[color] for m in per_session if color in m]
        mean: dict[str, Any] = {}
        for a in OUTPUTS:
            ok = [r["metrics"][a] for r in rows if r["metrics"][a]["weights"] is not None]
            mean[a] = {
                "relative": {c: float(np.mean([r["relative"][c] for r in ok])) for c in CELL_NAMES} if ok else None,
                "sum_over_full": float(np.mean([r["sum_over_full"] for r in ok])) if ok else None,
                "sum_over_full_range": ([float(min(r["sum_over_full"] for r in ok)),
                                         float(max(r["sum_over_full"] for r in ok))] if ok else None),
                "n_sessions": len(ok),
            }
        colors[color] = {"sessions": rows, "mean": mean}
    return {"schema": REPORT_SCHEMA, "created": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "analysis_version": __version__, "grid_sessions": [str(d) for d in session_dirs], "colors": colors}


def format_report(report: dict[str, Any]) -> str:
    lines = ["relative weight = 9 x cell / full field (1.00 = every pixel weighted equally)"]
    for color, rec in report["colors"].items():
        for a in OUTPUTS:
            m = rec["mean"][a]
            if m["relative"] is None:
                lines.append(f"{color} {a}: no complete block")
                continue
            rng = m["sum_over_full_range"]
            lines.append(f"{color} {a}: sum of 9 / full = {m['sum_over_full']:.3f} "
                         f"(range {rng[0]:.3f}-{rng[1]:.3f}, n={m['n_sessions']})")
            for row in LAYOUT:
                lines.append("    " + "  ".join(f"{c:>2} {m['relative'][c]:5.2f}" for c in row))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sessions", nargs="+", type=Path, help="grid session folders")
    p.add_argument("--out", type=Path, help="write the JSON report here")
    args = p.parse_args(argv)
    report = grid_report(args.sessions)
    print(format_report(report))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
