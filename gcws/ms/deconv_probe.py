"""A closer look at one integrated peak: a more sensitive deconvolution of its MS window.

The whole-run deconvolution (:mod:`gcws.ms.deconv`, the vendored NIAS engine) is deliberately
conservative: an ion is only seen where it has a maximum of its own, and a component needs three
ions whose apexes agree within half a scan. A small compound on the tail of a larger one shares
most of its ions with it and has no maximum in them, so it is not found, and the automatic split
never sees the peak as co-eluted.

:func:`probe` is used by the automatic split only for peaks whose trace is not explained by one
component (see :mod:`gcws.integration.auto_deconv`). It runs the same perception with more
sensitive parameters and then perceives once more in the residual left after the found components
are fitted to every ion (the AMDIS way of finding a shoulder). All components are purified
jointly, as the engine does it. What it finds still has to pass the split gates of the method.

The vendored engine is called through its helpers only; it is not changed.
"""
from __future__ import annotations

import numpy as np
import gc_deconv as legacy

from gcws.ms import deconv as D

#: perception parameters of the closer look (the whole run: 3.0 sigma, 3 ions, 0.5 scan, r 0.90)
NOISE_FACTOR = 2.0
MIN_IONS = 2
APEX_TOLERANCE = 1.0
SHAPE_R = 0.85
#: a shoulder: a second minimum of the smoothed second derivative at least SHOULDER_DEPTH of the
#: apex's one deep, standing out by SHOULDER_PROMINENCE, on a trace at least 5 % of the peak height
SHOULDER_DEPTH = 0.08
SHOULDER_PROMINENCE = 0.04
#: a component's model ion is its narrowest ion with at least this share of the seed's S/N
MODEL_MIN_SN_SHARE = 0.2
#: a component of the closer look below this share of the peak's component area is residual noise
MIN_AREA_SHARE = 0.005
#: ... or below this S/N of its model ion
MIN_SN = 5.0


def params_of(settings: D.DeconvSettings | None = None) -> legacy.DeconvParams:
    settings = settings or D.DeconvSettings()
    return legacy.DeconvParams(window=settings.window, noise_factor=min(NOISE_FACTOR, settings.noise_factor),
                               apex_tolerance=max(APEX_TOLERANCE, settings.apex_tol),
                               shape_r=min(SHAPE_R, settings.shape_r), min_ions=min(MIN_IONS, settings.min_ions))


