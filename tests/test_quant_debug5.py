"""Quantification / double determination: limits, report rows with the analyst's values, report units."""
from types import SimpleNamespace

from gcws.quant import duplicate_view as DV


def test_difference_limit_zero_is_kept():
    """0 % is a valid difference limit (it flags every pair that disagrees, as the workbook does);
    it used to become 30 % in the list and the reports."""
    ws = SimpleNamespace(quant={"mode": "nias_mgkg", "settings": {"duplicate_max_reldiff": 0.0}})
    assert DV.limits(ws)[0] == 0.0
    from gcws.quant.nias_bridge import make_settings
    assert DV.reldiff_limit(make_settings({"duplicate_max_reldiff": 0.0})) == 0.0
    assert DV.reldiff_limit(SimpleNamespace()) == 30.0


def test_report_determinations_take_the_analysts_difference():
    """The Determinations sheet's relative difference is the one with the analyst's values: none when
    one determination is dismissed, recomputed for edited concentrations."""
    from gcws.report import simple as S
    row = {"rt": 10.0, "name": "X", "cas": "", "c1": 1.0, "c2": 3.0, "mean": 2.0, "reldiff": 100.0,
           "cs": [1.0, 3.0], "sources": [{}, {}], "status": "Valid duplicate", "review": ""}
    ratio = lambda k, u: 1.0
    job = SimpleNamespace(samples=[1, 2], overrides={0: {"c1": 1.0, "c2": 3.0, "mean": 1.0, "reldiff": None,
                                                         "dismissed": 2}})
    _rows, dets = S.report_rows(job, [row], ["mg/kg", "mg/kg"], ratio)
    assert dets[0][-3] is None
    job.overrides = {0: {"c1": 2.0, "c2": 3.0, "mean": 2.5, "reldiff": 40.0, "dismissed": 0}}
    _rows, dets = S.report_rows(job, [row], ["mg/kg", "mg/kg"], ratio)
    assert dets[0][-3] == 40.0
    job.overrides = {}
    _rows, dets = S.report_rows(job, [row], ["mg/kg", "mg/kg"], ratio)
    assert dets[0][-3] == 100.0


def test_report_units_are_two_different_units():
    from gcws.quant import extraction as EX
    from gcws.quant.hs import report_units
    assert report_units({"report_units": ["µg/g", "µg/g"]}) == ["µg/g", "µg/dm²"]
    m = EX.normalise({"units": ["mg/kg", "mg/kg"]})
    assert m["units"][0] == "mg/kg" and m["units"][1] != "mg/kg"


def test_reported_row_never_takes_the_dismissed_value():
    """A dismissed and B lost after a re-integration (now only in A): reported by the analyst, the
    result must not become A's concentration, the outlier."""
    row = {"rt": 10.0, "name": "X", "cas": "", "status": "Artefact: only determination 1", "c1": 5.0, "c2": None,
           "mean": None, "reldiff": None, "id_status": "Accepted", "review": "",
           "source1": {"rt": 10.0, "name": "X", "area": 100.0}, "source2": None}
    verdicts = [DV.plain_verdict(row, 30.0, 0.01)]
    out = DV.apply_edits([row], verdicts, {"10.000": {"rt": 10.0, "dismiss": 1, "report": True}}, 0.035)
    assert out[0]["report"] and out[0]["dismissed"] == 1
    assert out[0]["mean"] is None
    # without a dismissal the analyst's kept single determination is reported with its value
    out = DV.apply_edits([row], verdicts, {"10.000": {"rt": 10.0, "report": True}}, 0.035)
    assert out[0]["mean"] == 5.0
