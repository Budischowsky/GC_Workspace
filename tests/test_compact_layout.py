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


def test_presets_give_work_panels_room(qtbot, win):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from gcws.ui.layout import presets
    for name in presets.PRESETS:
        win.apply_preset(name)
        QApplication.processEvents()
        left = [d for d in win.docks.values() if not d.isHidden() and not d.isFloating()
                and win.dockWidgetArea(d) == Qt.LeftDockWidgetArea]
        assert len(left) <= 4, (name, [d.windowTitle() for d in left])
        if name.startswith("Dual"):
            continue
        group = win.tabifiedDockWidgets(win.docks["table"])
        for key in ("quant", "replicates", "report2"):
            assert win.docks[key] in group, (name, key)
    win.apply_preset("Review")
    QApplication.processEvents()
    rep = win.docks["replicates"]
    assert rep.isVisible() and rep.width() > 0.4 * win.width()
    win.apply_preset("Chromatogram top")


def test_tabbed_panels_show_a_slim_bar(qtbot, win):
    from PySide6.QtWidgets import QApplication
    from gcws.ui.layout.title_bar import title_bar
    win.apply_preset("Chromatogram top")
    QApplication.processEvents()
    qtbot.wait(20)
    table, spec = title_bar(win.docks["table"]), title_bar(win.docks["spectrum"])
    props = title_bar(win.docks["props"])
    assert table.tabbed and props.tabbed and not spec.tabbed          # the rail of a plot panel stays
    assert table.label.isHidden() and table.b_close.isVisible() and table.b_max.isVisible()
    normal = table.sizeHint().height()
    win.docks["props"].setFloating(True)
    QApplication.processEvents()
    qtbot.wait(20)
    assert not props.tabbed and not props.label.isHidden()
    assert props.sizeHint().height() > normal
    win.docks["props"].setFloating(False)
    win.apply_preset("Chromatogram top")


def test_layout_menu_current_reset_update_and_confirmations(qtbot, win, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox
    from gcws.ui.layout import presets
    win.apply_preset("Review")
    win._update_layout_menu()
    assert win.preset_actions["Review"].isChecked() and not win.preset_actions["Chromatogram top"].isChecked()
    assert win.a_reset_layout.isEnabled() and not win.a_update_layout.isEnabled()
    # reset puts a moved panel back
    win.addDockWidget(Qt.LeftDockWidgetArea, win.docks["replicates"])
    win.reset_layout()
    assert win.docks["replicates"] in win.tabifiedDockWidgets(win.docks["table"])
    # save: asks only when the name exists; "No" keeps the old one
    asked = []
    with monkeypatch.context() as mp:
        mp.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("mine", True)))
        mp.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.No))
        win.save_layout()
        assert asked == [] and presets.current() == ("saved", "mine")
        win.apply_preset("Integration")
        win.save_layout()
        assert asked == [1] and presets.current() == ("preset", "Integration")
    win.restore_saved_layout("mine")
    win._update_layout_menu()
    win._fill_layouts()
    assert win.a_update_layout.isEnabled() and not any(a.isChecked() for a in win.preset_actions.values())
    assert [a.isChecked() for a in win.saved_layouts_menu.actions()] == [True]
    win.update_layout()
    # delete: "No" keeps it, "Yes" removes it and the tick
    win.delete_saved_layout("mine")
    assert "mine" in presets.saved_layouts()
    with monkeypatch.context() as mp:
        mp.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        win.delete_saved_layout("mine")
    assert "mine" not in presets.saved_layouts() and presets.current() is None
    win._update_layout_menu()
    assert not win.a_reset_layout.isEnabled()
    win.apply_preset("Chromatogram top")


def test_lock_and_docking_suggestion_are_remembered(qtbot, win):
    from PySide6.QtWidgets import QDockWidget
    from gcws.ui.main_window import MainWindow
    win.a_lock_panels.setChecked(True)
    win.a_suggest_docking.setChecked(False)
    other = MainWindow()
    qtbot.addWidget(other)
    try:
        assert other.a_lock_panels.isChecked() and not other.a_suggest_docking.isChecked()
        assert not other.overlay.enabled
        assert not other.docks["table"].features() & QDockWidget.DockWidgetMovable
    finally:
        other.a_lock_panels.setChecked(False)
        other.a_suggest_docking.setChecked(True)
        other.close()
    win.a_lock_panels.setChecked(False)


