"""Mass spectra of integrated peaks.

Default mode ``average_bg`` uses the retained component on split peaks.
For unassigned peaks it averages the peak top and subtracts an interpolated
background. Explicit ``raw_*`` modes bypass component and manual assignments.

The background is the baseline under the peak. Next to a peak's boundaries the scans are
normally baseline; in a cluster they sit on the neighbouring peaks, and subtracting them takes
the peak's own ions away together with the neighbours' (a peak between two neighbours of a
similar spectrum kept little more than noise). There the baseline is taken from the nearest
scans before and after that lie on the baseline (the trace's SNIP envelope), and of the valley
next to the peak only what is not the peak's own spectrum is subtracted: the ions of a different
neighbour. ``average_bg_classic`` keeps the adjacent scans.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from gcws.core.keys import is_fid
from gcws.core.model import parse_key

#: Scans are baseline when the trace there is at most this share of the peak's height above its
#: SNIP envelope (window ``BASELINE_WINDOW`` min); baseline scans are looked for up to ``MAX_WALK``
#: min before and after the peak. Measured on synthetic clusters of real peaks (known spectra) and
#: on the A/B runs of the sample batch.
CLEAN_FRACTION = 0.1
BASELINE_WINDOW = 1.0
MAX_WALK = 1.0

MODES = {
    "average_bg": "Assigned component, otherwise average minus background",
    "average_bg_classic": "Assigned component, otherwise average minus adjacent scans (classic)",
    "apex": "Apex scan",
    "apex_minus_start": "Apex minus start scan (PBM)",
    "deconvoluted": "Deconvoluted component",
    "raw_average_bg": "Raw scans: average minus background (ignore assignment)",
    "raw_apex": "Raw scans: apex (ignore assignment)",
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
            top_fraction: float = 0.5, n_bg: int = 3, override: dict | None = None,
            component=None) -> Spectrum | None:
    """Spectrum of an integrated peak. ``component``: callable returning the deconvoluted
    component of the peak (mode "deconvoluted"). Explicit raw modes bypass overrides;
    otherwise analyst overrides precede retained split components."""
    ms = run.ms
    if ms is None or ms.n_scans == 0 or peak is None:
        return None
    raw = mode.startswith("raw_")
    if raw:
        mode = mode[4:]
        override = None
    t0, t1, ta = ms_times(peak, key, delay)
    scans = ms.scans_between(t0, t1)
    if override and override.get("component"):
        pc = override["component"]
        mz = np.array([int(m) for m, _ in pc["spectrum"]], dtype=int)
        ab = np.array([float(v) for _, v in pc["spectrum"]], dtype=float)
        scan = ms.scan_at_rt(float(pc["rt"]))
        return Spectrum(mz, ab, float(pc["rt"]), "deconvoluted", [scan], [],
                        f"pinned deconvoluted component {pc['rt']:.3f} min (model m/z {pc.get('model_mz', '?')})")
    if override and override.get("apex_scans"):
        apex_scans = [s for s in override["apex_scans"] if 0 <= s < ms.n_scans]
        bg_scans = [s for s in override.get("bg_scans", []) if 0 <= s < ms.n_scans and s not in apex_scans]
        if not apex_scans:
            return Spectrum(np.zeros(0, int), np.zeros(0), ta, "manual", note="no valid analyst-defined scans")
        mean = ms.nominal_spectrum(apex_scans)
        spec = _sub(mean, ms.nominal_spectrum(bg_scans)) if bg_scans else mean
        mz, ab = _from_dict(spec)
        return Spectrum(mz, ab, ta, "manual", apex_scans, bg_scans, "analyst-defined scans")
    if not raw and mode in ("average_bg", "average_bg_classic", "deconvoluted"):
        from gcws.ms.deconv import allocated_component
        assigned = allocated_component(ms, peak)
        if assigned is not None:
            return Spectrum(np.array([int(m) for m, _ in assigned.spectrum]),
                            np.array([float(a) for _, a in assigned.spectrum]),
                            float(assigned.rt), "deconvoluted", [int(assigned.apex_scan)], [],
                            f"assigned component {assigned.rt:.4f} min (MS), model m/z {assigned.model_mz}")
    if scans.size == 0:
        i = ms.scan_at_rt(ta)
        if abs(ms.rt[i] - ta) > 0.05:
            return Spectrum(np.zeros(0, int), np.zeros(0), ta, mode, note="no MS data at this time")
        scans = np.array([i])
    sig_key = key if parse_key(key)[0] == "EIC" else "TIC"
    trace = run.signal(sig_key).y if run.signal(sig_key) is not None else ms.tic()
    seg = trace[scans]
    line = np.interp(ms.rt[scans], [ms.rt[scans[0]], ms.rt[scans[-1]]], [seg[0], seg[-1]]) if seg.size > 1 else seg
    height = seg - line
    k = int(np.argmax(height))
    positive_top = bool(height[k] > 0)
    if not positive_top:
        k = int(np.argmin(np.abs(ms.rt[scans] - ta)))
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
        comp = component() if component is not None else deconvoluted_component(run, peak, key, delay)
        if comp is not None:
            mz = np.array([int(m) for m, _ in comp.spectrum], dtype=int)
            ab = np.array([float(v) for _, v in comp.spectrum], dtype=float)
            quality = f", quality {comp.quality:.0f}" if hasattr(comp, "quality") else ""
            return Spectrum(mz, ab, float(comp.rt), mode, [int(comp.apex_scan)], [],
                            f"deconvoluted component {comp.rt:.3f} min: model m/z {comp.model_mz}, "
                            f"purity {comp.purity:.2f}{quality}")
        mode = "average_bg"
        fallback_note = "no deconvoluted component found: average spectrum shown"

    # average_bg
    top = height[k]
    lo = hi = k
    while positive_top and lo > 0 and height[lo - 1] >= top_fraction * top:
        lo -= 1
    while positive_top and hi < height.size - 1 and height[hi + 1] >= top_fraction * top:
        hi += 1
    apex_scans = [int(s) for s in scans[lo:hi + 1]]
    first, last = int(scans[0]), int(scans[-1])
    pre = [s for s in range(first - n_bg, first) if 0 <= s < ms.n_scans]
    post = [s for s in range(last + 1, last + 1 + n_bg) if 0 <= s < ms.n_scans]
    if mode == "average_bg" and positive_top:
        spec = _clean_background(ms, trace, sig_key, apex, apex_scans, first, last, pre, post, n_bg)
        if spec is not None:
            if "fallback_note" in locals():
                spec.note = fallback_note + "; " + spec.note
            return spec
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
    note = f"{len(apex_scans)} scans averaged, {len(pre) + len(post)} background scans"
    if not positive_top:
        note += "; no positive peak top: nearest scan to requested apex"
    if "fallback_note" in locals():
        note = fallback_note + "; " + note
    return Spectrum(mz, ab, float(np.mean(ms.rt[apex_scans])), mode, apex_scans, pre + post, note)


def _envelope(ms, key: str, trace: np.ndarray) -> np.ndarray:
    """The SNIP baseline of the run's ``key`` trace, computed once per run."""
    from gcws.signal.envelope import envelope
    cache = getattr(ms, "_bg_envelope", None)
    if cache is None:
        cache = ms._bg_envelope = {}
    env = cache.get(key)
    if env is None or env.size != trace.size:
        env = cache[key] = envelope(ms.rt, trace, BASELINE_WINDOW)
    return env


