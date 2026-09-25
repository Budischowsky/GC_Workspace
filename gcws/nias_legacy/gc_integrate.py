#!/usr/bin/env python3
"""FID integration maths and the editable-integration command history.

Spec v3.0 §VI.5 - §VI.9. Two halves:

* **the maths** -- baseline models, area/height/width over arbitrary bounds,
  the per-peak ratio scaling that keeps ChemStation's numbers intact, split,
  merge and valley snapping;
* **the commands** -- one class per analyst action, each storing the complete
  before state of every row it touched, plus an :class:`IntegrationHistory`
  that replays them.

Per-peak ratio scaling (§VI.5, entschieden)
-------------------------------------------
At load every ChemStation peak stores ``scale = reported_area / area_raw`` over
its *reported* bounds. Afterwards ``area(new bounds) = scale * area_raw(new
bounds)``. Four things follow, and they are why this option was chosen over a
single global factor:

1. an untouched peak reproduces its ``RESULTS.CSV`` area **bit for bit**, so
   today's reports do not move because the workspace gained a chromatogram;
2. a split conserves area exactly (§VI.7);
3. the baseline-model error cancels for the peak it was measured on, so the
   remaining error affects only the *change* the analyst makes;
4. a peak ChemStation never reported has no scale and falls back to
   :data:`AREA_SCALE`.

:data:`AREA_SCALE` is 10.0, measured at 10.005 with a CV of 0.86 % over the 11
clean peaks of the reference sample (§VI.5); everything above that floor is a
baseline-model difference, not a scale error.

Integration convention
----------------------
Areas are trapezoid integrals of ``y - baseline`` over a grid whose **first and
last points are interpolated at exactly the bounds**, not snapped to samples.
That is what makes a split conserve at 1e-16 rather than losing the trapezoid
element that straddles the split point, and it is the rule that reproduces the
§VI.5 calibration table.

The flat ``fid_*`` fields on :class:`gc_model.PeakRow` are the system of record
(§VI.13). :class:`Integration` is a transient adapter, built by
:meth:`Integration.from_row` and written back by :meth:`Integration.apply_to_row`;
there is deliberately no ``row.integration`` attribute.
"""

from __future__ import annotations

import math
from datetime import datetime
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

import gc_ch
import gc_model as M

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Counts*s -> ChemStation area, for peaks the instrument never reported.
#: §VI.5: 10.0051 median, CV 0.86 % over the 11 peaks whose height matches to
#: better than 0.5 %. Only ever used when a row has no ``fid_scale``.
AREA_SCALE = 10.0

#: Two bounds closer than this are the same bound: the shared valley of a
#: VV chain. ``RESULTS.CSV`` prints bounds to 0.001 min, so adjacency is exact
#: there; the tolerance covers bounds that a drag has since moved.
CHAIN_TOL = 1e-6

#: Undo depth per sample (§VI.8).
HISTORY_DEPTH = 100

#: Valley-snap radius in samples (§VI.6: +-3 points, +-0.15 s at 20 Hz).
SNAP_RADIUS = 3

#: A fragment of a deconvolution split needs at least this many real trace
#: samples (§VII.8): with two the trapezoid rule has a single element and the
#: apex, the half width and the height are all the same point, so the numbers
#: would be geometry-free. Fewer means the split point is refused.
MIN_FRAGMENT_SAMPLES = 3

BASELINE_MODES = (M.BASELINE_ENDPOINT, M.BASELINE_CLUSTER,
                  M.BASELINE_MANUAL, M.BASELINE_DROP)


# --------------------------------------------------------------------------
# The transient integration record
# --------------------------------------------------------------------------

@dataclass
class Integration:
    """Bounds and baseline of one peak, borrowed from its row.

    ``area_ref``/``raw_ref`` are an addition to the signature in the agent
    contract, both optional and both defaulted, so every call form in the
    contract still works. They hold the reported area and the raw integral it
    was calibrated against, which is what turns "reproduces its area bit for
    bit" from *almost always* into *always*: ``(a / r) * r`` differs from ``a``
    by one ulp for about 8 % of float64 pairs, so an unmoved bound returns the
    reported number verbatim instead of recomputing it. They round-trip through
    ``row.derived``, and a row without them simply falls back to the product.
    """

    start: float
    end: float
    baseline: str = M.BASELINE_ENDPOINT
    anchor_l: Optional[tuple[float, float]] = None
    anchor_r: Optional[tuple[float, float]] = None
    scale: Optional[float] = None
    pk_ty: str = ""
    origin: str = M.ORIGIN_CHEMSTATION
    area_ref: Optional[float] = None
    raw_ref: Optional[float] = None

    # -- row adapter -------------------------------------------------------

    @classmethod
    def from_row(cls, row) -> "Integration":
        """Build the adapter from a row's ``fid_*`` fields.

        ``start_tm``/``end_tm`` are accepted as a fallback so a session written
        before the FID bounds reached the row still yields an integration
        rather than an exception (ground rule 7: old input must load).
        """
        start = getattr(row, "fid_start", None)
        end = getattr(row, "fid_end", None)
        if start is None:
            start = getattr(row, "start_tm", None)
        if end is None:
            end = getattr(row, "end_tm", None)
        if start is None or end is None:
            raise ValueError(
                f"Peak {getattr(row, 'peak_no', '?')} hat keine "
                f"FID-Integrationsgrenzen.")
        derived = getattr(row, "derived", None) or {}
        return cls(
            start=float(start),
            end=float(end),
            baseline=getattr(row, "fid_baseline", None) or M.BASELINE_ENDPOINT,
            anchor_l=_as_anchor(getattr(row, "fid_anchor_l", None)),
            anchor_r=_as_anchor(getattr(row, "fid_anchor_r", None)),
            scale=_as_float(getattr(row, "fid_scale", None)),
            pk_ty=getattr(row, "fid_pk_ty", "") or getattr(row, "pk_ty", "") or "",
            origin=getattr(row, "integration_origin", None) or M.ORIGIN_CHEMSTATION,
            area_ref=_as_float(derived.get("fid_area_ref")),
            raw_ref=_as_float(derived.get("fid_raw_ref")),
        )

    def apply_to_row(self, row) -> None:
        """Write the adapter back into the row's ``fid_*`` fields."""
        row.fid_start = float(self.start)
        row.fid_end = float(self.end)
        row.fid_baseline = self.baseline
        row.fid_anchor_l = self.anchor_l
        row.fid_anchor_r = self.anchor_r
        row.fid_scale = self.scale
        row.fid_pk_ty = self.pk_ty
        row.integration_origin = self.origin
        if getattr(row, "derived", None) is None:      # pragma: no cover
            row.derived = {}
        row.derived["fid_area_ref"] = self.area_ref
        row.derived["fid_raw_ref"] = self.raw_ref

    # -- convenience -------------------------------------------------------

    @property
    def width(self) -> float:
        return float(self.end) - float(self.start)

    @property
    def effective_scale(self) -> float:
        """The factor actually applied -- the peak's own, or the fallback."""
        return AREA_SCALE if self.scale is None else float(self.scale)


@dataclass
class IntegrationResult:
    """What one integration yields. ``area_raw`` is counts*seconds."""

    area_raw: float
    area: float
    height: float
    apex_rt: float
    apex_index: int
    width_half: float


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(out) else out


def _as_anchor(value: Any) -> Optional[tuple[float, float]]:
    """Accept a tuple, a list (session JSON) or ``None``."""
    if value is None:
        return None
    try:
        rt, y = value
        return (float(rt), float(y))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# The integration grid
# --------------------------------------------------------------------------

def _y_at(trace: gc_ch.Trace, t: float) -> float:
    """Linear interpolation of the trace at an arbitrary retention time."""
    rt, y = trace.rt, trace.y
    if t <= rt[0]:
        return float(y[0])
    if t >= rt[-1]:
        return float(y[-1])
    k = int(np.searchsorted(rt, t, side="right")) - 1
    span = rt[k + 1] - rt[k]
    if span <= 0:                                        # pragma: no cover
        return float(y[k])
    return float(y[k] + (y[k + 1] - y[k]) * (t - rt[k]) / span)


def profile(trace: gc_ch.Trace, start: float,
            end: float) -> tuple[np.ndarray, np.ndarray]:
    """``(t[min], y)`` over ``[start, end]`` with interpolated end points.

    The interior points are the real samples; the two outer points sit exactly
    on the bounds. This is what makes the integral additive over an arbitrary
    split point, which is the whole basis of §VI.7's conservation guarantee.
    """
    rt, y = trace.rt, trace.y
    lo = max(float(start), float(rt[0]))
    hi = min(float(end), float(rt[-1]))
    if not (hi > lo):
        raise ValueError(
            f"Integrationsgrenzen ungültig: {start:.4f} bis {end:.4f} min "
            f"liegt außerhalb des Chromatogramms oder ist leer.")
    i = int(np.searchsorted(rt, lo, side="right"))
    j = int(np.searchsorted(rt, hi, side="left"))
    j = max(i, j)
    n = j - i + 2
    t_out = np.empty(n, dtype=np.float64)
    y_out = np.empty(n, dtype=np.float64)
    t_out[0] = lo
    t_out[-1] = hi
    y_out[0] = _y_at(trace, lo)
    y_out[-1] = _y_at(trace, hi)
    if j > i:
        t_out[1:-1] = rt[i:j]
        y_out[1:-1] = y[i:j]
    return t_out, y_out


