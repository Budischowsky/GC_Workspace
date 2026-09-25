"""Derivative-based peak detection (the integrator's state machine).

Classic slope detection: a peak starts when the smoothed first derivative
exceeds the slope sensitivity for a few consecutive points, the apex is the
sign change of the derivative, a valley is a renewed rise before the signal
has levelled off, and the peak ends when the derivative stays inside the
slope band. Runs in index space on plain Python lists (fast enough for
~50k points; ~20 ms).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Seg:
    i0: int
    ia: int
    i1: int
    ts: str = "B"
    te: str = "B"


def detect(ys: np.ndarray, d1: np.ndarray, slope: np.ndarray, on: np.ndarray,
           n_up: np.ndarray, n_dn: np.ndarray, at_base: np.ndarray | None = None) -> list[list[Seg]]:
    """Clusters of peaks; each cluster is a list of segments joined by valleys.

    ``at_base`` (optional) marks points close to the tracked baseline; a peak
    may only end there. Elsewhere a flat stretch is part of a valley chain.
    """
    ysl = ys.tolist()
    d = d1.tolist()
    s_ = slope.tolist()
    on_ = on.tolist()
    nu = n_up.tolist()
    nd = n_dn.tolist()
    ab = at_base.tolist() if at_base is not None else [True] * len(ysl)
    n = len(ysl)
    clusters: list[list[Seg]] = []
    cur: list[Seg] = []
    state = 0          # 0 baseline, 1 rising, 2 falling
    up = dn = flat = 0
    start = apex = 0
    last_end = 0

    def close_cluster():
        nonlocal cur
        if cur:
            clusters.append(cur)
        cur = []

    i = 0
    while i < n:
        if not on_[i]:
            if state:
                end = i
                a = max(range(start, end + 1), key=ysl.__getitem__) if state == 1 else apex
                cur.append(Seg(start, a, max(end, a + 1), cur and "V" or "B", "B"))
                if len(cur) > 1:
                    cur[-1].ts = "V"
                close_cluster()
                last_end = end
                state = 0
            up = 0
            i += 1
            continue
        s = s_[i]
        di = d[i]
        if state == 0:
            up = up + 1 if di > s else 0
            if up >= nu[i]:
                st = i - up + 1
                # walk back to where the rise began: the last point before the
                # signal started to climb (bounded by the previous peak end)
                floor = max(last_end, st - 4 * nd[i] - 4 * nu[i])
                while st > floor and st > 0 and d[st - 1] > 0 and on_[st - 1]:
                    st -= 1
                start = st
                state = 1
                up = 0
        elif state == 1:
            if di <= 0:
                apex = max(range(start, i + 1), key=ysl.__getitem__)
                state = 2
                dn = flat = 0
        else:  # falling
            dn = dn + 1 if di > s else 0
            if dn >= nu[i]:
                seg_lo = apex
                seg_hi = i - dn + 1
                valley = min(range(seg_lo, max(seg_hi, seg_lo + 1) + 1), key=ysl.__getitem__)
                ts = "V" if cur else "B"
                cur.append(Seg(start, apex, valley, ts, "V"))
                start = valley
                state = 1
                dn = 0
                i += 1
                continue
            flat = flat + 1 if abs(di) <= s else 0
            if flat >= nd[i] and ab[i]:
                end = max(i - nd[i] + 1, i - flat + 1)
                ts = "V" if cur else "B"
                cur.append(Seg(start, apex, max(end, apex + 1), ts, "B"))
                close_cluster()
                last_end = end
                state = 0
                flat = 0
        i += 1
    if state:
        end = n - 1
        a = apex if state == 2 else max(range(start, n), key=ysl.__getitem__)
        cur.append(Seg(start, a, max(end, a), "V" if cur else "B", "B"))
    close_cluster()
    return clusters
