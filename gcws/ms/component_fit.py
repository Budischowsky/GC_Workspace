"""Fit deconvoluted MS component shapes to a detector trace (FID or TIC).

The MS deconvolution tells how many components a peak holds, where they elute
and how their elution profiles look. It cannot tell how the FID responds to
each of them. This module maps the component profiles onto the trace's time
axis with one common time shift and one common width factor and fits them to
the measured trace with non-negative amplitudes. The fitted curves give each
component's share of the trace signal, and their crossings are the natural
cut points between the fragments.

Components with alike profiles a scan or so apart (a deuterated standard and
its weak D(n-1)H isotopologue) make the alignment ambiguous: either of them
explains the trace's peak about equally well, each with its own shift. The
trace cannot decide which component it shows, so where the MS signal as a
whole lines up with the trace decides (:func:`fit_trace_uncached`).

numpy only (scipy is not installed): a Fritsch-Carlson PCHIP interpolant and
the Lawson-Hanson NNLS of the vendored NIAS engine. Deterministic: the grid
search visits the same points in the same order and keeps the first minimum.
Fits are cached by the content of their inputs, so a re-integration only fits
the peaks whose trace window or components changed.
"""
from __future__ import annotations

import dataclasses
import hashlib
import threading
from collections import OrderedDict
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
#: Alignments whose R² differ by less than this explain the trace equally well
#: (within the mismatch of an MS elution profile and the trace's peak shape).
ALIGN_TIE_R2 = 0.005
#: A coarse-grid start this far below the best coarse R² is not refined: it
#: cannot become an equally good alignment (seen on real runs: at most 0.04).
ALIGN_START_R2 = 0.1


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


