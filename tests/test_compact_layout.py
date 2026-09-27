"""Plot space, moved controls and sample/sidebar behavior."""
from test_ui import win, _load


def test_menu_order_and_plot_geometry(qtbot, win):
    from PySide6.QtWidgets import QToolBar
    from gcws.ui.layout.title_bar import title_bar
    qtbot.waitUntil(lambda: win.chrom.width() > 100)
    names = [a.text().replace("&", "") for a in win.menuBar().actions()]
    i = names.index("View")
    assert names[i:i + 3] == ["View", "Chromatogramm", "Mass Spectrum"]
    assert not any(t.objectName() == "tb.runs" for t in win.findChildren(QToolBar))
    for key, panel in (("chrom", win.chrom), ("zoom", win.chrom2), ("spectrum", win.spectrum)):
        dock = win.docks[key]
        bar = title_bar(dock)
        assert bar.vertical and bar.width() == 30
        assert bar.x() >= panel.geometry().right()
        assert panel.y() == 0
        assert bar.height() == panel.height()
    assert win.chrom.plot.y() == 0
    assert not win.spectrum.tabs.isHidden()
    assert win.spectrum.plot.y() == 0
    overlay = win.chrom.controls
    assert overlay.parentWidget() is win.chrom.plot.viewport()
    assert overlay.y() >= 0 and overlay.geometry().bottom() < win.chrom.plot.height()


def test_solvent_menu_commit_undo_and_independent_blanks(qtbot, win, samples):
    from PySide6.QtCore import Qt
    _load(qtbot, win, samples, ["07_", "08_"])
    ws, menu = win.ws, win.chrom_menu
    menu.end.setFocus()
    menu.end.lineEdit().selectAll()
    qtbot.keyClicks(menu.end.lineEdit(), "6.125")
    assert (ws.quant.get("settings") or {}).get("solvent_end", 5.5) == 5.5
    qtbot.keyClick(menu.end, Qt.Key_Return)
    assert ws.quant["settings"]["solvent_end"] == 6.125
    win.a_undo.trigger()
    assert menu.end.value() == 5.5
    menu.cut.trigger()
    assert ws.quant["solvent_cut"] and win.chrom.cut.isChecked() and not win.chrom2.cut.isChecked()
    win.a_undo.trigger()
    assert not menu.cut.isChecked()
    menu.blanks[0].trigger()
    assert ws.panel_blank == [True, False]
    menu.blanks[1].trigger()
    assert ws.panel_blank == [True, True]
    ws.set_panels(["FID", "TIC"], [False, True])
    assert not menu.blanks[0].isChecked() and menu.blanks[1].isChecked()
    menu.end.lineEdit().selectAll()
    qtbot.keyClicks(menu.end.lineEdit(), "6,125")
    qtbot.keyClick(menu.end, Qt.Key_Return)
    assert ws.quant["settings"]["solvent_end"] == 6.125


