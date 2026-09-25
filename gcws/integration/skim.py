"""Tangent and exponential skimming of rider peaks.

Criteria as in OpenLab/ChemStation: a child peak on the tail (front) of a
larger parent is skimmed when parent/child height >= tail (front) skim
height ratio and the child's height above the valley divided by the valley's
height above the baseline is below the skim valley ratio. A peak on the tail
of a solvent peak is always skimmed.
"""
from __future__ import annotations

import numpy as np

from gcws.core.model import Baseline
from gcws.integration.work import WP, WorkSignal


def _height_above(sig: WorkSignal, t: float, base: Baseline) -> float:
    return sig.at(t) - float(base.eval(np.array([t]))[0])


def tangent_tail(sig: WorkSignal, v_t: float, child_apex: float, limit: float) -> tuple[float, Baseline] | None:
    """Tangent from the valley to the signal after the child apex."""
    iv = sig.idx(v_t)
    ia = sig.idx(child_apex)
    il = sig.idx(limit)
    if il <= ia + 1:
        return None
    t = sig.rt[ia + 1:il + 1]
    y = sig.ys[ia + 1:il + 1]
    yv = sig.ys[iv]
    slopes = (y - yv) / np.maximum(t - sig.rt[iv], 1e-9)
    k = int(np.argmin(slopes))
    te = float(t[k])
    return te, Baseline("line", float(sig.rt[iv]), float(yv), te, float(y[k]))


def tangent_front(sig: WorkSignal, v_t: float, child_apex: float, limit: float) -> tuple[float, Baseline] | None:
    iv = sig.idx(v_t)
    ia = sig.idx(child_apex)
    il = sig.idx(limit)
    if ia - 1 <= il:
        return None
    t = sig.rt[il:ia]
    y = sig.ys[il:ia]
    yv = sig.ys[iv]
    slopes = (yv - y) / np.maximum(sig.rt[iv] - t, 1e-9)
    k = int(np.argmax(slopes))
    ts = float(t[k])
    return ts, Baseline("line", ts, float(y[k]), float(sig.rt[iv]), float(yv))


def exponential_tail(sig: WorkSignal, parent: WP, v_t: float, child_apex: float,
                     limit: float, width: float) -> tuple[float, Baseline, float] | None:
    """Exponential decay of the parent's tail continued under the child."""
    base = parent.base
    ia_p = sig.idx(parent.ta + width)
    iv = sig.idx(v_t)
    if iv - ia_p < 4:
        ia_p = sig.idx(parent.ta + 0.5 * width)
    if iv - ia_p < 4:
        return None
    t = sig.rt[ia_p:iv]
    h = sig.ys[ia_p:iv] - base.eval(t)
    ok = h > 0
    if ok.sum() < 4:
        return None
    t, h = t[ok], h[ok]
    A = np.vstack([np.ones_like(t), t - v_t]).T
    coef, *_ = np.linalg.lstsq(A, np.log(h), rcond=None)
    pred = A @ coef
    lh = np.log(h)
    ss = float(((lh - lh.mean()) ** 2).sum()) or 1e-12
    r2 = 1.0 - float(((lh - pred) ** 2).sum()) / ss
    k = -float(coef[1])
    if k <= 0:
        return None
    b = float(base.eval(np.array([v_t]))[0])
    y0 = sig.at(v_t)
    curve = Baseline("exp", float(v_t), y0, float(limit), 0.0, k=k, b=b)
    ic, il = sig.idx(child_apex), sig.idx(limit)
    if il <= ic + 1:
        return None
    tt = sig.rt[ic + 1:il + 1]
    below = np.flatnonzero(sig.ys[ic + 1:il + 1] <= curve.eval(tt))
    if below.size == 0:
        return None
    te = float(tt[below[0]])
    curve.t1 = te
    curve.y1 = float(curve.eval(np.array([te]))[0])
    return te, curve, r2


def should_skim(parent: WP, child: WP, sig: WorkSignal, v_t: float,
                ratio: float, valley_ratio: float) -> bool:
    hp = parent.height
    hc = child.height
    if hc <= 0 or hp <= 0:
        return False
    valley_h = _height_above(sig, v_t, parent.base)
    if valley_h <= 0:
        return False
    above_valley = sig.at(child.ta) - sig.at(v_t)
    if "S" in parent.flags:
        return True
    return hp / hc >= ratio and above_valley / valley_h < valley_ratio
