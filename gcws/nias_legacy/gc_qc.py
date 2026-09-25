"""Analytical quality metrics for the chromatogram workspace.

Implements spec v3.0 §VI.11 (FID noise, S/N, LOD/LOQ, MS peak purity,
co-elution) and §VI.12 (Kovats retention index).

The module is deliberately **headless and pure**: no Tk, no matplotlib, no
file I/O, no global state that survives a call. Everything here is a function
of its arguments, so the same numbers come out on the selection path and in the
background pass -- and so the values can be regression-tested without a display.

Why a separate module at all: these are the only numbers in the workspace that
are *judgements about* the data rather than the data itself. Keeping them apart
from ``gc_integrate`` (which owns the areas) means a change to a QC convention
can never move a reported area.

Cost, measured on the reference sample (§VI.15 budget "never on the selection
path"): ``fid_noise`` touches 600 samples, ``peak_purity`` reads three spectra,
``retention_index`` is a dictionary lookup plus one division. A whole-sample
pass over 49 rows stays in the low milliseconds, but :func:`qc_pass` is a
generator anyway so a caller can abandon it between rows.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

import numpy as np

__all__ = [
    "ISTD_AREA_HIGH",
    "ISTD_AREA_LOW",
    "ISTD_EXEMPT",
    "IstdCheck",
    "Noise",
    "SN_CONVENTION",
    "istd_window",
    "is_window_exempt",
    "fid_noise",
    "signal_to_noise",
    "sn_convention_label",
    "lod_loq",
    "peak_purity",
    "coelution",
    "alkane_ladder",
    "alkane_number",
    "usable_ladder",
    "ladder_problems",
    "retention_index",
    "first_peak_after",
    "qc_pass",
    "ALKANES",
]


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Default end of the solvent front, minutes. Same value AutoLib's Settings
#: uses; passed in explicitly by callers that have the real setting at hand.
DEFAULT_SOLVENT_END = 5.5

#: Default length of the noise window, minutes (§VI.11).
NOISE_SPAN = 0.5

#: Length of one noise segment, minutes. The window is split into segments of
#: this length; a linear drift is removed per segment before the peak-to-peak
#: span is taken, so a slowly rising baseline does not inflate the noise.
NOISE_SEGMENT = 0.1

#: Fewest samples a segment must hold for its span to mean anything. Below
#: this the segment is dropped rather than producing a meaninglessly small
#: peak-to-peak value.
MIN_SEGMENT_POINTS = 8

#: MAD -> sigma for normally distributed residuals.
MAD_TO_SIGMA = 1.4826

#: The ISTD raw-FID-area window (spec v3.1 SS VII.6, entschieden). Below the low
#: bound the injection or the liner is suspect, above the high bound the
#: standard was over-spiked or the split failed. Both are **session** values in
#: the workspace, never ``AutoLib.Settings`` fields: the check is a banner and
#: nothing it produces may reach the report engine (SS VII.15, assumption 4).
ISTD_AREA_LOW = 8_000_000.0
ISTD_AREA_HIGH = 11_000_000.0

#: The verdicts :func:`istd_window` returns, named so the banner and its tests
#: cannot drift apart on a spelling.
ISTD_OK = "ok"
ISTD_LOW = "low"
ISTD_HIGH = "high"
ISTD_MISSING = "missing"
#: The QC standard was found, but the window was not applied to it -- see
#: :data:`ISTD_WINDOW_ROLES`. Distinct from ``ok`` so a caller that wants to
#: *show* the area (the Standards panel) can still say why it is not judged,
#: while :attr:`IstdCheck.ok` counts it as quiet.
ISTD_EXEMPT = "exempt"

#: Role AutoLib gives the QC standard (``gc_fid.ROLE_QC``). Repeated here
#: rather than imported for the same reason the rest of this module has no
#: project imports: it must judge a standards table without pulling the FID
#: stack in behind it, and a test builds its rows by hand.
ROLE_QC = "QC"

#: Name fragment that identifies the QC standard when a hand-built row carries
#: no role at all. Deliberately not ``-d4``: two of the three *quantification*
#: standards are deuterated too, so that would silence the very rows the window
#: exists for. Dibutyl phthalate is the only QC standard in ``IS_DEFS``.
QC_NAME_FRAGMENT = "dibutyl phthalate"

#: Roles the area window is applied to. **The QC standard is not one of them**
#: (spec v3.1 SS VII.6, revised 2026-09-06): DBP-d4 is spiked deliberately low,
#: so it sits under ``ISTD_AREA_LOW`` on every well-behaved run and a banner on
#: it is a false alarm that trains the analyst to ignore the true ones. What it
#: is spiked *for* -- being present at all -- is unaffected: a QC standard that
#: was not found still comes back ``missing``, and AutoLib's own
#: ``Below QC minimum`` status is untouched by anything in this module.
ISTD_WINDOW_ROLES_EXCLUDED: frozenset[str] = frozenset({ROLE_QC.casefold()})

#: How the default noise window is chosen (§VI.21, entschieden 2026-08-31).
#: ``"QUIETEST"`` scans the run and takes the calmest window; ``"BEFORE_PEAK"``
#: is the original §VI.11 rule, kept because it is what the spec text describes
#: and what the older tests pin.
NOISE_MODE_QUIETEST = "QUIETEST"
NOISE_MODE_BEFORE_PEAK = "BEFORE_PEAK"
NOISE_MODE_DEFAULT = NOISE_MODE_QUIETEST

#: Step of the sliding scan, minutes. Half a window, so every stretch of the
#: run is covered by at least one candidate without measuring 4 000 of them.
NOISE_SCAN_STEP = 0.25

#: **Assumption §VI.19 item 5.** ``"PH_EUR"`` -> ``S/N = 2·H / noise_pp``
#: (European Pharmacopoeia 2.2.46, the convention this spec was written
#: against). ``"ASTM"`` -> ``S/N = H / sigma``. Changing this one string
#: switches every S/N in the workspace, the grid and the export; nothing else
#: in the codebase may reimplement the formula.
SN_CONVENTION = "PH_EUR"

#: LOD / LOQ multiples of the noise *amplitude* (``noise_pp / 2``), §VI.11.
LOD_FACTOR = 3.0
LOQ_FACTOR = 10.0

#: Fraction of the apex height at which the purity flank spectra are taken.
PURITY_FLANK = 0.30

#: An ion counts as significant at or above this fraction of the base peak.
PURITY_ION_THRESHOLD = 0.01

#: Fewest shared significant ions for a cosine similarity to be reported. With
#: one or two ions the cosine is either 1.0 by construction or dominated by a
#: single ratio, which would look like a confident purity value and is not one.
MIN_SHARED_IONS = 3

#: n-alkane name -> carbon number. The list of §VI.12: the ``CLASS_RULES``
#: hydrocarbons of ``AutoLib`` (decane … triacontane), extended down to
#: heptane, plus the spellings ChemStation/NIST libraries actually emit
#: ("icosane" for eicosane, "henicosane" for heneicosane). ``undecane`` is
#: missing from AutoLib's list and is added here -- its absence there is a
#: classification gap, not a decision.
ALKANES: dict[str, int] = {
    "heptane": 7, "octane": 8, "nonane": 9, "decane": 10, "undecane": 11,
    "dodecane": 12, "tridecane": 13, "tetradecane": 14, "pentadecane": 15,
    "hexadecane": 16, "heptadecane": 17, "octadecane": 18, "nonadecane": 19,
    "eicosane": 20, "icosane": 20, "heneicosane": 21, "henicosane": 21,
    "docosane": 22, "tricosane": 23, "tetracosane": 24, "pentacosane": 25,
    "hexacosane": 26, "heptacosane": 27, "octacosane": 28, "nonacosane": 29,
    "triacontane": 30,
}

#: Matches *only* the unsubstituted n-alkane. Anchored on both ends on purpose:
#: "Hexadecane" must not match through the "decane" alternative, and
#: "Heptadecane, 2,6,10,14-tetramethyl-" (pristane) must not enter the ladder
#: at all -- a wrong rung silently biases every RI derived from it.
_ALKANE_RE = re.compile(
    r"^(?:n[-\s]?)?(" + "|".join(sorted(ALKANES, key=len, reverse=True)) + r")$"
)


# --------------------------------------------------------------------------
# FID noise
# --------------------------------------------------------------------------

@dataclass
class Noise:
    """A noise measurement on one FID trace.

    ``pp`` is the median peak-to-peak span of the drift-corrected segments,
    ``sigma`` the robust standard deviation of the pooled residuals. Both are
    in the trace's own counts unit. ``lo``/``hi`` are the window in minutes, so
    the panel can draw exactly the stretch that was measured -- a noise number
    without its window is not checkable.
    """

    pp: float
    sigma: float
    lo: float
    hi: float
    #: How many segments actually contributed. Zero means the window was
    #: unusable and ``pp``/``sigma`` are 0.0; callers must treat that as
    #: "no measurement", not as "no noise".
    n_segments: int = 0

    @property
    def ok(self) -> bool:
        """Whether this measurement may be divided by."""
        return self.n_segments > 0 and self.pp > 0.0


def fid_noise(trace: Any,
              first_peak_rt: Optional[float],
              solvent_end: float = DEFAULT_SOLVENT_END,
              span: float = NOISE_SPAN,
              *,
              bounds: Optional[tuple[float, float]] = None,
              mode: str = NOISE_MODE_DEFAULT) -> Noise:
    """Measure FID noise in a signal-free stretch (§VI.11).

    ``trace`` is a :class:`gc_ch.Trace` -- only ``.rt`` and ``.y`` are touched,
    so any object with those two ascending arrays works (which is what makes
    this testable without the reader).

    Window selection, in order of precedence:

    * ``bounds`` -- an explicit ``(lo, hi)``, the window the analyst drags in
      the FID panel;
    * ``mode="QUIETEST"`` (the default) -- the calmest ``span``-minute window
      of the run after ``solvent_end``, found by :func:`quietest_window`;
    * ``mode="BEFORE_PEAK"`` -- the original §VI.11 rule: the ``span`` minutes
      ending just before ``first_peak_rt``, the first *integrated* peak after
      ``solvent_end``.

    **Why the default changed (§VI.21).** §VI.11 prescribed the "before the
    first peak" window; on the reference method that leaves 0.091 min where
    0.5 was asked for -- less than one segment, so the median-of-spans
    robustness never existed -- and the solvent is still eluting there (mean
    signal 178 862 counts against 47 747 late in the run). It measured
    ``pp = 7 444`` against 125 in the calmest window, a factor of 59.5, which
    put 48 of 139 peaks below S/N 10 that are nowhere near it. The rule was not
    conservative, it was wrong, so the default now measures where the baseline
    actually is quiet. ``Noise.lo``/``hi`` report which stretch that was, so
    the choice stays checkable.

    The window is split into :data:`NOISE_SEGMENT` slices; each slice has a
    linear drift removed by least squares before its peak-to-peak span is
    taken, and the **median** of those spans is ``pp``. Median rather than mean
    because a single spike -- a stray ion, a pressure blip -- must not become
    the sample's noise figure.

    The calmest window is still not *provably* signal-free -- a broad, low
    hump would be detrended away segment by segment. It is the best available
    estimate, and ``lo``/``hi`` are returned so it can be inspected.
    """
    rt = np.asarray(trace.rt, dtype=float)
    y = np.asarray(trace.y, dtype=float)
    if rt.size == 0 or rt.size != y.size:
        return Noise(0.0, 0.0, 0.0, 0.0, 0)

    if bounds is not None:
        lo, hi = float(bounds[0]), float(bounds[1])
        if hi < lo:
            lo, hi = hi, lo
    elif mode == NOISE_MODE_QUIETEST:
        lo, hi = quietest_window(rt, y, solvent_end, span)
    else:
        # "ending just before the first integrated peak": the window stops at
        # the peak's *start* if the caller knows it, otherwise at its apex.
        hi = float(first_peak_rt) if first_peak_rt is not None else float(
            solvent_end) + float(span)
        lo = hi - float(span)
        # Never reach back into the solvent tail: it is not noise, it is the
        # descending flank of a peak orders of magnitude larger.
        if lo < solvent_end:
            lo = float(solvent_end)
            hi = min(hi, lo + float(span))

    return _measure_window(rt, y, lo, hi)


def quietest_window(rt: np.ndarray, y: np.ndarray,
                    solvent_end: float = DEFAULT_SOLVENT_END,
                    span: float = NOISE_SPAN,
                    step: float = NOISE_SCAN_STEP) -> tuple[float, float]:
    """The calmest ``span``-minute window after ``solvent_end`` (§VI.21).

    Slides a window in ``step`` increments and returns the one with the
    smallest ``pp``, measured by exactly the same segment machinery the noise
    figure uses -- there must not be two definitions of "noise" in this module,
    or the window chosen and the number reported could disagree.

    Windows that yield no usable segment are skipped. If none is usable at all
    the whole post-solvent stretch is returned, so the caller still gets a
    defined window rather than an exception.
    """
    rt = np.asarray(rt, dtype=float)
    y = np.asarray(y, dtype=float)
    first = max(float(solvent_end), float(rt[0])) if rt.size else float(solvent_end)
    last = float(rt[-1]) if rt.size else first
    fallback = (first, last)
    if last - first < span:
        return fallback

    best: Optional[tuple[float, float, float]] = None
    start = first
    while start + span <= last + 1e-9:
        measured = _measure_window(rt, y, start, start + span)
        if measured.n_segments and measured.pp > 0.0:
            if best is None or measured.pp < best[0]:
                best = (measured.pp, start, start + span)
        start += step
    return (best[1], best[2]) if best is not None else fallback


def _measure_window(rt: np.ndarray, y: np.ndarray,
                    lo: float, hi: float) -> Noise:
    """Noise of one explicit window. The single definition of ``pp``."""
    lo = max(lo, float(rt[0]))
    hi = min(hi, float(rt[-1]))
    if hi <= lo:
        return Noise(0.0, 0.0, lo, hi, 0)

    i0 = int(np.searchsorted(rt, lo, side="left"))
    i1 = int(np.searchsorted(rt, hi, side="right"))
    seg_rt = rt[i0:i1]
    seg_y = y[i0:i1]
    if seg_rt.size < MIN_SEGMENT_POINTS:
        return Noise(0.0, 0.0, lo, hi, 0)

    n_seg = max(1, int(round((hi - lo) / NOISE_SEGMENT)))
    edges = np.linspace(lo, hi, n_seg + 1)

    spans: list[float] = []
    residuals: list[np.ndarray] = []
    for k in range(n_seg):
        a = int(np.searchsorted(seg_rt, edges[k], side="left"))
        b = int(np.searchsorted(seg_rt, edges[k + 1], side="right"))
        xs = seg_rt[a:b]
        ys = seg_y[a:b]
        if xs.size < MIN_SEGMENT_POINTS:
            continue
        res = _detrend(xs, ys)
        spans.append(float(np.ptp(res)))
        residuals.append(res)

    if not spans:
        return Noise(0.0, 0.0, lo, hi, 0)

    pp = float(np.median(spans))
    pooled = np.concatenate(residuals)
    # Robust sigma: MAD of the pooled residuals. A plain std would be inflated
    # by any small unnoticed peak inside the window, which is exactly the
    # failure mode §VI.19 item 7 is about.
    sigma = float(MAD_TO_SIGMA * np.median(np.abs(pooled - np.median(pooled))))
    return Noise(pp, sigma, lo, hi, len(spans))


def _detrend(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Residual of ``y`` after removing a least-squares straight line in ``x``.

    Written out rather than ``np.polyfit`` to avoid its rank warnings on the
    short, badly conditioned segments a 0.1 min slice produces.
    """
    xm = x - x.mean()
    denom = float(np.dot(xm, xm))
    if denom <= 0.0:
        return y - y.mean()
    slope = float(np.dot(xm, y - y.mean())) / denom
    return y - (y.mean() + slope * xm)


