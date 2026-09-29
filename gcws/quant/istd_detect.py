"""Automatic detection of the internal standards defined in the ISTD table.

Each defined ISTD is looked for among the peaks of the quantification signal with three kinds
of evidence:

* **name** - the library hits of the peak (all of them, not only the first) against the ISTD's
  name, its aliases and CAS. A labelled standard (``-d4``, perdeutero, ``13C``) never matches the
  native compound and the other way round: dibutyl phthalate is not DBP-d4.
* **spectrum** - the match factor against a reference spectrum learned from a peak the analyst
  confirmed (*Learn spectrum*); works without any library search.
* **retention time** - the distance to the target RT after a common shift of the whole run,
  estimated from the standards that are certain by name or spectrum (a shortened column moves
  every standard alike).

The area is only a tie-breaker (ISTDs are spiked high). Each peak serves one standard. The
result tells per standard the peak, the evidence in words and a confidence (high / medium /
low); only confident results are bound automatically.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

DEFAULTS = {"max_shift": 0.5, "sigma": 0.05, "min_confidence": "high", "min_score": 0.3}
#: a library name counts as evidence from this similarity on
NAME_MIN = 0.5
CONFIDENCE = ("low", "medium", "high")
_LABEL = re.compile(r"(?:^|[^a-z0-9])d\d{1,2}(?:[^a-z0-9]|$)|deuter|perdeutero|13c|\(13c|d-labeled|d-labelled")


@dataclass
class PeakInfo:
    index: int
    rt: float                                  # on the binding axis (FID time; TIC time in HS mode)
    area: float
    hits: list = field(default_factory=list)   # [(name, cas, score)], best first
    spectrum: Optional[dict] = None            # {m/z: abundance}


@dataclass
class Candidate:
    code: str
    peak_index: int
    rt: float
    target_rt: Optional[float]
    shift: float
    area: float
    name_score: Optional[float]
    spec_score: Optional[float]
    rt_score: Optional[float]
    area_score: float
    score: float
    confidence: str = "low"
    evidence: list = field(default_factory=list)


@dataclass
class DetectResult:
    shift: float
    anchors: int
    best: dict                                 # code -> Candidate | None
    ranked: dict                               # code -> [Candidate] (best first)
    names: dict = field(default_factory=dict)  # code -> name

    def confident(self, minimum: str = "high") -> dict:
        need = CONFIDENCE.index(minimum)
        return {c: k for c, k in self.best.items() if k is not None and CONFIDENCE.index(k.confidence) >= need}


# -- evidence ------------------------------------------------------------------------------------

def labelled(name: str) -> bool:
    return bool(_LABEL.search(str(name or "").casefold()))


def _compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())


def _tokens(name: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", str(name or "").casefold()) if len(t) > 1}


def name_similarity(wanted: str, hit: str) -> float:
    """0..1 how well a library name ``hit`` names the standard ``wanted``."""
    a, b = _compact(wanted), _compact(hit)
    if not a or not b:
        return 0.0
    if a == b:
        s = 1.0
    elif len(a) >= 8 and (a in b or b in a):
        s = 0.9
    else:
        ta, tb = _tokens(wanted), _tokens(hit)
        s = 2 * len(ta & tb) / (len(ta) + len(tb)) if ta and tb else 0.0
        s *= 0.85
    if labelled(wanted) != labelled(hit):
        s *= 0.25                                # native compound vs labelled standard
    return s


def _cas(v) -> str:
    return re.sub(r"[^0-9]", "", str(v or ""))


def name_score(names: list[str], cas: str, hits: list) -> tuple[Optional[float], str]:
    """Best evidence of the peak's hits for a standard called ``names`` (aliases) with ``cas``."""
    if not hits or not (any(names) or cas):
        return None, ""
    best, text = 0.0, ""
    for rank, hit in enumerate(hits[:10]):
        hname, hcas, hscore = hit[0], hit[1] if len(hit) > 1 else "", hit[2] if len(hit) > 2 else None
        sim = 1.0 if cas and _cas(hcas) and _cas(hcas) == _cas(cas) else max(
            (name_similarity(n, hname) for n in names if n), default=0.0)
        q = 0.5 + 0.5 * min(1.0, max(0.0, float(hscore) / 100.0)) if hscore not in (None, "") else 0.85
        s = sim * (1.0, 0.85, 0.75, 0.65)[min(rank, 3)] * q
        if s > best:
            best = s
            text = f"library hit {rank + 1}: {hname}" + (f" ({float(hscore):.0f})" if hscore not in (None, "") else "")
    return best, text


