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


def _overview_setup(data):
    """A workflow with a local copy, the watcher's last look and a journal with one processed sample."""
    import json
    import os
    import time
    from gcws.automation import journal as J, store, templates
    watch, local = data / "watch", data / "local"
    batch = watch / "26016605_TEST"
    batch.mkdir(parents=True)
    for n in ("06_EtOH_ISTD", "07_26016606_x_A", "08_EtOH", "11_26016606_x_B"):
        (batch / f"{n}.D").mkdir()
    (batch / "S Sequence Log .TSV").write_text("x")
    wf = templates.make("nias", "Lab", source=str(watch), method="NIAS", folder_a=str(data / "A"))
    wf.insert_copy(folder=str(local))
    wf.enabled = True
    wf.save()
    jr = J.Journal(data / "automation" / "journal.sqlite")
    b = jr.batch(wf.id, batch)
    for n, role in (("06_EtOH_ISTD", "blank_istd"), ("07_26016606_x_A", "sample"), ("08_EtOH", "blank"),
                    ("11_26016606_x_B", "sample")):
        jr.upsert_run(b["id"], n.casefold(), path=str(batch / f"{n}.D"), fingerprint="fp", state="ready", role=role,
                      copied_fp="fp" if n != "11_26016606_x_B" else None)
    m = wf.methods()[0]
    job = jr.ensure_job(wf.id, m.id, b["id"], "26016606:x", "26016606_x", ["07_26016606_x_A.D", "11_26016606_x_B.D"],
                        {"blank": ["08_EtOH.D"], "blank_istd": ["06_EtOH_ISTD.D"]}, "fp", reason="waiting for quiet")
    (local / "26016605_TEST" / "07_26016606_x_A.D").mkdir(parents=True)
    (local / "26016605_TEST" / "05_old_copy.D").mkdir()
    (local / "26010000_OLDER" / "01_y_A.D").mkdir(parents=True)
    store.atomic_write_json(store.listing_path(wf.id), {
        "root": str(watch), "reachable": True, "scanned": time.time(), "checked": time.time() - 120,
        "folders": [{"path": str(batch), "name": batch.name, "status": "batch", "batch_id": b["id"],
                     "has_log": False, "quiet": False, "quiet_in": 600, "other": ["S Sequence Log .TSV"]},
                    {"path": str(watch / "Archive"), "name": "Archive", "status": "old"}]})
    return wf, jr, job, batch, local


def test_folders_overview_shows_both_sides(qtbot, data):
    from gcws.automation import overview as OV
    wf, jr, job, batch, local = _overview_setup(data)
    [top] = OV.overview(jr, [wf])
    assert top.watched == str(data / "watch") and top.local == str(local) and "2 min ago" in top.why
    items = {c.name: c for c in top.children}
    assert set(items) == {"26016605_TEST", "Archive", "26010000_OLDER"}
    assert "not looked at" in items["Archive"].watched
    assert items["26010000_OLDER"].local == "only in the local copy"
    b = items["26016605_TEST"]
    assert b.watched == "watched (no sequence log)" and b.local == "3 of 4 finished run(s) copied"
    assert b.sample == "1 sample(s)" and "about 10 min" in b.why and b.state == "1 waiting"
    runs = {c.name: c for c in b.children}
    assert runs["07_26016606_x_A.D"].sample == "26016606_x" and runs["07_26016606_x_A.D"].job_id == job.id
    assert runs["07_26016606_x_A.D"].local == "copied" and runs["11_26016606_x_B.D"].local == "waiting to be copied"
    assert runs["08_EtOH.D"].sample == "blank of 26016606_x"
    assert runs["05_old_copy.D"].local == "only in the local copy"
    assert runs["S Sequence Log .TSV"].kind == "file"


