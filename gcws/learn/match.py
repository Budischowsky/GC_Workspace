"""Pair the analyst's rows with the program's peaks. Both come from the FID trace of the same run (the
analyst's areas are ChemStation FID peaks), so the RT is compared directly."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from gcws.learn.model import HumanEvaluation
from gcws.learn.runner import ProgramResult
from gcws.learn.workbook import removed_peaks

DECISIONS = ("istd", "reported_named", "reported_group", "reported_unknown", "reported_coelution",
             "reported_derivative", "kept_unreported", "background", "removed_blank", "removed_other")

#: worksheet row class -> the class of a reported line
_REPORTED_CLASS = {"named": "named", "named_no_cas": "named", "group": "group", "unknown": "unknown",
                   "unnamed": "unknown", "coelution": "coelution", "derivative": "derivative"}


@dataclass
class HumanItem:
    rt: float
    area: Optional[float]
    label: str
    cas: str
    row_class: str
    decision: str
    source: str                 # "final" (worksheet row) | "removed" (raw FID peak the analyst left out)
    conc_mgkg: Optional[float] = None   # the worksheet's mg/kg value (None without a mg/kg column)


@dataclass
class Pair:
    human: Optional[int]        # index into the human items
    program: Optional[int]      # index into ProgramResult.peaks
    kind: str                   # "exact" | "inside" | "missing" (human only) | "extra" (program only)
    decision: str               # the analyst's decision ("" for extra)
    rt_diff: Optional[float]


def human_items(ev: HumanEvaluation, report_rt_tol: float = 0.005) -> list[HumanItem]:
    """The analyst's decisions per peak: worksheet rows with an RT (sum lines excluded), then the FID peaks
    the analyst removed (provisionally "removed_other"; match_run tells blank from other)."""
    reported_rts = [r.rt for r in ev.report if r.rt is not None]
    unit = next((u for u in ev.header.conc_units if "mg/kg" in u.casefold()), None)
    items = []
    for r in ev.final:
        if r.rt is None or r.row_class == "sum":
            continue
        if r.row_class in ("istd", "background"):
            decision = r.row_class
        elif any(abs(r.rt - x) <= report_rt_tol for x in reported_rts):
            decision = "reported_" + _REPORTED_CLASS.get(r.row_class, "unknown")
        else:
            decision = "kept_unreported"
        items.append(HumanItem(r.rt, r.area, r.label, r.cas, r.row_class, decision, "final",
                               r.conc.get(unit) if unit else None))
    for p in removed_peaks(ev):
        items.append(HumanItem(p.rt, p.area, "", "", "", "removed_other", "removed"))
    return items


def match_run(ev: HumanEvaluation, prog: ProgramResult, rt_tol: float = 0.02,
              items: Optional[list[HumanItem]] = None) -> list[Pair]:
    """One pair per human item (in item order), then the program peaks no item went to ("extra").
    Nearest apexes within ``rt_tol`` pair one-to-one ("exact"); a remaining item whose RT lies inside a program
    peak's start..end belongs to that peak ("inside": the program integrated it together with a neighbour)."""
    items = human_items(ev) if items is None else items
    peaks = prog.peaks
    candidates = sorted((abs(it.rt - p["rt"]), h, k) for h, it in enumerate(items)
                        for k, p in enumerate(peaks) if p.get("rt") is not None and abs(it.rt - p["rt"]) <= rt_tol)
    to_program: dict[int, tuple[int, str]] = {}
    taken: set[int] = set()
    for _, h, k in candidates:
        if h not in to_program and k not in taken:
            to_program[h] = (k, "exact")
            taken.add(k)
    for h, it in enumerate(items):
        if h in to_program:
            continue
        inside = [(abs(it.rt - p["rt"]), k) for k, p in enumerate(peaks)
                  if p.get("start") is not None and p.get("end") is not None and p["start"] <= it.rt <= p["end"]]
        if inside:
            to_program[h] = (min(inside)[1], "inside")
    pairs = []
    used: set[int] = set()
    for h, it in enumerate(items):
        k, kind = to_program.get(h, (None, "missing"))
        decision = it.decision
        if it.source == "removed":
            in_blank = peaks[k].get("in_blank") if k is not None else ""
            decision = "removed_blank" if in_blank else "removed_other"
        if k is not None:
            used.add(k)
        pairs.append(Pair(h, k, kind, decision, None if k is None else round(peaks[k]["rt"] - it.rt, 6)))
    pairs += [Pair(None, k, "extra", "", None) for k in range(len(peaks)) if k not in used]
    return pairs
