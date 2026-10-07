"""P60: the watcher - scanning a watched folder while the instrument writes, the job queue (one
job at a time, crashes, timeouts), Report² review and delivery, the batch report, the control
socket and the opt-in autostart. The job processes are replaced by a fake launcher."""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

BATCH = ["06_EtOH_ISTD", "07_26016606_x_A", "08_EtOH", "09_26016607_y_A", "10_EtOH", "11_26016606_x_B",
         "12_26016607_y_B", "13_EtOH"]


class FakeLauncher(QObject):
    finished = Signal(str, int, str)

    def __init__(self):
        super().__init__()
        self.started, self.killed = [], 0
        self.job = None

    def running(self):
        return self.job is not None

    def start(self, job_id, spec_path, kind="job"):
        self.started.append((job_id, json.loads(Path(spec_path).read_text(encoding="utf-8")), kind))
        self.job = job_id

    def kill(self):
        self.killed += 1
        self.complete(None, code=-1)

    def complete(self, result, code=0):
        """The job process ends: ``result`` goes to result.json (None: it crashed)."""
        job_id, spec, kind = self.started[-1]
        out = Path(spec["out_dir"])
        if result is not None:
            (out / "result.json").write_text(json.dumps(result), encoding="utf-8")
        self.job = None
        self.finished.emit(job_id, code, "")


@pytest.fixture()
def env(tmp_path, monkeypatch, qapp):
    from gcws import paths
    from gcws.automation import journal as J, templates
    from gcws.core import proc_method as PM
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(paths, "DATA", data)
    PM.save({"format": "gcws-processing-method", "version": 1, "name": "NIAS", "sections": {
        "quant": {"mode": "nias_mgkg"}, "migration": {"simulant": "x"}}})
    watch = tmp_path / "watch"
    watch.mkdir()
    wf = templates.make("nias", "Lab", source=str(watch), method="NIAS", folder_a=str(tmp_path / "A"),
                        folder_b=str(tmp_path / "B"))
    src = wf.source
    src.params.update(interval_min=1, min_age_min=0, stable_scans=1, quiet_min=30)
    wf.enabled = True
    wf.save()
    return {"data": data, "watch": watch, "wf": wf, "journal": J.Journal(data / "automation" / "journal.sqlite"),
            "tmp": tmp_path}


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _log(folder: Path, names, completed=False):
    head = "_seqline\t_dataname$\t_datapath$\tSdatafile$\n"
    rows = "".join(f"{i}\t{n}\tC:\\x\\{folder.name}\\\t{n}.D\n" for i, n in enumerate(names, 1))
    (folder / "S Sequence Log .TSV").write_text("Starting sequence\n" + head + rows, encoding="utf-8")
    (folder / "S Sequence Log .LOG").write_text("Starting\n" + ("Sequence completed\n" if completed else ""),
                                                encoding="utf-8")


def _acquire(folder: Path, name: str):
    d = folder / f"{name}.D"
    d.mkdir(parents=True)
    (d / "data.ms").write_bytes(b"x" * 64)
    (d / "checksum.xml").write_text("<x/>")
    t = time.time() - 600
    for p in d.iterdir():
        os.utime(p, (t, t))


def _result(spec, state="control", findings=1):
    out = Path(spec["out_dir"])
    files = {}
    for fmt in ("xlsx", "docx"):
        p = out / f"{spec['group']['name']}_NIAS_Report.{fmt}"
        p.write_text(fmt)
        files[fmt] = str(p)
    return {"state": state, "reason": "", "files": {spec["reports"][0]["node"]: files}, "project": "",
            "evidence": {}, "findings": [{"rule": "sml_exceeded", "text": "x"}] * findings, "warnings": [],
            "timings": {}}


@pytest.mark.parametrize("scenario,expected", [("same", "accepted_manual"), ("new", "accepted_manual"),
                                               ("failed", "control"), ("partial", "control")])
def test_edited_accept_waits_for_rereport_and_checks_new_findings(env, scenario, expected):
    from gcws.automation import journal as J
    from gcws.automation.watcher import WatcherCore

    jr, launcher = env["journal"], FakeLauncher()
    core = WatcherCore(jr, launcher)
    core.reload()
    folder = env["watch"] / "batch"
    folder.mkdir()
    batch = jr.batch(env["wf"].id, folder)
    method = env["wf"].methods()[0]
    job = jr.ensure_job(env["wf"].id, method.id, batch["id"], "sample", "Sample",
                        ["Sample_A.D", "Sample_B.D"], {}, "fp", state=J.QUEUED)
    project = env["tmp"] / "edited.gcws"
    project.write_text("{}")
    jr.transition(job.id, J.QUEUED, J.PROCESSING)
    old = [{"rule": "sml_exceeded", "member": "A", "substance": "X", "rt": 1.0, "text": "old"}]
    jr.transition(job.id, J.PROCESSING, J.CONTROL, project_path=str(project), findings=old)

    assert jr.mark_edited(job.id, 1)
    assert jr.accept_edited(job.id, user="analyst")
    queued = jr.job(job.id)
    assert queued.state == J.QUEUED and queued.review_pending["user"] == "analyst"
    assert queued.mode == "rereport" and queued.revision == 2
    reopened = J.Journal(jr.path)
    assert reopened.job(job.id).review_pending == queued.review_pending
    reopened.close()
    core.pump()
    assert launcher.started[-1][1]["project_path"] == str(project)
    result = _result(launcher.started[-1][1])
    if scenario != "partial":
        pdf = Path(launcher.started[-1][1]["out_dir"]) / "updated.pdf"
        pdf.write_text("pdf")
        result["files"][launcher.started[-1][1]["reports"][0]["node"]]["pdf"] = str(pdf)
    result["findings"] = list(old) + ([{"rule": "manual_check", "member": "B", "substance": "Y",
                                         "rt": 2.0, "text": "new"}] if scenario == "new" else [])
    if scenario == "failed":
        result.update(state="failed", files={}, reason="renderer crashed")
    launcher.complete(result)
    reviewed = jr.job(job.id)
    assert reviewed.state == expected and reviewed.review_pending is None
    assert reviewed.reviewer == ("analyst" if scenario in ("same", "new") else None)
    if scenario == "new":                                  # accepted as edited; the new finding is listed
        assert "1 new finding" in reviewed.reason and len(reviewed.findings) == 2
    assert reviewed.export_pending == 0
    if scenario in ("failed", "partial"):
        assert reviewed.edited == 1 and reviewed.project_path == str(project)
        assert ("renderer crashed" if scenario == "failed" else "missing") in reviewed.reason


