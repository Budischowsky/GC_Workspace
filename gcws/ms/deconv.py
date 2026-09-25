"""Spectral deconvolution of GC-MS data (AMDIS-style, numpy only).

Improves on the vendored NIAS engine (``gc_deconv``, kept unchanged for
parity) in the points where that engine is weak on real, thresholded data:

* **noise model** -- sigma(I) = sqrt(s0^2 + K^2 I): counting-like noise that
  grows with the signal, with a floor at the acquisition threshold (zeros in
  centroided data mean "below ~150 counts", not "exactly 0"); K is estimated
  per run;
* **scan skew** -- a quadrupole measures the masses of one scan at different
  times; the m/z-dependent apex offset is estimated from the data and
  removed before ions are grouped;
* **model shape** -- the weighted mean of the co-apexing, best-correlated
  ions of a component (not one truncated trace), tails continued smoothly;
* **background** -- every m/z is fitted with the component shapes *plus*
  two non-negative baseline ramps (together any non-negative linear
  baseline), 1/sigma weighted; what the ramps explain never enters a spectrum
  (background ions such as column bleed are flagged ``B``);
* **residual pass** -- components hidden under a larger one are looked for in
  the residual and added;
* **quality** -- R^2 of the model ion, fit chi^2, uniqueness and purity are
  combined into a 0-100 quality per component;
* **whole run** -- overlapping windows on a dense matrix built once per run.

Components are returned in the familiar shape (rt, model m/z, spectrum
normalised to 999, area, purity, ...), plus the new quality fields.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields
from typing import Callable, Optional

import numpy as np

ENGINE_VERSION = 1

#: masses that are poor model ions (air/water, column bleed)
BACKGROUND_MASSES = (18, 28, 32, 40, 44, 207, 281)


@dataclass
class DeconvSettings:
    window: float = 0.30              # min, half width of one deconvolution window
    noise_factor: float = 3.0         # an ion peak needs this S/N
    shape_r: float = 0.80             # minimum profile correlation with the component
    min_ions: int = 3                 # smallest component
    apex_tol: float = 0.7             # scans, apex agreement within a component
    min_sep: float = 0.6              # scans, closer components are merged
    smoothing: int = 0                # SG points (0 = automatic from the scan rate)
    baseline: bool = True             # fit non-negative baseline ramps per m/z
    residual_passes: int = 1
    skew: bool = True                 # correct the m/z-dependent scan skew
    exclude_model: tuple = BACKGROUND_MASSES
    max_components: int = 25

    @classmethod
    def from_dict(cls, d: dict | None) -> "DeconvSettings":
        names = {f.name for f in fields(cls)}
        vals = {k: v for k, v in (d or {}).items() if k in names}
        if "exclude_model" in vals:
            vals["exclude_model"] = tuple(vals["exclude_model"])
        return cls(**vals)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["exclude_model"] = list(self.exclude_model)
        return d


#: AMDIS-like presets: resolution (apex tolerance / separation), sensitivity, shape requirement
PRESETS = {
    "Resolution high": {"apex_tol": 0.5, "min_sep": 0.5},
    "Resolution medium": {"apex_tol": 0.7, "min_sep": 0.6},
    "Resolution low": {"apex_tol": 1.0, "min_sep": 1.0},
    "Sensitivity high": {"noise_factor": 2.0, "min_ions": 3},
    "Sensitivity medium": {"noise_factor": 3.0},
    "Sensitivity low": {"noise_factor": 5.0, "min_ions": 5},
    "Shape strict": {"shape_r": 0.92},
    "Shape medium": {"shape_r": 0.85},
    "Shape loose": {"shape_r": 0.75},
}


@dataclass
class Component:
    rt: float                              # min (MS axis), sub-scan apex
    apex_scan: int
    model_mz: int
    spectrum: list                         # [(m/z, 0..999)]
    area: float                            # counts x min, summed over the ions
    purity: float                          # share of the observed TIC at the apex
    n_ions: int
    s_n: float
    profile_rt: np.ndarray = field(default_factory=lambda: np.empty(0))
    profile_y: np.ndarray = field(default_factory=lambda: np.empty(0))
    model_ions: list = field(default_factory=list)
    bg_ions: list = field(default_factory=list)
    r2: float = 0.0
    chi2: float = 0.0
    uniqueness: int = 0
    quality: float = 0.0
    hidden: bool = False                   # found in the residual (under another component)
    tic_area: float = 0.0

    def spectrum_dict(self) -> dict[int, float]:
        return {int(m): float(v) for m, v in self.spectrum}


@dataclass
class DeconvResult:
    components: list
    t0: float
    t1: float
    noise_k: float
    skew: float                            # scans per u
    residual: np.ndarray = field(default_factory=lambda: np.empty(0))   # residual TIC over the window
    rt: np.ndarray = field(default_factory=lambda: np.empty(0))
    tic: np.ndarray = field(default_factory=lambda: np.empty(0))
    settings: DeconvSettings = field(default_factory=DeconvSettings)
    elapsed: float = 0.0


# -- run-level estimates -----------------------------------------------------------------------

@dataclass
class NoiseModel:
    k: float          # sigma = sqrt(s0^2 + k^2 I)
    s0: float

    def sigma(self, intensity):
        return np.sqrt(self.s0 ** 2 + self.k ** 2 * np.maximum(intensity, 0.0))


def estimate_noise(ms) -> NoiseModel:
    """Noise factor K from the scan-to-scan scatter of all ions, per run (cached)."""
    cached = getattr(ms, "_noise_model", None)
    if cached is not None:
        return cached
    d, _lo = ms.dense()
    x = d.astype(float)
    a, b, c = x[:-2], x[1:-1], x[2:]
    ok = (a > 0) & (b > 0) & (c > 0)
    r = (2 * b - a - c) / 3.0                # residual of a 3-point mean; var = 2/3 sigma^2 for white noise
    z = np.abs(r[ok]) / np.sqrt(b[ok])
    thr = ms.min_abundance() or 1.0
    k = float(np.median(z) * 1.4826 / np.sqrt(2.0 / 3.0)) if z.size > 100 else 3.0
    nm = NoiseModel(max(k, 0.5), max(thr / 2.0, 1.0))
    ms._noise_model = nm
    return nm


def _sg(width: int):
    import gc_deconv
    return lambda y: gc_deconv.savgol(y, width=width, order=2)


def estimate_skew(ms, nm: NoiseModel | None = None) -> float:
    """Apex offset per u (scans/u) of a scanning quadrupole, from the strongest peaks (cached)."""
    cached = getattr(ms, "_skew", None)
    if cached is not None:
        return cached
    nm = nm or estimate_noise(ms)
    d, lo = ms.dense()
    tic = d.sum(axis=1).astype(float)
    order = np.argsort(tic)[::-1]
    taken: list[int] = []
    for i in order:
        if len(taken) >= 25:
            break
        if 6 <= i < tic.size - 6 and all(abs(i - j) > 10 for j in taken) and tic[i] >= tic[i - 1] and tic[i] >= tic[i + 1]:
            taken.append(int(i))
    xs, ys, ws = [], [], []
    for i in taken:
        blk = d[i - 5:i + 6].astype(float)
        top = blk.max(axis=0)
        cols = np.flatnonzero(top > 30 * nm.sigma(top) / max(nm.k, 1))
        tic_apex = _parabolic(tic[i - 1:i + 2]) + i
        for c in cols:
            y = blk[:, c]
            j = int(np.argmax(y))
            if 1 <= j <= 9 and y[j - 1] > 0 and y[j + 1] > 0:
                xs.append(lo + c)
                ys.append(i - 5 + j + _parabolic(y[j - 1:j + 2]) - tic_apex)
                ws.append(np.sqrt(y[j]))
    skew = 0.0
    if len(xs) > 30:
        x = np.asarray(xs, float)
        y = np.asarray(ys, float)
        w = np.asarray(ws, float)
        keep = np.abs(y - np.median(y)) < 1.0
        if keep.sum() > 30:
            slope = np.polyfit(x[keep], y[keep], 1, w=w[keep])[0]
            span = (x.max() - x.min()) * abs(slope)
            skew = float(slope) if span > 0.15 else 0.0
    ms._skew = skew
    return skew


def _parabolic(y3) -> float:
    y0, y1, y2 = (float(v) for v in y3)
    den = y0 - 2 * y1 + y2
    if den >= 0:
        return 0.0
    return float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))


# -- per window ------------------------------------------------------------------------------------

@dataclass
class _IonPeak:
    mz: int
    col: int
    apex: int
    apex_sub: float           # skew-corrected, window index
    height: float
    prominence: float
    left: int
    right: int
    width: float              # half-height width, scans
    s_n: float
    sharp: float

    @property
    def score(self) -> float:
        return self.sharp * self.s_n


def _smooth_width(ms, settings: DeconvSettings) -> int:
    if settings.smoothing:
        w = int(settings.smoothing)
        return w if w % 2 else w + 1
    dt = float(np.median(np.diff(ms.rt))) * 60.0 if ms.n_scans > 1 else 0.5
    return 5 if dt >= 0.3 else (7 if dt >= 0.15 else 9)


def _perceive(s: np.ndarray, x: np.ndarray, mzs: np.ndarray, nm: NoiseModel, factor: float, skew: float,
              mz_ref: float, exclude: set) -> list[_IonPeak]:
    """Ion peaks: local maxima of the smoothed profiles that rise ``factor`` sigma above their flanks."""
    n = s.shape[0]
    out: list[_IonPeak] = []
    if n < 5:
        return out
    is_max = np.zeros_like(s, dtype=bool)
    is_max[1:-1] = (s[1:-1] > s[:-2]) & (s[1:-1] >= s[2:]) & (s[1:-1] > 0)
    for i, c in zip(*np.nonzero(is_max)):
        col = s[:, c]
        h = col[i]
        # topographic prominence: walk out until the profile rises above the apex
        left, lo_v, j = i, h, i
        while j > 0:
            j -= 1
            if col[j] > h:
                break
            if col[j] < lo_v:
                left, lo_v = j, col[j]
        right, hi_v, j = i, h, i
        while j < n - 1:
            j += 1
            if col[j] > h:
                break
            if col[j] < hi_v:
                right, hi_v = j, col[j]
        base = max(lo_v, hi_v)
        prom = h - base
        sig = float(nm.sigma(h)) / np.sqrt(2.0)          # smoothing lowers the noise
        if prom < factor * sig or np.count_nonzero(x[left:right + 1, c]) < 3:
            continue
        level = base + 0.5 * prom
        lw = np.flatnonzero(col[left:i + 1] <= level)
        rw = np.flatnonzero(col[i:right + 1] <= level)
        width = ((i + (rw[0] if rw.size else right - i)) - (left + (lw[-1] if lw.size else 0)))
        curv = max(2 * h - col[i - 1] - col[i + 1], 0.0) / prom
        sub = i + _parabolic(col[i - 1:i + 2]) - skew * (mzs[c] - mz_ref)
        s_n = prom / sig
        penalty = 0.2 if int(mzs[c]) in exclude else (0.6 if mzs[c] < 50 else 1.0)
        out.append(_IonPeak(int(mzs[c]), int(c), int(i), float(sub), float(h), float(prom), int(left), int(right),
                            float(max(width, 1.0)), float(s_n), float(curv * penalty)))
    return out


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    na, nb = float(np.sqrt(a @ a)), float(np.sqrt(b @ b))
    return float(a @ b / (na * nb)) if na > 0 and nb > 0 else 0.0


def _ion_shape(s: np.ndarray, p: _IonPeak) -> np.ndarray:
    """One ion's peak, baseline-removed and cut at its flanks, unit height."""
    n = s.shape[0]
    out = np.zeros(n)
    seg = s[p.left:p.right + 1, p.col]
    line = np.linspace(seg[0], seg[-1], seg.size)
    out[p.left:p.right + 1] = np.maximum(seg - np.minimum(line, seg.max()), 0.0)
    top = out.max()
    return out / top if top > 0 else out


