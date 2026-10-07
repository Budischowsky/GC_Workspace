"""The Local copy step: runs are copied from the (network) watched folder to a folder on this PC and
processed from there. The workflow model, the copy itself and the journal - without Qt."""
import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest


@pytest.fixture()
def data(tmp_path, monkeypatch):
    from gcws import paths
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    return tmp_path


def _workflow(tmp_path, local="local"):
    from gcws.automation import templates
    src = tmp_path / "watch"
    src.mkdir(exist_ok=True)
    wf = templates.make("nias", "Example", source=str(src), method="NIAS", folder_a=str(tmp_path / "A"),
                        folder_b=str(tmp_path / "B"))
    wf.edges[0].filter = {"name": "2601*"}
    cp = wf.insert_copy(160, 300, folder=str(tmp_path / local) if local else "")
    return wf, cp


def _run(folder: Path, name: str, size: int = 64, age: float = 600) -> Path:
    d = folder / f"{name}.D"
    (d / "AcqData").mkdir(parents=True)
    (d / "data.ms").write_bytes(b"x" * size)
    (d / "AcqData" / "MSScan.bin").write_bytes(b"y" * size)
    (d / "checksum.xml").write_text("<x/>")
    t = time.time() - age
    for p in [d / "data.ms", d / "AcqData" / "MSScan.bin", d / "checksum.xml"]:
        os.utime(p, (t, t))
    return d


def test_insert_copy_puts_the_step_between_folder_and_method(data):
    from gcws.automation import workflow as W
    wf, cp = _workflow(data)
    m = wf.methods()[0]
    assert [(wf._type(e.src), wf._type(e.dst)) for e in wf.incoming(m.id)] == [("copy", "method")]
    via, edges = wf.feed(m.id)
    assert via is cp and wf.copy_step is cp
    assert [(wf._type(e.src), wf._type(e.dst)) for e in edges] == [("source", "copy"), ("copy", "method")]
    assert edges[1].filter == {"name": "2601*"}           # the arrow's filter stays on the way to the method
    plain = W.Workflow.from_dict(wf.to_dict())
    plain.remove(cp.id)
    plain.connect(plain.source.id, m.id)
    assert plain.feed(m.id)[0] is None and plain.copy_step is None
    assert W.summary(cp).startswith(str(data / "local"))
    assert not W.errors(W.validate(wf, method_names=["NIAS"], word=True)), W.validate(wf, method_names=["NIAS"])


def test_validation_of_the_local_copy(data):
    from gcws.automation import workflow as W
    texts = lambda wf: " | ".join(i.text for i in W.validate(wf, method_names=["NIAS"], word=True))
    wf, cp = _workflow(data, local="")
    assert "Choose the local folder" in texts(wf)
    cp.params["folder"] = str(data / "watch" / "copies")
    assert "inside the watched folder" in texts(wf)
    cp.params["folder"] = str(data / "new")
    assert "will be created" in texts(wf) and not W.errors(W.validate(wf, method_names=["NIAS"], word=True))
    both = wf.copy()
    both.connect(both.source.id, both.methods()[0].id)
    assert "not from both" in texts(both)
    second = wf.copy()
    second.add_node("copy", folder=str(data / "other"))
    assert "one local copy" in texts(second)
    only = wf.copy()
    for e in list(only.outgoing(only.copy_step.id)):
        only.remove(e.id)
    only.connect(only.source.id, only.methods()[0].id)
    assert "only copied" in texts(only)                   # a workflow may also just copy
    away = wf.copy()
    away.source.params["folder"] = str(data / "offline")  # X: without the VPN: a note, it can be switched on
    assert "cannot be reached now" in texts(away) and not W.errors(W.validate(away, method_names=["NIAS"]))
    assert not any(i.text.startswith("Connect the watched") for i in W.validate(only, method_names=["NIAS"]))


def test_local_batch_keeps_the_layout_below_the_watched_folder(tmp_path):
    from gcws.automation import localcopy as LC
    assert LC.local_batch("C:\\GC Data", "X:\\GC", "X:\\GC\\2610\\26100001_A") == Path("C:\\GC Data\\2610\\26100001_A")
    assert LC.local_batch("C:\\GC Data", "X:\\GC", "X:\\GC") == Path("C:\\GC Data")
    assert LC.local_batch("C:\\GC Data", "X:\\GC", "Y:\\Other\\B1") == Path("C:\\GC Data\\B1")


