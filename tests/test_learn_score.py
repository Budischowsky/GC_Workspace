"""gcws.learn.score: per-run scores against the analyst's evaluation (hand-built items, pairs and program)."""
import pytest


def _item(rt, decision, area=100.0, cas="", label=""):
    from gcws.learn.match import HumanItem
    return HumanItem(rt=rt, area=area, label=label, cas=cas, row_class=decision.replace("reported_", ""),
                     decision=decision, source="removed" if decision.startswith("removed") else "final")


def _peak(rt, area=100.0, in_blank="", blank_ratio=None):
    return {"rt": rt, "start": rt - 0.01, "end": rt + 0.01, "area": area, "in_blank": in_blank,
            "blank_ratio": blank_ratio}


def _prog(peaks, reported=()):
    from gcws.learn.runner import ProgramResult
    return ProgramResult(run_dir="r", method="M", state="control", peaks=list(peaks), reported=list(reported))


def _pairs(items, prog):
    from gcws.learn.match import Pair
    pairs = []
    for h, it in enumerate(items):
        k = next((k for k, p in enumerate(prog.peaks) if abs(p["rt"] - it.rt) <= 0.02), None)
        pairs.append(Pair(h, k, "exact" if k is not None else "missing", it.decision,
                          None if k is None else prog.peaks[k]["rt"] - it.rt))
    return pairs


def _score(items, prog, **kw):
    from gcws.learn.score import score_run
    return score_run(items, prog, _pairs(items, prog), **kw)


def test_client_f1():
    items = [_item(7.07, "reported_named", cas="97-88-1"), _item(21.0, "reported_group"),
             _item(17.9, "reported_coelution")]
    prog = _prog([_peak(7.07), _peak(21.0), _peak(17.9)],
                 reported=[{"rt": 7.071, "cas": "50-00-0", "name": "x"}, {"rt": 21.0, "cas": "", "name": "y"},
                           {"rt": 17.9, "cas": "", "name": "z"}])
    s = _score(items, prog)
    assert s.client_precision == pytest.approx(2 / 3) and s.client_recall == pytest.approx(2 / 3)
    assert s.client_f1 == pytest.approx(2 / 3)
    assert s.name_agreement == 0 and s.disagreements["name_differs"] == 1


def test_missing_not_reported_extra():
    items = [_item(5.0, "reported_named", cas="1-1-1"), _item(6.0, "reported_named", cas="2-2-2"),
             _item(8.0, "kept_unreported")]
    prog = _prog([_peak(6.0), _peak(8.0)], reported=[{"rt": 8.0, "cas": "", "name": "q"}])
    s = _score(items, prog)
    assert s.disagreements["missing_peak"] == 1 and s.disagreements["not_reported"] == 1
    assert s.disagreements["extra_reported"] == 1
    assert s.client_f1 == 0


def test_conc_dev_uses_istd_scale():
    items = [_item(14.3, "istd", area=100.0), _item(7.0, "reported_group", area=30.0)]
    prog = _prog([_peak(14.3, area=50.0), _peak(7.0, area=18.0)], reported=[{"rt": 7.0, "cas": ""}])
    s = _score(items, prog)
    assert s.istd_scale == pytest.approx(2.0)
    assert s.conc_dev_median == pytest.approx(0.2)


def test_no_istd():
    items = [_item(7.0, "reported_group", area=30.0)]
    prog = _prog([_peak(7.0, area=18.0)], reported=[{"rt": 7.0, "cas": ""}])
    s = _score(items, prog)
    assert s.istd_scale is None and s.conc_dev_median is None
    assert s.client_f1 == 1


def test_worksheet_agreement():
    items = [_item(7.0, "kept_unreported"), _item(8.0, "removed_blank"), _item(9.0, "removed_other"),
             _item(10.0, "background")]
    prog = _prog([_peak(7.0), _peak(8.0, in_blank="b", blank_ratio=0.5), _peak(9.0, in_blank="", blank_ratio=None),
                  _peak(10.0, in_blank="b", blank_ratio=2.9)])
    s = _score(items, prog)
    assert s.worksheet_agreement == pytest.approx(3 / 4)
    assert s.disagreements["removed_but_program_keeps"] == 1
    assert s.disagreements["kept_but_program_removes"] == 0


def test_program_blank_limit():
    items = [_item(7.0, "kept_unreported")]
    s = _score(items, _prog([_peak(7.0, in_blank="b", blank_ratio=2.0)]))
    assert s.worksheet_agreement == 0 and s.disagreements["kept_but_program_removes"] == 1
    s = _score(items, _prog([_peak(7.0, in_blank="b", blank_ratio=2.0)]), blank_ratio_limit=1.5)
    assert s.worksheet_agreement == 1


def test_total_weights():
    items = [_item(7.0, "reported_group"), _item(8.0, "removed_blank")]
    prog = _prog([_peak(7.0), _peak(8.0)], reported=[{"rt": 7.0, "cas": ""}])
    s = _score(items, prog)
    assert s.client_f1 == 1 and s.worksheet_agreement == 0.5
    assert s.total == pytest.approx(0.7 * 1 + 0.3 * 0.5)
    assert _score([], _prog([])).total is None


def test_report_lines_preferred_over_register_list():
    """The client report's lines (report_lines) are what the client sees; the register list also holds ISTDs."""
    from gcws.learn.runner import ProgramResult
    items = [_item(7.0, "reported_group"), _item(14.3, "istd")]
    prog = ProgramResult(run_dir="r", method="M", state="control", peaks=[_peak(7.0), _peak(14.3)],
                         reported=[{"rt": 7.0, "cas": ""}, {"rt": 14.3, "cas": ""}],
                         report_lines=[{"rt": 7.0, "name": "x", "cas": "", "kind": "line"}])
    s = _score(items, prog)
    assert s.client_precision == 1 and s.disagreements["extra_reported"] == 0


def test_sum_line_covers_the_family():
    from gcws.learn.runner import ProgramResult
    items = [_item(21.0, "reported_group", label="Styrene Oligomer"),
             _item(24.5, "reported_group", label="styrene oligomer"),
             _item(26.0, "reported_group", label="Hydrocarbon")]
    prog = ProgramResult(run_dir="r", method="M", state="control", peaks=[_peak(21.0), _peak(24.5), _peak(26.0)],
                         report_lines=[{"rt": None, "name": "Sum of styrene oligomers (estimated)**", "cas": "",
                                        "kind": "sum"},
                                       {"rt": None, "name": "Sum of decanamides", "cas": "", "kind": "sum"}])
    s = _score(items, prog)
    assert s.client_recall == pytest.approx(2 / 3)
    assert s.client_precision == pytest.approx(1 / 2)
    assert s.disagreements["not_reported"] == 1 and s.disagreements["extra_reported"] == 1
