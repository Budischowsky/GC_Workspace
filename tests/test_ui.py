"""GUI smoke tests (pytest-qt)."""
import os

import pytest

pytest.importorskip("pytestqt")


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QSettings
    QCoreApplication.setOrganizationName("GCWorkspaceTest")
    QCoreApplication.setApplicationName("pytest")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    QSettings().clear()
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    from gcws.ui.main_window import MainWindow
    w = MainWindow()
    qtbot.addWidget(w)
    w.show()
    yield w
    w.ws.dirty = False
    w.close()             # while "No" is still the answer: a test run must never wait for a dialog


def _load(qtbot, w, samples, prefixes):
    paths = [str(next(samples.glob(p + "*.D"))) for p in prefixes]
    w.load_runs(paths)
    qtbot.waitUntil(lambda: w.loading == 0 and len(w.ws.runs) == len(paths), timeout=60000)


def test_load_tabs_and_overlay(qtbot, win, samples):
    _load(qtbot, win, samples, ["08_", "07_"])
    names = [win.loaded_samples.item(i).text() for i in range(win.loaded_samples.count())]
    assert names[0].startswith("07_26016606_130m_min_GIOSUN1635_A")      # injection order
    assert "[Blank]" in names[1]
    assert len(win.chrom.curves) == 2
    win.loaded_samples.setCurrentRow(1)
    assert win.ws.active.name == "08_EtOH"
    assert win.table.model.rowCount() > 10


def test_manual_integration_undo_redo(qtbot, win, samples):
    from gcws.core.events import ManualEvent, ManualKind as K
    _load(qtbot, win, samples, ["07_"])
    st = win.ws.active
    n0 = len(win.ws.active_result().peaks)
    win.tools.eventCreated.emit(ManualEvent(K.ADD_PEAK, 30.0, 30.2), "")
    assert len(win.ws.active_result().peaks) == n0 + 1
    assert win.table.model.rowCount() == n0 + 1
    win.a_undo.trigger()
    assert len(win.ws.active_result().peaks) == n0
    win.a_redo.trigger()
    assert len(win.ws.active_result().peaks) == n0 + 1
    assert any(r.action == "Manual integration" for r in win.ws.audit.records)
    assert st.events("FID")


def test_delete_marked_peaks(qtbot, win, samples, monkeypatch):
    from PySide6.QtCore import QItemSelectionModel, Qt
    from PySide6.QtWidgets import QMenu
    _load(qtbot, win, samples, ["07_"])
    table, st = win.table, win.ws.active
    before = win.ws.active_result().digest
    n = table.model.rowCount()
    table.view.sortByColumn(1, Qt.DescendingOrder)

    def select_three():
        table.view.clearSelection()
        for row in range(3):
            table.view.selectionModel().select(table.proxy.index(row, 0),
                                               QItemSelectionModel.Select | QItemSelectionModel.Rows)
        table.view.setFocus()

    select_three()
    qtbot.keyClick(table.view, Qt.Key_Delete)
    assert table.model.rowCount() == n - 3
    assert len(st.events(win.ws.active_key)) == 3
    win.integrate()
    assert table.model.rowCount() == n - 3
    win.a_undo.trigger()
    assert table.model.rowCount() == n and win.ws.active_result().digest == before
    select_three()
    with monkeypatch.context() as mp:
        from gcws.ui.docks import peak_table
        class TestMenu(QMenu):
            def exec(self, *_):
                assert table.delete_action in self.actions()
                table.delete_action.trigger()
        mp.setattr(peak_table, "QMenu", TestMenu)
        table._menu(table.view.rect().center())
    assert table.model.rowCount() == n - 3
    win.a_undo.trigger()
    assert win.ws.active_result().digest == before


def test_layout_presets_roundtrip(qtbot, win):
    from gcws.ui.layout import presets
    for name in presets.PRESETS:
        presets.apply_preset(win, name)
    presets.apply_preset(win, "Table left (classic)")
    presets.save_layout(win, "mine")
    presets.apply_preset(win, "Review")
    assert presets.restore_layout(win, "mine")
    assert "mine" in presets.saved_layouts()
    for d in win.docks.values():
        d.setFloating(False)