def test_copy_tree_copies_what_is_missing_and_leaves_the_rest(tmp_path):
    from gcws.automation import localcopy as LC
    src = _run(tmp_path / "x", "07_S_A")
    dst = tmp_path / "c" / "07_S_A.D"
    os.chmod(src / "data.ms", stat.S_IREAD)                # raw data are often read-only
    assert LC.copy_tree(src, dst) == (3, 2 * 64 + 4)
    assert (dst / "AcqData" / "MSScan.bin").read_bytes() == b"y" * 64
    assert abs((dst / "data.ms").stat().st_mtime - (src / "data.ms").stat().st_mtime) < 2
    assert LC.copy_tree(src, dst) == (0, 0)                # unchanged: nothing to do
    (dst / "Results").mkdir()
    (dst / "Results" / "mine.txt").write_text("analyst")   # added to the copy: stays
    os.chmod(src / "data.ms", stat.S_IREAD | stat.S_IWRITE)
    (src / "data.ms").write_bytes(b"z" * 80)               # changed at the source: copied again
    assert LC.copy_tree(src, dst) == (1, 80)
    assert (dst / "data.ms").read_bytes() == b"z" * 80 and (dst / "Results" / "mine.txt").exists()
    assert not list(dst.rglob("*" + LC.PART))
    single = tmp_path / "x" / "26012850_Sample1_A.qgd"
    single.write_bytes(b"q" * 10)
    assert LC.copy_tree(single, tmp_path / "c" / single.name) == (1, 10)


def test_run_copy_and_files_beside_the_runs(tmp_path):
    from gcws.automation import localcopy as LC
    from gcws.automation import scanner as SC
    batch = tmp_path / "x" / "B1"
    a, b = _run(batch, "07_S_A"), _run(batch, "08_EtOH")
    (batch / "S Sequence Log .TSV").write_text("log")
    sig = LC.extras_signature(batch)
    assert sig and [e.name for e in LC.extra_files(batch)] == ["S Sequence Log .TSV"]
    spec = {"src": str(batch), "dst": str(tmp_path / "c" / "B1"),
            "runs": [{"stem": "07_s_a", "name": a.name, "fingerprint": SC.fingerprint(a)[0]},
                     {"stem": "08_etoh", "name": b.name, "fingerprint": "changed-since"},
                     {"stem": "09_gone", "name": "09_gone.D", "fingerprint": "x"}]}
    res = LC.run_copy(spec)
    assert res["copied"] == {"07_s_a": SC.fingerprint(a)[0]}
    assert set(res["failed"]) == {"08_etoh", "09_gone"} and "changed" in res["failed"]["08_etoh"]
    assert res["extras"] == sig and (tmp_path / "c" / "B1" / "S Sequence Log .TSV").read_text() == "log"
    # a job that finds its copy deleted copies the run again itself
    import shutil
    shutil.rmtree(tmp_path / "c" / "B1" / a.name)
    assert LC.ensure_runs(batch, tmp_path / "c" / "B1", [a.name, b.name]) == [a.name]
    assert (tmp_path / "c" / "B1" / a.name / "data.ms").is_file()


def test_old_journal_gains_the_copy_columns(tmp_path):
    from gcws.automation import journal as J
    path = tmp_path / "old.sqlite"
    old = J._DDL.replace(" copied_fp TEXT, copy_error TEXT,", "").replace(" local_folder TEXT, copied_extras TEXT,", "")
    assert old != J._DDL
    con = sqlite3.connect(path)
    con.executescript(old)
    con.close()
    jr = J.Journal(path)
    assert {"copied_fp", "copy_error"} <= {r["name"] for r in jr.con.execute("PRAGMA table_info(runs)")}
    assert {"local_folder", "copied_extras"} <= {r["name"] for r in jr.con.execute("PRAGMA table_info(batches)")}
    b = jr.batch("wf", tmp_path / "B1")
    jr.upsert_run(b["id"], "07", fingerprint="f", copied_fp="f")
    assert jr.runs(b["id"])["07"]["copied_fp"] == "f"
    jr.close()


