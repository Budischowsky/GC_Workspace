"""The NIAS window deconvolution, computed a whole window at a time.

The vendored ``gc_deconv.deconvolute`` walks a window ion by ion, peak by peak and mass
by mass in Python. A whole run at the highest detection level asks for about 400 windows
and took close to a minute. This module computes the same steps on arrays:

* the window's ion matrix is cut straight from the run's point arrays;
* noise, smoothing and ion-peak perception run for all ions of the window at once;
* the greedy component grouping tests the shapes of a block of seeds in one step;
* the joint purification solves the NNLS of all masses together by block principal
  pivoting, with the stopping tolerances of the original Lawson-Hanson routine.

The vendored engine is the reference and stays unchanged: the same components, model
ions, apex scans, ion counts and spectra, with floating-point figures equal up to
rounding (tests/test_deconv_fast.py).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import gc_deconv as legacy

_EPS = np.finfo(float).eps


def deconvolute(ms, rt: float, params: legacy.DeconvParams) -> list:
    """``gc_deconv.deconvolute`` of the window around ``rt`` for an MSMatrix ``ms``."""
    scan_rt = np.asarray(ms.rt, dtype=float)
    sel = np.flatnonzero(np.abs(scan_rt - rt) <= params.window)
    if sel.size < 5:
        return []
    lo, hi = int(sel[0]), int(sel[-1])
    win_rt = scan_rt[lo:hi + 1]
    n = win_rt.size
    x, mzs = _ion_matrix(ms, lo, hi)
    if mzs.size == 0:
        return []
    sigma, nonzero = _ion_sigmas(x)
    sigma = np.maximum(sigma, legacy._sigma_floor(sigma))
    peaks = _perceive_ions(x, mzs, sigma, nonzero, params)
    if peaks is None:
        return []
    groups = _perceive_components(peaks, n, params)
    if not groups:
        return []
    a = np.column_stack([_model_shape(peaks, g[0], n) for g in groups])
    contrib = np.zeros((len(groups), mzs.size), dtype=float)
    cols = np.flatnonzero(x.any(axis=0))
    contrib[:, cols] = nnls_columns(a, x[:, cols])
    return _components(peaks, groups, a, contrib, x.sum(axis=1), mzs, win_rt, lo)


# --------------------------------------------------------------------------
# Ion matrix, noise, smoothing
# --------------------------------------------------------------------------

def _ion_matrix(ms, lo: int, hi: int) -> tuple[np.ndarray, np.ndarray]:
    """``(X[scan, mass], masses)`` over the nominal masses recorded in scans ``lo..hi``.

    Points are summed in scan order, as the original reader does, so the matrix is the same.
    """
    n = hi - lo + 1
    a, b = int(ms.ptr[lo]), int(ms.ptr[hi + 1])
    if b <= a:
        return np.zeros((n, 0)), np.zeros(0, dtype=np.int64)
    mzs, column = np.unique(ms.nom[a:b], return_inverse=True)
    row = np.repeat(np.arange(n), np.diff(ms.ptr[lo:hi + 2]))
    flat = np.bincount(row * mzs.size + column, weights=ms.ab[a:b], minlength=n * mzs.size)
    return flat.reshape(n, mzs.size), mzs


def _ion_sigmas(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``gc_deconv._ion_sigma`` of every column, and each column's count of nonzero scans."""
    n, m = x.shape
    q = max(4, n // 4)
    nonzero = np.count_nonzero(x, axis=0)
    exact_zero = (nonzero <= n - q) & (x.min(axis=0) >= 0.0)
    sigma = np.zeros(m)
    rest = np.flatnonzero(~exact_zero)
    if rest.size:
        low = np.sort(x[:, rest], axis=0)[:q]
        mad = np.median(np.abs(low - np.median(low, axis=0)), axis=0)
        sigma[rest] = legacy.mad_consistency(n) * mad
    return sigma, nonzero


def _smooth(x: np.ndarray) -> np.ndarray:
    """``gc_deconv.savgol(width=5, order=2)`` of every column of ``x`` (n >= 5), as rows."""
    n, k = x.shape
    kernel = legacy._sg_kernels(5, 2)
    y = np.ascontiguousarray(x.T)
    out = np.empty((k, n))
    # One pass over all traces laid end to end; values straddling two traces are dropped.
    centre = np.empty(k * n)
    valid = np.correlate(y.ravel(), kernel[2], mode="valid")
    centre[:valid.size] = valid
    out[:, 2:n - 2] = centre.reshape(k, n)[:, :n - 4]
    head = np.ascontiguousarray(y[:, :5]).ravel()
    tail = np.ascontiguousarray(y[:, -5:]).ravel()
    out[:, 0] = np.correlate(head, kernel[0], mode="valid")[::5]
    out[:, 1] = np.correlate(head, kernel[1], mode="valid")[::5]
    out[:, n - 2] = np.correlate(tail, kernel[3], mode="valid")[::5]
    out[:, n - 1] = np.correlate(tail, kernel[4], mode="valid")[::5]
    return out


# --------------------------------------------------------------------------
# Ion peaks
# --------------------------------------------------------------------------

@dataclass
class _Peaks:
    """The perceived ion peaks of a window, one array entry per peak."""
    smooth: np.ndarray      # (ions, scans) smoothed traces; ``row`` points into it
    row: np.ndarray
    mz: np.ndarray
    apex: np.ndarray
    apex_sub: np.ndarray
    base: np.ndarray
    width_half: np.ndarray
    s_n: np.ndarray
    score: np.ndarray
    left: np.ndarray
    right: np.ndarray


def _perceive_ions(x, mzs, sigma, nonzero, params) -> _Peaks | None:
    """``gc_deconv._perceive_ion`` for every surviving ion of the window at once."""
    n = x.shape[0]
    cols = np.flatnonzero((nonzero >= legacy.MIN_ION_SCANS)
                          & ~(x.max(axis=0) < params.noise_factor * sigma))
    if cols.size == 0:
        return None
    s = _smooth(x[:, cols])
    is_max = (s[:, 1:-1] > s[:, :-2]) & (s[:, 1:-1] >= s[:, 2:])
    r, i = np.nonzero(is_max)
    i = i + 1
    h = s[r, i]
    sig = sigma[cols][r]
    threshold = params.noise_factor * sig
    # The prominence base is never below the trace minimum: most noise maxima fail here.
    keep = ~((h - s.min(axis=1)[r]) < threshold)
    r, i, h, sig, threshold = r[keep], i[keep], h[keep], sig[keep], threshold[keep]
    if r.size == 0:
        return None

    # Flanking minima: walk out to the first higher point (or the window edge); the
    # minimum nearest the apex wins ties, as in gc_deconv._flanks.
    g = s[r]
    idx = np.arange(n)[None, :]
    apex = i[:, None]
    higher = g > h[:, None]
    before, after = idx < apex, idx > apex
    hl = higher & before
    stop_l = np.where(hl.any(axis=1), n - 1 - np.argmax(hl[:, ::-1], axis=1), -1)
    vl = np.where(before & (idx > stop_l[:, None]), g, np.inf)
    lo_v = vl.min(axis=1)
    left = n - 1 - np.argmax((vl == lo_v[:, None])[:, ::-1], axis=1)
    hr = higher & after
    stop_r = np.where(hr.any(axis=1), np.argmax(hr, axis=1), n)
    vr = np.where(after & (idx < stop_r[:, None]), g, np.inf)
    min_r = vr.min(axis=1)
    lower = min_r < h
    hi_v = np.where(lower, min_r, h)
    right = np.where(lower, np.argmax(vr == min_r[:, None], axis=1), i)
    base = np.maximum(lo_v, hi_v)
    prominence = h - base
    keep = ~(prominence < threshold)
    if not keep.any():
        return None
    r, i, h, sig, g = r[keep], i[keep], h[keep], sig[keep], g[keep]
    left, right, base, prominence = left[keep], right[keep], base[keep], prominence[keep]
    apex = i[:, None]
    k = np.arange(r.size)

    # Sub-scan apex and curvature from the three points around it.
    before_v, after_v = g[k, i - 1], g[k, i + 1]
    den = before_v - 2.0 * h + after_v
    with np.errstate(divide="ignore", invalid="ignore"):
        shift = np.where(den == 0, 0.0, 0.5 * (before_v - after_v) / den)
    shift = np.minimum(0.5, np.maximum(-0.5, shift))
    curvature = 2.0 * h - before_v - after_v

    # Half-height width, interpolated on the smoothed trace (gc_deconv._half_width).
    level = base + 0.5 * prominence
    below = g <= level[:, None]
    ml = (idx >= left[:, None]) & (idx < apex) & below
    has_l = ml.any(axis=1)
    jl = np.where(has_l, n - 1 - np.argmax(ml[:, ::-1], axis=1), 0)
    jl1 = np.minimum(jl + 1, n - 1)
    span_l = g[k, jl1] - g[k, jl]
    mr = (idx > apex) & (idx <= right[:, None]) & below
    has_r = mr.any(axis=1)
    jr = np.where(has_r, np.argmax(mr, axis=1), 1)
    span_r = g[k, jr - 1] - g[k, jr]
    with np.errstate(divide="ignore", invalid="ignore"):
        lo_w = np.where(has_l, np.where(span_l > 0, jl + (level - g[k, jl]) / span_l,
                                        jl1.astype(float)), left.astype(float))
        hi_w = np.where(has_r, np.where(span_r > 0, jr - (level - g[k, jr]) / span_r,
                                        (jr - 1).astype(float)), right.astype(float))
    width = np.maximum(hi_w - lo_w, 1e-6)

    s_n = prominence / sig
    return _Peaks(smooth=s, row=r, mz=mzs[cols][r], apex=i, apex_sub=i + shift, base=base,
                  width_half=width, s_n=s_n,
                  score=(np.maximum(curvature, 0.0) / prominence) * s_n, left=left, right=right)


# --------------------------------------------------------------------------
# Component perception
# --------------------------------------------------------------------------

def _perceive_components(p: _Peaks, n: int, params) -> list[list[int]]:
    """``gc_deconv._perceive_components`` on peak indices.

    Seeds are taken in the original order (score, m/z, apex). A peak can only join a seed
    ranked before it that it is still unused for; usage only grows, so the apex and
    shape tests of a block of upcoming seeds against the peaks unused now cover every
    question the sequential pass asks.
    """
    count = p.mz.size
    order = np.lexsort((p.apex, p.mz, -p.score))
    rank = np.empty(count, dtype=np.int64)
    rank[order] = np.arange(count)
    half = np.maximum(legacy.CORR_MIN_HALF_WIDTH, np.round(1.5 * p.width_half).astype(np.int64))
    lo = np.maximum(0, p.apex - half)
    hi = np.minimum(n, p.apex + half + 1)
    narrow = (hi - lo) < 3
    lo, hi = np.where(narrow, 0, lo), np.where(narrow, n, hi)
    by_apex = np.argsort(p.apex_sub, kind="stable")
    sorted_apex = p.apex_sub[by_apex]
    tol = params.apex_tolerance
    first = np.searchsorted(sorted_apex, p.apex_sub - tol - 1e-9, side="left")
    last = np.searchsorted(sorted_apex, p.apex_sub + tol + 1e-9, side="right")

    used = np.zeros(count, dtype=bool)
    order_l = order.tolist()
    groups: list[list[int]] = []
    pos, block = 0, 8
    while pos < count:
        seeds = []
        while pos < count and len(seeds) < block:
            k = order_l[pos]
            pos += 1
            if not used[k]:
                seeds.append(k)
        block = min(2 * block, 256)
        if not seeds:
            continue
        links = _links(np.array(seeds), p, params, rank, used, by_apex, first, last, lo, hi, n)
        for k in seeds:
            if used[k]:
                continue
            used[k] = True
            group = [k]
            for j in links.get(k, ()):
                if not used[j]:
                    used[j] = True
                    group.append(j)
            groups.append(group)
    return _merge_groups(p, groups, params)


def _links(seeds, p, params, rank, used, by_apex, first, last, lo, hi, n) -> dict[int, list[int]]:
    """For each seed: the unused, lower-ranked peaks within the apex tolerance whose
    profile correlates with the seed's over the seed's slice, in rank order."""
    count = last[seeds] - first[seeds]
    total = int(count.sum())
    if total == 0:
        return {}
    seed = np.repeat(seeds, count)
    pos = np.arange(total) - np.repeat(np.cumsum(count) - count, count) + np.repeat(first[seeds], count)
    cand = by_apex[pos]
    ok = (rank[cand] > rank[seed]) & ~used[cand]
    seed, cand = seed[ok], cand[ok]
    ok = ~(np.abs(p.apex_sub[cand] - p.apex_sub[seed]) > params.apex_tolerance)
    seed, cand = seed[ok], cand[ok]
    if seed.size == 0:
        return {}
    # Pearson r over the seed's slice; slices are padded to a common width and masked.
    length = (hi - lo)[seed]
    off = lo[seed][:, None] + np.arange(int(length.max()))[None, :]
    inside = off < hi[seed][:, None]
    off = np.minimum(off, n - 1)
    sa = np.where(inside, p.smooth[p.row[seed][:, None], off], 0.0)
    sb = np.where(inside, p.smooth[p.row[cand][:, None], off], 0.0)
    sa = np.where(inside, sa - (sa.sum(axis=1) / length)[:, None], 0.0)
    sb = np.where(inside, sb - (sb.sum(axis=1) / length)[:, None], 0.0)
    na = np.sqrt(np.einsum("ij,ij->i", sa, sa))
    nb = np.sqrt(np.einsum("ij,ij->i", sb, sb))
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.einsum("ij,ij->i", sa, sb) / (na * nb)
    r = np.where((na > 0.0) & (nb > 0.0), r, 0.0)
    keep = ~(r < params.shape_r)
    seed, cand = seed[keep], cand[keep]
    o = np.lexsort((rank[cand], seed))
    out: dict[int, list[int]] = {}
    for k, j in zip(seed[o].tolist(), cand[o].tolist()):
        out.setdefault(k, []).append(j)
    return out


def _merge_groups(p: _Peaks, groups: list[list[int]], params) -> list[list[int]]:
    """Discard weak groups, merge what the scan rate cannot separate, cap the count."""
    sub = p.apex_sub.tolist()
    mz = p.mz.tolist()
    score = p.score.tolist()
    groups = [g for g in groups if len({mz[q] for q in g}) >= params.min_ions]
    groups.sort(key=lambda g: (sub[g[0]], -score[g[0]], mz[g[0]]))
    merged: list[list[int]] = []
    for g in groups:
        if merged and abs(sub[g[0]] - sub[merged[-1][0]]) < legacy.MIN_SEPARATION_SCANS:
            prev = merged[-1]
            model = prev[0] if score[prev[0]] >= score[g[0]] else g[0]
            merged[-1] = [model] + [q for q in prev + g if q != model]
        else:
            merged.append(list(g))
    if len(merged) > legacy.MAX_COMPONENTS:
        merged.sort(key=lambda g: (-score[g[0]], mz[g[0]]))
        merged = merged[:legacy.MAX_COMPONENTS]
        merged.sort(key=lambda g: (sub[g[0]], -score[g[0]], mz[g[0]]))
    return merged


def _model_shape(p: _Peaks, k: int, n: int) -> np.ndarray:
    """``gc_deconv._model_shape``: the model ion's own peak, baseline removed, unit maximum."""
    shape = np.zeros(n, dtype=float)
    lo, hi = int(p.left[k]), int(p.right[k])
    shape[lo:hi + 1] = np.maximum(p.smooth[p.row[k]][lo:hi + 1] - p.base[k], 0.0)
    top = float(shape.max())
    return shape / top if top > 0 else shape


# --------------------------------------------------------------------------
# Joint purification
# --------------------------------------------------------------------------

def nnls_columns(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``x[:, c] = argmin ||a x - b[:, c]||`` subject to ``x >= 0``, for every column of ``b``.

    Block principal pivoting (Kim & Park, 2011) exchanges every infeasible variable at
    once, so all columns settle in a few joint steps. Optimality is judged with the
    tolerance of ``gc_deconv.nnls``: a variable stays in while its value exceeds ``tol``
    and stays out while its gradient ``a'(b - a x)`` does not. Columns that do not settle
    within the step budget are solved by the original Lawson-Hanson routine.
    """
    m, g = a.shape
    nc = b.shape[1]
    if g == 0 or nc == 0:
        return np.zeros((g, nc))
    scale = float(max(np.abs(a).max(), 1.0)) * np.maximum(np.abs(b).max(axis=0), 1.0)
    tol = max(m, g) * _EPS * scale
    gram = a.T @ a
    atb = (a.T @ b).T
    passive = np.zeros((nc, g), dtype=bool)
    x = np.zeros((nc, g))
    y = -atb                                  # the negative gradient where a variable is out
    best = np.full(nc, g + 1)
    backup = np.full(nc, 3)
    todo = np.arange(nc)
    for _ in range(5 * g + 10):
        t = tol[todo][:, None]
        pt = passive[todo]
        bad = (pt & (x[todo] <= t)) | (~pt & (y[todo] < -t))
        nbad = bad.sum(axis=1)
        live = nbad > 0
        todo, bad, nbad = todo[live], bad[live], nbad[live]
        if todo.size == 0:
            break
        fewer = nbad < best[todo]
        best[todo[fewer]] = nbad[fewer]
        backup[todo[fewer]] = 3
        retry = ~fewer & (backup[todo] >= 1)
        backup[todo[retry]] -= 1
        single = np.flatnonzero(~fewer & ~retry)
        if single.size:
            # Backup rule: exchange only the last infeasible variable (guarantees progress).
            last = g - 1 - np.argmax(bad[single][:, ::-1], axis=1)
            bad[single] = False
            bad[single, last] = True
        passive[todo] ^= bad
        sol = _solve_passive(a, gram, atb[todo], b[:, todo], passive[todo])
        x[todo] = sol
        y[todo] = np.where(passive[todo], 0.0, sol @ gram - atb[todo])
    else:
        solve = legacy._lstsq_solver(a)
        for q in todo.tolist():
            x[q] = legacy._nnls(a, b[:, q], None, solve)
    x[x < 0.0] = 0.0
    return x.T


def _solve_passive(a, gram, atb, b, passive) -> np.ndarray:
    """Least squares on each row's passive variables (zeros elsewhere), in one batched solve.

    The Gram matrix is restricted to the passive set and completed with an identity on the
    other variables, whose right-hand side is zero. A singular system falls back to the
    pseudo-inverse of the original routine.
    """
    rows, g = passive.shape
    pf = passive.astype(float)
    mat = gram[None, :, :] * (pf[:, :, None] * pf[:, None, :])
    diag = np.arange(g)
    mat[:, diag, diag] += 1.0 - pf
    try:
        sol = np.linalg.solve(mat, (atb * pf)[..., None])[..., 0]
        failed = ~np.isfinite(sol).all(axis=1)
    except np.linalg.LinAlgError:
        sol = np.zeros((rows, g))
        failed = np.ones(rows, dtype=bool)
    for q in np.flatnonzero(failed).tolist():
        key = np.flatnonzero(passive[q])
        sol[q] = 0.0
        sol[q, key] = np.linalg.pinv(a[:, key]) @ b[:, q]
    sol[~passive] = 0.0
    return sol


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------

def _components(p: _Peaks, groups, a, contrib, tic, mzs, win_rt, lo) -> list:
    """Steps 7-8 of gc_deconv.deconvolute: per-component figures, then the fixed order."""
    n = win_rt.size
    mz = p.mz.tolist()
    out = []
    for gi, group in enumerate(groups):
        model = group[0]
        coeff = contrib[gi]
        total = float(coeff.sum())
        if total <= 0.0:
            continue
        shape = a[:, gi]
        profile_y = shape * total
        area = float(np.trapezoid(shape)) * total
        apex_scan_win = int(np.argmax(profile_y))
        obs = float(tic[apex_scan_win])
        purity = min(1.0, profile_y[apex_scan_win] / obs) if obs > 0 else 0.0
        apex_sub = float(p.apex_sub[model])
        base = float(coeff.max())
        cutoff = base * legacy.SPECTRUM_MIN_PERMILLE / 1000.0
        keep = np.flatnonzero(~(coeff < cutoff))
        inten = np.round(coeff[keep] / base * 999)
        shown = inten > 0
        out.append(legacy.Component(
            rt=float(np.interp(apex_sub, np.arange(n, dtype=float), win_rt)),
            apex_scan=lo + int(round(apex_sub)),
            model_mz=int(mz[model]),
            spectrum=[(float(m), int(v)) for m, v in zip(mzs[keep][shown].tolist(),
                                                         inten[shown].tolist())],
            area=area,
            purity=float(purity),
            n_ions=len({mz[q] for q in group}),
            s_n=float(p.s_n[model]),
            profile_rt=win_rt.copy(),
            profile_y=profile_y,
        ))
    out.sort(key=lambda comp: (comp.rt, -comp.area, comp.model_mz))
    return out
