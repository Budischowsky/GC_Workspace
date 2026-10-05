"""P62 / Report² rework: one list with filter chips, To do and Archive, one-click accept and reject
with a reason (undo for a few seconds), right-click menus, delete (hide) and restore, keys, and the
batch report of a folder from GC Workspace. Dialogs are never left open."""
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


def _names(dock, bucket=None):
    """The samples in the list (of one chip filter)."""
    if bucket is not None and dock.filter != bucket:
        dock.set_filter(bucket)
    tree = dock.tree
    out = []
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        out += [top.child(k).text(0) for k in range(top.childCount())]
    return out


def _no_word(docx, pdf):
    raise RuntimeError("no Word in the tests")


@pytest.fixture()
def settings(tmp_path):
    """QSettings of the test only (the preview switch is remembered there)."""
    from PySide6.QtCore import QCoreApplication, QSettings
    QCoreApplication.setOrganizationName("GCWorkspaceTest")
    QCoreApplication.setApplicationName("pytest")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    QSettings().clear()


def _dock(jr, monkeypatch, qtbot, word=_no_word):
    """Report² on ``jr``; Microsoft Word (the preview of Word reports) is replaced by ``word``."""
    from gcws.report import service as RS
    from gcws.ui.docks.report2 import Report2Dock
    monkeypatch.setattr("gcws.automation.control.WatcherControl", lambda *a, **k: NoWatcher())
    monkeypatch.setattr(RS, "docx_to_pdf", word)
    dock = Report2Dock(journal=jr, poll_ms=60000)
    qtbot.addWidget(dock)
    return dock


