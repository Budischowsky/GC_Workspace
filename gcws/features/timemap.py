"""Retention-time map of one determination onto the reference determination.

Two injections of the same sample drift by a few hundredths of a minute, and
not always by the same amount over the whole run. A single tolerance around the
raw retention time then either misses pairs late in the run or pairs neighbours
early in it. The map removes the drift first:

* :func:`global_shift` -- the dominant offset between the largest peaks of
  both runs (weighted histogram of all pairwise differences within
  ``max_shift``);
* :func:`build_map` -- a monotone, piecewise-linear map through reliable
  anchor pairs (unambiguous pairs with very similar spectra). Anchors that
  disagree with their neighbours, lie closer than ``min_gap`` or would bend
  the map beyond the slope limits are left out; outside the anchors the
  nearest anchor's offset holds.

Ported from the analyst's own ``GC_Peak_Averaging`` (``_global_shift``,
``_alignment_map``, ``TimeMap``). mzmine's gap filler uses a cubic polynomial
fitted on the aligned rows instead; the piecewise-linear map keeps local drift
without overshooting between sparse anchors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Iterable, Optional

import numpy as np


@dataclass(frozen=True)
class TimeMap:
    """Run time -> reference time. ``shift`` is run minus reference, used without anchors."""

    source: tuple[float, ...] = ()        # anchor times in the run
    target: tuple[float, ...] = ()        # the same anchors in the reference
    shift: float = 0.0

    def to_ref(self, t):
        x = np.asarray(t, dtype=float)
        if not self.source:
            y = x - self.shift
        elif len(self.source) == 1:
            y = x + self.target[0] - self.source[0]
        else:
            y = np.interp(x, self.source, self.target)
            y = np.where(x < self.source[0], x + self.target[0] - self.source[0], y)
            y = np.where(x > self.source[-1], x + self.target[-1] - self.source[-1], y)
        return float(y) if np.ndim(y) == 0 else y

    def from_ref(self, t):
        x = np.asarray(t, dtype=float)
        if not self.source:
            y = x + self.shift
        elif len(self.source) == 1:
            y = x + self.source[0] - self.target[0]
        else:
            y = np.interp(x, self.target, self.source)
            y = np.where(x < self.target[0], x + self.source[0] - self.target[0], y)
            y = np.where(x > self.target[-1], x + self.source[-1] - self.target[-1], y)
        return float(y) if np.ndim(y) == 0 else y

    def offset(self, t: float) -> float:
        """Run minus reference at run time ``t``."""
        return float(t) - float(self.to_ref(t))

    @property
    def n_anchors(self) -> int:
        return len(self.source)

    def to_dict(self) -> dict:
        return {"source": list(self.source), "target": list(self.target), "shift": self.shift}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "TimeMap":
        d = d or {}
        return cls(tuple(float(x) for x in d.get("source", ())), tuple(float(x) for x in d.get("target", ())),
                   float(d.get("shift", 0.0)))


IDENTITY = TimeMap()


@dataclass(frozen=True)
class Anchor:
    run_rt: float
    ref_rt: float
    area: float = 1.0


@dataclass
class ShiftEstimate:
    shift: float
    support: int                   # pairs that agree with it
    method: str = "histogram"
    notes: list = field(default_factory=list)


def global_shift(ref_peaks: Iterable[tuple[float, float]], run_peaks: Iterable[tuple[float, float]],
                 max_shift: float = 0.2, top: int = 80, bin_width: float = 0.01) -> ShiftEstimate:
    """The dominant ``run - reference`` offset between the ``top`` largest peaks ``(rt, area)`` of
    both runs: a histogram of all pairwise differences within ``max_shift``, weighted by the
    geometric mean of the relative areas, smoothed, and refined by the weighted mean around its
    maximum."""
    a = sorted((p for p in ref_peaks if p[1] > 0), key=lambda p: -p[1])[:top]
    b = sorted((p for p in run_peaks if p[1] > 0), key=lambda p: -p[1])[:top]
    if not a or not b or max_shift <= 0:
        return ShiftEstimate(0.0, 0, "none")
    max_a, max_b = a[0][1], b[0][1]
    diffs, weights = [], []
    for ra, aa in a:
        for rb, ab in b:
            d = rb - ra
            if abs(d) <= max_shift:
                diffs.append(d)
                weights.append(np.sqrt((aa / max_a) * (ab / max_b)))
    if not diffs:
        return ShiftEstimate(0.0, 0, "none")
    edges = np.arange(-max_shift - bin_width, max_shift + 2 * bin_width, bin_width)
    hist, _ = np.histogram(diffs, bins=edges, weights=weights)
    smoothed = np.convolve(hist, [1, 2, 3, 2, 1], mode="full")[2:2 + hist.size]
    k = int(np.argmax(smoothed))
    center = (edges[k] + edges[k + 1]) / 2
    near = [i for i, d in enumerate(diffs) if abs(d - center) <= 2.5 * bin_width]
    shift = float(np.average([diffs[i] for i in near], weights=[weights[i] for i in near]))
    return ShiftEstimate(round(shift, 5), len(near))


def build_map(anchors: Iterable[Anchor], shift: float, *, max_local: float = 0.15, residual: float = 0.008,
              neighbour_window: float = 1.5, min_gap: float = 0.5,
              slope: tuple[float, float] = (0.9, 1.1)) -> TimeMap:
    """A monotone map through the reliable ``anchors`` (``shift`` = run minus reference)."""
    items = sorted(anchors, key=lambda a: a.run_rt)
    times = [a.run_rt for a in items]
    offsets = [a.run_rt - a.ref_rt for a in items]
    finite_times = all(isfinite(t) for t in times)
    candidates = []
    left = right = 0
    no_neighbours = isinstance(neighbour_window, (float, np.floating)) and bool(np.isnan(neighbour_window))
    for i, a in enumerate(items):
        src, d = times[i], offsets[i]
        if abs(d - shift) > max_local:
            continue
        if finite_times:
            if not no_neighbours:
                while left < len(items) and src - times[left] > neighbour_window:
                    left += 1
                right = max(right, i)
                while right < len(items) and times[right] - src <= neighbour_window:
                    right += 1
            near = [] if no_neighbours else offsets[left:right]
        else:
            # NaN and infinities do not have the ordered-window comparisons used above.
            near = [b.run_rt - b.ref_rt for b in items if abs(b.run_rt - a.run_rt) <= neighbour_window]
        local = float(np.median(near)) if len(near) >= 3 else shift
        if abs(d - local) > residual:
            continue
        candidates.append((src, a.ref_rt))
    chosen: list[tuple[float, float]] = []
    for src, tgt in candidates:
        if chosen and src - chosen[-1][0] < min_gap:
            continue
        if chosen:
            s = (tgt - chosen[-1][1]) / (src - chosen[-1][0])
            if not slope[0] <= s <= slope[1]:
                continue
        chosen.append((src, tgt))
    if not chosen:
        return TimeMap((), (), float(shift))
    return TimeMap(tuple(s for s, _ in chosen), tuple(t for _, t in chosen), float(shift))
