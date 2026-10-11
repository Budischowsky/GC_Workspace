"""Report² rework, the journal side: delete is hide-only (restorable, the watcher does not bring a
deleted sample or batch back), an accept or reject can be undone until the report is delivered
(the watcher waits a few seconds), batches are archived when everything is accepted and
delivered, the reject reasons and relative times."""
import sqlite3
import time

from test_watcher import BATCH, FakeLauncher, _acquire, _log, _result, env, qapp  # noqa: F401  (fixtures)


def _journal(tmp_path):
    from gcws.automation import journal as J
    jr = J.Journal(tmp_path / "journal.sqlite")
    b = jr.batch("wf", tmp_path / "26016605_TEST")
    return J, jr, b


def _done(J, jr, b, name, state, **fields):
    j = jr.ensure_job("wf", "m", b["id"], name, name, [name + ".D"], {}, "fp", state=J.QUEUED)
    jr.transition(j.id, J.QUEUED, J.PROCESSING)
    jr.transition(j.id, J.PROCESSING, state, finished=time.time(), **fields)
    return jr.job(j.id)


def test_old_journal_gains_delete_and_archive_columns(tmp_path):
    from gcws.automation import journal as J
    path = tmp_path / "old.sqlite"
    ddl = J._DDL.replace(" deleted INTEGER DEFAULT 0, reopened REAL DEFAULT 0,", "") \
                .replace(" deleted INTEGER DEFAULT 0,\n    deliver_after REAL DEFAULT 0,", "")
    assert "deleted" not in ddl and "deliver_after" not in ddl
    con = sqlite3.connect(path)
    con.executescript(ddl)
    con.close()
    jr = J.Journal(path)
    jobs = {r["name"] for r in jr.con.execute("PRAGMA table_info(jobs)")}
    batches = {r["name"] for r in jr.con.execute("PRAGMA table_info(batches)")}
    assert {"deleted", "deliver_after"} <= jobs and {"deleted", "reopened"} <= batches
    jr.close()


def test_delete_hides_and_restore_shows_again(tmp_path):
    J, jr, b = _journal(tmp_path)
    waiting = jr.ensure_job("wf", "m", b["id"], "w", "S-wait", ["w.D"], {}, "fp")
    done = _done(J, jr, b, "S-done", J.CONTROL)
    assert sorted(jr.delete([waiting.id, done.id], user="analyst")) == sorted([waiting.id, done.id])
    assert jr.job(waiting.id).state == J.REMOVED and jr.job(waiting.id).deleted   # the watcher skips it
    assert jr.job(done.id).state == J.CONTROL and jr.job(done.id).deleted         # the decision stays
    # the raw data changed: a deleted sample is not processed again behind the analyst's back
    again = jr.ensure_job("wf", "m", b["id"], "S-done", "S-done", ["S-done.D"], {}, "other fp")
    assert again.state == J.CONTROL and again.revision == 1
    assert jr.delete([done.id]) == []                                              # already deleted
    assert jr.restore([done.id], user="analyst") == [done.id] and not jr.job(done.id).deleted
    texts = " ".join(e["text"] for e in jr.events(job_id=done.id))
    assert "deleted in Report² by analyst" in texts and "restored in Report² by analyst" in texts


def test_restoring_a_batch_keeps_reports_deleted_before_hidden(tmp_path):
    """A report deleted on its own stays deleted when its batch is deleted and restored (or the batch
    delete is undone); the reports hidden with the batch come back."""
    J, jr, b = _journal(tmp_path)
    a, c, d = (_done(J, jr, b, n, J.CONTROL) for n in ("A", "C", "D"))
    assert jr.delete([a.id]) == [a.id]
    assert jr.delete_batch(b["id"]) and all(j.deleted for j in jr.jobs())
    assert jr.restore_batch(b["id"])
    assert jr.job(a.id).deleted and not jr.job(c.id).deleted and not jr.job(d.id).deleted
    # a batch deleted before reports remembered how they were hidden: all of it comes back
    jr.delete_batch(b["id"])
    jr.con.execute("UPDATE jobs SET deleted=1")
    jr.update_batch(b["id"], deleted=1)
    assert jr.restore_batch(b["id"]) and not any(j.deleted for j in jr.jobs())


