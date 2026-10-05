"""One identification per feature, from all determinations together.

Two injections of one substance often get two different, very similar library hits (isomers,
homologues): small differences of background or co-elution change the order of the best hits.
Taking each determination's first hit then turns one peak into two "substances". Here the hit
lists of the determinations are read together:

* **A** -- all determinations have the same first hit: taken over.
* **B** -- the first hits differ but one candidate is clearly best over all determinations: the
  consensus spectrum (the co-eluting-ion spectra of the determinations, each scaled to base peak
  999, averaged) is searched again, and the candidate it ranks first is taken when it leads the
  next one by at least ``id_margin`` score points and is among the first three hits of every
  determination. Without libraries the mean score of each candidate over the determinations
  (a missing hit counts 0) decides the same way.
* **C** -- no candidate leads clearly: both candidates are reported ("c1 / c2", both CAS) with
  the status "Manual review"; one click chooses (the analyst's decision).
* **H** -- case C where both candidates are non-aromatic hydrocarbons and the search method reports
  those as "Hydrocarbon": that name, accepted (which isomer does not matter then).
* **D** -- the determinations have different spectra at the same retention time: no consensus;
  "Conflict; manual review".

The acceptance rule stays AutoLib's: a name is "Accepted" only with a score at or above the
quality limit (default 70). An identification the analyst typed, or an ISTD binding, always wins.
Where a determination's first hit differs from the decided one, its hit list is re-ordered
(the decided hit first) and written back as that determination's identification, so the peak
table, the per-determination sheets and the double determination agree.

When the retention index and the library RI are both known, mzmine's ``AnnotationSummary``
RI score (1 - min(|dRI|, tol)/tol) joins the spectral score with the weights MS 6 : RI 2
(mzmine, MIT, Copyright (c) 2004-2025 The mzmine Development Team).
"""
from __future__ import annotations

import copy
import re
from typing import Optional

import numpy as np

from gcws.features.model import DETECTED, GAPFILL, Feature, Identity, Proposal, Settings

ACCEPTED = "Accepted"
REVIEW = "Manual review"
CONFLICT = "Conflict; manual review"
UNKNOWN = "Unknown"
SOURCE = "double determination consensus"


