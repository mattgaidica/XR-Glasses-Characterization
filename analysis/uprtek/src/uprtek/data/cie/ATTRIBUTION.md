# CIE datasets bundled with chronolume-uprtek

Copied unchanged from `analysis/hosi/src/hosi/data/cie/` so this package has no dependency on HOSI.
These files are official CIE data tables, CC BY-SA 4.0.

## CIE alpha-opic action spectra

- File: `CIE_a-opic_action_spectra.csv`
- Source: CIE S 026:2018, Table 2
- DOI: [10.25039/CIE.DS.vqqhzp5a](https://doi.org/10.25039/CIE.DS.vqqhzp5a)
- Citation: CIE 2018, CIE alpha-opic action spectra, International Commission on Illumination (CIE), Vienna, AT
- Columns (no header): wavelength_nm, s_sc, s_mc, s_lc, s_rh, s_mel
- Range: 380–780 nm, 1 nm. `NaN` is treated as 0.

## CIE standard illuminant D65

- File: `CIE_std_illum_D65.csv`
- DOI: [10.25039/CIE.DS.hjfjmt59](https://doi.org/10.25039/CIE.DS.hjfjmt59)
- Citation: CIE, CIE standard illuminant D65, International Commission on Illumination (CIE), Vienna, AT
- Columns (no header): wavelength_nm, SPD

## CIE 1931 2° colour-matching functions (photopic V(λ) = ȳ(λ))

- File: `CIE_xyz_1931_2deg.csv`
- DOI: [10.25039/CIE.DS.xvudnb9b](https://doi.org/10.25039/CIE.DS.xvudnb9b)
- Citation: CIE, Colour-matching functions of CIE 1931 standard colorimetric observer, International Commission on Illumination (CIE), Vienna, AT
- Columns (no header): wavelength_nm, x_bar, y_bar, z_bar
- Photopic luminous efficiency V(λ) is taken as ȳ(λ).
