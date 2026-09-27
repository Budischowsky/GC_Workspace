"""Fit deconvoluted MS component shapes to a detector trace (FID or TIC).

The MS deconvolution tells how many components a peak holds, where they elute
and how their elution profiles look. It cannot tell how the FID responds to
each of them. This module maps the component profiles onto the trace's time
axis with one common time shift and one common width factor and fits them to
the measured trace with non-negative amplitudes. The fitted curves give each
component's share of the trace signal, and their crossings are the natural
cut points between the fragments.

numpy only (scipy is not installed): a Fritsch-Carlson PCHIP interpolant and
the Lawson-Hanson NNLS of the vendored NIAS engine. Deterministic: the grid
search visits the same points in the same order and keeps the first minimum.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

import gc_deconv as _nias

#: A fit explaining less of the trace than this is not trusted for the areas;
#: the split then falls back to the MS component proportions.
FIT_MIN_R2 = 0.97
#: Two fitted curves more alike than this cannot be told apart in the trace.
COLLINEAR_COSINE = 0.99
#: Components below either limit are listed but not checked by default.
SUGGEST_MIN_SN = 20.0
SUGGEST_MIN_SHARE = 0.01
#: Search range of the common time shift (MS scans either side of the start
#: value) and of the common width factor (trace width / MS profile width).
SHIFT_SPAN_SCANS = 2.0
STRETCH_RANGE = (0.6, 1.8)


# -- interpolation -------------------------------------------------------------------------------

def _edge_slope(h0: float, h1: float, m0: float, m1: float) -> float:
    d = ((2.0 * h0 + h1) * m0 - h0 * m1) / (h0 + h1)
    if np.sign(d) != np.sign(m0):
        return 0.0
    if np.sign(m0) != np.sign(m1) and abs(d) > abs(3.0 * m0):
        return 3.0 * m0
    return float(d)


def pchip_slopes(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Fritsch-Carlson derivatives: monotone between samples, no overshoot."""
    n = x.size
    if n < 2:
        return np.zeros(n)
    h = np.diff(x)
    delta = np.diff(y) / h
    if n == 2:
        return np.full(n, delta[0])
    d = np.zeros(n)
    w1 = 2.0 * h[1:] + h[:-1]
    w2 = h[1:] + 2.0 * h[:-1]
    same = delta[:-1] * delta[1:] > 0
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = (w1 + w2) / (w1 / delta[:-1] + w2 / delta[1:])
    d[1:-1] = np.where(same, mean, 0.0)
    d[0] = _edge_slope(h[0], h[1], delta[0], delta[1])
    d[-1] = _edge_slope(h[-1], h[-2], delta[-1], delta[-2])
    return d


def pchip_eval(x: np.ndarray, y: np.ndarray, d: np.ndarray, xi: np.ndarray) -> np.ndarray:
    """Evaluate the cubic Hermite interpolant with slopes ``d``; ``xi`` inside ``x``."""
    k = np.clip(np.searchsorted(x, xi, side="right") - 1, 0, x.size - 2)
    h = x[k + 1] - x[k]
    s = (xi - x[k]) / h
    s2, s3 = s * s, s * s * s
    return ((2 * s3 - 3 * s2 + 1) * y[k] + (s3 - 2 * s2 + s) * h * d[k]
            + (-2 * s3 + 3 * s2) * y[k + 1] + (s3 - s2) * h * d[k + 1])


def pchip(x, y, xi) -> np.ndarray:
    x, y, xi = (np.asarray(v, dtype=float) for v in (x, y, xi))
    return pchip_eval(x, y, pchip_slopes(x, y), xi)


