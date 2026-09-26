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


def _load(qtbot, w, samples, prefixes):
    paths = [str(next(samples.glob(p + "*.D"))) for p in prefixes]
    w.load_runs(paths)
    qtbot.waitUntil(lambda: w.loading == 0 and len(w.ws.runs) == len(paths), timeout=60000)


def test_load_tabs_and_overlay(qtbot, win, samples):
    _load(qtbot, win, samples, ["08_", "07_"])
    names = [win.run_tabs.tabText(i) for i in range(win.run_tabs.count())]
    assert names[0].startswith("07_26016606_130m_min_GIOSUN1635_A")      # injection order
    assert "[Blank]" in names[1]
    assert len(win.chrom.curves) == 2
    win.run_tabs.setCurrentIndex(1)
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
    from gcws.ui.layout.title_bar import DockTitleBar
    qtbot.waitUntil(lambda: win.docks["chrom"].isVisible(), timeout=5000)
    for d in win.docks.values():
        bar = d.titleBarWidget()
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
    win.run_tabs.pairRequested.emit(a, b)
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
    assert dlg.comps and dlg.table.rowCount() == len(dlg.comps)
    assert any(abs(c.rt - 13.409) < 0.01 for c in dlg.comps)
    # pin a component's spectrum to the peak
    dlg.table.selectRow(0)
    dlg.pin()
    st = ws.active
    assert "component" in st.spectrum_overrides[round(res.peaks[idx].apex_rt, 4)]
    assert win.spectrum.spec.mode == "deconvoluted"
    # whole run in the background -> cached, hidden components marked in the chromatogram
    dlg.scope.button(2).setChecked(True)
    dlg.run()
    from gcws.ms import deconv_cache as DC
    qtbot.waitUntil(lambda: DC.whole_run(st, dlg.settings()) is not None, timeout=60000)
    assert len(DC.whole_run(st, dlg.settings())) > 50
    dlg.save_default()
    assert DC.whole_run(st, DC.settings_of(ws)) is not None or ws.quant.get("deconv")
    # the "deconvoluted" spectrum mode uses the new engine
    win.spectrum._clear_override()
    i = win.spectrum.mode.findData("deconvoluted")
    win.spectrum.mode.setCurrentIndex(i)
    assert win.spectrum.spec.mode == "deconvoluted" and "quality" in win.spectrum.spec.note
    win.spectrum.mode.setCurrentIndex(0)


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