def first_peak_after(rts: Sequence[float],
                     solvent_end: float = DEFAULT_SOLVENT_END) -> Optional[float]:
    """Earliest retention time strictly after ``solvent_end``, or ``None``.

    Convenience for the default noise window: the caller passes the integrated
    peaks' start times (or apex times) and gets the anchor ``fid_noise`` wants.
    """
    later = [float(r) for r in rts if r is not None and float(r) > solvent_end]
    return min(later) if later else None


# --------------------------------------------------------------------------
# S/N, LOD, LOQ
# --------------------------------------------------------------------------

def sn_convention_label() -> str:
    """Human-readable name of the active S/N convention, for report headers.

    The convention must be stated wherever an S/N is printed -- the two forms
    differ by roughly a factor of three and neither is 'the' S/N.
    """
    if SN_CONVENTION == "ASTM":
        return "ASTM (H/σ)"
    return "Ph. Eur. (2·H/pp)"


def signal_to_noise(height: Optional[float], noise: Optional[Noise]) -> float:
    """S/N of a peak of ``height`` counts against a :class:`Noise`.

    Ph. Eur. 2.2.46: ``2·H / noise_pp``. Switch :data:`SN_CONVENTION` to
    ``"ASTM"`` for ``H/σ`` -- the whole workspace follows, because this is the
    only place the formula exists (§VI.19 item 5).

    Returns ``nan`` when there is no usable noise measurement or no height.
    ``nan`` and not ``0.0`` on purpose: every comparison against a limit is
    then False, so a missing measurement can never *flag* a row.
    """
    if height is None or noise is None:
        return float("nan")
    h = float(height)
    if not math.isfinite(h) or h <= 0.0:
        return float("nan")
    if SN_CONVENTION == "ASTM":
        if noise.sigma <= 0.0:
            return float("nan")
        return h / noise.sigma
    if not noise.ok:
        return float("nan")
    return 2.0 * h / noise.pp