def trace_shoulders(t, y) -> list[float]:
    """Times of the shoulders of a peak (``y`` above its baseline): the second-derivative test of
    the integrators, smoothed over half the peak's width at half height so that noise and a
    tailing peak do not count. A pair resolved closer than about two standard deviations shows no
    shoulder and is left to the fit."""
    t, y = np.asarray(t, dtype=float), np.asarray(y, dtype=float)
    if y.size < 9 or not float(y.max()) > 0:
        return []
    top = float(y.max())
    above = np.flatnonzero(y >= 0.5 * top)
    width = int(max(5, min(51, (above[-1] - above[0] + 1) // 2))) | 1
    if width > y.size:
        return []
    d2 = legacy.savgol(np.gradient(np.gradient(legacy.savgol(y, width, 3))), width, 3)
    main = int(np.argmin(d2))
    depth = -float(d2[main])
    if not depth > 0:
        return []
    out = []
    for i in range(1, y.size - 1):
        if i == main or not (d2[i] < d2[i - 1] and d2[i] <= d2[i + 1]):
            continue
        if -d2[i] < SHOULDER_DEPTH * depth or y[i] < 0.05 * top or abs(i - main) < width // 2:
            continue
        a, b = sorted((i, main))
        if float(d2[a:b + 1].max()) - d2[i] < SHOULDER_PROMINENCE * depth:
            continue
        out.append(float(t[i]))
    return out


def _perceive(x: np.ndarray, mzs: np.ndarray, sigmas: np.ndarray, params) -> list:
    peaks = []
    for c in range(mzs.size):
        col = x[:, c]
        if np.count_nonzero(col) < legacy.MIN_ION_SCANS or col.max() < params.noise_factor * sigmas[c]:
            continue
        peaks.extend(legacy._perceive_ion(int(mzs[c]), c, col, float(sigmas[c]), params))
    return peaks


def _clean_model(group: list) -> list:
    """``group`` with its narrowest strong ion as the model: an ion the neighbour shares is broadened
    by it, and a broadened model shape takes the neighbour's signal into this component."""
    strong = [p for p in group if p.s_n >= MODEL_MIN_SN_SHARE * group[0].s_n]
    model = min(strong, key=lambda p: (p.width_half, -p.score, p.mz))
    return [model] + [p for p in group if p is not model]


def _purify(x: np.ndarray, mzs: np.ndarray, win_rt: np.ndarray, lo: int, groups: list) -> list[D.Component]:
    """Joint purification and the component figures, as in the engine's steps 6 to 8."""
    n = win_rt.size
    a = np.column_stack([legacy._model_shape(g[0], n) for g in groups])
    solve = legacy._lstsq_solver(a)
    contrib = np.zeros((len(groups), mzs.size), dtype=float)
    for c in range(mzs.size):
        if x[:, c].any():
            contrib[:, c] = legacy._nnls(a, x[:, c], None, solve)
    tic = x.sum(axis=1)
    out = []
    for gi, group in enumerate(groups):
        model, coeff = group[0], contrib[gi]
        total = float(coeff.sum())
        if total <= 0.0:
            continue
        shape = a[:, gi]
        profile_y = shape * total
        apex = int(np.argmax(profile_y))
        obs = float(tic[apex])
        base = float(coeff.max())
        cutoff = base * legacy.SPECTRUM_MIN_PERMILLE / 1000.0
        spectrum = [(float(mzs[c]), int(round(v / base * 999))) for c, v in enumerate(coeff)
                    if v >= cutoff and int(round(v / base * 999)) > 0]
        out.append(D.Component(
            rt=float(np.interp(model.apex_sub, np.arange(n, dtype=float), win_rt)),
            apex_scan=lo + int(round(model.apex_sub)), model_mz=int(model.mz), spectrum=spectrum,
            area=float(np.trapezoid(shape)) * total,
            purity=float(min(1.0, profile_y[apex] / obs) if obs > 0 else 0.0),
            n_ions=len({p.mz for p in group}), s_n=float(model.s_n),
            profile_rt=win_rt.copy(), profile_y=profile_y))
    out.sort(key=lambda comp: (comp.rt, -comp.area, comp.model_mz))
    return out


def probe(ms, t0: float, t1: float, apex: float, settings: D.DeconvSettings | None = None) -> list[D.Component]:
    """The components of the peak ``t0``..``t1`` (MS time, apex ``apex``) found by the closer look."""
    params = params_of(settings)
    scan_rt = np.asarray(ms.rt, dtype=float)
    sel = np.flatnonzero(np.abs(scan_rt - apex) <= params.window)
    if sel.size < 5:
        return []
    lo, hi = int(sel[0]), int(sel[-1])
    win_rt = scan_rt[lo:hi + 1]
    n = win_rt.size
    x, mzs = legacy._ion_matrix(D._DataMSAdapter(ms), lo, hi)
    if mzs.size == 0:
        return []
    sigmas = np.array([legacy._ion_sigma(x[:, c]) for c in range(mzs.size)])
    sigmas = np.maximum(sigmas, legacy._sigma_floor(sigmas))
    peaks = _perceive(x, mzs, sigmas, params)
    groups = [_clean_model(g) for g in legacy._perceive_components(peaks, n, params)] if peaks else []
    if not groups:
        return []
    # residual pass: what the found components leave of every ion
    a = np.column_stack([legacy._model_shape(g[0], n) for g in groups])
    solve = legacy._lstsq_solver(a)
    fitted = np.zeros_like(x)
    for c in range(mzs.size):
        if x[:, c].any():
            fitted[:, c] = a @ legacy._nnls(a, x[:, c], None, solve)
    residual = np.maximum(x - fitted, 0.0)
    extra = _perceive(residual, mzs, sigmas, params)
    if extra:
        index = np.arange(n, dtype=float)
        for g in legacy._perceive_components(extra, n, params):
            rt = float(np.interp(g[0].apex_sub, index, win_rt))
            if not t0 <= rt <= t1:
                continue
            if all(abs(g[0].apex_sub - h[0].apex_sub) >= 2 * legacy.MIN_SEPARATION_SCANS for h in groups):
                groups.append(g)
        groups.sort(key=lambda g: (g[0].apex_sub, -g[0].score, g[0].mz))
        groups = [_clean_model(g) for g in groups[:legacy.MAX_COMPONENTS]]
    inside = [c for c in _purify(x, mzs, win_rt, lo, groups) if t0 <= c.rt <= t1]
    total = sum(c.area for c in inside)
    return [c for c in inside if c.area >= MIN_AREA_SHARE * total and c.s_n >= MIN_SN]