# -- the watcher ---------------------------------------------------------------------------------

BATCH = ["06_EtOH_ISTD", "07_26016606_x_A", "08_EtOH", "09_26016607_y_A", "10_EtOH", "11_26016606_x_B",
         "12_26016607_y_B", "13_EtOH"]


class FakeCopier:
    """The copy process, run at once in this process when ``complete()`` is called."""

    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class _S(QObject):
            finished = Signal(str, int, str)
        self._s = _S()
        self.finished = self._s.finished
        self.started, self.busy = [], False

    def running(self):
        return self.busy

    def start(self, job_id, spec_path, kind="job"):
        import json
        self.started.append((json.loads(Path(spec_path).read_text(encoding="utf-8")), kind))
        self.busy = True

    def kill(self):
        self.busy = False
        self.finished.emit("", -1, "")

    def complete(self):
        import json
        from gcws.automation import localcopy as LC
        spec = self.started[-1][0]
        (Path(spec["out_dir"]) / "result.json").write_text(json.dumps(LC.run_copy(spec)), encoding="utf-8")
        self.busy = False
        self.finished.emit("", 0, "")


@pytest.fixture()
def env(tmp_path, monkeypatch, qapp):
    from gcws import paths
    from gcws.automation import journal as J
    from gcws.core import proc_method as PM
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    (tmp_path / "data").mkdir()
    PM.save({"format": "gcws-processing-method", "version": 1, "name": "NIAS", "sections": {
        "quant": {"mode": "nias_mgkg"}, "migration": {"simulant": "x"}}})
    wf, cp = _workflow(tmp_path)
    wf.edges[0].filter = {}
    wf.source.params.update(interval_min=1, min_age_min=0, stable_scans=1, quiet_min=30)
    wf.enabled = True
    wf.save()
    return {"wf": wf, "watch": tmp_path / "watch", "local": tmp_path / "local", "tmp": tmp_path,
            "journal": J.Journal(tmp_path / "data" / "automation" / "journal.sqlite")}


def test_runs_are_copied_and_processed_from_the_local_folder(env):
    from test_watcher import FakeLauncher, _log
    from gcws.automation import journal as J
    from gcws.automation.watcher import WatcherCore
    jr, launcher, copier = env["journal"], FakeLauncher(), FakeCopier()
    now = [time.time()]
    core = WatcherCore(jr, launcher, clock=lambda: now[0], copier=copier)
    core.tick()                                            # watching starts with an empty folder
    now[0] += 61
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH)
    for n in BATCH:
        _run(batch, n)
    core.tick()                                            # all finished on X:, none copied yet
    assert launcher.started == [] and len(copier.started) == 1
    spec, kind = copier.started[0]
    assert kind == "copy" and spec["dst"] == str(env["local"] / batch.name) and len(spec["runs"]) == 8
    jobs = {j.group_name: j for j in jr.jobs()}
    assert all(j.state == J.WAITING and "(being copied)" in j.reason for j in jobs.values()), jobs
    assert "copying 26016605_TEST" in core.copying_text()
    core.tick()                                            # one copy pass at a time
    assert len(copier.started) == 1
    copier.complete()
    local = env["local"] / batch.name
    assert sorted(p.name for p in local.iterdir()) == sorted([f"{n}.D" for n in BATCH] + [
        "S Sequence Log .LOG", "S Sequence Log .TSV"])
    assert all(r["copied_fp"] == r["fingerprint"] for r in jr.runs(jr.batch(env["wf"].id, batch)["id"]).values())
    assert jr.batch(env["wf"].id, batch)["local_folder"] == str(local)
    assert any("8 run(s) copied" in e["text"] for e in jr.events())
    now[0] += 5
    core.tick()                                            # planned again at once: processed from C:
    job_id, jspec, _ = launcher.started[0]
    assert jspec["batch_folder"] == str(local) and jspec["source_folder"] == str(batch)
    assert jspec["group"]["members"] == ["07_26016606_x_A.D", "11_26016606_x_B.D"]
    assert len(copier.started) == 1                        # nothing more to copy
    # the sequence log changes: only the files beside the runs are copied again
    _log(batch, BATCH, completed=True)
    now[0] += 61
    core.tick()
    assert len(copier.started) == 2 and copier.started[1][0]["runs"] == []
    copier.complete()
    assert "Sequence completed" in (local / "S Sequence Log .LOG").read_text()