def _continue_tails(shape: np.ndarray, width: float) -> np.ndarray:
    """Replace the zero tails beyond the cut with a smooth Gaussian decay."""
    nz = np.flatnonzero(shape > 0)
    if nz.size == 0:
        return shape
    out = shape.copy()
    sigma = max(width / 2.355, 0.6)
    a, b = nz[0], nz[-1]
    for k in range(a - 1, -1, -1):
        out[k] = shape[a] * np.exp(-0.5 * ((a - k) / sigma) ** 2)
        if out[k] < 1e-3:
            out[:k] = 0.0
            break
    for k in range(b + 1, shape.size):
        out[k] = shape[b] * np.exp(-0.5 * ((k - b) / sigma) ** 2)
        if out[k] < 1e-3:
            out[k + 1:] = 0.0
            break
    top = out.max()
    return out / top if top > 0 else out


def _group(peaks: list[_IonPeak], s: np.ndarray, settings: DeconvSettings) -> list[list[_IonPeak]]:
    order = sorted(range(len(peaks)), key=lambda k: (-peaks[k].score, peaks[k].mz, peaks[k].apex))
    used = [False] * len(peaks)
    groups = []
    n = s.shape[0]
    for k in order:
        if used[k]:
            continue
        seed = peaks[k]
        used[k] = True
        half = max(3, int(round(1.5 * seed.width)))
        lo, hi = max(0, seed.apex - half), min(n, seed.apex + half + 1)
        ref = s[lo:hi, seed.col]
        group = [seed]
        for j in order:
            if used[j]:
                continue
            c = peaks[j]
            if abs(c.apex_sub - seed.apex_sub) > settings.apex_tol:
                continue
            if _corr(ref, s[lo:hi, c.col]) < settings.shape_r:
                continue
            used[j] = True
            group.append(c)
        groups.append(group)
    groups = [g for g in groups if len({p.mz for p in g}) >= settings.min_ions]
    groups.sort(key=lambda g: g[0].apex_sub)
    merged: list[list[_IonPeak]] = []
    for g in groups:
        if merged and abs(g[0].apex_sub - merged[-1][0].apex_sub) < settings.min_sep:
            prev = merged[-1]
            model = prev[0] if prev[0].score >= g[0].score else g[0]
            merged[-1] = [model] + [p for p in prev + g if p is not model]
        else:
            merged.append(list(g))
    if len(merged) > settings.max_components:
        merged.sort(key=lambda g: -g[0].score)
        merged = sorted(merged[:settings.max_components], key=lambda g: g[0].apex_sub)
    return merged


