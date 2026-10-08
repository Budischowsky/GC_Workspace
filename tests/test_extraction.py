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


def test_named_quant_methods_round_trip(tmp_path, monkeypatch):
    from gcws import paths
    from gcws.quant import qmethod as QM
    monkeypatch.setattr(paths, "DATA", tmp_path)
    q = _quant(sample_type="foil", amount=0.6, units=["µg/dm²", "mg/dm²"])
    q["istd_defs"] = [{"code": "IS1", "name": "C17-d36", "concentration": 0.5, "quantify": True, "target_rt": 13.4}]
    q["method_samples"] = {"r1": {"amount": 0.2}}
    data = QM.collect(q, "Foil 0.6 dm²: ethanol")
    QM.save(data)
    assert QM.names() == ["Foil 0.6 dm²: ethanol"]
    back = QM.applied({"mode": "nias_mgkg", "method_samples": {"r9": {"amount": 1}}}, QM.load("Foil 0.6 dm²: ethanol"))
    assert back["mode"] == "extraction"
    assert back["method"]["amount"] == 0.6 and back["method"]["units"] == ["µg/dm²", "mg/dm²"]
    assert back["istd_defs"][0]["concentration"] == 0.5
    assert back["method_samples"] == {"r9": {"amount": 1}}        # a run's amount is never part of a method
    assert "method_samples" not in data
    assert QM.delete("Foil 0.6 dm²: ethanol") and QM.names() == []


def test_processing_methods_carry_the_quant_method():
    from gcws.core import proc_method as PM
    method = {"sections": {"quant": {"mode": "extraction", "method": {"amount": 3.0}}}}
    q = PM.plan_quant({"mode": "nias_mgkg", "method_samples": {"r": {"amount": 1}}}, method, ["quant"])
    assert q["method"] == {"amount": 3.0} and q["mode"] == "extraction"
    assert q["method_samples"] == {"r": {"amount": 1}}


def test_quant_method_editor_in_the_panel(qtbot, tmp_path, monkeypatch):
    """Extraction mode shows the method editor instead of the NIAS parameters; edits are undoable;
    the units follow the sample type; a method is saved, changed and chosen again by name."""
    from gcws import paths
    from gcws.ui.docks.quant import QuantDock
    from gcws.ui.workspace import Workspace
    monkeypatch.setattr(paths, "DATA", tmp_path)
    ws = Workspace()
    dock = QuantDock(ws)
    qtbot.addWidget(dock)
    dock.mode.setCurrentIndex(dock.mode.findData("extraction"))
    dock._mode_changed()
    panel = dock.method_panel
    assert ws.quant["mode"] == "extraction"
    assert not panel.isHidden() and dock.nias_params.isHidden()
    assert [panel.unit1.itemText(i) for i in range(panel.unit1.count())] == U.allowed("solid")
    panel.sample_type.setCurrentIndex(panel.sample_type.findData("foil"))
    panel.apply()
    m = ws.quant["method"]
    assert m["sample_type"] == "foil" and m["units"] == ["mg/dm²", "µg/L"]      # mg/kg became mg/dm²
    assert [panel.unit1.itemText(i) for i in range(panel.unit1.count())] == U.allowed("foil")
    panel.volume.setValue(20.0)
    panel.apply()
    assert ws.quant["method"]["extract_volume_ml"] == 20.0
    ws.project_undo.undo()
    assert ws.quant["method"]["extract_volume_ml"] == 10.0
    assert panel.save_as("Foil") == "Foil"
    assert ws.quant["method"]["name"] == "Foil" and not panel.is_modified()
    panel.refresh()
    panel.spike.setValue(25.0)
    panel.apply()
    assert panel.is_modified() and "Changed" in panel.modified.text()
    panel._chosen(panel.methods.findData("Foil"))
    assert ws.quant["method"]["spike_ul"] == 10.0
    assert panel.delete(confirm=False) and ws.quant["method"]["name"] == ""


def test_mean_in_another_unit_uses_each_runs_amount():
    q = _quant(sample_type="solid", amount=1.0, units=["mg/kg", "µg/L"])
    q["method_samples"] = {"b": {"amount": 2.0}}
    # A: 10 mg/kg of 1 g = 0.01 mg -> 1000 µg/L in 10 mL; B: 6 mg/kg of 2 g = 0.012 mg -> 1200 µg/L
    assert EX.mean_in(q, ["a", "b"], [10.0, 6.0], 8.0, "µg/L") == pytest.approx(1100.0)
    assert EX.mean_in(q, ["a", "b"], [10.0, 6.0], 6.0, "µg/L", dismissed=1) == pytest.approx(1200.0)
    assert EX.mean_in(q, ["a"], [10.0], 10.0, "mg/g") == pytest.approx(0.01)
    assert EX.mean_in(q, ["a", "b"], [10.0, 6.0], None, "µg/L") is None


def test_edits_are_kept_per_unit_for_the_extraction_method():
    from gcws.quant import duplicate_view as DV
    assert DV.edits_key({"mode": "extraction"}, "mg/kg") == "extraction_edits:mg/kg"
    assert DV.edits_key({"mode": "hs_screening"}, "µg/HS") == "hs_edits:µg/HS"
    assert DV.edits_key({"mode": "nias_mgkg"}, "mg/kg") == "edits"