def test_a_failed_copy_is_logged_once_and_tried_again(env, monkeypatch):
    from test_watcher import FakeLauncher
    from gcws.automation import localcopy as LC
    from gcws.automation.watcher import WatcherCore
    jr, copier = env["journal"], FakeCopier()
    now = [time.time()]
    core = WatcherCore(jr, FakeLauncher(), clock=lambda: now[0], copier=copier)
    core.tick()
    now[0] += 61
    batch = env["watch"] / "B1"
    _run(batch, "07_26016606_x_A")
    real = LC.copy_tree
    monkeypatch.setattr(LC, "copy_tree", lambda s, d: (_ for _ in ()).throw(OSError("disk full")))
    for _ in range(2):
        core.tick()
        copier.complete()
        now[0] += 61
    warnings = [e["text"] for e in jr.events() if "could not be copied" in e["text"]]
    assert len(warnings) == 1 and "disk full" in warnings[0]
    monkeypatch.setattr(LC, "copy_tree", real)
    core.tick()
    copier.complete()
    run = next(iter(jr.runs(jr.batch(env["wf"].id, batch)["id"]).values()))
    assert run["copied_fp"] == run["fingerprint"] and not run["copy_error"]
    # a copy process that hangs is stopped and tried again
    (batch / "S Sequence Log .TSV").write_text("new")
    now[0] += 61
    core.tick()
    assert copier.busy
    now[0] += LC.TIMEOUT_S + 1
    core.check_timeout(now[0])
    assert not copier.busy and core.copying is None
    assert any("stopped after" in e["text"] for e in jr.events())


def test_a_watched_folder_that_cannot_be_reached_is_waited_for(env):
    from test_watcher import FakeLauncher
    from gcws.automation.watcher import WatcherCore
    jr = env["journal"]
    env["watch"].rename(env["tmp"] / "offline")            # X: without the VPN
    core = WatcherCore(jr, FakeLauncher(), copier=FakeCopier())
    core.tick()
    core._last_scan.clear()
    core.tick()
    assert list(core.workflows) == [env["wf"].id]          # not refused: waited for
    texts = [e["text"] for e in jr.events()]
    assert sum("cannot be reached" in t for t in texts) == 1 and not any("not started" in t for t in texts)
    (env["tmp"] / "offline").rename(env["watch"])
    _run(env["watch"] / "B1", "07_26016606_x_A")           # there before watching could start
    core._last_scan.clear()
    core.tick()
    assert any("reached again" in e["text"] for e in jr.events())
    assert jr.jobs() == []                                 # the first look only takes note


# -- real data ---------------------------------------------------------------------------------------

def test_copy_process_and_a_job_from_the_local_copy(samples, tmp_path, monkeypatch, qapp):
    """``python -m gcws --copy-runs``, then a sample processed from C: whose blank copy was deleted."""
    import json
    import shutil
    import subprocess
    import sys
    from test_pipeline import A, B, copy_batch, lib_oracle, snapshot, spec
    from gcws import paths
    from gcws.automation import headless as H
    from gcws.automation import localcopy as LC
    from gcws.automation import pipeline as PL
    from gcws.automation import scanner as SC
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy2(paths.DATA / "settings.json", data / "settings.json")
    monkeypatch.setattr(paths, "DATA", data)
    batch = copy_batch(samples, tmp_path / "watch", ["06_", "07_", "08_", "11_", "13_"])
    before = snapshot(batch)
    local = tmp_path / "local" / batch.name
    runs = sorted(p for p in batch.iterdir() if p.suffix == ".D")
    out = tmp_path / "copy"
    out.mkdir()
    (out / "spec.json").write_text(json.dumps({"src": str(batch), "dst": str(local), "extras": True, "runs": [
        {"stem": p.stem.casefold(), "name": p.name, "fingerprint": SC.fingerprint(p)[0]} for p in runs]}),
        encoding="utf-8")
    done = subprocess.run([sys.executable, "-m", "gcws", "--copy-runs", str(out / "spec.json")],
                          cwd=str(paths.ROOT), env=dict(os.environ, GCWS_DATA=str(data)), timeout=300,
                          capture_output=True)
    assert done.returncode == 0, done.stdout[-800:]
    res = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert len(res["copied"]) == 5 and not res["failed"] and res["extras"] == LC.extras_signature(batch)
    assert {k: v[0] for k, v in snapshot(local).items()} == {k: v[0] for k, v in snapshot(batch).items()}
    shutil.rmtree(local / "13_EtOH.D")                     # the analyst tidied up the copy
    res = PL.run_job(spec(local, tmp_path / "job", source_folder=str(batch)), identify=lib_oracle)
    assert res.state in (PL.ACCEPTED_AUTO, PL.CONTROL), res.reason
    assert (local / "13_EtOH.D" / "data.ms").is_file()     # copied again by the job
    ws = H.new_workspace()
    assert not [n for n in H.open_project(ws, res.project) if "not found" in n]
    assert {Path(st.run.path).parent for st in ws.states()} == {local}   # the project points to C:
    assert snapshot(batch) == before                       # the watched folder is only read