def _model_shape(group: list[_IonPeak], s: np.ndarray) -> np.ndarray:
    seed = group[0]
    ref = _ion_shape(s, seed)
    shapes, weights = [ref], [seed.s_n]
    for p in sorted(group[1:], key=lambda q: -q.s_n)[:8]:
        sh = _ion_shape(s, p)
        if _corr(ref, sh) >= 0.95:
            shapes.append(sh)
            weights.append(p.s_n)
        if len(shapes) >= 5:
            break
    w = np.asarray(weights, float)
    shape = (np.vstack(shapes) * w[:, None]).sum(axis=0) / w.sum()
    return _continue_tails(shape / (shape.max() or 1.0), seed.width)


def _fit(x: np.ndarray, shapes: np.ndarray, nm: NoiseModel, baseline: bool) -> tuple[np.ndarray, np.ndarray]:
    """Weighted NNLS per m/z column: coefficients (components x masses) and the fitted matrix."""
    import gc_deconv
    n, m = x.shape
    cols = [shapes[:, k] for k in range(shapes.shape[1])]
    if baseline:
        cols += [np.linspace(0.0, 1.0, n), np.linspace(1.0, 0.0, n)]
    a = np.column_stack(cols)
    nc = shapes.shape[1]
    coef = np.zeros((a.shape[1], m))
    unweighted = gc_deconv._lstsq_solver(a)
    for c in range(m):
        col = x[:, c]
        if not col.any():
            continue
        w = 1.0 / nm.sigma(col)
        if np.ptp(w) / w.max() < 0.25:        # nearly uniform weights: use the cached solver
            coef[:, c] = gc_deconv._nnls(a, col, None, unweighted)
        else:
            coef[:, c] = gc_deconv.nnls(a * w[:, None], col * w)
    return coef[:nc], a @ coef, coef[nc:]


