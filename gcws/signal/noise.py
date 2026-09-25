"""Baseline noise estimates used by the integrator and for S/N."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class NoiseInfo:
    sigma: float          # robust standard deviation of the signal noise
    pp: float             # peak-to-peak in the quietest window
    sigma_d1: float       # robust sd of the first difference / sqrt(2)
    t0: float = 0.0
    t1: float = 0.0


def mad_sigma(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def estimate(rt: np.ndarray, y: np.ndarray, t_from: float | None = None,
             window_min: float = 0.5) -> NoiseInfo:
    """Noise from the quietest window after ``t_from``.

    Windows of ``window_min`` are detrended (least-squares line) and the one
    with the lowest residual peak-to-peak is taken, as in the ASTM/Ph. Eur.
    practice of measuring noise on a flat, peak-free stretch.
    """
    rt = np.asarray(rt, float)
    y = np.asarray(y, float)
    mask = rt >= t_from if t_from is not None else np.ones(rt.size, bool)
    if mask.sum() < 20:
        mask = np.ones(rt.size, bool)
    idx = np.flatnonzero(mask)
    r, v = rt[idx], y[idx]
    step = np.median(np.diff(r)) if r.size > 1 else 1.0
    n = max(10, int(round(window_min / step)))
    best = None
    for s in range(0, max(1, v.size - n), max(1, n // 2)):
        seg = v[s:s + n]
        if seg.size < 10:
            continue
        x = np.arange(seg.size)
        coef = np.polyfit(x, seg, 1)
        res = seg - np.polyval(coef, x)
        pp = float(res.max() - res.min())
        if best is None or pp < best[0]:
            best = (pp, float(np.std(res)), s)
    d1 = np.diff(v)
    sigma_d1 = mad_sigma(d1) / np.sqrt(2.0)
    if best is None:
        s = mad_sigma(v)
        return NoiseInfo(sigma=s, pp=6 * s, sigma_d1=sigma_d1)
    pp, sd, s = best
    return NoiseInfo(sigma=max(sd, 1e-12), pp=max(pp, 1e-12), sigma_d1=max(sigma_d1, 1e-12),
                     t0=float(r[s]), t1=float(r[min(s + n, r.size - 1)]))


def signal_to_noise(height: float, noise: NoiseInfo) -> float:
    """Ph. Eur. 2.2.46: S/N = 2H / h (h = peak-to-peak noise)."""
    return 2.0 * height / noise.pp if noise.pp > 0 else float("inf")