def _baseline_on(trace: gc_ch.Trace, integ: Integration,
                 t: np.ndarray, y: np.ndarray,
                 cluster: Optional[tuple[float, float]]) -> np.ndarray:
    """Evaluate the peak's baseline on an existing grid."""
    mode = integ.baseline or M.BASELINE_ENDPOINT
    if mode in (M.BASELINE_MANUAL, M.BASELINE_DROP) \
            and integ.anchor_l and integ.anchor_r:
        (t0, y0), (t1, y1) = integ.anchor_l, integ.anchor_r
    elif mode == M.BASELINE_CLUSTER and cluster:
        t0, t1 = float(cluster[0]), float(cluster[1])
        y0, y1 = _y_at(trace, t0), _y_at(trace, t1)
    else:
        # endpoint, and the fallback for a mode whose anchors are missing:
        # a straight line between the two ends of the peak's own window.
        t0, t1 = float(t[0]), float(t[-1])
        y0, y1 = float(y[0]), float(y[-1])
    if t1 == t0:                                         # pragma: no cover
        return np.full(t.shape, float(y0))
    return y0 + (y1 - y0) * (t - t0) / (t1 - t0)


def baseline_of(trace: gc_ch.Trace, integ: Integration,
                cluster: Optional[tuple[float, float]] = None) -> np.ndarray:
    """The baseline sampled on the peak's own integration grid.

    ``cluster`` is the ``(lo, hi)`` retention time pair of the VV chain the
    peak belongs to, as returned by :func:`cluster_of`; it is only read for
    ``baseline == "cluster"``.
    """
    t, y = profile(trace, integ.start, integ.end)
    return _baseline_on(trace, integ, t, y, cluster)


def _fwhm(t: np.ndarray, corrected: np.ndarray, apex: int) -> float:
    """Full width at half maximum in minutes, 0.0 when the window clips it."""
    half = corrected[apex] / 2.0
    if not (half > 0.0):
        return 0.0

    def crossing(a: int, b: int, step: int) -> Optional[float]:
        i = a
        while i != b:
            nxt = i + step
            if corrected[nxt] <= half:
                lo, hi = (nxt, i) if step < 0 else (i, nxt)
                span = corrected[hi] - corrected[lo]
                if span == 0:                            # pragma: no cover
                    return float(t[nxt])
                f = (half - corrected[lo]) / span
                return float(t[lo] + (t[hi] - t[lo]) * f)
            i = nxt
        return None

    left = crossing(apex, 0, -1)
    right = crossing(apex, len(corrected) - 1, +1)
    if left is None or right is None:
        return 0.0
    return right - left


def integrate(trace: gc_ch.Trace, integ: Integration,
              cluster: Optional[tuple[float, float]] = None) -> IntegrationResult:
    """Area, height, apex and half width of one peak.

    ``area`` applies the per-peak ratio scaling. When the raw integral is bit
    identical to the one the scale was calibrated against, the reported area is
    returned verbatim rather than recomputed -- see :class:`Integration`.
    A ``DISABLED`` peak keeps its real geometry but reports an area of 0, which
    is what "Peak verworfen" means for the report (§VI.8).
    """
    t, y = profile(trace, integ.start, integ.end)
    base = _baseline_on(trace, integ, t, y, cluster)
    corrected = y - base

    area_raw = float(np.trapezoid(corrected, t * 60.0))
    apex = int(np.argmax(corrected))
    height = float(corrected[apex])
    apex_rt = float(t[apex])

    if integ.origin == M.ORIGIN_DISABLED:
        area = 0.0
    elif (integ.area_ref is not None and integ.raw_ref is not None
            and area_raw == integ.raw_ref):
        area = float(integ.area_ref)
    else:
        area = integ.effective_scale * area_raw

    return IntegrationResult(
        area_raw=area_raw,
        area=area,
        height=height,
        apex_rt=apex_rt,
        apex_index=gc_ch.index_of(trace, apex_rt),
        width_half=_fwhm(t, corrected, apex),
    )


def calibrate(reported_area: float, trace: gc_ch.Trace, integ: Integration,
              cluster: Optional[tuple[float, float]] = None) -> float:
    """``reported_area / area_raw`` over the peak's *reported* bounds.

    Falls back to :data:`AREA_SCALE` when the raw integral is zero or the
    reported area is missing, so a degenerate peak cannot produce an infinite
    or negative factor that would poison the sample median.
    """
    bare = replace(integ, scale=None, area_ref=None, raw_ref=None)
    raw = integrate(trace, bare, cluster).area_raw
    value = _as_float(reported_area)
    if value is None or raw == 0.0:
        return AREA_SCALE
    scale = value / raw
    return scale if np.isfinite(scale) else AREA_SCALE


def calibrated(reported_area: float, trace: gc_ch.Trace, integ: Integration,
               cluster: Optional[tuple[float, float]] = None) -> Integration:
    """:func:`calibrate` plus the reference pair, as a new :class:`Integration`.

    The companion to :func:`calibrate`, whose contract signature returns only
    the float. Callers that want the bit-for-bit identity use this one.
    """
    bare = replace(integ, scale=None, area_ref=None, raw_ref=None)
    raw = integrate(trace, bare, cluster).area_raw
    value = _as_float(reported_area)
    if value is None or raw == 0.0:
        return replace(integ, scale=None, area_ref=None, raw_ref=None)
    scale = value / raw
    if not np.isfinite(scale):
        return replace(integ, scale=None, area_ref=None, raw_ref=None)
    return replace(integ, scale=scale, area_ref=value, raw_ref=raw)


# --------------------------------------------------------------------------
# Clusters, split, merge, snapping
# --------------------------------------------------------------------------

def cluster_of(integs: Sequence[Integration],
               i: int) -> Optional[tuple[float, float]]:
    """Outer bounds of the VV chain around ``integs[i]``, or ``None``.

    A chain is a run of peaks whose ``end`` is the next peak's ``start`` -- the
    shared valley of a ``VV``/``PV``/``BV``/``VB`` group. Anchoring one straight
    baseline at the chain's outer bounds reproduces ChemStation markedly better
    than a per-peak endpoint baseline (§VI.5: 20 % CV against 92 %).

    ``integs`` must be in retention-time order; a lone peak returns ``None`` so
    the caller keeps its endpoint baseline.
    """
    n = len(integs)
    if not (0 <= i < n):
        raise IndexError(i)
    lo = i
    while lo > 0 and abs(integs[lo - 1].end - integs[lo].start) <= CHAIN_TOL:
        lo -= 1
    hi = i
    while hi + 1 < n and abs(integs[hi + 1].start - integs[hi].end) <= CHAIN_TOL:
        hi += 1
    if lo == hi:
        return None
    return (float(integs[lo].start), float(integs[hi].end))


def _split_types(pk_ty: str) -> tuple[str, str]:
    """ChemStation's own convention: the left fragment ends on a valley."""
    text = (pk_ty or "").strip()
    if not text:
        return ("BV", "VB")
    return (text[0] + "V", "V" + text[-1])


def split(trace: gc_ch.Trace, integ: Integration, t_split: float,
          cluster: Optional[tuple[float, float]] = None
          ) -> tuple[Integration, Integration]:
    """Perpendicular drop line at ``t_split`` (§VI.7).

    The baseline is **not** touched: both fragments freeze the parent's
    baseline into their own anchors and carry ``baseline="drop"``, so the two
    areas are the parent's area cut at a vertical line. With the same anchors,
    the same scale and an integration grid that interpolates at the bounds, the
    raw integral is additive over the split point and

        ``|a_left + a_right - a_parent| / a_parent < 1e-9``

    holds by construction -- ``t_split`` need not fall on a sample.

    ``cluster`` is only needed when the parent's baseline is ``"cluster"``;
    it is an addition to the contract signature, defaulted, so the three
    argument form still works.
    """
    t_split = float(t_split)
    if not (integ.start < t_split < integ.end):
        raise ValueError(
            f"Der Split-Punkt {t_split:.4f} min liegt nicht innerhalb der "
            f"Peakgrenzen ({integ.start:.4f} bis {integ.end:.4f} min).")

    t, y = profile(trace, integ.start, integ.end)
    base = _baseline_on(trace, integ, t, y, cluster)
    anchor_l = (float(t[0]), float(base[0]))
    anchor_r = (float(t[-1]), float(base[-1]))
    ty_left, ty_right = _split_types(integ.pk_ty)

    # The reference pair belongs to the parent's bounds and would wrongly
    # short-circuit a fragment's area, so it is dropped on both.
    common = dict(baseline=M.BASELINE_DROP, anchor_l=anchor_l,
                  anchor_r=anchor_r, origin=M.ORIGIN_SPLIT,
                  area_ref=None, raw_ref=None)
    left = replace(integ, end=t_split, pk_ty=ty_left, **common)
    right = replace(integ, start=t_split, pk_ty=ty_right, **common)
    return left, right


def _drop_types(pk_ty: str, n: int) -> list[str]:
    """ChemStation peak types for ``n`` fragments cut by drop lines.

    The generalisation of :func:`_split_types` to more than two pieces: only
    the outer ends keep the parent's own letters, every interior end is a
    valley.
    """
    text = (pk_ty or "").strip()
    first, last = (text[0], text[-1]) if text else ("B", "B")
    if n <= 1:
        return [text]
    return [first + "V"] + ["VV"] * (n - 2) + ["V" + last]


def split_many(trace: gc_ch.Trace, integ: Integration,
               points: Sequence[float],
               cluster: Optional[tuple[float, float]] = None
               ) -> list[Integration]:
    """``len(points) + 1`` fragments cut out of ``integ`` by drop lines (§VII.8).

    The N-fragment form of :func:`split`, and deliberately built the same way:
    the parent's baseline is evaluated once and frozen into anchors every
    fragment shares, so the fragments are the parent's peak cut at vertical
    lines and their raw integrals are additive over the cuts. Perpendicular
    drop lines are the only geometry §VI.0/3 allows for a split.

    Raises ``ValueError`` when a point falls outside the parent or the points
    are not strictly ascending -- :class:`SplitByComponents` checks both before
    it calls this, so the analyst sees a refusal rather than a traceback.
    """
    bounds = [float(integ.start)] + [float(p) for p in points] + [float(integ.end)]
    for lo, hi in zip(bounds, bounds[1:]):
        if not (lo < hi):
            raise ValueError(
                f"Die Trennpunkte müssen streng aufsteigend innerhalb der "
                f"Peakgrenzen ({integ.start:.4f} bis {integ.end:.4f} min) "
                f"liegen.")

    t, y = profile(trace, integ.start, integ.end)
    base = _baseline_on(trace, integ, t, y, cluster)
    common = dict(baseline=M.BASELINE_DROP,
                  anchor_l=(float(t[0]), float(base[0])),
                  anchor_r=(float(t[-1]), float(base[-1])),
                  origin=M.ORIGIN_DECONV,
                  # The reference pair belongs to the parent's bounds and would
                  # wrongly short-circuit a fragment's area.
                  area_ref=None, raw_ref=None)
    types = _drop_types(integ.pk_ty, len(bounds) - 1)
    return [replace(integ, start=lo, end=hi, pk_ty=ty, **common)
            for (lo, hi), ty in zip(zip(bounds, bounds[1:]), types)]