def test_restoring_a_batch_whose_reports_were_all_deleted_before(tmp_path):
    """Every report deleted on its own, then the batch: restoring the batch shows the batch only (the
    reports were not hidden with it); before, all of them came back."""
    J, jr, b = _journal(tmp_path)
    a, c = (_done(J, jr, b, n, J.CONTROL) for n in ("A", "C"))
    assert sorted(jr.delete([a.id, c.id])) == sorted([a.id, c.id])
    assert jr.delete_batch(b["id"]) and jr.restore_batch(b["id"])
    assert not jr.batch_by_id(b["id"])["deleted"]
    assert jr.job(a.id).deleted and jr.job(c.id).deleted


def test_deleted_batch_is_not_scanned_and_comes_back_when_restored(env, qapp):
    from gcws.automation import journal as J
    from gcws.automation.watcher import WatcherCore
    jr, launcher = env["journal"], FakeLauncher()
    now = [time.time()]
    core = WatcherCore(jr, launcher, clock=lambda: now[0])
    core.tick()
    batch = env["watch"] / "26016605_TEST"
    batch.mkdir()
    _log(batch, BATCH)
    for n in BATCH[:3]:
        _acquire(batch, n)
    now[0] += 61
    core.tick()
    b = jr.batch(env["wf"].id, batch)
    before = {j.id for j in jr.jobs()}
    assert before
    assert jr.delete_batch(b["id"], user="analyst") and not jr.delete_batch(b["id"])
    assert all(j.deleted for j in jr.jobs())
    for n in BATCH[3:]:
        _acquire(batch, n)
    now[0] += 61
    core.tick()
    assert {j.id for j in jr.jobs()} == before            # new runs of a deleted batch are not looked at
    assert BATCH[3].lower() not in jr.runs(b["id"])
    assert jr.restore_batch(b["id"]) and not any(j.deleted for j in jr.jobs())
    now[0] += 61
    core.tick()
    assert BATCH[3].lower() in jr.runs(b["id"])                 # watched again


def test_undo_review_until_delivered(tmp_path):
    J, jr, b = _journal(tmp_path)
    job = _done(J, jr, b, "S", J.CONTROL)
    before = {k: job.row.get(k) for k in J.Journal.REVIEW_FIELDS} | {"revision": job.revision}
    assert jr.review(job.id, True, user="analyst", grace=J.UNDO_GRACE)
    accepted = jr.job(job.id)
    assert accepted.state == J.ACCEPTED_MANUAL and accepted.comment == "" and accepted.export_pending
    assert accepted.deliver_after > time.time() + 5
    assert jr.undo_review(job.id, before, user="analyst")
    back = jr.job(job.id)
    assert back.state == J.CONTROL and back.reviewer is None and not back.export_pending
    assert "accept undone by analyst" in " ".join(e["text"] for e in jr.events(job_id=job.id))
    # reject, then undo
    assert jr.review(job.id, False, "Bad chromatography", user="analyst")
    assert jr.undo_review(job.id, before) and jr.job(job.id).state == J.CONTROL
    # the grace time is over: the watcher may be delivering, no undo any more
    assert jr.review(job.id, True, user="analyst")
    assert not jr.undo_review(job.id, before) and jr.job(job.id).state == J.ACCEPTED_MANUAL
    # delivered meanwhile: no undo any more
    jr.add_export(job.id, job.revision, "f", "r", "docx", "a", "b")
    assert not jr.undo_review(job.id, before) and jr.job(job.id).state == J.ACCEPTED_MANUAL
    # processed again meanwhile: the old snapshot no longer applies
    job2 = _done(J, jr, b, "S2", J.CONTROL)
    snap = {k: job2.row.get(k) for k in J.Journal.REVIEW_FIELDS} | {"revision": job2.revision}
    assert jr.review(job2.id, True) and jr.request(job2.id)
    assert not jr.undo_review(job2.id, snap) and jr.job(job2.id).deliver_after == 0