def test_folders_tab_in_the_panel(qtbot, data):
    from gcws.ui.docks.automation import AutomationDock
    wf, jr, job, batch, local = _overview_setup(data)
    dock = AutomationDock(journal=jr, control=NoWatcher())
    qtbot.addWidget(dock)
    assert [dock.tabs.tabText(i) for i in range(dock.tabs.count())] == ["Workflows", "Folders", "Queue (1)",
                                                                        "Activity"]
    dock.tabs.setCurrentWidget(dock.folders)
    tree = dock.folders.tree
    top = tree.topLevelItem(0)
    assert top.text(0) == "Lab" and top.isExpanded()
    batch_item = next(top.child(i) for i in range(top.childCount()) if top.child(i).text(0) == "26016605_TEST")
    run = next(batch_item.child(i) for i in range(batch_item.childCount())
               if batch_item.child(i).text(0) == "07_26016606_x_A.D")
    shown = []
    dock.showJob.connect(shown.append)
    tree.clearSelection()
    run.setSelected(True)
    assert dock.folders.b_report.isEnabled() and dock.folders.b_local.isEnabled()
    dock.folders.show_in_report2()
    assert shown == [job.id]
    menu = dock.folders.context_menu(tree.visualItemRect(run).center())
    assert menu is not None and "Show in Report²" in [a.text() for a in menu.actions()]
    batch_item.setExpanded(True)
    dock.folders._signature = None
    dock.folders.refresh()                                 # rebuilt: what was open stays open
    top = tree.topLevelItem(0)
    again = next(top.child(i) for i in range(top.childCount()) if top.child(i).text(0) == "26016605_TEST")
    assert again.isExpanded()


def test_add_samples_by_hand(qtbot, data):
    from PySide6.QtCore import Qt
    from gcws.automation import journal as J, templates
    from gcws.ui.automation.add_samples import AddSamplesDialog, folder_samples
    from gcws.ui.docks.automation import AutomationDock
    watch = data / "watch"
    batch = data / "elsewhere" / "26016605_TEST"
    batch.mkdir(parents=True)
    watch.mkdir()
    for n in ("06_EtOH_ISTD", "07_26016606_x_A", "08_EtOH", "09_26016607_y_A", "11_26016606_x_B"):
        (batch / f"{n}.D").mkdir()
    assert [(n, m) for n, m, _b in folder_samples(batch)] == [
        ("26016606_x", ["07_26016606_x_A.D", "11_26016606_x_B.D"]), ("26016607_y", ["09_26016607_y_A.D"])]
    wf = templates.make("simple", "Lab", source=str(watch), method="NIAS", folder_a=str(data / "A"))
    wf.enabled = True
    wf.save()

    class Control(NoWatcher):
        started = 0

        def start(self, paused=False):
            Control.started += 1
            return True

    jr = J.Journal(data / "automation" / "journal.sqlite")
    dock = AutomationDock(journal=jr, control=Control())
    qtbot.addWidget(dock)
    dlg = AddSamplesDialog([wf], folder=str(batch))
    qtbot.addWidget(dlg)
    assert dlg.list.count() == 2 and dlg.ok.isEnabled()
    dlg.list.item(1).setCheckState(Qt.Unchecked)
    assert dlg.values() == (wf.id, str(batch), ["07_26016606_x_A.D", "11_26016606_x_B.D"])
    b = dock.add_samples(dlg)
    assert b["manual"] == 1 and jr.forced(b) == {"07_26016606_x_a", "11_26016606_x_b"}
    assert Control.started == 1                            # no watcher was running: started
    assert dock.tabs.currentWidget() is dock._queue_page and dock.queue.item(0, 3).text() == "Requested"
    assert dock.tabs.tabText(dock.tabs.indexOf(dock._queue_page)) == "Queue (1)"
    # from the Folders tab: a whole batch folder
    from gcws.automation.overview import Item
    out = dock.add_to_queue([Item("batch", batch.name, path=str(batch), workflow_id=wf.id)])
    assert len(out) == 1 and jr.forced(out[0]) == {"*"}


def test_watcher_starts_with_gc_workspace(qtbot, data):
    from PySide6.QtCore import QSettings
    from gcws.automation import templates
    from gcws.ui.docks.automation import AutomationDock
    calls = []

    class Control(NoWatcher):
        def start(self, paused=False):
            calls.append(paused)
            return True

    dock = AutomationDock(control=Control())
    qtbot.addWidget(dock)
    assert not dock.start_with_app()                       # no active workflow
    wf = templates.make("simple", "Lab", source=str(data), method="NIAS", folder_a=str(data / "A"))
    wf.enabled = True
    wf.save()
    dock.with_app.setChecked(False)
    assert not dock.start_with_app() and not QSettings().value("automation/start_with_app", True, type=bool)
    dock.with_app.setChecked(True)
    assert dock.start_with_app() and calls == [False]
