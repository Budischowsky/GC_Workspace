"""Time alignment of two chromatograms (e.g. a sample and its blank).

``xcorr_shift`` finds the offset ``s`` for which ``a(t) ≈ b(t - s)`` by
cross-correlating baseline-removed, square-root compressed traces on a common
0.001-min grid -- the method of :func:`gcws.signal.delay.estimate_delay`,
with an adjustable search range.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from gcws.signal.delay import GRID_STEP, _prep


@dataclass
class Alignment:
    shift: float            # minutes; blank time t appears in the sample at t + shift
    quality: float          # peak correlation (0..1)
    method: str
    blank: str = ""         # blank run name


def xcorr_shift(rt_a, y_a, rt_b, y_b, lo: float, hi: float, max_shift: float = 0.05) -> tuple[float, float]:
    """``(shift, quality)`` with ``a(t) ≈ b(t - shift)`` inside [lo, hi]."""
    rt_a, y_a, rt_b, y_b = (np.asarray(v, float) for v in (rt_a, y_a, rt_b, y_b))
    lo = max(lo, rt_a[0], rt_b[0]) + max_shift
    hi = min(hi, rt_a[-1], rt_b[-1]) - max_shift
    if hi - lo < 1.0:
        return 0.0, 0.0
    grid = np.arange(lo, hi, GRID_STEP)
    a = _prep(grid, rt_a, y_a)
    b = _prep(grid, rt_b, y_b)
    max_lag = max(1, int(round(max_shift / GRID_STEP)))
    lags = np.arange(-max_lag, max_lag + 1)
    n = grid.size
    corr = np.empty(lags.size)
    for i, lag in enumerate(lags):
        if lag >= 0:
            x, y = a[lag:], b[: n - lag]
        else:
            x, y = a[: n + lag], b[-lag:]
        corr[i] = float(np.dot(x, y) / max(1, x.size))
    j = int(np.argmax(corr))
    shift = float(lags[j])
    if 0 < j < lags.size - 1:
        c0, c1, c2 = corr[j - 1], corr[j], corr[j + 1]
        den = c0 - 2 * c1 + c2
        if den < 0:
            shift += 0.5 * (c0 - c2) / den
    quality = float(corr[j])
    if j in (0, lags.size - 1):              # at the edge of the search range: not trustworthy
        return 0.0, quality
    return round(shift * GRID_STEP, 5), quality


def peak_shift(sample_apexes, blank_apexes, window: float = 0.05, min_pairs: int = 3) -> tuple[float, int] | None:
    """Median offset of mutually nearest apex pairs (sample - blank) within ``window``.

    Robust when the blank has only a few peaks (a solvent blank): its peaks
    usually all occur in the sample too, while a cross-correlation is
    dominated by the sample's many other peaks and the baseline shape.
    Returns ``(shift, pairs)`` or None with fewer than ``min_pairs`` pairs.
    """
    s = np.sort(np.asarray(sample_apexes, float))
    b = np.sort(np.asarray(blank_apexes, float))
    if s.size == 0 or b.size == 0:
        return None
    diffs = []
    for t in b:
        i = int(np.argmin(np.abs(s - t)))
        back = int(np.argmin(np.abs(b - s[i])))
        if b[back] == t and abs(s[i] - t) <= window:
            diffs.append(s[i] - t)
    if len(diffs) < min_pairs:
        return None
    d = np.asarray(diffs)
    med = float(np.median(d))
    mad = float(np.median(np.abs(d - med))) or 1e-6
    d = d[np.abs(d - med) <= 3 * 1.4826 * mad + 1e-9]
    return round(float(np.median(d)), 5), int(d.size)