def test_watcher_waits_for_the_undo_grace_before_delivering(env, qapp, monkeypatch):
    from gcws.automation import export, journal as J
    from gcws.automation.watcher import WatcherCore
    jr = env["journal"]
    b = jr.batch(env["wf"].id, env["watch"] / "26016605_TEST")
    j = jr.ensure_job(env["wf"].id, "m", b["id"], "S", "S", ["S.D"], {}, "fp", state=J.QUEUED)
    jr.transition(j.id, J.QUEUED, J.PROCESSING)
    jr.transition(j.id, J.PROCESSING, J.CONTROL)
    delivered = []
    monkeypatch.setattr(export, "deliver", lambda journal, wf, job: delivered.append(job.id) or
                        journal.update_job(job.id, export_pending=0))
    now = [time.time()]
    core = WatcherCore(jr, FakeLauncher(), clock=lambda: now[0])
    assert jr.review(j.id, True, grace=J.UNDO_GRACE)
    core.deliver_pending()
    assert delivered == []
    now[0] += J.UNDO_GRACE + 1
    core.deliver_pending()
    assert delivered == [j.id]


def test_relative_times():
    from datetime import datetime, timedelta
    from gcws.automation.journal import ago
    now = datetime(2026, 10, 5, 15, 0).timestamp()
    assert ago(None) == "" and ago(now - 10, now) == "just now"
    assert ago(now - 300, now) == "5 min ago" and ago(now - 2 * 3600, now) == "2 h ago"
    assert ago((datetime(2026, 10, 4, 23, 0)).timestamp(), now) == "yesterday"
    assert ago((datetime(2026, 10, 1, 9, 0)).timestamp(), now) == "4 days ago"
    assert ago((datetime(2026, 9, 1, 9, 0)).timestamp(), now) == "2026-09-01"
    assert ago(now + timedelta(minutes=1).total_seconds(), now) == "just now"


def test_reject_reasons_default_and_saved(tmp_path, monkeypatch):
    from gcws import paths
    from gcws.automation import rules as RU
    monkeypatch.setattr(paths, "DATA", tmp_path)
    assert RU.load_reject_reasons() == RU.DEFAULT_REJECT_REASONS
    RU.save_reject_reasons([" Too dilute ", "", "Bad chromatography"])
    assert RU.load_reject_reasons() == ["Too dilute", "Bad chromatography"]


def test_pairs_accepted_by_hand(tmp_path):
    """A pair no workflow processed, accepted in Replicates: listed accepted under the batch of its folder
    (the workflow's batch when there is one), never waiting for delivery; accepted again it gets the next
    revision of the same entry."""
    from gcws.automation import manual as MA
    from gcws.automation import review as R
    from gcws.automation.workflow import new_id
    J, jr, b = _journal(tmp_path)
    folder = tmp_path / "26016605_TEST"
    assert MA.batch_for(jr, folder)["id"] == b["id"]               # the workflow's batch of that folder
    other = MA.batch_for(jr, tmp_path / "elsewhere")
    assert other["workflow_id"] == MA.MANUAL and other["id"] != b["id"]
    wf = _done(J, jr, b, "Pair", J.CONTROL)
    assert MA.clash(jr, b["id"], "pair").id == wf.id and MA.clash(jr, b["id"], "Other") is None
    assert MA.free_name(jr, b["id"], "Pair") == "Pair (2)"

    def listed(name, replace=None, jid=None):
        return MA.record(jr, job_id=jid or new_id("j"), batch_id=b["id"], name=name, members=["A.D", "B.D"],
                         files={MA.MANUAL: {"xlsx": "r.xlsx"}}, project_path=tmp_path / "p.gcws",
                         job_dir=tmp_path / "job", evidence={"members": []}, findings=[], summary={},
                         reviewer="analyst", replace=replace)
    job = listed("Pair (2)")
    assert job.state == J.ACCEPTED_MANUAL and job.reviewer == "analyst" and not job.export_pending
    assert job.revision == 1 and job.members == ["A.D", "B.D"] and MA.is_manual(job)
    assert MA.clash(jr, b["id"], "pair (2)").id == job.id and MA.free_name(jr, b["id"], "Pair") == "Pair (3)"
    again = listed("Pair (2)", replace=job.id)
    assert again.id == job.id and again.revision == 2 and again.state == J.ACCEPTED_MANUAL
    # rejected in Report², then accepted there again: still nothing to deliver
    assert jr.review(job.id, False) and jr.review(job.id, True)
    assert jr.job(job.id).state == J.ACCEPTED_MANUAL and not jr.job(job.id).export_pending
    # deleted, then accepted again under that name: the same entry comes back
    jr.delete([job.id])
    assert MA.clash(jr, b["id"], "Pair (2)") is None and MA.entry(jr, b["id"], "Pair (2)").id == job.id
    back = listed("Pair (2)", replace=MA.entry(jr, b["id"], "Pair (2)").id)
    assert back.id == job.id and not back.deleted and back.revision == 3
    assert R.batch_closed(jr.batch_by_id(b["id"]), [back])          # nothing pending: it can be archived


