"""P56: automatic ISTD detection - by library name, learned spectrum and retention time (with a
common run shift), one peak per standard, bound in one undo step."""
import copy

import pytest

from test_report import _ws_with, qapp  # noqa: F401  (fixture)

DEFS = [{"code": "IS1", "name": "Perdeutero-Heptadecane", "target_rt": 13.444, "quantify": True},
        {"code": "IS2", "name": "Benzyl-butyl-phthalate-d4", "target_rt": 18.954, "quantify": True},
        {"code": "IS4", "name": "Dibutyl phthalate-3,4,5,6-d4", "target_rt": 15.967, "quantify": False}]
D4 = {153: 999, 154: 90, 76: 60, 41: 120, 57: 200}          # labelled phthalate: base peak 153
NATIVE = {149: 999, 150: 90, 76: 60, 41: 120, 57: 200}      # native phthalate: base peak 149


def _peaks(shift=0.0, names=True):
    from gcws.quant.istd_detect import PeakInfo
    hits = lambda *h: list(h) if names else []
    return [PeakInfo(0, 13.444 + shift, 9e6, hits(("Perdeutero-Heptadecane", "", 97))),
            PeakInfo(1, 13.52 + shift, 2e6, hits(("Heptadecane", "629-78-7", 90))),
            PeakInfo(2, 15.95 + shift, 8e5, hits(("Dibutyl phthalate", "84-74-2", 93)), dict(NATIVE)),
            PeakInfo(3, 15.967 + shift, 9e5, hits(("Benzenamine, 3,5-dimethoxy-", "", 50)), dict(D4)),
            PeakInfo(4, 18.954 + shift, 4.5e6, hits(("Benzyl-butyl-phthalate-d4", "", 91))),
            PeakInfo(5, 19.02 + shift, 3e6, hits(("Octadecenamide", "", 60)))]


def test_labelled_names_never_match_the_native_compound():
    from gcws.quant.istd_detect import labelled, name_similarity
    assert labelled("Dibutyl phthalate-3,4,5,6-d4") and labelled("Perdeutero-Heptadecane")
    assert not labelled("Dibutyl phthalate") and not labelled("D-Limonene")
    assert name_similarity("Benzyl-butyl-phthalate-d4", "Benzyl butyl phthalate-d4") > 0.9
    assert name_similarity("Dibutyl phthalate-3,4,5,6-d4", "Dibutyl phthalate") < 0.3


def test_found_by_name_and_rt_with_a_run_shift():
    from gcws.quant.istd_detect import detect_core
    res = detect_core(DEFS, {}, _peaks(shift=0.30))
    assert res.shift == pytest.approx(0.30, abs=1e-6) and res.anchors == 2
    assert res.best["IS1"].peak_index == 0 and res.best["IS1"].confidence == "high"
    assert res.best["IS2"].peak_index == 4 and res.best["IS2"].confidence == "high"
    assert "run shift +0.300" in " ".join(res.best["IS2"].evidence)
    # DBP-d4: no library name, the native DBP next to it does not count; RT alone is not certain
    assert res.best["IS4"].confidence != "high"
    assert len({c.peak_index for c in res.best.values() if c}) == 3                   # one peak each


def test_spectrum_tells_labelled_from_native():
    from gcws.quant.istd_detect import detect_core
    refs = {"IS4": {"mz": list(D4), "ab": list(D4.values())}}
    res = detect_core(DEFS, refs, _peaks(names=False))
    assert res.best["IS4"].peak_index == 3 and res.best["IS4"].confidence == "high"
    assert res.best["IS4"].spec_score > 0.9
    # the native compound's spectrum does not match
    assert next(c for c in res.ranked["IS4"] if c.peak_index == 2).spec_score == 0.0


def test_no_evidence_is_low_confidence():
    from gcws.quant.istd_detect import PeakInfo, detect_core
    res = detect_core(DEFS[:1], {}, [PeakInfo(0, 13.0, 1e6), PeakInfo(1, 13.1, 1e6)])
    assert res.best["IS1"] is None or res.best["IS1"].confidence == "low"


@pytest.fixture(scope="module")
def ws(samples, qapp):
    return _ws_with(samples, ["06_", "07_", "08_", "11_"], qapp)


