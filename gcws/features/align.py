"""Pairing the peaks of the determinations into features.

The score of a candidate pair is mzmine's GC aligner score (``align_gc/GcRowAlignScorer``
with ``align_join/RowVsRowScore`` and ``ScoreAccumulator``; mzmine, MIT, Copyright (c)
2004-2025 The mzmine Development Team)::

    rt_score = max(0, 1 - |RT_ref - map(RT_run)| / rt_tol)
    score    = (w_rt * rt_score + sim) / (w_rt + 1)

with ``sim`` the composite cosine of the co-eluting-ion spectra (``pseudo``) of the peak being
added (library) against the aligned peak (query). A pair is only a candidate inside the RT
tolerance and with ``sim >= min_sim`` (mzmine: the similarity function returns null below
minCos). When a spectrum is too weak to compare (fewer than ``min_ions`` co-eluting ions, or no
MS) the RT score alone decides and the pair is marked so.

Two determinations are paired by an order-preserving maximum-weight matching (dynamic
programming, as in the analyst's GC_Peak_Averaging): the elution order does not change
between two injections, so a pair never crosses another. Three or more are aligned like
mzmine's ``align_common/BaseFeatureListAligner``: the determination with the most unaligned
peaks is the base, all others are scored against its peaks, and the best scores are taken
first, at most one peak per determination and feature.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from math import isfinite
from typing import Optional

import numpy as np

from gcws.features import similarity as SIM
from gcws.features import timemap as TM
from gcws.features.model import DETECTED, GAPFILL, Feature, FeatureTable, Member, RunInput, Settings


@dataclass
class Candidate:
    i: int                  # position in the reference peak list
    j: int                  # position in the other peak list
    score: float
    rt_score: float
    sim: Optional[float]    # None: no MS on one side
    drt: float              # RT_ref - map(RT_run)
    mismatch: bool = False  # below min_sim, kept because the retention times agree exactly


def weights_of(settings: Settings) -> SIM.Weights:
    return SIM.WEIGHTS.get(settings.weights, SIM.NIST_GC)


def comparable(peak, min_ions: int) -> bool:
    return peak.spectrum is not None and len(peak.spectrum[0]) >= min_ions


def pair_similarity(ref_peak, run_peak, weights, min_ions: int = 5) -> Optional[float]:
    """Composite cosine of the co-eluting-ion spectra (library = the peak being added, query = the
    aligned one), 0 if unrelated; None when either spectrum is too weak to compare."""
    if not (comparable(ref_peak, min_ions) and comparable(run_peak, min_ions)):
        return None
    s = SIM.composite_cosine(run_peak.spectrum, ref_peak.spectrum, weights)
    return s.score if s is not None else 0.0


def candidates(ref: RunInput, run: RunInput, tmap: TM.TimeMap, tol: float, settings: Settings,
               sim_cache: Optional[dict] = None) -> list[Candidate]:
    weights = weights_of(settings)
    sim_cache = {} if sim_cache is None else sim_cache
    ref_rt = np.array([p.rt for p in ref.peaks])
    out = []
    for j, q in enumerate(run.peaks):
        t = float(tmap.to_ref(q.rt))
        lo, hi = np.searchsorted(ref_rt, t - tol), np.searchsorted(ref_rt, t + tol, side="right")
        for i in range(lo, hi):
            p = ref.peaks[i]
            drt = p.rt - t
            rt_score = max(0.0, 1.0 - abs(drt) / tol)
            ck = (i, j)
            if ck not in sim_cache:
                sim_cache[ck] = pair_similarity(p, q, weights, settings.min_ions)
            sim = sim_cache[ck]
            mismatch = False
            if sim is None:
                score = rt_score
            else:
                if sim < settings.min_sim:
                    if abs(drt) > settings.exact_rt:
                        continue
                    mismatch = True
                score = (settings.w_rt * rt_score + sim) / (settings.w_rt + 1.0)
            out.append(Candidate(i, j, score, rt_score, sim, drt, mismatch))
    return out


def ordered_matching(n: int, m: int, cands: list[Candidate], ambiguity: float) -> list[tuple[Candidate, bool]]:
    """Maximum total score without crossing pairs; each pair with its ambiguity flag (another
    candidate of either peak within ``ambiguity`` of its score)."""
    if not n or not m or not cands:
        return []
    score = {(c.i, c.j): c for c in cands}
    by_i: dict[int, list[Candidate]] = {}
    by_j: dict[int, list[Candidate]] = {}
    for c in cands:
        by_i.setdefault(c.i, []).append(c)
        by_j.setdefault(c.j, []).append(c)

    # Only the preceding score row is needed; retain moves for backtracking.
    previous = np.zeros(m + 1)
    move = np.zeros((n + 1, m + 1), dtype=np.uint8)
    for i in range(1, n + 1):
        diagonal = np.full(m, -np.inf)
        for c in by_i.get(i - 1, ()):
            if 0 <= c.j < m:
                diagonal[c.j] = previous[c.j] + c.score
        above = previous[1:]
        current = np.empty(m + 1)
        current[0] = 0.0
        np.maximum.accumulate(np.fmax(above, diagonal), out=current[1:])
        left = current[:-1]
        row_moves = np.where(left > above, 2, 1).astype(np.uint8)
        row_moves[diagonal > np.maximum(above, left)] = 3
        move[i, 1:] = row_moves
        previous = current
    pairs = []
    i, j = n, m
    while i and j:
        mv = move[i, j]
        if mv == 3:
            c = score[(i - 1, j - 1)]
            others = [o.score for o in by_i.get(c.i, []) + by_j.get(c.j, []) if o is not c]
            pairs.append((c, bool(others and max(others) >= c.score - ambiguity)))
            i, j = i - 1, j - 1
        elif mv == 1:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def drift_map(ref: RunInput, run: RunInput, settings: Settings) -> tuple[TM.TimeMap, list[str]]:
    """The run's time map onto the reference: global shift, then anchors from a first, loose pairing."""
    est = TM.global_shift([(p.rt, p.area) for p in ref.peaks], [(p.rt, p.area) for p in run.peaks],
                          settings.max_shift)
    notes = [f"{run.label}: drift {est.shift * 60:+.1f} s ({est.support} peak pairs)"]
    shift_map = TM.TimeMap((), (), est.shift)
    loose = candidates(ref, run, shift_map, max(settings.rt_tol, 0.03) * 1.5, settings)
    pairs = ordered_matching(len(ref.peaks), len(run.peaks), loose, settings.ambiguity)
    anchors = [TM.Anchor(run.peaks[c.j].rt, ref.peaks[c.i].rt, ref.peaks[c.i].area)
               for c, amb in pairs if not amb and not c.mismatch and (c.sim is None or c.sim >= settings.anchor_sim)]
    if anchors:
        areas = np.array([a.area for a in anchors])
        cut = float(np.quantile(areas, 0.4)) if len(anchors) >= 10 else 0.0
        anchors = [a for a in anchors if a.area >= cut]
    tmap = TM.build_map(anchors, est.shift)
    if tmap.n_anchors:
        notes.append(f"{run.label}: time map with {tmap.n_anchors} anchors")
    return tmap, notes