def lod_loq(noise: Optional[Noise],
            width_min: Optional[float],
            hz: float) -> tuple[float, float]:
    """Raw areas equivalent to 3 and 10 × ``noise.pp/2`` over the peak width.

    ``width_min`` is the peak width in minutes (``IntegrationResult.width_half``
    at the call sites), ``hz`` the trace's sampling rate.

    The unit is the integrator's raw area, ``counts · seconds``: a rectangle of
    amplitude ``noise.pp/2`` spanning the same number of samples the peak does,
    ``amp · n / hz``. The sample count rather than the bare width keeps this on
    the same discrete grid ``gc_integrate.integrate`` sums over, so LOQ and area
    are directly comparable numbers rather than nearly-comparable ones.

    **Converting to mg/kg is the caller's job** (agent DATA): the same
    ``mean_factor · ov_ratio`` chain that turns any area into a concentration.
    Doing it here would duplicate the quantification rules in a QC module.

    Returns ``(0.0, 0.0)`` when there is nothing to compute from.
    """
    if noise is None or not noise.ok or not width_min or hz <= 0:
        return (0.0, 0.0)
    w = float(width_min)
    if not math.isfinite(w) or w <= 0.0:
        return (0.0, 0.0)
    amp = noise.pp / 2.0
    n = max(1, int(round(w * 60.0 * float(hz))))
    unit = amp * n / float(hz)          # counts * seconds
    return (LOD_FACTOR * unit, LOQ_FACTOR * unit)


