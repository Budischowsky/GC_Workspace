"""Peak measurement: area, height, widths, symmetry, S/N."""
from __future__ import annotations

import numpy as np

from gcws.core.model import Baseline
from gcws.integration.work import WorkSignal


def profile(rt: np.ndarray, y: np.ndarray, t0: float, t1: float) -> tuple[np.ndarray, np.ndarray]:
    """Samples strictly inside (t0, t1) plus interpolated end points."""
    if t1 < t0:
        t0, t1 = t1, t0
    a = int(np.searchsorted(rt, t0, side="right"))
    b = int(np.searchsorted(rt, t1, side="left"))
    t = np.concatenate([[t0], rt[a:b], [t1]])
    v = np.concatenate([[np.interp(t0, rt, y)], y[a:b], [np.interp(t1, rt, y)]])
    return t, v


def raw_area(sig: WorkSignal, t0: float, t1: float, base: Baseline, negative=False) -> float:
    """Area in signal units x seconds (trapezoid over raw data)."""
    t, v = profile(sig.rt, sig.y, t0, t1)
    d = v - base.eval(t)
    if negative:
        d = -d
    return float(np.trapezoid(d, t * 60.0))


def _crossing(t, d, level, i_apex, direction):
    """Time where ``d`` crosses ``level`` walking from the apex."""
    i = i_apex
    if direction < 0:
        while i > 0 and d[i] > level:
            i -= 1
        if d[i] > level:
            return None
        return float(np.interp(level, [d[i], d[i + 1]], [t[i], t[i + 1]]))
    while i < d.size - 1 and d[i] > level:
        i += 1
    if d[i] > level:
        return None
    return float(np.interp(level, [d[i], d[i - 1]], [t[i], t[i - 1]]))


def shape(sig: WorkSignal, t0: float, t1: float, base: Baseline, negative=False) -> dict:
    t, v = profile(sig.rt, sig.y, t0, t1)
    d = v - base.eval(t)
    if negative:
        d = -d
    if d.size < 3:
        return {"height": float(d.max()) if d.size else 0.0, "width50": 0.0, "width5": 0.0,
                "symmetry": None, "asymmetry": None}
    k = int(np.argmax(d))
    h = float(d[k])
    out = {"height": h, "width50": 0.0, "width5": 0.0, "symmetry": None, "asymmetry": None}
    if h <= 0:
        return out
    ta = float(t[k])
    l50, r50 = _crossing(t, d, 0.5 * h, k, -1), _crossing(t, d, 0.5 * h, k, +1)
    if l50 is not None and r50 is not None:
        out["width50"] = r50 - l50
    l5, r5 = _crossing(t, d, 0.05 * h, k, -1), _crossing(t, d, 0.05 * h, k, +1)
    if l5 is not None and r5 is not None:
        out["width5"] = r5 - l5
        f = ta - l5
        if f > 0:
            out["symmetry"] = (r5 - l5) / (2 * f)       # USP tailing factor
    l10, r10 = _crossing(t, d, 0.10 * h, k, -1), _crossing(t, d, 0.10 * h, k, +1)
    if l10 is not None and r10 is not None and ta - l10 > 0:
        out["asymmetry"] = (r10 - ta) / (ta - l10)      # EP asymmetry at 10 %
    return out
