"""Mass spectra of integrated peaks.

Default mode ``average_bg`` follows the MassHunter/ChemStation practice for a
clean library-search spectrum: average the scans across the top of the peak
and subtract a background interpolated per ion between the scans just
before the peak start and just after its end.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from gcws.core.keys import is_fid
from gcws.core.model import parse_key

MODES = {
    "average_bg": "Average of peak top minus background (start/end)",
    "apex": "Apex scan",
    "apex_minus_start": "Apex minus start scan (PBM)",
    "deconvoluted": "Deconvoluted component",
}


@dataclass
class Spectrum:
    mz: np.ndarray                 # nominal masses
    ab: np.ndarray                 # abundance
    rt: float
    mode: str
    apex_scans: list[int] = field(default_factory=list)
    bg_scans: list[int] = field(default_factory=list)
    note: str = ""

    def points(self, min_permille: float = 0.0, normalise: bool = True) -> list[tuple[int, float]]:
        if self.ab.size == 0 or self.ab.max() <= 0:
            return []
        ab = self.ab / self.ab.max() * 999.0 if normalise else self.ab
        keep = ab >= (min_permille / 1000.0 * 999.0 if normalise else 0.0)
        keep &= ab > 0
        return [(int(m), round(float(a), 2)) for m, a in zip(self.mz[keep], ab[keep])]

    def top_ions(self, n: int = 3) -> list[int]:
        if self.ab.size == 0:
            return []
        order = np.argsort(self.ab)[::-1][:n]
        return [int(self.mz[i]) for i in order]


@dataclass
class ScanRequest:
    """A spectrum asked for by time (right-click / right-drag), all times on the MS axis."""
    run_id: str
    t0: float | None
    t1: float | None
    bg: tuple[float, float] | None = None


def to_ms(t: float, key: str, delay: float) -> float:
    """Time on a ``key`` axis -> MS time (FID times are shifted by the delay)."""
    return t - delay if is_fid(key) else t


def from_ms(t_ms: float, key: str, delay: float) -> float:
    """MS time -> time on a ``key`` axis."""
    return t_ms + delay if is_fid(key) else t_ms


def subtract(a: tuple[np.ndarray, np.ndarray], b: tuple[np.ndarray, np.ndarray],
             f: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """``a - f*b`` for nominal spectra given as sorted (mz, ab) arrays; keeps positives."""
    ma, aa = a
    mb, ab = b
    if mb.size == 0:
        return ma, aa
    masses = np.union1d(ma, mb)
    out = np.zeros(masses.size)
    out[np.searchsorted(masses, ma)] += aa
    out[np.searchsorted(masses, mb)] -= f * ab
    keep = out > 0
    return masses[keep].astype(np.int64), out[keep]


def extract_range(run, t0_ms: float, t1_ms: float | None = None, bg: tuple[float, float] | None = None,
                  blank=None) -> Spectrum | None:
    """Spectrum of one scan (``t1_ms`` None or equal) or the mean over a time range.

    ``bg``: an MS time range whose mean spectrum is subtracted (scans of the
    range itself are never used as background). ``blank``: optional callable
    ``blank(scans) -> (mz, ab)`` returning the aligned blank spectrum to subtract.
    """
    ms = run.ms
    if ms is None or ms.n_scans == 0:
        return None
    if t1_ms is None or abs(t1_ms - t0_ms) < 1e-9:
        scans = np.array([ms.scan_at_rt(t0_ms)])
    else:
        lo, hi = sorted((t0_ms, t1_ms))
        scans = ms.scans_between(lo, hi)
        if scans.size == 0:
            scans = np.array([ms.scan_at_rt(0.5 * (lo + hi))])
    mz, ab = ms.nominal_spectrum_arrays(scans)
    notes = [f"scan {int(scans[0]) + 1}" if scans.size == 1 else
             f"{scans.size} scans averaged ({ms.rt[scans[0]]:.3f}-{ms.rt[scans[-1]]:.3f} min)"]
    bg_scans: list[int] = []
    if bg is not None:
        b0, b1 = sorted(bg)
        cand = ms.scans_between(b0, b1)
        if cand.size == 0:
            cand = np.array([ms.scan_at_rt(0.5 * (b0 + b1))])
        bg_scans = [int(s) for s in cand if s not in set(scans.tolist())]
        if bg_scans:
            mz, ab = subtract((mz, ab), ms.nominal_spectrum_arrays(bg_scans))
            notes.append(f"minus background of {len(bg_scans)} scans ({b0:.3f}-{b1:.3f} min)")
    if blank is not None:
        bmz, bab = blank(scans)
        if bmz.size:
            mz, ab = subtract((mz, ab), (bmz, bab))
            notes.append("blank spectrum subtracted")
    return Spectrum(mz, ab, float(np.mean(ms.rt[scans])), "scan", [int(s) for s in scans], bg_scans,
                    ", ".join(notes))


def ms_times(peak, key: str, delay: float) -> tuple[float, float, float]:
    """Peak start/end/apex on the MS time axis (FID peaks are delay-shifted)."""
    if is_fid(key):
        return peak.start - delay, peak.end - delay, peak.apex_rt - delay
    return peak.start, peak.end, peak.apex_rt


def _from_dict(d: dict[int, float]) -> tuple[np.ndarray, np.ndarray]:
    if not d:
        return np.zeros(0, int), np.zeros(0)
    mz = np.array(sorted(d), dtype=int)
    return mz, np.array([d[m] for m in mz], dtype=float)


def _sub(a: dict, b: dict, f: float = 1.0) -> dict:
    out = dict(a)
    for m, v in b.items():
        out[m] = out.get(m, 0.0) - f * v
    return {m: v for m, v in out.items() if v > 0}


def extract(run, peak, key: str, delay: float, mode: str = "average_bg",
            top_fraction: float = 0.5, n_bg: int = 3, override: dict | None = None) -> Spectrum | None:
    ms = run.ms
    if ms is None or peak is None:
        return None
    t0, t1, ta = ms_times(peak, key, delay)
    scans = ms.scans_between(t0, t1)
    if override and override.get("apex_scans"):
        apex_scans = [s for s in override["apex_scans"] if 0 <= s < ms.n_scans]
        bg_scans = [s for s in override.get("bg_scans", []) if 0 <= s < ms.n_scans]
        mean = ms.nominal_spectrum(apex_scans)
        spec = _sub(mean, ms.nominal_spectrum(bg_scans)) if bg_scans else mean
        mz, ab = _from_dict(spec)
        return Spectrum(mz, ab, ta, "manual", apex_scans, bg_scans, "analyst-defined scans")
    if scans.size == 0:
        i = ms.scan_at_rt(ta)
        if abs(ms.rt[i] - ta) > 0.05:
            return Spectrum(np.zeros(0, int), np.zeros(0), ta, mode, note="no MS data at this time")
        scans = np.array([i])
    sig_key = key if parse_key(key)[0] == "EIC" else "TIC"
    trace = run.signal(sig_key).y if run.signal(sig_key) is not None else ms.tic()
    seg = trace[scans]
    line = np.linspace(seg[0], seg[-1], seg.size) if seg.size > 1 else seg
    height = seg - line
    k = int(np.argmax(height))
    apex = int(scans[k])

    if mode == "apex":
        mz, ab = _from_dict(ms.nominal_spectrum([apex]))
        return Spectrum(mz, ab, float(ms.rt[apex]), mode, [apex], [])

    if mode == "apex_minus_start":
        start = int(scans[0])
        spec = _sub(ms.nominal_spectrum([apex]), ms.nominal_spectrum([start]))
        mz, ab = _from_dict(spec)
        return Spectrum(mz, ab, float(ms.rt[apex]), mode, [apex], [start])

    if mode == "deconvoluted":
        comp = deconvoluted_component(run, ta)
        if comp is not None:
            mz = np.array([int(m) for m, _ in comp.spectrum], dtype=int)
            ab = np.array([float(v) for _, v in comp.spectrum], dtype=float)
            return Spectrum(mz, ab, float(comp.rt), mode, [int(comp.apex_scan)], [],
                            f"component purity {comp.purity:.2f}, {comp.n_ions} model ions")
        mode = "average_bg"

    # average_bg
    top = height[k]
    lo = hi = k
    while lo > 0 and height[lo - 1] >= top_fraction * top:
        lo -= 1
    while hi < height.size - 1 and height[hi + 1] >= top_fraction * top:
        hi += 1
    apex_scans = [int(s) for s in scans[lo:hi + 1]]
    first, last = int(scans[0]), int(scans[-1])
    pre = [s for s in range(first - n_bg, first) if 0 <= s < ms.n_scans]
    post = [s for s in range(last + 1, last + 1 + n_bg) if 0 <= s < ms.n_scans]
    mean = ms.nominal_spectrum(apex_scans)
    if pre and post:
        bpre, bpost = ms.nominal_spectrum(pre), ms.nominal_spectrum(post)
        tpre, tpost = float(np.mean(ms.rt[pre])), float(np.mean(ms.rt[post]))
        tm = float(np.mean(ms.rt[apex_scans]))
        f = (tm - tpre) / (tpost - tpre) if tpost > tpre else 0.5
        bg = {m: bpre.get(m, 0.0) * (1 - f) + bpost.get(m, 0.0) * f for m in set(bpre) | set(bpost)}
    elif pre or post:
        bg = ms.nominal_spectrum(pre or post)
    else:
        bg = {}
    spec = _sub(mean, bg)
    mz, ab = _from_dict(spec)
    return Spectrum(mz, ab, float(np.mean(ms.rt[apex_scans])), "average_bg", apex_scans, pre + post,
                    f"{len(apex_scans)} scans averaged, {len(pre) + len(post)} background scans")


def deconvoluted_component(run, rt_ms: float, params=None):
    """Component of the vendored deconvolution nearest to ``rt_ms``."""
    import gc_deconv
    src = run.ms_source
    if src is None:
        return None
    try:
        comps = gc_deconv.deconvolute(src, rt_ms, params or gc_deconv.DeconvParams())
    except Exception:  # noqa: BLE001
        return None
    if not comps:
        return None
    return min(comps, key=lambda c: (abs(c.rt - rt_ms), -c.area))