# --------------------------------------------------------------------------
# MS peak purity
# --------------------------------------------------------------------------

def peak_purity(ms: Any,
                apex_scan: int,
                first_scan: int,
                last_scan: int) -> Optional[float]:
    """Spectral purity of one peak in percent, or ``None``.

    ``ms`` is an :class:`extract_ms_spectra.DataMS`. **All three scan arguments
    are 0-based indices into ``ms``**, like ``PeakRow.apex_scan``; a caller
    holding ChemStation's 1-based ``First``/``Last`` must subtract 1. Mixing the
    two conventions shifts the flanks by one scan, which at 2.23 scans/s is a
    large fraction of a peak.

    The spectra at the :data:`PURITY_FLANK` up-flank and down-flank are compared
    against the apex spectrum as cosine similarity over the ions significant
    (≥ :data:`PURITY_ION_THRESHOLD` of the base peak) in *both* spectra::

        purity = 100 · min( cos(up, apex), cos(down, apex) )

    Returns ``None`` -- never a fabricated number -- when the peak has no
    distinct flank scans, when fewer than :data:`MIN_SHARED_IONS` ions are
    shared, or when the scan window is unusable. At the reference sample's
    2.23 scans/s a great many peaks are only ~7 scans wide, so ``None`` is a
    normal and frequent answer, not an error.
    """
    n_scans = int(getattr(ms, "n_scans", 0) or 0)
    if n_scans <= 0:
        return None
    try:
        apex = int(apex_scan)
        lo = int(first_scan)
        hi = int(last_scan)
    except (TypeError, ValueError):
        return None
    if lo > hi:
        lo, hi = hi, lo
    lo = max(0, min(lo, n_scans - 1))
    hi = max(0, min(hi, n_scans - 1))
    apex = max(lo, min(apex, hi))
    if hi - lo < 2:
        return None                      # no room for two distinct flanks

    tic = list(getattr(ms, "tic", []) or [])
    if len(tic) < n_scans:
        return None
    y = np.asarray(tic[lo:hi + 1], dtype=float)
    # Endpoint baseline, the same convention gc_integrate uses for the FID.
    base = np.linspace(y[0], y[-1], y.size)
    corr = y - base
    a = apex - lo
    top = float(corr[a])
    if top <= 0.0:
        # Endpoint baseline above the apex: the scan bounds do not describe a
        # peak. Reporting a purity here would be an artefact of the baseline.
        return None

    thresh = PURITY_FLANK * top
    up = None
    for i in range(a - 1, -1, -1):
        if corr[i] < thresh:
            up = i + 1                    # first scan at or above 30 %
            break
    else:
        up = 0
    down = None
    for i in range(a + 1, corr.size):
        if corr[i] < thresh:
            down = i - 1                  # last scan at or above 30 %
            break
    else:
        down = corr.size - 1

    if up >= a or down <= a:
        return None                      # flank collapses onto the apex

    s_apex = _significant(ms.spectrum(apex))
    s_up = _significant(ms.spectrum(lo + up))
    s_down = _significant(ms.spectrum(lo + down))
    if not s_apex:
        return None

    c_up = _cosine(s_up, s_apex)
    c_down = _cosine(s_down, s_apex)
    if c_up is None or c_down is None:
        return None
    return 100.0 * min(c_up, c_down)


