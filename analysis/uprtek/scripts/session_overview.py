"""Overview figures (spectra, melanopic EDI, anchor stability) for one UPRtek session.

Reads only the session's own outputs (`analysis/summary.csv`, per-capture
`spectrum.csv`, `session.json`), so `chronolume-uprtek analyze` must have run.
Works for `hosi_set` and `color_set` sessions, and for partial ones: panels plot
whatever channels, ladders and brightness levels the session has.

    python analysis/uprtek/scripts/session_overview.py data/uprtek/session_YYYYMMDD_HHMMSS

Writes `analysis/overview/` in the session folder.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch
from matplotlib.ticker import NullFormatter

CIE_ACTION = Path(__file__).resolve().parents[1] / "src" / "uprtek" / "data" / "cie" / "CIE_a-opic_action_spectra.csv"
MEL_COL = 5

CHANNELS = "RGBW"
CHANNEL_COLORS = {"R": "#c0392b", "G": "#27ae60", "B": "#2c5fd6", "W": "0.35"}
PRIMARIES = [f"{c}255" for c in CHANNELS]
LADDER_CODES = (16, 32, 64, 128, 192, 255)
DAY_MIN_EDI = 250.0
EVENING_MAX_EDI = 10.0


def code_label(channel: str, code: int) -> str:
    return f"{channel}{code:03d}"


def ladder_labels(channel: str, rows: dict) -> list[str]:
    return [code_label(channel, c) for c in LADDER_CODES if code_label(channel, c) in rows]


def sweep_labels(channel: str, rows: dict) -> list[str]:
    # Duty-block captures (W255_br7_d60) run at their own duty and are not part of the brightness sweep.
    return sorted((l for l in rows if l.startswith(f"{channel}255_br") and "_d" not in l),
                  key=lambda l: rows[l]["brightness_level"])


def level(rows: dict, label: str) -> int:
    return int(rows[label]["brightness_level"])


def load_summary(analysis: Path) -> dict[str, dict]:
    rows = {}
    with (analysis / "summary.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            r["quantitative"] = r["quantitative"] == "True"
            r["qc_flags"] = r["qc_flags"].split()
            for k in ("melanopic_EDI_lux", "photopic_illuminance_lux", "brightness_level", "irradiance_380_780_W_m2"):
                r[k] = float(r[k])
            rows[r["label"]] = r
    return rows


def load_spectrum(analysis: Path, label: str) -> tuple[np.ndarray, np.ndarray]:
    d = np.genfromtxt(analysis / label / "spectrum.csv", delimiter=",", names=True)
    return d["wavelength_nm"], d["spectral_irradiance_uW_cm2_nm"]


def load_melanopic() -> tuple[np.ndarray, np.ndarray]:
    d = np.genfromtxt(CIE_ACTION, delimiter=",")
    wl, s = d[:, 0], np.nan_to_num(d[:, MEL_COL])
    return wl, s / s.max()


def session_conditions(session: dict, rows: dict) -> str:
    duty = (session.get("duty") or {}).get("reported", "?")
    base = (session.get("base_brightness") or {}).get("reported", "?")
    m = session.get("meter", {})
    white = ""
    if "W255" in rows and str(level(rows, "W255")) != str(base):
        white = f"; white at brightness {level(rows, 'W255')}, as white is not run at 8"
    return (f"{session.get('host', {}).get('device', '')}, duty {duty}, brightness {base} "
            f"(unless noted{white}), eye: {session.get('options', {}).get('eye', '')}. "
            f"UPRtek {m.get('model', '')} SN {m.get('optical_sn', '')}, corneal-plane irradiance.")


def _no_data(ax, title: str) -> None:
    ax.text(0.5, 0.5, "not in this session", transform=ax.transAxes, ha="center", va="center", color="0.5")
    ax.set_title(title, loc="left", fontweight="bold")


def fig_overview(analysis: Path, rows: dict, session: dict, out: Path) -> None:
    fig, axs = plt.subplots(2, 2, figsize=(11, 8.2))
    (ax_a, ax_b), (ax_c, ax_d) = axs
    prims = [l for l in PRIMARIES if l in rows]

    # A: primary spectra with the melanopic action spectrum.
    if prims:
        mwl, mel = load_melanopic()
        for lab in prims:
            wl, e = load_spectrum(analysis, lab)
            ax_a.plot(wl, e, color=CHANNEL_COLORS[lab[0]], lw=1.4, label=f"{lab} (br {level(rows, lab)})")
        ax_a2 = ax_a.twinx()
        ax_a2.fill_between(mwl, mel, color="#8e44ad", alpha=0.12, lw=0)
        ax_a2.plot(mwl, mel, color="#8e44ad", lw=1, ls="--", label="melanopic s(λ)")
        ax_a2.set_ylim(0, 1.05)
        ax_a2.set_ylabel("Melanopic sensitivity (norm.)", color="#8e44ad")
        ax_a2.tick_params(axis="y", colors="#8e44ad")
        ax_a.set_xlim(380, 780)
        ax_a.set_ylim(bottom=0)
        ax_a.set_xlabel("Wavelength (nm)")
        ax_a.set_ylabel(r"Spectral irradiance ($\mu$W cm$^{-2}$ nm$^{-1}$)")
        ax_a.set_title("A  Primaries at full code", loc="left", fontweight="bold")
        h1, l1 = ax_a.get_legend_handles_labels()
        h2, l2 = ax_a2.get_legend_handles_labels()
        ax_a.legend(h1 + h2, l1 + l2, fontsize=8, loc="upper right")
        ax_a.set_zorder(ax_a2.get_zorder() + 1)
        ax_a.patch.set_visible(False)
    else:
        _no_data(ax_a, "A  Primaries at full code")

    # B: photopic lux and melanopic EDI per primary, at each primary's own brightness.
    if prims:
        x = np.arange(len(prims))
        w = 0.38
        lux = [rows[l]["photopic_illuminance_lux"] for l in prims]
        edi = [rows[l]["melanopic_EDI_lux"] for l in prims]
        ax_b.bar(x - w / 2, lux, w, color="0.75")
        bars = ax_b.bar(x + w / 2, edi, w, color=[CHANNEL_COLORS[l[0]] for l in prims])
        for b, v, l in zip(bars, edi, prims):
            der = v / rows[l]["photopic_illuminance_lux"] if rows[l]["photopic_illuminance_lux"] else float("nan")
            ax_b.annotate(f"{v:.0f}\n(DER {der:.2f})", (b.get_x() + b.get_width() / 2, v),
                          xytext=(0, 3), textcoords="offset points", ha="center", va="bottom", fontsize=7.5)
        ax_b.axhline(DAY_MIN_EDI, color="k", ls=":", lw=1)
        ax_b.text(len(prims) - 0.5, DAY_MIN_EDI, f"daytime min. {DAY_MIN_EDI:.0f} lx", ha="right", va="bottom",
                  fontsize=7.5)
        ax_b.set_xticks(x, [f"{l}\nbr {level(rows, l)}" for l in prims])
        ax_b.set_ylim(0, max(max(lux), max(edi), DAY_MIN_EDI) * 1.25)
        ax_b.set_ylabel("lx")
        ax_b.legend(handles=[Patch(color="0.75", label="Photopic illuminance (lx)"),
                             Patch(facecolor="white", edgecolor="0.2", label="Melanopic EDI (lx), stimulus color")],
                    fontsize=8, loc="upper left")
    else:
        _no_data(ax_b, "")
    ax_b.set_title("B  Melanopic EDI vs photopic lux", loc="left", fontweight="bold")

    # C: code ladders, one power law per channel.
    plotted_codes: set[float] = set()
    for ch in CHANNELS:
        labs = [l for l in ladder_labels(ch, rows) if rows[l]["melanopic_EDI_lux"] > 0]
        if len(labs) < 2:
            continue
        codes = np.array([float(rows[l]["rgb"].split()["RGB".index(ch) if ch != "W" else 0]) for l in labs])
        e = np.array([rows[l]["melanopic_EDI_lux"] for l in labs])
        levels = sorted({level(rows, l) for l in labs})
        fit = np.polyfit(np.log(codes), np.log(e), 1)
        col = CHANNEL_COLORS[ch]
        ax_c.loglog(codes, e, "o", color=col,
                    label=f"{ch} (br {'/'.join(map(str, levels))}), γ = {fit[0]:.2f}")
        xx = np.geomspace(codes.min(), codes.max(), 60)
        ax_c.loglog(xx, np.exp(np.polyval(fit, np.log(xx))), "-", color=col, lw=0.8, alpha=0.6)
        if ch == "B":
            for c, v in zip(codes, e):
                ax_c.annotate(f"{v:.1f}" if v < 10 else f"{v:.0f}", (c, v), xytext=(-4, 6),
                              textcoords="offset points", ha="right", fontsize=7, color=col)
        plotted_codes.update(codes)
    if plotted_codes:
        lin_path = analysis / "linearity_blue_code.json"
        if lin_path.exists():
            g = json.loads(lin_path.read_text())["gamma"]
            ax_c.text(0.97, 0.05, f"blue 380–500 nm irradiance γ = {g:.2f}", transform=ax_c.transAxes,
                      ha="right", fontsize=7.5, color="0.35")
        ax_c.axhline(EVENING_MAX_EDI, color="k", ls=":", lw=1)
        ax_c.text(min(plotted_codes), EVENING_MAX_EDI, f" evening max. {EVENING_MAX_EDI:.0f} lx", va="bottom",
                  fontsize=7.5)
        ticks = sorted(plotted_codes)
        ax_c.set_xticks(ticks, [str(int(c)) for c in ticks])
        ax_c.xaxis.set_minor_formatter(NullFormatter())
        ax_c.set_xlabel("Channel code (other channels 0; white R = G = B)")
        ax_c.set_ylabel("Melanopic EDI (lx)")
        ax_c.grid(True, which="both", alpha=0.3)
        ax_c.legend(fontsize=7.5, loc="upper left")
        ax_c.set_title("C  Melanopic EDI vs code", loc="left", fontweight="bold")
    else:
        _no_data(ax_c, "C  Melanopic EDI vs code")

    # D: hardware brightness sweeps, one line per channel.
    all_vals = []
    for ch in CHANNELS:
        labs = sweep_labels(ch, rows)
        if not labs:
            continue
        lv = np.array([rows[l]["brightness_level"] for l in labs])
        ev = np.array([rows[l]["melanopic_EDI_lux"] for l in labs])
        ok = np.array([rows[l]["quantitative"] for l in labs])
        pos = ev > 0
        col = CHANNEL_COLORS[ch]
        ax_d.semilogy(lv[pos], ev[pos], "-", color=col, lw=1, alpha=0.6)
        ax_d.semilogy(lv[ok & pos], ev[ok & pos], "o", color=col, label=f"{ch}255")
        if (~ok & pos).any():
            ax_d.semilogy(lv[~ok & pos], ev[~ok & pos], "o", mfc="white", color=col)
        if ch == "B":
            for l_, v in zip(lv[pos], ev[pos]):
                ax_d.annotate(f"{v:.0f}", (l_, v), xytext=(0, 7), textcoords="offset points", ha="center",
                              fontsize=7, color=col)
        all_vals.extend(ev[pos])
    if all_vals:
        if "B255" in rows:
            ref = rows["B255"]
            ax_d.semilogy([ref["brightness_level"]], [ref["melanopic_EDI_lux"]], "*", ms=11, color="#e67e22",
                          label=f"B255 from ladder (br {ref['brightness_level']:.0f})")
        if any(not rows[l]["quantitative"] for ch in CHANNELS for l in sweep_labels(ch, rows)):
            ax_d.plot([], [], "o", mfc="white", color="0.3", label="non-quantitative")
        ax_d.axhline(DAY_MIN_EDI, color="k", ls=":", lw=1)
        ax_d.axhline(EVENING_MAX_EDI, color="k", ls=":", lw=1)
        ax_d.text(-0.3, DAY_MIN_EDI, "daytime min.", va="bottom", fontsize=7.5)
        ax_d.text(-0.3, EVENING_MAX_EDI, "evening max.", va="bottom", fontsize=7.5)
        ax_d.set_xticks(range(0, 9))
        ax_d.set_xlim(-0.4, 8.4)
        ax_d.set_ylim(min(min(all_vals) * 0.5, 5), max(max(all_vals), DAY_MIN_EDI) * 1.8)
        ax_d.set_xlabel("Hardware brightness level (code 255)")
        ax_d.set_ylabel("Melanopic EDI (lx)")
        ax_d.grid(True, which="both", alpha=0.3)
        ax_d.legend(fontsize=7.5, loc="lower right")
        ax_d.set_title("D  Melanopic EDI vs brightness", loc="left", fontweight="bold")
    else:
        _no_data(ax_d, "D  Melanopic EDI vs brightness")

    fig.suptitle(f"UPRtek session {analysis.parent.name.removeprefix('session_')}: spectra and melanopic dose", fontsize=12)
    fig.text(0.5, 0.005, session_conditions(session, rows), ha="center", fontsize=8, color="0.3")
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    for ext in ("png", "pdf"):
        fig.savefig(out / f"overview_edi.{ext}", dpi=200)
    plt.close(fig)


def fig_anchors(session: dict, out: Path) -> None:
    anchors = [a for a in session.get("anchors", []) if a.get("lux_median")]
    if not anchors:
        return
    t0 = datetime.fromisoformat(session["started"])
    t = np.array([(datetime.fromisoformat(a["time"]) - t0).total_seconds() / 60 for a in anchors])
    ref = anchors[0]["lux_median"]
    r = np.array([a["lux_median"] / ref for a in anchors])
    drift = np.array(["ANCHOR_DRIFT" in a["flags"] for a in anchors])

    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    ax.axhspan(0.9, 1.1, color="0.9", label="±10 % tolerance")
    ax.axhline(1.0, color="0.5", lw=0.8)
    ax.plot(t, r, "-", color="0.6", lw=1)
    ax.plot(t[~drift], r[~drift], "o", color="#2c5fd6", label="within tolerance")
    if drift.any():
        ax.plot(t[drift], r[drift], "o", mfc="white", color="#c0392b", label="ANCHOR_DRIFT")
    ax.set_xlabel("Time since session start (min)")
    ax.set_ylabel("Anchor lux / first anchor")
    ax.set_title(f"Anchor stability: B255 at brightness 8, reference {ref:.1f} lx", fontsize=10)
    ax.set_ylim(min(0.75, r.min() * 0.9), max(1.3, r.max() * 1.1))
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    for ext in ("png", "pdf"):
        fig.savefig(out / f"anchor_stability.{ext}", dpi=200)
    plt.close(fig)


def write_table(rows: dict, out: Path) -> None:
    order = ["K"] + PRIMARIES
    for ch in "BRGW":
        order += [l for l in ladder_labels(ch, rows) if l not in order]
    for ch in CHANNELS:
        order += sweep_labels(ch, rows)
    order += sorted(l for l in rows if l not in order)
    with (out / "edi_table.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["label", "rgb", "brightness_level", "duty_cycle", "photopic_lux", "melanopic_EDI_lux",
                    "melanopic_DER", "irradiance_uW_cm2", "quantitative", "qc_flags"])
        for l in order:
            if l not in rows:
                continue
            r = rows[l]
            der = r["melanopic_EDI_lux"] / r["photopic_illuminance_lux"] if r["photopic_illuminance_lux"] else float("nan")
            w.writerow([l, r["rgb"], int(r["brightness_level"]), r.get("duty_cycle", ""),
                        f"{r['photopic_illuminance_lux']:.3g}",
                        f"{r['melanopic_EDI_lux']:.3g}", f"{der:.3f}", f"{r['irradiance_380_780_W_m2'] * 100:.3g}",
                        r["quantitative"], " ".join(r["qc_flags"])])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("session", type=Path)
    args = p.parse_args()
    analysis = args.session / "analysis"
    session = json.loads((args.session / "session.json").read_text())
    rows = load_summary(analysis)
    out = analysis / "overview"
    out.mkdir(exist_ok=True)
    fig_overview(analysis, rows, session, out)
    fig_anchors(session, out)
    write_table(rows, out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
