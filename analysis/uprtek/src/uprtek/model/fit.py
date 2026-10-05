"""Fit the settings-to-exposure model from model-capture sessions (64-bit Python, numpy).

Inputs are session folders: calibration (primaries and mixtures stages), optional brightness
references (color_set sessions, for the full-code response at every level), and validation. Each
is analyzed with ``analyze_session`` if it has no analysis folder yet.

Outputs are display-attributable: a stimulus minus the mean of the black captures at the same level
and duty in the same session (the block's black_start/black_end; ``K`` for a color_set reference).
Signed differences are kept; predictions are clamped at zero.

Candidates are chosen by leave-one-session-out cross-validation on the calibration sessions (by
session index), simplest first. Validation sessions are only evaluated, never used for selection.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from uprtek import __version__
from uprtek.model import forward
from uprtek.model.estimator import multi_channel_level_ok
from uprtek.rawio import list_captures, read_session
from uprtek.report.analyze import analyze_session

PACKAGE_SCHEMA = "chronolume-exposure-calibration/1"
REPORT_SCHEMA = "chronolume-exposure-validation/1"
OUTPUTS = {"melanopic_edi": "edi", "photopic": "lux"}
# MK350S specified photopic minimum; applied to photopic illuminance only (spec Section 6).
PHOTOPIC_MIN_LUX = 1.0
TARGET_MAPE = 5.0
TARGET_P95 = 10.0
INTERACTION_RIDGE = 1e-3
# Per-output signal criterion: net >= SIGNAL_TO_NOISE * max(|K_end - K_start|, BACKGROUND_REL_FLOOR * mean K).
SIGNAL_TO_NOISE = 3.0
BACKGROUND_REL_FLOOR = 0.1
# Net melanopic EDI floor: red alone reads 0.1-0.3 lx melanopic EDI independent of its code
# (model sessions of 2026-10-05), a meter floor rather than display output.
MELANOPIC_MIN_EDI = 0.5
# Simplest first: (name, kind, interaction, melanopic-from-photopic channels). The interaction is
# False, True (total form) or "channel" (per-channel form). The listed channels take their melanopic
# response from their photopic one times a per-channel melanopic DER (fit_candidate): red's melanopic
# signal is at the meter floor, so its own melanopic code nodes drop out in some folds.
CANDIDATES = (
    ("factorized", "factorized", False, ()),
    ("factorized+H", "factorized", True, ()),
    ("factorized+Hc", "factorized", "channel", ()),
    ("factorized+Hc+Rder", "factorized", "channel", ("R",)),
    ("crossed", "crossed", False, ()),
    ("crossed+H", "crossed", True, ()),
    ("crossed+Hc", "crossed", "channel", ()),
)
REFERENCE_LABEL = re.compile(r"^([RGB])255(?:_br(\d))?$")


@dataclass
class Row:
    session: str
    session_index: int | None
    stage: str | None
    role: str
    block: int | None
    label: str
    rgb: tuple[int, int, int]
    level: int
    duty: int
    edi: float  # display-attributable
    lux: float
    lux_raw: float
    quantitative: bool
    edi_noise: float = 0.0  # background uncertainty of the net value (see background_noise)
    lux_noise: float = 0.0


@dataclass
class Setting:
    rgb: tuple[int, int, int]
    level: int
    duty: int
    edi: float
    lux: float
    lux_raw: float
    n: int
    sessions: tuple[str, ...]
    edi_noise: float = 0.0
    lux_noise: float = 0.0

    @property
    def key(self) -> str:
        return "{},{},{},{},{}".format(*self.rgb, self.level, self.duty)

    @property
    def group(self) -> str:
        return "mixture" if sum(1 for v in self.rgb if v > 0) > 1 else "primary"

    @property
    def composition(self) -> str:
        return composition(self.rgb)

    @property
    def in_range(self) -> bool:
        """Acquisition criterion: the gross photopic reading is within the meter's specified range."""
        return self.lux_raw >= PHOTOPIC_MIN_LUX

    def eligible(self, attr: str, floor: bool = True) -> bool:
        """Eligibility of one output, from the measurement alone: in range and a positive net value
        at least SIGNAL_TO_NOISE times its background uncertainty. Scoring (``floor``) also requires
        melanopic EDI >= MELANOPIC_MIN_EDI; fitting does not, so a channel whose own melanopic output
        is at the floor still has parameters for the mixtures it is part of."""
        net, noise = getattr(self, attr), getattr(self, f"{attr}_noise")
        if floor and attr == "edi" and net < MELANOPIC_MIN_EDI:
            return False
        return self.in_range and net > 0 and net >= SIGNAL_TO_NOISE * noise