def _significant(spectrum: Sequence[tuple[float, int]]) -> dict[int, float]:
    """Unit-mass spectrum reduced to ions ≥ 1 % of the base peak.

    Unit-mass binning (``round``) because the packed m/z of ``data.ms`` is a
    twentieth of a mass unit and identical ions differ in the last digit
    between scans; matching on the raw float would find almost no shared ions.
    """
    binned: dict[int, float] = {}
    for mz, ab in spectrum:
        k = int(round(float(mz)))
        binned[k] = binned.get(k, 0.0) + float(ab)
    if not binned:
        return {}
    base = max(binned.values())
    if base <= 0.0:
        return {}
    cut = PURITY_ION_THRESHOLD * base
    return {k: v for k, v in binned.items() if v >= cut}


def _cosine(a: dict[int, float], b: dict[int, float]) -> Optional[float]:
    """Cosine similarity over the ions significant in both spectra."""
    shared = a.keys() & b.keys()
    if len(shared) < MIN_SHARED_IONS:
        return None
    va = np.array([a[k] for k in sorted(shared)], dtype=float)
    vb = np.array([b[k] for k in sorted(shared)], dtype=float)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na <= 0.0 or nb <= 0.0:
        return None
    # Clipped: floating point can return 1.0000000000000002 for identical
    # spectra, and a purity of 100.00000000000002 in a grid cell is noise.
    return float(min(1.0, max(0.0, float(np.dot(va, vb)) / (na * nb))))


# --------------------------------------------------------------------------
# Co-elution
# --------------------------------------------------------------------------

