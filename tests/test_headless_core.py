"""P53: processing without the main window - methods applied to one workspace only, the blocking
library search, applying search hits and the replicate-group suggestion."""
import pytest

from conftest import run_dir


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _files(folder):
    return {p.relative_to(folder): p.stat().st_mtime_ns for p in folder.rglob("*") if p.is_file()}


def _workspace(prefixes):
    from gcws.io.run_loader import load_run
    from gcws.ui.workspace import Workspace
    ws = Workspace()
    for prefix in prefixes:
        ws.add_run(load_run(run_dir(prefix)))
    return ws


def test_method_applied_to_one_workspace_writes_nothing(qapp, samples):
    from PySide6.QtCore import QSettings
    from gcws import paths
    from gcws.core import proc_method as PM
    from gcws.integration.method import nias_fid_method
    from gcws.io.run_loader import load_run
    from gcws.ui.workspace import Workspace
    fid = nias_fid_method()
    fid.name = "P53 headless FID"
    fid.area_reject = 123.0
    method = {"name": "P53 headless", "sections": {
        "integration": {"FID": fid.to_dict()},
        "quant": {"mode": "nias_mgkg", "unit": "mg/L", "detector": "FID", "ms_solvent": {"enabled": False, "end": 0.0}},
        "migration": {"simulant": "Ethanol 95 %", "temperature": "60 °C", "duration": "10 d"},
        "search": {"method": {"name": "should not be saved"}, "target": "FID", "transfer": False}}}
    before, keys = _files(paths.DATA), set(QSettings().allKeys())
    ws = Workspace()
    applied = PM.apply_to_workspace(ws, method, persist=False)
    assert applied == ["integration", "quant", "migration"]            # the search is not a workspace part
    assert ws.quant["migration"]["simulant"] == "Ethanol 95 %" and ws.quant["unit"] == "mg/L"
    assert ws.default_methods["FID"].name == "P53 headless FID"
    st = ws.add_run(load_run(run_dir("07_")))
    assert st.methods["FID"].name == "P53 headless FID" and st.methods["FID"].area_reject == 123.0
    assert ws.method_for(st, "TIC").name == ws.methods.default_name("TIC")
    assert _files(paths.DATA) == before and set(QSettings().allKeys()) == keys
    assert any(r.action == "Processing method loaded" for r in ws.audit.records)
    ws.project_undo.undo()                                               # one step
    assert "migration" not in ws.quant


def test_search_config_from_method():
    from gcws.core import proc_method as PM
    cfg = PM.search_config({"sections": {"search": {"method": {"name": "Own", "min_score": 75}, "fast": True,
                                                    "target": "FID", "transfer": False}}})
    assert (cfg.method.name, cfg.method.min_score, cfg.fast, cfg.target, cfg.transfer, cfg.mode) == \
        ("Own", 75, True, "FID", False, "average_bg")
    cfg = PM.search_config({"sections": {}})
    assert cfg.method.name and cfg.target == "TIC" and cfg.transfer


def _items(ws, st, names):
    import gc_identify as GI
    from gcws.identify.service import SearchItem
    res = ws.result(st.id, "TIC")
    big = sorted(range(len(res.peaks)), key=lambda i: -res.peaks[i].area)[:len(names)]
    out = []
    for i, name in zip(sorted(big), names):
        p = res.peaks[i]
        job = GI.PeakJob(label=st.name, row_id=i, peak_no=p.number, rt=p.apex_rt, before=("", "", None),
                         hits=[{"name": name, "cas": "111-65-9", "score": 95}], done=True)
        out.append(SearchItem(st.id, "TIC", i, p.apex_rt, job, "average_bg", [57, 43, 71]))
    return out