def _on_grid(value: float, total: float) -> float:
    """``value`` rounded onto the floating-point grid of ``total``.

    Dekker's trick: for ``|value| <= |total|`` adding and then subtracting
    ``total`` quantises ``value`` to the spacing of the binade ``total`` lives
    in, and the subtraction is exact (Sterbenz). The result is therefore an
    exact multiple of ``ulp(total)``.
    """
    return (value + total) - total


def share_exactly(total: float, weights: Sequence[float]) -> list[float]:
    """Divide ``total`` in the ratio of ``weights``; the parts sum back to it.

    §VII.8 step 4 and the §VI.5 invariant: after a deconvolution split the
    determination's total FID area must not move, and that is asserted as an
    exact float equality, not within a tolerance. The last part absorbs the
    rounding.

    "The last part absorbs the rounding" is not by itself enough:
    ``total - running`` is exact only when Sterbenz' lemma applies, and the
    three ways a caller may add the parts up -- ``sum`` (compensated since
    CPython 3.12), :func:`math.fsum` and ``numpy.sum`` (pairwise) -- do not
    even agree with each other on a naive split. So every part is first
    quantised onto ``ulp(total)`` by :func:`_on_grid`. Each part, each partial
    sum and the remainder are then exact multiples of that spacing and no
    smaller in magnitude than ``total``, which makes every addition exact: the
    parts add up to ``total`` in the real numbers, and all three summation
    algorithms return it. The cost is at most half an ulp of ``total`` per
    part -- 3e-8 counts on the reference sample's largest peak.
    """
    values = [float(w) for w in weights]
    scale = math.fsum(values)
    if not (scale > 0.0) or not math.isfinite(scale):
        raise ValueError("Die Summe der Komponentenflächen ist nicht positiv.")
    total = float(total)
    if len(values) == 1:
        return [total]

    parts = [_on_grid(total * w / scale, total) for w in values[:-1]]
    running = 0.0
    for part in parts:
        running += part                      # exact: both are on the grid
    parts.append(total - running)            # exact, for the same reason
    return parts


def deconv_review_note(fractions: Sequence[float]) -> str:
    """``Fläche aus Dekonvolutionsverhältnis (0,63 / 0,37)`` (§VII.8 step 6).

    The note is what tells the reader of ``Manuell_pruefen`` that these areas
    are modelled from the MS deconvolution and not integrated off the FID
    trace, so it carries the ratio itself rather than only naming the method.
    """
    ratio = " / ".join(f"{float(f):.2f}".replace(".", ",") for f in fractions)
    return f"Fläche aus Dekonvolutionsverhältnis ({ratio})"


def merge(a: Integration, b: Integration) -> Integration:
    """One integration spanning two adjacent peaks (§VI.6, ``⋈ Verbinden``).

    Only adjacent peaks: a gap between them would silently integrate whatever
    lies in between. The merged scale is the mean of the two peaks' scales --
    both baseline models are equally valid over the union, so the mean bounds
    the worst-case error instead of arbitrarily preferring one of them.
    """
    lo, hi = (a, b) if a.start <= b.start else (b, a)
    if hi.start > lo.end + CHAIN_TOL:
        raise ValueError(
            f"Nur benachbarte Peaks können verbunden werden: zwischen "
            f"{lo.end:.4f} und {hi.start:.4f} min liegt eine Lücke.")

    scales = [s for s in (lo.scale, hi.scale) if s is not None]
    scale = float(np.mean(scales)) if scales else None

    manual = (lo.baseline in (M.BASELINE_MANUAL, M.BASELINE_DROP)
              and hi.baseline in (M.BASELINE_MANUAL, M.BASELINE_DROP)
              and lo.anchor_l and hi.anchor_r)
    return Integration(
        start=float(lo.start),
        end=float(hi.end),
        baseline=M.BASELINE_MANUAL if manual else M.BASELINE_ENDPOINT,
        anchor_l=lo.anchor_l if manual else None,
        anchor_r=hi.anchor_r if manual else None,
        scale=scale,
        pk_ty=(lo.pk_ty[:1] + hi.pk_ty[-1:]) if (lo.pk_ty and hi.pk_ty) else "",
        origin=M.ORIGIN_MERGED,
    )


def snap_to_valley(trace: gc_ch.Trace, rt: float,
                   radius: int = SNAP_RADIUS) -> float:
    """The local minimum within ``+-radius`` samples of ``rt`` (§VI.6).

    A released bound lands where the mouse was, which is rarely the valley the
    analyst meant. At 20 Hz the default radius is +-0.15 s.
    """
    if radius <= 0:
        return float(rt)
    i = gc_ch.index_of(trace, rt)
    lo = max(0, i - radius)
    hi = min(trace.y.size, i + radius + 1)
    return float(trace.rt[lo + int(np.argmin(trace.y[lo:hi]))])


# --------------------------------------------------------------------------
# Sample-level helpers
# --------------------------------------------------------------------------

def trace_of(sample) -> gc_ch.Trace:
    """The sample's FID trace, read and cached on first use.

    Reads the ``.D`` directory itself when nothing has cached a trace yet, so
    the maths is testable without the loader.
    """
    trace = getattr(sample, "fid_trace", None)
    if trace is not None:
        return trace
    path = gc_ch.find_fid(getattr(sample, "path", None) or ".")
    if path is None:
        raise FileNotFoundError(
            f"{getattr(sample, 'label', '?')}: keine FID-Rohdatei (.ch) im "
            f"Verzeichnis gefunden.")
    trace = gc_ch.read_ch(path)
    try:
        sample.fid_trace = trace
    except AttributeError:                               # pragma: no cover
        pass
    return trace


def integrated_rows(sample) -> list[Any]:
    """The rows carrying FID bounds, in retention-time order."""
    rows = [r for r in sample.rows
            if getattr(r, "fid_start", None) is not None
            and getattr(r, "fid_end", None) is not None]
    rows.sort(key=lambda r: (float(r.fid_start), float(r.fid_end)))
    return rows


def cluster_for_row(sample, row) -> Optional[tuple[float, float]]:
    """:func:`cluster_of` for one row of a sample, or ``None``."""
    if (row.fid_baseline or M.BASELINE_ENDPOINT) != M.BASELINE_CLUSTER:
        return None
    rows = integrated_rows(sample)
    try:
        i = rows.index(row)
    except ValueError:
        return None
    return cluster_of([Integration.from_row(r) for r in rows], i)


def default_baseline(sample, row) -> str:
    """The baseline a row would get at load: ``cluster`` inside a chain.

    §VI.5 applies the cluster baseline automatically to VV/PV/BV/VB chains, so
    "reset to how it was loaded" (§VI.8) can re-derive it rather than store a
    second copy that could drift.
    """
    rows = integrated_rows(sample)
    try:
        i = rows.index(row)
    except ValueError:
        return M.BASELINE_ENDPOINT
    integs = [Integration.from_row(r) for r in rows]
    return (M.BASELINE_CLUSTER if cluster_of(integs, i)
            else M.BASELINE_ENDPOINT)


def is_untouched(row, integ: Optional[Integration] = None) -> bool:
    """True when nothing has moved this row's integration since it loaded.

    "Untouched" is decided on the three things that define the window --
    origin, bounds and baseline model -- against the load-time snapshot in
    ``row.original``. It is the condition under which §VI.5's first consequence
    must hold: the peak reports its ``RESULTS.CSV`` area, not a recomputation
    of it.
    """
    if getattr(row, "integration_origin", None) != M.ORIGIN_CHEMSTATION:
        return False
    original = getattr(row, "original", None) or {}
    if "fid_start" not in original or "fid_end" not in original:
        return False
    if original.get("raw_area") is None:
        return False
    integ = integ if integ is not None else Integration.from_row(row)
    if integ.baseline not in (M.BASELINE_ENDPOINT, M.BASELINE_CLUSTER):
        return False
    return (M._same(integ.start, original["fid_start"])
            and M._same(integ.end, original["fid_end"]))


def integrate_row(sample, row, trace: Optional[gc_ch.Trace] = None,
                  integ: Optional[Integration] = None,
                  cluster: Optional[tuple[float, float]] = None) -> IntegrationResult:
    """Integrate one row of a sample, resolving its cluster automatically.

    Carries the bit-for-bit guarantee of §VI.5 even when the row was
    calibrated through the plain :func:`calibrate` float, which is what
    ``gc_fid.calibrate_sample`` stores: for an untouched row the reported area
    is handed back verbatim instead of being reconstructed as
    ``(a / r) * r``, a round trip that lands one ulp away from ``a`` for about
    8 % of float64 pairs. Geometry -- height, apex, width -- is always real.

    ``cluster`` spares a caller that already resolved the row's cluster a
    second :func:`cluster_for_row`, which sorts and integrates every row.
    """
    trace = trace if trace is not None else trace_of(sample)
    integ = integ if integ is not None else Integration.from_row(row)
    if integ.baseline != M.BASELINE_CLUSTER:
        cluster = None
    elif cluster is None:
        cluster = cluster_for_row(sample, row)
    result = integrate(trace, integ, cluster)
    if integ.area_ref is None and is_untouched(row, integ):
        result = replace(result, area=float(row.original["raw_area"]))
    return result


