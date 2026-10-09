"""Rule families 1 (background) and 2 (keep/report), spec §5. Each rule is a pure function of the evidence and
its parameters and returns its decision with a reason naming the rule, its version and the values."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from gcws.learn.match import HumanItem, Pair
from gcws.learn.runner import ProgramResult
from gcws.learn.score import score_run

BACKGROUND_SPACE = {"ratio_limit": [3.0, 2.0, 1.5, 5.0]}
REPORT_SPACE = {"limit": [0.01, 0.005, 0.02]}


def _num(x: float) -> str:
    return f"{x:g}"


def background(peak: dict, p: dict) -> tuple[bool, str]:
    """Family 1: remove a peak found in a batch blank when the sample is below ratio_limit x the blank."""
    blank, ratio = peak.get("in_blank"), peak.get("blank_ratio")
    if not blank or ratio is None:
        return False, "background v1: not in a blank"
    if ratio < p["ratio_limit"]:
        return True, f"background v1: in blank {blank}, ratio {ratio:.1f} < {_num(p['ratio_limit'])}"
    return False, f"background v1: in blank {blank}, ratio {ratio:.1f} >= {_num(p['ratio_limit'])}"


def keep_report(conc_mgkg: Optional[float], p: dict) -> tuple[bool, str]:
    """Family 2: report a kept peak at or above the reporting limit (mg/kg food)."""
    if conc_mgkg is None:
        return False, "report v1: no concentration"
    if conc_mgkg >= p["limit"]:
        return True, f"report v1: {conc_mgkg:.4f} mg/kg >= {_num(p['limit'])}"
    return False, f"report v1: {conc_mgkg:.4f} mg/kg < {_num(p['limit'])}"


@dataclass
class CachedRun:
    """One evaluated run with its phase-2 program result and pairs (no processing needed to evaluate rules)."""
    batch: str
    items: list[HumanItem]
    prog: Optional[ProgramResult]
    pairs: list[Pair]


def worksheet_agreement(run: CachedRun, params: dict) -> Optional[float]:
    return score_run(run.items, run.prog, run.pairs, blank_ratio_limit=params["ratio_limit"]).worksheet_agreement


def report_agreement(run: CachedRun, params: dict) -> Optional[float]:
    """Share of the analyst's kept peaks (with a mg/kg value) where family 2 makes the analyst's report choice."""
    judged = [(it.decision.startswith("reported_"), keep_report(it.conc_mgkg, params)[0]) for it in run.items
              if it.conc_mgkg is not None and (it.decision.startswith("reported_") or it.decision == "kept_unreported")]
    return sum(a == b for a, b in judged) / len(judged) if judged else None
