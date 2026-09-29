"""Gap filling: a peak found in one determination is searched for again in the other.

A peak that the integration found in A but not in B is often there in B too, just below the
integrator's thresholds or shaped slightly differently. Treating it as "not in B" (AutoLib:
"Artefact: only determination 1", not reported) loses real substances; integrating anything at
the expected time invents them. The gap filler therefore only fills a gap when the evidence is
there, and otherwise records the member as *not detectable* (never as area 0):

1. **Where.** The expected apex in the other run comes from the drift map (reference time of
   the found peak, mapped back); the search window is +- max(0.5 x FWHM, ``0.02`` min).
2. **MS.** On the EIC of the consensus quantifier ion (mzmine ``GCConsensusAlignerPostProcessor``:
   the ion found in most determinations, then the highest summed intensity) mzmine's gap
   filler (``gapfill_peakfinder/Gap``) looks for the peak: runs of points that rise into and fall
   out of the window (``intTolerance``), the highest local maximum inside the window, extended
   outwards while the signal keeps falling. mzmine keeps the *last* such candidate because its
   ``bestPeakHeight`` is never set; here the highest one wins. The characteristic ions must
   co-elute with it (apex within +-2 scans, Pearson r >= 0.8 as in mzmine's GC spectral
   deconvolution), and the spectrum at the apex must match the found peak's (weighted cosine
   over the found peak's ions).
3. **Quantification signal.** The FID (or TIC) must show a maximum there with S/N >= 3. The
   boundaries are the found peak's, relative to the apex (so the same integration rule holds in
   both determinations), each moved to the nearest local minimum and never into a neighbour.

Without a comparable spectrum (no MS, or too few co-eluting ions in the found peak) only step 3
decides, and the gap fill says so ("FID only"). Then the maximum must also lie close to the
expected apex (+- max(0.25 x FWHM, 0.01) min) and reach at least ``gap_min_fraction`` of the
expected height (the found peak's height times the median height ratio of the paired peaks of
the two determinations): a peak with S/N 100 in one injection is not "just below the threshold"
in the other, and a ripple of the baseline at that place is not that peak.

mzmine: MIT, Copyright (c) 2004-2025 The mzmine Development Team.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from gcws.core.keys import is_fid
from gcws.features import similarity as SIM
from gcws.features.model import GAPFILL_OPTION, MISSING, NOT_DETECTABLE, Feature, Proposal, Settings
from gcws.features.pseudo import pearson
from gcws.ms.knowledge import AIR_IONS, BLEED_IONS


@dataclass
class GapResult:
    ok: bool
    note: str
    apex: Optional[float] = None          # detector time
    t0: Optional[float] = None
    t1: Optional[float] = None
    sn: Optional[float] = None            # quantification signal
    ions: list = field(default_factory=list)   # co-eluting characteristic ions
    cos: Optional[float] = None
    ms_confirmed: bool = False


# -- mzmine ports ---------------------------------------------------------------------------------

def quant_ion(spectra) -> Optional[int]:
    """mzmine ``GCConsensusAlignerPostProcessor``: the m/z present in most of ``spectra`` (one value
    per spectrum and nominal mass), then the highest summed intensity (relative to each base peak)."""
    count: dict[int, int] = {}
    total: dict[int, float] = {}
    for spec in spectra:
        if spec is None or len(spec[0]) == 0:
            continue
        mz, ab = spec
        top = float(np.max(ab)) or 1.0
        for m, a in zip(mz, ab):
            m = int(m)
            count[m] = count.get(m, 0) + 1
            total[m] = total.get(m, 0.0) + float(a) / top
    if not count:
        return None
    return max(count, key=lambda m: (count[m], total[m]))


def gap_peak(rt: np.ndarray, y: np.ndarray, lo: float, hi: float, int_tol: float = 0.2,
             min_points: int = 1) -> Optional[tuple[int, int, int]]:
    """mzmine ``Gap``: ``(start, apex, stop)`` indices of the best peak with its maximum in
    [lo, hi], or None. Points are grouped into runs by ``checkRTShape`` (before the window only
    while rising, inside always, after only while falling, each within ``int_tol``); in each run
    ``checkCurrentPeak`` takes the highest inner local maximum inside the window and extends it
    outwards while the signal does not rise by more than ``int_tol`` (stopping after a zero)."""
    rt = np.asarray(rt, float)
    y = np.asarray(y, float)
    best: Optional[tuple[int, int, int]] = None
    best_h = -1.0

    def evaluate(run: list[int]):
        nonlocal best, best_h
        if len(run) < 3:
            return
        inner = [run[k] for k in range(1, len(run) - 1)
                 if lo <= rt[run[k]] <= hi and y[run[k]] > 0
                 and y[run[k]] >= y[run[k - 1]] and y[run[k]] >= y[run[k + 1]]]
        if not inner:
            return
        apex = max(inner, key=lambda i: y[i])
        pos = run.index(apex)
        s = pos
        cur = y[run[s]]
        while s > 0:
            nxt = y[run[s - 1]]
            if cur < nxt * (1 - int_tol):
                break
            s -= 1
            if nxt == 0:
                break
            cur = nxt
        e = pos
        cur = y[run[e]]
        while e < len(run) - 1:
            nxt = y[run[e + 1]]
            if nxt > cur * (1 + int_tol):
                break
            e += 1
            if nxt == 0:
                break
            cur = nxt
        if e - s + 1 < min_points:
            return
        if y[apex] > best_h:                      # mzmine: bestPeakHeight never set -> last wins
            best, best_h = (run[s], apex, run[e]), float(y[apex])

    run: list[int] = []
    for i in range(rt.size):
        if run:
            prev = y[run[-1]]
            if rt[i] < lo:
                keep = y[i] > prev * (1 - int_tol)
            elif rt[i] <= hi:
                keep = True
            else:
                keep = y[i] < prev * (1 + int_tol)
            if not keep:
                evaluate(run)
                run = []
        run.append(i)
    evaluate(run)
    return best


# -- helpers --------------------------------------------------------------------------------------

def characteristic_ions(spec, quant: Optional[int], n: int = 5) -> list[int]:
    """The quantifier ion first, then the most intense others (no bleed / air ions, m/z >= 50
    where possible)."""
    if spec is None or len(spec[0]) == 0:
        return [quant] if quant is not None else []
    mz, ab = spec
    order = [int(mz[i]) for i in np.argsort(-np.asarray(ab, float), kind="stable")]
    pool = [m for m in order if m not in BLEED_IONS and m not in AIR_IONS and m >= 50]
    pool += [m for m in order if m not in pool and m not in AIR_IONS]
    out = [quant] if quant is not None else []
    out += [m for m in pool if m != quant]
    return out[:n]


def _smooth(y: np.ndarray, n: int = 5) -> np.ndarray:
    if y.size < n:
        return y.astype(float)
    k = np.ones(n) / n
    return np.convolve(np.pad(y.astype(float), (n // 2, n // 2), mode="edge"), k, mode="valid")


def ms_check(ms, t_ms: float, window: float, ref_spec, ions: list[int], settings: Settings) -> tuple:
    """``(ok, apex_ms, coeluting ions, cosine, note)`` of the MS evidence at ``t_ms``."""
    if ms is None or ms.n_scans == 0 or not ions:
        return False, None, [], None, "no MS"
    ext = 3 * window
    scans = ms.scans_between(t_ms - window - ext, t_ms + window + ext)
    if scans.size < settings.gap_min_points:
        return False, None, [], None, "outside the MS acquisition"
    s0, s1 = int(scans[0]), int(scans[-1])
    lo, hi = min(ions), max(ions)
    block = ms.dense_block(s0, s1, lo, hi)
    rt = ms.rt[s0:s1 + 1]
    q = ions[0]
    eic = block[:, q - lo]
    found = gap_peak(rt, eic, t_ms - window, t_ms + window, settings.gap_int_tol, settings.gap_min_points)
    if found is None:
        return False, None, [], None, f"m/z {q} shows no peak"
    a, apex, b = found
    ref_prof = eic[a:b + 1]
    co = [q]
    for m in ions[1:]:
        prof = block[:, m - lo]
        seg = prof[a:b + 1]
        if np.count_nonzero(seg) < 3:
            continue
        top = a + int(np.argmax(seg))
        if abs(top - apex) <= 2 and pearson(seg, ref_prof) >= settings.gap_shape_r:
            co.append(m)
    need = min(settings.gap_min_ions, len(ions))
    # the spectrum at the apex (+-1 scan) minus the edges, over the found peak's ions
    ref_mz, ref_ab = ref_spec
    rlo, rhi = int(np.min(ref_mz)), int(np.max(ref_mz))
    sb = ms.dense_block(s0 + max(0, apex - 1), s0 + apex + 1, rlo, rhi).mean(axis=0)
    edge = (ms.dense_block(s0 + a, s0 + a, rlo, rhi)[0] + ms.dense_block(s0 + b, s0 + b, rlo, rhi)[0]) / 2
    here = np.clip(sb - edge, 0, None)[np.asarray(ref_mz, int) - rlo]
    cos = SIM.weighted_cosine((ref_mz, here), (ref_mz, ref_ab), SIM.WEIGHTS.get(settings.weights, SIM.NIST11))
    cos_v = cos.score if cos is not None else 0.0
    apex_ms = float(rt[apex])
    if len(co) < need:
        return False, apex_ms, co, cos_v, f"only {len(co)} of {len(ions)} ions co-elute ({', '.join(map(str, co))})"
    if cos_v < settings.gap_min_cos:
        return False, apex_ms, co, cos_v, f"spectrum differs (similarity {cos_v:.2f})"
    return True, apex_ms, co, cos_v, ""


def quant_check(sig, t_center: float, window: float, ref, neighbours: list, noise_pp: float,
                settings: Settings):
    """``(apex, t0, t1, S/N, note)`` of the peak on the quantification signal (apex None: not
    there). S/N = 2H/h with h the signal's peak-to-peak noise (Ph. Eur. 2.2.46, as for every peak)."""
    if sig is None:
        return None, None, None, None, "no quantification signal"
    left = max(ref.rt - ref.start, 0.004)
    right = max(ref.end - ref.rt, 0.004)
    sl = sig.window(t_center - window - 1.5 * left, t_center + window + 1.5 * right)
    rr, yy = sig.rt[sl], _smooth(sig.y[sl])
    inner = np.flatnonzero((rr >= t_center - window) & (rr <= t_center + window))
    if inner.size < 3:
        return None, None, None, None, "outside the signal"
    k = int(inner[np.argmax(yy[inner])])
    if k in (inner[0], inner[-1]):
        return None, None, None, None, "no maximum at the expected time"
    apex = float(rr[k])

    def snap(b: float, hw: float, before: bool) -> float:
        lo, hi = b - 0.3 * hw, b + 0.3 * hw
        if before:
            hi = min(hi, apex - 1e-6)
        else:
            lo = max(lo, apex + 1e-6)
        idx = np.flatnonzero((rr >= lo) & (rr <= hi))
        return float(rr[idx[np.argmin(yy[idx])]]) if idx.size else b

    t0, t1 = snap(apex - left, left, True), snap(apex + right, right, False)
    for n in neighbours:                           # never into an existing peak
        if n.end <= apex:
            t0 = max(t0, n.end)
        elif n.start >= apex:
            t1 = min(t1, n.start)
    if not t0 < apex < t1:
        return None, None, None, None, "no room between the neighbouring peaks"
    base = np.interp(apex, [t0, t1], [np.interp(t0, rr, yy), np.interp(t1, rr, yy)])
    height = float(yy[k] - base)
    sn = 2.0 * height / noise_pp if noise_pp > 0 else float("inf")
    if height <= 0 or sn < settings.gap_min_sn:
        return None, t0, t1, sn, f"S/N {max(sn, 0):.1f} < {settings.gap_min_sn:g}"
    return apex, t0, t1, sn, ""


