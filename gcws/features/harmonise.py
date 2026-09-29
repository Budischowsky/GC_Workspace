"""The same integration rule in every determination -- proposed only where it demonstrably helps.

Two injections integrated with the same method still get slightly different boundaries: a
little more tailing, a valley found one scan earlier. The rule "same boundaries relative to the
apex" (the reference is the determination with the better signal-to-noise ratio) sounds right,
but on the reference batch it made half of the pairs worse: in crowded parts of the
chromatogram the boundaries are valleys shared with the neighbours, and forcing another run's
offsets there cuts peaks or adds baseline.

So a boundary is only proposed when

* it is a baseline boundary (not a valley shared with a neighbour) and the new position does not
  reach into a neighbour, and
* the area with the new boundaries, estimated on the signal (trapezoid above the line between
  the boundaries, scaled to the integrator's area of the current boundaries), brings the ratio of
  the two determinations' areas at least 10 % closer to the typical ratio of all pairs of the
  two runs (their median).

These are proposals: the analyst takes them over with one click (one undo step).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from gcws.features.model import DETECTED, Feature, Proposal, Settings

FRACTION = 0.25            # of the FWHM: offsets differing by more than this are looked at
MIN_MOVE = 0.1             # of the FWHM: smaller moves are not worth a proposal
MIN_GAIN = math.log(1.1)   # the area ratio must come at least 10 % closer to the typical one
SHARED = 1e-6


def _width(feature: Feature) -> float:
    ws = [m.peak.width50 for m in feature.found if m.peak.width50]
    return (sum(ws) / len(ws)) if ws else 0.01


def reference(feature: Feature):
    """The member with the best S/N (then the highest peak)."""
    cands = [m for m in feature.found if m.origin == DETECTED]
    if not cands:
        return None
    return max(cands, key=lambda m: ((m.peak.sn or 0.0), m.peak.height))


def typical_ratio(table, run_a: str, run_b: str) -> Optional[float]:
    """Median area ratio ``run_b / run_a`` over the features detected in both."""
    r = []
    for f in table.features:
        a, b = f.member(run_a), f.member(run_b)
        if a is not None and b is not None and a.origin == DETECTED and b.origin == DETECTED \
                and a.peak.area > 0 and b.peak.area > 0:
            r.append(b.peak.area / a.peak.area)
    return float(np.median(r)) if len(r) >= 5 else None


def signal_area(sig, t0: float, t1: float) -> Optional[float]:
    """Trapezoid of the signal above the straight line between its values at ``t0`` and ``t1``."""
    if sig is None or t1 <= t0:
        return None
    sl = sig.window(t0, t1)
    rr = np.concatenate([[t0], sig.rt[sl], [t1]])
    yy = np.interp(rr, sig.rt, sig.y)
    base = np.interp(rr, [t0, t1], [yy[0], yy[-1]])
    return float(np.trapezoid(yy - base, rr))


def propose(feature: Feature, table, settings: Settings, runs: Optional[dict] = None) -> list[Proposal]:
    """MOVE_START / MOVE_END proposals for the detected members whose apex-relative boundaries
    differ from the reference's, where the estimated area agrees better afterwards."""
    from gcws.core.events import ManualEvent, ManualKind
    ref = reference(feature)
    if ref is None or len(feature.found) < 2 or feature.mismatch or feature.split or runs is None:
        return []
    w = _width(feature)
    lo_ref = ref.peak.rt - ref.peak.start
    hi_ref = ref.peak.end - ref.peak.rt
    out = []
    for m in feature.found:
        if m is ref or m.origin != DETECTED:
            continue
        p = m.peak
        if abs((p.rt - p.start) - lo_ref) <= FRACTION * w and abs((p.end - p.rt) - hi_ref) <= FRACTION * w:
            continue
        ri = table.input(m.run_id)
        others = [q for q in (ri.peaks if ri else []) if q.index != p.index]
        start_shared = any(abs(q.end - p.start) < SHARED for q in others)
        end_shared = any(abs(q.start - p.end) < SHARED for q in others)
        new_start = p.start if start_shared else p.rt - lo_ref
        new_end = p.end if end_shared else p.rt + hi_ref
        if any(q.rt < p.rt and new_start < q.end for q in others) or \
                any(q.rt > p.rt and new_end > q.start for q in others):
            continue                                   # would reach into a neighbour
        move_start = abs(new_start - p.start) > MIN_MOVE * w
        move_end = abs(new_end - p.end) > MIN_MOVE * w
        if not (move_start or move_end):
            continue
        ratio0 = typical_ratio(table, ref.run_id, m.run_id)
        sig = runs.get(m.run_id).signal(table.key) if runs.get(m.run_id) is not None else None
        now, new = signal_area(sig, p.start, p.end), signal_area(sig, new_start, new_end)
        if ratio0 is None or not now or now <= 0 or new is None or new <= 0 or ref.peak.area <= 0:
            continue
        est = p.area * new / now
        err_now = abs(math.log((p.area / ref.peak.area) / ratio0))
        err_new = abs(math.log((est / ref.peak.area) / ratio0))
        if err_new > err_now - MIN_GAIN:
            continue
        parts = []
        text = f"{feature.id} {m.label}: boundaries as in {ref.label} "
        if move_start:
            parts.append(f"start {p.start:.3f} -> {new_start:.3f}")
        if move_end:
            parts.append(f"end {p.end:.3f} -> {new_end:.3f}")
        text += f"({', '.join(parts)}; area {p.area:.4g} -> ~{est:.4g})"
        if move_start:
            out.append(Proposal("boundary", m.run_id, table.key, text, rt=p.rt, auto=False, event=ManualEvent(
                ManualKind.MOVE_START, float(new_start), ref_rt=float(p.rt),
                comment=f"{feature.id}: start as in {ref.label} (apex - {lo_ref * 60:.1f} s)")))
        if move_end:
            out.append(Proposal("boundary", m.run_id, table.key, text, rt=p.rt, auto=False, event=ManualEvent(
                ManualKind.MOVE_END, float(new_end), ref_rt=float(p.rt),
                comment=f"{feature.id}: end as in {ref.label} (apex + {hi_ref * 60:.1f} s)")))
    return out


def propose_table(table, settings: Settings, runs: Optional[dict] = None) -> None:
    for f in table.features:
        f.proposals += propose(f, table, settings, runs)