def test_loaded_samples_order_rename_close_and_menu(qtbot, win, samples, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QInputDialog, QMenu
    from gcws.ui import run_tabs
    _load(qtbot, win, samples, ["08_", "07_"])
    items = win.loaded_samples
    assert items.count() == 2 and "[Blank]" in items.item(1).text()
    items.setCurrentRow(1)
    assert win.ws.active_id == items.item(1).data(Qt.UserRole)
    ids = list(reversed(win.ws.order))
    win.ws.reorder(ids)
    assert [items.item(i).data(Qt.UserRole) for i in range(items.count())] == ids
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Renamed sample", True))
    items._rename(ids[0])
    assert items.item(0).text().startswith("Renamed sample")
    items._visible(ids[0], False)
    assert not win.ws.runs[ids[0]].visible

    class Menu(QMenu):
        def exec(self, *_):
            labels = {a.text() for a in self.actions()}
            assert {"Role", "Assign blanks...", "Rename sample...", "Colour...", "Show in overlay",
                    "Show in folder tree", "Close", "Close others"} <= labels
    monkeypatch.setattr(run_tabs, "QMenu", Menu)
    items._menu(items.visualItemRect(items.item(0)).center())
    items.closeRequested.emit(win.ws.active_id)
    assert items.count() == 1
    assert items.currentItem().data(Qt.UserRole) == win.ws.active_id


def test_ms_shared_actions_scan_overlay_and_details(qtbot, win, samples):
    from PySide6.QtCore import Qt, QTimer
    from gcws.ms.spectra import ScanRequest
    _load(qtbot, win, samples, ["07_"])
    sp = win.spectrum
    sp.show_range(ScanRequest(win.ws.active_id, 13.0, 13.0))
    assert sp.spec is not None and sp.source == "scan"
    assert not hasattr(sp, "source_bar")
    assert sp.info.isHidden() and not hasattr(sp, "source_text") and not hasattr(sp, "b_back")
    assert not any(a.text().startswith(("Previous scan", "Next scan", "Back to peak", "Hide noise"))
                   for a in sp.context_menu.actions() + win.ms_menu.actions())
    from PySide6.QtWidgets import QLabel, QToolButton
    assert not any(b.text() == "Details" for b in sp.plot.findChildren(QToolButton))
    assert not any(label.text().lower() == "scan spectrum" for label in sp.plot.findChildren(QLabel))
    for action in sp.spectrum_actions:
        assert action in win.ms_menu.actions() and action in sp.context_menu.actions()
    old = sp.spec.apex_scans[0]
    sp.step(1)
    assert sp.spec.apex_scans[0] == old + 1
    with qtbot.waitSignal(sp.context_menu.aboutToShow):
        QTimer.singleShot(50, sp.context_menu.close)
        qtbot.mouseClick(sp.plot.viewport(), Qt.RightButton, pos=sp.plot.viewport().rect().center())
    sp.atlasRequested.disconnect()
    with qtbot.waitSignal(sp.atlasRequested):
        sp.spectrum_actions[0].trigger()
    assert sp.tabs.isVisible() and not sp.split.childrenCollapsible()
    assert sp.tabs.currentWidget() is sp.hits
    sp.back_to_peak()
    assert sp.source == "peak"


def test_sidebar_collapse_restore_and_named_layout(qtbot, win):
    from PySide6.QtCore import Qt
    from gcws.ui.layout import presets
    qtbot.waitUntil(lambda: win.docks["tree"].isVisible())
    qtbot.wait(50)
    tree = win.docks["tree"]
    win.docks["audit"].setFloating(True)
    floating = win.docks["audit"]
    width = tree.width()
    other_visibility = {key: not dock.isHidden() for key, dock in win.docks.items() if key != "tree"}
    win.sidebar.collapse()
    qtbot.wait(50)
    assert tree.isHidden() and win.sidebar.strip.isVisible()
    assert win.sidebar.strip.width() < 45
    assert floating.isVisible() and floating.isFloating()
    assert {key: not dock.isHidden() for key, dock in win.docks.items() if key != "tree"} == other_visibility
    win.spectrum.show_details()
    presets.save_layout(win, "compact")
    win.sidebar.expand()
    qtbot.wait(50)
    assert tree.isVisible() and abs(tree.width() - width) < 30
    assert presets.restore_layout(win, "compact")
    assert win.sidebar.collapsed and tree.isHidden() and win.spectrum.tabs.isVisible()
    win._show_dock("tree")
    assert not win.sidebar.collapsed and not tree.isHidden()
    win.docks["props"].hide()
    win.sidebar.collapse()
    assert win.docks["props"].objectName() not in win.sidebar.members
    win._show_dock("props")
    assert win.sidebar.collapsed and win.docks["props"].isVisible() and tree.isHidden()
    win._show_dock("tree")
    assert not win.sidebar.collapsed
    floating.setFloating(False)


def test_right_title_controls_lock_and_drag(qtbot, win):
    from PySide6.QtCore import Qt, QPoint
    from gcws.ui.layout.title_bar import title_bar
    dock = win.docks["chrom"]
    bar = title_bar(dock)
    visible = {k for k, d in win.docks.items() if d.isVisible()}
    bar.b_max.click()
    assert bar.maximized and win.docks["zoom"].isHidden()
    bar.b_max.click()
    assert {k for k, d in win.docks.items() if d.isVisible()} == visible
    bar.b_float.click()
    assert dock.isFloating()
    bar.b_float.click()
    assert not dock.isFloating()
    win._lock(True)
    assert bar.b_float.isHidden()
    qtbot.mousePress(bar, Qt.LeftButton, pos=QPoint(12, 30))
    qtbot.mouseMove(bar, QPoint(60, 60))
    qtbot.mouseRelease(bar, Qt.LeftButton, pos=QPoint(60, 60))
    assert not dock.isFloating()
    win._lock(False)
    qtbot.mousePress(bar, Qt.LeftButton, pos=QPoint(12, 30))
    qtbot.mouseMove(bar, QPoint(70, 80))
    qtbot.mouseRelease(bar, Qt.LeftButton, pos=QPoint(70, 80))
    assert dock.isFloating()
    dock.setFloating(False)
    bar.b_close.click()
    assert dock.isHidden()
    win._show_dock("chrom")
    assert dock.isVisible()


def test_overflow_and_export_excludes_controls(qtbot, win):
    from PySide6.QtWidgets import QApplication
    from gcws.ui.dialogs.export_chrom import image
    panel = win.chrom
    win.docks["chrom"].setFloating(True)
    win.docks["chrom"].resize(420, 240)
    QApplication.processEvents()
    panel.controls.reposition()
    assert panel.controls.overflowed and panel.controls.more.isVisible()
    panel.controls._fill_menu()
    assert panel.controls.menu.actions()
    before = image([panel], 600, 220)
    panel.controls.hide()
    after = image([panel], 600, 220)
    assert before == after
    panel.controls.show()
    win.docks["chrom"].setFloating(False)


def test_plot_drop_tabs_and_edge(qtbot, win):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication
    moving, target = win.docks["spectrum"], win.docks["chrom"]
    moving.setFloating(True)
    QApplication.processEvents()
    win.overlay.dock = moving
    win.overlay._update(target.mapToGlobal(target.rect().center()))
    assert win.overlay.target is target
    win.overlay._poll()
    assert not moving.isFloating() and moving in win.tabifiedDockWidgets(target)
    moving.setFloating(True)
    win.overlay.dock = moving
    geometry = win.geometry()
    win.overlay._update(QPoint(geometry.right() - 3, geometry.center().y()))
    win.overlay._poll()
    assert win.dockWidgetArea(moving) == Qt.RightDockWidgetArea and not moving.isFloating()


def test_session_and_obsolete_toolbar_restore(qtbot, win):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QToolBar
    from gcws.ui.layout import presets
    from gcws.ui.main_window import MainWindow
    # Simulate a previous version's saved sample toolbar row.
    old = QToolBar("Chromatograms", win)
    old.setObjectName("tb.runs")
    old.addAction("Old sample")
    win.addToolBarBreak(Qt.TopToolBarArea)
    win.addToolBar(Qt.TopToolBarArea, old)
    state = win.saveState(presets.LAYOUT_VERSION)
    win.removeToolBar(old)
    old.deleteLater()
    QApplication.processEvents()
    assert win.restoreState(state, presets.LAYOUT_VERSION)
    assert not any(t.objectName() == "tb.runs" and t.isVisible() for t in win.findChildren(QToolBar))
    win.spectrum.show_details()
    win.folder_split.setSizes([350, 300])
    win.sidebar.collapse()
    win.close()
    restored = MainWindow()
    qtbot.addWidget(restored)
    restored.show()
    QApplication.processEvents()
    assert restored.sidebar.collapsed and restored.docks["tree"].isHidden()
    assert restored.spectrum.tabs.isVisible()
    restored._show_dock("tree")
    assert not restored.sidebar.collapsed
    restored.close()


def test_legacy_collapsed_group_restores_other_panels(qtbot, win):
    from PySide6.QtCore import QSettings
    s = QSettings()
    docks = [win.docks[key] for key in ("tree", "events", "props")]
    for dock in docks:
        dock.hide()
    s.setValue("legacy/sidebar_collapsed", True)
    s.setValue("legacy/sidebar_members", [d.objectName() for d in docks])
    s.setValue("legacy/sidebar_widths", [250, 250, 250])
    win.sidebar.restore("legacy")
    assert docks[0].isHidden()
    assert not docks[1].isHidden() and not docks[2].isHidden()
    win.sidebar.save("migrated")
    assert s.value("migrated/sidebar_scope") == "folder"


def test_metadata_changes_do_not_recalculate_deconvolution(qtbot, win, samples, monkeypatch):
    from gcws.ms import deconv_cache as DC
    from gcws.ms.spectra import extract
    import numpy as np
    _load(qtbot, win, samples, ["07_"])
    ws, sp = win.ws, win.spectrum
    st = ws.active
    assert sp.current_mode() == "average_bg"
    ws.select_peak(min(range(len(ws.active_result().peaks)),
                       key=lambda i: abs(ws.active_result().peaks[i].apex_rt - 13.0)))
    peak = ws.selected_peak()
    expected = extract(st.run, peak, ws.signal_key, st.delay_value, "average_bg")
    np.testing.assert_array_equal(sp.spec.mz, expected.mz)
    np.testing.assert_array_equal(sp.spec.ab, expected.ab)
    calls = []
    original = DC.for_peak
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(DC, "for_peak", counted)
    sp.mode.setCurrentIndex(sp.mode.findData("deconvoluted"))
    assert len(calls) == 1
    before = sp.spec.ab.copy()
    st.run.meta.sample_name = "Metadata edit"
    ws.runChanged.emit(st.id)
    assert len(calls) == 1
    np.testing.assert_array_equal(sp.spec.ab, before)
