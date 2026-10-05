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
    # delivered meanwhile: no undo any more
    assert jr.review(job.id, True, user="analyst")
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


def test_batch_closed_when_everything_is_accepted_and_delivered(tmp_path):
    J, jr, b = _journal(tmp_path)
    a = _done(J, jr, b, "A", J.ACCEPTED_AUTO, export_state="done")
    c = _done(J, jr, b, "C", J.CONTROL)
    jobs = lambda: jr.jobs(batch_id=b["id"])                       # noqa: E731
    batch = lambda: jr.batch_by_id(b["id"])                        # noqa: E731
    assert not J.batch_closed(batch(), jobs())                     # C needs control
    assert jr.review(c.id, True)
    assert not J.batch_closed(batch(), jobs())                     # accepted, not yet delivered
    jr.update_job(c.id, export_pending=0, export_state="done")
    assert J.batch_closed(batch(), jobs())
    jr.update_batch(b["id"], plan={"complete": False})
    assert not J.batch_closed(batch(), jobs())                     # more samples to come
    jr.update_batch(b["id"], plan={"complete": True})
    assert jr.reopen_batch(b["id"], user="analyst")
    assert not J.batch_closed(batch(), jobs())                     # reopened: back in To do
    time.sleep(0.01)
    assert jr.review(c.id, False) and jr.review(c.id, True)       # a rejected report can be accepted
    jr.update_job(c.id, export_pending=0)
    assert J.batch_closed(batch(), jobs())                         # decided again after reopening
    _done(J, jr, b, "F", J.FAILED)
    assert not J.batch_closed(batch(), jobs())
    f = [j for j in jobs() if j.group_name == "F"][0]
    jr.delete([f.id])
    assert J.batch_closed(batch(), jobs())                         # a deleted report does not count
    assert not J.batch_closed(batch(), [])


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
