"""Display response: power-law (gamma) fit against blue code, step ratios against hardware brightness."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from uprtek.report.plots import plot_brightness_steps, plot_duty, plot_gamma
from uprtek.sequences import DUTY_BLOCK_BRIGHTNESS, parse_duty_label

CODE_MAX = 255.0


@dataclass
class ResponseFit:
    x_name: str
    y_name: str
    model: str  # "power_law" (blue_code), "steps" (brightness) or "linear" (duty)
    labels: list[str]
    x: list[float]
    y: list[float]
    # power_law: y = amplitude_at_max * (x / 255) ** gamma, fitted in log-log space
    gamma: float | None = None
    amplitude_at_max: float | None = None
    r_squared_log: float | None = None
    percent_deviation_from_fit: list[float] = field(default_factory=list)
    # steps: ratio between consecutive measured levels (a skipped level is not bridged silently)
    steps: list[dict[str, float]] = field(default_factory=list)
    fraction_of_max: list[float] = field(default_factory=list)
    # linear (duty): y = slope * duty + intercept
    slope: float | None = None
    intercept: float | None = None
    r_squared: float | None = None


def collect_summaries(analysis_dir: str | Path) -> list[dict[str, Any]]:
    found = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(analysis_dir).rglob("summary.json"))]
    if not found:
        raise FileNotFoundError(f"no summary.json under {analysis_dir}")
    return found


def select_points(summaries: list[dict[str, Any]], x_name: str, y_name: str) -> list[tuple[str, float, float]]:
    pts: list[tuple[str, float, float]] = []
    for s in summaries:
        if not s.get("quantitative"):
            continue
        rgb = s.get("rgb")
        y = s.get(y_name)
        if y is None or not isinstance(rgb, list) or len(rgb) != 3:
            continue
        if rgb[0] != 0 or rgb[1] != 0 or rgb[2] <= 0:
            continue
        in_duty_block = parse_duty_label(s.get("label", "")) is not None
        if x_name == "duty":
            # B255 at the duty-block brightness: the duty block plus the base-duty captures.
            if rgb[2] != CODE_MAX or s.get("duty_cycle") is None:
                continue
            if s.get("brightness_level") != DUTY_BLOCK_BRIGHTNESS:
                continue
            pts.append((s.get("label", ""), float(s["duty_cycle"]), float(y)))
            continue
        if in_duty_block:
            continue
        if x_name == "blue_code":
            # Ladder points only: the brightness sweep holds B=255 and varies hardware brightness.
            if s.get("requested_brightness") is not None:
                continue
            x = float(rgb[2])
        elif x_name == "brightness":
            if s.get("requested_brightness") is None:
                continue
            x = float(s["brightness_level"] if s.get("brightness_level") is not None else s["requested_brightness"])
        else:
            raise ValueError(f"unsupported x axis {x_name!r}; use blue_code, brightness or duty")
        pts.append((s.get("label", ""), x, float(y)))
    return sorted(pts, key=lambda p: p[1])


def fit_gamma(points: list[tuple[str, float, float]], y_name: str) -> ResponseFit:
    pts = [p for p in points if p[1] > 0 and p[2] > 0]
    if len(pts) < 2:
        raise ValueError(f"need at least 2 positive quantitative points for blue_code, got {len(pts)}")
    x = np.array([p[1] for p in pts])
    y = np.array([p[2] for p in pts])
    lx = np.log(x / CODE_MAX)
    ly = np.log(y)
    gamma, log_amp = np.polyfit(lx, ly, 1)
    yhat = np.exp(log_amp) * (x / CODE_MAX) ** gamma
    resid = ly - (gamma * lx + log_amp)
    ss_tot = float(np.sum((ly - ly.mean()) ** 2))
    return ResponseFit(
        x_name="blue_code",
        y_name=y_name,
        model="power_law",
        labels=[p[0] for p in pts],
        x=x.tolist(),
        y=y.tolist(),
        gamma=float(gamma),
        amplitude_at_max=float(np.exp(log_amp)),
        r_squared_log=1.0 - float(np.sum(resid**2)) / ss_tot if ss_tot > 0 else float("nan"),
        percent_deviation_from_fit=[float(100.0 * (yi - yh) / yh) for yi, yh in zip(y, yhat)],
    )


def brightness_steps(points: list[tuple[str, float, float]], y_name: str) -> ResponseFit:
    if len(points) < 2:
        raise ValueError(f"need at least 2 quantitative points for brightness, got {len(points)}")
    x = [p[1] for p in points]
    y = [p[2] for p in points]
    steps = [
        {"from_level": x[i - 1], "to_level": x[i], "ratio": y[i] / y[i - 1] if y[i - 1] > 0 else float("nan")}
        for i in range(1, len(points))
    ]
    top = max(y)
    return ResponseFit(
        x_name="brightness",
        y_name=y_name,
        model="steps",
        labels=[p[0] for p in points],
        x=x,
        y=y,
        steps=steps,
        fraction_of_max=[v / top if top > 0 else float("nan") for v in y],
    )


def fit_duty(points: list[tuple[str, float, float]], y_name: str) -> ResponseFit:
    """Output against duty cycle: a straight line (least squares) and each point's fraction of the
    highest-duty output. Captures repeated at one duty (B255 and B255_br8 at the base) are averaged."""
    by_duty: dict[float, list[tuple[str, float]]] = {}
    for label, x, y in points:
        by_duty.setdefault(x, []).append((label, y))
    if len(by_duty) < 2:
        raise ValueError(f"need at least 2 duty values with quantitative points, got {len(by_duty)}")
    x = sorted(by_duty)
    y = [float(np.mean([v for _, v in by_duty[d]])) for d in x]
    labels = ["+".join(lab for lab, _ in by_duty[d]) for d in x]
    slope, intercept = np.polyfit(x, y, 1)
    yhat = slope * np.asarray(x) + intercept
    ss_tot = float(np.sum((np.asarray(y) - np.mean(y)) ** 2))
    ss_res = float(np.sum((np.asarray(y) - yhat) ** 2))
    top = y[-1]
    return ResponseFit(
        x_name="duty",
        y_name=y_name,
        model="linear",
        labels=labels,
        x=[float(v) for v in x],
        y=y,
        slope=float(slope),
        intercept=float(intercept),
        r_squared=1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan"),
        percent_deviation_from_fit=[float(100.0 * (yi - yh) / yh) if yh != 0 else float("nan")
                                    for yi, yh in zip(y, yhat)],
        fraction_of_max=[v / top if top > 0 else float("nan") for v in y],
    )


def run_linearity(analysis_dir: str | Path, output_dir: str | Path, x_name: str, y_name: str) -> ResponseFit:
    points = select_points(collect_summaries(analysis_dir), x_name, y_name)
    if x_name == "blue_code":
        fit = fit_gamma(points, y_name)
    elif x_name == "duty":
        fit = fit_duty(points, y_name)
    else:
        fit = brightness_steps(points, y_name)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"linearity_{x_name}.json").write_text(json.dumps(asdict(fit), indent=2) + "\n", encoding="utf-8")
    png = out / f"linearity_{x_name}.png"
    if fit.model == "power_law":
        plot_gamma(png, np.array(fit.x), np.array(fit.y), ylabel=y_name,
                   gamma=fit.gamma, amplitude_at_max=fit.amplitude_at_max, code_max=CODE_MAX)
    elif fit.model == "linear":
        plot_duty(png, np.array(fit.x), np.array(fit.y), ylabel=y_name,
                  slope=fit.slope, intercept=fit.intercept)
    else:
        plot_brightness_steps(png, np.array(fit.x), np.array(fit.y), ylabel=y_name,
                              ratios=[s["ratio"] for s in fit.steps])
    return fit
