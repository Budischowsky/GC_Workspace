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
