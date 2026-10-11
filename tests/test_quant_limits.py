"""gcws.quant.limits: one value for the difference limit, the reporting limit and the RT tolerance."""
from types import SimpleNamespace

from gcws.automation import rules as RU
from gcws.quant import limits as QL
from gcws.quant.nias_bridge import make_settings


def test_defaults_when_nothing_is_set():
    assert QL.of_settings(SimpleNamespace()) == QL.QuantLimits(30.0, 0.01, 0.035)
    assert QL.of_evidence({}) == QL.QuantLimits(30.0, 0.01, 0.035)
    assert QL.of_evidence({"settings": {"reporting_limit": "x", "duplicate_max_reldiff": float("nan")}}) \
        == QL.QuantLimits()


def test_zero_is_a_limit():
    """A configured 0 used to become 0.01 (reporting) in the rules and the register."""
    q = QL.of_settings(make_settings({"reporting_limit": 0.0, "duplicate_max_reldiff": 0.0}))
    assert (q.reporting, q.difference) == (0.0, 0.0)
    assert QL.of_evidence({"settings": q.as_evidence()}) == q


def test_rt_tolerance_not_positive_is_the_default():
    assert QL.of_settings(SimpleNamespace(rt_tolerance=0)).rt_tolerance == 0.035
    assert QL.of_settings(SimpleNamespace(rt_tolerance=0.05)).rt_tolerance == 0.05


def test_of_quant_by_mode():
    nias = QL.of_quant({"mode": "nias_mgkg", "settings": {"reporting_limit": 0.05, "duplicate_max_reldiff": 20}})
    assert (nias.difference, nias.reporting) == (20.0, 0.05)
    other = QL.of_quant({"mode": "total_ugl", "settings": {"reporting_limit": 0.05}})
    assert other.reporting == 0.0
    hs = QL.of_quant({"mode": "hs_screening", "hs": {"duplicate_max_reldiff": 0}})
    assert (hs.difference, hs.reporting) == (0.0, 0.0)


def test_rules_report_everything_with_reporting_limit_zero():
    """With reporting limit 0 a substance at 0.005 mg/kg is reported, so the rules look at it."""
    row = {"name": "Unknown 1", "mean": 0.005, "rt": 10.0}
    ev = {"rows": [row], "settings": QL.QuantLimits(reporting=0.0).as_evidence()}
    found = RU.CHECKS["unidentified_over"](RU.Rule("unidentified_over", params={"max": 0}), ev)
    assert found and found[0].value == 1
    ev["settings"] = QL.QuantLimits().as_evidence()
    assert RU.CHECKS["unidentified_over"](RU.Rule("unidentified_over", params={"max": 0}), ev) == []
