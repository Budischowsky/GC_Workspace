"""The closer look of :mod:`gcws.ms.deconv_probe`, computed a whole window at a time.

:func:`gcws.ms.deconv_probe.probe_reference` walks the window ion by ion with the vendored
engine's helpers and solves one Lawson-Hanson NNLS per mass, twice (the residual pass and the
purification); a peak took about a quarter of a second. Here the same steps run on the array
building blocks of :mod:`gcws.ms.deconv_fast`: perception of all ions at once, the grouping of
seed blocks, and one batched NNLS (block principal pivoting) per pass.

The perceptions and the groups are the reference's; the floating-point figures agree up to
rounding. The residual, however, comes from a different NNLS routine, so a decision taken on it
could differ when it sits within rounding of its threshold. Every such decision is checked
(``EPS``); when one is borderline, :func:`probe_fast` says so and the caller computes the peak
with the reference instead.
"""
from __future__ import annotations

import dataclasses

import numpy as np

import gc_deconv as legacy

from gcws.ms import deconv as D
from gcws.ms import deconv_fast as F

#: relative distance to a threshold below which a decision counts as borderline
EPS = 1e-9


class _Flag:
    ok = True


def _near(value: float, limit: float) -> bool:
    return abs(value - limit) <= EPS * max(1.0, abs(limit))


def gate_borderline(x: np.ndarray, diff: np.ndarray, residual: np.ndarray, sigma: np.ndarray,
                    noise_factor: float) -> bool:
    """True when the ion gate of the residual perception (``MIN_ION_SCANS`` nonzero scans and a
    maximum of ``noise_factor`` sigma) could decide differently for a residual equal up to
    rounding: a scan count that depends on entries that are zero only by rounding, or a maximum
    within ``EPS`` of the noise threshold. Shapes and amplitudes are non-negative, so where the
    data are zero the residual is zero in either routine, and where the fit is exactly zero
    (outside every model shape) it is the data itself: those entries are certain."""
    tol = EPS * np.maximum(np.abs(x).max(axis=0), 1e-300)
    sure = np.count_nonzero(diff > tol, axis=0)
    unsure = np.count_nonzero((np.abs(diff) <= tol) & (x > 0.0) & (x - diff != 0.0), axis=0)
    threshold = noise_factor * np.asarray(sigma, dtype=float)
    top = residual.max(axis=0)
    count = (sure < legacy.MIN_ION_SCANS) & (sure + unsure >= legacy.MIN_ION_SCANS) \
        & (top >= threshold * (1.0 - EPS))
    level = (threshold > 0) & (np.abs(top - threshold) <= EPS * threshold)
    return bool((count | level).any())


def maxima_borderline(x: np.ndarray, diff: np.ndarray, residual: np.ndarray, sigma: np.ndarray,
                      noise_factor: float) -> bool:
    """True when the residual perception could find its maxima differently for a residual equal up
    to rounding. Two cases are ties that rounding decides:

    * on a stretch of zeros, an entry that is zero only by rounding (see :func:`gate_borderline`)
      decides which of the flat points is the maximum, and the undershoot of the smoothing around
      it can make that a peak of ``noise_factor`` sigma;
    * a maximum as high as another point of its trace (a flat, saturated top): which of them is
      higher decides where the flank walk stops, and so the peak's base and whether it is kept."""
    tol = EPS * np.maximum(np.abs(x).max(axis=0), 1e-300)
    cols = np.flatnonzero(~(residual.max(axis=0) < noise_factor * sigma * (1.0 - EPS)))
    if cols.size == 0:
        return False
    s = F._smooth(residual, cols)
    n = s.shape[1]
    t = 2.0 * tol[cols][:, None]
    left, here, right = s[:, :-2], s[:, 1:-1], s[:, 2:]
    loose = (here > left - t) & (here >= right - t)
    tight = (here > left + t) & (here >= right + t)
    tall = (here - s.min(axis=1)[:, None]) >= noise_factor * sigma[cols][:, None] * (1.0 - EPS)

    def touched(entries):
        """The smoothed points within reach of ``entries`` (the edge points take the first and
        last five)."""
        near = entries[:, cols].T
        out = near.copy()
        for k in (1, 2):
            out[:, k:] |= near[:, :-k]
            out[:, :-k] |= near[:, k:]
        out[:, :2] |= near[:, :5].any(axis=1)[:, None]
        out[:, n - 2:] |= near[:, n - 5:].any(axis=1)[:, None]
        return out

    fitted = x - diff != 0.0
    zero = touched((np.abs(diff) <= tol) & (x > 0.0) & fitted)
    if (loose & ~tight & tall & (zero[:, :-2] | zero[:, 1:-1] | zero[:, 2:])).any():
        return True
    # points with another point of their trace within rounding, one of them computed from the fit
    # (where the fit is exactly zero, the residual is the data in either routine)
    rounded = touched(fitted & (diff > -tol))
    order = np.argsort(s, axis=1, kind="stable")
    close = np.diff(np.take_along_axis(s, order, axis=1), axis=1) <= t
    by_value = np.take_along_axis(rounded, order, axis=1)
    close &= by_value[:, :-1] | by_value[:, 1:]
    pair = np.zeros(s.shape, dtype=bool)
    pair[:, :-1] |= close
    pair[:, 1:] |= close
    twin = np.zeros(s.shape, dtype=bool)
    np.put_along_axis(twin, order, pair, axis=1)
    return bool((loose & tall & (here > t) & twin[:, 1:-1]).any())


