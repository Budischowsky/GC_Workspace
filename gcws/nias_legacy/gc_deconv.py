#!/usr/bin/env python3
"""AMDIS-style deconvolution of ChemStation MS data — spec v3.0 §VI.10.

Own implementation of the AMDIS method (decision 5): no external AMDIS, no
``.ELU`` import, and **no scipy** — the environment has numpy only, so the two
numerical kernels the method needs are hand-rolled here: a Savitzky-Golay
smoother built from its closed-form least-squares coefficients (:func:`savgol`)
and a Lawson-Hanson non-negative least squares solver (:func:`nnls`).

Why this module is deliberately conservative
--------------------------------------------
The reference method runs at **2.23 scans/s** (3 925 scans over 6.294-35.654 min)
and FID peak widths are 0.05-0.10 min, so a chromatographic peak is described by
only **7-13 scans**. At that density every step has to be sized for seven
points: the smoother is width 5 (the widest that still leaves a 7-scan peak
recognisable), the parabolic apex refinement uses three points, and components
whose apexes end up closer than one scan are merged rather than reported
separately. Where the data cannot support a separation this module returns
*fewer* components; it never splits noise to look clever.

This module is headless and pure: no Tk, no matplotlib, no I/O beyond the
``DataMS`` reader it is handed. It returns components and nothing else — library
search, promotion to a grid row and register writes are out of scope (§VI.20).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional, Sequence

import numpy as np

import extract_ms_spectra as ex

# The nominal-mass binning must be *identical* to the one behind the raw
# spectrum shown in the panel, otherwise a deconvoluted spectrum and the mixed
# apex spectrum would disagree about m/z labels for the same ion.
_nominal = ex._nominal

# --------------------------------------------------------------------------
# Tuning constants that are not user parameters
# --------------------------------------------------------------------------

#: Absolute floor on an ion's sigma. Abundances are integer counts out of the
#: packed 14-bit mantissa, so a sigma below one count is an artefact of an
#: all-zero quartile, not a measurement.
SIGMA_FLOOR = 1.0

#: An ion present in fewer scans than this cannot describe a profile over a
#: 7-13 scan peak, whatever its height. [Annahme]
MIN_ION_SCANS = 3

#: Two perceived components whose refined apexes are closer than this are the
#: same component seen twice: at 2.23 scans/s the data cannot support the
#: separation, so they are merged (brief: "do not invent components closer
#: together than about one scan").
MIN_SEPARATION_SCANS = 1.0

#: Half width, in scans, of the shortest slice used for the shape correlation.
#: Three points either side of the apex is the least that can distinguish two
#: profiles at this scan density.
CORR_MIN_HALF_WIDTH = 3

#: Relative cutoff applied to the purified spectrum, in permille of the base
#: peak. Same default as ``extract_ms_spectra`` uses for the raw spectra.
SPECTRUM_MIN_PERMILLE = 1.0

#: Consistency constants that turn "MAD of the lowest quartile" into a real
#: sigma. The usual 1.4826 is the constant for the MAD of a *complete* normal
#: sample; applied to a sample already truncated at its lower quartile it
#: underestimates sigma by about 2.4x, which would silently turn §VI.10's
#: "3 sigma" gate into a 1.3 sigma gate and flood component perception with
#: noise. The values below are Monte-Carlo calibrated (40 000 standard normal
#: samples per size, seed 20260830) against the estimator as implemented in
#: :func:`_ion_sigma`, and reproduced by the unit test.
_MAD_K_N = np.array([8, 12, 16, 24, 32, 48, 64, 80, 120, 200, 400], dtype=float)
_MAD_K_V = np.array([3.365, 4.001, 4.390, 3.915, 3.750, 3.591,
                     3.517, 3.454, 3.412, 3.369, 3.338], dtype=float)


def mad_consistency(n: int) -> float:
    """Scale factor from the lowest-quartile MAD of ``n`` samples to sigma."""
    return float(np.interp(float(n), _MAD_K_N, _MAD_K_V))


#: Hard cap on perceived components per window. Purification is O(2^k) in the
#: worst case through the NNLS passive-set cache; a window that wants more than
#: this many components is noise, not chemistry.
MAX_COMPONENTS = 20


# --------------------------------------------------------------------------
# Public data model (binding contract, .v30_agent_brief.md)
# --------------------------------------------------------------------------

@dataclass
class DeconvParams:
    """Deconvolution parameters. Defaults are §VI.10's starting values.

    They are stored in the session and shown in the Parameter panel; they are
    *not* part of ``AutoLib.Settings`` and never reach the report engine.
    """

    window: float = 0.30          # min, half width of the deconvolution window
    noise_factor: float = 3.0     # ion is noise below this many sigma
    apex_tolerance: float = 0.5   # scans, apex agreement for grouping
    shape_r: float = 0.90         # minimum profile correlation with the model ion
    min_ions: int = 3             # smallest acceptable component


@dataclass
class Component:
    """One perceived, purified component of a deconvolution window."""

    rt: float                              # min, sub-scan refined apex
    apex_scan: int                         # absolute 0-based scan index in DataMS
    model_mz: int                          # nominal m/z of the model ion
    spectrum: list[tuple[float, int]]      # purified, normalised to base peak 999
    area: float                            # counts * scans (profile integral x spectrum sum)
    purity: float                          # share of the observed TIC at the apex, 0..1
    n_ions: int                            # ions in the perception group
    s_n: float                             # model ion prominence / model ion sigma
    profile_rt: np.ndarray = field(default_factory=lambda: np.empty(0))
    profile_y: np.ndarray = field(default_factory=lambda: np.empty(0))


# --------------------------------------------------------------------------
# Kernel 1 — Savitzky-Golay, closed form, no scipy
# --------------------------------------------------------------------------

# GCWS-PATCH: reuse the same least-squares matrix across ion traces and windows.
@lru_cache(maxsize=16)
def _sg_coeffs(width: int, order: int) -> np.ndarray:
    """Closed-form SG coefficient matrix ``C`` with shape ``(order + 1, width)``.

    ``C @ y`` are the polynomial coefficients of the least-squares fit through
    the ``width`` samples at offsets ``-half .. +half``; row 0 evaluated at
    offset 0 is therefore the familiar smoothing kernel — for ``width=5,
    order=2`` exactly ``(-3, 12, 17, 12, -3) / 35``.
    """
    half = width // 2
    x = np.arange(-half, half + 1, dtype=float)
    v = np.vander(x, order + 1, increasing=True)     # [1, x, x^2, ...]
    return np.linalg.pinv(v)                         # (V^T V)^-1 V^T


def _sg_weights(coeffs: np.ndarray, order: int, t: float) -> np.ndarray:
    """Convolution weights that evaluate the fitted polynomial at offset ``t``."""
    return coeffs.T @ (float(t) ** np.arange(order + 1, dtype=float))


# GCWS-PATCH: reuse the edge and centre evaluation weights for each smoothing setting.
@lru_cache(maxsize=16)
def _sg_kernels(width: int, order: int) -> tuple[np.ndarray, ...]:
    coeffs = _sg_coeffs(width, order)
    half = width // 2
    return tuple(_sg_weights(coeffs, order, t) for t in range(-half, half + 1))


def savgol(y: Sequence[float] | np.ndarray, width: int = 5,
           order: int = 2) -> np.ndarray:
    """Savitzky-Golay smoothing of ``y``, hand-rolled (scipy is not installed).

    Edges are handled by *evaluating* the edge window's fit at the edge offsets
    instead of mirroring or padding, so a polynomial of degree <= ``order`` is
    reproduced exactly over the whole array — the property the unit test pins.
    Arrays shorter than ``width`` shrink the window rather than raising, because
    a 7-scan peak clipped by the window boundary must still smooth.
    """
    y = np.asarray(y, dtype=float)
    n = y.size
    if width < 3 or width % 2 == 0:
        raise ValueError(f"savgol width must be odd and >= 3, got {width}")
    if order < 1 or order >= width:
        raise ValueError(f"savgol order must be in 1..{width - 1}, got {order}")
    if n < 3:
        return y.copy()
    if n < width:
        width = n if n % 2 else n - 1
        order = min(order, width - 1)

    half = width // 2
    kernels = _sg_kernels(width, order)
    out = np.empty(n, dtype=float)

    centre = kernels[half]
    out[half:n - half] = np.correlate(y, centre, mode="valid")

    head, tail = y[:width], y[-width:]
    for i in range(half):
        out[i] = kernels[i] @ head
        out[n - half + i] = kernels[half + i + 1] @ tail
    return out


# --------------------------------------------------------------------------
# Kernel 2 — Lawson-Hanson NNLS, no scipy
# --------------------------------------------------------------------------

def _lstsq_solver(a: np.ndarray) -> Callable[[tuple[int, ...], np.ndarray], np.ndarray]:
    """Return ``solve(passive_key, b)`` for the sub-problem ``A[:, passive]``.

    Purification solves NNLS a few hundred times against the *same* ``A`` and
    only a handful of distinct passive sets, so the pseudo-inverse of each
    column subset is computed once and reused. That turns every later
    least-squares solve into one matrix-vector product.
    """
    cache: dict[tuple[int, ...], np.ndarray] = {}

    def solve(key: tuple[int, ...], b: np.ndarray) -> np.ndarray:
        m = cache.get(key)
        if m is None:
            m = np.linalg.pinv(a[:, list(key)])
            cache[key] = m
        return m @ b

    return solve


def nnls(a, b, max_iter: Optional[int] = None) -> np.ndarray:
    """Solve ``min ||A x - b||_2`` subject to ``x >= 0`` (Lawson-Hanson).

    Deterministic: ties in the gradient test are broken by the lowest column
    index, so the same inputs always take the same pivot sequence.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.ndim != 2:
        raise ValueError("nnls: A must be 2-dimensional")
    if b.ndim != 1 or b.size != a.shape[0]:
        raise ValueError("nnls: b must be a vector of length A.shape[0]")
    return _nnls(a, b, max_iter, _lstsq_solver(a))