def test_dock_title_buttons_and_maximize(qtbot, win):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from gcws.ui.layout.title_bar import DockTitleBar, title_bar
    qtbot.waitUntil(lambda: win.docks["chrom"].isVisible(), timeout=5000)
    for d in win.docks.values():
        bar = title_bar(d)
        assert isinstance(bar, DockTitleBar)
        assert not bar.b_close.icon().isNull() and not bar.b_float.icon().isNull()
    bar = win.docks["table"].titleBarWidget()
    assert bar.b_close.isVisible() and bar.b_float.isVisible() and bar.b_max.isVisible()
    before = {k for k, d in win.docks.items() if d.isVisible()}
    QTest.mouseDClick(bar, Qt.LeftButton, Qt.NoModifier, QPoint(30, bar.height() // 2))
    assert win.docks["table"].isVisible() and not win.docks["table"].isFloating()
    assert {k for k, d in win.docks.items() if d.isVisible()} == {"table"}
    assert bar.maximized
    QTest.mouseDClick(bar, Qt.LeftButton, Qt.NoModifier, QPoint(30, bar.height() // 2))
    assert {k for k, d in win.docks.items() if d.isVisible()} == before
    assert not bar.maximized
    # locked panels: no detach / close buttons
    win._lock(True)
    assert bar.b_float.isHidden() and not bar.b_close.isHidden()
    win._lock(False)
    assert not bar.b_float.isHidden()
    # detach and dock back with the button
    bar.b_float.click()
    assert win.docks["table"].isFloating()
    bar.b_float.click()
    assert not win.docks["table"].isFloating()


def test_project_roundtrip(qtbot, win, samples, tmp_path):
    from gcws.ms import deconv as D, deconv_cache as DC
    from gcws.core.events import ManualEvent, ManualKind as K
    from gcws.core.ident import Identification
    _load(qtbot, win, samples, ["07_", "08_"])
    st = next(s for s in win.ws.states() if s.name.startswith("07_"))
    win.ws.set_active(st.id)
    win.tools.eventCreated.emit(ManualEvent(K.SPLIT, 13.42), "")
    p = win.ws.active_result().peaks[5]
    st.ident_set("FID").set(Identification(apex_rt=p.apex_rt, name="Test compound", cas="50-00-0", manual=True))
    digest = win.ws.active_result().digest
    win.ws.replicate_groups = [{"id": "g1", "name": "grp", "members": [st.id], "policy": "all"}]
    settings = D.DeconvSettings(shape_r=0.95, noise_factor=4)
    win.ws.quant["deconv"] = settings.to_dict()
    from gcws.core import project as P
    target = P.save(win.ws, tmp_path / "test.gcws")
    win.close_all()
    assert not win.ws.runs
    win.open_project(target)
    qtbot.waitUntil(lambda: win.loading == 0 and len(win.ws.runs) == 2 and win._pending_project is None,
                    timeout=60000)
    st2 = win.ws.runs[st.id]
    assert st2.events("FID") and st2.events("FID")[0].kind == K.SPLIT
    assert win.ws.result(st.id, "FID").digest == digest
    assert any(i.name == "Test compound" for i in st2.ident_set("FID").items)
    assert win.ws.replicate_groups[0]["members"] == [st.id]
    assert DC.settings_of(win.ws) == settings


def test_theme_applied(qtbot, win):
    from PySide6.QtWidgets import QApplication
    from gcws.ui import theme
    app = QApplication.instance()
    assert app.property("gcws_theme")
    assert theme.ACCENT.lower() in app.styleSheet().lower()
    assert win.ws.next_color() in theme.RUN_COLORS


def _view_pos(plot_widget, vb, x, y):
    from PySide6.QtCore import QPointF
    scene = vb.mapViewToScene(QPointF(x, y))
    return plot_widget.mapFromScene(scene)


def _double_click(widget, pos):
    """A double-click as Windows delivers it (press, release, double-click, release); QTest's
    mouseDClick leaves out the first press that pyqtgraph pairs the double-click with."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    at = QPointF(pos)
    g = QPointF(widget.mapToGlobal(pos))
    for kind, buttons in ((QEvent.MouseButtonPress, Qt.LeftButton), (QEvent.MouseButtonRelease, Qt.NoButton),
                          (QEvent.MouseButtonDblClick, Qt.LeftButton), (QEvent.MouseButtonRelease, Qt.NoButton)):
        QApplication.sendEvent(widget, QMouseEvent(kind, at, g, Qt.LeftButton, buttons, Qt.NoModifier))
    QApplication.processEvents()


def test_right_click_shows_scan_spectrum(qtbot, win, samples):
    from PySide6.QtCore import Qt
    _load(qtbot, win, samples, ["07_"])
    st = win.ws.active
    win.ws.set_signal_key("TIC")
    sig = st.run.signal("TIC")
    t = 15.8
    y = float(sig.y[sig.index_of(t)])
    qtbot.mouseClick(win.chrom.plot.viewport(), Qt.RightButton, pos=_view_pos(win.chrom.plot, win.chrom.vb, t, y))
    sp = win.spectrum
    assert sp.source == "scan" and sp.spec is not None and len(sp.spec.apex_scans) == 1
    assert abs(sp.spec.rt - t) < 0.03              # one screen pixel is ~0.02 min here
    assert sp.points()
    s0 = sp.spec.apex_scans[0]
    sp.step(+1)
    assert sp.spec.apex_scans == [s0 + 1]
    # right-drag: mean over a range; Shift+right-drag: background
    from gcws.ms.spectra import ScanRequest
    sp.show_range(ScanRequest(st.id, None, None, (15.0, 15.05)))
    sp.show_range(ScanRequest(st.id, 15.75, 15.85, None))
    assert len(sp.spec.apex_scans) > 3 and sp.spec.bg_scans
    # selecting a peak returns to peak mode
    win.ws.select_peak(3)
    assert sp.source == "peak"
    sp.show_range(ScanRequest(st.id, t, t, None))
    target_st, peak = sp.target_peak()
    assert target_st is st
    res = win.ws.active_result()
    assert (peak is None) == (res.peak_at(t) is None)


def test_right_drag_does_not_scale(qtbot, win, samples):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    _load(qtbot, win, samples, ["07_"])
    win.ws.set_signal_key("TIC")
    vp = win.chrom.plot.viewport()
    before = win.chrom.vb.viewRange()
    a = _view_pos(win.chrom.plot, win.chrom.vb, 15.0, before[1][0] + 0.5 * (before[1][1] - before[1][0]))
    b = a + QPoint(60, 0)
    QTest.mousePress(vp, Qt.RightButton, Qt.NoModifier, a)
    for k in range(1, 7):
        QTest.mouseMove(vp, a + QPoint(10 * k, 0))
    QTest.mouseRelease(vp, Qt.RightButton, Qt.NoModifier, b)
    after = win.chrom.vb.viewRange()
    assert after == before
    assert win.spectrum.source == "scan" and len(win.spectrum.spec.apex_scans) > 1


def test_two_chromatograms(qtbot, win, samples):
    from PySide6.QtCore import Qt
    _load(qtbot, win, samples, ["07_", "08_"])
    ws = win.ws
    c1, c2 = win.chrom, win.chrom2
    assert win.docks["chrom"].windowTitle() == "Chromatogram 1" and win.docks["zoom"].windowTitle() == "Chromatogram 2"
    assert c1.key == "FID" and c2.key == "TIC"
    assert len(c1.curves) == 2 and len(c2.curves) == 2
    st = ws.active
    xs = c2.curves[st.id].xData
    tic = st.run.signal("TIC")
    assert abs(xs[0] - (tic.rt[0] + st.delay_value)) < 1e-9       # MS shifted onto the FID axis
    # zoom and pan stay in sync, whichever panel moves
    c1.vb.setXRange(13.2, 14.2, padding=0)
    assert c2.vb.viewRange()[0] == pytest.approx([13.2, 14.2])
    c2.vb.setXRange(15.0, 16.0, padding=0)
    assert c1.vb.viewRange()[0] == pytest.approx([15.0, 16.0])
    # a click in Chromatogram 2 (TIC) selects the table's FID peak at that time
    res = ws.active_result()
    p = max(res.peaks, key=lambda q: q.area if 13.2 < q.apex_rt < 14.2 else 0)
    win.tools.set_tool("select")
    win.tools.click(c2.vb, p.apex_rt - st.delay_value, 0.0, Qt.NoModifier, (1.0, 0.0), c2.tool_key())
    assert ws.selected_peak() is p
    assert c2.selected_index() >= 0                                 # highlighted in the TIC too
    # the table lists the peaks of the chromatogram chosen at its top
    win.table.source_buttons[1].click()
    assert ws.table_panel == 1 and ws.signal_key == "TIC"
    assert win.table.model.rowCount() == len(ws.result(st.id, "TIC").peaks)
    assert c2.table_chip.text() and not c1.table_chip.text()
    win.table.source_buttons[0].click()
    assert ws.signal_key == "FID"
    # a manual event in Chromatogram 2 changes the TIC integration only
    n_fid, n_tic = len(ws.result(st.id, "FID").peaks), len(ws.result(st.id, "TIC").peaks)
    win.tools.set_tool("add")
    win.tools.drag_finished(c2.vb, 30.0, 0.0, 30.2, 0.0, Qt.NoModifier, (1.0, 0.0), None, c2.tool_key())
    assert len(ws.result(st.id, "TIC").peaks) == n_tic + 1
    assert len(ws.result(st.id, "FID").peaks) == n_fid
    assert st.events("TIC") and not st.events("FID")
    win.tools.set_tool("select")
    # each panel has its own blank switch
    c1.blank.setChecked(True)
    assert ws.panel_key(0) == "FID - Blank" and ws.signal_key == "FID - Blank" and c2.key == "TIC"
    c1.blank.setChecked(False)
    # Chromatogram 1 on TIC: the FID in Chromatogram 2 is shifted back onto the MS axis
    c1.set_signal("TIC")
    c2.set_signal("FID")
    xs = c2.curves[st.id].xData
    assert abs(xs[0] - (st.run.fid.rt[0] - st.delay_value)) < 1e-9
    # double-click: the whole run in both
    win.reset_views()
    x0, x1 = c1.vb.viewRange()[0]
    assert x0 <= st.run.fid.rt[0] + 0.1 and x1 >= tic.rt[-1] - 0.1
    assert c2.vb.viewRange()[0] == pytest.approx([x0, x1])


def test_interpretation_tab_and_class_hints(qtbot, win, samples):
    _load(qtbot, win, samples, ["07_"])
    ws = win.ws
    ws.set_signal_key("TIC")
    res = ws.active_result()
    idx = min(range(len(res.peaks)), key=lambda i: abs(res.peaks[i].apex_rt - 24.857))
    ws.select_peak(idx)
    sp = win.spectrum
    assert sp.interp is not None and sp.interp.classes[0].id == "phosphite"
    assert "Irgafos 168" in sp.interp_view.toPlainText()
    assert sp.interp.m is not None and sp.interp.m.mz == 646
    # the optional "Class hint" column fills in lazily
    st, peak = ws.active, res.peaks[idx]
    win.table.set_shown(win.table.shown_keys() + ["class_hint"])
    ws.hints.get(st, "TIC", peak)                      # queues the computation
    qtbot.waitUntil(lambda: ws.hints.get(st, "TIC", peak) is not None, timeout=20000)
    text, tip = ws.hints.get(st, "TIC", peak)
    assert "phosphite" in text.lower() and tip
    # clicking an ion shows its EIC in the MS chromatogram (Chromatogram 2 when 1 shows the FID)
    ws.set_signal_key("FID")
    win.show_ion_eic(441)
    assert win.chrom2.key == "EIC 441" and win.chrom.key == "FID"


def test_double_determination_from_tab_menu(qtbot, win, samples):
    _load(qtbot, win, samples, ["07_", "08_", "11_"])
    ws = win.ws
    a = next(s.id for s in ws.states() if s.name.startswith("07_"))
    b = next(s.id for s in ws.states() if s.name.startswith("11_"))
    win.loaded_samples.pairRequested.emit(a, b)
    page = win.replicates.duplicate
    assert win.replicates.tabs.currentIndex() == 0
    assert page.a.currentData() == a and page.b.currentData() == b
    assert page.rows and len(page.verdicts) == len(page.rows), page.banner.text()
    g = page.group()
    assert g is not None and g["members"] == [a, b]
    win.a_undo.trigger()                                   # the group change is undoable
    assert page.group() is None or ws.replicate_groups == []
    win.a_redo.trigger()
    # the difference limit is the report parameter
    page.limit.setValue(12.5)
    page._limit_changed()
    from gcws.quant.duplicate_view import limits
    assert limits(ws)[0] == pytest.approx(12.5)
    # "needs attention" filter hides confirmed rows
    page.only_problems.setChecked(True)
    assert page.table.rowCount() <= len(page.rows)
    # navigation from a row activates a determination and selects its peak
    k = next(i for i, r in enumerate(page.rows) if r.get("source1"))
    page._navigate(page.rows[k])
    assert ws.active_id == a and ws.selected >= 0


def test_blank_key_keeps_peak_list(qtbot, win, samples):
    """'FID - Blank' on a run that has no plain Blank (the blank itself, a Blank+ISTD) must still list peaks."""
    from gcws.core.keys import is_derived
    _load(qtbot, win, samples, ["06_", "07_", "08_"])
    ws = win.ws
    sample = next(s for s in ws.states() if s.name.startswith("07_"))
    ws.set_active(sample.id)
    ws.set_signal_key("FID - Blank")
    assert win.table.model.rowCount() > 0
    # quantification columns are filled on the blank-subtracted trace (mapped to the FID peaks by RT)
    ws.recompute_quant()
    win.table.reload()
    assert any(r.quant.get("corr_area") for r in win.table.model.rows)
    # "In blank" on the subtracted trace comes from the matching FID peak
    assert ws.blank_matches(sample.id)
    for prefix in ("08_", "06_"):
        other = next(s for s in ws.states() if s.name.startswith(prefix))
        ws.set_active(other.id)
        assert win.table.model.rowCount() > 0, prefix
        assert not is_derived(ws.effective_key(other, "FID - Blank"))
        assert "not available" in win.table.banner.text()
    ws.set_active(sample.id)
    assert win.table.banner.isHidden()


def test_derived_trace_follows_base_reintegration(qtbot, win, samples):
    from gcws.core.events import ManualEvent, ManualKind as K
    _load(qtbot, win, samples, ["07_", "08_"])
    ws = win.ws
    sample = next(s for s in ws.states() if s.name.startswith("07_"))
    ws.set_active(sample.id)
    before = ws.result(sample.id, "FID - Blank")
    assert before is not None
    # a manual change of the plain FID integration re-derives the blank-subtracted trace
    from gcws.ui.undo import add_event
    sample.undo.push(add_event(ws, sample.id, "FID", ManualEvent(K.ADD_PEAK, 30.0, 30.2), ""))
    assert ws.result(sample.id, "FID - Blank") is not before


def test_blank_subtraction_toggle_and_project(qtbot, win, samples, tmp_path):
    _load(qtbot, win, samples, ["07_", "08_"])
    ws = win.ws
    st = next(s for s in ws.states() if s.name.startswith("07_"))
    ws.set_active(st.id)
    assert win.chrom.blank.isEnabled()
    win.chrom.blank.setChecked(True)
    assert ws.signal_key == "FID - Blank"
    assert win.table.model.rowCount() > 0
    assert "FID − Blank" in win.table.source_buttons[0].text()
    # peak-level check: hiding blank peaks reduces the visible rows
    ws.set_signal_key("FID")
    n = win.table.proxy.rowCount()
    win.table.hide_blank.setChecked(True)
    assert win.table.proxy.rowCount() < n
    win.table.hide_blank.setChecked(False)
    # settings are saved with the project, the derived key too
    import copy
    q = copy.deepcopy(ws.quant)
    q["blank_sub"] = {"mode_fid": "full", "scale": 1.1}
    ws.push_quant("blank settings", q)
    ws.set_signal_key("FID - Blank")
    from gcws.core import project as P
    target = P.save(ws, tmp_path / "blank.gcws")
    win.close_all()
    win.open_project(target)
    qtbot.waitUntil(lambda: win.loading == 0 and len(ws.runs) == 2 and win._pending_project is None, timeout=60000)
    assert ws.signal_key == "FID - Blank" and ws.quant["blank_sub"]["scale"] == 1.1
    assert ws.panel_blank == [True, False] and win.chrom.blank.isChecked()
    st2 = ws.runs[st.id]
    assert ws.result(st2.id, "FID - Blank") is not None


def test_deconvolution_dialog_whole_run_and_markers(qtbot, win, samples):
    _load(qtbot, win, samples, ["07_"])
    ws = win.ws
    ws.set_signal_key("TIC")
    res = ws.active_result()
    idx = min(range(len(res.peaks)), key=lambda i: abs(res.peaks[i].apex_rt - 13.41))
    ws.select_peak(idx)
    from gcws.ui.dialogs.deconv import DeconvolutionDialog
    dlg = DeconvolutionDialog(win, "peak")
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: not dlg._busy, timeout=30000)
    assert dlg.comps and dlg.model.rowCount() == len(dlg.plan.candidates)
    assert any(abs(c.rt - 13.409) < 0.01 for c in dlg.comps)
    import gc_deconv
    from gcws.ms.spectra import ms_times
    from test_deconv import assert_same
    ta = ms_times(ws.selected_peak(), ws.active_key, ws.active.delay_value)[2]
    assert_same(dlg.comps, gc_deconv.deconvolute(ws.active.run.ms_source, ta, gc_deconv.DeconvParams()))
    assert not hasattr(dlg, "residual") and not hasattr(dlg, "skew")
    # Components from whole-run markers must also display without newer-engine fields.
    win.spectrum.show_component(ws.active_id, dlg.comps[0])
    assert win.spectrum.source == "component" and win.spectrum.spec.ab.size
    # pin a component's spectrum to the peak
    dlg.table.selectRow(0)
    dlg.pin()
    st = ws.active
    from gcws.ms.assignment import override_for
    assert "component" in override_for(st, "TIC", res.peaks[idx])
    assert win.spectrum.spec.mode == "deconvoluted"
    # whole run in the background -> cached, hidden components marked in the chromatogram
    dlg.scope.button(2).setChecked(True)
    dlg.run()
    from gcws.ms import deconv_cache as DC
    qtbot.waitUntil(lambda: DC.whole_run(st, dlg.settings()) is not None, timeout=60000)
    assert len(DC.whole_run(st, dlg.settings())) > 50
    dlg.save_default()
    assert DC.whole_run(st, DC.settings_of(ws)) is not None or ws.quant.get("deconv")
    # The "deconvoluted" spectrum mode uses NIAS, without invented quality scores.
    win.spectrum._clear_override()
    i = win.spectrum.mode.findData("deconvoluted")
    win.spectrum.mode.setCurrentIndex(i)
    assert win.spectrum.spec.mode == "deconvoluted" and "purity" in win.spectrum.spec.note
    win.spectrum.mode.setCurrentIndex(0)


def test_quant_on_tic(qtbot, win, samples):
    """The Detector choice runs the same modes on the TIC peaks (FID time axis inside)."""
    _load(qtbot, win, samples, ["07_"])
    ws, qd = win.ws, win.quant
    rid, st = ws.active_id, ws.active
    win._show_dock("quant")
    assert qd.detector.isVisibleTo(qd) and qd.detector.currentData() == "FID"
    # area % on TIC: the TIC peaks' own area %
    qd.mode.setCurrentIndex(qd.mode.findData("area_pct"))
    qd.mode.activated.emit(qd.mode.currentIndex())
    qd.detector.setCurrentIndex(qd.detector.findData("TIC"))
    qd.detector.activated.emit(qd.detector.currentIndex())
    assert ws.quant["detector"] == "TIC" and ws.quant["mode"] == "area_pct"
    ws.recompute_quant()
    tic = ws.result(rid, "TIC")
    rows = ws.quant_result.rows[rid]
    assert rows and ws.quant_result.samples[rid].meta["detector"] == "TIC"
    for i, r in rows.items():
        assert r["conc"] == pytest.approx(tic.peaks[i].area_pct)
        assert r["raw_area"] == pytest.approx(tic.peaks[i].area)
    assert any(v.get("conc") for v in ws.quant_rows(rid, "TIC").values())
    assert not any(v.get("conc") for v in ws.quant_rows(rid, "FID").values())    # other detector: RRT only
    # ISTD concentration on TIC: ISTDs found on the TIC, their RTs kept in FID time
    qd.mode.setCurrentIndex(qd.mode.findData("istd_conc"))
    qd.mode.activated.emit(qd.mode.currentIndex())
    qd.istd_conc.setValue(10.0)
    qd.istd_conc.editingFinished.emit()
    assert ws.quant["detector"] == "TIC" and ws.quant["istd_conc_value"] == 10.0
    ws.recompute_quant()
    found = [s for s in ws.quant_result.samples[rid].standards if s.get("row_id") is not None]
    assert found
    for s in found:
        assert any(abs(p.apex_rt + st.delay_value - s["fid_rt"]) < 1e-6 for p in tic.peaks)
    assert sum(1 for r in ws.quant_result.rows[rid].values() if r["conc"]) > 10
    # a peak bound from the TIC is stored in FID time
    ws.set_signal_key("TIC")
    code = qd.bind_box.currentData()
    ws.select_peak(max(range(len(tic.peaks)), key=lambda k: tic.peaks[k].area))
    qd._bind_selected()
    assert ws.quant["istd_bindings"][rid][code] == pytest.approx(
        round(ws.selected_peak().apex_rt + st.delay_value, 4))
    # Undo reaches FID again; the selector is hidden in HS mode
    for _ in range(10):
        if ws.quant.get("detector", "FID") == "FID":
            break
        win.a_undo.trigger()
    assert ws.quant.get("detector", "FID") == "FID" and qd.detector.currentData() == "FID"
    qd.mode.setCurrentIndex(qd.mode.findData("hs_screening"))
    qd.mode.activated.emit(qd.mode.currentIndex())
    assert not qd.detector.isVisibleTo(qd)


def test_quant_panel_istd_concentration_mode(qtbot, win, samples):
    _load(qtbot, win, samples, ["07_"])
    ws, qd = win.ws, win.quant
    win._show_dock("quant")
    # NIAS mode: fixed unit shown, no editable unit / ISTD concentration
    assert qd.result_unit.isVisibleTo(qd) and "mg/kg" in qd.result_unit.text()
    assert not qd.unit.isVisibleTo(qd) and not qd.istd_conc.isVisibleTo(qd)
    qd.mode.setCurrentIndex(qd.mode.findData("istd_conc"))
    qd.mode.activated.emit(qd.mode.currentIndex())
    assert ws.quant["mode"] == "istd_conc"
    assert qd.unit.isVisibleTo(qd) and qd.istd_conc.isVisibleTo(qd) and qd.istd_conc.isEnabled()
    qd.istd_conc.setValue(10.0)
    qd.unit.setCurrentText("µg/mL")
    qtbot.waitUntil(lambda: ws.quant.get("istd_conc_value") == 10.0 and ws.quant.get("unit") == "µg/mL",
                    timeout=3000)
    ws.recompute_quant()
    rows = ws.quant_result.rows[ws.active_id]
    assert sum(1 for r in rows.values() if r["conc"]) > 10          # no ISTD concentration table needed
    assert ws.quant_unit() == "µg/mL"
    # an edit of the ISTD table applies at once (no Apply button to forget)
    from PySide6.QtCore import Qt
    item = qd.defs.item(0, 2)
    item.setText("0.5")
    assert ws.quant["istd_defs"][0]["concentration"] == 0.5
    win.a_undo.trigger()
    assert not ws.quant.get("istd_defs") or ws.quant["istd_defs"][0]["concentration"] != 0.5


def test_peak_table_value_filter(qtbot, win, samples):
    from gcws.ui.docks.peak_table import parse_number, value_test
    assert parse_number("0,05") == 0.05 and parse_number("1,234,567") == 1234567 and parse_number("x") is None
    assert value_test("=", 0.0123, decimals=4)(0.01234) and not value_test("=", 0.0123, decimals=4)(0.0124)
    assert value_test("between", 5, 1)(1) and value_test("outside", 1, 5)(6) and not value_test("outside", 1, 5)(3)
    _load(qtbot, win, samples, ["07_"])
    t = win.table
    n = t.model.rowCount()
    areas = sorted(r.peak.area for r in t.model.rows)
    median = areas[len(areas) // 2]
    t.set_value_filter("area", ">", median)
    assert t.proxy.rowCount() == sum(1 for a in areas if a > median)
    assert f"of {n} peaks shown" in t.info.text()
    t.set_value_filter("area", "≤", median)
    assert t.proxy.rowCount() == sum(1 for a in areas if a <= median)
    lo, hi = areas[5], areas[-5]
    t.set_value_filter("area", "between", lo, hi)
    inside = t.proxy.rowCount()
    t.set_value_filter("area", "outside", lo, hi)
    assert t.proxy.rowCount() + inside == n
    # between needs both limits: one limit only means no filter yet
    t.set_value_filter("area", "between", lo, None)
    assert t.proxy.rowCount() == n
    # concentration: peaks without a value are hidden while the filter is on
    win.ws.recompute_quant()
    t.reload()
    t.set_value_filter("conc", ">", 0)
    with_conc = sum(1 for r in t.model.rows if r.quant.get("conc") is not None and r.quant["conc"] > 0)
    assert t.proxy.rowCount() == with_conc
    t.clear_value_filter()
    assert t.proxy.rowCount() == n and t.proxy.value_filter is None


def test_agilent_style_zoom(qtbot, win, samples):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    _load(qtbot, win, samples, ["07_"])
    c1, c2 = win.chrom, win.chrom2
    win.reset_views()
    full = c1.vb.viewRange()[0]
    y2_full = c2.vb.viewRange()[1]
    # A box sets both time and relative intensity in the other detector's units.
    (x0, x1), (y0, y1) = c1.vb.viewRange()
    a = _view_pos(c1.plot, c1.vb, 13.0, y0 + 0.6 * (y1 - y0))
    b = _view_pos(c1.plot, c1.vb, 15.0, y0 + 0.1 * (y1 - y0))
    vp = c1.plot.viewport()
    QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, a)
    for k in range(1, 9):
        QTest.mouseMove(vp, a + (b - a) * k / 8)
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, b)
    QApplication.processEvents()
    zx = c1.vb.viewRange()[0]
    assert zx[0] == pytest.approx(13.0, abs=0.05) and zx[1] == pytest.approx(15.0, abs=0.05)
    assert c1.vb.viewRange()[1][1] == pytest.approx(y0 + 0.6 * (y1 - y0), rel=0.05)
    assert c2.vb.viewRange()[0] == pytest.approx(zx)
    boxed = c1.vb.viewRange()[1]
    assert c2.vb.viewRange()[1] == pytest.approx([
        y2_full[0] + (v - y0) / (y1 - y0) * (y2_full[1] - y2_full[0]) for v in boxed])
    # wheel over the plot: time only, both panels
    y_before = c1.vb.viewRange()[1]
    pos = QPointF(_view_pos(c1.plot, c1.vb, 14.0, sum(y_before) / 2))
    ev = QWheelEvent(pos, QPointF(vp.mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(0, 240), Qt.NoButton,
                     Qt.NoModifier, Qt.ScrollUpdate, False)
    QApplication.sendEvent(vp, ev)
    QApplication.processEvents()
    wx = c1.vb.viewRange()[0]
    assert wx[1] - wx[0] < zx[1] - zx[0] and c2.vb.viewRange()[0] == pytest.approx(wx)
    # double-click: the whole run in both
    yr = c1.vb.viewRange()[1]                                 # manual intensity survives the time zoom
    assert yr == pytest.approx(y_before)
    _double_click(vp, _view_pos(c1.plot, c1.vb, 14.0, sum(yr) / 2))
    QApplication.processEvents()
    assert c1.vb.viewRange()[0] == pytest.approx(full, abs=0.05)
    assert c2.vb.viewRange()[0] == pytest.approx(full, abs=0.05)
    # a peak picked in the table zooms both; a click in the chromatogram only selects
    t = win.table
    res = win.ws.active_result()
    k = max(range(len(res.peaks)), key=lambda i: res.peaks[i].area if 20 < res.peaks[i].apex_rt < 22 else 0)
    row = t.proxy.mapFromSource(t.model.index(k, 0))
    t.view.setCurrentIndex(row)
    p = res.peaks[k]
    zx = c1.vb.viewRange()[0]
    assert zx[0] < p.apex_rt < zx[1] and zx[1] - zx[0] < 2.0
    assert c2.vb.viewRange()[0] == pytest.approx(zx)
    win.reset_views()
    before = c1.vb.viewRange()[0]
    win.tools.click(c1.vb, res.peaks[k + 1].apex_rt, 0.0, Qt.NoModifier, (1.0, 0.0), c1.tool_key())
    assert win.ws.selected == k + 1 and c1.vb.viewRange()[0] == pytest.approx(before)


def test_axis_drag_pans_in_every_tool(qtbot, win, samples):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    _load(qtbot, win, samples, ["07_"])
    c1, c2 = win.chrom, win.chrom2
    st = win.ws.active
    n_events = len(st.events("FID"))
    for tool, side in (("baseline", "bottom"), ("select", "left"), ("delete", "bottom")):
        win.tools.set_tool(tool)
        c1.vb.setXRange(14.0, 18.0, padding=0)
        QApplication.processEvents()
        v = c1.vb.sceneBoundingRect()                    # just outside the plot area: on the axis
        y_before = c1.vb.viewRange()[1][:]
        on_axis = QPointF(v.center().x(), v.bottom() + 8) if side == "bottom" else             QPointF(v.left() - 20, v.center().y())
        centre = c1.plot.mapFromScene(on_axis)
        vp = c1.plot.viewport()
        QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, centre)
        step = QPoint(-10, 0) if side == "bottom" else QPoint(-5, 5)    # on the left axis: diagonal
        for k in range(1, 9):
            QTest.mouseMove(vp, centre + step * k)
        end = centre + step * 8
        QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, end)
        QApplication.processEvents()
        x0, x1 = c1.vb.viewRange()[0]
        assert x1 - x0 == pytest.approx(4.0, abs=0.01), tool      # moved, not zoomed
        if side == "left":
            assert (x0, x1) == pytest.approx((14.0, 18.0))
            yr = c1.vb.viewRange()[1]
            assert yr[0] > y_before[0]
            assert yr[1] - yr[0] == pytest.approx(y_before[1] - y_before[0])
        else:
            assert x0 > 14.05, tool
        assert c2.vb.viewRange()[0] == pytest.approx((x0, x1), abs=1e-6)
        assert len(st.events("FID")) == n_events, tool            # no manual event from an axis drag
    win.tools.set_tool("select")


def test_export_chromatogram(qtbot, win, samples, tmp_path):
    from PySide6.QtGui import QImage
    from gcws.ui.dialogs import export_chrom as E
    _load(qtbot, win, samples, ["07_"])
    c1, c2 = win.chrom, win.chrom2
    c1.vb.setXRange(12.0, 16.0, padding=0)
    ranges = (c1.vb.viewRange(), c2.vb.viewRange())
    size = c1.plot.getPlotItem().size()
    for ext in (".png", ".jpg", ".tif", ".bmp", ".svg", ".pdf"):
        out = E.export([c1, c2], tmp_path / f"c{ext}", 1000, 300, 2.0, title="07 FID / TIC")
        assert out.is_file() and out.stat().st_size > 1000, ext
    img = QImage(str(tmp_path / "c.png"))
    assert (img.width(), img.height()) == (2000, 2 * (2 * 300 + E.TITLE_H))
    assert (tmp_path / "c.svg").read_text(encoding="utf-8").lstrip().startswith("<?xml")
    assert (tmp_path / "c.pdf").read_bytes()[:4] == b"%PDF"
    # the view and the items are as before, and the cursor is back
    assert (c1.vb.viewRange(), c2.vb.viewRange()) == ranges
    assert c1.plot.getPlotItem().size() == size and c1.cursor.isVisible()
    assert c1.peaks.pen_scale == 1.0
    # the dialog: default name, save, clipboard
    dlg = E.ExportChromatogramDialog(win, 1)
    qtbot.addWidget(dlg)
    assert "TIC" in dlg.default_name() and dlg.default_name().endswith(".png")
    dlg.what.setCurrentIndex(2)
    assert len(dlg.panels()) == 2
    p = dlg.save(str(tmp_path / "dlg.png"))
    assert p is not None and p.is_file()


def test_library_search_on_tic_copies_names_to_fid(qtbot, win, samples):
    from types import SimpleNamespace
    from gcws.core.ident import Identification
    from gcws.identify.service import build_items, transfer_names
    _load(qtbot, win, samples, ["07_"])
    ws = win.ws
    st = ws.active
    # the start dialog offers TIC (default) or FID peaks
    from gcws.ui.dialogs.identify import SearchStartDialog
    dlg = SearchStartDialog(ws, win)
    qtbot.addWidget(dlg)
    assert dlg.target_tic.isChecked() and dlg.transfer.isChecked()
    v = dlg.values()
    assert v["target"] == "TIC" and v["transfer"] and win.search_key("TIC") == "TIC"
    # a TIC search (SpectrAtlas replaced by fixed hits on the five largest TIC peaks)
    items, _ = build_items(ws, [st.id], "TIC", "average_bg")
    items = sorted(items, key=lambda it: -ws.result(st.id, "TIC").peaks[it.peak_index].area)[:5]
    for n, it in enumerate(items):
        it.job.done, it.job.hits, it.job.chosen = True, [{"name": f"Substance {n}", "cas": "", "score": 95}], 0
    fid = ws.result(st.id, "FID")
    # the analyst named one FID peak by hand: it keeps its name
    t0 = items[0].apex_rt + st.delay_value
    j0 = min(range(len(fid.peaks)), key=lambda k: abs(fid.peaks[k].apex_rt - t0))
    st.ident_set("FID").set(Identification(apex_rt=fid.peaks[j0].apex_rt, name="Hand made", manual=True))
    method = SimpleNamespace(name="test", min_score=70, hydrocarbons=False)
    win._search_done(items, {"method": method, "review": False, "transfer": True, "target": "TIC"}, 0, False)
    tic_names = {i.name for i in st.ident_set("TIC").items}
    assert {f"Substance {n}" for n in range(5)} <= tic_names
    fid_idents, _ = st.ident_set("FID").bind(fid.peaks)
    assert fid_idents[j0].name == "Hand made"
    copied = [i for i in st.ident_set("FID").items if i.name.startswith("Substance")]
    assert len(copied) >= 3 and all("via TIC" in i.source for i in copied)
    for i in copied:                              # each sits on an FID peak at TIC apex + delay
        tic_rt = next(t.apex_rt for t in st.ident_set("TIC").items if t.name == i.name)
        assert abs(i.apex_rt - (tic_rt + st.delay_value)) < 0.03
    # one undo step takes back both
    win.a_undo.trigger()
    assert not any(i.name.startswith("Substance") for i in st.ident_set("FID").items + st.ident_set("TIC").items)
    # co-elution: two TIC names on one FID peak -> the better score wins
    p = fid.peaks[j0 + 1]
    t = p.apex_rt - st.delay_value
    out, counts = transfer_names(ws, st.id, [(t, Identification(apex_rt=t, name="A", score=80)),
                                             (t + 0.001, Identification(apex_rt=t + 0.001, name="B", score=90))])
    assert [i.name for _rt, i in out] == ["B"] and counts["coeluting"] == 1


def test_library_search_all_runs_and_only_shown_peaks(qtbot, win, samples):
    from gcws.identify.service import build_items
    from gcws.ui.dialogs.identify import SearchStartDialog
    from gcws.ui.models.peak_filter import visible_indices
    _load(qtbot, win, samples, ["07_", "08_"])
    ws = win.ws
    dlg = SearchStartDialog(ws, win)
    qtbot.addWidget(dlg)
    assert not dlg.only_shown.isEnabled()                 # no filter set
    # scope and peak type are separate choices: "all" keeps TIC and the copy to FID
    dlg.all_runs.setChecked(True)
    assert dlg.target_tic.isChecked()
    v = dlg.values()
    assert v["all"] and v["target"] == "TIC" and v["transfer"]
    dlg.target_fid.setChecked(True)
    assert dlg.all_runs.isChecked() and dlg.values()["target"] == "FID"
    # a value filter on the FID table: only the shown peaks are searched, in every run
    ws.set_signal_key("FID")
    t = win.table
    areas = sorted(r.peak.area for r in t.model.rows)
    t.set_value_filter("area", ">", areas[-8])
    assert t.filter_state().active and len(t.shown_indices()) == 7
    ids = list(ws.order)
    only = win.shown_peaks(ids, "FID")
    assert only[ws.active_id] == t.shown_indices()
    other = next(i for i in ids if i != ws.active_id)
    assert only[other] == visible_indices(ws, other, "FID", t.filter_state())
    items, _ = build_items(ws, ids, "FID", "average_bg", only=only)
    assert {(it.run_id, it.peak_index) for it in items} <= {(r, i) for r, s in only.items() for i in s}
    # a TIC search takes the TIC peaks at the shown FID peaks' times
    tic = win.shown_peaks([ws.active_id], "TIC")[ws.active_id]
    assert 0 < len(tic) <= 7
    st = ws.active
    fid_rts = [ws.result(st.id, "FID").peaks[i].apex_rt for i in t.shown_indices()]
    for j in tic:
        rt = ws.result(st.id, "TIC").peaks[j].apex_rt + st.delay_value
        assert min(abs(rt - f) for f in fid_rts) <= 0.03
    dlg2 = SearchStartDialog(ws, win, filter_text="7 of 90 peaks shown")
    qtbot.addWidget(dlg2)
    assert dlg2.only_shown.isEnabled()
    dlg2.only_shown.setChecked(True)
    assert dlg2.values()["only_shown"]


def test_edit_library_dialog_adds_current_spectrum(qtbot, win, samples, tmp_path, monkeypatch):
    from gcws.identify import library_edit as LE
    from gcws.ui.dialogs.library_edit import EditLibraryDialog, entry_from_spectrum
    from gcws import paths
    from gcws.libsearch import store
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    root = tmp_path / "libs"
    (root / "CCALU_GCMS_1.L").mkdir(parents=True)
    (root / "CCALU_GCMS_1.L" / "HEADER.IND").write_bytes(b"")
    lib = root / "Own spectra.msp"
    lib.write_text(LE.write_msp([LE.new_record("Old entry", [(57, 999), (71, 300)])]), encoding="cp1252",
                   newline="")
    store.save(store.discover(root))                 # the analyst's libraries (Identify > Libraries...)
    monkeypatch.setattr(LE, "find_lib2nist", lambda *a, **k: None)
    rescans = []
    monkeypatch.setattr(LE, "library_changed", lambda: rescans.append(1) or "")
    _load(qtbot, win, samples, ["07_"])
    ws = win.ws
    res = ws.result(ws.active_id, "TIC")
    ws.set_signal_key("TIC")
    k = max(range(len(res.peaks)), key=lambda i: res.peaks[i].area if 20 < res.peaks[i].apex_rt < 26 else 0)
    ws.select_peak(k)
    entry = entry_from_spectrum(win)
    assert entry["peaks"] and entry["rt"] and "07_" in entry["source"]
    dlg = EditLibraryDialog(win, entry)
    qtbot.addWidget(dlg)
    # the Agilent library is listed but cannot be chosen; the MSP library is the default
    assert dlg.current_library().name == "Own spectra"
    agilent = dlg.library.findData("CCALU_GCMS_1.L")
    assert agilent >= 0 and not dlg.library.model().item(agilent).isEnabled()
    dlg.name.setText("Test substance")
    dlg.cas.setText("117-81-7")
    dlg.formula.setText("C24H38O4")
    assert dlg.mw.text() == "390"                       # nominal mass from the formula
    dlg.ri.setText("2530")
    dlg.save_entry()
    qtbot.waitUntil(lambda: not dlg._busy, timeout=20000)
    recs = LE.parse_msp(lib.read_text(encoding="cp1252"))
    assert [r.name for r in recs] == ["Old entry", "Test substance"]
    new = recs[1]
    assert new.cas == "117-81-7" and new.ri == 2530 and len(new.peaks) == len(dlg.trimmed_peaks()) > 5
    assert "07_" in new.get("Comment") and rescans
    assert any(r.action == "Library entry added" for r in ws.audit.records)
    # browse, edit and delete
    dlg.tabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: not dlg._busy and len(dlg.records) == 2, timeout=20000)
    dlg.table.setCurrentCell(1, 0)
    dlg.edit_selected()
    assert dlg.editing == 1 and dlg.name.text() == "Test substance" and dlg.ri.text() == "2530"
    dlg.name.setText("Test substance, corrected")
    dlg.save_entry()
    qtbot.waitUntil(lambda: not dlg._busy and len(dlg.records) == 2, timeout=20000)
    assert [r.name for r in LE.parse_msp(lib.read_text(encoding="cp1252"))] == ["Old entry",
                                                                                 "Test substance, corrected"]
    from PySide6.QtWidgets import QMessageBox
    with monkeypatch.context() as m:                  # "Yes" only for this question, never for closing the app
        m.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        dlg.table.setCurrentCell(0, 0)
        dlg.delete_selected()
    qtbot.waitUntil(lambda: not dlg._busy and len(dlg.records) == 1, timeout=20000)
    dlg.close()
    ws.dirty = False
    assert [r.name for r in LE.parse_msp(lib.read_text(encoding="cp1252"))] == ["Test substance, corrected"]


def test_double_determination_cells_editable(qtbot, win, samples):
    from PySide6.QtCore import Qt
    from gcws.ui.docks.duplicate import C_C1, C_MEAN, C_NAME, C_REPORT
    _load(qtbot, win, samples, ["07_", "08_", "11_"])
    ws = win.ws
    a = next(s.id for s in ws.states() if s.name.startswith("07_"))
    b = next(s.id for s in ws.states() if s.name.startswith("11_"))
    win.loaded_samples.pairRequested.emit(a, b)
    page = win.replicates.duplicate
    t = page.table
    assert page.rows

    def cell(k, c):
        r = next(i for i in range(t.rowCount()) if t.item(i, 0).data(Qt.UserRole) == k)
        return t.item(r, c)
    k = next(i for i, r in enumerate(page.rows) if r.get("source1") and r.get("source2") and r.get("report"))
    rt = page.rows[k]["rt"]
    # the name becomes the identification of the peak in both determinations (one undo step)
    cell(k, C_NAME).setText("Analyst name")
    for rid, src in ((a, "source1"), (b, "source2")):
        st = ws.runs[rid]
        assert any(i.name == "Analyst name" and i.manual for i in st.ident_set("FID").items), rid
    # the report box and a number are kept in the group, marked and logged
    k = min(range(len(page.rows)), key=lambda i: abs(page.rows[i]["rt"] - rt))
    cell(k, C_REPORT).setCheckState(Qt.Unchecked)
    k = min(range(len(page.rows)), key=lambda i: abs(page.rows[i]["rt"] - rt))
    assert page.rows[k]["report"] is False and "report" in page.rows[k]["edited"]
    cell(k, C_MEAN).setText("0,777")
    k = min(range(len(page.rows)), key=lambda i: abs(page.rows[i]["rt"] - rt))
    assert page.rows[k]["mean"] == pytest.approx(0.777)
    assert cell(k, C_MEAN).font().italic() and "Changed by the analyst" in cell(k, C_MEAN).toolTip()
    g = page.group()
    e = next(iter(g["edits"].values()))
    assert e["report"] is False and e["mean"] == pytest.approx(0.777)
    assert any(r.action == "Double determination changed" for r in ws.audit.records)
    # a text that is no number changes nothing
    cell(k, C_C1).setText("abc")
    assert "c1" not in next(iter(page.group()["edits"].values()))
    # undo: mean, report box, then the name
    win.a_undo.trigger()
    k = min(range(len(page.rows)), key=lambda i: abs(page.rows[i]["rt"] - rt))
    assert "mean" not in page.rows[k]["edited"]
    win.a_undo.trigger()
    k = min(range(len(page.rows)), key=lambda i: abs(page.rows[i]["rt"] - rt))
    assert page.rows[k]["report"] is True
    page.reset_all()
    assert not page.group().get("edits")


def test_double_determination_mirror_scales_to_view(qtbot, win, samples):
    import numpy as np
    from PySide6.QtCore import Qt
    _load(qtbot, win, samples, ["07_", "11_"])
    ws = win.ws
    a = next(s.id for s in ws.states() if s.name.startswith("07_"))
    b = next(s.id for s in ws.states() if s.name.startswith("11_"))
    win.loaded_samples.pairRequested.emit(a, b)
    page = win.replicates.duplicate
    assert len(page._traces) == 2
    # the solvent front is not drawn and B is drawn downwards in its own units
    (rt_a, y_a), (rt_b, y_b) = page._traces
    assert rt_a[0] > float(ws.runs[a].run.signal("FID").rt[0]) and y_b.max() <= 1e-9 < y_a.max()
    # a substance picked in the table: the intensity fits the peaks in the ±0.4 min window
    k = min((i for i, r in enumerate(page.rows) if r.get("source1") and r.get("source2")),
            key=lambda i: page.rows[i].get("mean") or 1e9)
    r = next(i for i in range(page.table.rowCount()) if page.table.item(i, 0).data(Qt.UserRole) == k)
    page.table.setCurrentCell(r, 2)
    qtbot.waitUntil(lambda: page.mirror.getViewBox().viewRange()[0][1] - page.mirror.getViewBox().viewRange()[0][0] < 1)
    qtbot.wait(20)
    (x0, x1), (lo, hi) = page.mirror.getViewBox().viewRange()
    rt = page.rows[k]["rt"]
    assert x0 < rt < x1 and x1 - x0 < 1.0
    local = max(float(np.abs(y[(t >= x0) & (t <= x1)]).max()) for t, y in page._traces)
    assert hi == pytest.approx(1.12 * local, rel=1e-6) and lo == pytest.approx(-hi)
    assert hi < 0.5 * max(float(y_a.max()), float(-y_b.min()))     # zoomed in, not the whole run
    page.full_view()
    assert page.mirror.getViewBox().viewRange()[0][0] <= rt_a[0] + 0.05


def test_double_determination_sheet_keys_and_fill(qtbot, win, samples):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QTableWidgetSelectionRange
    from gcws.ui.docks.duplicate import C_NAME, C_REPORT
    _load(qtbot, win, samples, ["07_", "11_"])
    ws = win.ws
    a = next(s.id for s in ws.states() if s.name.startswith("07_"))
    b = next(s.id for s in ws.states() if s.name.startswith("11_"))
    win.loaded_samples.pairRequested.emit(a, b)
    page = win.replicates.duplicate
    t = page.table
    t.sortItems(2)                                # by RT

    def k_of(r):
        return t.item(r, 0).data(Qt.UserRole)

    def rows_by_rt(rts):
        return [next(r for r in range(t.rowCount()) if page.rows[k_of(r)]["rt"] == rt) for rt in rts]
    both = [r for r in range(t.rowCount()) if page.rows[k_of(r)].get("source1") and page.rows[k_of(r)].get("source2")
            and page.rows[k_of(r)].get("report")]
    r0 = next(r for r in both if r + 1 in both and r + 2 in both)
    rts = [page.rows[k_of(r0 + i)]["rt"] for i in range(3)]
    stack = page._stack()
    n0 = stack.count()
    # Delete: two marked rows leave the report, as one undo step
    t.setFocus()
    t.setCurrentCell(r0, C_NAME)
    t.setRangeSelected(QTableWidgetSelectionRange(r0, C_NAME, r0 + 1, C_NAME), True)
    QTest.keyClick(t, Qt.Key_Delete)
    r = rows_by_rt(rts[:2])
    assert all(not page.rows[k_of(x)]["report"] for x in r)
    assert stack.count() == n0 + 1
    assert {(t.item(x, C_NAME).row()) for x in r} == {i.row() for i in t.selectedItems()}   # marking kept
    # Enter: back into the report
    QTest.keyClick(t, Qt.Key_Return)
    assert all(page.rows[k_of(x)]["report"] for x in rows_by_rt(rts[:2]))
    win.a_undo.trigger()
    win.a_undo.trigger()
    assert all(page.rows[k_of(x)]["report"] for x in rows_by_rt(rts[:2]))
    # Ctrl+D: the top name goes into the marked cells below (both determinations' peaks)
    def names(n=3):
        return [page.rows[k_of(x)]["name"] for x in rows_by_rt(rts)][:n]

    r = rows_by_rt(rts)
    t.clearSelection()
    t.item(r[0], C_NAME).setText("Fill name")            # an ordinary edit first
    qtbot.waitUntil(lambda: names(1) == ["Fill name"], timeout=20000)   # the rows follow the quantification
    r = rows_by_rt(rts)
    t.clearSelection()
    t.setRangeSelected(QTableWidgetSelectionRange(r[0], C_NAME, r[2], C_NAME), True)
    QTest.keyClick(t, Qt.Key_D, Qt.ControlModifier)
    for rid in (a, b):
        assert sum(1 for i in ws.runs[rid].ident_set("FID").items if i.name == "Fill name") >= 3
    qtbot.waitUntil(lambda: names() == ["Fill name"] * 3, timeout=20000)
    win.a_undo.trigger()
    qtbot.waitUntil(lambda: names()[1:] != ["Fill name"] * 2, timeout=20000)
    # Ctrl+V: one copied value into every marked cell; Ctrl+C gives tab-separated text
    r = rows_by_rt(rts)
    t.clearSelection()
    t.setRangeSelected(QTableWidgetSelectionRange(r[1], C_NAME, r[2], C_NAME), True)
    QGuiApplication.clipboard().setText("Pasted")
    QTest.keyClick(t, Qt.Key_V, Qt.ControlModifier)
    qtbot.waitUntil(lambda: names()[1:] == ["Pasted", "Pasted"], timeout=20000)
    r = rows_by_rt(rts)
    t.clearSelection()
    t.setRangeSelected(QTableWidgetSelectionRange(r[1], C_REPORT, r[2], C_NAME), True)
    QTest.keyClick(t, Qt.Key_C, Qt.ControlModifier)
    assert QGuiApplication.clipboard().text().split(chr(10))[0].endswith(chr(9) + "Pasted")
    # the fill handle: drag the small square of a marked cell two rows down
    t.clearSelection()
    t.setRangeSelected(QTableWidgetSelectionRange(r[0], C_NAME, r[0], C_NAME), True)
    t.scrollToItem(t.item(r[0], C_NAME))
    h = t._handle_rect()
    assert h is not None
    vp = t.viewport()
    target = t.visualItemRect(t.item(r[2], C_NAME)).center()
    QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, h.center())
    QTest.mouseMove(vp, QPoint(h.center().x(), (h.center().y() + target.y()) // 2))
    QTest.mouseMove(vp, QPoint(h.center().x(), target.y()))
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, QPoint(h.center().x(), target.y()))
    qtbot.waitUntil(lambda: names() == ["Fill name"] * 3, timeout=20000)


def test_migration_dialog_derives_cell_and_occupancy(qtbot, win, monkeypatch):
    import copy
    from PySide6.QtWidgets import QMessageBox
    from gcws.quant import migration as MG
    from gcws.quant.nias_bridge import make_settings, settings_dict
    from gcws.ui.docks.quant import MigrationDialog
    assert MG.cell_text(0.51) == "Zelle groß (0.51 dm²)" and MG.cell_text("0,34") == "Zelle klein (0.34 dm²)"
    assert MG.cell_text(0.6) == "Zelle (0.6 dm²)" and MG.occupancy_text(2) == "doppelt"
    assert MG.occupancy_text(1.5) == "1.5-fach"
    ws = win.ws
    s = make_settings(ws.quant.get("settings"))
    s.cell_area_dm2, s.coverage = 0.34, 2.0          # as the parameter table has them now
    stale = {"cell_area_dm2": 0.51, "occupancy_factor": 1.0, "analyst": "Old", "simulant": "EtOH 20%"}
    dlg = MigrationDialog(stale, s, win)
    qtbot.addWidget(dlg)
    assert "migration_cell" not in dlg.edits and "occupancy" not in dlg.edits
    assert dlg.edits["cell_area_dm2"].text() == "0.34"            # the parameters, not the stale values
    assert "Zelle klein (0.34 dm²), doppelt" in dlg.derived.text()
    assert dlg.edits["simulant"].currentText() == "EtOH 20%"
    sim = dlg.edits["simulant"]
    sim.setCurrentIndex(sim.findText(MG.OTHER))
    sim.activated.emit(sim.currentIndex())
    assert sim.currentText() == ""
    sim.setEditText("Isooctane")
    for k, v in (("temperature", "40 °C"), ("duration", "10 d"), ("volume_ml", "100"), ("ov_ratio", "6")):
        dlg.edits[k].setText(v)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a)))
    dlg._ok()
    assert not warned and dlg.result() == 1
    m = dlg.metadata
    assert m["simulant"] == "Isooctane" and m["migration_cell"] == "Zelle klein (0.34 dm²)" and m["occupancy"] == "doppelt"
    assert m["effective_area_dm2"] == pytest.approx(0.68)
    # the report takes the current parameters even when they changed after the dialog
    q = copy.deepcopy(ws.quant)
    q["migration"] = m
    s.cell_area_dm2 = 0.44
    q["settings"] = settings_dict(s)
    cur = MG.current(q)
    assert cur["cell_area_dm2"] == pytest.approx(0.44) and cur["migration_cell"] == "Glaszelle (0.44 dm²)"
    assert MG.current({}) == {}



def test_edit_library_new_entry_takes_a_spectrum(qtbot, win, samples, tmp_path, monkeypatch):
    from gcws import paths
    from gcws.identify import library_edit as LE
    from gcws.libsearch import service, store
    from gcws.ui.dialogs.library_edit import EditLibraryDialog
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    monkeypatch.setattr(LE, "find_lib2nist", lambda *a, **k: None)
    service.reset()
    dlg = EditLibraryDialog(win, {})
    qtbot.addWidget(dlg)
    assert dlg.current_library() is None and not dlg.add_btn.isEnabled()
    # a new MSP library (no SpectrAtlas, no Lib2NIST needed)
    from PySide6.QtWidgets import QInputDialog
    with monkeypatch.context() as m:
        m.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("My spectra", True)))
        dlg.new_library()
    assert dlg.current_library().name == "My spectra" and not dlg.add_btn.isEnabled()   # no spectrum yet
    # the spectrum: from an MSP file with several records (the analyst picks one)
    src = tmp_path / "in.msp"
    src.write_text(LE.write_msp([LE.new_record("First", [(43, 999), (58, 400)], cas="67-64-1"),
                                 LE.new_record("Second", [(149, 999), (167, 320), (279, 80)], cas="84-74-2",
                                               formula="C16H22O4")]), encoding="cp1252", newline="")
    with monkeypatch.context() as m:
        m.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("2: Second", True)))
        dlg.import_msp(str(src))
    assert dlg.name.text() == "Second" and dlg.cas.text() == "84-74-2" and dlg.mw.text() == "278"
    assert [m for m, _ in dlg._peaks] == [149, 167, 279] and dlg.add_btn.isEnabled()
    # pasted pairs and typed ions replace it
    dlg.paste_spectrum("91 999\n92 610\n65 120")
    assert [m for m, _ in dlg._peaks] == [65, 91, 92]
    assert LE.parse_spectrum_text("Num Peaks: 2\n91 999; 92 500") == [(91.0, 999.0), (92.0, 500.0)]
    assert LE.parse_spectrum_text("91,999\n92,5 500") == [(91.0, 999.0), (92.5, 500.0)]
    dlg.paste_spectrum("no numbers here")
    assert [m for m, _ in dlg._peaks] == [65, 91, 92]
    # the current spectrum of the Mass spectrum panel, while the dialog is open
    _load(qtbot, win, samples, ["07_"])
    ws = win.ws
    ws.set_signal_key("TIC")
    res = ws.result(ws.active_id, "TIC")
    ws.select_peak(max(range(len(res.peaks)), key=lambda i: res.peaks[i].area if 20 < res.peaks[i].apex_rt < 26 else 0))
    dlg.name.setText("From the run")
    dlg.take_current()
    assert len(dlg._peaks) > 5 and dlg.rt.text() and "07_" in dlg.source.text()
    dlg.save_entry()
    qtbot.waitUntil(lambda: not dlg._busy, timeout=20000)
    lib = tmp_path / "data" / "libraries" / "My spectra.msp"
    assert [r.name for r in LE.parse_msp(lib.read_text(encoding="cp1252"))] == ["From the run"]
    # the new library is one of the analyst's libraries and searchable at once
    assert [s.name for s in store.load()] == ["My spectra"]
    hits = service.analyze(dlg.trimmed_peaks(), "q", {"lite": True, "libraries": ["My spectra"]})["hits"]
    assert hits[0]["name"] == "From the run"
    dlg.close()
    ws.dirty = False
    service.reset()


def test_processing_method_save_and_load(qtbot, win, samples, tmp_path, monkeypatch):
    from gcws.ms import deconv as D, deconv_cache as DC
    import copy
    from PySide6.QtCore import QSettings
    from gcws import paths
    from gcws.core import proc_method as PM
    from gcws.signal.blank import BlankOptions
    from gcws.ui.dialogs.own_search import load_options, save_options
    from gcws.ui.dialogs.proc_method import LoadMethodDialog, SaveMethodDialog
    from gcws.ui.undo import SetMethodCommand
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    _load(qtbot, win, samples, ["07_"])
    ws = win.ws
    ws.methods.folder = tmp_path / "data" / "methods"          # never the shared test data folder
    st = ws.active
    # the analyst's settings: an own FID method, blank options, report and own-library options
    fid = ws.method_for(st, "FID").copy()
    fid.name, fid.min_sn = "My FID", 7.0
    st.undo.push(SetMethodCommand(ws, [st.id], "FID", fid, "own FID method"))
    q = copy.deepcopy(ws.quant)
    q["blank_sub"] = BlankOptions(scale=1.5).to_dict()
    nias_settings = D.DeconvSettings(shape_r=0.95, noise_factor=4)
    q["deconv"] = nias_settings.to_dict()
    ws.push_quant("blank", q)
    win.a_keep_middle.setChecked(True)
    save_options({"library": "Own", "top_n": 7})
    dlg = SaveMethodDialog(win)
    qtbot.addWidget(dlg)
    dlg.name.setEditText("NIAS EtOH")
    dlg.comment.setPlainText("test method")
    dlg._save()
    assert dlg.saved.is_file() and PM.names() == ["NIAS EtOH"]
    # everything changed afterwards ...
    st.undo.push(SetMethodCommand(ws, [st.id], "FID", ws.methods.get(ws.methods.default_name("FID")), "built-in"))
    q = copy.deepcopy(ws.quant)
    q["blank_sub"] = BlankOptions(scale=1.0).to_dict()
    q["deconv"] = D.DeconvSettings().to_dict()
    ws.push_quant("blank", q)
    win.a_keep_middle.setChecked(False)
    save_options({"library": "", "top_n": 10})
    # ... and the method brings it back, except the part the analyst leaves out
    load = LoadMethodDialog(win)
    qtbot.addWidget(load)
    assert load.method["name"] == "NIAS EtOH" and "test method" in load.details.text()
    assert all(load.checks[k].isEnabled() for k in PM.SECTIONS)
    load.checks["own_search"].setChecked(False)
    load._load()
    assert "own_search" not in load.applied and "integration" in load.applied
    assert ws.method_for(st, "FID").name == "My FID" and ws.method_for(st, "FID").min_sn == 7.0
    assert ws.blank_options().scale == 1.5 and win.a_keep_middle.isChecked()
    assert DC.settings_of(ws) == nias_settings
    assert QSettings().value("report/keep_middle", False, type=bool)
    assert load_options()["library"] == ""                        # left out
    assert ws.methods.default_name("FID") == "My FID"              # runs loaded later start with it
    assert any(r.action == "Processing method loaded" for r in ws.audit.records)
    # one undo step takes the workspace settings and the integration back
    win.a_undo.trigger()
    assert ws.blank_options().scale == 1.0 and ws.method_for(st, "FID").name != "My FID"
    assert DC.settings_of(ws) == D.DeconvSettings()
    # export / import / delete
    load.export_file(str(tmp_path / "shared.json"))
    PM.delete("NIAS EtOH")
    assert PM.names() == []
    load.import_file(str(tmp_path / "shared.json"))
    assert PM.names() == ["NIAS EtOH"]
    ws.methods.set_default("FID", None)


def _contrast(a, b) -> float:
    from PySide6.QtGui import QColor

    def lum(c):
        c = QColor(c)
        ch = []
        for v in (c.redF(), c.greenF(), c.blueF()):
            ch.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_theme_menu(win):
    """The themes live in the Layout menu (one checked at a time), no longer in View."""
    from gcws.ui import theme
    layout = next(a.menu() for a in win.menuBar().actions() if a.text() == "&Layout")
    labels = [a.text() for a in layout.actions()]
    assert all(label in labels for label in ("Light Mode", "Dark Mode", "Dark Mode - Neon"))
    assert not any("dark" in a.text().lower() for a in win.view_menu.actions())
    assert win.theme_group.isExclusive() and win.theme_actions[theme.MODE].isChecked()
    assert win.a_next_theme.shortcut().toString() == "Ctrl+Shift+D"


def test_saved_theme_reads_the_old_dark_switch(win):
    from PySide6.QtCore import QSettings
    from gcws.ui import theme
    s = QSettings()
    assert theme.saved_theme() == "light"
    s.setValue("prefs/dark_mode", True)                 # set by earlier versions
    assert theme.saved_theme() == "dark"
    s.setValue("prefs/theme", "neon")
    assert theme.saved_theme() == "neon"
    s.setValue("prefs/theme", "no such theme")
    assert theme.saved_theme() == "dark"


@pytest.mark.parametrize("name", ["dark", "neon"])
def test_dark_mode(qtbot, win, samples, tmp_path, name):
    from PySide6.QtCore import QSettings, QSize
    from PySide6.QtGui import QColor, QImage, QPalette
    from PySide6.QtWidgets import QApplication
    from gcws.ui import icons, theme
    from gcws.ui.dialogs import export_chrom as E
    app = QApplication.instance()
    tokens = theme.THEMES[name]
    _load(qtbot, win, samples, ["07_"])
    st = win.ws.active
    light_color = st.color
    before = icons.icon("zoom").pixmap(QSize(32, 32)).toImage()
    try:
        win.theme_actions[name].trigger()
        assert theme.MODE == name and theme.is_dark() and QSettings().value("prefs/theme") == name
        assert win.theme_actions[name].isChecked() and not win.theme_actions["light"].isChecked()
        pal = app.palette()
        assert pal.color(QPalette.Window).name().lower() == tokens["BG"].lower()
        assert tokens["ACCENT"].lower() in app.styleSheet().lower()
        # every text stays readable, buttons included (WCAG AA: 4.5:1)
        for fg, bg in ((theme.TEXT, theme.BG), (theme.TEXT, theme.SURFACE), (theme.TEXT, theme.SURFACE_ALT),
                       (theme.MUTED, theme.BG), (theme.MUTED, theme.SURFACE), (theme.ACCENT_TEXT, theme.ACCENT_SOFT),
                       (theme.ACCENT_TEXT, theme.ACCENT_SOFT2), (theme.ON_ACCENT, theme.ACCENT),
                       (theme.ON_ACCENT, theme.ACCENT_HOVER), (theme.TEXT, theme.ACCENT_SOFT2),
                       (theme.PLOT["fg"], theme.PLOT["bg"]), (theme.PLOT["label"], theme.PLOT["bg"])):
            assert _contrast(fg, bg) >= 4.5, (fg, bg)
        for level in ("ok", "warn", "bad", "info", "neutral"):
            fg, soft = theme.LEVELS[level]
            assert _contrast(fg, soft) >= 4.5 and _contrast(fg, theme.SURFACE) >= 4.5, level
        assert _contrast(theme.BORDER_STRONG, theme.SURFACE) >= 1.8          # button outlines are visible
        # plots, icons and run colours follow; only the neon chromatograms glow
        assert win.chrom.plot.backgroundBrush().color().name().lower() == tokens["PLOT"]["bg"].lower()
        assert st.color == tokens["RUN_COLORS"][theme.LIGHT["RUN_COLORS"].index(light_color)]
        assert icons.icon("zoom").pixmap(QSize(32, 32)).toImage() != before
        curve = win.chrom.curves[st.id]
        assert curve.opts["pen"].color().name().lower() == st.color.lower()
        shadow = curve.opts.get("shadowPen")
        if name == "neon":
            assert shadow is not None and shadow.color().name().lower() == st.color.lower()
            assert shadow.widthF() > curve.opts["pen"].widthF()
        else:
            assert shadow is None
        # a picture for a report stays white, its traces in the light colours without the halo
        out = E.export([win.chrom], tmp_path / f"{name}.png", 600, 200)
        img = QImage(str(out))
        assert img.pixelColor(3, img.height() - 3).lightness() > 240
        themed, paper = QColor(st.color), QColor(light_color)

        def near(c, ref):
            return abs(c.red() - ref.red()) + abs(c.green() - ref.green()) + abs(c.blue() - ref.blue()) < 12
        pixels = [img.pixelColor(x, y) for x in range(img.width()) for y in range(img.height())]
        assert sum(near(c, paper) for c in pixels) > 5 * sum(near(c, themed) for c in pixels) + 50
        if name == "neon":                  # no blend of the light trace comes near a neon colour
            assert not any(near(c, themed) for c in pixels)
        assert theme.MODE == name and win.chrom.plot.backgroundBrush().color().name().lower() == \
            tokens["PLOT"]["bg"].lower()
        assert curve.opts["pen"].color().name().lower() == st.color.lower()       # restored after the export
        assert (curve.opts.get("shadowPen") is not None) == (name == "neon")
    finally:
        win.theme_actions["light"].trigger()
    assert not theme.is_dark() and st.color == light_color
    assert win.chrom.curves[st.id].opts.get("shadowPen") is None
    assert app.palette().color(QPalette.Window).name().lower() == theme.LIGHT["BG"].lower()
    for fg, bg in ((theme.TEXT, theme.SURFACE), (theme.ACCENT_TEXT, theme.ACCENT_SOFT), (theme.ON_ACCENT, theme.ACCENT)):
        assert _contrast(fg, bg) >= 4.5
