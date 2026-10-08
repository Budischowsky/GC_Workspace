"""Extraction quantification (quant method): units, standards factor, per-run amounts."""
from types import SimpleNamespace

import pytest

from gcws.quant import extraction as EX
from gcws.quant import service as Q
from gcws.quant import units as U


def _std(code, conc, area, quantify=True):
    return {"code": code, "concentration": conc, "fid_area": area,
            "role": "Quantification" if quantify else "QC"}


def _sample(*areas, standards=None):
    rows = [SimpleNamespace(area=a, derived={"gcws_index": i}) for i, a in enumerate(areas)]
    if standards is None:
        standards = [_std("IS1", 0.8, 1000.0), _std("IS2", 1.0, 3000.0)]
    return SimpleNamespace(rows=rows, standards=standards, meta={})


def _quant(**method):
    return {"mode": "extraction", "method": dict(method)}


def test_every_unit_from_the_mass_and_its_basis():
    b = {"volume_ml": 10.0, "mass_g": 2.0, "area_dm2": 0.5}
    mg = 0.004
    expected = {"mg/mL": 0.0004, "µg/L": 400.0, "mg/g": 0.002, "mg/kg": 2.0, "µg/g": 2.0, "µg/kg": 2000.0,
                "mg/dm²": 0.008, "µg/dm²": 8.0, "mg/m²": 0.8}
    for unit, value in expected.items():
        assert U.convert(mg, unit, b) == pytest.approx(value), unit
    assert U.ratio("µg/dm²", "mg/m²", b) == pytest.approx(0.1)


def test_units_offered_follow_the_sample_type():
    assert U.allowed("solid") == ["mg/mL", "µg/L", "mg/g", "mg/kg", "µg/g", "µg/kg"]
    assert U.allowed("foil") == ["mg/mL", "µg/L", "mg/dm²", "µg/dm²"]
    m = EX.normalise({"sample_type": "foil", "units": ["mg/kg", "µg/dm²"]})
    assert m["units"] == ["µg/dm²", "mg/mL"]          # mg/kg is no foil unit: replaced by the first free one


def test_missing_basis_and_unknown_unit_say_why():
    assert U.convert(1.0, "mg/kg", {"mass_g": None}) is None
    assert "sample mass" in U.missing("mg/kg", {})
    assert U.factor("ppm", {"mass_g": 1}) is None and "Unknown" in U.missing("ppm", {})


def test_mean_factor_of_the_quantifying_standards():
    m = EX.normalise({"spike_ul": 10.0})
    stds = [_std("IS1", 0.8, 1000.0), _std("IS2", 1.0, 3000.0), _std("IS4", 5.0, 10.0, quantify=False)]
    f, text, problem = EX.factor(stds, m, {"use_mean_area": True})
    assert problem == ""
    assert f == pytest.approx(0.9 * 10 / 1000 / 2000.0)    # mean stock × µL ÷ 1000 ÷ mean area
    assert "IS1, IS2" in text
    f, _t, _p = EX.factor(stds, m, {"use_mean_area": False, "reference": "IS2"})
    assert f == pytest.approx(1.0 * 10 / 1000 / 3000.0)
    assert EX.factor([], m)[2]


def test_rows_give_conc1_and_the_other_units_of_a_solid():
    q = _quant(sample_type="solid", amount=2.0, extract_volume_ml=10.0, spike_ul=10.0, units=["mg/kg", "µg/L"])
    sample = _sample(4000.0)
    rows, info = EX.rows(sample, q, "r1", Q.CONC_UNITS)
    f = 0.9 * 10 / 1000 / 2000.0
    mg = 4000.0 * f                                         # 0.018 mg
    r = rows[0]
    assert r["amount_mg"] == pytest.approx(mg)
    assert r["conc"] == pytest.approx(mg * 1000 / 2.0)     # mg/kg
    assert r["ug_l"] == pytest.approx(mg * 1e6 / 10.0)
    assert r["ug_kg"] == pytest.approx(mg * 1e6 / 2.0)
    assert r["mg_dm2"] is None and r["ug_dm2"] is None     # no area units for a solid
    assert "0.018 mg" in r["calc"]["conc"] and "2 g" in r["calc"]["conc"]
    assert info["factor"] == pytest.approx(f)


def test_a_runs_own_amount_replaces_the_methods():
    q = _quant(sample_type="foil", amount=1.0, units=["µg/dm²", "mg/mL"])
    q["method_samples"] = {"r2": {"amount": 0.5}}
    assert EX.basis(q, "r1")["area_dm2"] == 1.0
    assert EX.basis(q, "r2")["area_dm2"] == 0.5
    assert EX.convert_value(q, "r2", 8.0, "mg/dm²") == pytest.approx(0.008)


def test_mode_unit_and_unit_keys_follow_the_method():
    q = _quant(sample_type="foil", units=["mg/dm²", "µg/dm²"])
    assert Q.mode_unit(q) == "mg/dm²"
    assert Q.unit_keys("extraction", q) == ["mg_ml", "ug_l", "mg_dm2", "ug_dm2"]
    conv = Q.from_mode_unit("extraction", None, 0.01, quant=q, run_id="r1")
    assert conv["ug_dm2"] == pytest.approx(10.0)
    assert conv["mg_ml"] == pytest.approx(0.01 * 1.0 / 10.0)     # 1 dm² in 10 mL
    assert conv["mg_kg"] is None


def test_nias_standards_are_the_default():
    stds = EX.nias_standards()
    assert [s["code"] for s in stds] == ["IS1", "IS2", "IS3", "IS4"]
    assert [s["quantify"] for s in stds] == [True, True, True, False]
    assert stds[0]["concentration"] == pytest.approx(0.82)
