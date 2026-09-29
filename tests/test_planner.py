"""P58: the journal, the watched-folder scanner and the batch planner (which sample can be
processed now) - on synthetic batch folders, no Qt."""
import os
import time
from pathlib import Path

import pytest

BATCH = ["06_EtOH_ISTD", "07_26016606_130m_min_GIOSUN1635_A", "08_EtOH", "09_26016607_170m_min_GIOSUN1635_A",
         "10_EtOH", "11_26016606_130m_min_GIOSUN1635_B", "12_26016607_170m_min_GIOSUN1635_B", "13_EtOH"]
TSV_HEAD = "_seqline\t_dataname$\t_datapath$\tSdatafile$\t_runtype$\n"


def _write_log(folder: Path, names, other=(), completed=False):
    rows = [f"{i}\t{n}\tC:\\Messdaten\\2026\\August\\26017729_other\\\t{n}.D\tBlank\n" for i, n in enumerate(other, 1)]
    rows += [f"{i}\t{n}\tC:\\Messdaten\\2026\\August\\{folder.name}\\\t{n}.D\tBlank\n"
             for i, n in enumerate(names, len(other) + 1)]
    (folder / "2026 Aug 27 1345 Sequence Log .TSV").write_text("Starting sequence Thu Aug 27 13:45:37 2026\n"
                                                               + TSV_HEAD + "".join(rows), encoding="utf-8")
    (folder / "2026 Aug 27 1345 Sequence Log .LOG").write_text(
        "Starting sequence\n" + ("    Sequence completed Fri Aug 28 01:35:16 2026\n" if completed else ""),
        encoding="utf-8")


def _run(folder: Path, name: str, finished=True, age=600):
    d = folder / f"{name}.D"
    d.mkdir(parents=True, exist_ok=True)
    (d / "data.ms").write_bytes(b"x" * 100)
    if finished:
        (d / "checksum.xml").write_text("<x/>")
    t = time.time() - age
    for p in d.iterdir():
        os.utime(p, (t, t))
    return d


def _present(folder, ready_names):
    return {n.casefold(): {"name": n + ".D", "ready": n in ready_names} for n in ready_names}


def test_planner_waits_for_replicate_and_blank(tmp_path):
    from gcws.automation.planner import NOT_PROCESSED, READY, WAITING, plan_batch
    from gcws.io import sequence as SQ
    b = tmp_path / "26016605_GIOSUN1635"
    b.mkdir()
    _write_log(b, BATCH)
    seq = SQ.read_sequence(b)
    plan = plan_batch(_present(b, BATCH[:2]), seq, quiet=False, require="either")
    g = {p.name: p for p in plan.groups}
    s1 = g["26016606_130m_min_GIOSUN1635"]
    assert s1.members == [BATCH[1].casefold(), BATCH[5].casefold()] and s1.state == WAITING
    assert "11_26016606" in s1.reason and "planned" in s1.reason
    assert s1.blanks == {"blank": ["08_etoh", "13_etoh"], "blank_istd": ["06_etoh_istd"]}
    plan = plan_batch(_present(b, BATCH[:6]), seq, quiet=False)
    s1 = next(p for p in plan.groups if p.key == s1.key)
    assert s1.state == WAITING and "13_EtOH" in s1.reason                       # B's blank follows it
    plan = plan_batch(_present(b, BATCH), seq, quiet=False)
    assert [p.state for p in plan.groups] == [READY, READY] and not plan.complete
    _write_log(b, BATCH, completed=True)
    assert plan_batch(_present(b, BATCH), SQ.read_sequence(b), quiet=False).complete
    # no blank planned at all: not processed (it will never come)
    no_blank = [n for n in BATCH if "EtOH" not in n]
    _write_log(b, no_blank)
    plan = plan_batch(_present(b, no_blank), SQ.read_sequence(b), quiet=False, require="blank")
    assert {p.state for p in plan.groups} == {NOT_PROCESSED} and "no Blank" in plan.groups[0].reason
    assert {p.state for p in plan_batch(_present(b, no_blank), SQ.read_sequence(b), quiet=False,
                                        require="none").groups} == {READY}


def test_planner_without_log_and_aborted_sequence(tmp_path):
    from gcws.automation.planner import BASELINE, NOT_PROCESSED, READY, WAITING, plan_batch
    from gcws.io import sequence as SQ
    names = BATCH[:3]
    plan = plan_batch(_present(tmp_path, names), None, quiet=False)
    assert plan.groups[0].state == WAITING and "quiet" in plan.groups[0].reason
    plan = plan_batch(_present(tmp_path, names), None, quiet=True)
    assert plan.groups[0].state == READY and plan.groups[0].members == [BATCH[1].casefold()]
    # sequence ended early: B never came, the single determination is processed
    b = tmp_path / "26016605_GIOSUN1635"
    b.mkdir()
    _write_log(b, BATCH, completed=True)
    plan = plan_batch(_present(b, BATCH[:4]), SQ.read_sequence(b), quiet=False)
    assert [(p.members, p.state) for p in plan.groups] == [([BATCH[1].casefold()], READY),
                                                           ([BATCH[3].casefold()], READY)]
    assert plan.groups[1].blanks["blank"] == ["08_etoh"]  # 10_EtOH never came: the one before 09
    # baseline runs are not processed again
    plan = plan_batch(_present(b, BATCH), SQ.read_sequence(b), quiet=False,
                      baseline={n.casefold() for n in BATCH})
    assert {p.state for p in plan.groups} == {BASELINE}
    # nothing but samples: not processed
    plan = plan_batch(_present(tmp_path, [BATCH[1]]), None, quiet=True, require="blank")
    assert plan.groups[0].state == NOT_PROCESSED