def _member(run: RunInput, peak, tmap: TM.TimeMap) -> Member:
    origin = GAPFILL if peak.gapfill else DETECTED
    return Member(run.run_id, run.label, peak, origin, float(tmap.to_ref(peak.rt)))


def _feature(members: list[Member]) -> Feature:
    found = [m for m in members if m.rt_ref is not None]
    rt = float(np.mean([m.rt_ref for m in found])) if found else 0.0
    return Feature(members=members, rt=rt)


def align_pair(ref: RunInput, run: RunInput, settings: Settings) -> tuple[list[Feature], TM.TimeMap, list[str]]:
    tmap, notes = drift_map(ref, run, settings)
    cands = candidates(ref, run, tmap, settings.rt_tol, settings)
    pairs = ordered_matching(len(ref.peaks), len(run.peaks), cands, settings.ambiguity)
    used_i, used_j = set(), set()
    features = []
    for c, amb in pairs:
        used_i.add(c.i)
        used_j.add(c.j)
        f = _feature([_member(ref, ref.peaks[c.i], TM.IDENTITY), _member(run, run.peaks[c.j], tmap)])
        f.sim, f.score, f.ambiguous, f.mismatch = c.sim, c.score, amb, c.mismatch
        features.append(f)
    for i, p in enumerate(ref.peaks):
        if i not in used_i:
            features.append(_feature([_member(ref, p, TM.IDENTITY), Member(run.run_id, run.label)]))
    for j, q in enumerate(run.peaks):
        if j not in used_j:
            features.append(_feature([Member(ref.run_id, ref.label), _member(run, q, tmap)]))
    return features, tmap, notes


