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
