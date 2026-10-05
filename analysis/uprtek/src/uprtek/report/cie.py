"""Bundled CIE V(λ), D65, and S 026 α-opic action spectra."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

_CIE_DIR = Path(__file__).resolve().parent.parent / "data" / "cie"


def cie_dir() -> Path:
    return _CIE_DIR


@dataclass(frozen=True)
class CieTable:
    wavelength_nm: NDArray[np.floating]
    values: NDArray[np.floating]


def _load_xy_csv(name: str, y_column: int) -> CieTable:
    wl: list[float] = []
    y: list[float] = []
    for raw in (cie_dir() / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if not parts[0][:1].isdigit():
            continue
        wl.append(float(parts[0]))
        y.append(float(parts[y_column]))
    return CieTable(np.asarray(wl, dtype=float), np.asarray(y, dtype=float))


@lru_cache(maxsize=1)
def photopic_v() -> CieTable:
    """CIE 1931 2° ȳ(λ), used as V(λ)."""
    return _load_xy_csv("CIE_xyz_1931_2deg.csv", y_column=2)


@lru_cache(maxsize=1)
def illuminant_d65() -> CieTable:
    return _load_xy_csv("CIE_std_illum_D65.csv", y_column=1)


RECEPTORS = ("s_cone", "m_cone", "l_cone", "rhodopic", "melanopic")


@lru_cache(maxsize=1)
def alphaopic_action_spectra() -> tuple[NDArray[np.floating], dict[str, NDArray[np.floating]]]:
    wl: list[float] = []
    cols: list[list[float]] = [[] for _ in RECEPTORS]
    for raw in (cie_dir() / "CIE_a-opic_action_spectra.csv").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if not parts[0][:1].isdigit():
            continue
        wl.append(float(parts[0]))
        for i in range(len(RECEPTORS)):
            token = parts[i + 1]
            cols[i].append(0.0 if token.lower() == "nan" else float(token))
    return np.asarray(wl, dtype=float), {
        name: np.asarray(col, dtype=float) for name, col in zip(RECEPTORS, cols, strict=True)
    }
