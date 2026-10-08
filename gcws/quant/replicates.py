"""Replicate determinations (single, duplicate, triplicate, ... N-fold).

N = 1 and N = 2 delegate to AutoLib (``single_determination_rows`` and
``combine_determinations``), so a duplicate is paired exactly as NIAS always
paired it. N >= 3 generalises the same rules: identity first (accepted hits
with the same CAS or name), retention time within the tolerance for unknowns
and conflicts, "also in Blank" when any replicate had blank area subtracted.
"""
from __future__ import annotations

import statistics
from typing import Callable, Optional

POLICIES = {"all": "Found in all determinations", "majority": "Found in the majority",
            "any": "Found in at least one"}


def engine_peaks(sample, value: Optional[Callable] = None) -> list[dict]:
    if getattr(sample, "mode", None) == "hs_screening":
        from gcws.quant.hs import engine_peaks as hs_peaks
        return hs_peaks(sample, value)
    import gc_fid
    if value is None and (getattr(sample, "meta", None) or {}).get("extraction") is not None:
        value = lambda row: row.derived.get("extraction_conc")         # extraction: Conc. 1 of the method
    out = []
    for row in gc_fid.report_rows(sample):
        d = gc_fid._as_engine_peak(row)
        d["gcws_index"] = row.derived.get("gcws_index")      # the integrated peak (AutoLib ignores the key)
        if value is not None:
            d["mg_kg"] = value(row)
        out.append(d)
    return out


def _stats(values):
    present = [v for v in values if v is not None]
    mean = statistics.mean(present) if present else None
    sd = statistics.stdev(present) if len(present) >= 2 else None
    rsd = (sd / mean * 100.0) if (sd is not None and mean) else None
    return present, mean, sd, rsd


def _decorate(rows, n):
    """Add the N-fold keys to AutoLib's duplicate/single rows."""
    for r in rows:
        cs = [r.get("c1"), r.get("c2")][:n]
        present, mean, sd, rsd = _stats(cs)
        r["cs"] = cs
        r["sources"] = [r.get("source1"), r.get("source2")][:n]
        r["sd"] = sd
        r["rsd"] = rsd
        r["n_detected"] = sum(1 for s in r["sources"] if s is not None)
        r["n"] = n
    return rows


def required(policy: str, n: int) -> int:
    return {"all": n, "majority": n // 2 + 1, "any": 1}.get(policy, n)


def combine(lists: list[list[dict]], tolerance: float, policy: str = "all") -> list[dict]:
    import gc_fid
    eng = gc_fid.engine()
    n = len(lists)
    if n == 0:
        return []
    if n == 1:
        return _decorate(eng.single_determination_rows(lists[0]), 1)
    if n == 2 and policy == "all":
        return _decorate(eng.combine_determinations(lists[0], lists[1], tolerance), 2)

    clusters: list[dict] = []
    for k, peaks in enumerate(lists):
        for a in sorted(peaks, key=lambda x: x["rt"]):
            free = [c for c in clusters if k not in c["m"]
                    and abs(statistics.mean(p["rt"] for p in c["m"].values()) - a["rt"]) <= tolerance]
            same = [c for c in free if any(eng._same_identity(a, p) for p in c["m"].values())]
            pool = same or free
            if pool:
                c = min(pool, key=lambda c: abs(statistics.mean(p["rt"] for p in c["m"].values()) - a["rt"]))
                c["m"][k] = a
            else:
                clusters.append({"m": {k: a}})

    need = required(policy, n)
    rows = []
    for c in clusters:
        members = c["m"]
        sources = [members.get(k) for k in range(n)]
        present = [s for s in sources if s is not None]
        cs = [s["mg_kg"] if s is not None else None for s in sources]
        vals, mean, sd, rsd = _stats(cs)
        accepted = [s for s in present if s.get("id_status") == "Accepted"]
        conflict = any(not eng._same_identity(accepted[0], s) for s in accepted[1:]) if len(accepted) > 1 else False
        if len(present) >= need:
            status = "Identification conflict" if conflict else (
                "Valid duplicate" if n == 2 and len(present) == 2 else f"Valid replicate ({len(present)}/{n})")
        else:
            which = ", ".join(str(k + 1) for k in range(n) if sources[k] is not None)
            status = f"Artefact: only determination {which}"
            mean = None
        if any(s.get("blank_area") or s.get("blank_istd_area") for s in present):
            status += ", also in Blank"
        reviews = [s["review"] for s in present if s.get("review")]
        if conflict:
            names = list(dict.fromkeys(s["name"] for s in accepted))
            name = " / ".join(names)
            cas = " / ".join(dict.fromkeys(s["cas"] for s in accepted if s.get("cas")))
            id_status = "Conflict; manual review"
            reviews.append("Abweichende Identifikationen bei vergleichbarer RT; keine automatische Endidentifikation")
        else:
            best = accepted[0] if accepted else present[0]
            name, cas = best["name"], best["cas"] or next((s["cas"] for s in present if s.get("cas")), "")
            id_status = "Accepted" if len(accepted) == len(present) else "Manual review"
        if len(present) < need:
            reviews.append(f"Nur in Bestimmung {', '.join(str(k + 1) for k in range(n) if sources[k] is not None)} "
                           "detektiert; als Artefakt bewertet")
        reldiff = (abs(vals[0] - vals[1]) / mean * 100.0) if (len(vals) == 2 and mean) else rsd
        rows.append({"rt": statistics.mean(s["rt"] for s in present), "name": name, "cas": cas,
                     "mean": mean, "c1": cs[0], "c2": cs[1] if n > 1 else None, "reldiff": reldiff,
                     "status": status, "id_status": id_status, "review": "; ".join(dict.fromkeys(reviews)),
                     "source1": sources[0], "source2": sources[1] if n > 1 else None,
                     "cs": cs, "sources": sources, "sd": sd, "rsd": rsd,
                     "n_detected": len(present), "n": n})
    return sorted(rows, key=lambda r: r["rt"])
