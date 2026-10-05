"""Spectrum and linearity plots (Agg backend)."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def plot_spectrum(path: Path, wl: np.ndarray, e: np.ndarray, *, title: str = "Spectral irradiance") -> None:
    plt = _plt()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(wl, e, color="0.1", lw=1.4)
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel(r"Spectral irradiance (W m$^{-2}$ nm$^{-1}$)")
    ax.set_title(title)
    ax.set_xlim(380, 780)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_normalized_spectrum(path: Path, wl: np.ndarray, e: np.ndarray, *, title: str = "Normalized spectrum") -> None:
    y = np.maximum(0.0, np.asarray(e, dtype=float))
    peak = float(np.max(y)) if y.size else 0.0
    plt = _plt()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(wl, y / peak if peak > 0 else y, color="0.1", lw=1.4)
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Normalized irradiance (peak = 1)")
    ax.set_title(title)
    ax.set_xlim(380, 780)
    ax.set_ylim(0, 1.05)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_gamma(
    path: Path,
    x: np.ndarray,
    y: np.ndarray,
    *,
    ylabel: str,
    gamma: float,
    amplitude_at_max: float,
    code_max: float,
) -> None:
    plt = _plt()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    ax.loglog(x, y, "o", color="0.1", label="measured")
    xx = np.geomspace(float(np.min(x)), float(np.max(x)), 50)
    ax.loglog(xx, amplitude_at_max * (xx / code_max) ** gamma, "-", color="0.45",
              label=f"power law, gamma = {gamma:.3f}")
    ax.set_xlabel("Blue code B")
    ax.set_ylabel(ylabel)
    ax.set_title("UPRtek display response (blue code)")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_duty(
    path: Path,
    x: np.ndarray,
    y: np.ndarray,
    *,
    ylabel: str,
    slope: float,
    intercept: float,
) -> None:
    plt = _plt()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    ax.plot(x, y, "o", color="0.1", label="measured")
    xx = np.linspace(0.0, 100.0, 50)
    ax.plot(xx, slope * xx + intercept, "-", color="0.45", label="least-squares line")
    ax.set_xlabel("Duty cycle (%)")
    ax.set_ylabel(ylabel)
    ax.set_title("UPRtek display response (duty cycle)")
    ax.set_xlim(0, 100)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_brightness_steps(
    path: Path,
    x: np.ndarray,
    y: np.ndarray,
    *,
    ylabel: str,
    ratios: list[float],
) -> None:
    plt = _plt()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    ax.semilogy(x, y, "o-", color="0.1", label="measured")
    for i, r in enumerate(ratios):
        ax.annotate(f"×{r:.2f}", ((x[i] + x[i + 1]) / 2, np.sqrt(y[i] * y[i + 1])),
                    textcoords="offset points", xytext=(0, 8), ha="center", fontsize=8, color="0.35")
    ax.set_xlabel("Hardware brightness level")
    ax.set_ylabel(ylabel)
    ax.set_title("UPRtek display response (brightness steps)")
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
