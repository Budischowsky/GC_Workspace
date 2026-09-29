"""P60: the watcher - scanning a watched folder while the instrument writes, the job queue (one
job at a time, crashes, timeouts), Report² review and delivery, the batch report, the control
socket and the opt-in autostart. The job processes are replaced by a fake launcher."""
import json
import os
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
