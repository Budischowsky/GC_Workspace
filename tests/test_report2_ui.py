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


def test_docks_and_menus(qtbot, win):
    assert "report2" in win.docks and "automation" in win.docks
    labels = [a.text() for a in win.report_menu.actions()]
    assert "Batch report of this folder..." in labels
    assert "Report²" in [a.text() for a in win.automation_menu.actions()]


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
