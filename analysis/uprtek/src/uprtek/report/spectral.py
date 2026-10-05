"""Spectral metrics on measured spectral irradiance E(λ) in W·m⁻²·nm⁻¹."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.typing import NDArray

from uprtek.report.cie import alphaopic_action_spectra, illuminant_d65, photopic_v
from uprtek.report.constants import (
    BIOLOGICAL_HI_NM,
    BIOLOGICAL_LO_NM,
    BLUE_SEARCH_HI_NM,
    BLUE_SEARCH_LO_NM,
    K_M,
    NM_TO_M,
    PLANCK_H_J_S,
    SPEED_OF_LIGHT_M_S,
    W_M2_TO_UW_CM2,
)

Array = NDArray[np.floating]


def delta_lambda(wl: Array) -> Array:
    wl = np.asarray(wl, dtype=float)
    d = np.empty_like(wl)
    if len(wl) == 1:
        d[0] = 1.0
        return d
    d[:-1] = wl[1:] - wl[:-1]
    d[-1] = d[-2]
    return d


def band_integral(wl: Array, y: Array, dlam: Array, lo_nm: float, hi_nm: float,
                  *, include_hi: bool = True) -> float:
    """Sum of y·Δλ over [lo, hi]; [lo, hi) when include_hi is False, so adjacent bands partition."""
    wl = np.asarray(wl, dtype=float)
    y = np.asarray(y, dtype=float)
    upper = (wl <= hi_nm) if include_hi else (wl < hi_nm)
    mask = (wl >= lo_nm) & upper & np.isfinite(y)
    return float(np.sum(y[mask] * dlam[mask])) if np.any(mask) else 0.0


def resample_onto(wl: Array, src_wl: Array, src_y: Array) -> Array:
    """Linear interpolation; values outside the source grid are 0."""
    return np.interp(np.asarray(wl, float), np.asarray(src_wl, float), np.asarray(src_y, float),
                     left=0.0, right=0.0)


# Peak / FWHM / centroid ---------------------------------------------------------------------

@dataclass
class Characterization:
    peak_wavelength_nm: float
    peak_value: float
    measured_fwhm_nm: float
    lambda_half_left_nm: float
    lambda_half_right_nm: float
    spectral_centroid_nm: float
    search_lo_nm: float
    search_hi_nm: float
    fwhm_resolved: bool
    peak_in_search_range: bool
    global_peak_wavelength_nm: float


def peak_search_range(rgb: tuple[int, int, int] | None) -> tuple[float, float]:
    if rgb is not None and rgb[0] == 0 and rgb[1] == 0 and rgb[2] > 0:
        return BLUE_SEARCH_LO_NM, BLUE_SEARCH_HI_NM
    return BIOLOGICAL_LO_NM, BIOLOGICAL_HI_NM


def characterize(wl: Array, e: Array, rgb: tuple[int, int, int] | None = None) -> Characterization:
    wl = np.asarray(wl, dtype=float)
    y = np.maximum(0.0, np.asarray(e, dtype=float))
    dlam = delta_lambda(wl)
    lo, hi = peak_search_range(rgb)
    vis = (wl >= BIOLOGICAL_LO_NM) & (wl <= BIOLOGICAL_HI_NM)
    global_peak = float(wl[vis][int(np.argmax(y[vis]))]) if np.any(vis) else float("nan")
    mask = (wl >= lo) & (wl <= hi)
    nan = float("nan")
    if not np.any(mask) or not np.any(y[mask] > 0):
        return Characterization(nan, nan, nan, nan, nan, nan, lo, hi, False, False, global_peak)
    idx = np.arange(len(wl))[mask]
    peak_i = int(idx[int(np.argmax(y[mask]))])
    peak_y = float(y[peak_i])
    left = _crossing(wl, y, peak_i, 0.5 * peak_y, -1)
    right = _crossing(wl, y, peak_i, 0.5 * peak_y, 1)
    resolved = bool(np.isfinite(left) and np.isfinite(right))
    power = band_integral(wl, y, dlam, BIOLOGICAL_LO_NM, BIOLOGICAL_HI_NM)
    moment = band_integral(wl, y * wl, dlam, BIOLOGICAL_LO_NM, BIOLOGICAL_HI_NM)
    return Characterization(
        peak_wavelength_nm=float(wl[peak_i]),
        peak_value=peak_y,
        measured_fwhm_nm=(right - left) if resolved else nan,
        lambda_half_left_nm=left,
        lambda_half_right_nm=right,
        spectral_centroid_nm=moment / power if power > 0 else nan,
        search_lo_nm=lo,
        search_hi_nm=hi,
        fwhm_resolved=resolved,
        peak_in_search_range=lo <= global_peak <= hi,
        global_peak_wavelength_nm=global_peak,
    )


def _crossing(wl: Array, y: Array, peak_i: int, half: float, direction: int) -> float:
    i = peak_i
    while 0 <= i + direction < len(y):
        j = i + direction
        yi, yj = float(y[i]), float(y[j])
        if yj <= half <= yi or yi <= half <= yj:
            if yj == yi:
                return float(wl[j])
            return float(wl[i] + (half - yi) / (yj - yi) * (wl[j] - wl[i]))
        i = j
    return float("nan")


# Band integrals, irradiance, photons, photometry -----------------------------------------------

@dataclass
class Irradiance:
    irradiance_380_500_W_m2: float
    irradiance_500_600_W_m2: float
    irradiance_600_780_W_m2: float
    irradiance_380_780_W_m2: float
    blue_fraction_380_500: float
    green_fraction_500_600: float
    red_fraction_600_780: float
    offband_fraction_500_780: float
    irradiance_W_m2: float
    irradiance_uW_cm2: float
    irradiance_400_500_uW_cm2: float
    irradiance_430_470_uW_cm2: float
    irradiance_380_780_uW_cm2: float
    photon_irradiance_photons_m2_s: float
    photon_irradiance_photons_cm2_s: float
    photon_irradiance_400_500_photons_cm2_s: float
    photopic_illuminance_lux: float


def photon_spectrum(wl: Array, e: Array) -> Array:
    """N_λ = E_λ λ / (h c), photons·m⁻²·s⁻¹·nm⁻¹."""
    return np.asarray(e, float) * (np.asarray(wl, float) * NM_TO_M) / (PLANCK_H_J_S * SPEED_OF_LIGHT_M_S)


def photopic_lux(wl: Array, e: Array) -> float:
    wl = np.asarray(wl, dtype=float)
    v_tab = photopic_v()
    v = resample_onto(wl, v_tab.wavelength_nm, v_tab.values)
    return K_M * band_integral(wl, np.maximum(0.0, e) * v, delta_lambda(wl), 380.0, 780.0)


def irradiance(wl: Array, e: Array) -> Irradiance:
    wl = np.asarray(wl, dtype=float)
    y = np.maximum(0.0, np.asarray(e, dtype=float))
    dlam = delta_lambda(wl)
    b = band_integral(wl, y, dlam, 380.0, 500.0, include_hi=False)
    g = band_integral(wl, y, dlam, 500.0, 600.0, include_hi=False)
    r = band_integral(wl, y, dlam, 600.0, 780.0)
    vis = band_integral(wl, y, dlam, 380.0, 780.0)
    off = band_integral(wl, y, dlam, 500.0, 780.0)

    def frac(part: float) -> float:
        return part / vis if vis > 0 else float("nan")

    total = float(np.sum(y * dlam))
    n = photon_spectrum(wl, y)
    n_total = float(np.sum(n * dlam))
    return Irradiance(
        irradiance_380_500_W_m2=b,
        irradiance_500_600_W_m2=g,
        irradiance_600_780_W_m2=r,
        irradiance_380_780_W_m2=vis,
        blue_fraction_380_500=frac(b),
        green_fraction_500_600=frac(g),
        red_fraction_600_780=frac(r),
        offband_fraction_500_780=frac(off),
        irradiance_W_m2=total,
        irradiance_uW_cm2=total * W_M2_TO_UW_CM2,
        irradiance_400_500_uW_cm2=band_integral(wl, y, dlam, 400.0, 500.0) * W_M2_TO_UW_CM2,
        irradiance_430_470_uW_cm2=band_integral(wl, y, dlam, 430.0, 470.0) * W_M2_TO_UW_CM2,
        irradiance_380_780_uW_cm2=vis * W_M2_TO_UW_CM2,
        photon_irradiance_photons_m2_s=n_total,
        photon_irradiance_photons_cm2_s=n_total / 1e4,
        photon_irradiance_400_500_photons_cm2_s=band_integral(wl, n, dlam, 400.0, 500.0) / 1e4,
        photopic_illuminance_lux=photopic_lux(wl, y),
    )


# CIE S 026 α-opic ------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def d65_1lux_alphaopic_irradiance() -> dict[str, float]:
    """α-opic irradiance of D65 normalized to 1 photopic lux (CIE S 026)."""
    wl, actions = alphaopic_action_spectra()
    d65 = illuminant_d65()
    v_tab = photopic_v()
    d = resample_onto(wl, d65.wavelength_nm, d65.values)
    v = resample_onto(wl, v_tab.wavelength_nm, v_tab.values)
    photopic = K_M * float(np.sum(d * v))
    return {name: float(np.sum(d / photopic * s)) for name, s in actions.items()}


@dataclass
class AlphaOpic:
    irradiance_W_m2: dict[str, float]
    edi_lux: dict[str, float]
    der: dict[str, float]


def alphaopic(wl: Array, e: Array, photopic_illuminance_lux: float | None) -> AlphaOpic:
    wl = np.asarray(wl, dtype=float)
    y = np.maximum(0.0, np.asarray(e, dtype=float))
    dlam = delta_lambda(wl)
    a_wl, actions = alphaopic_action_spectra()
    ref = d65_1lux_alphaopic_irradiance()
    irr: dict[str, float] = {}
    edi: dict[str, float] = {}
    der: dict[str, float] = {}
    for name, s_src in actions.items():
        s = resample_onto(wl, a_wl, s_src)
        irr[name] = band_integral(wl, y * s, dlam, 380.0, 780.0)
        edi[name] = irr[name] / ref[name] if ref[name] > 0 else float("nan")
        lux = photopic_illuminance_lux
        der[name] = edi[name] / lux if lux and lux > 0 else float("nan")
    return AlphaOpic(irradiance_W_m2=irr, edi_lux=edi, der=der)
