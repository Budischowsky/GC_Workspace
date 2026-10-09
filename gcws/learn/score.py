"""How close the program's result for one run is to the analyst's (spec §7)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional

from gcws.learn.match import HumanItem, Pair
from gcws.learn.model import normalise_cas
from gcws.learn.runner import ProgramResult

KEEP = ("kept_unreported",)                     # + every "reported_*" decision
REMOVE = ("background", "removed_blank", "removed_other")
DISAGREEMENTS = ("missing_peak", "not_reported", "extra_reported", "name_differs", "kept_but_program_removes",
                 "removed_but_program_keeps")


@dataclass
class RunScore:
    client_f1: Optional[float] = None
    client_precision: Optional[float] = None
    client_recall: Optional[float] = None
    name_agreement: Optional[float] = None
    conc_dev_median: Optional[float] = None
    istd_scale: Optional[float] = None
    worksheet_agreement: Optional[float] = None
    total: Optional[float] = None
    disagreements: dict = field(default_factory=lambda: dict.fromkeys(DISAGREEMENTS, 0))


def _reported(decision: str) -> bool:
    return decision.startswith("reported_")


def program_removes(peak: dict, blank_ratio_limit: float = 3.0) -> bool:
    """The program's own blank classification: found in a blank and below ratio_limit x the blank area
    (gcws.quant.blank_match.classify, default limit 3)."""
    ratio = peak.get("blank_ratio")
    return bool(peak.get("in_blank")) and ratio is not None and ratio < blank_ratio_limit


def score_run(items: list[HumanItem], prog: ProgramResult, pairs: list[Pair], *, weights=(0.7, 0.3),
              rt_tol: float = 0.02, blank_ratio_limit: float = 3.0) -> RunScore:
    s = RunScore()
    peaks = prog.peaks
    by_human = {p.human: p for p in pairs if p.human is not None}
    lines = [(r.get("rt"), normalise_cas(r.get("cas"))) for r in prog.reported if r.get("rt") is not None]
    used: set[int] = set()
    tp = named_both = named_equal = 0
    human_lines = 0
    for h, it in enumerate(items):
        if not _reported(it.decision):
            continue
        human_lines += 1
        pair = by_human.get(h)
        ref = peaks[pair.program]["rt"] if pair and pair.program is not None else it.rt
        near = sorted((abs(rt - ref), j) for j, (rt, _) in enumerate(lines) if j not in used and abs(rt - ref) <= rt_tol)
        if not near:
            s.disagreements["missing_peak" if pair is None or pair.program is None else "not_reported"] += 1
            continue
        j = near[0][1]
        used.add(j)
        cas = lines[j][1]
        if it.decision == "reported_named" and it.cas and cas:
            named_both += 1
            if cas == it.cas:
                named_equal += 1
            else:
                s.disagreements["name_differs"] += 1
                continue
        tp += 1
    s.disagreements["extra_reported"] = len(lines) - len(used)
    if lines or human_lines:
        s.client_precision = tp / len(lines) if lines else 0.0
        s.client_recall = tp / human_lines if human_lines else 0.0
        pr = s.client_precision + s.client_recall
        s.client_f1 = 2 * s.client_precision * s.client_recall / pr if pr else 0.0
    if named_both:
        s.name_agreement = named_equal / named_both
    # concentration: ISTD-relative areas (units do not matter)
    istd = [(it.area, peaks[by_human[h].program]["area"]) for h, it in enumerate(items)
            if it.decision == "istd" and it.area and by_human.get(h) and by_human[h].program is not None
            and by_human[h].kind == "exact" and peaks[by_human[h].program].get("area")]
    if istd:
        s.istd_scale = statistics.mean(a for a, _ in istd) / statistics.mean(b for _, b in istd)
        devs = [abs(peaks[by_human[h].program]["area"] * s.istd_scale - it.area) / it.area
                for h, it in enumerate(items)
                if _reported(it.decision) and it.area and by_human.get(h) and by_human[h].kind == "exact"
                and peaks[by_human[h].program].get("area")]
        s.conc_dev_median = statistics.median(devs) if devs else None
    # worksheet: keep / remove agreement on the paired peaks
    agree = n = 0
    for h, it in enumerate(items):
        pair = by_human.get(h)
        if pair is None or pair.program is None:
            continue
        human_keeps = _reported(it.decision) or it.decision in KEEP
        if not human_keeps and it.decision not in REMOVE:
            continue
        removes = program_removes(peaks[pair.program], blank_ratio_limit)
        n += 1
        if human_keeps != removes:
            agree += 1
        elif human_keeps:
            s.disagreements["kept_but_program_removes"] += 1
        else:
            s.disagreements["removed_but_program_keeps"] += 1
    s.worksheet_agreement = agree / n if n else None
    if s.client_f1 is not None and s.worksheet_agreement is not None:
        s.total = weights[0] * s.client_f1 + weights[1] * s.worksheet_agreement
    return s