def deconvolute_window(ms, rt: float, settings: DeconvSettings | None = None) -> DeconvResult:
    """Deconvolution of the window ``rt +- settings.window`` (MS time)."""
    t_start = time.time()
    s_ = settings or DeconvSettings()
    nm = estimate_noise(ms)
    skew = estimate_skew(ms, nm) if s_.skew else 0.0
    d, lo_mass = ms.dense()
    sel = np.flatnonzero(np.abs(ms.rt - rt) <= s_.window)
    if sel.size < 7:
        return DeconvResult([], rt, rt, nm.k, skew, settings=s_)
    lo, hi = int(sel[0]), int(sel[-1])
    x_all = d[lo:hi + 1].astype(float)
    keep = np.count_nonzero(x_all, axis=0) >= 3
    mzs = np.arange(lo_mass, lo_mass + d.shape[1])[keep]
    x = x_all[:, keep]
    win_rt = ms.rt[lo:hi + 1]
    n = win_rt.size
    tic = x_all.sum(axis=1)
    res = DeconvResult([], float(win_rt[0]), float(win_rt[-1]), nm.k, skew, rt=win_rt, tic=tic, settings=s_)
    if mzs.size == 0:
        return res
    sg = _sg(_smooth_width(ms, s_))
    s = np.column_stack([sg(x[:, c]) for c in range(x.shape[1])])
    mz_ref = float(np.median(mzs))
    exclude = set(int(v) for v in s_.exclude_model)
    peaks = _perceive(s, x, mzs, nm, s_.noise_factor, skew, mz_ref, exclude)
    groups = _group(peaks, s, s_)
    if not groups:
        res.residual = tic.copy()
        res.elapsed = time.time() - t_start
        return res
    shapes = np.column_stack([_model_shape(g, s) for g in groups])
    hidden = [False] * len(groups)
    coef, fitted, base = _fit(x, shapes, nm, s_.baseline)
    for _ in range(max(0, s_.residual_passes)):
        resid = x - fitted
        rs = np.column_stack([sg(np.maximum(resid[:, c], 0.0)) for c in range(resid.shape[1])])
        extra = [g for g in _group(_perceive(rs, np.maximum(resid, 0), mzs, nm, s_.noise_factor + 1.0, skew,
                                             mz_ref, exclude), rs, s_)
                 if all(abs(g[0].apex_sub - h[0].apex_sub) >= max(s_.min_sep, 1.0) for h in groups)]
        if not extra:
            break
        groups += extra
        hidden += [True] * len(extra)
        shapes = np.column_stack([shapes] + [_model_shape(g, rs) for g in extra])
        coef, fitted, base = _fit(x, shapes, nm, s_.baseline)
    res.components = _components(groups, hidden, shapes, coef, base, fitted, x, mzs, win_rt, lo, tic, nm)
    res.residual = (x - fitted).sum(axis=1)
    res.elapsed = time.time() - t_start
    return res