def align_many(inputs: list[RunInput], settings: Settings) -> tuple[list[Feature], dict, list[str]]:
    """mzmine BaseFeatureListAligner over three or more determinations (reference axis = the first)."""
    ref = inputs[0]
    maps = {ref.run_id: TM.IDENTITY}
    notes: list[str] = []
    for run in inputs[1:]:
        maps[run.run_id], n = drift_map(ref, run, settings)
        notes += n
    weights = weights_of(settings)
    tol = settings.rt_tol
    t_ref = {r.run_id: [float(maps[r.run_id].to_ref(p.rt)) for p in r.peaks] for r in inputs}
    remaining = {r.run_id: set(range(len(r.peaks))) for r in inputs}
    by_id = {r.run_id: r for r in inputs}
    rows: list[dict] = []                   # run id -> peak position
    scores: list[dict] = []
    while any(remaining.values()):
        base_id = max((r.run_id for r in inputs), key=lambda k: (len(remaining[k]), -inputs.index(by_id[k])))
        base_rows = [{base_id: i} for i in sorted(remaining[base_id], key=lambda k: t_ref[base_id][k])]
        base_times = [t_ref[base_id][row[base_id]] for row in base_rows]
        finite_base = all(isfinite(t) for t in base_times)
        base_scores = [dict() for _ in base_rows]
        remaining[base_id] = set()
        found = []
        for r in inputs:
            if r.run_id == base_id:
                continue
            for j in remaining[r.run_id]:
                tj = t_ref[r.run_id][j]
                if finite_base and isfinite(tj) and isfinite(tol) and tol >= 0:
                    lo = bisect_left(base_times, tj - tol)
                    hi = bisect_right(base_times, tj + tol)
                    # Keep values included by the original subtraction at a rounded boundary.
                    while lo > 0 and abs(base_times[lo - 1] - tj) <= tol:
                        lo -= 1
                    while hi < len(base_times) and abs(base_times[hi] - tj) <= tol:
                        hi += 1
                    positions = range(lo, hi)
                else:
                    positions = range(len(base_rows))
                for k in positions:
                    row = base_rows[k]
                    i = row[base_id]
                    d = t_ref[base_id][i] - tj
                    if abs(d) > tol:
                        continue
                    rt_score = max(0.0, 1.0 - abs(d) / tol)
                    sim = pair_similarity(by_id[base_id].peaks[i], r.peaks[j], weights, settings.min_ions)
                    if sim is None:
                        s = rt_score
                    elif sim < settings.min_sim and abs(d) > settings.exact_rt:
                        continue
                    else:
                        s = (settings.w_rt * rt_score + sim) / (settings.w_rt + 1.0)
                    found.append((s, r.run_id, j, k, sim))
        found.sort(key=lambda x: -x[0])
        for s, rid, j, k, sim in found:
            if j in remaining[rid] and rid not in base_rows[k]:
                base_rows[k][rid] = j
                base_scores[k][rid] = (s, sim)
                remaining[rid].discard(j)
        rows += base_rows
        scores += base_scores
    features = []
    for row, sc in zip(rows, scores):
        members = []
        for r in inputs:
            j = row.get(r.run_id)
            members.append(_member(r, r.peaks[j], maps[r.run_id]) if j is not None else Member(r.run_id, r.label))
        f = _feature(members)
        sims = [v[1] for v in sc.values() if v[1] is not None]
        f.sim = min(sims) if sims else None
        f.mismatch = f.sim is not None and f.sim < settings.min_sim
        f.score = min(v[0] for v in sc.values()) if sc else None
        features.append(f)
    return features, maps, notes


def mark_splits(features: list[Feature]) -> None:
    """One peak in run R where the other determination S has two: feature ``f`` has peaks in R and
    S, feature ``g`` only in S, and ``g``'s apex, taken over to R, lies inside ``f``'s peak in R.
    Both are marked; the gap filler leaves ``g`` alone (its signal is inside ``f``'s peak)."""
    nearby: dict[tuple[str, str], tuple[list[float], list[tuple[Feature, Member, Member]]]] = {}
    grouped: dict[tuple[str, str], list[tuple[float, Feature, Member, Member]]] = {}
    for g in features:
        for own in (m for m in g.members if not m.found):
            for y in g.found:
                if y.rt_ref is not None:
                    grouped.setdefault((own.run_id, y.run_id), []).append((y.rt_ref, g, own, y))
    for key, entries in grouped.items():
        entries.sort(key=lambda item: item[0])
        nearby[key] = ([item[0] for item in entries], [(g, own, y) for _, g, own, y in entries])

    for f in features:
        for m in f.found:
            if m.rt_ref is None:
                continue
            lo = m.rt_ref + m.peak.start - m.peak.rt
            hi = m.rt_ref + m.peak.end - m.peak.rt
            for partner in f.found:
                if partner.run_id == m.run_id:
                    continue
                times, entries = nearby.get((m.run_id, partner.run_id), ([], []))
                for g, own, y in entries[bisect_right(times, lo):bisect_left(times, hi)]:
                    if g is not f and m.peak.start < _in_run(y, m) < m.peak.end:
                        f.split = g.split = True
                        if not own.note:
                            own.note = f"inside the peak at {m.peak.rt:.3f} min of {m.label}"


def _in_run(x: Member, m: Member) -> float:
    """Apex of ``x`` taken over onto the time axis of ``m``'s run (with ``m``'s local offset)."""
    return m.peak.rt + (x.rt_ref - m.rt_ref) if (x.rt_ref is not None and m.rt_ref is not None) else -1.0


def align(inputs: list[RunInput], settings: Settings) -> FeatureTable:
    """Features of the determinations ``inputs`` (the first is the reference)."""
    if not inputs:
        return FeatureTable([], [], "", [], settings=settings)
    if len(inputs) == 1:
        r = inputs[0]
        feats = [_feature([_member(r, p, TM.IDENTITY)]) for p in r.peaks]
        maps, notes = {r.run_id: TM.IDENTITY}, []
    elif len(inputs) == 2:
        feats, tmap, notes = align_pair(inputs[0], inputs[1], settings)
        maps = {inputs[0].run_id: TM.IDENTITY, inputs[1].run_id: tmap}
    else:
        feats, maps, notes = align_many(inputs, settings)
    feats.sort(key=lambda f: f.rt)
    mark_splits(feats)
    return FeatureTable([r.run_id for r in inputs], [r.label for r in inputs], inputs[0].key, feats, maps,
                        settings, notes)
