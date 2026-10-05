"""Physical constants and analysis thresholds."""

from __future__ import annotations

# CIE photopic constant (lm/W). Same value as the HOSI pipeline so results are comparable.
K_M = 683.0

# CODATA 2018 / SI exact.
PLANCK_H_J_S = 6.62607015e-34
SPEED_OF_LIGHT_M_S = 299792458.0
NM_TO_M = 1e-9
# 1 W/m^2 = 100 µW/cm^2
W_M2_TO_UW_CM2 = 100.0

BIOLOGICAL_LO_NM = 380.0
BIOLOGICAL_HI_NM = 780.0
BLUE_SEARCH_LO_NM = 400.0
BLUE_SEARCH_HI_NM = 520.0

# mk_GetSpectrum returns mW·m⁻²·nm⁻¹. Every capture is re-checked against the meter's own lux
# (UNIT_CHECK_MISMATCH) so a wrong factor cannot pass silently.
INSTRUMENT_TO_W_M2_NM = 1e-3
UNIT_CHECK_TOLERANCE = 0.03
UNIT_CHECK_MIN_LUX = 0.5

REPEAT_CV_LIMIT_PERCENT = 5.0
AMBIENT_FRACTION_LIMIT = 0.01