def test_editing_accepted_report_returns_it_to_control(env):
    from gcws.automation import journal as J

    jr = env["journal"]
    batch = jr.batch(env["wf"].id, env["watch"])
    job = jr.ensure_job(env["wf"].id, env["wf"].methods()[0].id, batch["id"], "sample", "Sample",
                        ["Sample_A.D", "Sample_B.D"], {}, "fp", state=J.QUEUED)
    jr.transition(job.id, J.QUEUED, J.PROCESSING)
    jr.transition(job.id, J.PROCESSING, J.ACCEPTED_AUTO)
    assert jr.mark_edited(job.id, job.revision)
    changed = jr.job(job.id)
    assert changed.state == J.CONTROL and changed.edited == 1 and changed.export_pending == 0
    assert jr.review(job.id, False, "bad data", user="analyst")
    assert jr.job(job.id).state == J.REJECTED


def test_new_unstructured_warning_requires_another_review():
    from gcws.automation.journal import finding_key

    first = {"rule": "processing_warnings", "text": "Word export failed"}
    second = {"rule": "processing_warnings", "text": "PDF export failed"}
    assert finding_key(first) != finding_key(second)


def test_pending_accept_spec_failure_returns_to_control(env, monkeypatch):
    from gcws.automation import journal as J
    from gcws.automation.watcher import WatcherCore

    jr, core = env["journal"], WatcherCore(env["journal"], FakeLauncher())
    core.reload()
    batch = jr.batch(env["wf"].id, env["watch"])
    job = jr.ensure_job(env["wf"].id, env["wf"].methods()[0].id, batch["id"], "pair", "Pair",
                        ["A.D", "B.D"], {}, "fp", state=J.QUEUED)
    jr.transition(job.id, J.QUEUED, J.PROCESSING)
    jr.transition(job.id, J.PROCESSING, J.CONTROL, project_path=str(env["tmp"] / "edited.gcws"))
    assert jr.mark_edited(job.id, job.revision)
    assert jr.accept_edited(job.id)
    monkeypatch.setattr(core, "spec_for", lambda *args: (_ for _ in ()).throw(ValueError("workflow missing")))
    core.pump()
    failed = jr.job(job.id)
    assert failed.state == J.CONTROL and failed.edited == 1 and failed.review_pending is None
    assert "workflow missing" in failed.reason
    assert jr.accept_edited(job.id)
    core.workflows.clear()
    core.pump()
    missing = jr.job(job.id)
    assert missing.state == J.CONTROL and missing.review_pending is None


def test_old_journal_gains_persistent_review_columns(tmp_path):
    from gcws.automation import journal as J

    path = tmp_path / "old.sqlite"
    old_ddl = J._DDL.replace("mode TEXT DEFAULT 'full', edited INTEGER DEFAULT 0, review_pending_json TEXT,",
                             "mode TEXT DEFAULT 'full',")
    con = sqlite3.connect(path)
    con.executescript(old_ddl)
    con.close()
    journal = J.Journal(path)
    columns = {row["name"] for row in journal.con.execute("PRAGMA table_info(jobs)")}
    assert {"edited", "review_pending_json"} <= columns
    journal.close()


def test_changed_sample_revision_refreshes_batch_report(env):
    from gcws.automation import journal as J
    from gcws.automation.watcher import WatcherCore

    jr, core = env["journal"], WatcherCore(env["journal"], FakeLauncher())
    batch_dir = env["watch"] / "batch"
    batch_dir.mkdir()
    batch = jr.batch(env["wf"].id, batch_dir)
    method = env["wf"].methods()[0]
    sample = jr.ensure_job(env["wf"].id, method.id, batch["id"], "sample", "Sample",
                           ["Sample_A.D", "Sample_B.D"], {}, "fp", state=J.QUEUED)
    jr.transition(sample.id, J.QUEUED, J.PROCESSING)
    jr.transition(sample.id, J.PROCESSING, J.ACCEPTED_AUTO)
    core._batch_report(env["wf"], method, batch, batch_dir)
    before = next(j for j in jr.jobs(batch_id=batch["id"]) if j.is_batch)
    assert before.state == J.QUEUED
    jr.transition(before.id, J.QUEUED, J.PROCESSING)
    jr.transition(before.id, J.PROCESSING, J.ACCEPTED_AUTO)
    jr.update_job(sample.id, revision=2)
    core._batch_report(env["wf"], method, batch, batch_dir)
    after = jr.job(before.id)
    assert after.revision == 2 and after.state == J.QUEUED