def _nnls(a: np.ndarray, b: np.ndarray, max_iter: Optional[int],
          solve: Callable[[tuple[int, ...], np.ndarray], np.ndarray]) -> np.ndarray:
    m, n = a.shape
    if n == 0:
        return np.zeros(0)
    if max_iter is None:
        max_iter = 3 * n

    scale = float(max(np.abs(a).max(), 1.0)) * float(max(np.abs(b).max(), 1.0))
    tol = max(m, n) * np.finfo(float).eps * scale

    x = np.zeros(n)
    passive = np.zeros(n, dtype=bool)
    w = a.T @ b
    outer = 0

    while outer < max_iter:
        active = ~passive
        if not active.any():
            break
        idx = np.flatnonzero(active)
        j = int(idx[int(np.argmax(w[idx]))])       # argmax -> lowest index on ties
        if w[j] <= tol:
            break
        outer += 1
        passive[j] = True

        inner = 0
        while inner <= 3 * n:
            inner += 1
            key = tuple(np.flatnonzero(passive).tolist())
            if not key:
                x = np.zeros(n)
                break
            s = np.zeros(n)
            s[list(key)] = solve(key, b)
            if s[passive].min() > tol:
                x = s
                break
            # Move towards s until the first passive coefficient hits zero.
            hit = passive & (s <= tol)
            denom = x[hit] - s[hit]
            with np.errstate(divide="ignore", invalid="ignore"):
                ratios = np.where(denom > 0, x[hit] / denom, 0.0)
            alpha = float(ratios.min()) if ratios.size else 0.0
            x = x + alpha * (s - x)
            passive &= ~(np.abs(x) <= tol)
            x[~passive] = 0.0
        w = a.T @ (b - a @ x)

    x[x < 0.0] = 0.0
    return x