def test_layout_saved_before_a_panel_existed_puts_it_in_its_group(qtbot, win):
    from PySide6.QtCore import QByteArray, QSettings, Qt
    from PySide6.QtWidgets import QApplication
    from gcws.ui.layout import presets
    win.apply_preset("Chromatogram top")
    QApplication.processEvents()
    old = bytes(win.saveState(presets.LAYOUT_VERSION))
    old = old.replace("dock.report2".encode("utf-16-be"), "dock.zzzzzzz".encode("utf-16-be"))   # unknown then
    QSettings().setValue("layouts/old/state", QByteArray(old))
    report2, table = win.docks["report2"], win.docks["table"]
    win.addDockWidget(Qt.LeftDockWidgetArea, report2)        # where Qt alone would leave it
    QApplication.processEvents()
    assert presets.restore_layout(win, "old")
    QApplication.processEvents()
    assert report2 in win.tabifiedDockWidgets(table) and not report2.isFloating()
    assert table.isVisible()                                # the panel in front stays in front
    assert presets.place_missing(win, win.saveState(presets.LAYOUT_VERSION)) == []


def test_detached_panel_off_every_screen_comes_back(qtbot, win):
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication
    from gcws.ui.layout import presets
    audit = win.docks["audit"]
    audit.setFloating(True)
    audit.setGeometry(-20000, -20000, 500, 300)
    QApplication.processEvents()
    presets.ensure_on_screen(win)
    QApplication.processEvents()
    frame = audit.frameGeometry()
    assert any(frame.intersects(s.availableGeometry()) for s in QGuiApplication.screens())
    audit.setFloating(False)
    win.apply_preset("Chromatogram top")


def test_sample_codes():
    from gcws.ui.layout.sample_rail import sample_codes
    assert sample_codes(["06_EtOH_ISTD.D", "07_26016606_130m_min_GIOSUN1635_A.D", "11_x_B.D"]) == ["06", "07", "11"]
    assert sample_codes(["26012850_Sample1_A.qgd", "EtOH.D", "EtOH_2.D", "7-x.D", "07_y.D"]) == \
        ["SA", "Et", "Etb", "07", "07b"]


def test_collapsed_folders_show_loaded_samples_as_squares(qtbot, win, samples, monkeypatch):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QMenu
    from gcws.ui import run_tabs
    _load(qtbot, win, samples, ["08_", "07_"], process=False)
    win.sidebar.collapse()
    rail = win.sidebar.rail
    assert rail.isVisible() and win.sidebar.strip.width() < 45
    order = [rail.box.itemAt(i).widget() for i in range(rail.box.count() - 1)]
    assert [s.text() for s in order] == ["07", "08"]
    assert [s.run_id for s in order] == win.ws.order
    blank = order[1]
    assert blank.role == "blank" and "Blank" in blank.toolTip()
    qtbot.mouseClick(blank, Qt.LeftButton)
    assert win.ws.active_id == blank.run_id and blank.active and not order[0].active
    shown = []

    class Menu(QMenu):
        def exec(self, *_):
            shown.append({a.text() for a in self.actions()})
    monkeypatch.setattr(run_tabs, "QMenu", Menu)
    blank.customContextMenuRequested.emit(QPoint(5, 5))
    assert shown and {"Role", "Close", "Rename sample..."} <= shown[0]
    win.loaded_samples.closeRequested.emit(blank.run_id)
    assert list(rail.squares) == [order[0].run_id]
    win.sidebar.expand()


