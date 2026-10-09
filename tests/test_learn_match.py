"""gcws.learn.match: the analyst's FID rows paired with the program's FID peaks of the same run."""
import pytest


def _peak(rt, start=None, end=None, in_blank="", blank_ratio=None, area=1000.0, name="", cas=""):
    return {"rt": rt, "start": rt - 0.01 if start is None else start, "end": rt + 0.01 if end is None else end,
            "area": area, "in_blank": in_blank, "blank_ratio": blank_ratio, "name": name, "cas": cas}


def _prog(*peaks, reported=()):
    from gcws.learn.runner import ProgramResult
    return ProgramResult(run_dir="r", method="M", state="control", peaks=list(peaks), reported=list(reported))


@pytest.fixture
def ev(tmp_path):
    from learn_fixtures import make_workbook
    from gcws.learn.workbook import parse_workbook
    return parse_workbook(make_workbook(tmp_path / "w.xlsx"))


def _ev_with(final_rows, tmp_path, fid_peaks=()):
    from learn_fixtures import make_workbook
    from gcws.learn.workbook import parse_workbook
    return parse_workbook(make_workbook(tmp_path / "x.xlsx", final_rows=final_rows, report_rows=[],
                                        fid_peaks=[[f"{i}=", i, rt, rt - 0.01, rt + 0.01, "VV", 1, 100, 0, 0]
                                                   for i, rt in enumerate(fid_peaks, 1)]))


def test_human_items_decisions(ev):
    from gcws.learn.match import human_items
    items = {i.rt: i for i in human_items(ev)}
    assert items[7.07].decision == "reported_named"
    assert items[17.878].decision == "reported_coelution"
    assert items[21.048].decision == "reported_group"
    assert items[6.917].decision == "kept_unreported"
    assert items[14.33].decision == "istd"
    assert items[5.132].source == items[6.409].source == "removed"
    assert all(i.source == "final" for rt, i in items.items() if rt not in (5.132, 6.409))
    assert not any(i.row_class == "sum" for i in items.values())


def test_named_no_cas_counts_as_named(tmp_path):
    from learn_fixtures import make_workbook
    from gcws.learn.match import human_items
    from gcws.learn.workbook import parse_workbook
    ev = parse_workbook(make_workbook(tmp_path / "w.xlsx", report_rows=[{"A": 26.5, "B": "Some Additive"}]))
    assert {i.rt: i.decision for i in human_items(ev)}[26.5] == "reported_named"


def test_match_exact_and_extra(ev):
    from gcws.learn.match import human_items, match_run
    items = human_items(ev)
    pairs = match_run(ev, _prog(_peak(7.071), _peak(9.5)))
    by_human = {items[p.human].rt: p for p in pairs if p.human is not None}
    assert by_human[7.07].kind == "exact" and by_human[7.07].rt_diff == pytest.approx(0.001)
    assert by_human[7.07].decision == "reported_named"
    extra = [p for p in pairs if p.kind == "extra"]
    assert len(extra) == 1 and extra[0].program == 1 and extra[0].decision == ""
    assert by_human[14.33].kind == "missing" and by_human[14.33].program is None


def test_match_inside(ev):
    from gcws.learn.match import human_items, match_run
    items = human_items(ev)
    pairs = match_run(ev, _prog(_peak(21.2, start=21.0, end=21.5)))
    by_human = {items[p.human].rt: p for p in pairs if p.human is not None}
    assert by_human[21.048].kind == "inside" and by_human[21.048].program == 0
    assert by_human[21.449].kind == "inside" and by_human[21.449].program == 0
    assert not any(p.kind == "extra" for p in pairs)


def test_removed_blank_vs_other(ev):
    from gcws.learn.match import human_items, match_run
    items = human_items(ev)
    pairs = match_run(ev, _prog(_peak(5.131, in_blank="EtOH_3", blank_ratio=0.8), _peak(6.41, in_blank="")))
    by_human = {items[p.human].rt: p for p in pairs if p.human is not None}
    assert by_human[5.132].decision == "removed_blank"
    assert by_human[6.409].decision == "removed_other"


def test_unpaired_removed_peak_is_removed_other(ev):
    from gcws.learn.match import human_items, match_run
    items = human_items(ev)
    by_human = {items[p.human].rt: p for p in match_run(ev, _prog()) if p.human is not None}
    assert by_human[5.132].kind == "missing" and by_human[5.132].decision == "removed_other"


def test_one_to_one_exact(tmp_path):
    from gcws.learn.match import human_items, match_run
    ev = _ev_with([{"A": 7.00, "F": 10}, {"A": 7.01, "F": 10}], tmp_path, fid_peaks=[7.00, 7.01])
    items = human_items(ev)
    pairs = match_run(ev, _prog(_peak(7.006, start=6.99, end=7.02)))
    by_human = {items[p.human].rt: p for p in pairs if p.human is not None}
    assert by_human[7.01].kind == "exact" and by_human[7.00].kind == "inside"
    pairs = match_run(ev, _prog(_peak(7.006, start=7.005, end=7.02)))
    by_human = {items[p.human].rt: p for p in pairs if p.human is not None}
    assert by_human[7.01].kind == "exact" and by_human[7.00].kind == "missing"