# --------------------------------------------------------------------------
# Step 1-4 — window, ion matrix, per-ion noise, per-ion peak perception
# --------------------------------------------------------------------------

@dataclass
class _IonPeak:
    """One perceived peak of one ion — the raw material of component perception."""

    mz: int
    col: int                # column index into the window ion matrix
    apex: int               # integer apex, index within the window
    apex_sub: float         # parabolically refined apex, index within the window
    height: float           # smoothed apex value
    base: float             # prominence base (higher of the two flanking minima)
    prominence: float
    width_half: float       # scans
    sharpness: float        # apex curvature over height, AMDIS style
    s_n: float
    left: int               # window index of the left flanking minimum
    right: int              # window index of the right flanking minimum
    smooth: np.ndarray      # the ion's smoothed profile over the whole window

    @property
    def score(self) -> float:
        """Model-ion ranking: "the sharpest ion with the best S/N" (§VI.10 step 5)."""
        return self.sharpness * self.s_n


def _ion_matrix(ms, lo: int, hi: int) -> tuple[np.ndarray, np.ndarray]:
    """``(X[scan, mz], mzs)`` over the integral masses present in ``lo..hi``.

    Scans are decoded lazily through ``DataMS.spectrum`` (§5) — 40 scans at
    ~60 us each — and never held beyond this call.
    """
    binned: list[dict[int, int]] = []
    masses: set[int] = set()
    for i in range(lo, hi + 1):
        row: dict[int, int] = {}
        for mz, ab in ms.spectrum(i):
            k = _nominal(mz)
            row[k] = row.get(k, 0) + ab
        binned.append(row)
        masses.update(row)

    mzs = np.array(sorted(masses), dtype=int)
    index = {m: c for c, m in enumerate(mzs.tolist())}
    x = np.zeros((len(binned), mzs.size), dtype=float)
    for r, row in enumerate(binned):
        for m, v in row.items():
            x[r, index[m]] = v
    return x, mzs