def coelution(row: Any, pbm_rows: Any, delay: float) -> bool:
    """Whether more than one PBM peak falls inside the FID bounds (§VI.11).

    The delay convention is AutoLib's: the MS retention time is corrected as
    ``p.rt + delay`` before being compared with the FID integration limits
    (``AutoLib.assign``). Using the opposite sign would shift every MS peak by
    twice the delay.

    ``pbm_rows`` may be anything iterable of PBM peaks: objects with ``.rt``,
    mappings with an ``"rt"`` key, or bare numbers. The bounds are read from
    ``row.fid_start``/``fid_end``, falling back to ``start_tm``/``end_tm`` for
    rows that predate the v3.0 fields.
    """
    lo = _first_not_none(getattr(row, "fid_start", None),
                         getattr(row, "start_tm", None))
    hi = _first_not_none(getattr(row, "fid_end", None),
                         getattr(row, "end_tm", None))
    if lo is None or hi is None:
        return False
    lo, hi = float(lo), float(hi)
    if hi < lo:
        lo, hi = hi, lo
    d = float(delay or 0.0)
    n = 0
    for p in pbm_rows or ():
        rt = _rt_of(p)
        if rt is None:
            continue
        if lo <= rt + d <= hi:
            n += 1
            if n > 1:
                return True
    return False


def _rt_of(p: Any) -> Optional[float]:
    rt = getattr(p, "rt", None)
    if rt is None and isinstance(p, dict):
        rt = p.get("rt")
    if rt is None and isinstance(p, (int, float)):
        rt = p
    try:
        return float(rt) if rt is not None else None
    except (TypeError, ValueError):
        return None


