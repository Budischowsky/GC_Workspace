"""What each quantification mode answers for itself (gcws.quant.modes)."""
import pytest

from gcws.quant import modes as QM
from gcws.quant import nias_bridge as NB


def _settings(**values):
    s = NB.make_settings()
    for key, value in values.items():
        setattr(s, key, value)
    return s


def _extraction(**method):
    return {"mode": "extraction", "method": {"sample_type": "solid", "amount": 1.0, "extract_volume_ml": 10.0,
                                             "units": ["mg/kg", "µg/L"], **method}}


def test_no_mode_is_nias_mgkg_and_an_unknown_key_keeps_its_name():
    assert QM.of(None).key == "nias_mgkg" and QM.of({}).nias
    odd = QM.of({"mode": "legacy", "unit": "ng/mL"})
    assert odd.key == "legacy" and odd.unit == "ng/mL" and odd.label == "legacy"
    assert odd.available_units() == ["ng/mL"] and odd.report_units() == ["ng/mL"] and odd.unit_keys() == []
    assert not odd.hs and not odd.nias and odd.edits_key("ng/mL") == "edits"


@pytest.mark.parametrize("quant, unit, report_units", [
    ({"mode": "nias_mgkg"}, "mg/kg", ["mg/kg", "mg/dm²"]),
    ({"mode": "total_ugl"}, "µg/L", ["µg/L", "mg/L"]),
    ({"mode": "istd_conc", "unit": "mg/L"}, "mg/L", ["mg/L"]),
    ({"mode": "area_pct"}, "%", ["%"]),
    ({"mode": "hs_screening", "hs": {"unit": "µg/HS"}}, "µg/HS", ["µg/dm²", "mg/m²"]),
    (_extraction(), "mg/kg", ["mg/kg", "µg/L"]),
])
def test_unit_and_report_units(quant, unit, report_units):
    mode = QM.of(quant)
    assert mode.unit == unit
    assert mode.report_units() == report_units
    assert mode.available_units()[0] == unit


def test_only_hs_screening_is_ms_only_and_only_nias_mgkg_is_the_migration_engine():
    flags = {k: (QM.of({"mode": k}).hs, QM.of({"mode": k}).nias) for k in QM.ADAPTERS}
    assert flags == {"nias_mgkg": (False, True), "total_ugl": (False, False), "istd_conc": (False, False),
                     "area_pct": (False, False), "hs_screening": (True, False), "extraction": (False, False)}


def test_detector_is_the_panels_choice_except_hs_screening():
    assert QM.of({}).detector == "FID"
    assert QM.of({"mode": "total_ugl", "detector": "TIC"}).detector == "TIC"
    assert QM.of({"mode": "hs_screening", "detector": "FID"}).detector == "TIC"


def test_further_units():
    assert QM.of({}).unit_keys() == ["mg_dm2", "ug_dm2", "ug_l", "mg_l", "mg_ml"]
    assert QM.of({}).available_units() == ["mg/kg", "mg/dm²", "µg/dm²", "µg/L", "mg/L", "mg/mL"]
    assert QM.of({"mode": "total_ugl"}).available_units() == ["µg/L", "mg/L", "mg/mL"]
    assert QM.of({"mode": "hs_screening"}).available_units() == ["µg/HS", "µg/dm²", "µg/g", "mg/m²"]
    assert QM.of(_extraction()).unit_keys() == ["mg_ml", "ug_l", "mg_g", "mg_kg", "ug_g", "ug_kg"]


def test_convert_and_ratio_agree():
    s = _settings(cell_area_dm2=0.51, coverage=1.0, extract_volume_ml=10.0, ov_ratio=6.0)
    nias = QM.of({})
    out = nias.convert(0.06, s)
    assert out["mg_dm2"] == pytest.approx(0.01) and out["mg_kg"] == pytest.approx(0.06)
    assert "surface/volume" in out["calc"]["mg_dm2"]
    assert nias.ratio(s, ["a"])(0, "mg/dm²") == pytest.approx(1 / 6)
    assert QM.of({"mode": "area_pct"}).ratio(s, ["a"])(0, "µg/L") is None
    ext = QM.of({**_extraction(), "method_samples": {"b": {"amount": 2.0}}})
    assert ext.convert(1.0, None, "b")["ug_l"] == pytest.approx(200.0)
    assert ext.ratio(None, ["a", "b"])(1, "µg/L") == pytest.approx(200.0)


def test_limits_and_edits_key_by_mode():
    nias = QM.of({"settings": {"reporting_limit": 0.05, "duplicate_max_reldiff": 20}})
    assert (nias.limits().difference, nias.limits().reporting) == (20.0, 0.05)
    assert QM.of({"mode": "total_ugl", "settings": {"reporting_limit": 0.05}}).limits().reporting == 0.0
    hs = QM.of({"mode": "hs_screening", "hs": {"duplicate_max_reldiff": 12}})
    assert (hs.limits().difference, hs.limits().reporting) == (12.0, 0.0)
    assert hs.default_difference == 30.0 and nias.default_difference == 30.0
    assert hs.edits_key("µg/HS") == "hs_edits:µg/HS"
    assert QM.of(_extraction()).edits_key("mg/kg") == "extraction_edits:mg/kg"
    assert nias.edits_key("mg/kg") == "edits"


def test_report_kind_template_preset_and_named():
    assert QM.REPORT_KINDS == {"hs_screening": "hs_screening", "extraction": "quant"}
    assert [QM.of({"mode": k}).template_preset for k in ("nias_mgkg", "hs_screening", "extraction")] == \
        ["NIAS", "HS-Screening", "Quantification"]
    chosen = QM.named("hs_screening", {"mode": "nias_mgkg", "hs": {"unit": "µg/dm²"}})
    assert chosen.hs and chosen.unit == "µg/dm²"