def composition(rgb: tuple[int, int, int] | list[int]) -> str:
    """R, G or B alone; 'gray' (equal codes); a two-channel pair (RG, RB, GB); or 'RGB_unequal'."""
    on = "".join(c for c, v in zip("RGB", rgb) if v > 0)
    if len(on) <= 1:
        return on or "black"
    if len(on) == 3 and rgb[0] == rgb[1] == rgb[2]:
        return "gray"
    return on if len(on) == 2 else "RGB_unequal"


def background_noise(blacks: list[float]) -> float:
    """Uncertainty of a block's background: the larger of the drift between its bracketing blacks
    and BACKGROUND_REL_FLOOR of their mean (a single black gives only the floor)."""
    if not blacks:
        return 0.0
    drift = abs(blacks[-1] - blacks[0]) if len(blacks) > 1 else 0.0
    return max(drift, BACKGROUND_REL_FLOOR * abs(float(np.mean(blacks))))


def _summaries(session_dir: Path) -> dict[str, dict[str, Any]]:
    analysis = session_dir / "analysis"
    if not (analysis / "summary.csv").exists():
        analyze_session(session_dir)
    return {p.parent.name: json.loads(p.read_text(encoding="utf-8")) for p in analysis.glob("*/summary.json")}


def load_rows(session_dir: str | Path, *, reference: bool = False) -> list[Row]:
    """Stimulus rows of a session with the same-state black subtracted. With ``reference`` only the
    full-code primaries of a color_set session (R255, R255_br<n>, …) are kept."""
    session_dir = Path(session_dir)
    session = read_session(session_dir)
    summaries = _summaries(session_dir)
    raw: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for p in list_captures(session_dir):
        meta = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        s = summaries.get(meta.get("label", p.stem))
        if s is not None:
            raw.append((meta, s))

    def vals(s: dict[str, Any]) -> tuple[float, float]:
        return float(s.get("melanopic_EDI_lux") or 0.0), float(s.get("photopic_illuminance_lux") or 0.0)

    # Per block: (mean edi, mean lux, edi noise, lux noise) of its blacks, start before end.
    background: dict[Any, tuple[float, float, float, float]] = {}
    by_block: dict[Any, list[tuple[float, float]]] = {}
    order = {"black_start": 0, "stimulus": 0, "black_end": 1}
    for meta, s in sorted(raw, key=lambda ms: order.get(str(ms[0].get("role", "")), 0)):
        if reference and meta.get("label") == "K":
            by_block.setdefault(None, []).append(vals(s))
        elif not reference and str(meta.get("role", "")).startswith("black"):
            by_block.setdefault(meta.get("block_index"), []).append(vals(s))
    for b, vs in by_block.items():
        e, x = [v[0] for v in vs], [v[1] for v in vs]
        background[b] = (float(np.mean(e)), float(np.mean(x)), background_noise(e), background_noise(x))

    rows = []
    for meta, s in raw:
        label = meta.get("label", "")
        if reference:
            if not REFERENCE_LABEL.match(label):
                continue
            bg = background.get(None, (0.0, 0.0, 0.0, 0.0))
        else:
            if meta.get("role") != "stimulus":
                continue
            bg = background.get(meta.get("block_index"), (0.0, 0.0, 0.0, 0.0))
        state = meta.get("host_state") or {}
        try:
            level, duty = int(state["brightness"]), int(state["duty_cycle"])
        except (KeyError, TypeError, ValueError):
            continue
        edi, lux = vals(s)
        rows.append(Row(
            session=session_dir.name,
            session_index=session.get("session_index"),
            stage=session.get("model_stage") or ("reference" if reference else None),
            role=meta.get("role", "stimulus"),
            block=meta.get("block_index"),
            label=label,
            rgb=tuple(int(v) for v in meta["rgb"]),
            level=level,
            duty=duty,
            edi=edi - bg[0],
            lux=lux - bg[1],
            lux_raw=lux,
            quantitative=bool(s.get("quantitative")),
            edi_noise=bg[2],
            lux_noise=bg[3],
        ))
    return rows


