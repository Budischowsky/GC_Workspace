"""gcws.learn.rules: families 1 (background) and 2 (keep/report) with their reasons."""
import pytest


def _item(rt, decision, conc=None):
    from gcws.learn.match import HumanItem
    return HumanItem(rt=rt, area=100.0, label="", cas="", row_class="", decision=decision,
                     source="removed" if decision.startswith("removed") else "final", conc_mgkg=conc)


def test_background_reasons():
    from gcws.learn.rules import background
    assert background({"in_blank": "EtOH_3", "blank_ratio": 0.6}, {"ratio_limit": 3.0}) == (
        True, "background v1: in blank EtOH_3, ratio 0.6 < 3")
    assert background({"in_blank": "EtOH_3", "blank_ratio": 4.2}, {"ratio_limit": 3.0}) == (
        False, "background v1: in blank EtOH_3, ratio 4.2 >= 3")
    assert background({"in_blank": "", "blank_ratio": None}, {"ratio_limit": 3.0}) == (
        False, "background v1: not in a blank")


def test_keep_report_reasons():
    from gcws.learn.rules import keep_report
    assert keep_report(0.0123, {"limit": 0.01}) == (True, "report v1: 0.0123 mg/kg >= 0.01")
    assert keep_report(0.004, {"limit": 0.01}) == (False, "report v1: 0.0040 mg/kg < 0.01")
    assert keep_report(None, {"limit": 0.01}) == (False, "report v1: no concentration")


def test_report_agreement():
    from gcws.learn.rules import CachedRun, report_agreement
    run = CachedRun(batch="B", items=[_item(7.0, "reported_named", 0.02), _item(8.0, "kept_unreported", 0.004),
                                      _item(9.0, "istd", 0.1), _item(10.0, "removed_other")], prog=None, pairs=[])
    assert report_agreement(run, {"limit": 0.01}) == 1
    assert report_agreement(run, {"limit": 0.03}) == 0.5
    assert report_agreement(CachedRun("B", [_item(9.0, "istd", 0.1)], None, []), {"limit": 0.01}) is None


def test_worksheet_agreement_uses_ratio_limit():
    from gcws.learn.match import Pair
    from gcws.learn.rules import CachedRun, worksheet_agreement
    from gcws.learn.runner import ProgramResult
    items = [_item(7.0, "kept_unreported"), _item(8.0, "removed_blank")]
    prog = ProgramResult(run_dir="r", method="M", state="control",
                         peaks=[{"rt": 7.0, "in_blank": "b", "blank_ratio": 2.0, "area": 1.0},
                                {"rt": 8.0, "in_blank": "b", "blank_ratio": 0.5, "area": 1.0}])
    pairs = [Pair(0, 0, "exact", "kept_unreported", 0.0), Pair(1, 1, "exact", "removed_blank", 0.0)]
    run = CachedRun("B", items, prog, pairs)
    assert worksheet_agreement(run, {"ratio_limit": 3.0}) == 0.5
    assert worksheet_agreement(run, {"ratio_limit": 1.5}) == 1
