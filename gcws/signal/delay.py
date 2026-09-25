"""FID-to-MS retention time offset from the raw signals.

In a split FID/MS setup the same compound reaches the two detectors at
slightly different times. Convention (as in AutoLib): ``delay = FID_rt -
MS_rt``; an MS time is ``fid_rt - delay``.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from gcws.signal import savgol

DEFAULT_DELAY = 0.006
MAX_LAG_MIN = 0.10
GRID_STEP = 0.001


@dataclass
class DelayEstimate:
    value: float
    quality: float
    method: str

    @property
    def reliable(self) -> bool:
        return self.quality >= 0.5


def _prep(t_grid, rt, y):
    v = np.interp(t_grid, rt, y)
    # remove slow baseline: running minimum over ~1 min, then smooth it
    n = max(3, int(round(1.0 / GRID_STEP)))
    pad = np.pad(v, n // 2, mode="edge")
    try:
        from numpy.lib.stride_tricks import sliding_window_view
        base = sliding_window_view(pad, n).min(axis=1)[: v.size]
    except Exception:  # noqa: BLE001
        base = np.full_like(v, v.min())
    base = savgol.smooth(base, n | 1, 1)
    v = np.sqrt(np.clip(v - base, 0, None))
    sd = v.std()
    return (v - v.mean()) / sd if sd > 0 else v * 0


def estimate_delay(fid, tic, solvent_end: float = 5.5) -> DelayEstimate:
    """Cross-correlation of FID and TIC after ``solvent_end``."""
    if fid is None or tic is None or fid.n < 10 or tic.n < 10:
        return DelayEstimate(DEFAULT_DELAY, 0.0, "default")
    lo = max(solvent_end, float(fid.rt[0]), float(tic.rt[0])) + 0.1
    hi = min(float(fid.rt[-1]), float(tic.rt[-1])) - 0.1
    if hi - lo < 1.0:
        return DelayEstimate(DEFAULT_DELAY, 0.0, "default")
    grid = np.arange(lo, hi, GRID_STEP)
    a = _prep(grid, fid.rt, fid.y)
    b = _prep(grid, tic.rt, tic.y)
    max_lag = int(round(MAX_LAG_MIN / GRID_STEP))
    lags = np.arange(-max_lag, max_lag + 1)
    n = grid.size
    corr = np.empty(lags.size)
    for i, L in enumerate(lags):
        # FID(t) ~ TIC(t - delay): compare a[k] with b[k - L]
        if L >= 0:
            x, y = a[L:], b[: n - L]
        else:
            x, y = a[: n + L], b[-L:]
        corr[i] = float(np.dot(x, y) / max(1, x.size))
    j = int(np.argmax(corr))
    shift = float(lags[j])
    if 0 < j < lags.size - 1:
        c0, c1, c2 = corr[j - 1], corr[j], corr[j + 1]
        den = c0 - 2 * c1 + c2
        if den < 0:
            shift += 0.5 * (c0 - c2) / den
    quality = float(corr[j])
    value = shift * GRID_STEP
    if quality < 0.5 or abs(value) >= MAX_LAG_MIN * 0.95:
        return DelayEstimate(DEFAULT_DELAY, quality, "default (low correlation)")
    return DelayEstimate(round(value, 5), quality, "cross-correlation")


def refine_with_peaks(estimate: DelayEstimate, fid_apexes, ms_apexes,
                      window: float = 0.03) -> DelayEstimate:
    """Median offset of mutually nearest FID/MS apex pairs (AutoLib rule)."""
    f = np.sort(np.asarray(fid_apexes, float))
    m = np.sort(np.asarray(ms_apexes, float))
    if f.size < 3 or m.size < 3:
        return estimate
    diffs = []
    for t in f:
        j = int(np.argmin(np.abs(m - (t - estimate.value))))
        back = int(np.argmin(np.abs(f - (m[j] + estimate.value))))
        if f[back] == t and abs(t - m[j] - estimate.value) <= window:
            diffs.append(t - m[j])
    if len(diffs) < 3:
        return estimate
    d = np.asarray(diffs)
    med = float(np.median(d))
    mad = float(np.median(np.abs(d - med))) or 1e-6
    d = d[np.abs(d - med) <= 3 * 1.4826 * mad]
    value = float(np.median(d))
    if abs(value - estimate.value) <= 0.003 or not estimate.reliable:
        return DelayEstimate(round(value, 5), max(estimate.quality, 0.5), "apex pairs")
    return estimate