def target_raw_area(row, area: float) -> float:
    """The raw FID area a *corrected* area of ``area`` implies (SS 11.4).

    ``area = raw_area - blank_area`` everywhere in ``gc_fid``, and an
    ISTD-protected row is never blank-subtracted, so this is that one rule read
    backwards. Keeping it in one function is what stops the geometry and the
    grid from disagreeing about which of the two areas was meant.
    """
    value = _as_float(area)
    if value is None:
        raise ValueError("Für die Fläche wurde keine Zahl übergeben.")
    if getattr(row, "protected_standard", False):
        return value
    blank = _as_float(getattr(row, "blank_area", None)) or 0.0
    return value + blank


def baseline_for_area(trace: gc_ch.Trace, integ: Integration,
                      target_raw: float,
                      cluster: Optional[tuple[float, float]] = None
                      ) -> Integration:
    """``integ`` with its baseline shifted so the peak integrates to ``target_raw``.

    A parallel shift by ``delta`` changes the integral by exactly
    ``delta * width_in_seconds`` -- the baseline is linear between its two
    anchors and both move together -- so the offset is closed form rather than
    a search. ``target_raw`` is the *reported* area, scale included; the shift
    is computed on the bare integral underneath it.

    Raises ``ValueError`` when the area cannot be reached: a target of zero or
    less has no baseline, and neither does one that would need the baseline to
    sit above the peak's own apex.
    """
    wanted_reported = _as_float(target_raw)
    if wanted_reported is None or wanted_reported <= 0.0:
        raise ValueError(
            "Eine Fläche von 0 lässt sich nicht über die Basislinie "
            "einstellen. Peak verwerfen, wenn er nicht berichtet werden soll.")

    t, y = profile(trace, integ.start, integ.end)
    base = _baseline_on(trace, integ, t, y, cluster)
    span = (float(t[-1]) - float(t[0])) * 60.0
    if span <= 0.0:                                      # pragma: no cover
        raise ValueError("Der Peak hat keine Breite.")
    scale = integ.effective_scale
    if not scale:                                        # pragma: no cover
        raise ValueError("Für diesen Peak ist kein Flächenfaktor bekannt.")

    raw_now = float(np.trapezoid(y - base, t * 60.0))
    delta = (wanted_reported / scale - raw_now) / span
    if not np.isfinite(delta):                           # pragma: no cover
        raise ValueError("Die Fläche ließ sich nicht in eine Basislinie "
                         "umrechnen.")
    if float(np.max(y - (base - delta))) <= 0.0:
        raise ValueError(
            "Diese Fläche ist zu klein: die Basislinie käme über den Apex zu "
            "liegen. Grenzen anpassen oder den Peak verwerfen.")

    if integ.baseline in (M.BASELINE_MANUAL, M.BASELINE_DROP)             and integ.anchor_l and integ.anchor_r:
        (t0, y0), (t1, y1) = integ.anchor_l, integ.anchor_r
    else:
        t0, t1 = float(t[0]), float(t[-1])
        y0, y1 = float(base[0]), float(base[-1])
    return replace(integ, baseline=M.BASELINE_MANUAL,
                   anchor_l=(float(t0), float(y0) - delta),
                   anchor_r=(float(t1), float(y1) - delta),
                   # The reference pair was measured against the old baseline.
                   area_ref=None, raw_ref=None)


def calibrate_sample(sample, trace: Optional[gc_ch.Trace] = None) -> int:
    """Store ``scale`` on every row that has a reported area but no scale yet.

    Called once after loading. Returns how many rows were calibrated. Rows the
    instrument never reported keep ``fid_scale = None`` and fall back to
    :data:`AREA_SCALE`.
    """
    trace = trace if trace is not None else trace_of(sample)
    rows = integrated_rows(sample)
    integs = [Integration.from_row(r) for r in rows]
    done = 0
    for i, (row, integ) in enumerate(zip(rows, integs)):
        if integ.scale is not None:
            continue
        reported = row.raw_area if row.raw_area is not None else row.area
        if reported is None:
            continue
        cluster = (cluster_of(integs, i)
                   if integ.baseline == M.BASELINE_CLUSTER else None)
        calibrated(reported, trace, integ, cluster).apply_to_row(row)
        done += 1
    return done


# --------------------------------------------------------------------------
# Row state: undo is a restore, never a recomputation
# --------------------------------------------------------------------------

#: Attributes captured separately because they are mutable containers.
_CONTAINERS = ("derived", "original", "edited", "manual", "alt_hits")


def _copy_value(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, set):
        return set(value)
    return value


def _capture_row(row) -> dict[str, Any]:
    """Every mutable piece of a row, one level deep.

    One level is enough: the commands replace ``derived`` entries wholesale
    rather than mutating a list in place, and a full deep copy of 139 rows per
    command would cost far more than it protects.
    """
    state = {name: getattr(row, name)
             for name in row.__dataclass_fields__            # type: ignore[attr-defined]
             if name not in _CONTAINERS}
    state["derived"] = {k: _copy_value(v)
                        for k, v in (getattr(row, "derived", None) or {}).items()}
    state["original"] = dict(getattr(row, "original", None) or {})
    state["edited"] = set(getattr(row, "edited", None) or ())
    state["manual"] = set(getattr(row, "manual", None) or ())
    state["alt_hits"] = list(getattr(row, "alt_hits", None) or ())
    return state


def _restore_row(row, state: dict[str, Any]) -> None:
    for name, value in state.items():
        if name in ("derived", "original"):
            target = getattr(row, name)
            target.clear()
            target.update(value)
        elif name in ("edited", "manual"):
            target = getattr(row, name)
            target.clear()
            target.update(value)
        elif name == "alt_hits":
            row.alt_hits = list(value)
        else:
            setattr(row, name, value)


#: ``sample.meta`` entries a command can move, via ``_refresh``.
#:
#: Each is captured **with its presence**, not only with its value: "undo is a
#: restore" (§VI.8) has to be able to take a key away again, not only to put a
#: value back. A determination that had no ``mean_factor`` before a command --
#: a bare :class:`gc_model.Sample` on the DIN SPEC path, where nothing writes
#: one -- must not have one after the undo (D-INT-4).
_META_KEYS = ("mean_factor", "fid_count")


def _capture_sample(sample) -> dict[str, Any]:
    """Sample-level state a structural command can move."""
    meta = getattr(sample, "meta", None) or {}
    state: dict[str, Any] = {
        "rows": list(sample.rows),
        "mean_factor": getattr(sample, "mean_factor", None),
        "is_row_id": getattr(sample, "is_row_id", None),
        "is_code": getattr(sample, "is_code", None),
    }
    for name in _META_KEYS:
        state[f"meta_{name}"] = meta.get(name)
        state[f"meta_has_{name}"] = name in meta
    return state


def _restore_sample(sample, state: dict[str, Any]) -> None:
    sample.rows[:] = state["rows"]
    for name in ("mean_factor", "is_row_id", "is_code"):
        try:
            setattr(sample, name, state[name])
        except AttributeError:                           # pragma: no cover
            pass
    meta = getattr(sample, "meta", None)
    if isinstance(meta, dict):
        for name in _META_KEYS:
            # ``.get(..., True)`` so a state captured before the presence flag
            # existed still restores the value rather than deleting the key.
            if state.get(f"meta_has_{name}", True):
                meta[name] = state[f"meta_{name}"]
            else:
                meta.pop(name, None)


def _sync_edited(row, *names: str) -> None:
    """Keep ``row.edited`` truthful after a command wrote a field."""
    for name in names:
        if name not in row.original:
            row.edited.add(name)
            continue
        if M._same(getattr(row, name, None), row.original[name]):
            row.edited.discard(name)
        else:
            row.edited.add(name)


def _refresh(sample) -> None:
    """Rebuild everything an integration change invalidates (§VI.6).

    The standards are re-linked first: a split or a disabled peak can move
    which row *is* the standard, and ``mean_factor`` is built from those areas
    (§V.1). ``recalculate`` is deterministic in the row state, which is why a
    restore followed by a refresh reproduces the pre-command numbers exactly.
    """
    link = getattr(sample, "link_standards", None)
    if callable(link):
        link()
    recalc = getattr(sample, "recalculate", None)
    if callable(recalc):
        recalc()


def _fid():
    """``gc_fid`` imported lazily, so this module imports before it lands."""
    import gc_fid
    return gc_fid


def write_result(sample, row, integ: Integration,
                 result: IntegrationResult) -> None:
    """Write one integration and its numbers onto a row.

    ``raw_area`` is the FID area before blank subtraction, which is what
    ``RESULTS.CSV`` reports and what ``NiasSample.recalculate`` turns into
    ``area`` and then into mg/kg. The corrected ``area`` is deliberately *not*
    written here: a dragged bound marks it as edited, never as a manual
    override, or the next drag could no longer reach it (§V.1, §VI.13).
    """
    integ.apply_to_row(row)
    row.raw_area = result.area
    row.height = result.height
    _sync_edited(row, "fid_start", "fid_end", "raw_area", "height")


def apply_area_by_baseline(sample, row, area: float, trace=None) -> None:
    """Shift the baseline until *row* really integrates to *area* (§V.1).

    The body :class:`SetAreaByBaseline` used to carry inline. Lifted out because
    :class:`SetValues` needs the same step for every ``area`` cell a fill or a
    paste writes, and a typed number whose picture does not follow is the thing
    §V.1 exists to prevent. Deliberately does **not** call :func:`_refresh`: the
    caller owns that, and a bulk edit refreshes once at the end rather than once
    per cell.
    """
    trace = trace if trace is not None else trace_of(sample)
    integ = Integration.from_row(row)
    cluster = (cluster_for_row(sample, row)
               if integ.baseline == M.BASELINE_CLUSTER else None)
    integ = baseline_for_area(trace, integ, target_raw_area(row, area), cluster)
    if integ.origin == M.ORIGIN_CHEMSTATION:
        integ.origin = M.ORIGIN_MANUAL
    write_result(sample, row, integ, integrate_row(sample, row, trace, integ, cluster))
    review_note(row)


