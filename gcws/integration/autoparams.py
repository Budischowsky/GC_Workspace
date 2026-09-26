"""Automatic integration parameters from the signal itself.

Like the auto-parameter wizards of Chromeleon (Cobra) and MassHunter
(Agile2): the baseline noise and the typical peak width are measured and
every threshold is expressed relative to them, so one method works across
runs with different response and noise.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from gcws.integration.method import EventKind, IntegrationMethod
from gcws.signal import noise as N
from gcws.signal import savgol

#: default slope sensitivity in units of the derivative noise
DEFAULT_SLOPE_MULT = 4.0
#: default threshold as S/N (Ph. Eur. 2H/h); 3 is the usual detection limit
DEFAULT_THRESHOLD_SN = 3.0


@dataclass
class Resolved:
    peak_width: float        # min
    window: int              # smoothing window, points
    order: int
    slope_mult: float
    sigma_d1: float          # derivative noise, signal units / min
    threshold: float         # signal units
    noise: N.NoiseInfo
    auto_fields: tuple[str, ...] = ()

    def slope_abs(self, mult=None) -> float:
        return (self.slope_mult if mult is None else mult) * self.sigma_d1


def _integration_start(rt, method: IntegrationMethod, t_min=None) -> float | None:
    """First time the integrator is on (skips the solvent front)."""
    on = True
    start = None
    for e in method.events():
        if e.kind == EventKind.INTEGRATOR_OFF and e.time <= rt[0] + 1e-9:
            on = False
        elif e.kind == EventKind.INTEGRATOR_ON and not on:
            start = e.time
            on = True
    return max(start, t_min) if start is not None and t_min is not None else (start if t_min is None else t_min)


def measure_width(rt, y, noise_sigma, t_from=None) -> float:
    """Median FWHM (min) of the strongest well-defined peaks."""
    ys = savgol.smooth(y, 5, 2)
    mask = np.ones(rt.size, bool) if t_from is None else rt >= t_from
    i = np.flatnonzero(mask)
    if i.size < 20:
        return float(np.median(np.diff(rt))) * 10
    lo = i[0]
    seg = ys[lo:]
    # local maxima
    mx = np.flatnonzero((seg[1:-1] > seg[:-2]) & (seg[1:-1] >= seg[2:])) + 1
    if mx.size == 0:
        return float(np.median(np.diff(rt))) * 10
    step = float(np.median(np.diff(rt)))
    win = max(10, int(round(0.3 / step)))
    cands = []
    for m in mx:
        a, b = max(0, m - win), min(seg.size, m + win + 1)
        base = max(seg[a:m + 1].min(), seg[m:b].min())
        h = seg[m] - base
        if h < 20 * noise_sigma:
            continue
        half = base + h / 2
        left = m
        while left > a and seg[left] > half:
            left -= 1
        right = m
        while right < b - 1 and seg[right] > half:
            right += 1
        if seg[left] > half or seg[right] > half:
            continue
        # interpolate crossings
        tl = np.interp(half, [seg[left], seg[left + 1]], [left, left + 1])
        tr = np.interp(half, [seg[right], seg[right - 1]], [right, right - 1])
        w = (tr - tl) * step
        if w > 0:
            cands.append((h, w))
    if not cands:
        return step * 10
    cands.sort(reverse=True)
    top = cands[: max(3, len(cands) // 5)]
    return float(np.median([w for _, w in top]))


def resolve(rt: np.ndarray, y: np.ndarray, method: IntegrationMethod, t_min=None) -> Resolved:
    t_from = _integration_start(rt, method, t_min)
    # A cut near/past the run end must not fall back to estimating solvent noise.
    keep = rt >= t_min if t_min is not None else np.ones(rt.size, bool)
    noise = N.estimate(rt[keep], y[keep], t_from)
    auto = []
    step = float(np.median(np.diff(rt))) if rt.size > 1 else 1.0
    pw = method.peak_width
    if not pw:
        pw = measure_width(rt, y, noise.sigma, t_from)
        auto.append("peak_width")
    pw_pts = pw / step
    window = method.smoothing_window
    if not window:
        window = savgol.odd(min(101, max(5, pw_pts * 0.6)))
        auto.append("smoothing_window")
    order = method.smoothing_order
    d1 = savgol.derivative(y, window, order, 1, step)
    # derivative noise measured where the baseline is quietest (the window the
    # noise estimate found); a MAD over the whole run would be inflated by the
    # peaks of a dense chromatogram
    quiet = (rt >= noise.t0) & (rt <= noise.t1)
    if quiet.sum() >= 10:
        sigma_d1 = float(np.std(d1[quiet]))
    else:
        mask = np.ones(rt.size, bool) if t_from is None else rt >= t_from
        sigma_d1 = N.mad_sigma(d1[mask])
    sigma_d1 = sigma_d1 or 1e-12
    slope = method.slope_sensitivity
    if not slope:
        slope = DEFAULT_SLOPE_MULT
        auto.append("slope_sensitivity")
    thr = method.threshold
    if thr is None:
        thr = DEFAULT_THRESHOLD_SN * noise.pp / 2.0
        auto.append("threshold")
    return Resolved(peak_width=float(pw), window=int(window), order=order,
                    slope_mult=float(slope), sigma_d1=float(sigma_d1), threshold=float(thr),
                    noise=noise, auto_fields=tuple(auto))