def average(rows: list[Row]) -> list[Setting]:
    """Mean of the quantitative rows of each setting. Eligibility is decided per output on the means
    (Setting.eligible); nothing is dropped here for being dim."""
    groups: dict[tuple, list[Row]] = {}
    for r in rows:
        if r.quantitative:
            groups.setdefault((r.rgb, r.level, r.duty), []).append(r)
    return [Setting(rgb, level, duty,
                    edi=float(np.mean([r.edi for r in rs])), lux=float(np.mean([r.lux for r in rs])),
                    lux_raw=float(np.mean([r.lux_raw for r in rs])), n=len(rs),
                    sessions=tuple(sorted({r.session for r in rs})),
                    edi_noise=float(np.mean([r.edi_noise for r in rs])),
                    lux_noise=float(np.mean([r.lux_noise for r in rs])))
            for (rgb, level, duty), rs in sorted(groups.items())]


def _single_channel(s: Setting) -> tuple[str, int] | None:
    nz = [(c, s.rgb[i]) for c, i in forward.CHANNEL_INDEX.items() if s.rgb[i] > 0]
    return nz[0] if len(nz) == 1 else None


def fit_primaries(model_settings: list[Setting], reference_settings: list[Setting], attr: str,
                  kind: str) -> dict[str, Any]:
    """Code (nodes per ladder level), level and duty responses per primary (log-linear least squares),
    plus the crossed grid nodes for ``kind == "crossed"``. Only settings eligible for ``attr`` (without
    the melanopic scoring floor) are used, so a primary's melanopic and photopic fits can rest on
    different settings."""
    prims: dict[str, Any] = {}
    for c in forward.CHANNEL_INDEX:
        obs = []  # (code, level, duty, value, from the model grid)
        for s in model_settings:
            sc = _single_channel(s)
            if sc and sc[0] == c and s.eligible(attr, floor=False):
                obs.append((sc[1], s.level, s.duty, getattr(s, attr), True))
        for s in reference_settings:
            if _single_channel(s) == (c, 255) and s.eligible(attr, floor=False):
                obs.append((255, s.level, s.duty, getattr(s, attr), False))
        grid_duties = sorted({o[2] for o in obs if o[4]})
        if not grid_duties:
            continue
        obs = [o for o in obs if o[2] in grid_duties]
        pos = [o for o in obs if o[3] > 0]
        base = max(grid_duties)
        if not any(o[0] == 255 for o in pos) or not any(o[2] == base for o in pos):
            continue
        # A node needs its level's code-255 output to be identified; nodes without it are dropped.
        full_levels = {o[1] for o in pos if o[0] == 255}
        pos = [o for o in pos if o[0] == 255 or o[1] in full_levels]
        node_keys = sorted({(o[1], o[0]) for o in pos if o[0] != 255})
        levels = sorted({o[1] for o in pos})
        duties = sorted({o[2] for o in pos} - {base})
        col = {("a", L, k): i for i, (L, k) in enumerate(node_keys)}
        col.update({("b", L): len(col) + i for i, L in enumerate(levels)})
        col.update({("q", D): len(col) + i for i, D in enumerate(duties)})
        X = np.zeros((len(pos), len(col)))
        y = np.log([o[3] for o in pos])
        for r, (k, L, D, _, _) in enumerate(pos):
            if k != 255:
                X[r, col[("a", L, k)]] = 1.0
            X[r, col[("b", L)]] = 1.0
            if D != base:
                X[r, col[("q", D)]] = 1.0
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        code_nodes: dict[str, Any] = {}
        for L, k in node_keys:
            node = code_nodes.setdefault(str(L), {"codes": [0], "a": [0.0]})
            node["codes"].append(k)
            node["a"].append(float(np.exp(coef[col[("a", L, k)]])))
        for node in code_nodes.values():
            node["codes"].append(255)
            node["a"].append(1.0)
        top = code_nodes[str(max(L for L, _ in node_keys))] if node_keys else {"codes": [0, 255], "a": [0.0, 1.0]}
        prim: dict[str, Any] = {
            "codes": list(top["codes"]),
            "a": list(top["a"]),
            "code_nodes": code_nodes,
            "min_code": min(k for _, k in node_keys) if node_keys else None,
            "levels": {str(L): float(np.exp(coef[col[("b", L)]])) for L in levels},
            "duties": sorted(duties + [base]),
            "q": [1.0 if D == base else float(np.exp(coef[col[("q", D)]])) for D in sorted(duties + [base])],
            "base_duty": base,
        }
        if kind == "crossed":
            nodes: dict[str, dict[str, Any]] = {}
            for k, L, D, v, from_grid in obs:
                if not from_grid:
                    continue
                node = nodes.setdefault(str(L), {}).setdefault(str(D), {})
                node.setdefault(k, []).append(v)
            prim["crossed"] = {
                L: {D: {"codes": sorted(node), "values": [float(np.mean(node[k])) for k in sorted(node)]}
                    for D, node in by_d.items()}
                for L, by_d in nodes.items()
            }
        prims[c] = prim
    return {"primaries": prims}