# -- component shapes ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Shape:
    """A component's elution profile in MS time: zero at both ends, non-negative."""
    rt: float
    t: np.ndarray
    y: np.ndarray
    d: np.ndarray

    @classmethod
    def from_arrays(cls, rt: float, t, y) -> "Shape":
        t = np.asarray(t, dtype=float)
        y = np.maximum(np.asarray(y, dtype=float), 0.0)
        if t.size != y.size or t.size < 2 or not np.all(np.diff(t) > 0):
            raise ValueError("an elution profile needs at least two samples in time order")
        on = np.flatnonzero(y > 0)
        if on.size == 0:
            raise ValueError("the elution profile is empty")
        lo, hi = max(int(on[0]) - 1, 0), min(int(on[-1]) + 1, t.size - 1)
        t, y = t[lo:hi + 1].copy(), y[lo:hi + 1].copy()
        step = float(np.median(np.diff(t))) if t.size > 1 else 0.0075
        # A profile cut by its deconvolution window still has to end at zero.
        if y[0] > 0:
            t, y = np.concatenate([[t[0] - step], t]), np.concatenate([[0.0], y])
        if y[-1] > 0:
            t, y = np.concatenate([t, [t[-1] + step]]), np.concatenate([y, [0.0]])
        return cls(float(rt), t, y, pchip_slopes(t, y))

    @classmethod
    def of(cls, component) -> Optional["Shape"]:
        """The shape of a deconvolution component, or None without a usable profile."""
        get = component.get if isinstance(component, dict) else (
            lambda k, default=None: getattr(component, k, default))
        try:
            if get("profile") is not None:
                points = np.asarray(get("profile"), dtype=float)
                return cls.from_arrays(get("rt"), points[:, 0], points[:, 1])
            t, y = get("profile_rt"), get("profile_y")
            if t is None or y is None or len(t) < 2:
                return None
            return cls.from_arrays(get("rt"), t, y)
        except (TypeError, ValueError, IndexError):
            return None

    def points(self) -> list[list[float]]:
        return [[float(a), float(b)] for a, b in zip(self.t, self.y)]

    @property
    def scan_dt(self) -> float:
        return float(np.median(np.diff(self.t)))


def curve(shape: Shape, t, shift: float, stretch: float = 1.0) -> np.ndarray:
    """``shape`` on the trace axis ``t``: apex at ``rt + shift``, widths x ``stretch``."""
    t = np.asarray(t, dtype=float)
    src = shape.rt + (t - shift - shape.rt) / stretch
    out = np.zeros(t.shape)
    inside = (src > shape.t[0]) & (src < shape.t[-1])
    if inside.any():
        out[inside] = np.maximum(pchip_eval(shape.t, shape.y, shape.d, src[inside]), 0.0)
    return out


# -- fitting --------------------------------------------------------------------------------------

@dataclass
class TraceFit:
    shift: float
    stretch: float
    amplitudes: np.ndarray
    curves: np.ndarray        # (components, points): the fitted curves on ``t``
    areas: np.ndarray         # integral of each fitted curve over ``t`` (signal x min)
    r2: float
    residual: float           # rms residual / peak height of the fitted data
    collinear: Optional[tuple[int, int]] = None

    @property
    def shares(self) -> np.ndarray:
        total = float(self.areas.sum())
        return self.areas / total if total > 0 else np.zeros_like(self.areas)

    @property
    def total(self) -> np.ndarray:
        return self.curves.sum(axis=0)


def _design(shapes: Sequence[Shape], t: np.ndarray, shift: float, stretch: float) -> np.ndarray:
    return np.column_stack([curve(s, t, shift, stretch) for s in shapes])


def _residual(a: np.ndarray, y: np.ndarray) -> tuple[float, np.ndarray]:
    if not a.any():
        return float(y @ y), np.zeros(a.shape[1])
    x = _nias.nnls(a, y)
    r = y - a @ x
    return float(r @ r), x


def _grid(center: float, step: float, half: int) -> np.ndarray:
    return center + step * np.arange(-half, half + 1)