def test_right_click_on_panel_title_or_tab_detaches(qtbot, win, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QApplication, QMenu, QTabBar
    from gcws.ui import main_window
    from gcws.ui.layout.title_bar import title_bar
    shown = []

    class Menu(QMenu):
        def exec(self, *_):
            shown.append(self)

    def pick(text):
        return next(a for a in shown[-1].actions() if a.text() == text)
    monkeypatch.setattr(main_window, "QMenu", Menu)
    win.apply_preset("Chromatogram top")
    QApplication.processEvents()
    qtbot.wait(20)
    table = win.docks["table"]
    bar = title_bar(table)
    QApplication.sendEvent(bar, QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), bar.mapToGlobal(QPoint(5, 5))))
    assert {"Maximize", "Detach", "Close"} <= {a.text() for a in shown[-1].actions()}
    pick("Detach").trigger()
    assert table.isFloating() and table.isVisible()
    QApplication.sendEvent(bar, QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), bar.mapToGlobal(QPoint(5, 5))))
    pick("Dock back").trigger()
    assert not table.isFloating()
    # the right-edge strip of a plot panel has the same menu
    rail = title_bar(win.docks["chrom"])
    QApplication.sendEvent(rail, QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), rail.mapToGlobal(QPoint(5, 5))))
    assert "Detach" in {a.text() for a in shown[-1].actions()}
    # a tab of a tab group: the menu of that tab's panel
    win.apply_preset("Chromatogram top")
    QApplication.processEvents()
    qtbot.wait(20)
    tabs = next(t for t in win.findChildren(QTabBar) if t.property("panelMenu")
                and "Quantification" in [t.tabText(i) for i in range(t.count())])
    i = [tabs.tabText(k) for k in range(tabs.count())].index("Quantification")
    pos = tabs.tabRect(i).center()
    QApplication.sendEvent(tabs, QContextMenuEvent(QContextMenuEvent.Mouse, pos, tabs.mapToGlobal(pos)))
    pick("Detach").trigger()
    assert win.docks["quant"].isFloating()
    # onto a given screen: centred there and no bigger than it
    screen = win.screen()
    win.detach_panel(win.docks["audit"], screen)
    g, frame = screen.availableGeometry(), win.docks["audit"].geometry()
    assert win.docks["audit"].isFloating() and g.contains(frame.center()) and frame.width() <= g.width()
    win.apply_preset("Chromatogram top")


def test_move_to_close_other_tabs_and_revealing_a_panel(qtbot, win, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMenu
    from gcws.ui import main_window
    from gcws.ui.layout.title_bar import title_bar
    shown = []

    class Menu(QMenu):
        def exec(self, *_):
            shown.append(self)
    monkeypatch.setattr(main_window, "QMenu", Menu)
    win.apply_preset("Chromatogram top")
    QApplication.processEvents()
    table, quant, spec = win.docks["table"], win.docks["quant"], win.docks["spectrum"]
    labels = {a.text(): a for a in win.panel_menu(table).actions()}
    assert "Close other tabs" in labels and "Move to" in labels
    labels["Close other tabs"].trigger()
    assert table.isVisible() and quant.isHidden() and win.docks["report2"].isHidden()
    move = {a.text(): a for a in labels["Move to"].menu().actions()}
    assert list(move) == ["Left side", "Right side", "Top", "Bottom"]
    move["Bottom"].trigger()
    assert win.dockWidgetArea(table) == Qt.BottomDockWidgetArea and not table.isFloating()
    assert "Close other tabs" not in {a.text() for a in win.panel_menu(spec).actions()}
    # a menu command reveals a closed panel: it lights up briefly
    win._show_dock("quant")
    bar = title_bar(quant)
    assert quant.isVisible() and bar.property("flash")
    qtbot.waitUntil(lambda: not bar.property("flash"), timeout=2000)
    win._show_dock("quant")                     # already in front: no flash
    assert not bar.property("flash")
    # while another panel is maximized, the layout comes back first
    win.toggle_maximize(spec)
    assert win.docks["chrom"].isHidden()
    win._show_dock("props")
    assert win._maximized is None and win.docks["chrom"].isVisible() and win.docks["props"].isVisible()
    win.apply_preset("Chromatogram top")