def review_note(row) -> None:
    """Flag a touched row for ``Manuell_pruefen`` (§VI.7.5, §VI.14)."""
    add_review_note(row, M.MANUAL_INTEGRATION_REVIEW)


def add_review_note(row, note: str) -> None:
    """Append one review note to a row without duplicating it.

    §VII.8 needs a second note beside ``Manuelle Integration; Prüfung``, and
    both go into the same ``review`` string the report reads, so the appending
    lives in one place.
    """
    current = (row.derived.get("review") or "").strip()
    if note not in current:
        row.derived["review"] = f"{current}; {note}" if current else note
    row.review_done = False


def samples_between(trace: gc_ch.Trace, lo: float, hi: float) -> int:
    """How many real trace samples fall inside ``[lo, hi]``.

    The measure §VII.8 refuses a split on: interpolated bounds always give a
    grid of at least two points, so counting the grid would never notice a
    fragment that has no trace under it.
    """
    rt = trace.rt
    left = int(np.searchsorted(rt, float(lo), side="left"))
    right = int(np.searchsorted(rt, float(hi), side="right"))
    return max(right - left, 0)


def _component_weight(item: Any) -> Optional[float]:
    """The area of one deconvolution component, however it was handed over.

    §VII.8's contract says ``weights`` are "the component areas", so a plain
    float is the primary form. A :class:`gc_deconv.Component` (or a dict) is
    accepted too, because only the component itself carries the ``model_mz``
    and ``purity`` that step 5 has to store on the fragment -- see
    :class:`SplitByComponents`.
    """
    if isinstance(item, Mapping):
        return _as_float(item.get("area"))
    return _as_float(getattr(item, "area", item))