def test_scanner_readiness(tmp_path):
    from gcws.automation import scanner as SC
    root = tmp_path / "watch"
    b = root / "26016605_GIOSUN1635"
    _run(b, BATCH[0])
    _run(b, BATCH[1], finished=False, age=0)
    (root / "empty").mkdir()
    assert SC.batch_folders(root, depth=1) == [b]
    assert SC.batch_folders(b, depth=0) == [b]
    assert SC.batch_folders(root, depth=1, pattern="9*") == []
    obs = {o.stem: o for o in SC.observe(b)}
    assert obs["06_etoh_istd"].marker and not obs["07_26016606_130m_min_giosun1635_a"].marker
    cfg = SC.Readiness(stable_scans=2, min_age_s=120, quiet_s=1800)
    now = time.time()
    o = obs["06_etoh_istd"]
    state, n = SC.readiness(None, o, now, cfg, successor_started=True, seq_finished=False, folder_quiet=False)
    assert (state, n) == ("acquiring", 1)                                  # seen once
    state, n = SC.readiness({"fingerprint": o.fingerprint, "stable_count": n}, o, now, cfg,
                            successor_started=True, seq_finished=False, folder_quiet=False)
    assert (state, n) == ("ready", 2)
    young = obs["07_26016606_130m_min_giosun1635_a"]
    state, _ = SC.readiness({"fingerprint": young.fingerprint, "stable_count": 5}, young, now, cfg,
                            successor_started=True, seq_finished=False, folder_quiet=False)
    assert state == "acquiring"                                            # too recent
    (b / f"{BATCH[1]}.D" / "data.ms").write_bytes(b"y" * 200)
    assert SC.observe(b)[1].fingerprint != young.fingerprint


def test_journal_states_and_review(tmp_path):
    from gcws.automation import journal as J
    jr = J.Journal(tmp_path / "j.sqlite")
    other = J.Journal(tmp_path / "j.sqlite")                               # the GUI's connection
    b = jr.batch("wf1", tmp_path / "B1")
    assert jr.batch("wf1", tmp_path / "B1")["id"] == b["id"]
    job = jr.ensure_job("wf1", "m1", b["id"], "k1", "Sample 1", ["07"], {}, "fp1", reason="waiting for B")
    assert job.state == J.WAITING and other.job(job.id).group_name == "Sample 1"
    assert jr.transition(job.id, J.WAITING, J.QUEUED, queued_at=time.time())
    assert not jr.transition(job.id, J.WAITING, J.PROCESSING)             # compare-and-set
    assert not jr.transition(job.id, J.QUEUED, J.ACCEPTED_MANUAL)         # not an allowed step
    assert jr.next_queued().id == job.id
    assert jr.transition(job.id, J.QUEUED, J.PROCESSING, started=time.time())
    assert jr.transition(job.id, J.PROCESSING, J.CONTROL, findings=[{"rule": "sml_exceeded"}])
    assert jr.job(job.id).findings == [{"rule": "sml_exceeded"}]
    assert other.review(job.id, True, "checked", user="analyst")
    j = jr.job(job.id)
    assert (j.state, j.reviewer, j.comment, j.export_pending) == (J.ACCEPTED_MANUAL, "analyst", "checked", 1)
    assert jr.counts() == {J.ACCEPTED_MANUAL: 1}
    # the raw data changed: new revision, review reset
    j = jr.ensure_job("wf1", "m1", b["id"], "k1", "Sample 1", ["07", "11"], {}, "fp2", state=J.QUEUED)
    assert (j.revision, j.state, j.reviewer) == (2, J.QUEUED, None)
    assert not jr.request(j.id)                                            # already queued
    jr.add_export(j.id, 2, "f1", "r1", "docx", "a", "b")
    assert jr.exported(j.id, 2) == {("f1", "r1", "docx")}
    assert any("revision 2" in e["text"] for e in other.events(job_id=j.id))
    jr.heartbeat("running", "j1")
    assert other.watcher_status()["state"] == "running"
    jr.close()
    other.close()


def test_simulated_acquisition_is_seen_run_by_run(samples, tmp_path):
    """The simulator writes a copy the way the instrument does; the source is only read."""
    from gcws.automation import scanner as SC
    from gcws.automation import simulate
    from gcws.io import sequence as SQ
    before = {p: p.stat().st_mtime_ns for p in samples.rglob("*") if p.is_file()}
    steps = simulate.steps(samples, tmp_path, chunks=2, skip=("09_", "10_", "12_"))
    assert next(steps) == "sequence log"
    batch = tmp_path / samples.name
    seq = SQ.read_sequence(batch)
    assert len(seq.stems) == 8 and not seq.finished
    seen = []
    for what in steps:
        seen.append(what)
        if what.endswith("part 1"):
            obs = {o.stem: o for o in SC.observe(batch)}
            run = what.split(" ")[0]
            assert not obs[run[:-2].casefold()].marker          # checksum.xml comes last
    assert seen[-1] == "sequence completed" and SQ.read_sequence(batch).completed
    assert sorted(p.name for p in batch.glob("*.D")) == [p.name for p in sorted(samples.glob("*.D"))
                                                         if not p.name.startswith(("09_", "10_", "12_"))]
    assert all(o.marker for o in SC.observe(batch))
    assert {p: p.stat().st_mtime_ns for p in samples.rglob("*") if p.is_file()} == before
