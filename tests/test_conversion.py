"""A replicate row's concentration in other units, in every quantification mode (gcws.quant.conversion)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from gcws.quant import conversion as CV
from gcws.quant import nias_bridge as NB
from gcws.quant import service as QS


def _settings(**values):
    s = NB.make_settings()
    for key, value in values.items():
        setattr(s, key, value)
    return s


def _ws(*names):
    runs = {f"r{k}": SimpleNamespace(run=SimpleNamespace(path=Path(n))) for k, n in enumerate(names)}
    return SimpleNamespace(runs=runs)


def test_labels_are_the_replicate_letters_or_a_b_c():
    ws = _ws("07_GIOSUN_A.D", "08_GIOSUN_B.D")
    assert CV.labels(ws, ["r0", "r1"]) == ["A", "B"]
    ws = _ws("07_GIOSUN_1.D", "08_GIOSUN_1.D", "09_other.D")
    assert CV.labels(ws, ["r0", "r1", "r2"]) == ["1", "B", "C"]
    assert CV.letter(26) == "27"


def test_nias_ratios_are_constant_multiples_of_mg_per_kg():
    q = {"mode": "nias_mgkg"}
    s = _settings(cell_area_dm2=0.51, coverage=1.0, extract_volume_ml=10.0, ov_ratio=6.0)
    ratio = CV.ratio_fn(q, s, ["a", "b"])
    assert ratio(0, "mg/kg") == 1.0
    assert ratio(1, "mg/dm²") == pytest.approx(1 / 6)
    assert ratio(0, "µg/dm²") == pytest.approx(1000 / 6)
    assert ratio(0, "µg/L") == pytest.approx(1 / 6 * 0.51 * 1e6 / 10)
    assert ratio(0, "µg/HS") is None and ratio(0, "%") is None
    assert CV.report_units(q) == ["mg/kg", "mg/dm²"]
    assert CV.available_units(q)[:3] == ["mg/kg", "mg/dm²", "µg/dm²"]


def test_total_extraction_and_the_single_unit_modes():
    ratio = CV.ratio_fn({"mode": "total_ugl"}, _settings(), ["a"])
    assert ratio(0, "mg/L") == pytest.approx(1e-3) and ratio(0, "mg/dm²") is None
    q = {"mode": "istd_conc", "unit": "ng/mL"}
    assert CV.available_units(q) == ["ng/mL"] and CV.report_units(q) == ["ng/mL"]
    assert CV.ratio_fn(q, _settings(), ["a"])(0, "ng/mL") == 1.0
    assert CV.available_units({"mode": "area_pct"}) == ["%"]


def test_extraction_converts_each_determination_with_its_own_amount():
    q = {"mode": "extraction", "method": {"sample_type": "solid", "amount": 1.0, "extract_volume_ml": 10.0,
                                          "units": ["mg/kg", "µg/L"]},
         "method_samples": {"b": {"amount": 2.0}}}
    ratio = CV.ratio_fn(q, None, ["a", "b"])
    assert ratio(0, "mg/kg") == 1.0
    # 1 mg/kg of 1 g = 0.001 mg in 10 mL = 100 µg/L; of 2 g = 200 µg/L
    assert ratio(0, "µg/L") == pytest.approx(100.0)
    assert ratio(1, "µg/L") == pytest.approx(200.0)
    assert ratio(0, "mg/dm²") is None
    row = {"c1": 1.0, "c2": 3.0, "mean": 2.0, "cs": [9.0, 9.0]}
    each, mean = CV.in_unit(row, 2, "µg/L", ratio)
    assert each == pytest.approx([100.0, 600.0])
    assert mean == pytest.approx(350.0)                      # the mean of the converted values
    row["dismissed"] = 2
    each, mean = CV.in_unit(dict(row, mean=1.0), 2, "µg/L", ratio)
    assert mean == pytest.approx(100.0)                      # B dismissed: A alone
    assert CV.report_units(q) == ["mg/kg", "µg/L"]


def test_hs_ratios_use_each_runs_inputs():
    q = {"mode": "hs_screening", "hs": {"unit": "µg/HS", "samples": {"a": {"area_dm2": 0.5}, "b": {"area_dm2": 0.25}}}}
    ratio = CV.ratio_fn(q, None, ["a", "b"])
    assert ratio(0, "µg/dm²") == pytest.approx(2.0) and ratio(1, "µg/dm²") == pytest.approx(4.0)
    assert ratio(0, "mg/m²") == pytest.approx(0.2)
    assert ratio(0, "µg/g") is None                          # no mass entered
    assert CV.report_units(q) == ["µg/dm²", "mg/m²"]
    assert CV.available_units(q) == ["µg/HS", "µg/dm²", "µg/g", "mg/m²"]


def test_edited_values_count_and_n_fold_rows_use_their_list():
    ratio = CV.ratio_fn({"mode": "nias_mgkg"}, _settings(ov_ratio=6.0), ["a", "b", "c"])
    edited = {"c1": 6.0, "c2": 12.0, "mean": 9.0, "cs": [1.0, 1.0]}
    assert CV.det_values(edited, 2) == ([6.0, 12.0], 9.0, 0)
    each, mean = CV.in_unit(edited, 2, "mg/dm²", ratio)
    assert each == pytest.approx([1.0, 2.0]) and mean == pytest.approx(1.5)
    nfold = {"cs": [6.0, None, 18.0], "mean": 12.0}
    each, mean = CV.in_unit(nfold, 3, "mg/dm²", ratio)
    assert each[1] is None and each[2] == pytest.approx(3.0) and mean == pytest.approx(2.0)
    assert CV.det_values({"c1": 4.0, "mean": 4.0}, 1) == ([4.0], 4.0, 0)


def test_unit_cells_match_the_double_determination_columns():
    s = _settings(cell_area_dm2=0.51, coverage=1.0, extract_volume_ml=10.0, ov_ratio=6.0)
    q = {"mode": "nias_mgkg"}
    row = {"c1": 6.0, "c2": 12.0, "mean": 9.0}
    cells = CV.unit_cells(q, s, row, ["a", "b"], QS.unit_keys("nias_mgkg"), ["A", "B"])
    assert len(cells) == len(QS.CONC_UNITS) * 3
    keys = [f"{u}_{side}" for u in QS.CONC_UNITS for side, _f in CV.UNIT_SIDES]
    got = dict(zip(keys, cells))
    assert got["mg_dm2_a"][0] == pytest.approx(1.0) and got["mg_dm2_mean"][0] == pytest.approx(1.5)
    assert "surface/volume" in got["mg_dm2_b"][1]
    assert got["mg_g_a"] == (None, None)                      # not a NIAS unit