def fit_interaction(params: dict[str, Any], model_settings: list[Setting], attr: str,
                    kind: str, form: str = "total") -> list[float] | None:
    """beta of g(L, D). With the channel form the demand regressor is sum_c f_c O_c / sum_c f_c, so
    t = 1 - measured / sum is linear in beta for both forms."""
    X, t = [], []
    for s in model_settings:
        if s.group != "mixture" or not s.eligible(attr, floor=False):
            continue
        parts = forward.channel_outputs(params, s.rgb, s.level, s.duty, kind)
        total = sum(parts.values()) if parts else 0.0
        if not total:
            continue
        if form == "channel":
            e = sum(v * forward.other_demand(s.rgb, i) for i, v in parts.items()) / total
        else:
            e = forward.extra_demand(s.rgb)
        X.append([e, e * s.level / forward.MAX_LEVEL, e * s.duty / 100.0])
        t.append(1.0 - getattr(s, attr) / total)
    if len(t) < 3:
        return None
    A = np.asarray(X)
    beta = np.linalg.solve(A.T @ A + INTERACTION_RIDGE * np.eye(3), A.T @ np.asarray(t))
    return [float(v) for v in beta]


def primary_der(model_settings: list[Setting], reference_settings: list[Setting]) -> dict[str, float]:
    """Melanopic DER of each primary: geometric mean of edi / lux over its single-channel settings
    eligible for both outputs (scoring rule, with the melanopic floor)."""
    ratios: dict[str, list[float]] = {}
    for s in list(model_settings) + list(reference_settings):
        sc = _single_channel(s)
        if sc and s.eligible("edi") and s.eligible("lux"):
            ratios.setdefault(sc[0], []).append(s.edi / s.lux)
    return {c: float(np.exp(np.mean(np.log(v)))) for c, v in ratios.items()}


def fit_candidate(model_settings: list[Setting], reference_settings: list[Setting], kind: str,
                  interaction: bool | str, der_channels: tuple[str, ...] = ()) -> dict[str, Any]:
    """With ``der_channels``, each listed primary's melanopic parameters are its photopic ones with
    the full-code levels times its melanopic DER (code and duty responses shared); a primary without
    a setting eligible for both outputs keeps its own melanopic fit. The interaction is fitted after."""
    out = {}
    form = "channel" if interaction == "channel" else "total"
    for name, attr in OUTPUTS.items():
        out[name] = fit_primaries(model_settings, reference_settings, attr, kind)
    if der_channels:
        der = primary_der(model_settings, reference_settings)
        lux_prims = out["photopic"]["primaries"]
        for c in der_channels:
            if c in der and c in lux_prims:
                prim = json.loads(json.dumps(lux_prims[c]))
                prim["levels"] = {L: v * der[c] for L, v in prim["levels"].items()}
                if "crossed" in prim:
                    prim["crossed"] = {L: {D: {**node, "values": [v * der[c] for v in node["values"]]}
                                           for D, node in by_d.items()} for L, by_d in prim["crossed"].items()}
                prim["melanopic_der"] = der[c]
                out["melanopic_edi"]["primaries"][c] = prim
    for name, attr in OUTPUTS.items():
        params = out[name]
        params["interaction"] = fit_interaction(params, model_settings, attr, kind, form) if interaction else None
        if interaction:
            params["interaction_form"] = form
    return out