class _Legacy:
    """The vendored perception of single ions of the window, for ties that rounding decides.

    Ions of one pure component have proportional profiles and so the same half-height width; which
    of them the reference takes as the model is then decided by the rounding of its own scalar
    arithmetic. Recomputing just the tied ions the reference's way gives its order exactly. This
    works for the first perception only (the residual comes from a different NNLS routine)."""

    def __init__(self, x: np.ndarray, mzs: np.ndarray, params, first: int):
        self.x, self.mzs, self.params, self.first = x, mzs, params, first
        self._sigmas = None

    def sigma(self, c: int) -> float:
        if self._sigmas is None:
            sig = np.array([legacy._ion_sigma(self.x[:, j]) for j in range(self.mzs.size)])
            self._sigmas = np.maximum(sig, legacy._sigma_floor(sig))
        return float(self._sigmas[c])

    def order(self, p: F._Peaks, tied: list[int]) -> list[int] | None:
        """``tied`` in the reference's model order, or None when that cannot be told."""
        keys = {}
        for k in tied:
            if k >= self.first:
                return None
            c = int(np.searchsorted(self.mzs, p.mz[k]))
            found = [q for q in legacy._perceive_ion(int(p.mz[k]), c, self.x[:, c], self.sigma(c), self.params)
                     if q.apex == int(p.apex[k])]
            if len(found) != 1:
                return None
            q = found[0]
            keys[k] = (q.width_half, -q.score, q.mz)
        return sorted(tied, key=lambda k: keys[k])


def _clean(p: F._Peaks, group: list[int], flag: _Flag, min_share: float, ref: _Legacy) -> list[int]:
    """``deconv_probe._clean_model`` on peak indices: the narrowest strong ion is the model."""
    limit = min_share * float(p.s_n[group[0]])
    if any(abs(float(p.s_n[k]) - limit) <= EPS * abs(limit) for k in group):
        flag.ok = False
    strong = [k for k in group if p.s_n[k] >= limit]
    ranked = sorted(strong, key=lambda k: (p.width_half[k], -p.score[k], p.mz[k]))
    w0 = float(p.width_half[ranked[0]])
    tied = [k for k in ranked if float(p.width_half[k]) - w0 <= EPS * abs(w0)]
    model = ranked[0]
    if len(tied) > 1:
        order = ref.order(p, tied)
        if order is None:
            flag.ok = False
        else:
            model = order[0]
    return [model] + [k for k in group if k != model]


def _concat(p: F._Peaks, q: F._Peaks) -> tuple[F._Peaks, int]:
    """One peak table of ``p`` then ``q`` (``q``'s rows point behind ``p``'s smoothed traces)."""
    fields = {f.name: np.concatenate([getattr(p, f.name), getattr(q, f.name)])
              for f in dataclasses.fields(F._Peaks) if f.name not in ("smooth", "row")}
    merged = F._Peaks(smooth=np.vstack([p.smooth, q.smooth]),
                      row=np.concatenate([p.row, q.row + p.smooth.shape[0]]), **fields)
    return merged, int(p.mz.size)


def _groups(x: np.ndarray, mzs: np.ndarray, sigma: np.ndarray, n: int, params) -> list:
    """The residual's components as ordered ``(m/z, apex)`` lists (for the sensitivity test)."""
    q = F._perceive_ions(x, mzs, sigma, np.count_nonzero(x, axis=0), params)
    if q is None:
        return []
    return [[(int(q.mz[k]), int(q.apex[k])) for k in g] for g in F._perceive_components(q, n, params)]


def _shapes(p: F._Peaks, groups: list, n: int) -> np.ndarray:
    return np.column_stack([F._model_shape(p, g[0], n) for g in groups])