def test_watching_a_batch_being_acquired(env, qapp):
    from gcws.automation import journal as J
    from gcws.automation.watcher import WatcherCore
    jr, launcher = env["journal"], FakeLauncher()
    now = [time.time()]
    core = WatcherCore(jr, launcher, clock=lambda: now[0])
    notes = []
    core.notify.connect(lambda t, x: notes.append((t, x)))
    core.reload()
    assert list(core.workflows) == [env["wf"].id]
    core.tick()                                            # watching starts with an empty folder
    now[0] += 61
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH)
    for n in BATCH[:2]:
        _acquire(batch, n)
    core.tick()
    jobs = {j.group_name: j for j in jr.jobs()}
    assert set(jobs) == {"26016606_x", "26016607_y"} and all(j.state == J.WAITING for j in jobs.values())
    assert "11_26016606_x_B.D (planned)" in jobs["26016606_x"].reason
    assert launcher.started == []
    for n in BATCH[2:]:
        _acquire(batch, n)
    now[0] += 61
    core.tick()                                            # both samples ready; one job at a time
    assert len(launcher.started) == 1
    job_id, spec, kind = launcher.started[0]
    assert kind == "job" and spec["group"]["members"] == ["07_26016606_x_A.D", "11_26016606_x_B.D"]
    assert spec["blanks"] == {"blank": ["08_EtOH.D", "13_EtOH.D"], "blank_istd": ["06_EtOH_ISTD.D"]}
    assert spec["require_blank"] == "blank" and spec["method"]["name"] == "NIAS" and spec["has_review"]
    assert jr.job(job_id).state == J.PROCESSING
    other = next(j for j in jr.jobs() if j.id != job_id)
    assert jr.job(other.id).state == J.QUEUED
    launcher.complete(_result(spec, "control"))            # Report²: control needed
    j = jr.job(job_id)
    assert j.state == J.CONTROL and j.export_pending == 0 and notes[-1][0].startswith("Report")
    assert not (env["tmp"] / "A").exists()                 # held back until accepted
    assert len(launcher.started) == 2                      # the next sample started at once
    _, spec2, _ = launcher.started[1]
    launcher.complete(_result(spec2, "accepted_auto", findings=0))
    assert jr.job(other.id).state == J.ACCEPTED_AUTO
    assert (env["tmp"] / "A" / batch.name / "26016607_y_NIAS_Report.xlsx").is_file()
    assert (env["tmp"] / "B" / batch.name / "26016607_y_NIAS_Report.docx").is_file()
    # the analyst accepts the first one in Report²: it is delivered on the next look
    assert jr.review(job_id, True, "checked", user="analyst")
    core.tick()
    assert (env["tmp"] / "A" / batch.name / "26016606_x_NIAS_Report.xlsx").is_file()
    # sequence completed, every sample accepted: the batch report is made
    _log(batch, BATCH, completed=True)
    now[0] += 61
    core.tick()
    assert launcher.started[-1][2] == "batch"
    bspec = launcher.started[-1][1]
    assert [e["name"] for e in bspec["entries"]] == ["26016606_x", "26016607_y"]
    assert set(bspec["formats"]) == {"batch_docx", "batch_xlsx"}


def test_crash_timeout_and_retry(env, qapp):
    from gcws.automation import journal as J
    from gcws.automation.watcher import WatcherCore
    jr, launcher = env["journal"], FakeLauncher()
    now = [time.time()]
    core = WatcherCore(jr, launcher, clock=lambda: now[0])
    core.tick()                                            # the first look: an empty folder
    now[0] += 61
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH[:3], completed=True)
    for n in BATCH[:3]:
        _acquire(batch, n)
    core.tick()
    job_id, spec, _ = launcher.started[0]
    launcher.complete(None, code=-1)                       # the job process crashed
    j = jr.job(job_id)
    assert j.state == J.FAILED and "without a result" in j.reason
    assert jr.request(job_id)                               # the analyst: process again
    now[0] += 61
    core.tick()
    assert jr.job(job_id).state == J.PROCESSING and jr.job(job_id).revision == 2
    now[0] += 31 * 60                                       # longer than the method step's time limit
    core.check_timeout(now[0])
    assert launcher.killed == 1 and jr.job(job_id).state == J.FAILED and "timeout" in jr.job(job_id).reason
    jr.request(job_id)
    now[0] += 61
    core.tick()
    _, spec3, _ = launcher.started[-1]
    launcher.complete({"state": "retry", "reason": "file in use"}, code=3)
    j = jr.job(job_id)
    assert j.state == J.QUEUED and j.attempts == 1 and j.not_before > now[0]
    core.pump(now[0])
    assert launcher.job is None                             # waits for the back-off


def test_planned_runs_that_never_came(env, qapp):
    """The sequence ends without the second sample's runs: it is not processed, the first one is."""
    from gcws.automation import journal as J
    from gcws.automation.watcher import WatcherCore
    jr, launcher = env["journal"], FakeLauncher()
    now = [time.time()]
    core = WatcherCore(jr, launcher, clock=lambda: now[0])
    core.tick()
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH)
    for n in BATCH[:2]:
        _acquire(batch, n)
    now[0] += 61
    core.tick()
    assert {j.state for j in jr.jobs()} == {J.WAITING}
    for n in ("08_EtOH", "11_26016606_x_B", "13_EtOH"):
        _acquire(batch, n)
    _log(batch, BATCH, completed=True)                     # 09, 10 and 12 were never measured
    now[0] += 61
    core.tick()
    jobs = {j.group_name: j for j in jr.jobs()}
    assert jobs["26016606_x"].state == J.PROCESSING
    assert jobs["26016607_y"].state == J.NOT_PROCESSED and "sequence ended" in jobs["26016607_y"].reason
    launcher.complete(_result(launcher.started[-1][1], "accepted_auto", findings=0))
    now[0] += 61
    core.tick()                                            # every sample settled: the batch report follows
    assert launcher.started[-1][2] == "batch"
    bspec = launcher.started[-1][1]
    assert [e["name"] for e in bspec["entries"]] == ["26016606_x", "26016607_y"]
    # every reported sample accepted: the batch report counts as accepted (the missing one is listed)
    launcher.complete({"state": "accepted_auto", "files": {}, "findings": []})
    assert next(j for j in jr.jobs() if j.is_batch).state == J.ACCEPTED_AUTO


def test_removed_sample_does_not_hold_up_the_batch(env, qapp):
    """A sample that cannot be processed (its B run is never measured, the sequence goes on) is
    removed from the queue: rescans keep it removed, the batch report no longer waits for it and
    "Process again" brings it back."""
    from gcws.automation import journal as J
    from gcws.automation.watcher import WatcherCore
    jr, launcher = env["journal"], FakeLauncher()
    now = [time.time()]
    core = WatcherCore(jr, launcher, clock=lambda: now[0])
    core.tick()
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH)                                     # not completed: 12 y_B is still planned
    for n in BATCH:
        if n != "12_26016607_y_B":
            _acquire(batch, n)
    now[0] += 61
    core.tick()
    jobs = {j.group_name: j for j in jr.jobs()}
    y = jobs["26016607_y"]
    assert y.state == J.WAITING
    launcher.complete(_result(launcher.started[-1][1], "accepted_auto", findings=0))
    now[0] += 61
    core.tick()
    assert not any(j.is_batch for j in jr.jobs())          # waits for y
    assert jr.remove([y.id, jobs["26016606_x"].id], user="analyst") == [y.id]   # x is done: not removable
    assert jr.job(y.id).state == J.REMOVED and "analyst" in jr.job(y.id).reason
    assert jr.counts().get(J.WAITING, 0) == 0
    now[0] += 61
    core.tick()
    assert jr.job(y.id).state == J.REMOVED                 # the rescan keeps it removed
    assert launcher.started[-1][2] == "batch"
    assert [e["name"] for e in launcher.started[-1][1]["entries"]] == ["26016606_x"]
    launcher.complete({"state": "accepted_auto", "files": {}, "findings": []})
    assert jr.request(y.id) and jr.job(y.id).state == J.QUEUED
    assert not jr.remove([]) and J.STATE_LABELS[J.REMOVED] == "Removed"


