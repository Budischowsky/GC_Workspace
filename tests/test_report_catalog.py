"""The report columns: every panel column is offered, with its availability per mode (gcws.report.catalog)."""
from gcws.quant import service as QS
from gcws.report import catalog as C

MODES = list(QS.MODES)


def test_every_peak_table_column_is_offered():
    from gcws.quant.peak_values import VALUES
    from gcws.ui.models.peak_table import COLUMN_KEYS
    assert set(VALUES) == set(COLUMN_KEYS)
    # the peak table's unit columns are concentration fields, its Status is the SML status
    alias = {k: f"conc:{k}" for k in QS.CONC_UNITS} | {"ug_hs": "conc:ug_hs", "mg_m2": "conc:mg_m2",
                                                       "status": "id_status"}
    for key in COLUMN_KEYS:
        assert C.get(alias.get(key, key)) is not None, key


def test_every_double_determination_column_is_offered():
    from gcws.ui.docks.duplicate import COLUMN_KEYS
    # icon and the Report tick are the list's own; A / B / mean are the views of a per-determination field
    concept = {"rt": "rt", "name": "name", "cas": "cas", "area_a": "corr_area", "area_b": "corr_area",
               "conc_a": "conc", "conc_b": "conc", "mean": "conc", "diff": "reldiff", "verdict": "verdict",
               "notes": "notes", "comment": "comment", "feature": "feature", "similarity": "similarity",
               "hit_a": "name", "hit_b": "name"}
    for key in COLUMN_KEYS:
        if key in ("icon", "report"):
            continue
        field = concept.get(key) or "conc:" + key.rsplit("_", 1)[0]
        f = C.get(field)
        assert f is not None, key
        if key.endswith(("_a", "_b")) or key in ("mean",):
            assert f.per_det and "each_mean" in C.views_for(f) or f.kind == "text"


def test_groups_views_and_headers():
    groups = C.by_group()
    assert list(groups) == list(C.GROUPS) and all(groups.values())
    assert C.views_for(C.get("conc:mg_kg")) == ["mean", "each", "each_mean", "merged"]
    assert C.views_for(C.get("name")) == ["mean", "each", "merged"]
    assert C.views_for(C.get("reldiff")) == ["mean"]
    q = {"mode": "hs_screening", "hs": {"unit": "µg/HS"}}
    assert C.header_of(C.get("report_unit_1"), "", q) == "Conc. 1 [µg/dm²]"
    assert C.header_of(C.get("conc"), "", q) == "Conc. µg/HS"
    assert C.header_of(C.get("conc:ug_l"), "My {unit}", {"mode": "nias_mgkg"}) == "My µg/L"
    assert C.unit_of("rt", None) == ""


def test_availability_per_mode():
    def ok(key, quant, **kw):
        return C.availability(C.get(key), C.Info.of_quant(quant, **kw))[0]
    nias, hs = {"mode": "nias_mgkg"}, {"mode": "hs_screening"}
    ext = {"mode": "extraction", "method": {"sample_type": "foil"}}
    assert ok("conc:mg_kg", nias) and not ok("conc:mg_kg", hs) and not ok("conc:mg_kg", ext)
    assert ok("conc:mg_dm2", ext) and ok("conc:mg_m2", hs) and not ok("conc:ug_hs", nias)
    assert ok("rrt", nias) and not ok("rrt", hs)
    assert ok("sml_check", nias) and not ok("sml_check", hs)
    assert not ok("report_unit_2", {"mode": "area_pct"}) and ok("report_unit_2", hs)
    assert not ok("ms_rt", hs)                                  # HS quantifies on the TIC
    assert ok("reldiff", nias) and not ok("reldiff", nias, n=1) and ok("reldiff", nias, n=2)
    assert not ok("feature", nias) and ok("feature", {"mode": "nias_mgkg", "features": {"pairing": "features"}})
    assert not ok("class_hint", nias, has_ms=False) and ok("class_hint", nias)
    assert not ok("sml", nias, has_cas=False)
    ok_, why = C.availability(C.get("conc:mg_kg"), C.Info.of_quant(hs))
    assert not ok_ and "mg/kg" in why
    for mode in MODES:                                          # every mode fills its own concentration
        assert ok("conc", {"mode": mode}) and ok("rt", {"mode": mode}) and ok("report_unit_1", {"mode": mode})