def predict(outputs: dict[str, Any], s: Setting | tuple, kind: str, interaction: bool) -> dict[str, float | None]:
    rgb, level, duty = (s.rgb, s.level, s.duty) if isinstance(s, Setting) else s
    return {attr: forward.output(outputs[name], rgb, level, duty, kind, interaction)
            for name, attr in OUTPUTS.items()}


Pair = tuple[Setting, dict[str, float | None]]


def _pct_summary(meas: list[float], pred: list[float]) -> dict[str, Any]:
    if not meas:
        return {"n": 0}
    m, q = np.asarray(meas, dtype=float), np.asarray(pred, dtype=float)
    pct = 100.0 * (q - m) / m
    return {"n": int(len(m)), "mape": float(np.mean(np.abs(pct))),
            "p95_ape": float(np.percentile(np.abs(pct), 95)), "bias_pct": float(np.mean(pct)),
            "mae": float(np.mean(np.abs(q - m))), "rmse": float(np.sqrt(np.mean((q - m) ** 2)))}


def _output_summary(pairs: list[Pair], attr: str) -> dict[str, Any]:
    """Percentage errors over the settings eligible for ``attr`` (decided by the measurement alone;
    a prediction below range stays in). An eligible setting without a prediction is a failure. The
    absolute error ``mae_all`` covers every quantitative setting with a prediction, eligible or not."""
    elig = [(s, p) for s, p in pairs if s.eligible(attr)]
    done = [(getattr(s, attr), p[attr]) for s, p in elig if p[attr] is not None]
    every = [abs(p[attr] - getattr(s, attr)) for s, p in pairs if p[attr] is not None]
    return {**_pct_summary([m for m, _ in done], [q for _, q in done]),
            "n_eligible": len(elig), "n_ineligible": len(pairs) - len(elig),
            "n_failed": sum(1 for _, p in elig if p[attr] is None),
            "n_all": len(every), "mae_all": float(np.mean(every)) if every else None}


def _der_summary(pairs: list[Pair]) -> dict[str, Any]:
    """DER where both measured outputs are eligible and both predictions exist."""
    elig = [(s, p) for s, p in pairs if s.eligible("edi") and s.eligible("lux")]
    done = [(s.edi / s.lux, p["edi"] / p["lux"]) for s, p in elig
            if p["edi"] is not None and p["lux"] is not None and p["lux"] > 0]
    return {**_pct_summary([m for m, _ in done], [q for _, q in done]),
            "n_eligible": len(elig), "n_ineligible": len(pairs) - len(elig),
            "n_failed": len(elig) - len(done)}


def metrics(pairs: list[Pair]) -> dict[str, Any]:
    """Error summary with measurement-based eligibility. Counts: settings without any prediction
    (``n_unsupported``), eligible settings without a prediction (``n_failed``), and photopic-eligible
    settings predicted below the meter's range (``n_pred_below_range``, kept in the errors)."""
    res: dict[str, Any] = {
        "n_settings": len(pairs),
        "n_unsupported": sum(1 for _, p in pairs if p["edi"] is None or p["lux"] is None),
        "n_failed": sum(1 for s, p in pairs
                        if any(s.eligible(a) and p[a] is None for a in OUTPUTS.values())),
        "n_pred_below_range": sum(1 for s, p in pairs if s.eligible("lux") and p["lux"] is not None
                                  and p["lux"] < PHOTOPIC_MIN_LUX),
    }
    for attr in OUTPUTS.values():
        res[attr] = _output_summary(pairs, attr)
    res["der"] = _der_summary(pairs)
    res["by_group"] = {}
    for group in ("all", "primary", "mixture"):
        sub = pairs if group == "all" else [(s, p) for s, p in pairs if s.group == group]
        res["by_group"][group] = {**{a: _output_summary(sub, a) for a in OUTPUTS.values()},
                                  "der": _der_summary(sub)}
    strata: dict[str, Any] = {}
    for key, fn in (("group", lambda s: s.group), ("composition", lambda s: s.composition),
                    ("level", lambda s: s.level), ("duty", lambda s: s.duty)):
        values = sorted({fn(s) for s, _ in pairs}, key=str)
        strata[key] = {str(v): {a: _output_summary([(s, p) for s, p in pairs if fn(s) == v], a)
                                for a in OUTPUTS.values()}
                       for v in values}
    res["strata"] = strata
    return res