def test_first_look_skips_existing_runs(env, qapp):
    from gcws.automation.watcher import WatcherCore
    jr, launcher = env["journal"], FakeLauncher()
    batch = env["watch"] / "26010000_OLD"
    batch.mkdir()
    _log(batch, BATCH[:3], completed=True)
    for n in BATCH[:3]:
        _acquire(batch, n)
    core = WatcherCore(jr, launcher)
    core.tick()
    assert jr.jobs() == [] and launcher.started == []      # already there before watching started


def test_control_socket(env, qapp, qtbot, monkeypatch):
    """The GUI's commands reach the watcher through its local socket (client in another process,
    as in real use)."""
    import subprocess
    import sys
    from gcws import paths
    from gcws.automation import watcher as WM
    from gcws.automation.control import WatcherControl
    name = "gcws-watcher-test-" + str(os.getpid())
    monkeypatch.setattr(WM, "server_name", lambda: name)
    core = WM.WatcherCore(env["journal"], FakeLauncher())
    app = WM.WatcherApp(core, tray=False)
    assert app.listen()
    code = ("import json, sys; from PySide6.QtCore import QCoreApplication; a = QCoreApplication([]); "
            "from gcws.automation.control import WatcherControl; "
            f"print(json.dumps(WatcherControl({name!r}).send('pause', 5000)))")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(paths.ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    qtbot.waitUntil(lambda: proc.poll() is not None, timeout=20000)
    reply = json.loads(proc.stdout.read().decode().strip().splitlines()[-1])
    assert reply["state"] == "paused" and core.paused
    assert app.handle("resume")["state"] == "running"
    app.server.close()
    assert WatcherControl("gcws-watcher-nobody").send("status", 300) is None


def test_autostart_shortcut(tmp_path, monkeypatch):
    pytest.importorskip("win32com.client")
    from gcws.automation import autostart
    monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path / "Startup")
    assert not autostart.is_installed()
    link = autostart.install()
    assert link.is_file() and autostart.is_installed()
    autostart.remove()
    assert not autostart.is_installed()


# -- repairs (Oct 2026): stuck "processing", delivery again -------------------------------------------

def test_alive_knows_running_and_reused_process_numbers():
    import subprocess
    import sys
    from gcws.automation.store import alive
    assert alive(os.getpid()) and alive(os.getpid(), time.time())
    assert not alive(os.getpid(), time.time() - 3 * 86400)     # created after that job started: a reused number
    assert not alive(0) and not alive(None) and not alive("x")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not alive(proc.pid)


def test_job_left_processing_by_a_stopped_watcher_is_queued_again(env, qapp):
    from gcws.automation import journal as J
    from gcws.automation.watcher import WatcherCore
    jr = env["journal"]
    batch = jr.batch(env["wf"].id, env["watch"])
    method = env["wf"].methods()[0].id
    jobs = {}
    for key, pid, started in (("dead", 0, time.time()), ("old", os.getpid(), time.time() - 3 * 86400),
                              ("mine", os.getpid(), time.time()), ("none", None, None)):
        j = jr.ensure_job(env["wf"].id, method, batch["id"], key, key, [f"{key}.D"], {}, "fp", state=J.QUEUED)
        jr.transition(j.id, J.QUEUED, J.PROCESSING, pid=pid, started=started)
        jobs[key] = j.id
    core = WatcherCore(jr, FakeLauncher())
    core.start()
    core.stop()
    states = {k: jr.job(v).state for k, v in jobs.items()}
    assert states == {"dead": J.QUEUED, "old": J.QUEUED, "mine": J.PROCESSING, "none": J.QUEUED}
    assert "stopped" in jr.job(jobs["dead"]).reason and jr.job(jobs["dead"]).pid is None
    assert any("processed again" in e["text"] for e in jr.events(job_id=jobs["none"]))


def _accepted_with_files(env, name="26016606_x"):
    from gcws.automation import journal as J
    jr = env["journal"]
    folder = env["watch"] / "26016605_TEST"
    folder.mkdir(exist_ok=True)
    batch = jr.batch(env["wf"].id, folder)
    method = env["wf"].methods()[0]
    rep = next(n for n in env["wf"].nodes if n.type == "report")
    out = env["tmp"] / "job" / name
    out.mkdir(parents=True)
    files = {}
    for fmt in ("xlsx", "docx"):
        p = out / f"{name}_NIAS_Report.{fmt}"
        p.write_text(fmt)
        files[fmt] = str(p)
    j = jr.ensure_job(env["wf"].id, method.id, batch["id"], name, name, [f"{name}_A.D"], {}, "fp", state=J.QUEUED)
    jr.transition(j.id, J.QUEUED, J.PROCESSING)
    jr.transition(j.id, J.PROCESSING, J.ACCEPTED_AUTO, files={rep.id: files}, export_pending=1)
    return jr.job(j.id), folder


def test_delivered_file_deleted_in_the_target_is_delivered_again(env):
    from gcws.automation import export
    jr = env["journal"]
    job, folder = _accepted_with_files(env)
    export.deliver(jr, env["wf"], job)
    a = env["tmp"] / "A" / folder.name / "26016606_x_NIAS_Report.xlsx"
    b = env["tmp"] / "B" / folder.name / "26016606_x_NIAS_Report.docx"
    assert a.is_file() and b.is_file()
    assert export.deliver(jr, env["wf"], jr.job(job.id)) == []        # delivered and still there
    a.unlink()
    lines = export.deliver(jr, env["wf"], jr.job(job.id))
    assert len(lines) == 1 and a.is_file()
    b.write_text("changed by someone")
    lines = export.deliver(jr, env["wf"], jr.job(job.id), force=True)  # "Deliver now": everything again
    assert len(lines) == 2 and b.read_text() == "docx"
    assert not (b.parent / "26016606_x_NIAS_Report_2.docx").exists()   # its own copy replaced, not versioned
    assert jr.job(job.id).export_state == "done"
    assert set(jr.targets()[job.id]) == {str(a), str(b)}


