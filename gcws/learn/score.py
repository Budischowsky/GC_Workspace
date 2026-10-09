"""How close the program's result for one run is to the analyst's (spec §7)."""
from __future__ import annotations

import re
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
    details: list = field(default_factory=list)     # one entry per counted disagreement


def _reported(decision: str) -> bool:
    return decision.startswith("reported_")


def _words(text: str) -> str:
    """Lower-case words without plural s: 'Sum of styrene oligomers (estimated)**' -> 'sum of styrene oligomer estimated'."""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-zäöüß]+", text.casefold()))


def same_family(label: str, sum_line: str) -> bool:
    """Whether a program 'Sum of …' line names the analyst's group label (scoring only)."""
    a = _words(label)
    return bool(a) and a in _words(sum_line)


def client_lines(prog: ProgramResult) -> list[tuple]:
    """(rt, cas, name) of the program's client report lines; the register list (which also holds the ISTDs)
    only when the report lines are not known."""
    if prog.report_lines:
        return [(r.get("rt"), normalise_cas(r.get("cas")), r.get("name", "")) for r in prog.report_lines]
    return [(r.get("rt"), normalise_cas(r.get("cas")), r.get("name", "")) for r in prog.reported
            if r.get("rt") is not None]


def program_removes(peak: dict, blank_ratio_limit: float = 3.0) -> bool:
    """The program's own blank classification: found in a blank and below ratio_limit x the blank area
    (gcws.quant.blank_match.classify, default limit 3)."""
    ratio = peak.get("blank_ratio")
    return bool(peak.get("in_blank")) and ratio is not None and ratio < blank_ratio_limit


def score_run(items: list[HumanItem], prog: ProgramResult, pairs: list[Pair], *, weights=(0.7, 0.3),
              rt_tol: float = 0.02, blank_ratio_limit: float = 3.0, verdicts: Optional[dict] = None) -> RunScore:
    """``verdicts``: (disagreement type, RT rounded to 2) -> "analyst" | "program" | "both" (the user's review);
    with "program" or "both" that disagreement does not count."""
    s = RunScore()
    verdicts = verdicts or {}

    def excused(kind: str, rt) -> bool:
        return verdicts.get((kind, None if rt is None else round(rt, 2))) in ("program", "both")

    def note(kind: str, rt, human=None, program=None, line=None) -> None:
        s.disagreements[kind] += 1
        s.details.append({"type": kind, "rt": rt, "human": human, "program": program, "line": line})

    peaks = prog.peaks
    by_human = {p.human: p for p in pairs if p.human is not None}
    lines = client_lines(prog)
    used: set[int] = set()
    correct: set[int] = set()
    tp = named_both = named_equal = 0
    human_lines = 0
    for h, it in enumerate(items):
        if not _reported(it.decision):
            continue
        human_lines += 1
        pair = by_human.get(h)
        k = pair.program if pair else None
        ref = peaks[k]["rt"] if k is not None else it.rt
        near = sorted((abs(rt - ref), j) for j, (rt, _, _) in enumerate(lines)
                      if rt is not None and j not in used and abs(rt - ref) <= rt_tol)
        if not near and it.decision == "reported_group":
            family = [j for j, (rt, _, name) in enumerate(lines) if rt is None and same_family(it.label, name)]
            if family:                  # a "Sum of …" line reports the whole family
                used.add(family[0])
                correct.add(family[0])
                tp += 1
                continue
        if not near:
            kind = "missing_peak" if k is None else "not_reported"
            if excused(kind, it.rt):
                human_lines -= 1
            else:
                note(kind, it.rt, human=h, program=k)
            continue
        j = near[0][1]
        used.add(j)
        cas = lines[j][1]
        if it.decision == "reported_named" and it.cas and cas:
            named_both += 1
            if cas == it.cas or excused("name_differs", it.rt):
                named_equal += 1
            else:
                note("name_differs", it.rt, human=h, program=k, line=j)
                continue
        correct.add(j)
        tp += 1
    n_lines = len(lines)
    for j, (rt, _, _) in enumerate(lines):
        if j in used:
            continue
        if excused("extra_reported", rt):
            n_lines -= 1
        else:
            note("extra_reported", rt, line=j)
    if n_lines or human_lines:
        s.client_precision = len(correct) / n_lines if n_lines else 0.0
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
        kind = "kept_but_program_removes" if human_keeps else "removed_but_program_keeps"
        if human_keeps != removes or excused(kind, it.rt):
            agree += 1
        else:
            note(kind, it.rt, human=h, program=pair.program)
    s.worksheet_agreement = agree / n if n else None
    if s.client_f1 is not None and s.worksheet_agreement is not None:
        s.total = weights[0] * s.client_f1 + weights[1] * s.worksheet_agreement
    return s