def _first_not_none(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


# --------------------------------------------------------------------------
# Kovats retention index
# --------------------------------------------------------------------------

def alkane_ladder(rows: Any) -> dict[int, float]:
    """Carbon number -> retention time, from the n-alkanes among ``rows``.

    A row joins the ladder only if its name *is* an n-alkane after stripping a
    leading ``n-`` -- not if it merely contains one. "Heptadecane,
    2,6,10,14-tetramethyl-" (pristane) elutes well away from n-heptadecane, and
    admitting it would bias every RI in that segment.

    Where two rows carry the same alkane the larger area wins; the ladder is a
    set of landmarks, and the small one is a shoulder or a misassignment.
    """
    best: dict[int, tuple[float, float]] = {}     # n -> (area, rt)
    for row in rows or ():
        name = _row_name(row)
        n = alkane_number(name)
        if n is None:
            continue
        rt = _first_not_none(getattr(row, "rt", None),
                             row.get("rt") if isinstance(row, dict) else None)
        if rt is None:
            continue
        try:
            rt = float(rt)
        except (TypeError, ValueError):
            continue
        area = _first_not_none(getattr(row, "area", None),
                               row.get("area") if isinstance(row, dict) else None)
        try:
            area = float(area) if area is not None else 0.0
        except (TypeError, ValueError):
            area = 0.0
        prev = best.get(n)
        if prev is None or area > prev[0]:
            best[n] = (area, rt)
    return {n: rt for n, (_a, rt) in sorted(best.items())}


def alkane_number(name: str) -> Optional[int]:
    """Carbon number if ``name`` is an unsubstituted n-alkane, else ``None``."""
    if not name:
        return None
    s = str(name).strip().lower().rstrip(".;, ")
    # Drop a trailing CAS-style parenthesis and any leading locant prefix.
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    m = _ALKANE_RE.match(s)
    return ALKANES[m.group(1)] if m else None


def _row_name(row: Any) -> str:
    name = getattr(row, "name", None)
    if name is None and isinstance(row, dict):
        name = row.get("name")
    return str(name or "")


def usable_ladder(ladder: dict[int, float]) -> dict[int, float]:
    """The strictly increasing subset of ``ladder`` that RI interpolates over.

    A ladder whose retention times do not rise with carbon number contains a
    misassignment. Interpolating across such a rung produces retention indices
    that move backwards, so the offending rungs are dropped rather than used.
    Kept public because the ``Alkanreihe`` dialog must be able to show the
    analyst which of their rungs are actually in play.
    """
    clean: dict[int, float] = {}
    last = None
    for n in sorted(ladder or {}):
        try:
            t = float(ladder[n])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(t):
            continue
        if last is not None and t <= last:
            continue
        clean[int(n)] = t
        last = t
    return clean


def ladder_problems(ladder: dict[int, float]) -> list[str]:
    """German warnings about an implausible alkane ladder (§VI.12 dialog).

    n-Alkanes elute strictly in carbon-number order on any column in scope, so
    a rung out of order is proof of a wrong identification -- and a PBM library
    search is very willing to call any hydrocarbon-shaped spectrum an alkane.
    Returned as messages rather than raised: the dialog exists so the analyst
    can correct the ladder, not so the workspace can refuse it.
    """
    msgs: list[str] = []
    items = [(n, float(t)) for n, t in sorted((ladder or {}).items())]
    if len(items) < 2:
        msgs.append(f"Alkanreihe hat nur {len(items)} Stufe(n); "
                    f"fuer einen Retentionsindex sind mindestens zwei noetig.")
        return msgs
    keep = usable_ladder(ladder)
    last_n, last_t = items[0]
    for n, t in items[1:]:
        if t <= last_t:
            msgs.append(f"C{n} ({t:.3f} min) eluiert vor C{last_n} "
                        f"({last_t:.3f} min) - Reihenfolge unplausibel.")
        else:
            last_n, last_t = n, t
    dropped = sorted(set(ladder) - set(keep))
    if dropped:
        msgs.append("Nicht verwendet: "
                    + ", ".join(f"C{n}" for n in dropped) + ".")
    return msgs


def retention_index(rt: Optional[float],
                    ladder: dict[int, float]) -> Optional[float]:
    """Temperature-programmed Kovats retention index, or ``None`` (§VI.12).

    ``RI = 100·n + 100·(N−n)·(rt − rt_n)/(rt_N − rt_n)`` for the bracketing
    ladder rungs ``n < N``. With a complete ladder ``N = n+1`` and this is
    exactly the formula of §VI.12; the ``(N−n)`` factor only matters when the
    ladder has gaps, where the spec's two-term form would otherwise return a
    number that is wrong by a whole carbon.

    Outside the ladder the answer is ``None``. Extrapolating a Kovats index is
    meaningless -- the oven program outside the bracket is not constrained by
    the two rungs at all -- and a wrong RI is worse than a missing one.

    The ladder is reduced by :func:`usable_ladder` first. Note that this makes
    the result *self-consistent*, not *correct*: a ladder built from wrong
    identifications still yields smoothly interpolated nonsense. Check
    :func:`ladder_problems` before believing an RI column.
    """
    if rt is None or not ladder:
        return None
    try:
        x = float(rt)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None

    clean = sorted(usable_ladder(ladder).items())
    if len(clean) < 2:
        return None
    if x < clean[0][1] or x > clean[-1][1]:
        return None

    for (n, tn), (nn, tnn) in zip(clean, clean[1:]):
        if tn <= x <= tnn:
            if tnn <= tn:
                return None
            return 100.0 * n + 100.0 * (nn - n) * (x - tn) / (tnn - tn)
    return None


# --------------------------------------------------------------------------
# Whole-sample pass
# --------------------------------------------------------------------------

def qc_pass(rows: Sequence[Any],
            *,
            noise: Optional[Noise] = None,
            hz: float = 0.0,
            ms: Any = None,
            pbm_rows: Any = (),
            delay: float = 0.0,
            ladder: Optional[dict[int, float]] = None,
            should_cancel: Optional[Callable[[], bool]] = None,
            ) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(row_id, metrics)`` for every row, one row at a time.

    Not part of the binding contract of §VI.19 -- a convenience so the
    background pass of §VI.11 has one place to call. A generator rather than a
    function returning a list precisely so it is cancellable: the caller stops
    iterating, or ``should_cancel()`` returns True, and the pass ends between
    rows without leaving half-written state anywhere.

    Every metric is optional and independently ``None``: a row with no MS
    linkage still gets its S/N, a row outside the alkane ladder still gets its
    purity.
    """
    for row in rows or ():
        if should_cancel is not None and should_cancel():
            return
        m: dict[str, Any] = {}
        height = _first_not_none(getattr(row, "height", None),
                                 row.get("height") if isinstance(row, dict) else None)
        m["sn"] = signal_to_noise(height, noise) if noise is not None else float("nan")

        lo = _first_not_none(getattr(row, "fid_start", None),
                             getattr(row, "start_tm", None))
        hi = _first_not_none(getattr(row, "fid_end", None),
                             getattr(row, "end_tm", None))
        width = (float(hi) - float(lo)) if (lo is not None and hi is not None) else None
        m["lod"], m["loq"] = lod_loq(noise, width, hz)

        purity = None
        if ms is not None:
            first = getattr(row, "first_scan", None)
            last = getattr(row, "last_scan", None)
            apex = getattr(row, "effective_apex", None)
            if apex is None:
                apex = getattr(row, "apex_scan", None)
            if first is not None and last is not None and apex is not None:
                purity = peak_purity(ms, int(apex), int(first), int(last))
        m["purity"] = purity

        m["coelution"] = coelution(row, pbm_rows, delay)
        m["ri"] = retention_index(getattr(row, "rt", None), ladder or {})
        yield (int(getattr(row, "row_id", 0) or 0), m)


# --------------------------------------------------------------------------
# ISTD area window (§VII.6)
# --------------------------------------------------------------------------

@dataclass
class IstdCheck:
    """One standard's verdict against the area window.

    ``area`` is the standard's **effective** raw FID area -- the hand-typed
    value when the analyst entered one, otherwise what AutoLib measured. An
    ISTD row is never blank-subtracted (``gc_fid.py``, ``_apply_blank``), so
    for a standard the raw area and the corrected area are the same number and
    there is no ambiguity about which one the window applies to.

    ``label`` is the determination label, carried through rather than looked up,
    because the banner has to name both determinations in one sentence and this
    function is only ever given one determination's table at a time.
    """

    label: str
    name: str
    area: Optional[float]
    #: ``"ok"`` | ``"low"`` | ``"high"`` | ``"missing"`` | ``"exempt"``.
    verdict: str

    @property
    def ok(self) -> bool:
        """Whether this standard has nothing to say -- the banner's own test.

        ``exempt`` counts as quiet: the QC standard was measured and the window
        was deliberately not applied to it, which is not a finding. Callers
        must ask this rather than comparing the verdict to ``"ok"``, or a new
        quiet verdict silently becomes an alarm again.
        """
        return self.verdict in (ISTD_OK, ISTD_EXEMPT)

    @property
    def exempt(self) -> bool:
        """Whether the area window was skipped for this standard."""
        return self.verdict == ISTD_EXEMPT

    @property
    def millions(self) -> Optional[float]:
        """``area`` in millions, which is the unit the banner speaks in."""
        return None if self.area is None else self.area / 1e6


def istd_window(standards: Any,
                label: str = "",
                low: float = ISTD_AREA_LOW,
                high: float = ISTD_AREA_HIGH) -> list[IstdCheck]:
    """One verdict per standard in ``standards`` (spec v3.1 SS VII.6).

    ``standards`` is the list of dicts ``gc_fid.standards_table`` puts on
    ``sample.meta["standards"]`` and ``NiasSample.recompute_factors`` keeps
    current, so calling this after a recalculation always judges the numbers the
    Standards panel is showing.

    Every standard is checked **except against the window itself**: the QC
    standard (DBP-d4) comes back ``exempt`` (spec v3.1 SS VII.6, revised
    2026-09-06). It is spiked deliberately low, so ``low`` was its normal
    verdict on a perfectly good run, and a banner that is up on every run is a
    banner nobody reads. The earlier reading -- "any ISTD" taken literally
    (SS VII.15, assumption 3) -- is what this replaces.

    The QC standard is still *reported*: ``area`` is filled in exactly as
    before, so the Standards panel and any caller that wants the number keeps
    it, and a QC standard that was not found is still ``missing``. Whether its
    area is large enough at all remains AutoLib's ``qc_min_area`` /
    ``Below QC minimum`` question, which nothing here touches.

    ``missing`` when the standard was not found or carries no area. That is
    already an error elsewhere (``analyse_determination`` refuses a missing
    quantification standard); it is carried here only so the banner can say it
    in the same sentence instead of leaving a silent gap in the list.

    Pure: nothing is written, nothing is cached, and the order of ``standards``
    is preserved so the banner lists them in the Standards panel's own order.
    """
    out: list[IstdCheck] = []
    for std in standards or ():
        if isinstance(std, Mapping):
            name = str(std.get("name") or "")
            # ``fid_area`` is the key ``standards_table`` writes and
            # ``recompute_factors`` refreshes; ``area`` is accepted as well
            # because the spec's §VII.6 "Ist" names it and a hand-built table
            # in a test should not have to know which of the two is real.
            raw = std.get("fid_area", None)
            if raw is None:
                raw = std.get("area", None)
            status = str(std.get("status") or "")
            role = str(std.get("role") or "")
        else:
            name = str(getattr(std, "name", "") or "")
            raw = getattr(std, "fid_area", None)
            if raw is None:
                raw = getattr(std, "area", None)
            status = str(getattr(std, "status", "") or "")
            role = str(getattr(std, "role", "") or "")

        area = _area_value(raw)
        if area is None or status == "Not found":
            verdict = ISTD_MISSING
            area = None
        elif is_window_exempt(name, role):
            # Measured, kept, not judged. The area stays on the check so the
            # number is still available to whoever wants to show it.
            verdict = ISTD_EXEMPT
        elif area < low:
            verdict = ISTD_LOW
        elif area > high:
            verdict = ISTD_HIGH
        else:
            verdict = ISTD_OK
        out.append(IstdCheck(label=label, name=name, area=area, verdict=verdict))
    return out


def is_window_exempt(name: str = "", role: str = "") -> bool:
    """Whether the ISTD area window is skipped for this standard (SS VII.6).

    ``role`` is what ``standards_table`` and ``recompute_factors`` carry and is
    the real answer. ``name`` is the fallback for a row that has no role -- a
    hand-built table in a test, or an old session file -- and matches only
    :data:`QC_NAME_FRAGMENT`, never the ``-d4`` suffix the quantification
    standards share.
    """
    if str(role or "").strip().casefold() in ISTD_WINDOW_ROLES_EXCLUDED:
        return True
    if str(role or "").strip():
        # A row that states a role states it fully; guessing from the name on
        # top of it could exempt a quantification standard.
        return False
    return QC_NAME_FRAGMENT in str(name or "").casefold()


def _area_value(value: Any) -> Optional[float]:
    """``value`` as a finite float, or ``None``.

    A zero area is *not* a measurement: AutoLib leaves 0.0 where it found
    nothing, and calling that "low" would send an analyst after a liner when
    the real fault is a standard that was never integrated.
    """
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0.0:
        return None
    return out