# -- detection (Oct 2026): data put in again, loose runs, old folders, copies --------------------------

def _age(path: Path, seconds: float):
    """Make ``path`` (and what is in it) look ``seconds`` old."""
    t = time.time() - seconds
    for p in [*path.rglob("*"), path]:
        os.utime(p, (t, t))


def _processed(jr, launcher, state="accepted_auto"):
    """Complete every job the fake launcher starts until the queue is empty."""
    while launcher.job is not None:
        _, spec, kind = launcher.started[-1]
        launcher.complete(_result(spec, state, findings=0) if kind == "job" else
                          {"state": "accepted_auto", "files": {}, "findings": []})


def _core(env):
    from gcws.automation.watcher import WatcherCore
    now = [time.time()]
    launcher = FakeLauncher()
    core = WatcherCore(env["journal"], launcher, clock=lambda: now[0])
    return core, launcher, now


def test_batch_folder_put_in_again_is_processed_again(env, qapp):
    import shutil
    from gcws.automation import journal as J
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()                                            # first look: empty
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH[:3], completed=True)
    for n in BATCH[:3]:
        _acquire(batch, n)
    now[0] += 61
    core.tick()
    _processed(jr, launcher)
    sample = next(j for j in jr.jobs() if not j.is_batch)
    assert sample.state == J.ACCEPTED_AUTO
    jr.delete([j.id for j in jr.jobs()])                   # deleted in Report²
    keep = env["tmp"] / "keep"
    shutil.move(str(batch), str(keep))                    # taken out of the watched folder ...
    now[0] += 61
    core.tick()
    assert jr.batch(env["wf"].id, batch)["missing"] == 1
    shutil.move(str(keep), str(batch))                    # ... and put back: the same data
    now[0] += 61
    core.tick()
    again = jr.job(sample.id)
    assert again.revision == 2 and not again.deleted and again.state == J.PROCESSING
    assert any("put in again" in e["text"] for e in jr.events())
    _processed(jr, launcher)
    # deleted and copied in again before the next look: a new folder (its creation time) - again
    shutil.copytree(str(batch), str(keep))
    shutil.rmtree(batch)
    shutil.copytree(str(keep), str(batch))
    now[0] += 61
    core.tick()
    assert jr.job(sample.id).revision == 3


def test_run_copied_in_again_is_processed_again(env, qapp):
    import shutil
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH[:3], completed=True)
    for n in BATCH[:3]:
        _acquire(batch, n)
    now[0] += 61
    core.tick()
    _processed(jr, launcher)
    sample = next(j for j in jr.jobs() if not j.is_batch)
    jr.delete([sample.id])                                 # deleted in Report²
    run = batch / f"{BATCH[1]}.D"
    copy = env["tmp"] / run.name
    shutil.copytree(str(run), str(copy))                  # keeps the file times: the same fingerprint
    shutil.rmtree(run)
    shutil.copytree(str(copy), str(run))
    now[0] += 61
    core.tick()
    assert jr.job(sample.id).revision == 2 and not jr.job(sample.id).deleted
    _processed(jr, launcher)
    now[0] += 61
    core.tick()
    assert jr.job(sample.id).revision == 2                 # once


def test_runs_dropped_straight_into_the_watched_folder(env, qapp):
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    _log(env["watch"], BATCH[:3], completed=True)
    for n in BATCH[:3]:
        _acquire(env["watch"], n)
    now[0] += 61
    core.tick()
    jobs = [j for j in jr.jobs() if not j.is_batch]
    assert [j.group_name for j in jobs] == ["26016606_x"] and launcher.started
    assert jr.batch_by_id(jobs[0].batch_id)["name"] == env["watch"].name


def test_old_folder_moved_in_later_is_processed(env, qapp):
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    batch = env["tmp"] / "26010000_OLD"
    batch.mkdir()
    _log(batch, BATCH[:3], completed=True)
    for n in BATCH[:3]:
        _acquire(batch, n)
    _age(batch, 40 * 86400)                               # older than "Skip folders older than" (14 days)
    batch.rename(env["watch"] / batch.name)               # moved: keeps its time
    now[0] += 61
    core.tick()
    assert [j.group_name for j in jr.jobs() if not j.is_batch] == ["26016606_x"] and launcher.started


def test_old_folder_there_at_the_first_look_is_not_processed(env, qapp):
    jr = env["journal"]
    batch = env["watch"] / "26010000_OLD"
    batch.mkdir()
    _log(batch, BATCH[:3], completed=True)
    for n in BATCH[:3]:
        _acquire(batch, n)
    _age(batch, 40 * 86400)
    core, launcher, now = _core(env)
    core.tick()
    now[0] += 61
    core.tick()
    assert jr.jobs() == [] and launcher.started == []
    runs = jr.runs(jr.batch(env["wf"].id, batch)["id"])
    assert len(runs) == 3 and all(r["baseline"] for r in runs.values())
    _acquire(batch, "14_26016608_z_A")                    # a new sample in the old folder: processed
    now[0] += 61
    core.tick()
    assert [j.group_name for j in jr.jobs() if not j.is_batch] == ["26016608_z"]


def test_journal_from_before_records_what_is_there(env, qapp):
    """The first look of this version at a folder watched before: old folders and runs lying loose are
    recorded as there before, not processed."""
    jr = env["journal"]
    jr.first_scan(env["wf"].id, env["watch"])              # watched before this version (no census)
    jr.con.execute("UPDATE watched SET census=NULL")
    old = env["watch"] / "26010000_OLD"
    old.mkdir()
    for n in BATCH[:3]:
        _acquire(old, n)
        _acquire(env["watch"], n)
    _age(old, 40 * 86400)
    core, launcher, now = _core(env)
    core.tick()
    now[0] += 61
    core.tick()
    assert [j for j in jr.jobs() if j.state != "waiting"] == [] and launcher.started == []