def meets_targets(m: dict[str, Any]) -> bool:
    return all(m[a].get("n", 0) > 0 and m[a]["mape"] <= TARGET_MAPE and m[a]["p95_ape"] <= TARGET_P95
               for a in OUTPUTS.values()) and m["n_failed"] == 0


def cross_validate(cal_rows: list[Row], ref_settings: list[Setting], kind: str, interaction: bool,
                   der_channels: tuple[str, ...] = ()) -> dict[str, Any]:
    indices = sorted({r.session_index for r in cal_rows if r.session_index is not None})
    if len(indices) < 2:
        settings = average(cal_rows)
        outputs = fit_candidate(settings, ref_settings, kind, interaction, der_channels)
        return {"method": "in_sample", **metrics([(s, predict(outputs, s, kind, interaction)) for s in settings])}
    pairs = []
    for k in indices:
        train = average([r for r in cal_rows if r.session_index != k])
        test = average([r for r in cal_rows if r.session_index == k])
        outputs = fit_candidate(train, ref_settings, kind, interaction, der_channels)
        pairs += [(s, predict(outputs, s, kind, interaction)) for s in test]
    return {"method": f"leave-one-session-out ({len(indices)} folds)", **metrics(pairs)}


def fallback_candidate(selection: dict[str, dict[str, Any]]) -> str:
    """When no candidate meets the targets: fewest failed predictions, then lowest worst-case MAPE."""
    def worst(n: str) -> tuple[int, float]:
        m = selection[n]
        return m["n_failed"], max(m[a].get("mape", float("inf")) if m[a].get("n") else float("inf")
                                  for a in OUTPUTS.values())
    return min(selection, key=worst)


def session_anchor(session_dir: str | Path) -> float | None:
    """Mean photopic illuminance (meter reading) of a session's anchor captures (blue 255 at level 8)."""
    vals = []
    for p in sorted((Path(session_dir) / "anchors" / "raw").glob("ANCHOR_*.json")):
        reps = [r["instrument"]["lux"] for r in json.loads(p.read_text(encoding="utf-8")).get("repeats", [])
                if (r.get("instrument") or {}).get("lux") is not None]
        if reps:
            vals.append(float(np.mean(reps)))
    return float(np.mean(vals)) if vals else None


def anchor_normalized(calibration: list[Path], cal_rows: list[Row],
                      ref_settings: list[Setting]) -> dict[str, Any] | None:
    """Cross-validation with every calibration session scaled to the mean anchor of all sessions
    (net values and their background uncertainties; the gross reading that decides the meter range
    is not scaled). Reported alongside the raw selection, never used for it."""
    anchors = {Path(d).name: session_anchor(d) for d in calibration}
    known = [v for v in anchors.values() if v]
    if len(known) != len(anchors):
        return None
    ref = float(np.mean(known))
    rows = []
    for r in cal_rows:
        f = ref / anchors[r.session]
        rows.append(Row(**{**r.__dict__, "edi": r.edi * f, "lux": r.lux * f,
                           "edi_noise": r.edi_noise * f, "lux_noise": r.lux_noise * f}))
    return {"anchor_lux": anchors, "reference_lux": ref,
            "rule": "each calibration session scaled by reference_lux / its mean anchor; reference sessions unscaled",
            "candidates": {n: cross_validate(rows, ref_settings, k, i, d) for n, k, i, d in CANDIDATES}}


