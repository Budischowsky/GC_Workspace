import copy

import pytest

from test_ui import win, _load


def test_cut_curves_peaks_undo_and_methods(qtbot, win, samples, tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from gcws.core import proc_method as PM, project as P
    from gcws.ui.workspace import Workspace
    _load(qtbot, win, samples, ["07_"])
    ws, st = win.ws, win.ws.active
    digest = ws.active_result().digest
    st.deconv[("run", "old")] = [object()]
    win.chrom.cut.setChecked(True)
    QApplication.processEvents()
    assert win.chrom2.cut.isChecked()
    assert ws.solvent_cut(st, "FID") == 5.5
    assert ws.solvent_cut(st, "TIC") == pytest.approx(5.5 - st.delay_value)
    for c in win.chroms:
        x = c.curves[st.id].xData
        sig = st.run.signal(c.run_key(st))
        start = max(5.5, float(sig.rt[0]) + c.shift(st))
        assert x[0] >= start and x[0] - start < 2 * (sig.rt[1] - sig.rt[0])
        assert all(p.apex_rt >= ws.solvent_cut(st, c.key) for p in ws.result(st.id, c.key).peaks)
    assert not st.deconv
    assert QSettings().value("integration/solvent_cut", type=bool)
    new_ws = Workspace()
    assert new_ws.quant["solvent_cut"]
    method = PM.collect(win, "with solvent cut")
    assert PM.read(PM.save(method, tmp_path / "method.json"))["sections"]["quant"]["solvent_cut"]
    path = P.save(ws, tmp_path / "cut.gcws")
    assert P.read(path)["quant"]["solvent_cut"]
    win.a_undo.trigger()
    QApplication.processEvents()
    assert not win.chrom.cut.isChecked() and not win.chrom2.cut.isChecked()
    assert ws.active_result().digest == digest
    assert win.chrom.curves[st.id].xData[0] < 5.5
    win.a_redo.trigger()
    assert ws.solvent_cut(st, "FID") == 5.5
    # The NIAS field is the same setting; moving it invalidates the cut results too.
    q = copy.deepcopy(ws.quant)
    q.setdefault("settings", {})["solvent_end"] = 14
    ws.push_quant("NIAS solvent end", q)
    assert all(p.apex_rt >= 14 for p in ws.active_result().peaks)
    assert win.chrom.curves[st.id].xData[0] >= 14
    ws.set_panels(["TIC", "FID"], [False, False])
    win.reset_views()
    assert win.chrom.vb.viewRange()[0][0] == pytest.approx(14 - st.delay_value)
    # Applying the saved method restores the shared time and on/off switch.
    PM.apply(win, method, ["quant"])
    assert ws.solvent_cut(st, "FID") == 5.5
    # A saved project restores the cut even if the current workspace has switched it off.
    ws.set_solvent_cut(False)
    win.close_all()
    win.open_project(path)
    qtbot.waitUntil(lambda: win.loading == 0 and win._pending_project is None, timeout=60000)
    assert ws.quant["solvent_cut"] and win.chrom.cut.isChecked()
    assert ws.active_result().digest == digest


def test_cut_on_load_and_dialog(qtbot, win, samples):
    from gcws.ui.dialogs.solvent_cut import SolventCutDialog
    ws = win.ws
    dialog = SolventCutDialog(ws, win)
    qtbot.addWidget(dialog)
    assert dialog.end.value() == 5.5
    dialog.end.setValue(14)
    dialog.enabled.setChecked(True)
    dialog._save()
    _load(qtbot, win, samples, ["07_"])
    st = ws.active
    assert all(p.apex_rt >= 14 for p in ws.result(st.id, "FID").peaks)
    assert all(p.apex_rt >= 14 - st.delay_value for p in ws.result(st.id, "TIC").peaks)


def test_whole_run_deconv_obeys_cut(qtbot, win, samples, monkeypatch):
    from types import SimpleNamespace
    from gcws.ms import deconv_cache as DC
    _load(qtbot, win, samples, ["07_"])
    ws, st = win.ws, win.ws.active
    ws.set_solvent_cut(True, 5.5)
    settings = DC.settings_of(ws)
    cut = ws.solvent_cut(st, "TIC")
    seen = []
    def compute(ms, t0, t1, *args, **kwargs):
        seen.append(t0)
        return []
    monkeypatch.setattr(DC.D, "deconvolute_range", compute)
    DC.compute_whole_run(st, settings, t_min=cut)
    assert seen == [max(cut, float(st.run.ms.rt[0]))]
    early = SimpleNamespace(rt=cut - 1, quality=100)
    late = SimpleNamespace(rt=float(st.run.ms.rt[-1]), quality=100)
    DC.store_whole_run(st, settings, [early, late])
    assert early not in DC.hidden_components(ws, st, "FID", settings)
