"""Raw UPRtek session files (stdlib only; shared by 32-bit capture and 64-bit analysis).

Layout::

    <session>/session.json        meter, host, sequence, operator settings
    <session>/raw/<label>.csv     wavelength_nm, rep0, rep1, …  (instrument spectral units)
    <session>/raw/<label>.json    stimulus + device state + per-repeat instrument readings
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SPECTRUM_UNITS_RAW = "instrument (mkusb mk_GetSpectrum)"


@dataclass
class RawCapture:
    label: str
    wavelength_nm: list[float]
    repeats: list[list[float]]
    meta: dict[str, Any]
    csv_path: Path


def raw_dir(session_dir: str | Path) -> Path:
    return Path(session_dir) / "raw"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_capture(
    session_dir: str | Path,
    label: str,
    wavelength_nm: list[float],
    repeats: list[list[float]],
    meta: dict[str, Any],
) -> Path:
    if not repeats:
        raise ValueError("no repeats to write")
    n = len(wavelength_nm)
    for r in repeats:
        if len(r) != n:
            raise ValueError("repeat length does not match wavelength grid")
    d = raw_dir(session_dir)
    d.mkdir(parents=True, exist_ok=True)
    csv_path = d / f"{label}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["wavelength_nm"] + [f"rep{i}" for i in range(len(repeats))])
        for i, wl in enumerate(wavelength_nm):
            w.writerow([f"{wl:g}"] + [f"{r[i]:.8e}" for r in repeats])
    write_json(d / f"{label}.json", {**meta, "label": label, "spectrum_units_raw": SPECTRUM_UNITS_RAW})
    return csv_path


def read_capture(csv_path: str | Path) -> RawCapture:
    csv_path = Path(csv_path)
    wl: list[float] = []
    reps: list[list[float]] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        if not header or header[0].strip() != "wavelength_nm":
            raise ValueError(f"{csv_path}: first column must be wavelength_nm")
        reps = [[] for _ in header[1:]]
        for row in reader:
            if not row:
                continue
            wl.append(float(row[0]))
            for i, v in enumerate(row[1:]):
                reps[i].append(float(v))
    meta_path = csv_path.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return RawCapture(
        label=meta.get("label", csv_path.stem),
        wavelength_nm=wl,
        repeats=reps,
        meta=meta,
        csv_path=csv_path,
    )


def list_captures(session_dir: str | Path) -> list[Path]:
    d = raw_dir(session_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"no raw/ directory in {session_dir}")
    return sorted(d.glob("*.csv"))


def read_session(session_dir: str | Path) -> dict[str, Any]:
    p = Path(session_dir) / "session.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