def test_report2_list_counters_and_review(qtbot, data, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    wf, jr, ids, batch = _seed(data, tmp_path)
    dock = _dock(jr, monkeypatch, qtbot)
    assert set(_names(dock)) == {"S-control", "S-auto", "S-wait", "S-noblank", "S-failed"}
    assert _names(dock, "control") == ["S-control"] and _names(dock, "accepted") == ["S-auto"]
    assert _names(dock, "waiting") == ["S-wait"]
    assert _names(dock, "not_processed") == ["S-noblank"] and _names(dock, "failed") == ["S-failed"]
    dock.set_filter("failed")                                  # clicked again: all
    assert dock.filter == "all"
    assert dock.chips["all"].text() == "All 5"
    assert dock.chips["control"].text() == "Control needed 1"
    assert dock.chips["accepted"].text() == "Accepted 1" and "1 automatic" in dock.chips["accepted"].toolTip()
    assert dock.chips["rejected"].text() == ""                 # none: hidden
    assert dock.b_todo.text() == "To do (1)" and dock.b_archive.text() == "Archive (0)"
    assert dock.watcher.text() == "● Watcher stopped" and not dock.start_btn.isHidden()
    batch_row = dock.tree.topLevelItem(0)
    assert batch_row.text(0) == batch.name and batch_row.text(1) == "1/5 accepted · 1 control · 2 failed/rejected · 1 waiting"
    dock.select(ids["S-control"])
    assert dock.reasons.text() == "" and dock.b_accept.isEnabled() and dock.b_open.isEnabled()   # no labels
    assert dock._items[f"j:{ids['S-control']}"].childCount() == 0     # no double determination: nothing below
    jr.update_job(ids["S-control"], evidence={"features": [
        {"light": "red", "name": "Bisphenol A", "rt": 17.2}, {"light": "red", "name": "unknown", "rt": 9.1},
        {"light": "yellow", "name": "BHT", "rt": 11.87}, {"light": "green", "name": "x"}, {"light": "grey"}]})
    dock.refresh()
    row = dock._items[f"j:{ids['S-control']}"]
    assert [(row.child(k).text(0), row.child(k).text(1)) for k in range(row.childCount())] ==         [("Red - to decide", "2"), ("Yellow - to check", "1")]                # counts, not every finding
    assert "Bisphenol A" in row.child(0).toolTip(0)
    # accept: one click, no comment; delivered once the undo time is over (no watcher running)
    assert dock.review(True)
    j = jr.job(ids["S-control"])
    assert j.state == J.ACCEPTED_MANUAL and j.comment == "" and j.reviewer
    assert dock.bar.isVisibleTo(dock) and dock.b_undo.isVisibleTo(dock)
    assert not (tmp_path / "A" / batch.name / "S-control_NIAS_Report.xlsx").is_file()
    dock.dismiss_message()
    assert (tmp_path / "A" / batch.name / "S-control_NIAS_Report.xlsx").is_file()
    assert (tmp_path / "B" / batch.name / "S-control_NIAS_Report.docx").is_file()
    assert _names(dock, "control") == [] and set(_names(dock, "accepted")) == {"S-control", "S-auto"}
    assert dock._items[f"j:{ids['S-control']}"].text(3) == "✓"
    # reject with a reason, process again, process without blank
    dock.set_filter("all")
    dock.select(ids["S-auto"])
    assert dock.reject("Wrong sample / mix-up") and jr.job(ids["S-auto"]).state == J.REJECTED
    assert jr.job(ids["S-auto"]).comment == "Wrong sample / mix-up"
    assert dock.reprocess("full") and jr.job(ids["S-auto"]).state == J.QUEUED
    assert "start the watcher" in dock.bar_text.text()
    dock.select(ids["S-noblank"])
    assert dock.a_noblank.isEnabled()
    assert dock.process_without_blank(confirm=False)
    j = jr.job(ids["S-noblank"])
    assert j.state == J.QUEUED and j.override["allow_no_blank"] and j.revision == 2


def test_open_in_gc_workspace(qtbot, data, tmp_path, monkeypatch):
    wf, jr, ids, batch = _seed(data, tmp_path)
    proj = tmp_path / "x.gcws"
    proj.write_text("{}")
    jr.update_job(ids["S-control"], project_path=str(proj))
    dock = _dock(jr, monkeypatch, qtbot)
    got = []
    dock.openProject.connect(got.append)
    dock.select(ids["S-control"])
    assert dock.b_project.isEnabled()
    dock.open_project()
    assert got == [str(proj)]


def test_accept_and_reject_comments_are_optional(qtbot):
    from PySide6.QtWidgets import QDialog
    from gcws.ui.dialogs.report2 import ReviewDialog

    for accept in (True, False):
        dlg = ReviewDialog(accept, "S-control", 2)
        qtbot.addWidget(dlg)
        assert not dlg.required
        dlg._ok()
        assert dlg.result() == QDialog.Accepted and dlg.text() == ""


def test_processed_pairs_are_nested_and_available_to_switch(qtbot, data, tmp_path, monkeypatch):
    wf, jr, ids, batch = _seed(data, tmp_path)
    project = tmp_path / "pair.gcws"
    project.write_text("{}")
    for name in ("S-control", "S-auto"):
        jr.update_job(ids[name], members=[f"{name}_A.D", f"{name}_B.D"], project_path=str(project))
    dock = _dock(jr, monkeypatch, qtbot)
    child = dock._items[f"j:{ids['S-control']}"]
    assert batch.name == dock.tree.topLevelItem(0).text(0)
    assert "S-control_A" in child.text(0) and "S-control_B" in child.text(0)
    assert {j.id for j in dock.visible_pairs()} == {ids["S-control"], ids["S-auto"]}
    opened = []
    dock.openDetermination.connect(opened.append)
    dock.open_determination(ids["S-control"])
    assert opened == [ids["S-control"]]
    opened.clear()
    menu = dock._tree_context_menu(dock.tree.visualItemRect(child).center())
    texts = [a.text() for a in menu.actions()]
    assert "Open in Replicates / results" in texts
    next(a for a in menu.actions() if a.text() == "Open in Replicates / results").trigger()
    assert opened == [ids["S-control"]]
    dock.search.setText("S-auto")
    assert [j.id for j in dock.visible_pairs()] == [ids["S-auto"]]


def test_edited_accept_queues_regeneration(qtbot, data, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    wf, jr, ids, batch = _seed(data, tmp_path)
    project = tmp_path / "pair.gcws"
    project.write_text("{}")
    jr.update_job(ids["S-control"], project_path=str(project), edited=1)
    dock = _dock(jr, monkeypatch, qtbot)
    dock.select(ids["S-control"])
    assert dock.review(True)
    job = jr.job(ids["S-control"])
    assert job.state == J.QUEUED and job.review_pending
    assert not dock.b_undo.isVisibleTo(dock)                   # regenerating: nothing to undo
    assert _names(dock, "control") == ["S-control"] and "S-control" not in _names(dock, "waiting")


def test_undo_and_next_report_needing_control(qtbot, data, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    wf, jr, ids, batch = _seed(data, tmp_path)
    second = jr.ensure_job(wf.id, wf.methods()[0].id, jr.batch(wf.id, batch)["id"], "S-control2", "S-control2",
                           ["x.D"], {}, "fp", state=J.QUEUED)
    jr.transition(second.id, J.QUEUED, J.PROCESSING)
    jr.transition(second.id, J.PROCESSING, J.CONTROL)
    dock = _dock(jr, monkeypatch, qtbot)
    dock.select(ids["S-control"])
    assert dock.review(True)
    assert dock.current == second.id                           # the next one needing control is selected
    assert dock.undo() and jr.job(ids["S-control"]).state == J.CONTROL
    assert not dock.b_undo.isVisibleTo(dock) and dock._deliver == set()
    dock.dismiss_message()
    assert not (tmp_path / "A" / batch.name / "S-control_NIAS_Report.xlsx").is_file()   # undone: not delivered
    # a reject is undone the same way
    dock.select(second.id)
    assert dock.reject("Bad chromatography") and jr.job(second.id).state == J.REJECTED
    assert dock.undo() and jr.job(second.id).state == J.CONTROL
    # several at once
    dock.set_filter("control")
    dock.tree.selectAll()
    assert len(dock.selected_jobs()) == 2 and "2 reports selected" in dock.title.text()
    assert dock.review(True) and {jr.job(i).state for i in (ids["S-control"], second.id)} == {J.ACCEPTED_MANUAL}
    assert "and 1 more" in dock.bar_text.text()


def test_reject_reasons_menu_and_other(qtbot, data, tmp_path, monkeypatch):
    from gcws.automation import journal as J, rules as RU
    from gcws.ui.dialogs.report2 import ReviewDialog
    wf, jr, ids, batch = _seed(data, tmp_path)
    RU.save_reject_reasons(["Too dilute", "Bad chromatography"])
    dock = _dock(jr, monkeypatch, qtbot)
    dock.select(ids["S-control"])
    dock.reject_menu.aboutToShow.emit()
    texts = [a.text() for a in dock.reject_menu.actions() if not a.isSeparator()]
    assert texts == ["Too dilute", "Bad chromatography", "Other..."]
    with monkeypatch.context() as m:
        m.setattr(ReviewDialog, "exec", lambda self: (self.comment.setPlainText("smells"), 1)[1])
        dock.reject_menu.actions()[-1].trigger()
    assert jr.job(ids["S-control"]).state == J.REJECTED and jr.job(ids["S-control"]).comment == "smells"
    # a rejected report can be accepted after all
    assert dock.b_accept.isEnabled() and dock.review(True)
    assert jr.job(ids["S-control"]).state == J.ACCEPTED_MANUAL


def test_context_menus_of_a_sample_and_a_batch(qtbot, data, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from gcws.automation import journal as J
    wf, jr, ids, batch = _seed(data, tmp_path)
    dock = _dock(jr, monkeypatch, qtbot)
    row = dock._items[f"j:{ids['S-control']}"]
    menu = dock._tree_context_menu(dock.tree.visualItemRect(row).center())
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    for t in ("Open report", "Open in GC Workspace", "Accept", "Accept with comment...", "Reject",
              "Process again", "Remove from the queue...", "Deliver to the target folders now",
              "Show history...", "Open the job folder", "Copy sample name", "Delete..."):
        assert t in texts, t
    assert dock.current == ids["S-control"]                    # right-click selects the row
    top = dock.tree.topLevelItem(0)
    menu = dock._tree_context_menu(dock.tree.visualItemRect(top).center())
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts == ["Open batch report", "Open batch folder", 'Accept all "control needed" (1)...', "Reject batch",
                     "Process batch again...", "Delete batch..."]
    bid = top.data(0, 0x0100)
    with monkeypatch.context() as m:
        m.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        assert dock.accept_batch(bid)
        assert jr.job(ids["S-control"]).state == J.ACCEPTED_MANUAL
        assert dock.reject_batch(bid, "Repeat measurement")
        assert {jr.job(ids[n]).state for n in ("S-control", "S-auto")} == {J.REJECTED}
        assert dock.reprocess_batch(bid)
        assert jr.job(ids["S-failed"]).state == J.QUEUED and jr.job(ids["S-control"]).state == J.QUEUED


def test_delete_hides_and_show_deleted_restores(qtbot, data, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    wf, jr, ids, batch = _seed(data, tmp_path)
    dock = _dock(jr, monkeypatch, qtbot)
    dock.select(ids["S-failed"])
    asked = []
    with monkeypatch.context() as m:
        m.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
        assert dock.delete_selected() == [ids["S-failed"]]
    assert "Nothing on disk is deleted" in asked[0]
    assert "S-failed" not in _names(dock) and dock.chips["all"].text() == "All 4"
    assert dock.undo() and "S-failed" in _names(dock)            # Undo restores it
    dock.select(ids["S-failed"])
    assert dock.delete_selected(confirm=False)
    dock.a_show_deleted.setChecked(True)
    row = dock._items[f"j:{ids['S-failed']}"]
    assert row.text(1).startswith("Deleted") and row.font(0).italic()
    dock.select(ids["S-failed"])
    assert dock.a_restore.isEnabled() and not dock.a_delete.isEnabled()
    assert dock.restore_selected() == [ids["S-failed"]] and not jr.job(ids["S-failed"]).deleted
    # a whole batch
    bid = jr.batch(wf.id, batch)["id"]
    assert dock.delete_batch(bid, confirm=False)
    dock.a_show_deleted.setChecked(False)
    assert dock.tree.topLevelItemCount() == 0 and "Nothing to do" in dock.empty.text()
    dock.a_show_deleted.setChecked(True)
    assert dock.tree.topLevelItem(0).text(1) == "Deleted"
    assert dock.restore_batch(bid) and len(_names(dock)) == 5


def test_archive_and_reopen(qtbot, data, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    wf, jr, ids, batch = _seed(data, tmp_path)
    for name in ("S-wait", "S-noblank", "S-failed"):
        jr.delete([ids[name]])
    dock = _dock(jr, monkeypatch, qtbot)
    dock.select(ids["S-control"])
    assert dock.review(True)
    dock.dismiss_message()                                     # delivered: the whole batch is done
    jr.update_job(ids["S-auto"], export_state="done")
    dock.refresh()
    assert dock.tree.topLevelItemCount() == 0 and "Archive" in dock.empty.text()
    assert dock.b_archive.text() == "Archive (1)"
    dock.set_mode("archive")
    top = dock.tree.topLevelItem(0)
    assert top.text(0) == batch.name and top.text(1) == "2/2 accepted" and top.text(3) == "2/2"
    assert not top.isExpanded()                                # the archive lists batches
    bid = top.data(0, 0x0100)
    dock.search.setText("S-auto")                              # a sample name finds its batch
    assert dock.tree.topLevelItem(0).isExpanded() and _names(dock) == ["S-auto"]
    dock.search.setText("")
    menu = dock._tree_context_menu(dock.tree.visualItemRect(dock.tree.topLevelItem(0)).center())
    assert "Reopen" in [a.text() for a in menu.actions()]
    assert dock.reopen_batch(bid) and dock.mode == "todo" and dock.b_todo.isChecked()
    assert set(_names(dock)) == {"S-control", "S-auto"}
    dock.select(ids["S-auto"])
    assert dock.reject("Repeat measurement") and jr.job(ids["S-auto"]).state == J.REJECTED


def test_to_do_counts_only_batches_with_something_to_show(qtbot, data, tmp_path, monkeypatch):
    """A batch folder without samples, or whose reports were all deleted, is neither listed nor counted."""
    from gcws.automation import journal as J
    wf, jr, ids, batch = _seed(data, tmp_path)
    jr.batch(wf.id, tmp_path / "watch" / "26016700_EMPTY")                  # seen, no sample
    gone = jr.batch(wf.id, tmp_path / "watch" / "26016701_GONE")
    j = jr.ensure_job(wf.id, wf.methods()[0].id, gone["id"], "g", "S-gone", ["g.D"], {}, "fp", state=J.QUEUED)
    jr.delete([j.id])
    dock = _dock(jr, monkeypatch, qtbot)
    assert dock.b_todo.text() == "To do (1)" and dock.tree.topLevelItemCount() == 1
    dock.a_show_deleted.setChecked(True)
    assert dock.b_todo.text() == "To do (2)" and "S-gone" in _names(dock)


def test_keys_history_and_watcher(qtbot, data, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from gcws.automation import journal as J
    wf, jr, ids, batch = _seed(data, tmp_path)
    dock = _dock(jr, monkeypatch, qtbot)
    dock.select(ids["S-control"])
    dlg = dock.show_history()
    try:
        assert dlg.isVisible() and not dlg.isModal()
        assert dlg.files.rowCount() == 2 and dlg.history.rowCount() >= 1
    finally:
        dlg.close()
    dock.tree.setFocus()
    QTest.keyClick(dock.tree, Qt.Key_A)
    assert jr.job(ids["S-control"]).state == J.ACCEPTED_MANUAL
    QTest.keyClick(dock.tree, Qt.Key_Z, Qt.ControlModifier)
    assert jr.job(ids["S-control"]).state == J.CONTROL
    dock.select(ids["S-failed"])
    QTest.keyClick(dock.tree, Qt.Key_A)                        # not possible: nothing happens
    assert jr.job(ids["S-failed"]).state == J.FAILED
    with monkeypatch.context() as m:
        m.setattr("PySide6.QtWidgets.QMessageBox.question", lambda *a, **k: 0x00010000)   # No
        QTest.keyClick(dock.tree, Qt.Key_Delete)
    assert not jr.job(ids["S-failed"]).deleted


class RunningWatcher(NoWatcher):
    def status(self, journal=None):
        return {"state": "running", "heartbeat": 1, "current": ""}


def test_with_a_running_watcher_the_accept_is_left_to_it(qtbot, data, tmp_path, monkeypatch):
    from gcws.automation import journal as J
    from gcws.ui.docks.report2 import Report2Dock
    wf, jr, ids, batch = _seed(data, tmp_path)
    monkeypatch.setattr("gcws.automation.control.WatcherControl", lambda *a, **k: RunningWatcher())
    monkeypatch.setattr("gcws.report.service.docx_to_pdf", _no_word)
    dock = Report2Dock(journal=jr, poll_ms=60000)
    qtbot.addWidget(dock)
    assert dock.watcher.text() == "● Watcher running" and dock.start_btn.isHidden()
    dock.select(ids["S-control"])
    assert dock.review(True) and dock._deliver == set()
    job = jr.job(ids["S-control"])
    assert job.export_pending and job.deliver_after > time.time() and job.state == J.ACCEPTED_MANUAL
    assert dock._items[f"j:{ids['S-control']}"].text(3) == "pending"


def _pdf(path):
    from PySide6.QtGui import QPainter, QPdfWriter
    w = QPdfWriter(str(path))
    p = QPainter(w)
    p.drawText(200, 200, "NIAS report")
    p.end()
    return path


def test_pdf_preview(qtbot, data, tmp_path, monkeypatch, settings):
    from PySide6.QtCore import QSettings
    wf, jr, ids, batch = _seed(data, tmp_path)
    job = jr.job(ids["S-control"])
    files = dict(job.files)
    node = next(iter(files))
    pdf = _pdf(tmp_path / "S-control_NIAS_Report.pdf")
    files[node] = dict(files[node], pdf=str(pdf))
    jr.update_job(job.id, files=files)
    dock = _dock(jr, monkeypatch, qtbot)
    assert dock.b_preview.isChecked()
    dock.select(ids["S-auto"])                                  # Word and Excel only, and no Word here
    qtbot.waitUntil(lambda: "no Word in the tests" in dock.preview_note.text())
    dock.select(ids["S-control"])
    assert dock.preview.currentWidget() is dock.pdf_view and dock._pdf_doc.pageCount() == 1
    pdf.unlink()                                                # read into memory: the file is not held open
    dock.b_preview.setChecked(False)
    assert not dock.preview.isVisibleTo(dock) and QSettings().value("report2/preview", type=bool) is False
    dock.b_preview.setChecked(True)
    assert dock.preview.isVisibleTo(dock)


def test_word_report_is_previewed_through_a_pdf_made_once(qtbot, data, tmp_path, monkeypatch, settings):
    """No PDF among the report's formats: Word converts the Word report once (in a thread); the PDF is
    kept beside it in the job folder, made again only when the Word report is newer."""
    import os
    import shutil
    wf, jr, ids, batch = _seed(data, tmp_path)
    made = _pdf(tmp_path / "made_by_word.pdf")
    calls = []

    def word(docx, pdf):
        calls.append(Path(docx).name)
        shutil.copyfile(made, pdf)
        return Path(pdf)
    dock = _dock(jr, monkeypatch, qtbot, word)
    assert dock.b_preview.isChecked()
    dock.select(ids["S-control"])
    assert "Making a preview" in dock.preview_note.text()
    qtbot.waitUntil(lambda: dock.pdf_view is not None and dock.preview.currentWidget() is dock.pdf_view)
    docx = Path(jr.job(ids["S-control"]).files[next(iter(jr.job(ids["S-control"]).files))]["docx"])
    kept = dock.preview_pdf(docx)
    assert kept == docx.with_name(docx.stem + ".preview.pdf") and kept.is_file()
    assert calls == [docx.name]
    dock.select(ids["S-auto"])                                  # another report: converted as well
    qtbot.waitUntil(lambda: len(calls) == 2 and dock.preview.currentWidget() is dock.pdf_view)
    dock.select(ids["S-control"])                               # kept: no Word again
    assert dock.preview.currentWidget() is dock.pdf_view and len(calls) == 2
    later = kept.stat().st_mtime + 10
    os.utime(docx, (later, later))                              # the Word report was made again
    dock.select(ids["S-auto"])
    dock.select(ids["S-control"])
    qtbot.waitUntil(lambda: len(calls) == 3 and dock.preview.currentWidget() is dock.pdf_view)


def test_word_preview_failure_is_shown_once(qtbot, data, tmp_path, monkeypatch, settings):
    wf, jr, ids, batch = _seed(data, tmp_path)
    calls = []

    def no_word(docx, pdf):
        calls.append(docx)
        raise RuntimeError("Word is not installed")
    dock = _dock(jr, monkeypatch, qtbot, no_word)
    dock.select(ids["S-control"])
    qtbot.waitUntil(lambda: "Word is not installed" in dock.preview_note.text())
    dock.select(ids["S-auto"])
    qtbot.waitUntil(lambda: len(calls) == 2 and dock._converting is None)
    dock.select(ids["S-control"])                               # not tried again and again
    assert len(calls) == 2 and "Word could not convert" in dock.preview_note.text()


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