def probe_fast(ms, t0: float, t1: float, apex: float,
               settings: D.DeconvSettings | None = None) -> tuple[list[D.Component], bool]:
    """``(components, robust)`` of the closer look at the peak ``t0``..``t1`` (MS time, apex
    ``apex``); ``robust`` is False when a decision lies within rounding of its threshold."""
    from gcws.ms import deconv_probe as P
    flag = _Flag()
    params = P.params_of(settings)
    scan_rt = np.asarray(ms.rt, dtype=float)
    sel = np.flatnonzero(np.abs(scan_rt - apex) <= params.window)
    if sel.size < 5:
        return [], True
    lo, hi = int(sel[0]), int(sel[-1])
    win_rt = scan_rt[lo:hi + 1]
    n = win_rt.size
    x, mzs = F._ion_matrix(ms, lo, hi)
    if mzs.size == 0:
        return [], True
    sigma, nonzero = F._ion_sigmas(x)
    sigma = np.maximum(sigma, legacy._sigma_floor(sigma))
    p = F._perceive_ions(x, mzs, sigma, nonzero, params)
    ref = _Legacy(x, mzs, params, int(p.mz.size) if p is not None else 0)
    groups = [_clean(p, g, flag, P.MODEL_MIN_SN_SHARE, ref) for g in F._perceive_components(p, n, params)] \
        if p is not None else []
    if not groups:
        return P._sensitive_window(ms, t0, t1, apex, settings, []), flag.ok
    cols = np.flatnonzero(x.any(axis=0))
    a = _shapes(p, groups, n)
    fitted = np.zeros_like(x)
    fitted[:, cols] = a @ F.nnls_columns(a, x[:, cols])
    diff = x - fitted
    residual = np.maximum(diff, 0.0)
    if gate_borderline(x, diff, residual, sigma, params.noise_factor) \
            or maxima_borderline(x, diff, residual, sigma, params.noise_factor):
        flag.ok = False
    q = F._perceive_ions(residual, mzs, sigma, np.count_nonzero(residual, axis=0), params)
    if q is not None:
        nominal = _groups(residual, mzs, sigma, n, params)
        for sign in (1.0, -1.0):
            moved = dataclasses.replace(params, noise_factor=params.noise_factor * (1.0 + sign * EPS),
                                        shape_r=params.shape_r - sign * EPS,
                                        apex_tolerance=params.apex_tolerance + sign * EPS)
            if _groups(residual, mzs, sigma, n, moved) != nominal:
                flag.ok = False
        merged, offset = _concat(p, q)
        index = np.arange(n, dtype=float)
        for g in F._perceive_components(q, n, params):
            g = [k + offset for k in g]
            sub = float(merged.apex_sub[g[0]])
            rt = float(np.interp(sub, index, win_rt))
            if _near(rt, t0) or _near(rt, t1):
                flag.ok = False
            if not t0 <= rt <= t1:
                continue
            gaps = [abs(sub - float(merged.apex_sub[h[0]])) for h in groups]
            if any(abs(gap - 2 * legacy.MIN_SEPARATION_SCANS) <= EPS for gap in gaps):
                flag.ok = False
            if all(gap >= 2 * legacy.MIN_SEPARATION_SCANS for gap in gaps):
                groups.append(g)
        groups.sort(key=lambda g: (merged.apex_sub[g[0]], -merged.score[g[0]], merged.mz[g[0]]))
        groups = [_clean(merged, g, flag, P.MODEL_MIN_SN_SHARE, ref) for g in groups[:legacy.MAX_COMPONENTS]]
        p = merged
    a = _shapes(p, groups, n)
    contrib = np.zeros((len(groups), mzs.size), dtype=float)
    contrib[:, cols] = F.nnls_columns(a, x[:, cols])
    F.settle_spectrum_ties(a, x, contrib)
    comps = [D.Component(**vars(c)) for c in F._components(p, groups, a, contrib, x.sum(axis=1), mzs, win_rt, lo)]
    if any(_near(c.rt, t0) or _near(c.rt, t1) for c in comps):
        flag.ok = False
    inside = [c for c in comps if t0 <= c.rt <= t1]
    total = sum(c.area for c in inside)
    sensitive = settings is not None and settings.noise_factor < 2.0
    min_share = 0.001 if sensitive else P.MIN_AREA_SHARE
    min_sn = 1.5 if sensitive else P.MIN_SN
    if any(abs(c.area - min_share * total) <= EPS * abs(total) or abs(c.s_n - min_sn) <= EPS * min_sn
           for c in inside):
        flag.ok = False
    selected = [c for c in inside if c.area >= min_share * total and c.s_n >= min_sn]
    return P._sensitive_window(ms, t0, t1, apex, settings, selected), flag.ok