def _ion_sigma(col: np.ndarray) -> float:
    """Noise of one ion profile: MAD of its lowest quartile (§VI.10 step 3).

    Returns the bare MAD estimate, which is legitimately 0.0 for an ion that is
    simply absent for three quarters of the window; :func:`_sigma_floor` supplies
    what such an ion has to be compared against instead.
    """
    q = max(4, col.size // 4)
    # GCWS-PATCH: nonnegative traces with at least q zeros have an exact zero lowest-quartile MAD.
    if np.count_nonzero(col) <= col.size - q and np.min(col) >= 0.0:
        return 0.0
    low = np.sort(col)[:q]
    mad = float(np.median(np.abs(low - np.median(low))))
    return mad_consistency(col.size) * mad


def _sigma_floor(sigmas: np.ndarray) -> float:
    """Noise floor for ions whose own lowest quartile is degenerate. [Annahme]

    On the reference sample 160 of 167 ions in a window have an all-zero lowest
    quartile and therefore sigma == 0, which would make ``max >= noise_factor *
    sigma`` true for any ion with a single stray count and flood component
    perception with junk. The physically meaningful comparison for such an ion
    is the detector noise the *other* masses of the same window show, so the
    median of the positive per-ion sigmas is used as the floor.
    """
    positive = sigmas[sigmas > 0.0]
    if positive.size == 0:
        return SIGMA_FLOOR
    return max(float(np.median(positive)), SIGMA_FLOOR)


def _flanks(s: np.ndarray, i: int) -> tuple[int, int, float]:
    """Walk both ways to the flanking minima; return ``(left, right, base)``."""
    h = s[i]
    left, lo_v = i, h
    j = i
    while j > 0:
        j -= 1
        if s[j] > h:
            break
        if s[j] < lo_v:
            left, lo_v = j, s[j]
    right, hi_v = i, h
    j = i
    while j < s.size - 1:
        j += 1
        if s[j] > h:
            break
        if s[j] < hi_v:
            right, hi_v = j, s[j]
    return left, right, float(max(lo_v, hi_v))


def _half_width(s: np.ndarray, i: int, base: float, prominence: float,
                left: int, right: int) -> float:
    """Half-height width in scans, linearly interpolated on the smoothed profile."""
    level = base + 0.5 * prominence
    lo = float(left)
    for j in range(i, left, -1):
        if s[j - 1] <= level:
            span = s[j] - s[j - 1]
            lo = (j - 1) + (level - s[j - 1]) / span if span > 0 else float(j)
            break
    hi = float(right)
    for j in range(i, right):
        if s[j + 1] <= level:
            span = s[j] - s[j + 1]
            hi = (j + 1) - (level - s[j + 1]) / span if span > 0 else float(j)
            break
    return max(hi - lo, 1e-6)


def _perceive_ion(mz: int, col_index: int, col: np.ndarray, sigma: float,
                  params: DeconvParams) -> list[_IonPeak]:
    """Step 4: smooth, find prominent maxima, refine the apex, measure them."""
    s = savgol(col, width=5, order=2)
    out: list[_IonPeak] = []
    for i in range(1, s.size - 1):
        if not (s[i] > s[i - 1] and s[i] >= s[i + 1]):
            continue
        left, right, base = _flanks(s, i)
        prominence = float(s[i] - base)
        if prominence < params.noise_factor * sigma:
            continue

        # Sub-scan apex from the parabola through the three points around it.
        den = s[i - 1] - 2.0 * s[i] + s[i + 1]
        shift = 0.0 if den == 0 else 0.5 * (s[i - 1] - s[i + 1]) / den
        shift = float(min(0.5, max(-0.5, shift)))

        curvature = float(2.0 * s[i] - s[i - 1] - s[i + 1])
        out.append(_IonPeak(
            mz=mz, col=col_index, apex=i, apex_sub=i + shift,
            height=float(s[i]), base=base, prominence=prominence,
            width_half=_half_width(s, i, base, prominence, left, right),
            sharpness=max(curvature, 0.0) / prominence,
            s_n=prominence / sigma, left=left, right=right, smooth=s,
        ))
    return out


# --------------------------------------------------------------------------
# Step 5 — component perception
# --------------------------------------------------------------------------

def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson r; 0 when either slice is flat (no shape to agree about)."""
    a = a - a.mean()
    b = b - b.mean()
    na = float(np.sqrt(a @ a))
    nb = float(np.sqrt(b @ b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float((a @ b) / (na * nb))


def _perceive_components(peaks: list[_IonPeak], n_scans: int,
                         params: DeconvParams) -> list[list[_IonPeak]]:
    """Greedy grouping around the best-scoring unused ion peak (§VI.10 step 5).

    Deterministic by construction: seeds are taken in a fully ordered sequence
    (score, then m/z, then apex) and membership is a pure predicate, so no dict
    or set iteration order can reach the result.
    """
    order = sorted(range(len(peaks)),
                   key=lambda k: (-peaks[k].score, peaks[k].mz, peaks[k].apex))
    used = [False] * len(peaks)
    groups: list[list[_IonPeak]] = []

    for k in order:
        if used[k]:
            continue
        seed = peaks[k]
        used[k] = True
        half = max(CORR_MIN_HALF_WIDTH, int(round(1.5 * seed.width_half)))
        lo = max(0, seed.apex - half)
        hi = min(n_scans, seed.apex + half + 1)
        if hi - lo < 3:
            lo, hi = 0, n_scans
        ref = seed.smooth[lo:hi]

        group = [seed]
        for j in order:
            if used[j] or j == k:
                continue
            cand = peaks[j]
            if abs(cand.apex_sub - seed.apex_sub) > params.apex_tolerance:
                continue
            if _correlation(ref, cand.smooth[lo:hi]) < params.shape_r:
                continue
            used[j] = True
            group.append(cand)
        groups.append(group)

    # Discard weak groups, then merge anything the scan rate cannot separate.
    groups = [g for g in groups if len({p.mz for p in g}) >= params.min_ions]
    groups.sort(key=lambda g: (g[0].apex_sub, -g[0].score, g[0].mz))

    merged: list[list[_IonPeak]] = []
    for g in groups:
        if merged and abs(g[0].apex_sub - merged[-1][0].apex_sub) < MIN_SEPARATION_SCANS:
            prev = merged[-1]
            model = prev[0] if prev[0].score >= g[0].score else g[0]
            rest = [p for p in prev + g if p is not model]
            merged[-1] = [model] + rest
        else:
            merged.append(list(g))

    if len(merged) > MAX_COMPONENTS:
        merged.sort(key=lambda g: (-g[0].score, g[0].mz))
        merged = merged[:MAX_COMPONENTS]
        merged.sort(key=lambda g: (g[0].apex_sub, -g[0].score, g[0].mz))
    return merged


# --------------------------------------------------------------------------
# Step 6-8 — joint purification, per-component figures, ordering
# --------------------------------------------------------------------------

def _model_shape(model: _IonPeak, n_scans: int) -> np.ndarray:
    """The component's elution shape: baseline-removed, isolated, unit maximum.

    Isolating the model ion's *own* peak (zeroing everything outside its
    flanking minima) keeps a second peak of the same ion elsewhere in the window
    out of this component's column of ``A``.
    """
    shape = np.zeros(n_scans, dtype=float)
    lo, hi = model.left, model.right
    seg = model.smooth[lo:hi + 1] - model.base
    shape[lo:hi + 1] = np.maximum(seg, 0.0)
    top = float(shape.max())
    return shape / top if top > 0 else shape


def deconvolute(ms, rt: float, params: DeconvParams = DeconvParams()
                ) -> list[Component]:
    """AMDIS-style deconvolution of the window around ``rt`` (§VI.10).

    ``ms`` is an :class:`extract_ms_spectra.DataMS`, ``rt`` a retention time in
    minutes. Returns the perceived components ordered by ``rt``, then by
    descending ``area``; the same input always yields the same output.
    """
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

    # Steps 3 and 4: noise, survivors, per-ion peak perception.
    sigmas = np.array([_ion_sigma(x[:, c]) for c in range(mzs.size)])
    floor = _sigma_floor(sigmas)
    sigmas = np.maximum(sigmas, floor)

    peaks: list[_IonPeak] = []
    for c in range(mzs.size):
        col = x[:, c]
        if np.count_nonzero(col) < MIN_ION_SCANS:
            continue
        if col.max() < params.noise_factor * sigmas[c]:
            continue
        peaks.extend(_perceive_ion(int(mzs[c]), c, col, float(sigmas[c]), params))
    if not peaks:
        return []

    groups = _perceive_components(peaks, n, params)
    if not groups:
        return []

    # Step 6: joint purification. One NNLS per mass over all components at once,
    # which removes the order dependence and the negative residuals that
    # sequential subtraction produces on 7-scan peaks.
    a = np.column_stack([_model_shape(g[0], n) for g in groups])
    solve = _lstsq_solver(a)
    contrib = np.zeros((len(groups), mzs.size), dtype=float)
    for c in range(mzs.size):
        col = x[:, c]
        if not col.any():
            continue
        contrib[:, c] = _nnls(a, col, None, solve)

    tic = x.sum(axis=1)
    out: list[Component] = []
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

        # Sub-scan RT: interpolate the scan RT axis at the refined apex.
        rt_apex = float(np.interp(model.apex_sub, np.arange(n, dtype=float), win_rt))

        base = float(coeff.max())
        cutoff = base * SPECTRUM_MIN_PERMILLE / 1000.0
        spectrum: list[tuple[float, int]] = []
        for c in range(mzs.size):
            v = coeff[c]
            if v < cutoff:
                continue
            inten = int(round(v / base * 999))
            if inten > 0:
                spectrum.append((float(mzs[c]), inten))

        out.append(Component(
            rt=rt_apex,
            apex_scan=lo + int(round(model.apex_sub)),
            model_mz=int(model.mz),
            spectrum=spectrum,
            area=area,
            purity=float(purity),
            n_ions=len({p.mz for p in group}),
            s_n=float(model.s_n),
            profile_rt=win_rt.copy(),
            profile_y=profile_y,
        ))

    # Step 8: deterministic ordering. model_mz is the final tie-break so that
    # two components at the same rt and area can never swap between runs.
    out.sort(key=lambda comp: (comp.rt, -comp.area, comp.model_mz))
    return out
