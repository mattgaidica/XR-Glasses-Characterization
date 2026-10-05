import math

import pytest

pytest.importorskip("numpy")

import numpy as np

from uprtek.report.spectral import alphaopic, characterize, irradiance, photopic_lux

WL = np.arange(380.0, 781.0)


def gaussian(center, sigma, peak=1.0):
    return peak * np.exp(-0.5 * ((WL - center) / sigma) ** 2)


def test_blue_peak_and_fwhm():
    sigma = 10.0
    c = characterize(WL, gaussian(450, sigma), rgb=(0, 0, 255))
    assert c.peak_wavelength_nm == 450
    assert c.measured_fwhm_nm == pytest.approx(2 * math.sqrt(2 * math.log(2)) * sigma, abs=0.05)
    assert c.spectral_centroid_nm == pytest.approx(450, abs=0.01)
    assert c.peak_in_search_range


def test_band_fractions_and_offband():
    e = gaussian(450, 8) + 0.1 * gaussian(620, 8)
    irr = irradiance(WL, e)
    assert irr.blue_fraction_380_500 + irr.green_fraction_500_600 + irr.red_fraction_600_780 == pytest.approx(1.0, abs=1e-3)
    assert irr.offband_fraction_500_780 == pytest.approx(0.1 / 1.1, abs=1e-3)


def test_band_fractions_partition_flat_spectrum():
    irr = irradiance(WL, np.ones_like(WL))
    total = irr.blue_fraction_380_500 + irr.green_fraction_500_600 + irr.red_fraction_600_780
    assert total == pytest.approx(1.0, abs=1e-12)
    assert irr.blue_fraction_380_500 + irr.offband_fraction_500_780 == pytest.approx(1.0, abs=1e-12)


def test_photopic_lux_of_555nm_line():
    e = np.zeros_like(WL)
    e[WL == 555] = 1.0  # 1 W/m² in a 1 nm bin
    assert photopic_lux(WL, e) == pytest.approx(683.0, rel=1e-3)


def test_photon_irradiance_of_450nm_line():
    e = np.zeros_like(WL)
    e[WL == 450] = 1.0
    irr = irradiance(WL, e)
    expected = 450e-9 / (6.62607015e-34 * 299792458.0)
    assert irr.photon_irradiance_photons_m2_s == pytest.approx(expected, rel=1e-9)


def test_melanopic_edi_of_d65_equals_lux():
    from uprtek.report.cie import illuminant_d65

    d65 = illuminant_d65()
    e = np.interp(WL, d65.wavelength_nm, d65.values) * 1e-3
    lux = photopic_lux(WL, e)
    a = alphaopic(WL, e, lux)
    assert a.edi_lux["melanopic"] == pytest.approx(lux, rel=1e-3)
    assert a.der["melanopic"] == pytest.approx(1.0, rel=1e-3)