def test_apply_search_results_names_tic_and_fid_peaks(qapp, samples):
    import gc_search_method as SM
    from gcws.core.ident import Identification
    from gcws.identify.service import apply_search_results
    ws = _workspace(["07_"])
    st = ws.active
    items = _items(ws, st, ["Alpha", "Beta", "Gamma"])
    # an analyst's name on a FID peak is never replaced by a copied TIC name
    fid = ws.result(st.id, "FID")
    target = items[0].apex_rt + st.delay_value
    j = min(range(len(fid.peaks)), key=lambda k: abs(fid.peaks[k].apex_rt - target))
    st.ident_set("FID").set(Identification(apex_rt=fid.peaks[j].apex_rt, name="Mine", manual=True))
    summary = apply_search_results(ws, items, SM.SearchMethod(name="Test"), transfer=True)
    assert summary.identified == 3
    assert summary.copied["protected"] <= 1 and summary.copied["copied"] >= 1
    tic, _ = st.ident_set("TIC").bind(ws.result(st.id, "TIC").peaks)
    assert {tic[it.peak_index].name for it in items} == {"Alpha", "Beta", "Gamma"}
    fid_names, _ = st.ident_set("FID").bind(fid.peaks)
    assert fid_names[j].name == "Mine"
    assert st.undo.count() == 1                                          # one step per run
    st.undo.undo()
    tic, _ = st.ident_set("TIC").bind(ws.result(st.id, "TIC").peaks)
    assert not any(tic.get(it.peak_index) for it in items)


def test_run_search_blocking(monkeypatch):
    import gc_identify as GI
    import gc_search_method as SM
    from gcws.identify import service as S
    monkeypatch.setattr(S, "prepare_local", lambda method, progress: {})
    monkeypatch.setattr(S, "search_one", lambda job, method, rng: [{"name": f"hit {job.row_id}", "score": 90}])
    jobs = [GI.PeakJob(label="x", row_id=i, peak_no=i + 1, rt=5.0 + i, before=("", "", None),
                       spectrum=[(57, 999), (71, 300)]) for i in range(3)]
    items = [S.SearchItem("r", "TIC", i, j.rt, j, "apex", [57]) for i, j in enumerate(jobs)]
    done = S.run_search_blocking(items, SM.SearchMethod(name="t"), fast=False, timeout=30)
    assert [it.job.hits[0]["name"] for it in done] == ["hit 0", "hit 1", "hit 2"]
    monkeypatch.setattr(S, "prepare_local", lambda method, progress: (_ for _ in ()).throw(RuntimeError("no lib")))
    for j in jobs:
        j.done = False
    with pytest.raises(S.SearchError, match="no lib"):
        S.run_search_blocking(items, SM.SearchMethod(name="t"), fast=False, timeout=30)


def test_suggest_groups_real_batch_names():
    from gcws.io.sequence import classify_role
    from gcws.quant.grouping import suggest_groups
    files = ["06_EtOH_ISTD.D", "07_26016606_130m_min_GIOSUN1635_A.D", "08_EtOH.D",
             "09_26016607_170m_min_GIOSUN1635_A.D", "10_EtOH.D", "11_26016606_130m_min_GIOSUN1635_B.D",
             "12_26016607_170m_min_GIOSUN1635_B.D", "13_EtOH.D", "14_26016608_single_A.D"]
    ids = [f[:2] for f in files]
    names = dict(zip(ids, files))
    roles = {i: classify_role(f[:-2]) for i, f in names.items()}
    counter = iter(range(100))
    groups, added = suggest_groups(names, {i: names[i][:-2] for i in ids}, roles, ids,
                                   new_id=lambda: f"g{next(counter)}")
    assert added == 3
    assert [g["members"] for g in groups] == [["07", "11"], ["09", "12"], ["14"]]
    assert groups[0]["name"] == "26016606_130m_min_GIOSUN1635"
    # a determination waiting alone joins its partner when it arrives
    alone = [{"id": "x", "name": "07 alone", "members": ["07"], "policy": "all"}]
    groups, _ = suggest_groups({k: names[k] for k in ("07", "11")}, {}, roles, ["07", "11"], alone)
    assert [g["members"] for g in groups] == [["07", "11"]]
