"""NIAS concentrations in further units (µg/dm², µg/L, mg/L, mg/mL) and how each is calculated."""
from types import SimpleNamespace

import pytest

from gcws.quant import nias_bridge as NB
from gcws.quant import service as Q


def _settings(**values):
    s = NB.make_settings()
    for key, value in values.items():
        setattr(s, key, value)
    return s


def test_nias_units_follow_mg_dm2_and_the_extract_volume():
    s = _settings(cell_area_dm2=0.51, coverage=1.0, extract_volume_ml=10.0, ov_ratio=6.0)
    v = Q.unit_values("nias_mgkg", s, corr_area=1000.0, mg_dm2=0.01, conc=0.06)
    assert v["mg_dm2"] == pytest.approx(0.01)
    assert v["ug_dm2"] == pytest.approx(10.0)
    assert v["ug_l"] == pytest.approx(0.01 * 0.51 * 1.0 * 1e6 / 10.0)        # 5.1 µg in 10 mL
    assert v["mg_l"] == pytest.approx(0.51)
    assert v["mg_ml"] == pytest.approx(0.00051)
    calc = v["calc"]
    assert "10 mL" in calc["ug_l"] and "0.51 dm²" in calc["ug_l"] and "510" in calc["ug_l"]
    assert "1000" in calc["mg_dm2"] and "0.01" in calc["mg_dm2"]
    assert "6 dm²/kg" in calc["conc"]


def test_without_an_extract_volume_the_liquid_units_stay_empty_and_say_why():
    s = _settings(cell_area_dm2=0.51, coverage=1.0, extract_volume_ml=0.0)
    v = Q.unit_values("nias_mgkg", s, corr_area=1000.0, mg_dm2=0.01, conc=0.06)
    assert v["ug_dm2"] == pytest.approx(10.0)
    assert v["ug_l"] is None and v["mg_l"] is None and v["mg_ml"] is None
    assert "extract volume" in v["calc"]["ug_l"].lower()


def test_without_an_istd_factor_no_unit_is_computed():
    v = Q.unit_values("nias_mgkg", _settings(), corr_area=1000.0, mg_dm2=None, conc=None)
    assert all(v[k] is None for k in Q.CONC_UNITS)
    assert "istd" in v["calc"]["ug_l"].lower()


def test_total_extraction_converts_its_ug_per_l():
    v = Q.unit_values("total_ugl", _settings(), corr_area=2000.0, conc=629.3, mean_area=4000.0, c_istd=1258.6)
    assert v["ug_l"] == pytest.approx(629.3)
    assert v["mg_l"] == pytest.approx(0.6293)
    assert v["mg_ml"] == pytest.approx(0.0006293)
    assert v["mg_dm2"] is None and v["ug_dm2"] is None
    assert "4000" in v["calc"]["conc"] and "1258.6" in v["calc"]["conc"]


def test_other_modes_add_nothing():
    v = Q.unit_values("area_pct", _settings(), corr_area=1.0, conc=3.0)
    assert all(v[k] is None for k in Q.CONC_UNITS) and not v["calc"]


def test_quant_rows_carry_the_units_and_their_calculation():
    row = SimpleNamespace(row_id=1, area=1000.0, ri=None,
                          derived={"gcws_index": 0, "mg_dm2": 0.01, "mg_kg": 0.06})
    sample = SimpleNamespace(rows=[row], standards=[], meta={})
    s = _settings(cell_area_dm2=0.51, coverage=1.0, extract_volume_ml=10.0)
    out = Q.rows_for(sample, SimpleNamespace(results={}), "nias_mgkg", {}, s, None, {})
    assert out[0]["conc"] == pytest.approx(0.06)
    assert out[0]["ug_l"] == pytest.approx(510.0)
    assert out[0]["mg_ml"] == pytest.approx(0.00051)
    assert "510" in out[0]["calc"]["ug_l"]


def test_peak_table_lists_the_unit_columns_hidden_with_their_calculation(qapp):
    from PySide6.QtCore import Qt
    from gcws.ui.docks.peak_table import FILTER_COLUMNS
    from gcws.ui.models.peak_table import COLUMN_KEYS, COLUMNS, PeakTableModel, Row
    for key in ("ug_dm2", "ug_l", "mg_l", "mg_ml"):
        assert key in COLUMN_KEYS and not COLUMNS[COLUMN_KEYS.index(key)].default
        assert key in [k for k, _ in FILTER_COLUMNS]
    model = PeakTableModel(SimpleNamespace(selected=-1))
    peak = SimpleNamespace(number=1, apex_rt=10.0, flags="", negative=False, extra={})
    model.rows = [Row(0, peak, None, {"ug_l": 510.0, "calc": {"ug_l": "µg/L = ... = 510"}})]
    col = COLUMN_KEYS.index("ug_l")
    assert model.data(model.index(0, col)) == "510.00"
    assert model.data(model.index(0, col), Qt.ToolTipRole) == "µg/L = ... = 510"


def test_peak_table_colours_a_peak_found_in_the_blank(qapp):
    """The In blank cell is coloured by the blank match's level (it raised NameError before)."""
    from PySide6.QtCore import Qt
    from gcws.quant.blank_match import BlankMatch
    from gcws.ui import theme
    from gcws.ui.models.peak_table import COLUMN_KEYS, PeakTableModel, Row
    match = BlankMatch(0, "b1", 3, 0.001, 900.0, 800.0, 1.1, 0.95, "blank")
    ws = SimpleNamespace(selected=-1, active_id="s1", signal_key="FID", runs={"s1": object()},
                         blank_matches=lambda run_id, key: {0: match} if (run_id, key) == ("s1", "FID") else {})
    model = PeakTableModel(ws)
    peak = SimpleNamespace(number=1, apex_rt=10.0, flags="", negative=False, extra={})
    model.rows = [Row(0, peak, None, {}), Row(1, peak, None, {})]
    col = COLUMN_KEYS.index("in_blank")
    assert model.data(model.index(0, col)) == match.text
    assert model.data(model.index(0, col), Qt.BackgroundRole).color() == theme.status_brush("bad").color()
    assert model.data(model.index(1, col), Qt.BackgroundRole) is None


def test_a_value_in_the_mode_unit_converts_like_the_peak_rows():
    """The double determination converts its values (mg/kg, analyst edits included) the same way."""
    s = _settings(cell_area_dm2=0.51, coverage=1.0, extract_volume_ml=10.0, ov_ratio=6.0)
    v = Q.from_mode_unit("nias_mgkg", s, 0.06)
    row = Q.unit_values("nias_mgkg", s, mg_dm2=0.01, conc=0.06)
    for key in Q.CONC_UNITS:
        assert v[key] == pytest.approx(row[key])
    assert "6 dm²/kg" in v["calc"]["mg_dm2"]
    assert Q.from_mode_unit("total_ugl", s, 629.3)["mg_l"] == pytest.approx(0.6293)
    assert all(x is None for k, x in Q.from_mode_unit("nias_mgkg", s, None).items() if k != "calc")
    assert Q.from_mode_unit("nias_mgkg", _settings(ov_ratio=0.0), 0.06)["ug_l"] is None


def test_units_offered_per_mode():
    assert Q.unit_keys("nias_mgkg") == Q.NIAS_UNITS
    assert Q.unit_keys("total_ugl") == ["ug_l", "mg_l", "mg_ml"]
    assert Q.unit_keys("area_pct") == [] and Q.unit_keys("hs_screening") == []
