"""Baseline envelope estimation (SNIP peak clipping).

Used by the integrator for baseline tracking: a peak may only end where the
signal has returned close to the baseline envelope. Where the signal flattens
on an elevated hump of unresolved material the peak continues as a valley
chain -- the behaviour of the ChemStation integrator, which only sets a
baseline point on a genuine baseline stretch.

SNIP: Ryan et al., Nucl. Instr. Meth. B34 (1988) 396; Morhac et al., NIM A401
(1997) 113. Iteratively ``y[i] = min(y[i], (y[i-k] + y[i+k]) / 2)`` for
increasing k clips everything narrower than the final window.
"""
from __future__ import annotations

import numpy as np


def snip(y: np.ndarray, half_window: int, decreasing: bool = True) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    n = y.size
    m = int(max(1, min(half_window, (n - 1) // 2)))
    # LLS transform compresses dynamic range so large and small peaks clip alike
    offset = y.min()
    v = np.log(np.log(np.sqrt(y - offset + 1.0) + 1.0) + 1.0)
    ks = range(m, 0, -1) if decreasing else range(1, m + 1)
    for k in ks:
        a = v[:-2 * k] if k else v
        b = v[2 * k:]
        mid = v[k:n - k]
        np.minimum(mid, 0.5 * (a + b), out=mid)
    out = (np.exp(np.exp(v) - 1.0) - 1.0) ** 2 - 1.0 + offset
    return out


def envelope(rt: np.ndarray, y: np.ndarray, window_min: float, max_points: int = 4000) -> np.ndarray:
    """SNIP baseline with a clipping window of ``window_min`` minutes.

    Long signals are decimated (block minimum) before clipping and the result
    interpolated back, which keeps the cost independent of the sampling rate.
    """
    rt = np.asarray(rt, float)
    y = np.asarray(y, float)
    n = y.size
    if n < 5:
        return y.copy()
    step = max(1, int(np.ceil(n / max_points)))
    if step > 1:
        m = n // step * step
        yb = y[:m].reshape(-1, step).min(axis=1)
        tb = rt[:m].reshape(-1, step).mean(axis=1)
        if m < n:
            yb = np.append(yb, y[m:].min())
            tb = np.append(tb, rt[m:].mean())
    else:
        yb, tb = y, rt
    dt = float(np.median(np.diff(tb))) if tb.size > 1 else 1.0
    half = int(round(window_min / dt / 2))
    env = snip(yb, half)
    return np.minimum(np.interp(rt, tb, env), y)
