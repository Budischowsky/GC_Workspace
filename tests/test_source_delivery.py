"""Reports delivered back into the source: batch and sample reports into the batch folder; the watcher does not
take the evaluation folders for new data."""
import time
from pathlib import Path

from test_watcher import _accepted_with_files, env, qapp  # noqa: F401 (fixtures)


def _run(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "data.ms").write_bytes(b"x" * 64)
    return path


def test_fingerprint_ignores_the_evaluation_folders(tmp_path):
    from gcws.automation.scanner import fingerprint
    run = _run(tmp_path / "05_X_A.D")
    before = fingerprint(run)[0]
    (run / "Auswertung").mkdir()
    (run / "Auswertung" / "X_NIAS_Report.docx").write_text("report")
    (run / "GCWS").mkdir()
    (run / "GCWS" / "x.xlsx").write_text("x")
    assert fingerprint(run)[0] == before
    (run / "data2.ms").write_bytes(b"y")
    assert fingerprint(run)[0] != before


def test_target_dir_source():
    from gcws.automation.routing import target_dir
    p = {"target": "source", "subfolder": "Auswertung"}
    tokens = {"batch_dir": r"C:\x\B1", "sample": "X", "batch": "B1"}
    assert target_dir(p, tokens) == Path(r"C:\x\B1\Auswertung")
    assert target_dir(p, dict(tokens, sample="")) == Path(r"C:\x\B1\Auswertung")
    assert target_dir(dict(p, subfolder="Auswertung/{sample}"), tokens) == Path(r"C:\x\B1\Auswertung\X")
    assert target_dir({"target": "source", "subfolder": ""}, dict(tokens, sample="")) == Path(r"C:\x\B1")
    assert target_dir({"path": r"D:\out", "subfolder": "{batch}"}, tokens) == Path(r"D:\out\B1")


def _to_source(wf, subfolder="Auswertung"):
    folder = next(n for n in wf.nodes if n.type == "folder")
    folder.params.update(target="source", path="", subfolder=subfolder)
    return folder


def test_sample_report_delivered_into_the_batch_folder(env):
    from gcws.automation import export
    _to_source(env["wf"])
    job, folder = _accepted_with_files(env)
    _run(folder / "26016606_x_A.D")
    lines = export.deliver(env["journal"], env["wf"], job)
    target = folder / "Auswertung" / "26016606_x_NIAS_Report.xlsx"
    assert target.is_file(), lines
    assert env["journal"].job(job.id).export_state == "done"


def test_batch_report_delivered_into_the_batch_folder(env):
    from gcws.automation import export
    from gcws.automation import journal as J
    _to_source(env["wf"])
    jr = env["journal"]
    folder = env["watch"] / "26016605_TEST"
    folder.mkdir()
    batch = jr.batch(env["wf"].id, folder)
    method = env["wf"].methods()[0]
    rep = next(n for n in env["wf"].nodes if n.type == "report")
    src = env["tmp"] / "job" / "batch.xlsx"
    src.parent.mkdir(parents=True)
    src.write_text("batch")
    j = jr.ensure_job(env["wf"].id, method.id, batch["id"], J.BATCH_KEY, "26016605_TEST", [], {}, "fp",
                      state=J.QUEUED)
    jr.transition(j.id, J.QUEUED, J.PROCESSING)
    jr.transition(j.id, J.PROCESSING, J.ACCEPTED_AUTO, files={rep.id: {"batch_xlsx": str(src)}}, export_pending=1)
    export.deliver(jr, env["wf"], jr.job(j.id))
    assert (folder / "Auswertung" / "batch.xlsx").is_file()


def test_validate_source_target(env):
    from gcws.automation.workflow import errors, validate
    folder = _to_source(env["wf"])
    assert not [i for i in errors(validate(env["wf"], check_paths=False)) if i.item == folder.id]


def test_folder_dialog_offers_the_source_folder(qapp):
    from gcws.automation import workflow as W
    from gcws.ui.automation.node_dialogs import NodeDialog
    wf = W.Workflow(name="x")
    node = wf.add_node("folder", 0, 0, path=r"D:\out")
    dlg = NodeDialog(node)
    target = dlg.w["target"]
    assert target.currentData() == "path" and dlg.w["path"].isEnabled()
    target.setCurrentIndex(target.findData("source"))
    assert not dlg.w["path"].isEnabled()
    dlg.w["subfolder"].setText("Auswertung")
    v = dlg.values()
    assert v["target"] == "source" and v["subfolder"] == "Auswertung"
    dlg.deleteLater()
