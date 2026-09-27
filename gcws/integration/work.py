"""Working representation of peaks while the integrator builds them."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from gcws.core.model import Baseline


@dataclass
class WorkSignal:
    """Signal plus its smoothed version and derivatives (all per point)."""
    rt: np.ndarray
    y: np.ndarray
    ys: np.ndarray
    d1: np.ndarray
    d2: np.ndarray
    env: Optional[np.ndarray] = None

    @property
    def n(self) -> int:
        return int(self.rt.size)

    def idx(self, t: float) -> int:
        j = int(np.searchsorted(self.rt, t))
        if j <= 0:
            return 0
        if j >= self.n:
            return self.n - 1
        return j if abs(self.rt[j] - t) < abs(self.rt[j - 1] - t) else j - 1

    def at(self, t: float, smoothed: bool = True) -> float:
        return float(np.interp(t, self.rt, self.ys if smoothed else self.y))


@dataclass
class WP:
    """A peak under construction (times in minutes)."""
    t0: float
    t1: float
    ta: float
    base: Baseline
    ts: str = "B"
    te: str = "B"
    flags: str = ""
    origin: str = "auto"
    negative: bool = False
    cluster: int = 0
    children: list["WP"] = field(default_factory=list)   # skimmed riders
    parent: Optional["WP"] = None
    manual: bool = False
    # measured
    area_raw: float = 0.0
    height: float = 0.0
    # Deconvolution allocates the measured parent area by MS component weights.
    # These values are rebuilt from manual events, never stored as cached results.
    allocated_area_raw: Optional[float] = None
    area_allocation: Optional[tuple] = None  # (parent raw area, MS weights, component index)
    deconv_component: dict = field(default_factory=dict)
    spectrum_id: str = ""

    def add_flag(self, f: str) -> None:
        if f not in self.flags:
            self.flags += f

    @property
    def region_end(self) -> float:
        """End of the region the peak's own baseline spans (incl. riders)."""
        return max([self.t1] + [c.t1 for c in self.children])

    @property
    def region_start(self) -> float:
        return min([self.t0] + [c.t0 for c in self.children])


def line_between(sig: WorkSignal, t0: float, t1: float, y0=None, y1=None,
                 smoothed: bool = True) -> Baseline:
    return Baseline("line", t0, sig.at(t0, smoothed) if y0 is None else float(y0),
                    t1, sig.at(t1, smoothed) if y1 is None else float(y1))


def apex_of(sig: WorkSignal, t0: float, t1: float, base: Baseline, negative=False) -> float:
    a, b = sig.idx(t0), sig.idx(t1)
    if b <= a:
        return (t0 + t1) / 2
    seg = sig.ys[a:b + 1] - base.eval(sig.rt[a:b + 1])
    if negative:
        seg = -seg
    k = int(np.argmax(seg))
    i = a + k
    if 0 < k < seg.size - 1:
        c0, c1, c2 = seg[k - 1], seg[k], seg[k + 1]
        den = c0 - 2 * c1 + c2
        if den < 0:
            frac = 0.5 * (c0 - c2) / den
            return float(np.interp(i + frac, np.arange(sig.n), sig.rt))
    return float(sig.rt[i])