def test_real_runs(ws):
    from gcws.quant import istd_detect as ID
    s07, s11 = [s for s in ws.states() if s.role == "sample"]
    engine = {s["code"]: s["fid_rt"] for s in ws.nias_sample(s07.id).standards}
    res = ID.detect(ws, s07.id)
    for code in ("IS1", "IS2", "IS3"):
        assert res.best[code].confidence == "high"
        assert res.best[code].rt == pytest.approx(engine[code], abs=0.02)
    # target RTs 0.25 min off (a shortened column): the same peaks
    q = copy.deepcopy(ws.quant)
    defs = ID.definitions(ws)
    for d in defs:
        d["target_rt"] = round(d["target_rt"] + 0.25, 3)
    old, ws.quant = ws.quant, dict(q, istd_defs=defs)
    try:
        shifted = ID.detect(ws, s07.id)
        assert shifted.shift == pytest.approx(res.shift - 0.25, abs=0.01)
        assert {c: k.peak_index for c, k in shifted.best.items() if k} == \
            {c: k.peak_index for c, k in res.best.items() if k}
    finally:
        ws.quant = old
    # spectra learned on 07 find every standard on 11, even without any library name
    refs = {d["code"]: ID.learn_reference(ws, s07.id, d["code"]) for d in ID.definitions(ws)}
    assert all(r and r["mz"] for r in refs.values())
    saved = {st.id: {k: copy.deepcopy(v.items) for k, v in st.idents.items()} for st in ws.states()}
    ws.quant["istd_refs"] = refs
    try:
        for st in ws.states():
            for iset in st.idents.values():
                iset.items = []
        found = ID.detect(ws, s11.id)
        assert all(k is not None and k.confidence == "high" for k in found.best.values())
        # BBP-d4 of run 11 is at the RT of run 07, not where a name-less fallback would take it
        assert found.best["IS2"].rt == pytest.approx(res.best["IS2"].rt, abs=0.01)
    finally:
        ws.quant.pop("istd_refs", None)
        for st in ws.states():
            for k, items in saved[st.id].items():
                st.idents[k].items = items


def test_dialog_binds_in_one_undo_step(qtbot, ws):
    from gcws.quant import istd_detect as ID
    from gcws.ui.dialogs.istd_detect import DetectIstdDialog
    s07 = [s for s in ws.states() if s.role == "sample"][0]
    ws.set_active(s07.id)
    before = copy.deepcopy(ws.quant.get("istd_bindings") or {})
    dlg = DetectIstdDialog(ws)
    qtbot.addWidget(dlg)
    chosen = dlg.chosen()
    assert set(chosen[s07.id]) >= {"IS1", "IS2", "IS3"}            # high confidence is checked
    dlg.update_targets.setChecked(True)
    dlg.apply()
    assert ws.quant["istd_bindings"][s07.id]["IS2"] == pytest.approx(chosen[s07.id]["IS2"], abs=1e-4)
    moved = {d["code"]: d["target_rt"] for d in ws.quant["istd_defs"]}
    assert moved["IS1"] == pytest.approx(chosen[s07.id]["IS1"], abs=1e-3)
    assert ID.bound_rt(ws, s07.id, "IS2") == pytest.approx(chosen[s07.id]["IS2"], abs=0.02)
    stack = ws.undo_group.activeStack() or ws.project_undo
    stack.undo()
    assert (ws.quant.get("istd_bindings") or {}) == before


def test_quant_panel_learns_a_spectrum(qtbot, ws):
    from gcws.ui.docks.quant import QuantDock
    s07 = [s for s in ws.states() if s.role == "sample"][0]
    ws.set_active(s07.id)
    dock = QuantDock(ws)
    qtbot.addWidget(dock)
    ref = dock.learn_spectrum("IS1")
    assert ref is not None and ws.quant["istd_refs"]["IS1"]["learned_from"] == s07.name
    assert "IS1" in dock.refs_note.text()
    stack = ws.undo_group.activeStack() or ws.project_undo
    stack.undo()
    assert "IS1" not in (ws.quant.get("istd_refs") or {})
    from gcws.core import proc_method as PM
    assert "istd_refs" in PM.QUANT_KEYS