def spectrum_score(ref: Optional[dict], spec: Optional[dict]) -> tuple[Optional[float], str]:
    if not ref or not spec:
        return None, ""
    from gcws.ms.similarity import match_factor
    mf = match_factor(ref, spec)
    return max(0.0, min(1.0, (mf - 600.0) / 350.0)), f"spectrum match {mf:.0f} to the learned reference"


def ref_spectrum(ref: Optional[dict]) -> Optional[dict]:
    if not ref or not ref.get("mz"):
        return None
    return {int(m): float(a) for m, a in zip(ref["mz"], ref["ab"])}


# -- detection (pure) -------------------------------------------------------------------------------

def detect_core(defs: list[dict], refs: dict, peaks: list[PeakInfo], options: Optional[dict] = None) -> DetectResult:
    """Find each standard of ``defs`` (``code``, ``name``, ``target_rt``, ``quantify``) among ``peaks``."""
    o = dict(DEFAULTS, **(options or {}))
    max_shift, sigma = float(o["max_shift"]), max(1e-3, float(o["sigma"]))
    refs = refs or {}
    # evidence without RT
    evid: dict = {}
    for d in defs:
        code = d["code"]
        ref = refs.get(code) or {}
        names = [d.get("name") or ""] + list(ref.get("aliases") or [])
        rspec = ref_spectrum(ref)
        for p in peaks:
            ns, ntext = name_score(names, ref.get("cas") or "", p.hits)
            if ns is not None and ns < NAME_MIN:
                # labelled standards are often missing from the libraries: another name is no evidence
                # against the peak (a spectrum that does not match is)
                ns, ntext = None, ""
            ss, stext = spectrum_score(rspec, p.spectrum)
            evid[(code, p.index)] = (ns, ntext, ss, stext)
    # pass 1: a common RT shift from the standards that are certain by name or spectrum
    shifts = []
    for d in defs:
        t = d.get("target_rt")
        if t is None:
            continue
        near = [p for p in peaks if abs(p.rt - t) <= max_shift]
        sure = [(max(evid[(d["code"], p.index)][0] or 0, evid[(d["code"], p.index)][2] or 0), p) for p in near]
        sure = [(s, p) for s, p in sure if s >= 0.8]
        if sure:
            s, p = max(sure, key=lambda sp: (sp[0], sp[1].area))
            shifts.append(p.rt - t)
    shift = statistics.median(shifts) if shifts else 0.0
    shift = max(-max_shift, min(max_shift, shift))
    # pass 2: candidates with RT evidence
    ranked: dict = {}
    for d in defs:
        code, t = d["code"], d.get("target_rt")
        pool = [p for p in peaks if t is None or abs(p.rt - (t + shift)) <= max_shift]
        top_area = max((p.area for p in pool), default=0.0) or 1.0
        cands = []
        for p in pool:
            ns, ntext, ss, stext = evid[(code, p.index)]
            rs = math.exp(-((p.rt - (t + shift)) / sigma) ** 2) if t is not None else None
            parts = [(0.35, ns), (0.35, ss), (0.3, rs)]
            avail = [(w, v) for w, v in parts if v is not None]
            if not avail:
                continue
            area_score = max(0.0, p.area) / top_area
            score = sum(w * v for w, v in avail) / sum(w for w, _ in avail)
            score = 0.92 * score + 0.08 * area_score
            ev = [x for x in (ntext, stext) if x]
            if t is not None:
                ev.append(f"RT {p.rt:.3f} (target {t:.3f}" + (f" + run shift {shift:+.3f}" if shift else "") + ")")
            if area_score >= 0.999 and len(pool) > 1:
                ev.append("largest peak in the RT window")
            cands.append(Candidate(code, p.index, p.rt, t, shift, p.area, ns, ss, rs, area_score, score,
                                   evidence=ev))
        cands.sort(key=lambda c: -c.score)
        ranked[code] = cands
    # one peak per standard, the best pairs first
    pairs = sorted(((c.score, code, c) for code, cs in ranked.items() for c in cs), key=lambda x: -x[0])
    best: dict = {d["code"]: None for d in defs}
    used = set()
    for score, code, c in pairs:
        if best[code] is not None or c.peak_index in used or score < float(o["min_score"]):
            continue
        best[code] = c
        used.add(c.peak_index)
    for code, c in best.items():
        if c is None:
            continue
        others = [x.score for x in ranked[code] if x.peak_index != c.peak_index]
        margin = c.score - (max(others) if others else 0.0)
        strong = max(c.name_score or 0.0, c.spec_score or 0.0)
        rt_ok = c.rt_score is None or c.rt_score >= 0.3
        if c.score >= 0.75 and strong >= 0.8 and rt_ok and margin >= 0.1:
            c.confidence = "high"
        elif c.score >= 0.5 and (strong >= 0.5 or (c.rt_score or 0) >= 0.8 and margin >= 0.2):
            c.confidence = "medium"
        if margin < 0.1 and others:
            c.evidence.append(f"another peak scores nearly as well ({max(others):.2f})")
    return DetectResult(shift, len(shifts), best, ranked, {d["code"]: d.get("name", "") for d in defs})


