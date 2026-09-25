"""Rule-based interpretation of an EI mass spectrum.

Gives an analyst *clues* -- not identifications -- about an unknown:

1. significant ions and the molecular-ion candidate (isotope peaks rejected,
   illogical losses 3-14 / 21-25 u, nitrogen rule),
2. the isotope pattern (Cl, Br, S, Si) and the carbon number from M+1,
3. ion series (alkyl, alkenyl, aromatic, siloxane, ...) and neutral losses,
4. substance classes from the rule base (:mod:`gcws.ms.knowledge`) with the
   evidence and the contradictions behind every score,
5. substance fingerprints of NIAS-relevant compounds,
6. formula suggestions for M,
7. checks against the retention index and the library hit.

Ions below the start of the scan range cannot be seen and never count as
absent; isotope peaks that would lie under the acquisition threshold likewise.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from gcws.ms import isotopes as iso
from gcws.ms.knowledge import AIR_IONS, BLEED_IONS, ILLOGICAL_LOSSES, LOSSES, SERIES, load


@dataclass
class Context:
    rt_ms: float | None = None
    ri: float | None = None
    mass_range: tuple[int, int] | None = None     # scan range; ions below its start are unobservable
    min_abundance: float = 0.0                     # acquisition threshold (absolute abundance)
    library_hit: dict | None = None                # {"name", "cas", "formula", "mw", "score"}
    purity: float | None = None


@dataclass
class MCandidate:
    mz: int
    score: float
    level: str                   # probable | possible | uncertain
    evidence: list[str] = field(default_factory=list)
    hetero: dict = field(default_factory=dict)     # atoms from the isotope cluster, e.g. {"Cl": 2}
    protonated: bool = False                       # M+1 inflated by [M+H]+ / co-elution


@dataclass
class IsotopeHint:
    label: str                   # "Cl2", "Br", "S", "Si2"
    counts: dict
    at_mz: int
    certainty: str               # clear | likely
    text: str


@dataclass
class SeriesHit:
    id: str
    label: str
    members: list[int]
    fraction: float


@dataclass
class LossHit:
    from_mz: int
    to_mz: int
    loss: int
    rel: float
    meaning: str
    illogical: bool = False


@dataclass
class ClassHint:
    id: str
    label: str
    group: str
    score: float
    level: str                   # high | medium | low
    evidence: list[str] = field(default_factory=list)
    contra: list[str] = field(default_factory=list)
    examples: str = ""
    note: str = ""
    diagnostic: list[int] = field(default_factory=list)
    fp_support: bool = False                       # a substance fingerprint of this class matches


@dataclass
class CompoundHint:
    name: str
    cas: str
    mw: int | None
    score: float
    level: str                   # strong | fair | weak
    cls: str
    key_ions: list[int] = field(default_factory=list)


@dataclass
class Interpretation:
    base_peak: int | None = None
    n_ions: int = 0
    low_mz: int = 0
    m: MCandidate | None = None
    m_alternatives: list[MCandidate] = field(default_factory=list)
    m_note: str = ""
    carbon: tuple[int, int, int] | None = None
    isotopes: list[IsotopeHint] = field(default_factory=list)
    series: list[SeriesHit] = field(default_factory=list)
    losses: list[LossHit] = field(default_factory=list)
    classes: list[ClassHint] = field(default_factory=list)
    compounds: list[CompoundHint] = field(default_factory=list)
    formulas: list = field(default_factory=list)
    checks: list[tuple[str, str]] = field(default_factory=list)     # (level ok|warn|bad|info, text)
    warnings: list[str] = field(default_factory=list)
    summary: str = ""

    def marks(self) -> dict[int, tuple[str, str]]:
        """{m/z: (label, level)} for annotating the stick plot."""
        out: dict[int, tuple[str, str]] = {}
        if self.classes:
            for m in self.classes[0].diagnostic[:6]:
                out[m] = (str(m), "ok")
        if self.m is not None:
            out[self.m.mz] = (f"M⁺· {self.m.mz}" + ("?" if self.m.level != "probable" else ""), "accent")
        return out


# -- helpers ------------------------------------------------------------------------

class _Spec:
    def __init__(self, mz, ab, ctx: Context):
        mz = np.rint(np.asarray(mz, float)).astype(int)
        ab = np.asarray(ab, float)
        keep = ab > 0
        self.abs: dict[int, float] = {}
        for m, a in zip(mz[keep].tolist(), ab[keep].tolist()):
            self.abs[m] = self.abs.get(m, 0.0) + a
        self.base_abs = max(self.abs.values()) if self.abs else 0.0
        self.rel = {m: 100.0 * a / self.base_abs for m, a in self.abs.items()} if self.base_abs else {}
        self.base = max(self.abs, key=self.abs.get) if self.abs else None
        lo = ctx.mass_range[0] if ctx.mass_range else (min(self.abs) if self.abs else 0)
        self.low = int(lo)
        self.threshold = float(ctx.min_abundance or 0.0)
        self.rel_thr = 100.0 * self.threshold / self.base_abs if self.base_abs else 0.0
        self.total = sum(a for m, a in self.abs.items() if m >= self.low)
        self.top = sorted(self.abs, key=self.abs.get, reverse=True)

    def r(self, m: int) -> float:
        return self.rel.get(int(m), 0.0)

    def observable(self, m: int) -> bool:
        return m >= self.low

    def is_isotope(self, m: int) -> bool:
        """Plausibly the 13C (+1) or +2 isotope peak of a stronger ion just below."""
        a = self.abs.get(m, 0.0)
        return (a > 0 and (a <= 0.6 * self.abs.get(m - 1, 0.0) or a <= 0.12 * self.abs.get(m - 2, 0.0)))

    def significant(self, min_rel: float = 0.5) -> list[int]:
        lim = max(min_rel, 2 * self.rel_thr)
        return sorted(m for m, v in self.rel.items() if v >= lim)


def _level(score: float, hi: float, mid: float, names=("high", "medium", "low")) -> str:
    return names[0] if score >= hi else (names[1] if score >= mid else names[2])


# -- molecular ion ---------------------------------------------------------------------

def _cluster_fit(sp: _Spec, m: int, top: int):
    n = max(2, min(8, top - m + 2))
    obs = iso.observed_cluster(sp.abs, m, n)
    est = iso.carbon_estimate(obs[0], obs[1] if n > 1 else 0.0, sp.threshold, None, m)
    nc = est[0] if est else None
    fits = iso.fit_cluster(obs, sp.threshold, nc, m=m)
    if not fits:
        return None, None, obs, est
    best, chi = fits[0]
    null = next((c for combo, c in fits if not combo), chi)
    return best, (chi / (n - 1), null / (n - 1)), obs, est


#: cyclic-siloxane column-bleed ions that can top an otherwise unrelated spectrum
_BLEED_TOPS = {207, 281, 355, 429}


def _molecular_ion(sp: _Spec) -> tuple[MCandidate | None, list[MCandidate]]:
    """The highest plausible cluster is the M+ candidate; lower clusters are alternatives."""
    sig = [m for m in sp.significant(0.5) if sp.r(m) >= (1.0 if m < 150 else 0.5)]
    if not sig:
        return None, []
    heads = sorted(sig, reverse=True)
    tried: set[int] = set()
    cands: list[MCandidate] = []
    for h in heads:
        if h in tried:
            continue
        region = [m for m in range(h, h - 9, -1) if sp.r(m) >= 0.3]
        tried.update(range(h - 8, h + 1))
        best_m, best = None, None
        for m in sorted(region):             # lowest M whose pattern explains the ions above it
            combo, chis, obs, est = _cluster_fit(sp, m, h)
            if combo is None:
                continue
            chi, null = chis
            above = [x for x in region if x > m]
            explains = chi <= 1.5 and (not above or max(sp.abs[x] for x in above) <= 3.2 * sp.abs[m])
            if explains:
                best_m, best = m, (combo, chi, null, est)
                break
        if best_m is None:
            best_m, best = h, ({}, 9.0, 9.0, None)
        # [M+H]+ from self-CI (or a co-eluting ion) can make M+1 larger than M; the "explaining"
        # peak then has a carbon number far too small for its mass. Take the lowest strong ion.
        est = best[3]
        protonated = False
        if est and est[0] < best_m / 45.0:
            lower = [x for x in region if x < best_m and sp.abs[x] >= 0.4 * max(sp.abs[y] for y in region)]
            if lower:
                best_m, best, protonated = min(lower), ({}, 1.0, 1.0, None), True
        cand = _score_m(sp, best_m, best)
        cand.protonated = protonated
        if protonated:
            cand.evidence.append(f"m/z {best_m + 1} exceeds its isotope expectation: [M+H]⁺ (self-CI) or "
                                 "co-elution")
        bleed = best_m in _BLEED_TOPS and sp.r(best_m) < 15 and sp.base not in _BLEED_TOPS | {73}
        if bleed:
            cand.score -= 0.35
            cand.evidence.append(f"m/z {best_m} is a typical column-bleed ion: may not belong to the analyte")
        cand.level = _level(cand.score, 0.7, 0.45, ("probable", "possible", "uncertain"))
        cands.append(cand)
        if len(cands) >= 3:
            break
    if not cands:
        return None, []
    primary = next((c for c in cands if c.level != "uncertain"), cands[0])
    alts = [c for c in cands if c is not primary and c.mz < primary.mz][:2] if sp.r(primary.mz) < 5 else []
    return primary, alts


def _score_m(sp: _Spec, m: int, best) -> MCandidate:
    combo, chi, null, est = best
    ev = []
    score = 0.5
    r = sp.r(m)
    if r >= 10:
        score += 0.2
        ev.append(f"m/z {m} at {r:.0f} % of the base peak")
    elif r >= 3:
        score += 0.1
        ev.append(f"m/z {m} at {r:.1f} %")
    elif r < 1:
        score -= 0.15
        ev.append(f"m/z {m} is weak ({r:.1f} %)")
    else:
        ev.append(f"m/z {m} at {r:.1f} %")
    if chi <= 1.0 and sp.abs.get(m + 1, 0) > 0:
        score += 0.1
        ev.append("the ions above it fit its isotope pattern" + (f" ({iso.combo_label(combo)})" if combo else ""))
    illogical = [(d, sp.r(m - d)) for d in sorted(ILLOGICAL_LOSSES)
                 if sp.observable(m - d) and sp.r(m - d) >= max(5.0, 0.8 * r) and not sp.is_isotope(m - d)]
    if illogical:
        score -= 0.3
        ev.append("strong ions at illogical losses " + ", ".join(f"M−{d} (m/z {m - d})" for d, _ in illogical[:3])
                  + ": M may be higher (not seen) or the spectrum is mixed")
    logical = [d for d in (15, 17, 18, 28, 29, 31, 43, 45, 57) if sp.observable(m - d) and sp.r(m - d) >= 3]
    if logical:
        score += 0.05 * min(2, len(logical))
    ev.append(f"nominal mass {'odd → odd number of N' if m % 2 else 'even → no or an even number of N'}")
    cand = MCandidate(m, max(0.0, min(1.0, score)), "", ev, dict(combo))
    return cand


# -- isotopes ----------------------------------------------------------------------------

def _isotope_hints(sp: _Spec, m: MCandidate | None) -> list[IsotopeHint]:
    anchors: list[int] = []
    if m is not None:
        anchors.append(m.mz)
    for a in sp.top[:6]:
        if a >= 50 and a not in anchors:
            anchors.append(a)
    found: dict[str, IsotopeHint] = {}
    for a in anchors:
        # the anchor must be the lightest peak of its cluster
        if sp.abs.get(a - 2, 0) > 0.25 * sp.abs[a] or sp.abs.get(a - 1, 0) > 1.5 * sp.abs[a]:
            continue
        obs = iso.observed_cluster(sp.abs, a, 8)
        if obs[0] < 20 * sp.threshold:              # too weak to judge ratios
            continue
        est = iso.carbon_estimate(obs[0], obs[1], sp.threshold, None, a)
        fits = iso.fit_cluster(obs, sp.threshold, est[0] if est else None, m=a)
        if not fits:
            continue
        best, chi = fits[0]
        null = next((c for combo, c in fits if not combo), chi)
        is_m = m is not None and a == m.mz
        # a heteroatom only when it fits (<= 1.5 per offset) and clearly beats "none";
        # fragment clusters often have an unrelated ion at +2, so they need a strong, clear case
        if not best or chi > 1.5 * 7 or null - chi < (4.0 if is_m else 12.0):
            continue
        if not is_m and sp.r(a) < 20:
            continue
        for el in ("Cl", "Br", "S", "Si"):
            n = best.get(el, 0)
            if not n:
                continue
            # S and Si rest on small M+2 signals: only in the molecular ion, with enough abundance
            if el in ("S", "Si") and obs[2] < 5 * sp.threshold:
                continue
            if el == "S" and not is_m:
                continue
            certainty = "clear" if null - chi > 12 else "likely"
            prev = found.get(el)
            if prev is None or n > prev.counts.get(el, 0) or (a == (m.mz if m else None)):
                ratio = ", ".join(f"M+{k} {100 * obs[k] / obs[0]:.0f} %" for k in (2, 4, 6) if obs[k] > 0)
                found[el] = IsotopeHint(f"{el}{n if n > 1 else ''}", {el: n}, a, certainty,
                                        f"cluster at m/z {a}: {ratio} fits {el}{n if n > 1 else ''}"
                                        + (" (molecular ion)" if m is not None and a == m.mz else " (fragment: at least)"))
    return list(found.values())


# -- series and losses ---------------------------------------------------------------------

def _series(sp: _Spec) -> dict[str, SeriesHit]:
    out = {}
    total = sp.total or 1.0
    for sid, s in SERIES.items():
        members = [m for m in s["ions"] if sp.observable(m) and sp.r(m) >= 1.0]
        frac = sum(sp.abs[m] for m in members) / total
        out[sid] = SeriesHit(sid, s["label"], members, frac)
    return out


def _losses(sp: _Spec, m: MCandidate | None) -> list[LossHit]:
    if m is None:
        return []
    out = []
    for d in range(1, min(m.mz - sp.low, 150) + 1):
        t = m.mz - d
        v = sp.r(t)
        if v < 5 or t < sp.low or sp.is_isotope(t):
            continue
        if d in LOSSES:
            out.append(LossHit(m.mz, t, d, v, LOSSES[d]))
        elif d in ILLOGICAL_LOSSES:
            out.append(LossHit(m.mz, t, d, v, "illogical loss", True))
    out.sort(key=lambda x: -x.rel)
    return out[:8]


# -- class rules -------------------------------------------------------------------------------

class _Features:
    def __init__(self, sp: _Spec, m: MCandidate | None, series, isos):
        self.sp, self.m, self.series = sp, m, series
        self.iso = {}
        for h in isos:
            for el, n in h.counts.items():
                self.iso[el] = max(self.iso.get(el, 0), n)


def _eval(c: dict, f: _Features) -> tuple[str, str]:
    """('ok' | 'fail' | 'na', text)."""
    sp = f.sp
    if "ion" in c:
        m = int(c["ion"])
        if not sp.observable(m):
            return "na", ""
        v = sp.r(m)
        ok = c.get("min", 0) <= v <= c.get("max", 1000)
        if "max" in c and "min" not in c:
            return ("ok" if ok else "fail"), f"m/z {m} weak ({v:.0f} %)" if ok else f"m/z {m} at {v:.0f} %"
        return ("ok" if ok else "fail"), (f"m/z {m} base peak" if m == sp.base else f"m/z {m} at {v:.0f} %")
    if "any" in c:
        ms = [int(x) for x in c["any"] if sp.observable(int(x))]
        if not ms:
            return "na", ""
        best = max(ms, key=sp.r)
        ok = sp.r(best) >= c.get("min", 1)
        return ("ok" if ok else "fail"), f"m/z {best} at {sp.r(best):.0f} %"
    if "base" in c:
        ok = sp.base in [int(x) for x in c["base"]]
        return ("ok" if ok else "fail"), f"base peak m/z {sp.base}"
    if "top" in c:
        k = int(c.get("k", 3))
        hit = [m for m in sp.top[:k] if m in c["top"]]
        return ("ok" if hit else "fail"), f"m/z {hit[0] if hit else c['top'][0]} among the {k} largest ions"
    if "ratio" in c:
        a, b = (int(x) for x in c["ratio"])
        if not (sp.observable(a) and sp.observable(b)):
            return "na", ""
        if sp.r(b) < c.get("min_b", 0):       # denominator too weak for the ratio to mean anything
            return "na", ""
        if sp.r(b) <= 0:
            return "fail", ""
        q = sp.r(a) / sp.r(b)
        ok = c.get("lo", 0) <= q <= c.get("hi", 1e9)
        return ("ok" if ok else "fail"), f"I({a})/I({b}) = {q:.2f}"
    if "series" in c:
        s = f.series[c["series"]]
        obs_members = [m for m in SERIES[c["series"]]["ions"] if sp.observable(m)]
        need = min(int(c.get("members", 3)), max(1, len(obs_members) - 1))
        ok = len(s.members) >= need and s.fraction >= c.get("frac", 0)
        return ("ok" if ok else "fail"), (f"{s.label}: {', '.join(map(str, s.members[:7]))} "
                                          f"({100 * s.fraction:.0f} % of the ion current)")
    if "m" in c:
        if f.m is None:
            return "na", ""
        lo, hi = c["m"]
        return ("ok" if lo <= f.m.mz <= hi else "fail"), f"M⁺· candidate {f.m.mz}"
    if "parity" in c:
        if f.m is None or f.m.level == "uncertain":
            return "na", ""
        odd = f.m.mz % 2 == 1
        ok = odd if c["parity"] == "odd" else not odd
        return ("ok" if ok else "fail"), f"M {f.m.mz} {'odd (N)' if odd else 'even'}"
    if "mrel" in c:
        if f.m is None:
            return "na", ""
        v = sp.r(f.m.mz)
        return ("ok" if v >= c["mrel"] else "fail"), f"M⁺· {f.m.mz} at {v:.0f} %"
    if "loss" in c:
        if f.m is None:
            return "na", ""
        d = int(c["loss"])
        t = f.m.mz - d
        if not sp.observable(t):
            return "na", ""
        v = sp.r(t)
        ok = c.get("min", 1) <= v <= c.get("max", 1000)
        return ("ok" if ok else "fail"), f"M−{d} (m/z {t}) at {v:.0f} %"
    if "iso" in c:
        n = f.iso.get(c["iso"], 0)
        ok = n >= int(c.get("n", 1))
        return ("ok" if ok else "fail"), f"isotope pattern: {c['iso']}{n if n > 1 else ''}"
    return "na", ""


def _diagnostic_ions(rule: dict, sp: _Spec) -> list[int]:
    out = []
    for part in ("require", "support"):
        for c in rule.get(part, []):
            for m in ([c["ion"]] if "ion" in c else c.get("any", []) if "any" in c else c.get("base", []) if "base" in c else []):
                m = int(m)
                if sp.r(m) >= 3 and m not in out and ("max" not in c or "min" in c):
                    out.append(m)
    return sorted(out, key=sp.r, reverse=True)


def _classes(f: _Features, rules: list[dict]) -> list[ClassHint]:
    out = []
    for rule in rules:
        req = [(c, *_eval(c, f)) for c in rule.get("require", [])]
        evaluated = [x for x in req if x[1] != "na"]
        if not evaluated or any(x[1] == "fail" for x in evaluated):
            continue
        evidence = [t for _c, s, t in evaluated if t]
        num = den = 0.0
        for c in rule.get("support", []):
            s, t = _eval(c, f)
            if s == "na":
                continue
            w = float(c.get("w", 1))
            den += w
            if s == "ok":
                num += w
                if t and t not in evidence:
                    evidence.append(t)
        score = 0.55 + 0.45 * (num / den if den else 0.4)
        contra = []
        for c in rule.get("contra", []):
            s, t = _eval(c, f)
            if s == "ok":
                score -= 0.22 * float(c.get("w", 1))
                contra.append(t)
        if len(evaluated) < len(req):          # some requirements not observable
            score -= 0.05
        score = max(0.0, min(1.0, score))
        if score < 0.4:
            continue
        out.append(ClassHint(rule["id"], rule["label"], rule.get("group", ""), score, _level(score, 0.8, 0.6),
                             evidence, contra, rule.get("examples", ""), rule.get("note", ""),
                             _diagnostic_ions(rule, f.sp)))
    out.sort(key=lambda h: -h.score)
    return out


# -- fingerprints ------------------------------------------------------------------------------

def _fingerprints(sp: _Spec, fps: list[dict]) -> list[CompoundHint]:
    out = []
    obs_top = [m for m in sp.top[:6] if sp.observable(m)]
    heavy = [m for m in sp.significant(2.0)]
    top_obs = max(heavy) if heavy else None
    for fp in fps:
        ions = [(int(m), float(r)) for m, r in fp["ions"] if sp.observable(int(m))]
        if len(ions) < 2:
            continue
        ref = dict(ions)
        total = sum(ref.values())
        pres = sum(r for m, r in ions if sp.r(m) >= max(1.0, 0.25 * r)) / total
        masses = sorted(set(ref) | set(obs_top))
        a = np.sqrt([ref.get(m, 0.0) for m in masses])
        b = np.sqrt([sp.r(m) for m in masses])
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        cos = float(a @ b / (na * nb)) if na > 0 and nb > 0 else 0.0
        fp_base = max(ref, key=ref.get)
        base_ok = 1.0 if sp.base == fp_base else (0.5 if fp_base in sp.top[:3] else 0.0)
        score = 0.5 * cos + 0.3 * pres + 0.2 * base_ok
        mw = fp.get("mw")
        if mw and top_obs is not None and top_obs > mw + 3:
            score *= 0.45                         # the spectrum has ions above the molecular mass
        if mw and sp.observable(mw) and ref.get(mw, 0) >= 10 and sp.r(mw) < 1.0:
            score *= 0.85
        if score < 0.55:
            continue
        key = [m for m, r in sorted(ions, key=lambda x: -x[1])[:5]]
        out.append(CompoundHint(fp["name"], fp.get("cas", ""), mw, score, _level(score, 0.82, 0.68,
                                                                                ("strong", "fair", "weak")),
                                fp.get("cls", ""), key))
    out.sort(key=lambda h: -h.score)
    return out[:5]


# -- checks ---------------------------------------------------------------------------------------

def _checks(res: Interpretation, sp: _Spec, ctx: Context) -> list[tuple[str, str]]:
    checks: list[tuple[str, str]] = []
    top_cls = res.classes[0].id if res.classes else ""
    if ctx.ri and ctx.ri > 0:
        n = int(round(ctx.ri / 100.0))
        if abs(ctx.ri - 100 * n) <= 12 and top_cls in ("alkane", ""):
            text = f"RI {ctx.ri:.0f} ≈ n-C{n} (C{n}H{2 * n + 2}, M {14 * n + 2})"
            if res.m is not None and res.m.mz == 14 * n + 2:
                checks.append(("ok", text + ": M⁺· matches the n-alkane"))
            elif top_cls == "alkane":
                checks.append(("info", text + ": an n-alkane elutes exactly here"))
        elif top_cls == "alkane":
            checks.append(("info", f"RI {ctx.ri:.0f} is between the n-alkanes: branched alkane or other"))
    hit = ctx.library_hit or {}
    if hit.get("name"):
        name = hit["name"]
        counts = iso.parse_formula(hit.get("formula", ""))
        hal = {el: counts.get(el, 0) for el in ("Cl", "Br")}
        seen = {h.label[:2].rstrip("0123456789"): h for h in res.isotopes}
        for el in ("Cl", "Br"):
            if hal[el] and el not in seen and res.m is not None and sp.r(res.m.mz) >= 5:
                checks.append(("warn", f"library hit {name} contains {el}{hal[el]} but no {el} isotope pattern is seen"))
            if not hal[el] and el in seen and counts:
                checks.append(("warn", f"{el} isotope pattern ({seen[el].label}) but the hit {name} has no {el}"))
        mw = hit.get("mw") or (iso.nominal_mass({k: v for k, v in counts.items() if k in iso.MASS}) if counts else None)
        heavy = [m for m in sp.significant(3.0)]
        if mw and heavy and max(heavy) > mw + 3:
            checks.append(("warn", f"ions above the molecular mass of the hit (MW {mw}; m/z {max(heavy)} at "
                                   f"{sp.r(max(heavy)):.0f} %): wrong hit or mixed spectrum"))
        elif mw and res.m is not None and res.m.level == "probable" and abs(mw - res.m.mz) > 0:
            checks.append(("info", f"M⁺· candidate {res.m.mz} differs from the MW {mw} of the hit "
                                   "(M⁺· may be absent, or the hit is a homologue)"))
        elif mw and res.m is not None and mw == res.m.mz:
            checks.append(("ok", f"MW {mw} of the hit matches the M⁺· candidate"))
        if res.classes:
            tokens = {"phthalate": "phthal", "adipate": "adip", "citrate": "citr", "fame": "methyl",
                      "fatty_amide": "amide", "siloxane_cyclic": "silox", "siloxane_linear": "silox",
                      "alkane": "ane", "hindered_phenol": "phenol", "benzoate": "benzo"}
            tok = tokens.get(res.classes[0].id)
            if tok and tok in name.lower():
                checks.append(("ok", f"hit {name} agrees with the interpreted class ({res.classes[0].label})"))
    if ctx.purity is not None and ctx.purity < 0.7:
        checks.append(("warn", f"spectral purity {ctx.purity:.2f}: co-elution likely, consider deconvolution"))
    return checks


def _warnings(sp: _Spec, res: Interpretation) -> list[str]:
    out = []
    n_sig = len(sp.significant(1.0))
    if n_sig < 6 or sp.base_abs < 30 * max(sp.threshold, 1.0):
        out.append("weak or sparse spectrum: the interpretation is uncertain")
    air = [m for m in AIR_IONS if sp.observable(m) and sp.r(m) >= 30]
    if air:
        out.append("air/water ions (" + ", ".join(map(str, sorted(air))) + ") are strong: check the background")
    bleed = [m for m in (207, 281, 355) if m in sp.top[:4]]
    if bleed and not (res.classes and res.classes[0].id.startswith("siloxane")):
        out.append("column-bleed ions (" + ", ".join(map(str, bleed)) + ") among the largest: subtract background")
    if any(l.illogical for l in res.losses):
        out.append("illogical losses from the M⁺· candidate: mixed spectrum or M⁺· not observed")
    return out


def _summary(res: Interpretation) -> str:
    parts = []
    if res.classes:
        c = res.classes[0]
        parts.append(f"{c.label} ({c.level} confidence)")
    if res.compounds and res.compounds[0].level in ("strong", "fair"):
        parts.append(f"pattern of {res.compounds[0].name}")
    if res.m is not None:
        parts.append(f"M⁺· {res.m.mz} ({res.m.level})")
    for h in res.isotopes:
        parts.append(h.label)
    if res.carbon:
        parts.append(f"≈C{res.carbon[0]}")
    return "; ".join(parts) if parts else "no characteristic pattern found"


# -- entry point --------------------------------------------------------------------------------

def interpret(mz, ab, ctx: Context | None = None, rules=None, fingerprints=None) -> Interpretation:
    ctx = ctx or Context()
    if rules is None or fingerprints is None:
        r, f = load()
        rules = rules if rules is not None else r
        fingerprints = fingerprints if fingerprints is not None else f
    sp = _Spec(mz, ab, ctx)
    res = Interpretation(base_peak=sp.base, n_ions=len(sp.significant(0.5)), low_mz=sp.low)
    if not sp.abs:
        res.summary = "empty spectrum"
        return res
    res.m, res.m_alternatives = _molecular_ion(sp)
    res.isotopes = _isotope_hints(sp, res.m)
    if res.m is not None:
        # heteroatoms of M only when the isotope analysis confirmed them
        res.m.hetero = {el: n for h in res.isotopes if h.at_mz == res.m.mz for el, n in h.counts.items()}
        obs = iso.observed_cluster(sp.abs, res.m.mz, 3)
        if not res.m.protonated:
            res.carbon = iso.carbon_estimate(obs[0], obs[1], sp.threshold, res.m.hetero, res.m.mz)
    series = _series(sp)
    res.series = sorted([s for s in series.values() if len(s.members) >= 3 and s.fraction >= 0.15],
                        key=lambda s: -s.fraction)
    res.losses = _losses(sp, res.m)
    feats = _Features(sp, res.m, series, res.isotopes)
    res.classes = _classes(feats, rules)
    # classes whose M+. is usually absent: the highest ion may just be the end of a fragment series
    weak = {r["id"] for r in rules if r.get("m_weak")}
    if res.m is not None and res.classes and res.classes[0].id in weak:
        members = {mz for s in series.values() if s.fraction >= 0.1 for mz in s.members}
        if res.m.mz in members or sp.r(res.m.mz) < 1.0 or res.m.mz < 150:
            res.m_note = (f"no molecular ion observed: the highest ion m/z {res.m.mz} is a fragment "
                          f"(typical for {res.classes[0].label.lower()})")
            res.m = None
            res.losses = []
            feats = _Features(sp, None, series, res.isotopes)
            res.classes = _classes(feats, rules)
    res.compounds = _fingerprints(sp, fingerprints)
    # a strong substance match supports its class
    for comp in res.compounds:
        cls = next((c for c in res.classes if c.id == comp.cls), None)
        if cls is not None and comp.level in ("strong", "fair"):
            cls.score = min(1.0, max(cls.score, 0.5 + 0.5 * comp.score))
            cls.level = _level(cls.score, 0.8, 0.6)
            cls.fp_support = True
            note = f"ion pattern of {comp.name}"
            if note not in cls.evidence:
                cls.evidence.append(note)
    res.classes.sort(key=lambda h: (-h.score, not h.fp_support))
    res.classes = res.classes[:6]
    if res.m is not None and res.classes:
        if res.classes[0].id in weak:
            res.m.evidence.append(f"{res.classes[0].label}: the M⁺· is often weak or absent — the highest ions "
                                  "may be fragments")
            if res.m.level == "probable":
                res.m.level = "possible"
    if res.m is not None and res.m.level != "uncertain" and res.m.mz <= 700:
        from gcws.ms.formula import candidates
        fixed = {el: n for el, n in res.m.hetero.items() if el in ("Cl", "Br", "Si", "S")}
        c_range = (res.carbon[1], res.carbon[2]) if res.carbon else None
        obs = iso.observed_cluster(sp.abs, res.m.mz, 5)
        top = res.classes[0] if res.classes and res.classes[0].score >= 0.6 else None
        hints = next((r.get("hints") for r in rules if top is not None and r["id"] == top.id), None)
        res.formulas = candidates(res.m.mz, obs, sp.threshold, c_range, fixed,
                                  max_n=3 if res.m.mz % 2 else 4, max_o=8, hints=hints)
    res.checks = _checks(res, sp, ctx)
    res.warnings = _warnings(sp, res)
    res.summary = _summary(res)
    return res