def test_change_check_and_stamp_are_cheap_and_ignore_the_heartbeat(tmp_path):
    """The panels poll every few seconds: changed() is nearly free, stamp() reads an index only and does
    not move with the watcher's heartbeat; decoded JSON is kept with the job."""
    J, jr, b = _journal(tmp_path)
    other = J.Journal(jr.path)                              # another program (the watcher)
    assert jr.changed("a") and not jr.changed("a")
    assert jr.changed("b")                                  # every caller on its own
    stamp = jr.stamp()
    other.heartbeat("running")
    assert jr.changed("a") and jr.stamp() == stamp          # written, but nothing a panel shows
    job = _done(J, jr, b, "S1", J.CONTROL, findings=[{"rule": "x", "text": "t"}])
    assert jr.changed("a") and jr.stamp() != stamp
    stamp = jr.stamp()
    jr.update_job(job.id, edited=1)                         # saved changes: Accept now makes it again
    assert jr.stamp() != stamp
    # a state of the same length (removed -> waiting: a run put in again) changes the stamp as well
    w = jr.ensure_job("wf", "m", b["id"], "W", "W", ["W.D"], {}, "fp")
    assert jr.remove([w.id])
    stamp = jr.stamp()
    assert jr.put_in_again([w.id]) == [w.id] and jr.job(w.id).state == J.WAITING
    assert jr.stamp() != stamp
    stamp = jr.stamp()
    plan = jr.con.execute("EXPLAIN QUERY PLAN SELECT COUNT(*), SUM(edited), SUM(revision) FROM jobs").fetchall()
    assert "COVERING INDEX jobs_stamp" in " ".join(str(tuple(r)) for r in plan)
    job = jr.job(job.id)
    assert job.findings is job.findings                     # decoded once
    other.close()
    jr.close()


def test_history_of_a_report_shows_its_newest_events(tmp_path):
    """A report with a long history: the history window shows the latest events, not the first ones."""
    J, jr, b = _journal(tmp_path)
    job = _done(J, jr, b, "S1", J.CONTROL)
    for i in range(30):
        jr.event("info", f"event {i}", job_id=job.id)
    texts = [e["text"] for e in jr.events(job_id=job.id, limit=10)]
    assert texts == [f"event {i}" for i in range(20, 30)]                  # the newest, oldest first
    jr.close()


def test_stamp_moves_when_only_the_reason_of_a_waiting_sample_changes(tmp_path):
    """The watcher updates why a sample waits (Report² shows it): the panel sees it at its next poll."""
    J, jr, b = _journal(tmp_path)
    w = jr.ensure_job("wf", "m", b["id"], "W", "W", ["W.D"], {}, "fp", reason="waiting for 2 runs")
    stamp = jr.stamp()
    jr.update_job(w.id, reason="waiting for 1 run")
    assert jr.stamp() != stamp
    stamp = jr.stamp()
    jr.ensure_job("wf", "m", b["id"], "W", "W", ["W.D"], {}, "fp", reason="waiting for 9 run")   # same length
    assert jr.job(w.id).reason == "waiting for 9 run" and jr.stamp() != stamp
    jr.close()