# -- the workspace ---------------------------------------------------------------------------------

def _hs(ws) -> bool:
    return (ws.quant or {}).get("mode") == "hs_screening"


def definitions(ws) -> list[dict]:
    """The ISTD table in use (HS: the seven HS standards)."""
    import gc_fid
    q = ws.quant or {}
    if _hs(ws):
        from gcws.quant.hs import default_defs
        return [dict(d) for d in (q.get("hs") or {}).get("istd_defs", default_defs())]
    if q.get("istd_defs"):
        return gc_fid.normalise_istd_defs(q["istd_defs"])
    from gcws.quant.nias_bridge import make_settings
    return gc_fid.default_istd_defs(make_settings(q.get("settings")))


def signal_key(ws) -> str:
    from gcws.quant.service import quant_detector
    return quant_detector(ws.quant)


def axis_shift(ws, st, key) -> float:
    """Peak RT on ``key`` + this = RT on the binding axis (FID time; TIC time for HS)."""
    from gcws.core.keys import is_fid
    return 0.0 if _hs(ws) or is_fid(key) else st.delay_value


def peak_infos(ws, run_id: str, want_spectra: bool = True) -> list[PeakInfo]:
    from gcws.core.keys import TIC, is_fid
    from gcws.ms.spectra import extract
    st = ws.runs[run_id]
    key = signal_key(ws)
    res = ws.result(run_id, key)
    if res is None:
        return []
    shift = axis_shift(ws, st, key)
    idents, _ = st.ident_set(key).bind(res.peaks)
    tic_idents, tic_peaks = {}, []
    if is_fid(key) and st.run.ms is not None:
        tres = ws.result(run_id, TIC)
        if tres is not None:
            tic_peaks = tres.peaks
            tic_idents, _ = st.ident_set(TIC).bind(tic_peaks)
    out = []
    for i, p in enumerate(res.peaks):
        ident = idents.get(i)
        if (ident is None or not (ident.hits or ident.name)) and tic_peaks:
            t = p.apex_rt - st.delay_value
            j = min(range(len(tic_peaks)), key=lambda k: abs(tic_peaks[k].apex_rt - t))
            if abs(tic_peaks[j].apex_rt - t) <= 0.03:
                ident = tic_idents.get(j)
        hits = []
        if ident is not None:
            hits = [(h.get("name") or "", h.get("cas") or "", h.get("score")) for h in ident.hits or []]
            if ident.name and (not hits or ident.manual):
                hits.insert(0, (ident.name, ident.cas or "", 100 if ident.manual else ident.score))
        spec = None
        if want_spectra and st.run.ms is not None:
            s = extract(st.run, p, key, st.delay_value, "average_bg")
            if s is not None and s.mz.size:
                spec = {int(m): float(a) for m, a in zip(s.mz, s.ab)}
        out.append(PeakInfo(i, round(p.apex_rt + shift, 4), float(p.area), hits, spec))
    return out


