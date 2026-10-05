"""P61: the Automation panel and the workflow chart editor (no dialog is ever left open)."""
import pytest

from test_ui import win  # noqa: F401  (fixture)


@pytest.fixture()
def data(tmp_path, monkeypatch):
    from gcws import paths
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(paths, "DATA", d)
    return d


class NoWatcher:
    def status(self, journal=None):
        return {"state": "stopped", "heartbeat": None, "current": ""}

    def send(self, cmd, timeout=0):
        return None

    def start(self, paused=False):
        return True


def test_dock_is_listed_and_shows_the_watcher_state(qtbot, win):
    assert "automation" in win.docks
    assert any(a.text() == "Automation" for a in win.view_menu.actions())
    assert win.automation_menu.title() == "&Automation"
    dock = win.automation
    dock._control = NoWatcher()
    dock.refresh()
    assert dock.state.text() == "stopped" and dock.b_start.isEnabled() and not dock.b_stop.isEnabled()


def test_editor_builds_a_workflow(qtbot, data):
    from gcws.automation import workflow as W
    from gcws.core import proc_method as PM
    from gcws.ui.automation.editor import WorkflowEditor
    from gcws.ui.automation.items import EdgeItem, NodeItem
    PM.save({"format": "gcws-processing-method", "version": 1, "name": "NIAS",
             "sections": {"quant": {"mode": "nias_mgkg"}, "migration": {"simulant": "x"}}})
    watch, a = data / "watch", data / "A"
    watch.mkdir()
    ed = WorkflowEditor(W.Workflow(name="Lab"))
    qtbot.addWidget(ed)
    src = ed.add_node("source", 0, 0, folder=str(watch))
    m = ed.add_node("method", 250, 0, method="NIAS")
    r2 = ed.add_node("report2", 500, 0)
    rep = ed.add_node("report", 750, 0, kind="nias", formats=["xlsx", "docx"])
    out = ed.add_node("folder", 1000, 0, path=str(a))
    assert ed.connect_nodes(src.id, rep.id) == "Watched folder cannot pass to Report"     # not allowed
    for x, y in ((src, m), (m, r2), (r2, rep), (rep, out)):
        assert ed.connect_nodes(x.id, y.id) == ""
    assert ed.connect_nodes(m.id, r2.id) == "these steps are already connected"
    assert sum(isinstance(i, NodeItem) for i in ed.scene.items()) == 5
    assert sum(isinstance(i, EdgeItem) for i in ed.scene.items()) == 4
    assert not W.errors(ed.validate())
    edge = next(e for e in ed.wf.edges if e.src == rep.id)
    ed.set_edge_filter(edge.id, {"formats": ["docx"]})
    assert ed.edges[edge.id].label.text() == "Word"
    ed.undo.undo()
    assert ed.wf.edge(edge.id).filter == {}
    ed.undo.redo()
    ed.set_node_params(out.id, dict(out.params, path=str(watch / "reports")))
    assert any("inside the watched folder" in i.text for i in ed.validate())
    ed.undo.undo()
    ed.enabled.setChecked(True)
    assert ed.save()
    back = W.find(ed.wf.id)
    assert back.enabled and back.name == "Lab" and len(back.edges) == 4
    assert back.edge(edge.id).filter == {"formats": ["docx"]}
    ed.scene.clearSelection()
    ed.nodes[out.id].setSelected(True)
    ed.delete_selected()
    assert ed.wf.node(out.id) is None and len(ed.wf.edges) == 3
    ed.dirty = False                                     # closing must not ask to save


def test_step_and_arrow_dialogs(qtbot, data):
    from gcws.automation import templates
    from gcws.ui.automation.node_dialogs import EdgeFilterDialog, NodeDialog
    wf = templates.make("nias", source=str(data), method="NIAS", folder_a=str(data / "A"))
    for node in wf.nodes:
        dlg = NodeDialog(node, method_names=["NIAS", "Other"])
        qtbot.addWidget(dlg)
        vals = dlg.values()
        assert set(node.params) <= set(vals) or node.type == "report2"
    src = NodeDialog(wf.source)
    src.w["interval_min"].setValue(7)
    assert src.values()["interval_min"] == 7 and src.values()["folder"] == str(data)
    rep = next(n for n in wf.nodes if n.type == "report")
    d = NodeDialog(rep)
    d.fmt["pdf"].setChecked(False)
    d.fmt["dd"].setChecked(True)
    assert "pdf" not in d.values()["formats"] and "dd" in d.values()["formats"]
    edge = wf.outgoing(rep.id)[0]
    ed = EdgeFilterDialog(edge, wf)
    qtbot.addWidget(ed)
    ed.status["control"].setChecked(True)
    ed.name.setText("2601*")
    v = ed.values()
    assert v["status"] == ["control"] and v["name"] == "2601*" and "formats" in v