# -- a new revision starts clean ---------------------------------------------------------------------------

def _reviewed_and_delivered(J, jr, b, name="S"):
    """A report made again from the edited project, accepted, delivered and with an override: every field
    a new revision has to clear is set."""
    j = _done(J, jr, b, name, J.CONTROL, project_path="p.gcws", findings=[{"level": "control", "rule": "x"}])
    assert jr.mark_edited(j.id, j.revision)
    assert jr.update_edited(j.id, "changed")
    jr.transition(j.id, J.QUEUED, J.PROCESSING)
    jr.transition(j.id, J.PROCESSING, J.CONTROL, finished=time.time(), edited=0)
    jr.update_job(j.id, review_pending=None, override={"allow_no_blank": True})
    assert jr.review(j.id, False, "bad") and jr.review(j.id, True, grace=60)
    jr.update_job(j.id, export_state="done", comment="ok", edited=1)
    j = jr.job(j.id)
    assert j.mode == "rereport" and j.export_state == "done" and j.deliver_after and j.reviewer
    return j


def _assert_clean(J, jr, before, mode="full"):
    j = jr.job(before.id)
    assert j.revision == before.revision + 1
    assert j.mode == mode
    assert (j.reviewer, j.comment, j.reviewed_at) == (None, None, None)
    assert not j.export_pending and j.export_state == "none" and not j.deliver_after
    assert not j.edited and not j.attempts and not j.not_before
    return j


def test_raw_data_changed_starts_a_clean_full_revision(tmp_path):
    J, jr, b = _journal(tmp_path)
    before = _reviewed_and_delivered(J, jr, b)
    jr.ensure_job("wf", "m", b["id"], "S", "S", ["S.D"], {}, "fp2")
    j = _assert_clean(J, jr, before)
    assert j.state == J.WAITING and j.review_pending is None
    assert not j.override                          # the new raw data is processed by the method as it is


def test_process_again_starts_a_clean_revision(tmp_path):
    J, jr, b = _journal(tmp_path)
    before = _reviewed_and_delivered(J, jr, b)
    assert jr.review(before.id, False, "bad")
    before = jr.job(before.id)
    assert jr.request(before.id, "full")
    j = _assert_clean(J, jr, before)
    assert j.state == J.QUEUED and j.override == {"allow_no_blank": True}   # kept unless a new one is given
    assert jr.review(j.id, True) is False                                    # queued: nothing to accept


def test_update_report_starts_a_clean_rereport_revision(tmp_path):
    J, jr, b = _journal(tmp_path)
    before = _reviewed_and_delivered(J, jr, b)
    assert jr.set_control(before.id)
    assert jr.mark_edited(before.id, before.revision)
    before = jr.job(before.id)
    assert jr.update_edited(before.id, "again")
    j = jr.job(before.id)
    assert j.revision == before.revision + 1 and j.mode == "rereport" and j.export_state == "none"
    assert not j.deliver_after and not j.edited and j.review_pending["comment"] == "again"


def test_batch_put_back_starts_a_clean_full_revision(tmp_path):
    J, jr, b = _journal(tmp_path)
    before = _reviewed_and_delivered(J, jr, b)
    jr.delete([before.id])
    assert jr.reset_batch(b["id"]) == [before.id]
    j = _assert_clean(J, jr, before)
    assert j.state == J.WAITING and not j.deleted and not j.override and j.input_fp == ""


def test_review_fields_are_the_ones_a_new_revision_clears(tmp_path):
    from gcws.automation import journal as J
    cleared = set(J.new_revision())
    assert set(J.Journal.REVIEW_FIELDS) - {"state"} <= cleared