def _configuration(dirs: list[Path]) -> tuple[dict[str, Any], list[str]]:
    keys = ("device", "firmware", "product_id", "film", "display_mode")
    seen: dict[str, set] = {k: set() for k in (*keys, "eye", "wear_detection", "grid_version")}
    for d in dirs:
        s = read_session(d)
        host = s.get("host") or {}
        for k in keys:
            if host.get(k) is not None:
                seen[k].add(str(host[k]))
        eye = (s.get("measure_options") or s.get("options") or {}).get("eye")
        if eye:
            seen["eye"].add(eye)
        if s.get("wear_detection"):
            seen["wear_detection"].add(s["wear_detection"])
        if s.get("grid"):
            seen["grid_version"].add(s["grid"]["version"])
    warnings = [f"sessions differ in {k}: {sorted(v)}" for k, v in seen.items() if len(v) > 1]
    return {k: (sorted(v)[0] if len(v) == 1 else sorted(v) if v else None) for k, v in seen.items()}, warnings


def fit_package(calibration: list[Path], references: list[Path] | None = None, *,
                calibration_id: str | None = None, multi_channel_max_level: int = 7,
                white_max_level: int = 7) -> dict[str, Any]:
    references = references or []
    cal_rows = [r for d in calibration for r in load_rows(d)]
    ref_rows = [r for d in references for r in load_rows(d, reference=True)]
    if not cal_rows:
        raise ValueError("no calibration stimulus rows")
    ref_settings = average(ref_rows)
    selection = {}
    for name, kind, inter, der in CANDIDATES:
        selection[name] = cross_validate(cal_rows, ref_settings, kind, inter, der)
    chosen = next((n for n, *_ in CANDIDATES if meets_targets(selection[n])), None)
    met = chosen is not None
    if chosen is None:
        chosen = fallback_candidate(selection)
    kind, inter, der = next((k, i, d) for n, k, i, d in CANDIDATES if n == chosen)
    settings = average(cal_rows)
    outputs = fit_candidate(settings, ref_settings, kind, inter, der)
    config, warnings = _configuration(calibration + references)
    duties = sorted({d for p in outputs["photopic"]["primaries"].values() for d in p["duties"]})
    mix_levels = sorted({s.level for s in settings if sum(1 for v in s.rgb if v > 0) > 1})
    created = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "schema": PACKAGE_SCHEMA,
        "calibration_id": calibration_id or f"chronolume-{_dt.date.today().isoformat()}",
        "created": created,
        "analysis_version": __version__,
        "model": {"name": chosen, "kind": kind, "interaction": inter, "melanopic_from_photopic": list(der)},
        "configuration": config,
        "units": {"melanopic_edi": "lx", "photopic": "lx", "melanopic_der": "1"},
        "background_convention": ("display-attributable: stimulus minus the mean of black captures at the "
                                  "same level and duty in the same session"),
        "validity": {
            "photopic_min_lux": PHOTOPIC_MIN_LUX,
            "der_min_photopic_lux": PHOTOPIC_MIN_LUX,
            "multi_channel_max_level": multi_channel_max_level,
            "multi_channel_min_level": mix_levels[0] if mix_levels else None,
            "white_max_level": white_max_level,
            "stimulus_geometry": "uniform_full_frame",
        },
        "domain": {"duty_range": [min(duties), max(duties)] if duties else None, "duties_measured": duties,
                   "levels": {c: sorted(int(l) for l in p["levels"])
                              for c, p in outputs["photopic"]["primaries"].items()}},
        "outputs": outputs,
        "calibration_settings": [s.key for s in settings if s.in_range],
        "fit": {
            "eligibility": {
                "photopic_min_lux_gross": PHOTOPIC_MIN_LUX,
                "signal_to_noise": SIGNAL_TO_NOISE,
                "background_rel_floor": BACKGROUND_REL_FLOOR,
                "melanopic_min_edi": MELANOPIC_MIN_EDI,
                "rule": ("per output: quantitative capture, gross photopic >= photopic_min_lux_gross, net > 0 "
                         "and net >= signal_to_noise * max(|K_end - K_start|, background_rel_floor * mean K); "
                         "for scoring, melanopic EDI also net >= melanopic_min_edi (fitting keeps lower values)"),
            },
            "primaries": ("log-space least squares of a_c(k, L) b_c(L) q_c(D) on eligible single-channel "
                          "settings, one free a_c(k, L) per code below 255 and level where it was measured "
                          "(code nodes); between nodes power-law segments in code and log-linear in level; "
                          "below a level's lowest node the lowest segment is extended toward code 0"),
            "melanopic_from_photopic": ("for the model's melanopic_from_photopic channels, melanopic parameters are "
                                        "the photopic ones with full-code levels times melanopic_der, the geometric "
                                        "mean of edi / lux over that channel's single-channel settings eligible for "
                                        "both outputs (scoring rule); code and duty responses are shared"),
            "interaction": {"ridge_lambda": INTERACTION_RIDGE,
                            "equation": "beta = (A^T A + ridge_lambda I)^-1 A^T t, rows [E, E L/8, E D/100], "
                                        "t = 1 - measured / predicted sum, eligible mixtures only; "
                                        "E = sum(k/255) - max(k/255) (total form) or sum_c f_c O_c / sum_c f_c "
                                        "(channel form)"},
            "cross_validation": ("leave-one-session-out by session_index; every learned parameter (a, b, q, "
                                 "beta, crossed nodes) is refit on each fold's training sessions; reference "
                                 "sessions are fixed inputs and are not folded"),
            "no_candidate_meets_targets": ("fewest failed predictions, then lowest worst-case cross-validation "
                                           "MAPE; meets_targets false"),
        },
        "selection": {"targets": {"mape": TARGET_MAPE, "p95_ape": TARGET_P95}, "chosen": chosen,
                      "meets_targets": met, "candidates": selection,
                      "anchor_normalized": anchor_normalized(calibration, cal_rows, ref_settings)},
        "validation": None,
        "training_sessions": [str(d) for d in calibration],
        "reference_sessions": [str(d) for d in references],
        "warnings": warnings,
    }