def test_copied_batch_without_log_does_not_wait_for_the_quiet_time(env, qapp):
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    batch = env["watch"] / "26016605_COPY"
    batch.mkdir()
    for n in BATCH[:3]:
        _acquire(batch, n)
    _age(batch, 2 * 3600)                                  # acquired hours ago, copied in now
    now[0] += 61
    core.tick()                                            # seen: may still be copied
    assert not launcher.started
    now[0] += 61
    core.tick()                                            # unchanged, nothing written for 30 min: quiet
    assert launcher.started and launcher.started[0][1]["group"]["name"] == "26016606_x"
    live = env["watch"] / "26016605_LIVE"
    live.mkdir()
    for n in BATCH[:3]:
        _acquire(live, n)
    _age(live, 60)                                         # being acquired: waits for the quiet time
    for _ in range(3):
        _processed(jr, launcher)
        now[0] += 61
        core.tick()
    _processed(jr, launcher)
    assert not any(k == "job" and spec["batch_folder"] == str(live) for _, spec, k in launcher.started)
    assert any(j.state == "waiting" and "quiet" in j.reason for j in jr.jobs())


def test_the_watcher_writes_what_it_saw(env, qapp):
    """The Folders tab reads the watcher's last look instead of the (maybe slow) watched folder."""
    from gcws.automation import store
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH[:3])
    for n in BATCH[:3]:
        _acquire(batch, n)
    other = env["watch"] / "Archive_2025"
    other.mkdir()
    _acquire(other, "01_x_A")
    src = env["wf"].source
    src.params.update(pattern="2601*")
    env["wf"].save()
    now[0] += 61
    core.tick()
    listing = store.read_json(store.listing_path(env["wf"].id))
    assert listing["reachable"] and listing["root"] == str(env["watch"])
    folders = {f["name"]: f for f in listing["folders"]}
    assert folders["26016605_TEST"]["status"] == "batch" and folders["Archive_2025"]["status"] == "name"
    assert folders["26016605_TEST"]["batch_id"] == jr.batch(env["wf"].id, batch)["id"]
    assert "S Sequence Log .TSV" in folders["26016605_TEST"]["other"]
    assert folders["26016605_TEST"]["has_log"]
    import shutil
    shutil.rmtree(env["watch"])                            # the network drive is gone
    now[0] += 61
    core.tick()
    listing = store.read_json(store.listing_path(env["wf"].id))
    assert not listing["reachable"] and {f["name"] for f in listing["folders"]} == set(folders)


# -- samples added to the queue by hand (Oct 2026) ----------------------------------------------------

def test_samples_added_by_hand_are_processed(env, qapp):
    from gcws.automation import journal as J
    jr = env["journal"]
    old = env["watch"] / "26010000_OLD"                    # there before watching: not processed by itself
    old.mkdir()
    _log(old, BATCH, completed=True)
    for n in BATCH:
        _acquire(old, n)
    core, launcher, now = _core(env)
    core.tick()
    assert jr.jobs() == []
    jr.request_samples(env["wf"].id, old, ["07_26016606_x_A.D"], user="analyst")
    assert jr.forced(jr.batch(env["wf"].id, old)) == {"07_26016606_x_a"}
    now[0] += 61
    core.tick()
    jobs = {j.group_name: j for j in jr.jobs() if not j.is_batch}
    assert set(jobs) == {"26016606_x"} and jobs["26016606_x"].state == J.PROCESSING   # only the one added
    assert launcher.started[-1][1]["blanks"]["blank"] == ["08_EtOH.D", "13_EtOH.D"]
    assert jr.forced(jr.batch(env["wf"].id, old)) == set()                            # taken up
    _processed(jr, launcher)
    # added again once processed (and deleted in Report² meanwhile): a new revision, shown again
    jr.delete([jobs["26016606_x"].id])
    jr.request_samples(env["wf"].id, old, None)
    now[0] += 61
    core.tick()
    again = jr.job(jobs["26016606_x"].id)
    assert again.revision == 2 and not again.deleted
    assert {j.group_name for j in jr.jobs() if not j.is_batch} == {"26016606_x", "26016607_y"}


def test_sample_added_by_hand_does_not_wait_for_the_quiet_time(env, qapp):
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    live = env["watch"] / "26016605_LIVE"
    live.mkdir()
    for n in BATCH[:3]:
        _acquire(live, n)                                  # no sequence log, written 10 min ago
    now[0] += 61
    core.tick()
    now[0] += 61
    core.tick()
    assert not launcher.started and "quiet" in jr.jobs()[0].reason
    jr.request_samples(env["wf"].id, live, None)
    core.scan_now()
    core.tick()
    assert launcher.started and launcher.started[0][1]["group"]["name"] == "26016606_x"


def test_folder_outside_the_watched_folder_added_by_hand(env, qapp):
    from gcws.automation import journal as J
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    outside = env["tmp"] / "elsewhere" / "26016605_OTHER"
    outside.mkdir(parents=True)
    _log(outside, BATCH, completed=True)
    for n in BATCH:
        _acquire(outside, n)
    jr.request_samples(env["wf"].id, outside, ["09_26016607_y_A.D", "12_26016607_y_B.D"], outside=True)
    now[0] += 61
    core.tick()
    jobs = [j for j in jr.jobs() if not j.is_batch]
    assert [j.group_name for j in jobs] == ["26016607_y"] and jobs[0].state == J.PROCESSING
    _processed(jr, launcher)
    now[0] += 61
    core.tick()                                            # only what was added: the batch report follows
    assert launcher.started[-1][2] == "batch"
    assert [e["name"] for e in launcher.started[-1][1]["entries"]] == ["26016607_y"]


def test_samples_added_from_this_pc_go_on_while_the_watched_folder_is_away(env, qapp):
    """Home office without VPN: the watched network folder cannot be reached, a batch on this PC added
    by hand is processed anyway."""
    import shutil
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    shutil.rmtree(env["watch"])                            # X: is not there
    here = env["tmp"] / "C" / "26016605_HERE"
    here.mkdir(parents=True)
    _log(here, BATCH[:3], completed=True)
    for n in BATCH[:3]:
        _acquire(here, n)
    jr.request_samples(env["wf"].id, here, None, outside=True)
    now[0] += 61
    core.tick()
    assert launcher.started and launcher.started[0][1]["batch_folder"] == str(here)


