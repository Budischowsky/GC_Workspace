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
    win.tools.eventCreated.emit(ManualEvent(K.ADD_PEAK, 30.0, 30.2))
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


def test_project_roundtrip(qtbot, win, samples, tmp_path):
    from gcws.core.events import ManualEvent, ManualKind as K
    from gcws.core.ident import Identification
    _load(qtbot, win, samples, ["07_", "08_"])
    st = next(s for s in win.ws.states() if s.name.startswith("07_"))
    win.ws.set_active(st.id)
    win.tools.eventCreated.emit(ManualEvent(K.SPLIT, 13.42))
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


def test_dual_fid_ms_view(qtbot, win, samples):
    _load(qtbot, win, samples, ["07_", "08_"])
    chrom = win.chrom
    chrom.dual.setChecked(True)
    assert chrom.dual_on() and chrom.companion.isVisible()
    comp = chrom.companion
    assert comp.key == "TIC" and len(comp.curves) == 2 and len(chrom.curves) == 2
    st = win.ws.active
    xs = comp.curves[st.id].xData
    tic = st.run.signal("TIC")
    assert abs(xs[0] - (tic.rt[0] + st.delay_value)) < 1e-9       # MS shifted onto the FID axis
    assert chrom.split.indexOf(chrom.plot) == 0                   # FID on top
    chrom.vb.setXRange(13.2, 14.2, padding=0)
    assert comp.vb.viewRange()[0] == pytest.approx(chrom.vb.viewRange()[0])
    # a click in the companion selects the FID peak at that time
    res = win.ws.active_result()
    p = max(res.peaks, key=lambda q: q.area if 13.2 < q.apex_rt < 14.2 else 0)
    comp._clicked(p.apex_rt, 0.0)
    assert win.ws.selected_peak() is p
    # MS working signal: the FID moves to the top pane, shifted back
    win.ws.set_signal_key("TIC")
    assert comp.key == "FID" and chrom.split.indexOf(comp) == 0
    xs = comp.curves[st.id].xData
    assert abs(xs[0] - (st.run.fid.rt[0] - st.delay_value)) < 1e-9
    chrom.dual.setChecked(False)
    assert not comp.isVisible()


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
    # clicking an ion shows its EIC below the FID
    ws.set_signal_key("FID")
    win.chrom.dual.setChecked(True)
    win.show_ion_eic(441)
    assert win.chrom.companion.key == "EIC 441"