# -- one gap ----------------------------------------------------------------------------------------

def height_ratio(table, run_a: str, run_b: str) -> float:
    """Median height of ``run_b`` / ``run_a`` over the features detected in both (1 without any)."""
    r = []
    for f in table.features:
        a, b = f.member(run_a), f.member(run_b)
        if a is not None and b is not None and a.found and b.found and a.peak.height > 0 and b.peak.height > 0:
            r.append(b.peak.height / a.peak.height)
    return float(np.median(r)) if r else 1.0


def fill(feature: Feature, run_id: str, run, runinput, tmap, settings: Settings, noise_pp: float,
         ratio: float = 1.0) -> GapResult:
    """Search the member ``run_id`` of ``feature`` (``run``: the raw run with ``.ms`` and ``.signal``;
    ``noise_pp``: peak-to-peak noise of its quantification signal)."""
    found = feature.found
    if not found:
        return GapResult(False, "nothing to compare with")
    ref = max(found, key=lambda m: m.peak.height)
    t_pred = float(tmap.from_ref(ref.rt_ref))
    window = max(0.5 * (ref.peak.width50 or 0.0), 0.02)
    peaks = runinput.peaks
    inside = [p for p in peaks if p.start < t_pred < p.end]
    if inside:
        return GapResult(False, f"inside the peak at {inside[0].rt:.3f} min (integrated as one peak)")
    close = [p for p in peaks if abs(p.rt - t_pred) <= window]
    if close:
        return GapResult(False, f"the peak at {close[0].rt:.3f} min is a different substance")
    neighbours = [p for p in peaks if abs(p.rt - t_pred) <= 10 * window]
    key = runinput.key
    sig = run.signal(key) if run is not None else None
    specs = [m.peak.spectrum for m in found]
    ms_ok, ions, cos, note_ms, t_center = False, [], None, "", t_pred
    comparable = ref.peak.spectrum is not None and len(ref.peak.spectrum[0]) >= settings.min_ions
    if comparable and run is not None and getattr(run, "ms", None) is not None:
        chars = characteristic_ions(ref.peak.spectrum, quant_ion(specs))
        delay = runinput.delay if is_fid(key) else 0.0
        ms_ok, apex_ms, ions, cos, note_ms = ms_check(run.ms, t_pred - delay, window, ref.peak.spectrum, chars,
                                                      settings)
        if not ms_ok:
            return GapResult(False, f"not detectable: {note_ms}", ions=ions, cos=cos)
        t_center = apex_ms + delay
    near = 0.6 * window if ms_ok else max(0.25 * (ref.peak.width50 or 0.0), 0.01)
    apex, t0, t1, sn, note_q = quant_check(sig, t_center, near, ref.peak, neighbours, noise_pp, settings)
    if apex is not None and not ms_ok and ref.peak.height > 0:
        expected = ref.peak.height * ratio
        i = sig.index_of(apex)
        base = np.interp(apex, [t0, t1], [sig.y[sig.index_of(t0)], sig.y[sig.index_of(t1)]])
        got = float(sig.y[i] - base)
        if got < settings.gap_min_fraction * expected:
            apex, note_q = None, (f"only {got / expected:.0%} of the expected height "
                                  f"(at least {settings.gap_min_fraction:.0%})")
    if apex is None:
        what = f"MS confirms m/z {', '.join(map(str, ions))}, but {key}: {note_q}" if ms_ok else f"{key}: {note_q}"
        return GapResult(False, f"not detectable: {what}", sn=sn, ions=ions, cos=cos, ms_confirmed=ms_ok)
    if ms_ok:
        note = f"ions {'/'.join(map(str, ions))} co-elute, similarity {cos:.2f}, S/N {sn:.0f}"
    else:
        note = f"{key} only (no comparable spectrum), S/N {sn:.0f}"
    return GapResult(True, note, apex, t0, t1, sn, ions, cos, ms_ok)