# -- one watcher only, and the GUI never waits for it (Oct 2026) -----------------------------------------

def _holder(name: str, seconds: float = 60):
    """Another process that holds the watcher lock ``name`` (like a running watcher that never answers)."""
    import subprocess
    import sys
    from gcws import paths
    code = (f"import os, time; from gcws.automation import store; ok = store.take_lock({name!r}); "
            f"print('locked' if ok else 'refused', os.getpid(), flush=True); time.sleep({seconds})")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(paths.ROOT), stdout=subprocess.PIPE)
    word, pid = proc.stdout.readline().decode().split()
    assert word == "locked"
    proc.real_pid = int(pid)                               # a venv's python.exe runs the interpreter as a child
    return proc


def _end(proc):
    import signal
    try:
        os.kill(proc.real_pid, signal.SIGTERM)
    except OSError:
        pass
    proc.kill()
    proc.wait()
    from gcws.automation.store import alive
    end = time.monotonic() + 10
    while alive(proc.real_pid) and time.monotonic() < end:
        time.sleep(0.05)


@pytest.fixture()
def lock_name(monkeypatch):
    from gcws.automation import watcher as WM
    name = f"gcws-watcher-test-{os.getpid()}-{time.time_ns()}"
    monkeypatch.setattr(WM, "server_name", lambda: name)
    return name


def test_the_lock_keeps_a_second_process_out_until_the_first_ends(lock_name):
    from gcws.automation import store
    assert not store.lock_held(lock_name)
    proc = _holder(lock_name)
    try:
        assert store.lock_held(lock_name) and not store.take_lock(lock_name)
    finally:
        _end(proc)
    assert not store.lock_held(lock_name)                  # released by Windows when the process ended
    assert store.take_lock(lock_name) and store.lock_held(lock_name)
    store.release_lock(lock_name)
    assert not store.lock_held(lock_name)


def test_no_second_watcher_beside_a_busy_one(env, lock_name, monkeypatch):
    """A watcher busy for minutes neither answers its socket nor writes heartbeats: nothing may start a
    second one beside it (that doubled the CPU load); the state is read without waiting for it."""
    from PySide6.QtCore import QProcess
    from gcws.automation import watcher as WM
    from gcws.automation.control import WatcherControl
    started = []
    monkeypatch.setattr(QProcess, "startDetached", lambda *a: started.append(a) or True)
    ctl = WatcherControl()
    assert ctl.status(env["journal"])["state"] == "stopped" and ctl.start() and len(started) == 1
    proc = _holder(lock_name)
    try:
        jr = env["journal"]
        t0 = time.monotonic()
        assert ctl.status(jr)["state"] == "starting"       # lock taken, no heartbeat yet
        jr.heartbeat("running", "", "", time.time())
        jr.con.execute("UPDATE watcher SET pid=?", (proc.real_pid,))
        assert ctl.status(jr)["state"] == "running"
        jr.con.execute("UPDATE watcher SET heartbeat=?", (time.time() - 120,))
        assert ctl.status(jr)["state"] == "not responding"
        assert time.monotonic() - t0 < 1.0                 # no socket waits
        assert ctl.start() and len(started) == 1           # nothing started beside it
        assert WM.main(["gcws", "--watch"]) == 0           # a watcher started by hand leaves at once
    finally:
        _end(proc)
    assert ctl.status(jr)["state"] == "stopped" and ctl.start() and len(started) == 2


def test_a_watcher_of_an_older_version_counts_as_running(env, lock_name):
    """Watchers from before the lock are known by their heartbeat and process."""
    from gcws.automation.control import WatcherControl
    jr = env["journal"]
    jr.heartbeat("running", "", "", time.time())           # this process plays the old watcher
    assert WatcherControl().running(jr)
    jr.con.execute("UPDATE watcher SET pid=?", (2 ** 30,))  # one that ended
    assert not WatcherControl().running(jr)


def test_restart_ends_a_watcher_that_hangs(env, lock_name, monkeypatch):
    from PySide6.QtCore import QProcess
    from gcws.automation.control import WatcherControl
    started = []
    monkeypatch.setattr(QProcess, "startDetached", lambda *a: started.append(a) or True)
    proc = _holder(lock_name)
    jr = env["journal"]
    jr.heartbeat("running", "", "", time.time() - 600)
    jr.con.execute("UPDATE watcher SET pid=?, heartbeat=?", (proc.real_pid, time.time() - 600))
    try:
        assert WatcherControl().status(jr)["state"] == "not responding"
        assert WatcherControl().restart(jr, wait_s=0.5)
        assert proc.wait(10) is not None and len(started) == 1
    finally:
        _end(proc)


# -- little work per look: finished runs are not read again (Oct 2026) -----------------------------------

def test_finished_runs_are_not_read_again_until_they_change(tmp_path):
    from gcws.automation import scanner as SC
    batch = tmp_path / "26016605_TEST"
    _acquire(batch, "06_EtOH_ISTD")
    live = batch / "07_26016606_x_A.D"
    live.mkdir()
    (live / "data.ms").write_bytes(b"x")                   # still being acquired: no checksum.xml yet
    cache, now = SC.LookCache(reverify_s=600), 1000.0
    for _ in range(3):
        SC.observe(batch, cache, now)                      # the folder listing may lag behind: walked
    n = cache.walked
    SC.observe(batch, cache, now)
    assert cache.walked == n + 1                           # only the run still being acquired
    (batch / "06_EtOH_ISTD.D" / "data.ms").write_bytes(b"y" * 99)       # rewritten in place
    (batch / "06_EtOH_ISTD.D" / "report.txt").write_text("new file")
    obs = {o.stem: o for o in SC.observe(batch, cache, now + 601)}
    assert obs["06_etoh_istd"].fingerprint == SC.fingerprint(batch / "06_EtOH_ISTD.D")[0]   # seen at the re-check
    assert [(o.stem, o.fingerprint) for o in SC.observe(batch)] == \
        [(o.stem, o.fingerprint) for o in SC.observe(batch, cache, now + 602)]             # same as without cache
    # deleted and copied in again: a new entry, read at once
    import shutil
    shutil.copytree(batch / "06_EtOH_ISTD.D", tmp_path / "keep.D")
    shutil.rmtree(batch / "06_EtOH_ISTD.D")
    (tmp_path / "keep.D" / "extra.txt").write_text("x")
    shutil.copytree(tmp_path / "keep.D", batch / "06_EtOH_ISTD.D")
    obs = {o.stem: o for o in SC.observe(batch, cache, now + 603)}
    assert obs["06_etoh_istd"].fingerprint == SC.fingerprint(batch / "06_EtOH_ISTD.D")[0]