def _components(groups, hidden, shapes, coef, base, fitted, x, mzs, win_rt, lo, tic, nm) -> list[Component]:
    n = win_rt.size
    idx = np.arange(n, dtype=float)
    dt = float(np.median(np.diff(win_rt))) if n > 1 else 1.0
    contributions = [np.outer(shapes[:, k], coef[k]) for k in range(len(groups))]
    total_fit = np.sum(contributions, axis=0) if contributions else np.zeros_like(x)
    out = []
    for k, group in enumerate(groups):
        model = group[0]
        shape = shapes[:, k]
        c = coef[k]
        if c.sum() <= 0:
            continue
        apex = int(np.argmax(shape))
        # an ion belongs to the spectrum when it is significant at the apex
        sig = nm.sigma(c) / np.sqrt(max(float(shape @ shape), 1.0))
        ok = (c > 2.0 * sig) & (c > 0)
        if not ok.any():
            continue
        cmax = c[ok].max()
        spec = [(int(mzs[i]), int(round(999 * c[i] / cmax))) for i in np.flatnonzero(ok) if c[i] / cmax >= 0.001]
        spec = [(m, v) for m, v in spec if v > 0]
        if len({m for m, _ in spec}) < 2:
            continue
        bg = []
        if base.size:
            bl_area = base.sum(axis=0) * n / 2.0
            comp_area = c * float(shape.sum())
            bg = [int(mzs[i]) for i in np.flatnonzero((bl_area > 3 * comp_area) & (x.max(axis=0) > 0))]
        # model-ion fit quality
        mc = int(np.flatnonzero(mzs == model.mz)[0])
        obs, fit = x[:, mc], fitted[:, mc]
        ss_res = float(((obs - fit) ** 2).sum())
        ss_tot = float(((obs - obs.mean()) ** 2).sum()) or 1.0
        r2 = max(0.0, 1.0 - ss_res / ss_tot)
        region = shape > 0.05
        cols = np.flatnonzero(ok)
        z = ((x[np.ix_(region, cols)] - fitted[np.ix_(region, cols)]) / nm.sigma(x[np.ix_(region, cols)]))
        chi2 = float((z ** 2).mean()) if z.size else 0.0
        mine = contributions[k][apex]
        uniq = int(np.count_nonzero((mine > 0.8 * np.maximum(total_fit[apex], 1e-12)) & ok))
        comp_tic = float(mine.sum())
        purity = float(min(1.0, comp_tic / tic[apex])) if tic[apex] > 0 else 0.0
        rt_apex = float(np.interp(model.apex_sub, idx, win_rt))
        area = float(c.sum() * shape.sum() * dt)
        s_n = float(model.s_n)
        quality = 100.0 * (0.35 * r2 + 0.25 * min(1.0, uniq / 5.0) + 0.25 * purity
                           + 0.15 * min(1.0, s_n / 50.0)) / (1.0 + 0.1 * max(0.0, chi2 - 1.0))
        out.append(Component(
            rt=rt_apex, apex_scan=lo + apex, model_mz=int(model.mz), spectrum=spec, area=area, purity=purity,
            n_ions=len({p.mz for p in group}), s_n=s_n, profile_rt=win_rt.copy(), profile_y=shape * c.sum(),
            model_ions=sorted({p.mz for p in group}), bg_ions=bg, r2=r2, chi2=chi2, uniqueness=uniq,
            quality=float(np.clip(quality, 0, 100)), hidden=bool(hidden[k]), tic_area=comp_tic))
    out.sort(key=lambda cmp: (cmp.rt, -cmp.area, cmp.model_mz))
    return out