def fill_table(table, runs: dict, noise: dict, settings: Settings) -> None:
    """Search every missing member of ``table``'s features (``runs``: run id -> raw run, ``noise``:
    run id -> peak-to-peak noise of the quantification signal); sets the members' origin and note
    and adds the gap-fill proposals (ADD_PEAK events with the option ``"gapfill"``)."""
    from gcws.core.events import ManualEvent, ManualKind
    for f in table.features:
        if not f.found or f.split:
            continue
        for m in f.members:
            if m.found or m.origin not in (MISSING,):
                continue
            ri = table.input(m.run_id)
            if ri is None:
                continue
            src = max(f.found, key=lambda x: x.peak.height)
            ratio = height_ratio(table, src.run_id, m.run_id)
            res = fill(f, m.run_id, runs.get(m.run_id), ri, table.maps.get(m.run_id), settings,
                       noise.get(m.run_id, 0.0), ratio)
            if not res.ok:
                m.origin = NOT_DETECTABLE
                m.note = res.note
                continue
            text = f"gap fill {f.id} from {src.label}: {res.note}"
            ev = ManualEvent(ManualKind.ADD_PEAK, float(res.t0), float(res.t1), option=GAPFILL_OPTION,
                             comment=text)
            m.note = res.note
            f.proposals.append(Proposal("gapfill", m.run_id, ri.key, text, event=ev, rt=res.apex))