def test_the_sequence_log_is_read_again_only_when_it_changes(tmp_path):
    from gcws.automation import scanner as SC
    batch = tmp_path / "w" / "26016605_TEST"
    batch.mkdir(parents=True)
    _log(batch, BATCH)
    for n in BATCH[:2]:
        _acquire(batch, n)
    cache = SC.LookCache(reverify_s=600)
    names = [o.name for o in SC.observe(batch, cache, 0)]
    assert not cache.sequence(batch, names, 0).finished and cache.read == 1
    SC.observe(batch, cache, 10)
    cache.sequence(batch, names, 10)
    assert cache.read == 1                                 # nothing changed: not read again
    _log(batch, BATCH, completed=True)
    SC.observe(batch, cache, 20)
    assert cache.sequence(batch, names, 20).finished and cache.read == 2
    # no log of its own: the neighbours' logs are searched, again every reverify_s / 2 at the latest
    other = tmp_path / "w" / "26016606_COPY"
    _acquire(other, "01_EtOH")
    SC.observe(other, cache, 0)
    cache.sequence(other, ["01_EtOH.D"], 0)
    cache.sequence(other, ["01_EtOH.D"], 200)
    assert cache.read == 3
    cache.sequence(other, ["01_EtOH.D"], 301)
    assert cache.read == 4


def test_a_look_that_finds_nothing_new_writes_nothing(env, qapp):
    """Every look used to rewrite every run and take a write lock per sample (also making Report²
    refresh); now an unchanged folder leaves the journal untouched."""
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH, completed=True)
    for n in BATCH:
        _acquire(batch, n)
    for _ in range(4):
        now[0] += 61
        core.tick()
        _processed(jr, launcher)
    before = jr.con.total_changes
    for _ in range(3):
        now[0] += 61
        core.tick()
    assert jr.con.total_changes == before


def test_the_folders_are_looked_at_in_a_thread_of_their_own(env, qapp, qtbot, monkeypatch):
    """A slow network drive must not hold up the watcher: while it looks, it answers and keeps its
    heartbeat; what the look found is processed afterwards."""
    import threading
    from gcws.automation import scanner as SC
    from gcws.automation.watcher import WatcherCore
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH, completed=True)
    for n in BATCH:
        _acquire(batch, n)
    slow, threads = threading.Event(), set()
    real = SC.batch_folders

    def batch_folders(*a, **k):
        threads.add(threading.get_ident())
        slow.wait(10)                                      # the drive takes its time
        return real(*a, **k)

    monkeypatch.setattr(SC, "batch_folders", batch_folders)
    launcher = FakeLauncher()
    core = WatcherCore(env["journal"], launcher, threaded=True)
    env["wf"].source.params.update(process_existing=True)
    env["wf"].save()
    core.reload()
    t0 = time.monotonic()
    core.tick()
    assert time.monotonic() - t0 < 1 and core.looking and threading.get_ident() not in threads
    core.heartbeat()
    core.tick()                                            # no second look while one is under way
    assert env["journal"].watcher_status()["state"] == "running"
    slow.set()
    qtbot.waitUntil(lambda: bool(launcher.started), timeout=10000)
    assert not core.looking and launcher.started[0][2] == "job"
    assert {j.group_name for j in env["journal"].jobs()} == {"26016606_x", "26016607_y"}


def test_sample_removed_from_the_queue_stays_removed_while_a_request_is_open(env, qapp):
    """Remove from queue works also for samples of a batch added by hand: the open request does not put
    them back; adding them again does."""
    from gcws.automation import journal as J
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    live = env["watch"] / "26016605_LIVE"
    live.mkdir()
    for n in BATCH[:3]:
        _acquire(live, n)
    jr.request_samples(env["wf"].id, live, None)
    core.scan_now()
    core.tick()
    job = next(j for j in jr.jobs() if not j.is_batch)
    jr.update_job(job.id, state=J.QUEUED, pid=None)       # waiting for the watcher again
    assert jr.remove([job.id]) == [job.id]
    b = jr.batch(env["wf"].id, live)
    jr.update_batch(b["id"], force_json='["*"]')           # a request still open
    now[0] += 61
    core.scan_now()
    core.tick()
    assert jr.job(job.id).state == J.REMOVED
    jr.request_samples(env["wf"].id, live, ["07_26016606_x_A.D"])
    assert jr.job(job.id).state == J.WAITING


def test_deleted_batch_folder_leaves_the_queue(env, qapp):
    import shutil
    from gcws.automation import journal as J
    jr = env["journal"]
    core, launcher, now = _core(env)
    core.tick()
    live = env["watch"] / "26016605_LIVE"
    live.mkdir()
    for n in BATCH[:3]:
        _acquire(live, n)                                  # no sequence log: waits for the quiet time
    now[0] += 61
    core.tick()
    [job] = [j for j in jr.jobs() if not j.is_batch]
    assert job.state == J.WAITING
    shutil.rmtree(live)
    now[0] += 61
    core.scan_now()
    core.tick()
    assert jr.job(job.id).state == J.REMOVED and jr.batch(env["wf"].id, live)["missing"] == 1
    # a folder added by hand outside the watched folder and deleted: the request is dropped
    outside = env["tmp"] / "elsewhere" / "26016605_OTHER"
    outside.mkdir(parents=True)
    jr.request_samples(env["wf"].id, outside, None, outside=True)
    outside.rmdir()
    now[0] += 61
    core.scan_now()
    core.tick()
    b = jr.batch(env["wf"].id, outside)
    assert jr.forced(b) == set() and b["missing"] == 1
