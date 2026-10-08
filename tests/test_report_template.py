"""Report templates: model, presets, placeholders and the named store (gcws.report.template)."""
import json

import pytest

from gcws import paths
from gcws.report import template as TP


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    return tmp_path / "data" / "report_templates"


def test_every_preset_is_valid_and_complete():
    for name in TP.PRESETS:
        t = TP.preset(name)
        assert t["format"] == TP.FORMAT and t["name"] == name
        assert TP.validate(t) == []
        assert TP.normalise(t) == t                          # normalised presets are stable
    nias = TP.preset("NIAS")
    assert [c["field"] for c in nias["columns"]] == ["rt", "name", "cas", "score", "conc:mg_dm2", "conc:mg_kg",
                                                     "sml", "ref"]
    assert nias["rows"]["limit"] == {"field": "conc:mg_kg", "value": 0.01, "use_method": False}
    assert nias["extras"]["orientation"] == "landscape" and nias["rows"]["category_sums"]
    assert TP.preset("Empty")["columns"] == []


def test_normalise_fills_defaults_and_drops_rubbish():
    t = TP.normalise({"name": 3, "columns": [{"field": "rt", "decimals": "12", "view": "nope"},
                                            {"header": "no field"}, "junk"],
                      "rows": {"limit": {"value": "0,5"}, "sort": "weird", "unidentified": "x"},
                      "extras": {"orientation": "upside", "notes": "one note", "file_suffix": 'a/b:c'},
                      "unknown": 1})
    assert t["name"] == "3" and "unknown" not in t
    assert t["columns"] == [{"field": "rt", "header": "", "decimals": 8, "view": "mean"}]
    assert t["rows"]["limit"]["value"] is None and t["rows"]["sort"] == "rt"
    assert t["rows"]["unidentified"] == "report"
    assert t["extras"]["orientation"] == "portrait" and t["extras"]["notes"] == ["one note"]
    assert t["extras"]["file_suffix"] == "a_b_c"
    assert t["extras"]["sheets"] == {"determinations": True, "calculation": False, "audit": True}


def test_validate_names_unknown_columns_placeholders_and_mode_gaps():
    t = TP.preset("NIAS")
    t["columns"].append({"field": "nonsense"})
    t["header"]["title"] = "Report {nosuch}"
    problems = TP.validate(t, {"mode": "hs_screening"})
    assert "Unknown column 'nonsense'" in problems
    assert "Unknown placeholder {nosuch}" in problems
    assert any(p.startswith("mg/kg:") for p in problems)      # HS gives no mg/kg


def test_fill_replaces_known_placeholders_only():
    assert TP.fill("PA 26.007, {determination} {x}", {"determination": "double determination"}) == \
        "PA 26.007, double determination {x}"
    assert TP.fill("{operator}|", {"operator": ""}) == "|"


def test_differs_ignores_name_date_and_author():
    a = TP.preset("NIAS")
    b = dict(TP.preset("NIAS"), name="Customer", created="2026-01-01T00:00:00", by="someone")
    assert not TP.differs(a, b)
    b["columns"] = b["columns"][:-1]
    assert TP.differs(a, b)


def test_the_method_copy_is_pure():
    q = {"mode": "nias_mgkg"}
    q2 = TP.applied(q, TP.preset("NIAS"))
    assert TP.QUANT_KEY not in q and TP.of(q2)["name"] == "NIAS"
    assert TP.of(TP.applied(q2, None)) is None
    assert TP.of({}) is None
    assert TP.starter_for({"mode": "hs_screening"}) == "HS-Screening"
    assert TP.starter_for({"mode": "extraction"}) == "Quantification"
    assert TP.starter_for(None) == "NIAS"


def test_named_store_round_trip(store):
    assert TP.names() == []
    t = TP.stamped(TP.preset("NIAS"), "Customer A: v1")
    path = TP.save(t)
    assert path.parent == store and path.name == "Customer A_ v1.json"
    assert TP.names() == ["Customer A: v1"]
    assert TP.load("Customer A: v1") == TP.normalise(t)
    assert t["by"] and t["created"]
    with pytest.raises(KeyError):
        TP.load("other")
    (store / "junk.json").write_text(json.dumps({"format": "something else"}), encoding="utf-8")
    (store / "broken.json").write_text("{", encoding="utf-8")
    assert TP.names() == ["Customer A: v1"]
    with pytest.raises(ValueError):
        TP.read(store / "junk.json")
    with pytest.raises(ValueError):
        TP.save(dict(t, name=" "))
    assert TP.delete("Customer A: v1") and not TP.delete("Customer A: v1")
    assert TP.names() == []


def test_the_processing_method_carries_the_template():
    from gcws.core import proc_method as PM
    tpl = TP.stamped(TP.preset("NIAS"), "Customer A")
    method = {"name": "M", "sections": {"report_template": tpl}}
    assert "report_template" in PM.chosen_sections(method) and "report_template" in PM.WORKSPACE_SECTIONS
    q = PM.plan_quant({"mode": "nias_mgkg"}, method, PM.chosen_sections(method))
    assert TP.of(q)["name"] == "Customer A"
    # an older method without the section keeps the workspace's template; one saved without it removes it
    assert TP.of(PM.plan_quant(q, {"sections": {"quant": {}}}, ["quant"]))["name"] == "Customer A"
    assert TP.of(PM.plan_quant(q, {"sections": {"report_template": None}}, ["report_template"])) is None
    assert "Report template: 'Customer A', 8 columns, made when the method runs" in PM.summary(method)
