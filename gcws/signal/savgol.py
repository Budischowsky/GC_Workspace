"""Savitzky-Golay smoothing and derivatives (numpy only).

Coefficients from the least-squares polynomial fit over a centred window;
edges are handled by odd (point-symmetric) reflection, which keeps a sloping
baseline straight instead of bending it towards zero.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np


def odd(n: float, lo: int = 3) -> int:
    n = max(lo, int(round(n)))
    return n if n % 2 else n + 1


@lru_cache(maxsize=64)
def coeffs(window: int, order: int, deriv: int = 0) -> np.ndarray:
    window = odd(window)
    order = min(order, window - 1)
    half = window // 2
    x = np.arange(-half, half + 1, dtype=float)
    A = np.vander(x, order + 1, increasing=True)
    pinv = np.linalg.pinv(A)
    fact = float(np.prod(np.arange(1, deriv + 1))) if deriv else 1.0
    return pinv[deriv] * fact


def _pad(y: np.ndarray, half: int) -> np.ndarray:
    if y.size < 2 or half == 0:
        return np.pad(y, half, mode="edge")
    half_eff = min(half, y.size - 1)
    left = 2 * y[0] - y[half_eff:0:-1]
    right = 2 * y[-1] - y[-2:-half_eff - 2:-1]
    out = np.concatenate([left, y, right])
    if half_eff < half:
        out = np.pad(out, half - half_eff, mode="edge")
    return out


def filt(y: np.ndarray, window: int, order: int = 2, deriv: int = 0) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    window = odd(window)
    if y.size < 3:
        return y.copy() if deriv == 0 else np.zeros_like(y)
    c = coeffs(window, order, deriv)
    half = window // 2
    ypad = _pad(y, half)
    # correlate: coefficient k applies to offset k - half
    return np.convolve(ypad, c[::-1], mode="valid")


def smooth(y, window: int, order: int = 2) -> np.ndarray:
    return filt(y, window, order, 0)


def derivative(y, window: int, order: int = 2, deriv: int = 1, dt=1.0) -> np.ndarray:
    """Derivative per unit of ``dt`` (scalar or per-point array)."""
    d = filt(y, window, max(order, deriv + 1), deriv)
    return d / (np.asarray(dt, dtype=float) ** deriv)
