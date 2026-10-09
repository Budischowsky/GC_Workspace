"""gcws.learn.families: families learned from the analysts' group labels (no name lists in code)."""


def _run(batch, rows, footnotes=()):
    """rows: (analyst decision, analyst label, program name, program class hint)"""
    from gcws.learn.match import HumanItem, Pair
    from gcws.learn.rules import CachedRun
    from gcws.learn.runner import ProgramResult
    items, peaks, pairs = [], [], []
    for i, (decision, label, name, hint) in enumerate(rows):
        rt = 5.0 + i
        items.append(HumanItem(rt=rt, area=1.0, label=label, cas="", row_class="", decision=decision, source="final"))
        peaks.append({"rt": rt, "name": name, "class_hint": hint, "area": 1.0})
        pairs.append(Pair(i, i, "exact", decision, 0.0))
    prog = ProgramResult(run_dir=batch, method="M", state="control", peaks=peaks)
    return CachedRun(batch, items, prog, pairs, footnotes=list(footnotes))


HC = ("reported_group", "Hydrocarbon", "Hydrocarbon", "Alkane (n- or branched)")


def test_family_learned_across_batches():
    from gcws.learn.families import learn_families
    runs = [_run("B1", [HC] * 3, ["Sum of hydrocarbons (alkanes, cyclic alkanes; estimated)**"]),
            _run("B2", [HC] * 3)]
    (fam,) = learn_families(runs)
    assert fam["label"] == "hydrocarbon" and fam["names"] == ["Hydrocarbon"]
    assert fam["hints"] == ["Alkane (n- or branched)"] and fam["support"] == 6 and fam["batches"] == 2
    assert fam["sum_text"] == "Sum of hydrocarbons (alkanes, cyclic alkanes; estimated)**"


def test_one_batch_is_not_a_family():
    from gcws.learn.families import learn_families
    assert learn_families([_run("B1", [HC] * 6)]) == []


def test_shared_evidence_below_min_share_is_not_a_member():
    from gcws.learn.families import learn_families
    named = ("reported_named", "Hexadecane", "Hydrocarbon", "Alkane (n- or branched)")
    runs = [_run("B1", [HC] * 3 + [named] * 3), _run("B2", [HC] * 3 + [named] * 3)]
    assert learn_families(runs) == []
    (fam,) = learn_families(runs, min_share=0.5)
    assert fam["names"] == ["Hydrocarbon"] and "Sum of hydrocarbon" == fam["sum_text"]


def test_family_of_prefers_more_support():
    from gcws.learn.families import family_of
    fams = [{"label": "a", "names": ["X"], "hints": [], "support": 5, "sum_text": "Sum of a"},
            {"label": "b", "names": [], "hints": ["H"], "support": 9, "sum_text": "Sum of b"}]
    assert family_of({"name": "X", "class_hint": "H"}, fams)["label"] == "b"
    assert family_of({"name": "X", "class_hint": "other"}, fams)["label"] == "a"
    assert family_of({"name": "Y", "class_hint": "other"}, fams) is None
    fams[1]["support"] = 5
    assert family_of({"name": "X", "class_hint": "H"}, fams)["label"] == "a"


def test_simulate_with_family_gives_one_sum_line():
    from gcws.learn.runner import ProgramResult
    from gcws.learn.simulate import simulate_report
    peaks = [{"rt": 5.0 + i, "area": 2000.0, "name": "Hydrocarbon", "cas": "", "score": 90, "istd": "",
              "in_blank": "", "blank_ratio": None, "class_hint": "Alkane"} for i in range(3)]
    prog = ProgramResult(run_dir="r", method="M", state="c", peaks=peaks, reported=[{"rt": 5.0, "mean_mgkg": 0.02}])
    fam = {"label": "hydrocarbon", "names": ["Hydrocarbon"], "hints": [], "support": 6, "sum_text": "Sum of hydrocarbons"}
    lines = simulate_report(prog, {"min_score": 80.0, "unknowns": "report"}, [fam])
    assert lines == [{"rt": None, "name": "Sum of hydrocarbons", "cas": "", "kind": "sum"}]


def test_unreported_group_rows_count_for_the_family():
    """Analysts put the group label on peaks below the limit too; they are family evidence as well."""
    from gcws.learn.families import learn_families
    from gcws.learn.match import HumanItem
    low = ("kept_unreported", "Hydrocarbon", "Hydrocarbon", "Alkane (n- or branched)")
    runs = [_run("B1", [HC] * 2 + [low] * 2), _run("B2", [HC] * 2 + [low] * 2)]
    for r in runs:
        for it in r.items:
            it.row_class = "group"
    (fam,) = learn_families(runs, min_peaks=5)
    assert fam["names"] == ["Hydrocarbon"] and fam["support"] == 8