def _component_meta(item: Any) -> dict[str, Any]:
    """``rt`` / ``model_mz`` / ``purity`` of a component, empty for a bare float."""
    if isinstance(item, Mapping):
        get = item.get
    else:
        def get(key: str, default: Any = None) -> Any:
            return getattr(item, key, default)
    mz = get("model_mz")
    return {
        "rt": _as_float(get("rt")),
        "model_mz": None if mz is None else int(mz),
        "purity": _as_float(get("purity")),
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

class IntegrationCommand:
    """Base class: capture, act, restore.

    Every subclass stores the complete before state of every row it touched
    plus the rows it created, so :meth:`undo` puts the values back rather than
    trying to invert the arithmetic that produced them (§VI.8).
    """

    label: str = ""
    #: Structural commands renumber and reorder the whole sample, so they
    #: capture every row rather than only the ones they name.
    structural: bool = False

    def __init__(self) -> None:
        self._rows: dict[int, dict[str, Any]] = {}
        self._sample: Optional[dict[str, Any]] = None
        self._after_rows: dict[int, dict[str, Any]] = {}
        self._after_sample: Optional[dict[str, Any]] = None

    # -- capture / restore -------------------------------------------------

    def _capture(self, sample, rows: Iterable[Any] = ()) -> None:
        self._sample = _capture_sample(sample)
        targets = list(sample.rows) if self.structural else list(rows)
        self._rows = {r.row_id: _capture_row(r) for r in targets}

    def _capture_after(self, sample) -> None:
        self._after_sample = _capture_sample(sample)
        targets = (list(sample.rows) if self.structural
                   else [r for r in sample.rows if r.row_id in self._rows])
        self._after_rows = {r.row_id: _capture_row(r) for r in targets}

    @staticmethod
    def _apply(sample, sample_state: Optional[dict[str, Any]],
               row_states: dict[str, Any]) -> None:
        if sample_state is not None:
            _restore_sample(sample, sample_state)
        by_id = {r.row_id: r for r in sample.rows}
        for row_id, state in row_states.items():
            row = by_id.get(row_id)
            if row is not None:
                _restore_row(row, state)
        _refresh(sample)

    def do(self, sample) -> None:
        """Run the action, or -- on a redo -- restore the state it produced.

        Redo is a restore for the same reason undo is (§VI.8), and for one more:
        re-running a structural command would allocate *new* row ids for the
        rows it created, so a row would change identity every time the analyst
        pressed Ctrl+Y. Ids are never reused (§VI.7.1), and a redone split has
        to give back the same fragment, not a twin of it.
        """
        if self._after_sample is not None:
            self._apply(sample, self._after_sample, self._after_rows)
            return
        self._perform(sample)
        self._capture_after(sample)

    def undo(self, sample) -> None:
        self._apply(sample, self._sample, self._rows)

    # -- helpers for the subclasses ---------------------------------------

    @staticmethod
    def _row(sample, row_id: int):
        row = sample.row(row_id)
        if row is None:
            raise ValueError(f"Peak mit der Zeilen-ID {row_id} existiert nicht.")
        return row

    @staticmethod
    def _clamp(trace: gc_ch.Trace, start: float, end: float) -> tuple[float, float]:
        lo, hi = float(trace.rt[0]), float(trace.rt[-1])
        start, end = float(start), float(end)
        if not (start < end):
            raise ValueError(
                f"Die linke Grenze ({start:.4f} min) muss vor der rechten "
                f"({end:.4f} min) liegen. Die Grenzen werden nicht vertauscht.")
        return max(start, lo), min(end, hi)

    def _perform(self, sample) -> None:                  # pragma: no cover
        """The action itself. Subclasses implement this, never ``do``."""
        raise NotImplementedError


class SetBounds(IntegrationCommand):
    """Move one peak's integration bounds (``⇥ Grenzen``)."""

    def __init__(self, row_id: int, start: float, end: float) -> None:
        super().__init__()
        self.row_id = row_id
        self.start = float(start)
        self.end = float(end)
        self.label = f"Grenzen Peak {row_id}"

    def _perform(self, sample) -> None:
        row = self._row(sample, self.row_id)
        trace = trace_of(sample)
        start, end = self._clamp(trace, self.start, self.end)
        self._capture(sample, [row])
        self.label = f"Grenzen Peak {row.peak_no}"

        integ = Integration.from_row(row)
        integ = replace(integ, start=start, end=end)
        if integ.origin == M.ORIGIN_CHEMSTATION:
            integ.origin = M.ORIGIN_MANUAL
        write_result(sample, row, integ,
                     integrate_row(sample, row, trace, integ))
        review_note(row)
        _refresh(sample)


class SetBaseline(IntegrationCommand):
    """Switch a peak's baseline model or drag its anchors (``⌇ Basislinie``)."""

    def __init__(self, row_id: int, mode: str,
                 anchor_l: Optional[tuple[float, float]] = None,
                 anchor_r: Optional[tuple[float, float]] = None) -> None:
        super().__init__()
        if mode not in BASELINE_MODES:
            raise ValueError(f"Unbekanntes Basislinienmodell: {mode!r}")
        self.row_id = row_id
        self.mode = mode
        self.anchor_l = _as_anchor(anchor_l)
        self.anchor_r = _as_anchor(anchor_r)
        self.label = f"Basislinie Peak {row_id}"

    def _perform(self, sample) -> None:
        row = self._row(sample, self.row_id)
        trace = trace_of(sample)
        self._capture(sample, [row])
        self.label = f"Basislinie Peak {row.peak_no}"

        integ = Integration.from_row(row)
        integ = replace(integ, baseline=self.mode)
        if self.mode == M.BASELINE_MANUAL:
            # A manual baseline that keeps only one anchor is under-determined;
            # the missing one falls back to the peak's own end point.
            t, y = profile(trace, integ.start, integ.end)
            integ.anchor_l = self.anchor_l or (float(t[0]), float(y[0]))
            integ.anchor_r = self.anchor_r or (float(t[-1]), float(y[-1]))
        elif self.mode != M.BASELINE_DROP:
            integ.anchor_l = integ.anchor_r = None
        # The reference pair was measured against the old baseline.
        integ.area_ref = integ.raw_ref = None
        if integ.origin == M.ORIGIN_CHEMSTATION:
            integ.origin = M.ORIGIN_MANUAL
        write_result(sample, row, integ,
                     integrate_row(sample, row, trace, integ))
        review_note(row)
        _refresh(sample)


class SetAreaByBaseline(IntegrationCommand):
    """Lower (or raise) the baseline until the peak carries a typed area.

    The corrected FID area is a column an analyst may type into, and a typed
    number that leaves the picture untouched is a number nobody can check. This
    command is the other direction of §V.1's coupling: the value the analyst
    entered is turned back into geometry, so the shaded peak in the FID panel
    really is the area the grid reports.

    The baseline is shifted *parallel* -- both anchors move by the same amount,
    so its slope, and with it the peak's own shape, is left alone. A larger area
    drops the baseline and the fill grows downwards; a smaller one lifts it.
    The bounds are never moved: widening a peak to reach an area would swallow
    signal that belongs to its neighbours.
    """

    def __init__(self, row_id: int, area: float) -> None:
        super().__init__()
        self.row_id = row_id
        self.area = _as_float(area)
        if self.area is None:
            raise ValueError("Für die Fläche wurde keine Zahl übergeben.")
        self.label = f"Fläche Peak {row_id}"

    def _perform(self, sample) -> None:
        row = self._row(sample, self.row_id)
        trace = trace_of(sample)
        self._capture(sample, [row])
        self.label = f"Fläche Peak {row.peak_no}"

        apply_area_by_baseline(sample, row, self.area, trace)
        _refresh(sample)


class NothingChanged(ValueError):
    """A value block in which every cell already held the typed value.

    Still a ``ValueError`` so ``IntegrationHistory.push`` records no empty step,
    but distinct from a refusal: re-committing an unchanged cell -- a FocusOut
    right after Enter in the Standards panel -- is not an error to report.
    """


class SetValues(IntegrationCommand):
    """A block of typed cell values as one undoable step (§V.1, §VI.8).

    Until now a typed value was the one edit the analyst could not take back
    with ``Strg+Z``: the integration history carried geometry, and a number had
    to be undone cell by cell with "Zelle zurücksetzen". That was tolerable
    while a cell could only be typed one at a time. It stops being tolerable
    the moment a fill or a paste writes forty of them, so the values join the
    same history the geometry already uses.

    Undo is the base class's restore, not an inverse write: ``_capture_row``
    snapshots ``edited``, ``manual``, ``original`` and ``derived``, so taking a
    fill back also takes back the blue "edited" tint and the sticky override
    §V.1 attaches to a typed number -- neither of which could be reconstructed
    from the old value alone.

    A cell the model refuses does not abort the block. It lands in
    :attr:`failures` and the rest is applied; the grid paints those cells red
    and the status line counts them. Only a block in which *nothing* was
    applied raises, so that ``IntegrationHistory.push`` -- which records a
    command only after ``do`` returned -- leaves no empty step behind.
    """

    def __init__(self, writes: Sequence[tuple[int, str, Any]] = (),
                 resets: Sequence[tuple[int, str]] = (),
                 label: str = "Werte", fit_area: bool = True) -> None:
        super().__init__()
        self.writes = [(int(r), str(f), v) for r, f, v in writes]
        self.resets = [(int(r), str(f)) for r, f in resets]
        self.fit_area = bool(fit_area)
        #: One per cell that really moved, for the "Manuelle Änderungen" sheet.
        #: The workspace appends them to ``session.audit`` -- this command only
        #: produces them, so the sheet still gets exactly one row per changed
        #: cell however the edit was entered.
        self.records: list[Any] = []
        #: ``(row_id, field, message)`` for every cell the model refused. The
        #: value did *not* change; the grid paints these red.
        self.failures: list[tuple[int, str, str]] = []
        #: Areas that were accepted but whose baseline could not be made to
        #: follow. Deliberately not a failure: the number is stored, so calling
        #: it "abgelehnt" and colouring the cell red would misreport it. Kept
        #: apart so the status line can still mention it.
        self.warnings: list[str] = []
        self.applied = 0
        self._base_label = label or "Werte"
        self.label = self._base_label

    def _perform(self, sample) -> None:
        ids = list(dict.fromkeys([r for r, _f, _v in self.writes]
                                 + [r for r, _f in self.resets]))
        rows = {}
        for row_id in ids:
            row = sample.row(row_id)
            if row is None:
                # A stale id must not cost the other 39 cells their edit.
                self.failures.append(
                    (row_id, "", f"Zeile {row_id} existiert nicht mehr."))
                continue
            rows[row_id] = row
        # Before the first mutation, and only the rows this block names: a
        # value edit never renumbers or reorders, so it is not ``structural``.
        self._capture(sample, list(rows.values()))

        for row_id, field in self.resets:
            row = rows.get(row_id)
            if row is None:
                continue
            if field not in M.EDITABLE_FIELDS:
                self.failures.append(
                    (row_id, field, f"Spalte '{field}' ist nicht editierbar."))
                continue
            old = getattr(row, field, None)
            new = row.reset_field(field)
            if not M._same(old, new):
                self.records.append(
                    M.EditRecord(
                        timestamp=datetime.now().isoformat(timespec="seconds"),
                        sample=getattr(sample, "label", ""), row_id=row.row_id,
                        peak_no=row.peak_no, field=field, old=old, new=new))

        areas: list[tuple[Any, float]] = []
        for row_id, field, value in self.writes:
            if rows.get(row_id) is None:
                continue
            try:
                # The model's own gate: coercion, the non-negative rules,
                # ``_check_fid_bounds`` and the ISTD protection of §11.2.
                record = sample.set_value(row_id, field, value)
            except ValueError as exc:
                self.failures.append((row_id, field, str(exc)))
                continue
            if record is None:                    # unchanged, nothing to undo
                continue
            self.records.append(record)
            if field == "area":
                areas.append((rows[row_id], record.new))

        if self.fit_area and areas:
            self._fit_areas(sample, areas)

        if not self.records:
            if self.failures:
                raise ValueError(self.failures[0][2])
            raise NothingChanged("Keine Zelle wurde geändert.")
        self.applied = len(self.records)
        self.label = (f"{self._base_label} (1 Zelle)" if self.applied == 1
                      else f"{self._base_label} ({self.applied} Zellen)")
        _refresh(sample)

    def _fit_areas(self, sample, areas: list[tuple[Any, float]]) -> None:
        """Let the drawn peaks follow the typed corrected areas (§V.1).

        A row whose picture cannot follow keeps its number: refusing an edit
        that the model already accepted would be worse than a sentence saying
        the baseline stayed put.
        """
        try:
            trace = trace_of(sample)
        except Exception as exc:                  # no .ch, unreadable, no gc_ch
            self.warnings.append(
                f"{len(areas)} Fläche(n) übernommen, die Basislinie konnte "
                f"nicht folgen: {exc}")
            return
        for row, value in areas:
            try:
                apply_area_by_baseline(sample, row, value, trace)
            except Exception as exc:
                self.warnings.append(
                    f"Peak {row.peak_no}: Fläche übernommen, Basislinie "
                    f"nicht: {exc}")


class Split(IntegrationCommand):
    """Perpendicular drop line: one row becomes two (§VI.7)."""

    structural = True

    def __init__(self, row_id: int, at: float) -> None:
        super().__init__()
        self.row_id = row_id
        self.at = float(at)
        self.label = f"Split Peak {row_id} @ {self.at:.2f}"
        #: The fragment created, for the caller to select afterwards.
        self.child_id: Optional[int] = None

    def _perform(self, sample) -> None:
        fid = _fid()
        parent = self._row(sample, self.row_id)
        trace = trace_of(sample)
        integ = Integration.from_row(parent)
        cluster = cluster_for_row(sample, parent)
        left, right = split(trace, integ, self.at, cluster)

        self._capture(sample)
        self.label = f"Split Peak {parent.peak_no} @ {self.at:.2f}"

        child = fid.new_row(sample, right.start, right.end,
                            M.ORIGIN_SPLIT, parent=parent)
        self.child_id = child.row_id

        write_result(sample, parent, left, integrate(trace, left))
        write_result(sample, child, right, integrate(trace, right))
        for row in (parent, child):
            review_note(row)
        # Identification and blank correction belong to the data layer; the
        # split point moved the window, so both have to be re-run for the
        # fragments (§VI.7.4 and §VI.7.6).
        for row in (parent, child):
            fid.reassign_row(sample, row)
            fid.rematch_blank(sample, row)
        _refresh(sample)


class SplitByComponents(IntegrationCommand):
    """Deconvolution-driven split: one row becomes ``len(points) + 1`` (§VII.8).

    The sibling of :class:`Split`, not a second implementation of it: the same
    perpendicular drop lines, the same frozen parent baseline, the same
    ``reassign_row`` / ``rematch_blank`` per fragment, the same "undo is a
    restore" contract. Two things are different, and only two:

    * the cut points come from the MS side -- the valleys between the
      deconvoluted components -- rather than from the mouse;
    * the areas are **modelled**. The fragments are integrated off the trace as
      usual and then rescaled so ``area_i = area_parent * w_i / sum(w)``, with
      the last fragment absorbing the rounding, so ``sum(area_i)`` equals the
      parent's area exactly. The determination's total FID area therefore does
      not move (§VI.5), which is the invariant that lets a deconvolution be
      applied to a report that was already reviewed.

    Steps 1 and 2 of §VII.8 -- mapping a component's MS retention time out to
    FID time with ``sample.meta["delay"]`` and finding the valley between two
    components -- belong to the caller, which is why this takes finished FID
    retention times.

    ``points`` are those FID retention times, ``weights`` the component areas,
    one per fragment (``len(weights) == len(points) + 1``). A ``weights``
    element may also be the :class:`gc_deconv.Component` itself (or a mapping);
    its ``area`` is then the weight and its ``rt``/``model_mz``/``purity`` fill
    ``row.derived["deconv_component"]``, which step 5 requires and which a bare
    float cannot carry.

    Refusals (§VII.8) raise nothing: they leave the sample untouched and put
    the reason in :attr:`refusal` for the status line. A refused command is not
    an undo step -- ``undo`` is then a no-op -- but a caller that runs it
    through :class:`IntegrationHistory` should check :attr:`refusal` before
    pushing, or the analyst spends a Ctrl+Z on nothing.
    """

    structural = True

    def __init__(self, row_id: int, points: Sequence[float],
                 weights: Sequence[Any]) -> None:
        super().__init__()
        self.row_id = int(row_id)
        self.points = [float(p) for p in points]
        self.weights = list(weights)
        #: Why nothing happened, or None. Never an exception (§VII.8).
        self.refusal: Optional[str] = None
        #: The fragments created, for the caller to select afterwards.
        self.child_ids: list[int] = []
        self.label = (f"FID nach Dekonvolution geteilt "
                      f"({len(self.weights)} Komponenten)")

    # -- refusal-aware do/undo --------------------------------------------

    def do(self, sample) -> None:
        """As the base class, except that a refusal produces no after state.

        Without this a refused command would capture the untouched sample as
        its "after" state and a later redo would restore it -- harmless, but it
        would make ``refusal`` look like a result rather than a rejection.
        """
        if self._after_sample is not None:
            self._apply(sample, self._after_sample, self._after_rows)
            return
        self.refusal = None
        self._perform(sample)
        if self.refusal is None:
            self._capture_after(sample)

    def undo(self, sample) -> None:
        if self._sample is None:
            return                      # refused: there is nothing to put back
        super().undo(sample)

    # -- the action --------------------------------------------------------

    def _refuse(self, reason: str) -> None:
        self.refusal = reason

    def _check(self, sample, trace: gc_ch.Trace,
               integ: Integration) -> Optional[list[float]]:
        """Every §VII.8 refusal, before a single value is written.

        Returns the component weights, or None after setting :attr:`refusal`.
        The order is the spec's: too few components, a zero weight, then a
        split point that starves a fragment.
        """
        n = len(self.weights)
        if n < 2 or len(self.points) != n - 1:
            self._refuse(
                "Weniger als zwei Komponenten innerhalb der Peakgrenzen — "
                "der Peak wird nicht geteilt.")
            return None

        weights: list[float] = []
        for i, item in enumerate(self.weights, 1):
            value = _component_weight(item)
            if value is None or not (value > 0.0):
                self._refuse(
                    f"Komponente {i} hat die Fläche 0 — die Peakfläche lässt "
                    f"sich nicht im Verhältnis der Komponenten aufteilen.")
                return None
            weights.append(value)

        bounds = [float(integ.start)] + self.points + [float(integ.end)]
        for lo, hi in zip(bounds, bounds[1:]):
            if not (lo < hi):
                self._refuse(
                    f"Ein Trennpunkt liegt nicht zwischen den Peakgrenzen "
                    f"({integ.start:.4f} bis {integ.end:.4f} min).")
                return None
            if samples_between(trace, lo, hi) < MIN_FRAGMENT_SAMPLES:
                self._refuse(
                    f"Ein Trennpunkt ließe das Fragment {lo:.4f}–{hi:.4f} min "
                    f"mit weniger als {MIN_FRAGMENT_SAMPLES} Messpunkten "
                    f"zurück — der Peak wird nicht geteilt.")
                return None
        return weights

    def _perform(self, sample) -> None:
        fid = _fid()
        parent = self._row(sample, self.row_id)
        trace = trace_of(sample)
        try:
            integ = Integration.from_row(parent)
        except ValueError as exc:
            # No FID bounds: a legitimate state of a row, not a bug, so it is a
            # refusal like the other three rather than a traceback.
            self._refuse(str(exc))
            return

        weights = self._check(sample, trace, integ)
        if weights is None:
            return

        cluster = cluster_for_row(sample, parent)
        frags = split_many(trace, integ, self.points, cluster)

        # The area the fragments have to add up to. The row's own ``raw_area``
        # is what the determination's total is built from *right now* -- for an
        # untouched peak it is the RESULTS.CSV number bit for bit -- so it, and
        # not a fresh integral, is what must be conserved.
        parent_area = _as_float(getattr(parent, "raw_area", None))
        if parent_area is None:
            parent_area = integrate_row(sample, parent, trace, integ).area

        total = math.fsum(weights)
        fractions = [w / total for w in weights]
        areas = share_exactly(parent_area, weights)
        note = deconv_review_note(fractions)

        self._capture(sample)
        self.label = (f"FID nach Dekonvolution geteilt "
                      f"({len(frags)} Komponenten)")

        # The parent keeps the first fragment, exactly as ``Split`` does, so
        # its row id -- and every edit flag and register entry keyed on it --
        # survives the split.
        rows = [parent]
        self.child_ids = []
        for frag in frags[1:]:
            child = fid.new_row(sample, frag.start, frag.end,
                                M.ORIGIN_DECONV, parent=parent)
            self.child_ids.append(child.row_id)
            rows.append(child)

        for row, frag, area, fraction, item in zip(
                rows, frags, areas, fractions, self.weights):
            result = replace(integrate(trace, frag), area=area)
            write_result(sample, row, frag, result)
            meta = _component_meta(item)
            row.derived["deconv_component"] = {
                # A bare float carries no component retention time; the
                # fragment's own apex is then the honest answer.
                "rt": meta["rt"] if meta["rt"] is not None else result.apex_rt,
                "model_mz": meta["model_mz"],
                "purity": meta["purity"],
                "weight": fraction,
            }
            review_note(row)
            add_review_note(row, note)

        # Identification and blank correction belong to the data layer; the
        # split moved every window, so both are re-run per fragment
        # (§VI.7.4, §VI.7.6, §VII.8 step 5).
        for row in rows:
            fid.reassign_row(sample, row)
            fid.rematch_blank(sample, row)
        _refresh(sample)


class AddPeak(IntegrationCommand):
    """Integrate a stretch of trace ChemStation never reported (``＋ Peak``)."""

    structural = True

    def __init__(self, start: float, end: float) -> None:
        super().__init__()
        self.start = float(start)
        self.end = float(end)
        self.label = f"Peak anlegen {self.start:.2f}–{self.end:.2f}"
        self.row_id: Optional[int] = None

    def _perform(self, sample) -> None:
        fid = _fid()
        trace = trace_of(sample)
        start, end = self._clamp(trace, self.start, self.end)
        self._capture(sample)

        row = fid.new_row(sample, start, end, M.ORIGIN_MANUAL)
        self.row_id = row.row_id
        # No reported area, so no scale: AREA_SCALE is the documented fallback.
        integ = Integration(start=start, end=end,
                            baseline=M.BASELINE_ENDPOINT,
                            origin=M.ORIGIN_MANUAL)
        write_result(sample, row, integ, integrate(trace, integ))
        review_note(row)
        fid.reassign_row(sample, row)
        fid.rematch_blank(sample, row)
        _refresh(sample)


class DisablePeak(IntegrationCommand):
    """Reject a ChemStation peak without losing it (``－ Peak``, §VI.8).

    The row stays, its area becomes 0 and its status reads "Peak verworfen", so
    the report still documents that the analyst rejected it. Reversible.
    """

    def __init__(self, row_id: int) -> None:
        super().__init__()
        self.row_id = row_id
        self.label = f"Peak verwerfen {row_id}"

    def _perform(self, sample) -> None:
        row = self._row(sample, self.row_id)
        validate = getattr(sample, "validate_edit", None)
        if callable(validate):
            # Zeroing a quantification standard divides by zero downstream.
            validate(row, "raw_area", 0.0)
        self._capture(sample, [row])
        self.label = f"Peak verwerfen {row.peak_no}"

        integ = Integration.from_row(row)
        integ.origin = M.ORIGIN_DISABLED
        integ.apply_to_row(row)
        row.raw_area = 0.0
        _sync_edited(row, "raw_area")
        review_note(row)
        _refresh(sample)


class DeletePeak(IntegrationCommand):
    """Remove a row the analyst created (``－ Peak`` on a MANUAL/SPLIT row).

    A ChemStation peak is refused here on purpose: §VI.8 disables it instead,
    so the report keeps the evidence that it existed and was rejected.
    """

    structural = True

    def __init__(self, row_id: int) -> None:
        super().__init__()
        self.row_id = row_id
        self.label = f"Peak löschen {row_id}"

    def _perform(self, sample) -> None:
        fid = _fid()
        row = self._row(sample, self.row_id)
        if row.integration_origin == M.ORIGIN_CHEMSTATION:
            raise ValueError(
                f"Peak {row.peak_no} stammt aus der Geräteintegration und wird "
                f"verworfen statt gelöscht, damit der Bericht die Verwerfung "
                f"dokumentiert.")
        self._capture(sample)
        self.label = f"Peak löschen {row.peak_no}"
        fid.drop_row(sample, row)
        _refresh(sample)


class DeleteRows(IntegrationCommand):
    """Remove a whole selection of peaks in one step (§VI.8, §IX.1).

    Per row it does exactly what :class:`DeletePeak` and :class:`DisablePeak`
    do on their own -- a peak the instrument found is disabled so the report
    keeps documenting the rejection, one the analyst created is removed -- but
    the block is one command, for two reasons. The analyst made one selection
    and answered one dialog, so one ``Strg+Z`` has to take it back; and routing
    thirty rows through thirty commands would run thirty requantifications and
    thirty QC passes for a single gesture.

    ``structural`` because deletion renumbers the sample: capturing every row,
    not only the named ones, is what lets undo put the whole set back. It is
    also what makes mixing the two per-row policies safe -- :class:`DisablePeak`
    alone is not structural, and half a capture would not survive a renumber.
    """

    structural = True

    def __init__(self, row_ids: Sequence[int]) -> None:
        super().__init__()
        self.row_ids = [int(r) for r in row_ids]
        #: ``(row_id, peak_no, "delete" | "disable")`` per row that changed, so
        #: the workspace can write one audit record each and say which happened.
        self.outcomes: list[tuple[int, int, str]] = []
        self.failures: list[tuple[int, str]] = []
        self.label = f"{len(self.row_ids)} Zeilen entfernen"

    def _perform(self, sample) -> None:
        fid = _fid()
        rows = []
        for row_id in dict.fromkeys(self.row_ids):
            row = sample.row(row_id)
            if row is None:
                self.failures.append(
                    (row_id, f"Zeile {row_id} existiert nicht mehr."))
                continue
            rows.append(row)
        if not rows:
            raise ValueError("Keine der gewählten Zeilen existiert noch.")
        self._capture(sample)

        # Descending, so removing one row cannot shift the next one out from
        # under the loop.
        for row in sorted(rows, key=lambda r: r.peak_no, reverse=True):
            try:
                if (row.integration_origin == M.ORIGIN_CHEMSTATION
                        or hasattr(row, "integration_loaded")):
                    self._disable(sample, row)
                    self.outcomes.append((row.row_id, row.peak_no, "disable"))
                else:
                    peak_no = row.peak_no
                    fid.drop_row(sample, row)
                    self.outcomes.append((row.row_id, peak_no, "delete"))
            except ValueError as exc:
                # A quantification standard refuses to be zeroed; the rest of
                # the selection is still removed.
                self.failures.append((row.row_id, str(exc)))

        if not self.outcomes:
            raise ValueError(self.failures[0][1] if self.failures
                             else "Keine Zeile wurde entfernt.")
        deleted = sum(1 for _r, _p, a in self.outcomes if a == "delete")
        disabled = len(self.outcomes) - deleted
        if len(self.outcomes) == 1:
            row_id, peak_no, action = self.outcomes[0]
            self.label = (f"Peak löschen {peak_no}" if action == "delete"
                          else f"Peak verwerfen {peak_no}")
        else:
            parts = ([f"{deleted} gelöscht"] if deleted else []) \
                + ([f"{disabled} verworfen"] if disabled else [])
            self.label = "Zeilen entfernen (" + ", ".join(parts) + ")"
        _refresh(sample)

    @staticmethod
    def _disable(sample, row) -> None:
        """:class:`DisablePeak`'s body, without its own capture and refresh."""
        validate = getattr(sample, "validate_edit", None)
        if callable(validate):
            # Zeroing a quantification standard divides by zero downstream.
            validate(row, "raw_area", 0.0)
        integ = Integration.from_row(row)
        integ.origin = M.ORIGIN_DISABLED
        integ.apply_to_row(row)
        row.raw_area = 0.0
        _sync_edited(row, "raw_area")
        review_note(row)


class MergePeaks(IntegrationCommand):
    """Integrate two adjacent rows as one (``⋈ Verbinden``).

    The earlier row survives and keeps its identity; the later one is removed
    (§VI.6). Merging a ChemStation row is allowed -- unlike deletion, nothing
    is lost, the two areas become one documented number.
    """

    structural = True

    def __init__(self, row_id: int, other_row_id: int) -> None:
        super().__init__()
        self.row_id = row_id
        self.other_row_id = other_row_id
        self.label = f"Peaks verbinden {row_id}+{other_row_id}"

    def _perform(self, sample) -> None:
        fid = _fid()
        first = self._row(sample, self.row_id)
        second = self._row(sample, self.other_row_id)
        if first.row_id == second.row_id:
            raise ValueError("Ein Peak kann nicht mit sich selbst verbunden werden.")
        a, b = Integration.from_row(first), Integration.from_row(second)
        if a.start > b.start:
            first, second, a, b = second, first, b, a
        merged = merge(a, b)

        trace = trace_of(sample)
        self._capture(sample)
        self.label = f"Peaks verbinden {first.peak_no}+{second.peak_no}"

        write_result(sample, first, merged, integrate(trace, merged))
        review_note(first)
        fid.drop_row(sample, second)
        fid.reassign_row(sample, first)
        fid.rematch_blank(sample, first)
        _refresh(sample)


def _split_fragments_of(sample, row) -> list[Any]:
    """The fragments a split left behind on ``row``; empty when there are none.

    Identity, not numbering: ``derived["split_parent"]`` is written by
    ``gc_fid.new_row`` for every fragment -- :class:`Split` and
    :class:`SplitByComponents` alike -- and unlike the ``74a``/``74b`` FID
    numbers or ``peak_no`` it survives every renumbering.
    """
    row_id = getattr(row, "row_id", None)
    return [r for r in sample.rows
            if r is not row
            and (getattr(r, "derived", None) or {}).get("split_parent") == row_id]


def _reset_refusal(sample, row) -> Optional[str]:
    """Why ``⟳ Zurücksetzen`` cannot restore ``row``, or ``None`` (§VI.8).

    See :class:`ResetPeak` for why these rows are refused rather than
    half-restored. The split cases are tested first: a fragment has no loaded
    area either, and "you split this peak" is the truer sentence than "this row
    was never loaded".
    """
    derived = getattr(row, "derived", None) or {}
    original = getattr(row, "original", None) or {}
    if derived.get("split_parent") is not None:
        return (f"Peak {row.peak_no} ist das Fragment eines geteilten Peaks "
                f"und kann nicht einzeln zurückgesetzt werden — die Teilung "
                f"wird mit Rückgängig (Strg+Z) aufgehoben.")
    if _split_fragments_of(sample, row):
        return (f"Peak {row.peak_no} wurde geteilt; ihn allein zurückzusetzen "
                f"würde die geteilte Fläche doppelt zählen — die Teilung wird "
                f"mit Rückgängig (Strg+Z) aufgehoben.")
    if original.get("raw_area") is None:
        return (f"Peak {row.peak_no} stammt nicht aus der Geräteintegration "
                f"und hat keine geladene Integration, die zurückgesetzt "
                f"werden könnte.")
    return None


class ResetPeak(IntegrationCommand):
    """Restore one row's loaded integration (``⟳ Zurücksetzen``, §VI.8).

    Bounds, area and height come back from the load-time snapshot; the baseline
    is re-derived by the rule that produced it at load, so there is no second
    copy of it that could drift out of sync.

    Two kinds of row have no loaded integration to come back to, and both are
    refused by :func:`_reset_refusal` rather than half-restored:

    * **a row that belongs to a split.** Reset is per row, a split is not. On
      the fragment the shared area falls away; on the parent its *pre-split*
      area comes back while the fragment keeps its own, so the area is counted
      twice. Either way the determination's total FID area moves by the full
      amount of a fragment -- and that total is the number §VI.5 quantifies
      everything against. Undo (``Strg+Z``) is the gesture that takes a split
      back coherently, and the refusal says so.
    * **a row the analyst created.** ``gc_fid.new_row`` calls ``snapshot()``
      before the caller writes the area, so such a row's ``original`` claims an
      empty integration: resetting to it empties the area to ``None`` ("never
      integrated"), not to ``0.0`` ("rejected"). Worse, the origin fall-back
      below would then declare a peak the analyst added to be a ChemStation
      peak, after which :class:`DeletePeak` refuses to delete it. ``original``
      with no ``raw_area`` is exactly :func:`is_untouched`'s test for "there is
      no load-time integration here" and is used for the same reason.

    The refusal is a ``ValueError``, not a ``refusal`` attribute like
    :class:`SplitByComponents`', and that is deliberate:
    ``gc_workspace.on_integration`` -- the only caller that builds this command
    -- puts an exception on the status line and, because ``IntegrationHistory``
    records a command only after its ``do`` returned, keeps the refused command
    out of the history. It never reads ``refusal`` (only
    ``on_split_components`` does), so a refusal set that way would be silent
    *and* would still cost the analyst a ``Strg+Z``. :class:`DeletePeak`
    refuses its own case the same way.
    """

    def __init__(self, row_id: int) -> None:
        super().__init__()
        self.row_id = row_id
        self.label = f"Integration zurücksetzen Peak {row_id}"

    def _perform(self, sample) -> None:
        row = self._row(sample, self.row_id)
        reason = _reset_refusal(sample, row)
        if reason:
            # Before ``_capture``: a refused command has touched nothing, so
            # its undo stays the no-op the base class makes of an empty state.
            raise ValueError(reason)
        self._capture(sample, [row])
        self.label = f"Integration zurücksetzen Peak {row.peak_no}"

        for name in ("fid_start", "fid_end", "raw_area", "area", "height"):
            if name in row.original:
                row.reset_field(name)
        row.fid_anchor_l = row.fid_anchor_r = None
        # ORIGIN_MANUAL belongs in this set: ``SetBounds`` puts it on a
        # ChemStation row whose bound the analyst *dragged*, and undragging it
        # is the whole point of a reset. A row the analyst *created* carries
        # the same origin but never reaches this line -- ``_reset_refusal``
        # turns it away above, which is what keeps it deletable.
        if row.integration_origin in (M.ORIGIN_CHEMSTATION, M.ORIGIN_DISABLED,
                                      M.ORIGIN_MANUAL, M.ORIGIN_MERGED):
            row.integration_origin = M.ORIGIN_CHEMSTATION
        row.fid_baseline = default_baseline(sample, row)
        _refresh(sample)


class CopyIntegration(IntegrationCommand):
    """Apply a prepared copy plan to the other determination (§VI.9).

    The command deliberately takes a finished plan -- ``(row_id, Integration)``
    pairs the dialog built and the analyst confirmed -- rather than doing the
    partner matching itself. Nothing is written before that confirmation, and
    the matching rules belong to the report's pairing logic (§11.6).
    """

    def __init__(self, plan: Sequence[tuple[int, Integration]],
                 label: str = "") -> None:
        super().__init__()
        self.plan = [(int(rid), integ) for rid, integ in plan]
        self.label = label or f"Integration übertragen ({len(self.plan)} Peaks)"

    def _perform(self, sample) -> None:
        trace = trace_of(sample)
        rows = [self._row(sample, rid) for rid, _ in self.plan]
        self._capture(sample, rows)
        for row, (_, integ) in zip(rows, self.plan):
            start, end = self._clamp(trace, integ.start, integ.end)
            # The destination keeps its own scale; only the geometry travels.
            current = Integration.from_row(row)
            moved = replace(integ, start=start, end=end,
                            scale=current.scale,
                            area_ref=None, raw_ref=None,
                            origin=M.ORIGIN_MANUAL)
            write_result(sample, row, moved, integrate(trace, moved))
            review_note(row)
        _refresh(sample)


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

class IntegrationHistory:
    """Undo/redo stack for one sample, depth :data:`HISTORY_DEPTH` (§VI.8).

    Not persisted: the session file stores the resulting state and the audit
    records, so a reopened session starts with a clean history rather than a
    stack of commands whose captured rows may no longer exist.
    """

    def __init__(self, depth: int = HISTORY_DEPTH) -> None:
        self.depth = int(depth)
        self._done: list[IntegrationCommand] = []
        self._undone: list[IntegrationCommand] = []

    def push(self, cmd: IntegrationCommand, sample) -> None:
        """Run ``cmd`` and record it. A failed command is not recorded."""
        cmd.do(sample)
        self._done.append(cmd)
        if self.depth > 0 and len(self._done) > self.depth:
            del self._done[:-self.depth]
        self._undone.clear()

    def undo(self, sample) -> Optional[str]:
        if not self._done:
            return None
        cmd = self._done.pop()
        cmd.undo(sample)
        self._undone.append(cmd)
        return cmd.label

    def redo(self, sample) -> Optional[str]:
        if not self._undone:
            return None
        cmd = self._undone.pop()
        cmd.do(sample)
        self._done.append(cmd)
        return cmd.label

    def clear(self) -> None:
        self._done.clear()
        self._undone.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._done)

    @property
    def can_redo(self) -> bool:
        return bool(self._undone)

    @property
    def label(self) -> str:
        """Label of the command Undo would reverse, empty when there is none."""
        return self._done[-1].label if self._done else ""

    def __len__(self) -> int:
        return len(self._done)