def validate_package(package: dict[str, Any], validation: list[Path]) -> dict[str, Any]:
    """Evaluate the locked package on independent sessions; every candidate kind is reported for
    context, but only the package's model is the validation result."""
    rows = [r for d in validation for r in load_rows(d)]
    settings = average(rows)
    kind, inter = package["model"]["kind"], package["model"]["interaction"]
    validity = package.get("validity") or {}
    pairs = [(s, predict(package["outputs"], s, kind, inter) if multi_channel_level_ok(validity, s.rgb, s.level)
              else {"edi": None, "lux": None}) for s in settings]
    m = metrics(pairs)
    cal_duties = set((package.get("domain") or {}).get("duties_measured") or [])
    val_duties = sorted({s.duty for s in settings})
    return {
        "schema": REPORT_SCHEMA,
        "calibration_id": package["calibration_id"],
        "model": package["model"],
        "created": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sessions": [str(d) for d in validation],
        "n_non_quantitative": len({(r.rgb, r.level, r.duty) for r in rows}) - len(settings),
        "validation_duties": val_duties,
        "duty_heldout": bool(val_duties) and not (set(val_duties) & cal_duties),
        "targets": package["selection"]["targets"],
        "meets_targets": meets_targets(m),
        "metrics": m,
        "cross_validation": package["selection"]["candidates"][package["model"]["name"]],
        "candidates": {n: {a: {k: c[a].get(k) for k in ("n", "mape", "p95_ape")} for a in ("edi", "lux")}
                       | {"meets_targets": meets_targets(c), "n_failed": c["n_failed"]}
                       for n, c in package["selection"]["candidates"].items()},
        "n_calibration_settings": len(package["calibration_settings"]),
        "domain": package["domain"],
        "predictions": [{"rgb": list(s.rgb), "level": s.level, "duty": s.duty, "group": s.group,
                         "composition": s.composition,
                         "eligible_edi": s.eligible("edi"), "eligible_lux": s.eligible("lux"),
                         "measured_edi": s.edi, "predicted_edi": p["edi"],
                         "measured_lux": s.lux, "predicted_lux": p["lux"]} for s, p in pairs],
    }