def _unit_columns(a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``a`` with each nonzero column scaled to a maximum of 1, and the scales.

    The NNLS of the NIAS engine takes a value below ``m * eps * max|A| * max|b|`` for zero; it is
    sized for the engine's unit-maximum model shapes. MS profiles of millions of counts fitted to a
    trace of a few hundred thousand pA have amplitudes far below that tolerance, so the columns are
    scaled to unit maximum first (the amplitudes are scaled back)."""
    scale = np.abs(a).max(axis=0)
    scale = np.where(scale > 0, scale, 1.0)
    return a / scale, scale


def _residual(a: np.ndarray, y: np.ndarray) -> tuple[float, np.ndarray]:
    if not a.any():
        return float(y @ y), np.zeros(a.shape[1])
    a, scale = _unit_columns(a)
    x = _nias.nnls(a, y)
    r = y - a @ x
    return float(r @ r), x / scale


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


#: fits kept for reuse (a whole run has a few hundred; each holds a few small arrays)
FIT_CACHE_SIZE = 4096
_FIT_CACHE: "OrderedDict[bytes, TraceFit]" = OrderedDict()
_FIT_LOCK = threading.Lock()


def fit_cache_clear() -> None:
    with _FIT_LOCK:
        _FIT_CACHE.clear()


def _fit_key(t, y, shapes: Sequence[Shape], shift0: float, scan_dt: Optional[float],
             mask: Optional[np.ndarray]) -> bytes:
    """Digest of everything :func:`fit_trace_uncached` reads (a shape's slopes follow from t, y)."""
    h = hashlib.blake2b(digest_size=16)
    t = np.ascontiguousarray(t, dtype=float)
    h.update(t.tobytes())
    h.update(b"|")
    h.update(np.ascontiguousarray(y, dtype=float).tobytes())
    h.update(b"|")
    use = np.ones(t.size, dtype=bool) if mask is None else np.ascontiguousarray(mask, dtype=bool)
    h.update(use.tobytes())
    for s in shapes:
        h.update(b"#")
        h.update(repr(float(s.rt)).encode())
        h.update(b"|")
        h.update(np.ascontiguousarray(s.t, dtype=float).tobytes())
        h.update(b"|")
        h.update(np.ascontiguousarray(s.y, dtype=float).tobytes())
    h.update(b"#")
    h.update(repr((float(shift0), None if scan_dt is None else float(scan_dt))).encode())
    return h.digest()


def _copy(fit: TraceFit) -> TraceFit:
    return dataclasses.replace(fit, amplitudes=fit.amplitudes.copy(), curves=fit.curves.copy(),
                               areas=fit.areas.copy())


def fit_trace(t, y, shapes: Sequence[Shape], shift0: float, scan_dt: Optional[float] = None,
              mask: Optional[np.ndarray] = None) -> TraceFit:
    """:func:`fit_trace_uncached`, cached by the content of the inputs; every caller gets its own copy."""
    if not shapes:
        return fit_trace_uncached(t, y, shapes, shift0, scan_dt, mask)
    key = _fit_key(t, y, shapes, shift0, scan_dt, mask)
    with _FIT_LOCK:
        fit = _FIT_CACHE.get(key)
        if fit is not None:
            _FIT_CACHE.move_to_end(key)
            return _copy(fit)
    fit = fit_trace_uncached(t, y, shapes, shift0, scan_dt, mask)
    with _FIT_LOCK:
        _FIT_CACHE[key] = fit
        _FIT_CACHE.move_to_end(key)
        while len(_FIT_CACHE) > FIT_CACHE_SIZE:
            _FIT_CACHE.popitem(last=False)
    return _copy(fit)


def _curves(shape: Shape, t: np.ndarray, pairs) -> np.ndarray:
    """:func:`curve` of ``shape`` on ``t`` for many (shift, log_k) grid points, one row per point:
    the same element-wise operations, the width factor ``float(np.exp(log_k))`` as in the search."""
    stretches = np.array([float(np.exp(log_k)) for _shift, log_k in pairs])
    shifts = np.array([float(shift) for shift, _log_k in pairs])
    src = shape.rt + (t[None, :] - shifts[:, None] - shape.rt) / stretches[:, None]
    out = np.zeros(src.shape)
    inside = (src > shape.t[0]) & (src < shape.t[-1])
    if inside.any():
        out[inside] = np.maximum(pchip_eval(shape.t, shape.y, shape.d, src[inside]), 0.0)
    return out


def _ss_column(columns: np.ndarray, y: np.ndarray) -> list:
    """``_residual(column[:, None], y)[0]`` for each row of ``columns``, value for value.

    With one column the Lawson-Hanson NNLS of the NIAS engine comes down to one pseudo-inverse
    product: x = pinv(a) @ y when both a.T @ y and that product exceed the engine's tolerance,
    else 0. The pseudo-inverses are taken in one stacked call (LAPACK factors each matrix on its
    own); every product, tolerance and residual is the engine's own operation, on the column
    scaled to unit maximum as :func:`_residual` scales it."""
    out = [0.0] * len(columns)
    pending = []
    m = y.size
    eps = np.finfo(float).eps
    ymax = None
    for i in range(len(columns)):
        a = columns[i].reshape(-1, 1)
        if not a.any():
            out[i] = float(y @ y)
            continue
        a = _unit_columns(a)[0]
        if ymax is None:
            ymax = float(max(np.abs(y).max(), 1.0))
        tol = max(m, 1) * eps * (float(max(np.abs(a).max(), 1.0)) * ymax)
        if (a.T @ y)[0] <= tol:
            r = y - a @ np.zeros(1)
            out[i] = float(r @ r)
        else:
            pending.append((i, a, tol))
    if pending:
        inverse = np.linalg.pinv(np.stack([a for _i, a, _tol in pending]))
        for (i, a, tol), pinv in zip(pending, inverse):
            s = np.zeros(1)
            s[[0]] = pinv @ y
            x = s if s[0] > tol else np.zeros(1)
            x[x < 0.0] = 0.0
            r = y - a @ x
            out[i] = float(r @ r)
    return out


def _search(ss_many, shift0: float, dt: float) -> tuple[tuple, list[tuple]]:
    """The coarse grid around ``shift0``: its best point ``(ss, shift, log_k)`` (the first minimum)
    and, per shift, the best point over the width factors (the shift profile). ``ss_many`` gives
    the residuals of a list of (shift, log_k) points."""
    lo, hi = STRETCH_RANGE
    logs = np.log(lo) + np.log(hi / lo) / 12 * np.arange(13)
    shifts = _grid(shift0, dt * SHIFT_SPAN_SCANS / 4, 4)
    values = iter(ss_many([(shift, log_k) for shift in shifts for log_k in logs]))
    best, profile = None, []
    for shift in shifts:
        row = None
        for log_k in logs:
            ss = next(values)
            if row is None or ss < row[0]:
                row = (ss, float(shift), float(log_k))
        profile.append(row)
        if best is None or row[0] < best[0]:
            best = row
    return best, profile


def _refine(ss_many, start: tuple, dt: float) -> tuple:
    """Two finer grid levels around ``start``, each with a third of the previous step (the points
    of a level evaluated together)."""
    lo, hi = STRETCH_RANGE
    best = start
    shift_step, log_step = dt * SHIFT_SPAN_SCANS / 4, np.log(hi / lo) / 12
    for _level in range(2):
        shift_step /= 3
        log_step /= 3
        shifts = _grid(best[1], shift_step, 2)
        logs = np.clip(_grid(best[2], log_step, 2), np.log(lo), np.log(hi))
        values = iter(ss_many([(shift, log_k) for shift in shifts for log_k in logs]))
        for shift in shifts:
            for log_k in logs:
                ss = next(values)
                if ss < best[0]:
                    best = (ss, float(shift), float(log_k))
    return best


def _other_minima(profile: list[tuple], best: tuple, yu: np.ndarray) -> list[tuple]:
    """The local minima of the coarse shift profile besides ``best`` worth refining."""
    ss = [p[0] for p in profile]
    total = float(((yu - yu.mean()) ** 2).sum()) if yu.size else 0.0
    out = []
    for j, point in enumerate(profile):
        if point[1] == best[1] or (j > 0 and not ss[j] < ss[j - 1]) or (j + 1 < len(ss) and not ss[j] <= ss[j + 1]):
            continue
        if total > 0 and (point[0] - best[0]) / total > ALIGN_START_R2:
            continue
        out.append(point)
    return out


def fit_trace_uncached(t, y, shapes: Sequence[Shape], shift0: float, scan_dt: Optional[float] = None,
                       mask: Optional[np.ndarray] = None) -> TraceFit:
    """Best common shift and width factor, searched on a coarse grid and refined twice.

    ``shift0`` is the expected detector offset (the FID-MS delay, or 0 for an MS
    trace). ``mask`` excludes points (e.g. skimmed riders) from the fit.

    The other local minima of the coarse search are refined as well. Of the alignments that explain
    the trace equally well (R² within ALIGN_TIE_R2 of the best), the one nearest the shift that lines
    the MS signal as a whole (the sum of the profiles) up with the trace is taken: the coarse grid
    alone would take whichever of them it happens to sample better, which depends on ``shift0``.

    The grid points of a level are evaluated together (:func:`_curves`; one component:
    :func:`_ss_column`), with the same residuals to the last bit as one design matrix and one NNLS
    per point.
    """
    if not shapes:
        raise ValueError("no component shapes to fit")
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    use = np.ones(t.size, dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    tu, yu = t[use], y[use]
    dt = scan_dt or min(s.scan_dt for s in shapes)

    def designs(pairs):
        """The design matrices of the grid points: (points, trace points, shapes)."""
        return np.stack([_curves(s, tu, pairs) for s in shapes], axis=2)

    def ss_many(pairs):
        if len(shapes) == 1:
            return _ss_column(_curves(shapes[0], tu, pairs), yu)
        return [_residual(a, yu)[0] for a in designs(pairs)]

    best, profile = _search(ss_many, shift0, dt)
    found = [_refine(ss_many, best, dt)]
    if len(shapes) > 1:
        found += [_refine(ss_many, start, dt) for start in _other_minima(profile, best, yu)]
    fits = [solve(t, y, shapes, shift, float(np.exp(log_k)), use) for _ss, shift, log_k in found]
    top = max(f.r2 for f in fits)
    tied = [f for f in fits if f.r2 >= top - ALIGN_TIE_R2]
    if len(tied) == 1:
        return tied[0]

    def ms_many(pairs):
        return _ss_column(designs(pairs).sum(axis=2), yu)

    ms_shift = _refine(ms_many, _search(ms_many, shift0, dt)[0], dt)[1]
    return min(tied, key=lambda f: abs(f.shift - ms_shift))


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