def solve(t, y, shapes: Sequence[Shape], shift: float, stretch: float,
          mask: Optional[np.ndarray] = None) -> TraceFit:
    """Non-negative amplitudes of ``shapes`` at a fixed time shift and width factor."""
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    use = np.ones(t.size, dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    a = _design(shapes, t, shift, stretch)
    ss, x = _residual(a[use], y[use])
    curves = (a * x).T
    areas = np.array([float(np.trapezoid(c, t)) for c in curves])
    yu = y[use]
    total = float(((yu - yu.mean()) ** 2).sum()) if yu.size else 0.0
    r2 = 1.0 - ss / total if total > 0 else 0.0
    height = float(yu.max()) if yu.size else 0.0
    rms = float(np.sqrt(ss / yu.size)) if yu.size else 0.0
    collinear = None
    au = a[use]
    norms = np.sqrt((au * au).sum(axis=0))
    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            if norms[i] > 0 and norms[j] > 0 and au[:, i] @ au[:, j] / (norms[i] * norms[j]) > COLLINEAR_COSINE:
                collinear = collinear or (i, j)
    return TraceFit(float(shift), float(stretch), x, curves, areas, r2,
                    rms / height if height > 0 else 1.0, collinear)


def fit_trace(t, y, shapes: Sequence[Shape], shift0: float, scan_dt: Optional[float] = None,
              mask: Optional[np.ndarray] = None) -> TraceFit:
    """Best common shift and width factor, searched on a coarse grid and refined twice.

    ``shift0`` is the expected detector offset (the FID-MS delay, or 0 for an MS
    trace). ``mask`` excludes points (e.g. skimmed riders) from the fit.
    """
    if not shapes:
        raise ValueError("no component shapes to fit")
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    use = np.ones(t.size, dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    tu, yu = t[use], y[use]
    dt = scan_dt or min(s.scan_dt for s in shapes)
    lo, hi = STRETCH_RANGE
    shift_step = dt * SHIFT_SPAN_SCANS / 4
    log_step = np.log(hi / lo) / 12
    shifts = _grid(shift0, shift_step, 4)
    logs = np.log(lo) + log_step * np.arange(13)
    best = None
    for _level in range(3):
        for shift in shifts:
            for log_k in logs:
                ss, _x = _residual(_design(shapes, tu, shift, float(np.exp(log_k))), yu)
                if best is None or ss < best[0]:
                    best = (ss, float(shift), float(log_k))
        shift_step /= 3
        log_step /= 3
        shifts = _grid(best[1], shift_step, 2)
        logs = np.clip(_grid(best[2], log_step, 2), np.log(lo), np.log(hi))
    return solve(t, y, shapes, best[1], float(np.exp(best[2])), use)


def cut_points(t, curves: np.ndarray, apexes: Sequence[float]) -> list[float]:
    """Cut times between neighbouring fitted curves (``apexes`` in increasing order).

    The crossing of the two curves between their apexes; without a crossing, the
    lowest point of the summed fit between them, and failing that the midpoint.
    """
    t = np.asarray(t, dtype=float)
    total = np.asarray(curves).sum(axis=0)
    out = []
    for i in range(len(apexes) - 1):
        a, b = float(apexes[i]), float(apexes[i + 1])
        inside = np.flatnonzero((t > a) & (t < b))
        point = None
        if inside.size >= 2:
            diff = curves[i][inside] - curves[i + 1][inside]
            down = np.flatnonzero((diff[:-1] > 0) & (diff[1:] <= 0))
            if down.size:
                j = int(down[0])
                t0, t1 = t[inside[j]], t[inside[j + 1]]
                d0, d1 = diff[j], diff[j + 1]
                point = float(t0 + (t1 - t0) * d0 / (d0 - d1)) if d0 != d1 else float(t0)
            else:
                seg = total[inside]
                k = int(np.argmin(seg))
                if 0 < k < seg.size - 1:
                    point = float(t[inside[k]])
        out.append(point if point is not None else (a + b) / 2)
    return out
