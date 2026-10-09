"""A client report simulated from the cached program evidence and rule parameters, so the naming families can be
fitted against the client F1 without processing the runs again. Rules per peak, in order: ISTD out, family 1
(background), family 2 (reporting limit), family 4 (learned families -> one sum line each), family 3 (named when
the hit is good enough), family 5 (the rest: unknown line or dropped)."""
from __future__ import annotations

import statistics
from typing import Optional

from gcws.learn.rules import background, keep_report
from gcws.learn.runner import ProgramResult

NAMING_SPACE = {"min_score": [80.0, 70.0, 75.0, 85.0, 90.0], "unknowns": ["report", "drop"]}


def peak_conc(prog: ProgramResult, rt_tol: float = 0.02) -> list[Optional[float]]:
    """mg/kg per peak: area x the run's median (reported mg/kg / area) over reported rows matched to a peak."""
    ratios = []
    for r in prog.reported:
        mg, rt = r.get("mean_mgkg"), r.get("rt")
        if mg is None or rt is None:
            continue
        near = [p for p in prog.peaks if p.get("area") and abs(p["rt"] - rt) <= rt_tol]
        if near:
            ratios.append(mg / min(near, key=lambda p: abs(p["rt"] - rt))["area"])
    if not ratios:
        return [None] * len(prog.peaks)
    k = statistics.median(ratios)
    return [p["area"] * k if p.get("area") is not None else None for p in prog.peaks]


def simulate_report(prog: ProgramResult, params: dict, families=()) -> Optional[list[dict]]:
    from gcws.learn.families import family_of
    conc = peak_conc(prog)
    if all(c is None for c in conc):
        return None
    p_bg = {"ratio_limit": params.get("ratio_limit", 3.0)}
    p_rep = {"limit": params.get("limit", 0.01)}
    lines: list[dict] = []
    sums: dict[str, dict] = {}
    for peak, c in zip(prog.peaks, conc):
        if peak.get("istd") or background(peak, p_bg)[0] or not keep_report(c, p_rep)[0]:
            continue
        fam = family_of(peak, families) if families else None
        if fam is not None:
            sums.setdefault(fam["label"], {"rt": None, "name": fam["sum_text"], "cas": "", "kind": "sum"})
            continue
        name = peak.get("name") or ""
        score = peak.get("score")
        if score is not None and score >= params["min_score"] and peak.get("cas") and \
                not name.casefold().startswith("unknown"):
            lines.append({"rt": peak["rt"], "name": name, "cas": peak["cas"], "kind": "line"})
        elif params["unknowns"] == "report":
            lines.append({"rt": peak["rt"], "name": name or "unknown", "cas": "", "kind": "line"})
    return lines + list(sums.values())