def test_rules_dialog_round_trip(qtbot, data):
    from gcws.automation import rules as RU
    from gcws.ui.dialogs.report2 import RulesDialog
    dlg = RulesDialog(RU.default_rules())
    qtbot.addWidget(dlg)
    rid, box, level, widgets = next(r for r in dlg._rows if r[0] == "no_sml_above_limit")
    box.setChecked(True)
    level.setCurrentIndex(1)
    out = {r.id: r for r in dlg.rules()}
    assert out["no_sml_above_limit"].enabled and out["no_sml_above_limit"].level == "info"
    assert out["istd_qc"].params["max_area_diff_pct"] == 50.0
    dlg.as_default.setChecked(True)
    dlg._ok()
    assert {r.id: r.enabled for r in RU.load_default_rules()}["no_sml_above_limit"]


def test_panel_new_duplicate_delete(qtbot, data, monkeypatch):
    from gcws.automation import workflow as W
    from gcws.ui.docks.automation import AutomationDock
    dock = AutomationDock(control=NoWatcher())
    qtbot.addWidget(dock)
    ed = dock.new("simple")
    assert ed is not None and len(W.list_workflows()) == 1
    ed.dirty = False
    ed.close()
    dock.refresh()
    dock.table.selectRow(0)
    dock.duplicate()
    assert sorted(w.name for w in W.list_workflows()) == ["One report into one folder",
                                                          "One report into one folder (copy)"]
    dock.table.selectRow(0)
    dock.delete(confirm=False)
    assert len(W.list_workflows()) == 1
    path = W.list_workflows()[0].path
    imported = dock.import_(str(path))
    assert imported is not None and len(W.list_workflows()) == 2 and not imported.enabled


def test_queue_remove_and_process_again(qtbot, data, monkeypatch):
    """P72: a sample that cannot be processed is removed from the queue in the panel (and in
    Report²); "Process again" brings it back."""
    from PySide6.QtWidgets import QMessageBox
    from gcws.automation import journal as J
    from gcws.ui.docks.automation import AutomationDock
    from gcws.ui.docks.report2 import Report2Dock
    jr = J.Journal(data / "journal.sqlite")
    b = jr.batch("wf1", data / "26016605_TEST")
    stuck = jr.ensure_job("wf1", "m", b["id"], "7:x", "26016606_x", ["07.D"], {}, "fp", reason="waiting for 11.D")
    failed = jr.ensure_job("wf1", "m", b["id"], "9:y", "26016607_y", ["09.D"], {}, "fp")
    assert jr.transition(failed.id, J.WAITING, J.NOT_PROCESSED, reason="no Blank in the batch folder")
    dock = AutomationDock(journal=jr, control=NoWatcher())
    qtbot.addWidget(dock)
    assert dock.queue.rowCount() == 2 and not dock.b_remove.isEnabled()
    dock.queue.selectAll()
    assert dock.b_remove.isEnabled()
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or QMessageBox.Yes)
    assert sorted(dock.remove_from_queue()) == sorted([stuck.id, failed.id]) and len(asked) == 1
    assert dock.queue.rowCount() == 0
    dock.show_removed.setChecked(True)
    assert dock.queue.rowCount() == 2 and dock.queue.item(0, 3).text() == "Removed"
    dock.queue.selectRow(0)
    first = dock.queue.item(0, 0).data(0x0100)
    assert dock.process_again() == [first] and jr.job(first).state == J.QUEUED
    # Report²: remove from its right-click menu
    r2 = Report2Dock(journal=jr, poll_ms=60000)
    qtbot.addWidget(r2)
    r2.select(first)
    assert r2.a_remove.isEnabled()
    assert r2.remove_from_queue(confirm=False) and jr.job(first).state == J.REMOVED
    r2.select(first)
    assert not r2.a_remove.isEnabled()


def test_view_menu_stays_open_while_panels_are_switched(qtbot, win):
    """P74: a click on a panel in View toggles it and leaves the menu open; a click elsewhere closes it."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from gcws.ui.widgets.stay_open_menu import StayOpenMenu
    menu = win.view_menu
    assert isinstance(menu, StayOpenMenu)
    act = next(a for a in menu.actions() if a.text() == "Automation")
    before = act.isChecked()
    menu.popup(win.mapToGlobal(QPoint(40, 40)))
    qtbot.waitUntil(menu.isVisible)
    try:
        pos = menu.actionGeometry(act).center()
        QTest.mouseClick(menu, Qt.LeftButton, Qt.NoModifier, pos)
        assert act.isChecked() != before and menu.isVisible()
        QTest.mouseClick(menu, Qt.LeftButton, Qt.NoModifier, pos)
        assert act.isChecked() == before and menu.isVisible()
        QTest.keyClick(menu, Qt.Key_Escape)
        qtbot.waitUntil(lambda: not menu.isVisible())
    finally:
        menu.close()