def _baseline_group(excess: np.ndarray, starts: np.ndarray, n_bg: int):
    """The first group of ``n_bg`` scans from ``starts`` (in the order they are looked at) whose mean
    excess over the envelope is baseline, and True; otherwise the lowest group and False."""
    starts = starts[(starts >= 0) & (starts + n_bg <= excess.size)]
    if not starts.size:
        return None, False
    c = np.concatenate([[0.0], np.cumsum(excess)])
    level = (c[starts + n_bg] - c[starts]) / n_bg
    hit = np.flatnonzero(level <= CLEAN_FRACTION)
    s = int(starts[hit[0]] if hit.size else starts[int(np.argmin(level))])
    return list(range(s, s + n_bg)), bool(hit.size)


def _clean_background(ms, trace, key: str, apex: int, apex_scans: list, first: int, last: int, pre0: list,
                      post0: list, n_bg: int) -> Spectrum | None:
    """The peak top minus its baseline when the scans next to the peak (scans ``first`` to ``last``)
    are not baseline (a peak in a cluster), or None when they are (then the adjacent scans are the
    background, as always).

    The baseline spectrum is interpolated between the nearest baseline scans before and after the
    peak (one side only when the other has none within ``MAX_WALK``; the lowest scans of both sides
    when neither has). Of each valley next to the peak, minus that baseline, the least-squares share
    of the peak's own spectrum stays: the rest - the ions of a different neighbour - is interpolated
    to the peak top and subtracted as well."""
    trace = np.asarray(trace, dtype=float)
    env = _envelope(ms, key, trace)
    height = trace[apex] - env[apex]
    if not height > 0:
        return None
    excess = (trace - env) / height

    def baseline(group):
        return not group or float(np.mean(excess[group])) <= CLEAN_FRACTION
    clean0 = (baseline(pre0), baseline(post0))
    if all(clean0):
        return None
    reach = max(1, int(round(MAX_WALK / float(np.median(np.diff(ms.rt))))))
    pre, pre_ok = (pre0, True) if clean0[0] and pre0 else \
        _baseline_group(excess, np.arange(first - n_bg, first - n_bg - reach, -1), n_bg)
    post, post_ok = (post0, True) if clean0[1] and post0 else \
        _baseline_group(excess, np.arange(last + 1, last + 1 + reach), n_bg)
    if pre_ok or post_ok:                     # a side without baseline in reach is left out
        pre, post = (pre if pre_ok else None), (post if post_ok else None)
    groups = {"top": apex_scans, "pre": pre, "post": post,
              "valley_pre": None if clean0[0] else pre0, "valley_post": None if clean0[1] else post0}
    arrays = {k: ms.nominal_spectrum_arrays(g) for k, g in groups.items() if g}
    axis = np.unique(np.concatenate([mz for mz, _ab in arrays.values()]))
    vec, when = {}, {}
    for k, (mz, ab) in arrays.items():
        vec[k] = np.zeros(axis.size)
        vec[k][np.searchsorted(axis, mz)] = ab
        when[k] = float(np.mean(ms.rt[groups[k]]))

    def interpolated(a, b, t):
        if a in vec and b in vec and when[b] > when[a]:
            f = (t - when[a]) / (when[b] - when[a])
            return (1.0 - f) * vec[a] + f * vec[b]
        return vec[a] if a in vec else vec[b] if b in vec else np.zeros(axis.size)
    spec = np.maximum(vec["top"] - interpolated("pre", "post", when["top"]), 0.0)
    norm = float(spec @ spec)
    for k in ("valley_pre", "valley_post"):
        if k in vec:
            net = np.maximum(vec[k] - interpolated("pre", "post", when[k]), 0.0)
            share = max(float(net @ spec) / norm, 0.0) if norm > 0 else 0.0
            vec[k] = np.maximum(net - share * spec, 0.0)          # the ions that are not the peak's own
    spec = np.maximum(spec - interpolated("valley_pre", "valley_post", when["top"]), 0.0)
    keep = spec > 0
    used = [g for g in (pre, post) if g]
    where = " and ".join(f"{ms.rt[g[0]]:.3f}-{ms.rt[g[-1]]:.3f}" for g in used)
    note = (f"{len(apex_scans)} scans averaged, background from the baseline scans at {where} min "
            if pre_ok or post_ok else
            f"{len(apex_scans)} scans averaged, no baseline within {MAX_WALK:g} min: background from the "
            f"lowest scans at {where} min ") + "(neighbouring peaks skipped), ions of the neighbours removed"
    return Spectrum(axis[keep].astype(np.int64), spec[keep], when["top"], "average_bg", apex_scans,
                    [s for g in used for s in g], note)


def deconvoluted_component(run, peak, key: str, delay: float, settings=None):
    """The deconvoluted component that represents ``peak`` (GC Workspace engine, one window)."""
    import logging
    from gcws.ms import deconv as D
    if run.ms is None or peak is None:
        return None
    allocated = D.allocated_component(run.ms, peak)
    if allocated is not None:
        return allocated
    t0, t1, ta = ms_times(peak, key, delay)
    try:
        res = D.deconvolute_window(run.ms, ta, settings or D.DeconvSettings())
    except Exception:  # noqa: BLE001 - logged; the caller falls back to the average spectrum
        logging.getLogger(__name__).exception("deconvolution at %.3f min failed", ta)
        return None
    return D.component_for_peak(res.components, t0, t1, ta)
