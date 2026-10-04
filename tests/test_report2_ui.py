"""P62: Report² (accepted / control needed, findings, review, reprocess, delivery) and the batch
report of a folder from GC Workspace. Dialogs are never left open."""
import json
import time
from pathlib import Path

import pytest

from test_ui import _load, win  # noqa: F401  (fixture)


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


def _seed(data, tmp_path):
    """A workflow and a journal with samples in every state."""
    from gcws.automation import journal as J, templates
    watch = tmp_path / "watch"
    batch = watch / "26016605_GIOSUN1635"
    batch.mkdir(parents=True)
    wf = templates.make("nias", "Lab", source=str(watch), method="NIAS", folder_a=str(tmp_path / "A"),
                        folder_b=str(tmp_path / "B"))
    wf.save()
    rep = wf.by_type("report")[0]
    jr = J.Journal()
    b = jr.batch(wf.id, batch)
    m = wf.methods()[0].id
    out = tmp_path / "jobs"
    out.mkdir()
    ids = {}
    for name, state in (("S-control", J.CONTROL), ("S-auto", J.ACCEPTED_AUTO), ("S-wait", J.WAITING),
                        ("S-noblank", J.NOT_PROCESSED), ("S-failed", J.FAILED)):
        j = jr.ensure_job(wf.id, m, b["id"], name, name, [name + "_A.D"], {}, "fp", state=J.QUEUED
                          if state not in (J.WAITING,) else J.WAITING, reason="waiting for B" if state == J.WAITING
                          else "")
        files = {}
        for fmt in ("xlsx", "docx"):
            p = out / f"{name}_NIAS_Report.{fmt}"
            p.write_text(fmt)
            files[fmt] = str(p)
        if state != J.WAITING:
            jr.transition(j.id, J.QUEUED, J.PROCESSING)
            fields = {"files": {rep.id: files}, "job_dir": str(out)}
            if state == J.CONTROL:
                fields["findings"] = [{"rule": "sml_exceeded", "level": "control", "text": "0.5 mg/kg > SML 0.05",
                                       "member": "07_A", "substance": "Bisphenol A", "cas": "80-05-7", "rt": 17.2}]
            if state == J.NOT_PROCESSED:
                fields["reason"] = "07_A: no Blank from the same batch"
            jr.transition(j.id, J.PROCESSING, state, **fields)
        ids[name] = j.id
    return wf, jr, ids, batch


def _names(tree):
    out = []
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        out += [top.child(k).text(0) for k in range(top.childCount())]
    return out