# -- the chart editor and Report² -------------------------------------------------------------------

def test_editor_puts_the_local_copy_in_place(qtbot, data):
    from gcws.automation import templates
    from gcws.automation import workflow as W
    from gcws.core import proc_method as PM
    from gcws.ui.automation.editor import WorkflowEditor
    from gcws.ui.automation.items import EdgeItem
    from gcws.ui.automation.node_dialogs import NodeDialog
    PM.save({"format": "gcws-processing-method", "version": 1, "name": "NIAS",
             "sections": {"quant": {"mode": "nias_mgkg"}, "migration": {"simulant": "x"}}})
    watch = data / "watch"
    watch.mkdir()
    ed = WorkflowEditor(templates.make("simple", "Lab", source=str(watch), method="NIAS", folder_a=str(data / "A")))
    qtbot.addWidget(ed)
    titles = [ed.palette.item(i).text() for i in range(ed.palette.count())]
    assert titles[:3] == ["Watched folder", "Local copy", "Method"]
    x_method = ed.wf.methods()[0].x
    cp = ed.add_node("copy", 160, 300, folder=str(data / "local"))
    m = ed.wf.methods()[0]
    assert ed.wf.feed(m.id)[0].id == cp.id
    assert (cp.x, cp.y) == (x_method, ed.wf.source.y) and m.x > cp.x     # in line, the rest moved right
    assert sum(isinstance(i, EdgeItem) for i in ed.scene.items()) == 4
    assert not W.errors(ed.validate())
    ed.undo.undo()                                         # one step: the step and its arrows
    assert ed.wf.copy_step is None and ed.wf.source_edge(m.id) is not None
    ed.undo.redo()
    dlg = NodeDialog(ed.wf.copy_step, source_folder=r"X:\GC")
    qtbot.addWidget(dlg)
    dlg.w["folder"].setText(r"C:\GC Data")
    assert dlg.values()["folder"] == r"C:\GC Data"
    assert dlg.where.text() == r"X:\GC\<batch folder>  →  C:\GC Data\<batch folder>"
    ed.dirty = False                                       # closing must not ask to save


def test_report2_opens_the_local_copy_of_a_batch(qtbot, data, tmp_path, monkeypatch):
    from test_report2_ui import _dock, _seed
    wf, jr, ids, batch = _seed(data, tmp_path)
    local = tmp_path / "local" / batch.name
    local.mkdir(parents=True)
    jr.update_batch(jr.batch(wf.id, batch)["id"], local_folder=str(local))
    dock = _dock(jr, monkeypatch, qtbot)
    opened = []
    monkeypatch.setattr(dock, "open_path", lambda p: opened.append(p))
    top = dock.tree.topLevelItem(0)
    menu = dock._tree_context_menu(dock.tree.visualItemRect(top).center())
    action = next(a for a in menu.actions() if a.text() == "Open the local copy")
    assert action.isEnabled()
    action.trigger()
    assert opened == [str(local)]