def deconvolute_range(ms, t0: float, t1: float, settings: DeconvSettings | None = None,
                      progress: Optional[Callable[[str], None]] = None,
                      cancel: Optional[Callable[[], bool]] = None) -> list[Component]:
    """Components between ``t0`` and ``t1`` (MS time) from overlapping windows."""
    from gcws.ms.similarity import cosine
    s_ = settings or DeconvSettings()
    step = s_.window                       # windows of 2*window, 50 % overlap; each keeps its core
    centers = np.arange(t0 + step / 2.0, t1 + step / 2.0, step)
    found: list[Component] = []
    dt = float(np.median(np.diff(ms.rt))) if ms.n_scans > 1 else 0.0075
    for k, c in enumerate(centers):
        if cancel is not None and cancel():
            break
        res = deconvolute_window(ms, float(c), s_)
        for comp in res.components:
            if abs(comp.rt - c) <= step / 2.0 and t0 <= comp.rt <= t1:
                found.append(comp)
        if progress is not None and k % 5 == 0:
            progress(f"deconvolution {100 * (k + 1) / len(centers):.0f} %")
    found.sort(key=lambda cmp: cmp.rt)
    out: list[Component] = []
    for comp in found:                     # duplicates at the window borders
        dup = next((o for o in out[-3:] if abs(o.rt - comp.rt) < 0.6 * dt
                    and cosine(o.spectrum_dict(), comp.spectrum_dict()) > 0.9), None)
        if dup is None:
            out.append(comp)
        elif comp.quality > dup.quality:
            out[out.index(dup)] = comp
    return out


def component_for_peak(components: list[Component], t0: float, t1: float, apex: float | None = None):
    """The component that best represents a peak between t0 and t1 (MS time): the largest,
    best-quality one inside the peak, else the nearest to the apex."""
    inside = [c for c in components if t0 <= c.rt <= t1]
    if inside:
        return max(inside, key=lambda c: (c.area * (0.5 + c.quality / 200.0)))
    if apex is not None and components:
        return min(components, key=lambda c: abs(c.rt - apex))
    return None