def test_report2_areas_counters_and_review(qtbot, data, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    from gcws.ui.docks.report2 import Report2Dock
    wf, jr, ids, batch = _seed(data, tmp_path)
    monkeypatch.setattr("gcws.automation.control.WatcherControl", lambda *a, **k: NoWatcher())
    dock = Report2Dock(journal=jr)
    qtbot.addWidget(dock)
    assert _names(dock.control) == ["S-control"] and _names(dock.accepted) == ["S-auto"]
    assert _names(dock.pending) == ["S-wait"] and set(_names(dock.problems)) == {"S-noblank", "S-failed"}
    assert dock.chips["processed"].text() == "Processed 2"
    assert dock.chips["control"].text() == "Control needed 1"
    assert dock.chips["accepted"].text().startswith("Accepted 1 (1 automatic")
    dock.select(ids["S-control"])
    assert dock.findings.rowCount() == 1 and dock.findings.item(0, 2).text() == "Bisphenol A"
    assert dock.files.rowCount() == 2 and dock.b_accept.isEnabled() and dock.b_open.isEnabled()
    # accept: recorded with the analyst; with no watcher running, delivered at once
    assert dock.review(True, comment="SML checked, migration below the limit")
    j = jr.job(ids["S-control"])
    assert j.state == J.ACCEPTED_MANUAL and j.comment.startswith("SML checked") and j.reviewer
    assert (tmp_path / "A" / batch.name / "S-control_NIAS_Report.xlsx").is_file()
    assert (tmp_path / "B" / batch.name / "S-control_NIAS_Report.docx").is_file()
    assert _names(dock.control) == [] and set(_names(dock.accepted)) == {"S-control", "S-auto"}
    assert "accepted by" in " ".join(dock.history.item(r, 2).text() for r in range(dock.history.rowCount()))
    # reject, process again, process without blank
    dock.select(ids["S-auto"])
    assert dock.review(False, comment="wrong sample") and jr.job(ids["S-auto"]).state == J.REJECTED
    assert dock.reprocess("full") and jr.job(ids["S-auto"]).state == J.QUEUED
    dock.select(ids["S-noblank"])
    assert dock.a_noblank.isEnabled()
    assert dock.process_without_blank(confirm=False)
    j = jr.job(ids["S-noblank"])
    assert j.state == J.QUEUED and j.override["allow_no_blank"] and j.revision == 2


def test_open_in_gc_workspace(qtbot, data, tmp_path, monkeypatch):
    from gcws.ui.docks.report2 import Report2Dock
    wf, jr, ids, batch = _seed(data, tmp_path)
    proj = tmp_path / "x.gcws"
    proj.write_text("{}")
    jr.update_job(ids["S-control"], project_path=str(proj))
    monkeypatch.setattr("gcws.automation.control.WatcherControl", lambda *a, **k: NoWatcher())
    dock = Report2Dock(journal=jr)
    qtbot.addWidget(dock)
    got = []
    dock.openProject.connect(got.append)
    dock.select(ids["S-control"])
    assert dock.b_project.isEnabled()
    dock.open_project()
    assert got == [str(proj)]


def test_accept_comment_is_optional_but_reject_comment_is_required(qtbot):
    from PySide6.QtWidgets import QDialog
    from gcws.ui.dialogs.report2 import ReviewDialog

    accept = ReviewDialog(True, "S-control", 2)
    qtbot.addWidget(accept)
    assert not accept.required
    accept._ok()
    assert accept.result() == QDialog.Accepted

    reject = ReviewDialog(False, "S-control", 0)
    qtbot.addWidget(reject)
    assert reject.required
    reject._ok()
    assert reject.result() != QDialog.Accepted


def test_processed_pairs_are_nested_and_available_to_switch(qtbot, data, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    from gcws.ui.docks.report2 import Report2Dock

    wf, jr, ids, batch = _seed(data, tmp_path)
    project = tmp_path / "pair.gcws"
    project.write_text("{}")
    for name in ("S-control", "S-auto"):
        jr.update_job(ids[name], members=[f"{name}_A.D", f"{name}_B.D"], project_path=str(project))
    monkeypatch.setattr("gcws.automation.control.WatcherControl", lambda *a, **k: NoWatcher())
    dock = Report2Dock(journal=jr)
    qtbot.addWidget(dock)
    child = dock.control.topLevelItem(0).child(0)
    assert batch.name == dock.control.topLevelItem(0).text(0)
    assert "S-control_A" in child.text(0) and "S-control_B" in child.text(0)
    assert {j.id for j in dock.visible_pairs()} == {ids["S-control"], ids["S-auto"]}
    opened = []
    dock.openDetermination.connect(opened.append)
    dock.open_determination(ids["S-control"])
    assert opened == [ids["S-control"]]
    opened.clear()
    menu = dock._tree_context_menu(dock.control, dock.control.visualItemRect(child).center())
    assert menu.actions()[0].text() == "Open in Replicates / results"
    menu.actions()[0].trigger()
    assert opened == [ids["S-control"]]
    dock.search.setText("S-auto")
    assert [j.id for j in dock.visible_pairs()] == [ids["S-auto"]]


def test_edited_accept_queues_regeneration(qtbot, data, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    from gcws.ui.docks.report2 import Report2Dock

    wf, jr, ids, batch = _seed(data, tmp_path)
    project = tmp_path / "pair.gcws"
    project.write_text("{}")
    jr.update_job(ids["S-control"], project_path=str(project), edited=1)
    monkeypatch.setattr("gcws.automation.control.WatcherControl", lambda *a, **k: NoWatcher())
    dock = Report2Dock(journal=jr)
    qtbot.addWidget(dock)
    dock.select(ids["S-control"])
    assert dock.review(True, comment="")
    job = jr.job(ids["S-control"])
    assert job.state == J.QUEUED and job.review_pending
    assert _names(dock.control) == ["S-control"] and "S-control" not in _names(dock.pending)


def test_docks_and_menus(qtbot, win):
    assert "report2" in win.docks and "automation" in win.docks
    labels = [a.text() for a in win.report_menu.actions()]
    assert "Batch report of this folder..." in labels
    assert "Report²" in [a.text() for a in win.automation_menu.actions()]


def test_replicates_has_report2_pair_picker(qtbot, win, monkeypatch):
    page = win.replicates.duplicate
    assert page.b_report2.text() == "Load from Report²…"
    page.resize(1000, 600)
    page.show()
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    assert page.b_report2.y() == page.b_compare.y()
    assert page.b_report2.x() + page.b_report2.width() < 388  # fits the usual dock width
    win.replicates.report2Requested.disconnect(win._show_report2_pair_menu)
    selected = []
    win.replicates.report2Requested.connect(lambda: selected.append(True))
    page.b_report2.click()
    assert selected == [True]


def test_processed_pair_display_does_not_apply_automatic_changes(qtbot, win, monkeypatch):
    page = win.replicates.duplicate
    calls = []
    monkeypatch.setattr(page, "compare", lambda sync=False: calls.append(sync))
    page.set_pair("A", "B", processed=True)
    assert calls == [False] and page._view_processed


def test_switch_saves_accepted_project_and_returns_it_to_control(qtbot, win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from gcws.automation import journal as J

    jr = J.Journal(tmp_path / "journal.sqlite")
    batch = jr.batch("wf", tmp_path)
    job = jr.ensure_job("wf", "method", batch["id"], "pair", "Pair", ["A.D", "B.D"], {}, "fp",
                        state=J.QUEUED)
    project = tmp_path / "pair.gcws"
    project.write_text("original")
    jr.transition(job.id, J.QUEUED, J.PROCESSING)
    jr.transition(job.id, J.PROCESSING, J.ACCEPTED_AUTO, project_path=str(project))
    win.report2._journal = jr
    win._report2_job = (job.id, 1, project)
    win._report2_baseline = {"quant": {"limit": 1}}
    win.ws.project_path = project
    win.ws.dirty = True
    monkeypatch.setattr("gcws.ui.main_window.P.to_dict", lambda ws, path: {"quant": {"limit": 2}})
    monkeypatch.setattr("gcws.ui.main_window.P.save", lambda ws, path: (project.write_text("edited"), project)[1])

    assert win._save_report2_project()
    assert project.read_text() == "edited"
    assert jr.job(job.id).state == J.CONTROL and jr.job(job.id).edited == 1
    assert jr.accept_edited(job.id, user="analyst")
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    assert win._save_report2_project()  # switching after Accept must not trap the open project
    writes = []
    monkeypatch.setattr(win.ws, "states", lambda: [object()])
    monkeypatch.setattr("gcws.ui.main_window.P.save", lambda ws, path: (writes.append(path), path)[1])
    win.save_project()
    assert writes == []  # Ctrl+S on an old revision must not overwrite the pending source


def test_run_rename_marks_report2_project_edited(qtbot, win, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    from gcws.ui.main_window import _report_inputs

    jr = J.Journal(tmp_path / "journal.sqlite")
    batch = jr.batch("wf", tmp_path)
    job = jr.ensure_job("wf", "method", batch["id"], "pair", "Pair", ["A.D", "B.D"], {}, "fp",
                        state=J.QUEUED)
    project = tmp_path / "pair.gcws"
    project.write_text("original")
    jr.transition(job.id, J.QUEUED, J.PROCESSING)
    jr.transition(job.id, J.PROCESSING, J.ACCEPTED_AUTO, project_path=str(project))
    win.report2._journal = jr
    win._report2_job = (job.id, 1, project)
    original = {"runs": [{"id": "a", "name": "Old"}]}
    renamed = {"runs": [{"id": "a", "name": "New"}]}
    win._report2_baseline = _report_inputs(original)
    win.ws.project_path = project
    win.ws.dirty = False
    monkeypatch.setattr("gcws.ui.main_window.P.to_dict", lambda ws, path: renamed)
    monkeypatch.setattr("gcws.ui.main_window.P.save", lambda ws, path: (project.write_text("edited"), project)[1])

    assert win._save_report2_project()
    assert project.read_text() == "edited"
    assert jr.job(job.id).state == J.CONTROL and jr.job(job.id).edited == 1


def test_rejected_report2_project_can_be_saved_and_requeued(qtbot, win, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    from gcws.ui.main_window import _report_inputs

    jr = J.Journal(tmp_path / "journal.sqlite")
    batch = jr.batch("wf", tmp_path)
    job = jr.ensure_job("wf", "method", batch["id"], "pair", "Pair", ["A.D", "B.D"], {}, "fp",
                        state=J.QUEUED)
    project = tmp_path / "pair.gcws"
    project.write_text("original")
    jr.transition(job.id, J.QUEUED, J.PROCESSING)
    jr.transition(job.id, J.PROCESSING, J.CONTROL, project_path=str(project))
    assert jr.review(job.id, False, "bad data", user="reviewer")
    win.report2._journal = jr
    win._report2_job = (job.id, 1, project)
    win._report2_baseline = _report_inputs({"quant": {"limit": 1}})
    win.ws.project_path = project
    win.ws.dirty = False
    monkeypatch.setattr("gcws.ui.main_window.P.to_dict", lambda ws, path: {"quant": {"limit": 2}})
    monkeypatch.setattr("gcws.ui.main_window.P.save", lambda ws, path: (project.write_text("edited"), project)[1])
    monkeypatch.setattr("gcws.ui.main_window.QMessageBox.warning", lambda *args: None)

    assert win._save_report2_project()
    saved = jr.job(job.id)
    assert project.read_text() == "edited"
    assert saved.state == J.REJECTED and saved.edited == 1
    assert saved.reviewer == "reviewer" and saved.comment == "bad data"
    assert jr.accept_edited(job.id, user="analyst")
    queued = jr.job(job.id)
    assert queued.state == J.QUEUED and queued.review_pending["user"] == "analyst"
    assert queued.reviewer is None and queued.comment is None and queued.reviewed_at is None


def test_stale_report2_project_refuses_unsaved_display_changes(qtbot, win, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    from gcws.ui.main_window import _report_inputs

    jr = J.Journal(tmp_path / "journal.sqlite")
    batch = jr.batch("wf", tmp_path)
    job = jr.ensure_job("wf", "method", batch["id"], "pair", "Pair", ["A.D", "B.D"], {}, "fp",
                        state=J.QUEUED)
    project = tmp_path / "pair.gcws"
    project.write_text("original")
    win.report2._journal = jr
    win._report2_job = (job.id, 1, project)
    win._report2_baseline = _report_inputs({"runs": [{"id": "a", "color": "#111111"}]})
    win.ws.project_path = project
    win.ws.dirty = True
    jr.update_job(job.id, revision=2, project_path=str(project))
    monkeypatch.setattr("gcws.ui.main_window.P.to_dict", lambda ws, path: {"runs": [{"id": "a", "color": "#222222"}]})
    monkeypatch.setattr("gcws.ui.main_window.P.save", lambda ws, path: pytest.fail("stale source overwritten"))
    warnings = []
    monkeypatch.setattr("gcws.ui.main_window.QMessageBox.warning", lambda *args: warnings.append(args[-1]))

    assert not win._save_report2_project()
    assert warnings and project.read_text() == "original"


def test_incomplete_report2_project_is_not_attached_or_saved(qtbot, win, tmp_path, monkeypatch):
    from gcws.automation import journal as J

    jr = J.Journal(tmp_path / "journal.sqlite")
    batch = jr.batch("wf", tmp_path)
    job = jr.ensure_job("wf", "method", batch["id"], "pair", "Pair", ["A.D", "B.D"], {}, "fp",
                        state=J.QUEUED)
    project = tmp_path / "pair.gcws"
    project.write_text("original")
    jr.transition(job.id, J.QUEUED, J.PROCESSING)
    jr.transition(job.id, J.PROCESSING, J.CONTROL, project_path=str(project))
    win.report2._journal = jr
    monkeypatch.setattr("gcws.ui.main_window.P.read", lambda path: {"runs": [{"id": "a"}, {"id": "b"}]})
    monkeypatch.setattr("gcws.ui.main_window.P.resolve_run_path",
                        lambda entry, path: project if entry["id"] == "a" else None)
    warnings = []
    monkeypatch.setattr("gcws.ui.main_window.QMessageBox.warning", lambda *args: warnings.append(args[-1]))

    assert not win.open_report2_job(job.id)
    assert win._report2_job is None and project.read_text() == "original"
    assert warnings


def test_async_report2_load_failure_does_not_bind_partial_workspace(qtbot, win, tmp_path, monkeypatch):
    from gcws.automation import journal as J

    jr = J.Journal(tmp_path / "journal.sqlite")
    batch = jr.batch("wf", tmp_path)
    job = jr.ensure_job("wf", "method", batch["id"], "pair", "Pair", ["A.D", "B.D"], {}, "fp",
                        state=J.QUEUED)
    project = tmp_path / "pair.gcws"
    project.write_text("original")
    jr.transition(job.id, J.QUEUED, J.PROCESSING)
    jr.transition(job.id, J.PROCESSING, J.CONTROL, project_path=str(project))
    win.report2._journal = jr
    monkeypatch.setattr("gcws.ui.main_window.P.read", lambda path: {"runs": [{"id": "a"}, {"id": "b"}]})
    monkeypatch.setattr("gcws.ui.main_window.P.resolve_run_path",
                        lambda entry, path: tmp_path / f"{entry['id']}.D")
    monkeypatch.setattr(win, "load_runs", lambda *args, **kwargs: win._finish_project_load())
    warnings = []
    monkeypatch.setattr("gcws.ui.main_window.QMessageBox.warning", lambda *args: warnings.append(args[-1]))

    assert win.open_report2_job(job.id)
    assert win._report2_job is None and project.read_text() == "original"
    assert any("did not all load" in text for text in warnings)


def test_batch_report_of_a_folder(qtbot, win, samples, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from test_pipeline import MIGRATION, lib_oracle
    _load(qtbot, win, samples, ["06_", "07_", "08_", "09_", "10_", "11_", "12_", "13_"])
    ws = win.ws
    lib_oracle(ws, [s.id for s in ws.states() if s.role == "sample"])
    ws.quant["migration"] = dict(MIGRATION)
    ws.recompute_quant()
    out = tmp_path / "reports"
    out.mkdir()
    with monkeypatch.context() as m:
        m.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
        win.batch_report("nias", out_dir=str(out))
        qtbot.waitUntil(lambda: getattr(win, "last_batch", None) is not None, timeout=240000)
    entries, files = win.last_batch
    assert [e["name"] for e in entries] == ["26016606_130m_min_GIOSUN1635", "26016607_170m_min_GIOSUN1635"]
    assert all(Path(e["xlsx"]).is_file() for e in entries)
    assert Path(files["batch_docx"]).is_file() and Path(files["batch_xlsx"]).is_file()
    assert all(e["state"] in ("accepted_auto", "control") for e in entries)
    assert any(r.action == "Batch report" for r in ws.audit.records)
    assert sorted(p.name for p in samples.iterdir())                         # nothing written into the batch
    assert not any(p.name.endswith("_Batch_Report.docx") for p in samples.iterdir())