def normalise(name: str) -> str:
    """AutoLib ``_normalise_identity``: lowercase, letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def clean_cas(cas) -> str:
    c = str(cas or "").strip()
    return "" if c in ("", "0", "0-00-0", "None") else c


def _score(hit: dict) -> float:
    try:
        return float(hit.get("score")) if hit.get("score") is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def ri_score(ri: Optional[float], lib_ri: Optional[float], tol: float) -> Optional[float]:
    """mzmine AnnotationSummary: 1 - min(|dRI|, tol)/tol (None when either RI is unknown)."""
    if ri is None or lib_ri is None or tol <= 0:
        return None
    return 1.0 - min(abs(float(ri) - float(lib_ri)), tol) / tol


def combined(ms_score: float, ri: Optional[float]) -> float:
    """MS 6 : RI 2 (mzmine's default weights) on the 0-100 score scale."""
    return ms_score if ri is None else (6 * ms_score + 2 * 100.0 * ri) / 8


# -- consensus spectrum -----------------------------------------------------------------------------

def consensus_spectrum(feature: Feature, settings: Settings) -> Optional[tuple]:
    """The mean of the determinations' co-eluting-ion spectra (base peak 999 each) when every found
    member has a comparable spectrum and they agree (similarity >= ``green_sim``)."""
    specs = [m.peak.spectrum for m in feature.found]
    if len(specs) < 2 or any(s is None or len(s[0]) < settings.min_ions for s in specs):
        return None
    if feature.sim is None or feature.sim < settings.green_sim:
        return None
    masses = np.unique(np.concatenate([np.asarray(s[0], np.int64) for s in specs]))
    acc = np.zeros(masses.size)
    for mz, ab in specs:
        ab = np.asarray(ab, float)
        acc[np.searchsorted(masses, np.asarray(mz, np.int64))] += ab / ab.max() * 999.0
    acc /= len(specs)
    keep = acc > 0
    return masses[keep], acc[keep]


def search_range(points: list, method) -> tuple[int, int]:
    """The m/z range a spectrum is searched with, as :func:`gcws.identify.service.search_spectrum`
    takes it (the spectrum's own masses, unless the method fixes the range)."""
    import gc_search_method as SM
    masses = [m for m, _a in points]
    return SM.mz_range(method, (max(1, int(min(masses))), int(max(masses)) + 1))


def search_consensus(table, method, needed: list[Feature], progress=lambda t: None) -> str:
    """Search the consensus spectrum of ``needed`` features (sets ``consensus``/``consensus_hits``);
    returns a note when the libraries cannot be used ("" otherwise). Spectra of one search range go
    through the Fast search together when the method has it switched on (the hits of the search
    spectrum by spectrum); the ranges are taken in ascending order, so the library norms resume."""
    if not needed:
        return ""
    try:
        import gc_identify as GI
        import gc_search_method as SM
        from gcws.identify.service import is_fast, prepare_local, search_spectrum
        from gcws.libsearch import service as LS
        prepare_local(method, progress)
    except Exception as exc:  # noqa: BLE001 - no library: the hit lists decide
        return f"consensus spectra not searched ({exc})"
    groups: dict = {}
    for f in needed:
        points = [(int(m), float(a)) for m, a in zip(*f.consensus)]
        groups.setdefault(search_range(points, method), []).append((f, points))
    fast = is_fast(method)
    for rng in sorted(groups):
        group = groups[rng]
        results = None
        if fast:
            settings = SM.to_api_settings(method, rng, lite=True)
            try:
                results = LS.analyze_many([(f.id or "consensus", points) for f, points in group], settings)
            except Exception:  # noqa: BLE001 - the batch failed as a whole: spectrum by spectrum
                results = None
        if results is None:
            results = []
            for f, points in group:
                try:
                    results.append({"hits": search_spectrum(points, f.id or "consensus", method)})
                except Exception as exc:  # noqa: BLE001
                    results.append(exc)
        for (f, _points), result in zip(group, results):
            if isinstance(result, BaseException):
                f.reasons.append(f"consensus search failed: {result}")
                continue
            f.consensus_hits = [dict(GI.hit_record(h), peaks=h.get("peaks") or []) for h in result.get("hits") or []]
    return ""


# -- the decision -------------------------------------------------------------------------------------

def _hit_label(h: dict) -> str:
    return str(h.get("name") or "?")


class Groups:
    """Hits of one substance: the same CAS or the same normalised name (union-find), so that a
    library entry without CAS, a duplicate entry or a synonym is not a second candidate."""

    def __init__(self, hit_lists):
        self.parent: dict = {}
        for hits in hit_lists:
            for h in hits or []:
                keys = self._keys(h)
                for k in keys:
                    self.parent.setdefault(k, k)
                for k in keys[1:]:
                    self._union(keys[0], k)

    @staticmethod
    def _keys(h: dict) -> list:
        cas, name = clean_cas(h.get("cas")), normalise(h.get("name", ""))
        return [k for k in (("cas:" + cas) if cas else "", ("name:" + name) if name else "") if k]

    def _find(self, k):
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def _union(self, a, b):
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self.parent[rb] = ra

    def of(self, hit: dict) -> str:
        keys = self._keys(hit)
        for k in keys:
            if k in self.parent:
                return self._find(k)
        return keys[0] if keys else ""


def _scores(hits: list, groups: Groups, k: int) -> dict:
    out: dict = {}
    for h in (hits or [])[:k]:
        g = groups.of(h)
        if g:
            out[g] = max(out.get(g, 0.0), _score(h))
    return out


def _rank(hits: list, groups: Groups, g: str) -> Optional[int]:
    for i, h in enumerate(hits or []):
        if groups.of(h) == g:
            return i
    return None


def decide(feature: Feature, settings: Settings, quality_limit: float = 70.0,
           ri: Optional[float] = None, hydrocarbons: bool = False) -> Identity:
    found = feature.found
    if not found:
        return Identity(case="none")
    manual = [m for m in found if m.peak.manual]
    if manual:
        m = manual[0]
        names = {normalise(x.peak.name) for x in manual}
        status = ACCEPTED if len(names) == 1 else REVIEW
        return Identity(m.peak.name, m.peak.cas, status, "manual", basis="the analyst's identification")
    istd = [m for m in found if m.peak.istd and m.peak.name]
    if istd:
        m = istd[0]
        return Identity(m.peak.name, m.peak.cas, ACCEPTED, "istd", basis="internal standard")
    if feature.mismatch:
        names = list(dict.fromkeys(m.peak.name for m in found if m.peak.name)) or ["unknown"]
        cas = " / ".join(dict.fromkeys(m.peak.cas for m in found if m.peak.cas))
        return Identity(" / ".join(names), cas, CONFLICT, "D",
                        basis=f"different spectra at the same retention time (similarity {feature.sim:.2f})")
    with_hits = [m for m in found if m.peak.hits]
    if not with_hits:
        m = max(found, key=lambda x: x.peak.height)
        status = getattr(m.peak.ident, "status", "") or UNKNOWN
        return Identity(m.peak.name, m.peak.cas, status, "none", basis="no library hits")
    k = settings.id_topk
    groups = Groups([m.peak.hits[:k] for m in with_hits] + [feature.consensus_hits[:k]])
    top_score = {m.run_id: _score(m.peak.hits[0]) for m in with_hits}
    accepted = [m for m in with_hits if top_score[m.run_id] >= quality_limit]
    if not accepted:
        # no determination has an acceptable hit: AutoLib's display (unknown / class) of the best one
        m = max(with_hits, key=lambda x: top_score[x.run_id])
        status = getattr(m.peak.ident, "status", "") or UNKNOWN
        return Identity(m.peak.name, m.peak.cas, status, "U",
                        basis=f"no hit reaches the quality limit {quality_limit:g}")
    tops = {groups.of(m.peak.hits[0]) for m in with_hits}
    member_scores = {m.run_id: _scores(m.peak.hits, groups, k) for m in with_hits}
    cons_scores = _scores(feature.consensus_hits, groups, k) if feature.consensus_hits else None
    first_hit: dict = {}
    for m in with_hits:
        for h in m.peak.hits[:k]:
            first_hit.setdefault(groups.of(h), h)
    for h in feature.consensus_hits[:k]:
        first_hit.setdefault(groups.of(h), h)
    first_hit.pop("", None)

    def ms(g):
        if cons_scores is not None:
            return cons_scores.get(g, 0.0)
        return float(np.mean([s.get(g, 0.0) for s in member_scores.values()]))

    def total(g):
        return combined(ms(g), ri_score(ri, (first_hit.get(g) or {}).get("ri"), settings.ri_tol))

    labels = {m.run_id: m.label for m in with_hits}
    ranked = sorted(first_hit, key=lambda g: -total(g))
    cands = [{"name": _hit_label(first_hit[g]), "cas": clean_cas(first_hit[g].get("cas")),
              "score": round(total(g), 1), "scores": {labels[r]: s.get(g) for r, s in member_scores.items()}}
             for g in ranked[:3]]
    if len(tops) == 1:
        h = accepted[0].peak.hits[0]
        basis = "the same first hit in every determination"
        low = [labels[r] for r, s in top_score.items() if s < quality_limit]
        if low:
            basis += f" (below {quality_limit:g} in {', '.join(low)})"
        return Identity(_hit_label(h), clean_cas(h.get("cas")), ACCEPTED, "A", cands, basis=basis,
                        hits=list(accepted[0].peak.hits))
    # the first hits differ: only candidates accepted somewhere (or the consensus spectrum's first)
    eligible = {groups.of(m.peak.hits[0]) for m in accepted}
    if feature.consensus_hits and _score(feature.consensus_hits[0]) >= quality_limit:
        eligible.add(groups.of(feature.consensus_hits[0]))
    ranked_e = [g for g in ranked if g in eligible]
    best = ranked_e[0]
    others = [g for g in ranked if g != best]
    margin = total(best) - (total(others[0]) if others else 0.0)
    top3 = all((_rank(m.peak.hits, groups, best) is not None and _rank(m.peak.hits, groups, best) < 3)
               for m in with_hits)
    consensus_first = cons_scores is None or groups.of(feature.consensus_hits[0]) == best
    basis = "consensus spectrum" if cons_scores is not None else "hit lists of the determinations"
    if top3 and consensus_first and margin >= settings.id_margin and total(best) >= quality_limit:
        h = first_hit[best]
        return Identity(_hit_label(h), clean_cas(h.get("cas")), ACCEPTED, "B", cands, margin,
                        f"{basis}: {_hit_label(h)} leads by {margin:.1f} points",
                        hits=[copy.deepcopy(first_hit[g]) for g in ranked])
    # C: no clear leader -- the accepted candidates, best first
    pair = [first_hit[g] for g in ranked_e[:2]]
    name = " / ".join(_hit_label(h) for h in pair)
    if hydrocarbons and len(pair) == 2:
        import gc_identify as GI
        if all(GI.is_hydrocarbon(h) for h in pair):
            # either way a hydrocarbon: the search method reports it under the common name
            return Identity(GI.HYDROCARBON_NAME, "", ACCEPTED, "H", cands, margin,
                            f"both candidates are hydrocarbons: {name}")
    cas = " / ".join(c for c in (clean_cas(h.get("cas")) for h in pair) if c)
    why = f"{basis}: " + " vs ".join(f"{_hit_label(first_hit[g])} {total(g):.0f}" for g in ranked_e[:2])
    if len(pair) == 1:
        why += " (accepted in " + ", ".join(m.label for m in accepted) + " only)"
    return Identity(name, cas, REVIEW, "C", cands, margin, why)


def write_back(feature: Feature, identity: Identity, key: str, quality_limit: float = 70.0) -> list[Proposal]:
    """Identifications for the determinations whose first hit is not the decided one (cases A/B
    with one name; never over an analyst's or an ISTD identification)."""
    from gcws.core.ident import Identification
    if identity.case not in ("A", "B") or identity.status != ACCEPTED:
        return []
    groups = Groups([m.peak.hits for m in feature.found] + [identity.hits])
    decided = groups.of({"name": identity.name, "cas": identity.cas})
    out = []
    for m in feature.found:
        p = m.peak
        if p.manual or p.istd or m.origin not in (DETECTED, GAPFILL):
            continue
        hits = list(p.hits or [])
        if hits and groups.of(hits[0]) == decided:
            continue
        pos = _rank(hits, groups, decided)
        if pos is not None:
            chosen = hits[pos]
            new_hits = [chosen] + [h for i, h in enumerate(hits) if i != pos]
        else:
            chosen = next((h for h in identity.hits if groups.of(h) == decided), None)
            if chosen is None:
                continue
            new_hits = [copy.deepcopy(chosen)] + hits
        old = p.ident
        ident = Identification(
            apex_rt=p.rt, name=_hit_label(chosen), cas=clean_cas(chosen.get("cas")),
            score=_score(chosen) or None,
            status=ACCEPTED if _score(chosen) >= quality_limit else REVIEW,
            formula=str(chosen.get("formula") or ""), library=str(chosen.get("library") or ""),
            hits=new_hits, source=SOURCE, method=getattr(old, "method", "") if old else "",
            searched_at=getattr(old, "searched_at", "") if old else "",
            spectrum_mode=getattr(old, "spectrum_mode", "") if old else "",
            istd=getattr(old, "istd", "") if old else "", peak_id=p.fragment)
        was = p.name or "no name"
        text = f"{feature.id} {m.label}: {was} -> {ident.name} ({identity.basis})"
        out.append(Proposal("identity", m.run_id, key, text, ident=ident, rt=p.rt))
    return out


def resolve(table, settings: Settings, quality_limit: float = 70.0, ri_of=None,
            hydrocarbons: bool = False) -> None:
    """Decide the identity of every feature and add the write-back proposals."""
    for f in table.features:
        ri = ri_of(f) if ri_of is not None else None
        f.identity = decide(f, settings, quality_limit, ri, hydrocarbons)
        f.proposals += write_back(f, f.identity, table.key, quality_limit)