def detect(ws, run_id: str, options: Optional[dict] = None) -> DetectResult:
    q = ws.quant or {}
    refs = q.get("istd_refs") or {}
    defs = definitions(ws)
    opts = dict(q.get("istd_detect") or {}, **(options or {}))
    return detect_core(defs, refs, peak_infos(ws, run_id, want_spectra=bool(refs)), opts)


def bindings_key(ws) -> tuple[str, ...]:
    return ("hs", "istd_bindings") if _hs(ws) else ("istd_bindings",)


def with_bindings(ws, found: dict, update_targets: bool = False, quant: Optional[dict] = None) -> dict:
    """``ws.quant`` (or ``quant``) with ``found`` = ``{run id: {code: rt on the binding axis}}`` bound.

    ``update_targets`` also moves each definition's target RT (the median over the runs)."""
    import copy
    q = copy.deepcopy(quant if quant is not None else ws.quant)
    base = q.setdefault("hs", {}) if _hs(ws) else q
    b = base.setdefault("istd_bindings", {})
    for rid, codes in found.items():
        b.setdefault(rid, {}).update({c: round(float(rt), 4) for c, rt in codes.items()})
    if update_targets:
        per_code: dict = {}
        for codes in found.values():
            for c, rt in codes.items():
                per_code.setdefault(c, []).append(float(rt))
        defs = definitions(ws) if "istd_defs" not in base else [dict(d) for d in base["istd_defs"]]
        for d in defs:
            if d["code"] in per_code:
                d["target_rt"] = round(statistics.median(per_code[d["code"]]), 3)
        base["istd_defs"] = defs
    return q


def learn_reference(ws, run_id: str, code: str) -> Optional[dict]:
    """The spectrum of the peak bound to (or found for) ``code`` in ``run_id`` as a reference."""
    from gcws.core.audit import current_user
    from gcws.ms.spectra import extract
    st = ws.runs.get(run_id)
    if st is None or st.run.ms is None:
        return None
    key = signal_key(ws)
    res = ws.result(run_id, key)
    if res is None or not res.peaks:
        return None
    rt = bound_rt(ws, run_id, code)
    if rt is None:
        return None
    t = rt - axis_shift(ws, st, key)
    j = min(range(len(res.peaks)), key=lambda k: abs(res.peaks[k].apex_rt - t))
    if abs(res.peaks[j].apex_rt - t) > 0.03:
        return None
    s = extract(st.run, res.peaks[j], key, st.delay_value, "average_bg")
    if s is None or not s.mz.size:
        return None
    keep = s.ab >= s.ab.max() * 0.005
    old = ((ws.quant or {}).get("istd_refs") or {}).get(code) or {}
    return {"mz": [int(m) for m in s.mz[keep]], "ab": [round(float(a), 1) for a in s.ab[keep]],
            "aliases": list(old.get("aliases") or []), "cas": old.get("cas", ""), "learned_from": st.name,
            "learned_rt": round(rt, 4), "learned_at": datetime.now().isoformat(timespec="seconds"),
            "by": current_user()}


def bound_rt(ws, run_id: str, code: str) -> Optional[float]:
    """RT (binding axis) of the peak the quantification uses for ``code`` in ``run_id``."""
    q = ws.quant or {}
    if _hs(ws):
        st = ws.runs.get(run_id)
        from gcws.quant.hs import matched_standards
        if st is None or ws.result(run_id, "TIC") is None:
            return None
        try:
            m = next((s for s in matched_standards(st, q.get("hs") or {}) if s["code"] == code), None)
        except (ValueError, KeyError):
            m = None
        return m["rt"] if m and m.get("rt") is not None else None
    sample = ws.nias_sample(run_id)
    if sample is None:
        return None
    std = next((s for s in sample.standards if s.get("code") == code and s.get("fid_rt")), None)
    return float(std["fid_rt"]) if std else None
