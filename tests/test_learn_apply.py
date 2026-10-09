"""Learned rules in the real pipeline: the method section, renaming NIAS rows by learned families."""
F = {"label": "hydrocarbon", "row_name": "Hydrocarbon", "sum_text": "Sum of hydrocarbons", "names": ["Hydrocarbon"],
     "hints": ["Alkane (n- or branched; POSH/MOSH) (high)"], "support": 99, "batches": 6}


def test_learned_rules_method_section():
    from gcws.core import proc_method as PM
    assert "learned_rules" in PM.SECTIONS and "learned_rules" in PM.WORKSPACE_SECTIONS
    q = PM.plan_quant({}, {"sections": {"learned_rules": {"version": 1, "families": [F]}}}, ["learned_rules"])
    assert q["learned_rules"]["families"] == [F]
    gone = PM.plan_quant(q, {"sections": {"learned_rules": None}}, ["learned_rules"])
    assert "learned_rules" not in gone


def _row(row_id, name, cas="630-02-4", rt=20.0):
    import gc_model as M
    return M.PeakRow(row_id=row_id, peak_no=row_id, source="FID+PBM", rt=rt, area=1000.0, name=name, cas=cas)


def _sample(rows, istd_rows=()):
    from types import SimpleNamespace
    return SimpleNamespace(rows=rows, standards=[{"code": "IS1", "row_id": r} for r in istd_rows])


def test_apply_families_renames_with_reason():
    from gcws.learn.apply import apply_families
    rows = [_row(1, "Octacosane"), _row(2, "Tetracosane", "646-31-1"), _row(3, "Butyl methacrylate", "97-88-1"),
            _row(4, "Eicosane"), _row(5, "Perdeutero-Heptadecane", "")]
    evidence = {1: {"name": "Hydrocarbon", "hint": "", "manual": False},
                2: {"name": "Tetracosane", "hint": "Alkane (n- or branched; POSH/MOSH) (high)", "manual": False},
                3: {"name": "Butyl methacrylate", "hint": "", "manual": False},
                4: {"name": "Hydrocarbon", "hint": "", "manual": True},
                5: {"name": "Hydrocarbon", "hint": "", "manual": False}}
    n = apply_families(_sample(rows, istd_rows=[5]), evidence, [F])
    assert n == 2
    assert (rows[0].name, rows[0].cas) == ("Hydrocarbon", "0")
    assert rows[0].derived["learned"] == ("family v1 (hydrocarbon): name 'Hydrocarbon' -> 'Hydrocarbon'; "
                                          "was 'Octacosane' (630-02-4)")
    assert rows[1].name == "Hydrocarbon" and "hint 'Alkane (n- or branched; POSH/MOSH) (high)'" in rows[1].derived["learned"]
    assert rows[2].name == "Butyl methacrylate" and "learned" not in rows[2].derived
    assert rows[3].name == "Eicosane" and rows[4].name == "Perdeutero-Heptadecane"


def test_renamed_rows_reach_the_category_sums():
    from gcws.report.legacy_api import main_script
    main = main_script()
    assert main.classify_name("Hydrocarbon") == "hydrocarbon"
    assert main.classify_name("Styrene Oligomer") == "styrene"


def test_learned_reason_reaches_the_peak_table_and_report_columns():
    from gcws.quant.peak_values import VALUES
    from gcws.report import catalog as C
    from gcws.ui.models.peak_table import COLUMN_KEYS
    assert "learned" in VALUES and "learned" in COLUMN_KEYS and C.get("learned") is not None


def test_rows_for_carries_the_reason():
    from types import SimpleNamespace
    import gc_model as M
    from gcws.quant.service import rows_for
    row = M.PeakRow(row_id=1, peak_no=1, source="FID+PBM", rt=20.0, area=1000.0, name="Hydrocarbon", cas="0")
    row.derived.update(gcws_index=0, learned="family v1 (hydrocarbon): ...")
    sample = SimpleNamespace(rows=[row], standards=[], meta={})
    st = SimpleNamespace(id="r", results={})
    out = rows_for(sample, st, "area_pct", {"detector": "FID"}, None, [], {})
    assert out[0]["learned"] == "family v1 (hydrocarbon): ..."
