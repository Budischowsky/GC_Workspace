"""The Report² entry module (gcws.automation.review), without Qt: status, allowed actions, review and
delete with undo, delivery once the undo time is over, and which batches the views hide."""
import time

from gcws.automation import review as R


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


# -- status ------------------------------------------------------------------------------------------------

def test_done_means_accepted_and_delivered(tmp_path):
    from gcws.automation import journal as J
    from gcws.automation.review import is_done
    jr = J.Journal(tmp_path / "j.sqlite")
    b = jr.batch("wf", tmp_path)
    job = jr.ensure_job("wf", "m", b["id"], "s", "S", ["S.D"], {}, "fp", state=J.QUEUED)
    jr.transition(job.id, J.QUEUED, J.PROCESSING)
    jr.transition(job.id, J.PROCESSING, J.ACCEPTED_AUTO, export_pending=1)
    assert not is_done(jr.job(job.id))                         # accepted, not delivered yet
    jr.update_job(job.id, export_pending=0, export_state="done")
    assert is_done(jr.job(job.id))
    jr.review(job.id, False, "bad")
    assert not is_done(jr.job(job.id))                         # rejected: not done
    manual = jr.record_manual("jm", workflow_id=J.MANUAL_WORKFLOW, method_node="manual", batch_id=b["id"],
                              group_key="manual:p", group_name="P", members=[], files={}, project_path="", job_dir="",
                              evidence={}, findings=[], summary={}, reviewer="a")
    assert is_done(manual)                                     # no workflow delivers it: accepted is done


def test_batch_closed_when_everything_is_accepted_and_delivered(tmp_path):
    J, jr, b = _journal(tmp_path)
    a = _done(J, jr, b, "A", J.ACCEPTED_AUTO, export_state="done")
    c = _done(J, jr, b, "C", J.CONTROL)
    jobs = lambda: jr.jobs(batch_id=b["id"])                       # noqa: E731
    batch = lambda: jr.batch_by_id(b["id"])                        # noqa: E731
    assert not R.batch_closed(batch(), jobs())                     # C needs control
    assert jr.review(c.id, True)
    assert not R.batch_closed(batch(), jobs())                     # accepted, not yet delivered
    jr.update_job(c.id, export_pending=0, export_state="done")
    assert R.batch_closed(batch(), jobs())
    jr.update_batch(b["id"], plan={"complete": False})
    assert not R.batch_closed(batch(), jobs())                     # more samples to come
    jr.update_batch(b["id"], plan={"complete": True})
    assert jr.reopen_batch(b["id"], user="analyst")
    assert not R.batch_closed(batch(), jobs())                     # reopened: back in To do
    time.sleep(0.01)
    assert jr.review(c.id, False) and jr.review(c.id, True)       # a rejected report can be accepted
    jr.update_job(c.id, export_pending=0)
    assert R.batch_closed(batch(), jobs())                         # decided again after reopening
    _done(J, jr, b, "F", J.FAILED)
    assert not R.batch_closed(batch(), jobs())
    f = [j for j in jobs() if j.group_name == "F"][0]
    jr.delete([f.id])
    assert R.batch_closed(batch(), jobs())                         # a deleted report does not count
    assert not R.batch_closed(batch(), [])


def test_a_partly_delivered_report_is_not_done_and_keeps_its_batch_open(tmp_path):
    """The register and the archive agree: a report delivered only partly is not done, so its batch stays
    in To do."""
    J, jr, b = _journal(tmp_path)
    a = _done(J, jr, b, "A", J.ACCEPTED_AUTO, export_state="partial")
    batch = jr.batch_by_id(b["id"])
    assert not R.is_done(a) and not R.batch_closed(batch, [a])
    jr.update_job(a.id, export_state="done")
    assert R.is_done(jr.job(a.id)) and R.batch_closed(batch, [jr.job(a.id)])


def test_status_of_a_report(tmp_path):
    J, jr, b = _journal(tmp_path)
    c = _done(J, jr, b, "C", J.CONTROL)
    a = _done(J, jr, b, "A", J.ACCEPTED_AUTO, export_pending=1)
    f = _done(J, jr, b, "F", J.FAILED)
    assert (R.bucket(c), R.level(c)) == ("control", "warn")
    assert (R.bucket(a), R.level(a), R.delivered(a)) == ("accepted", "ok", ("pending", "info"))
    assert (R.bucket(f), R.level(f), R.delivered(f)) == ("failed", "bad", ("", "neutral"))
    assert R.worst([R.level(a), R.level(c)]) == "warn" and R.worst([]) == "neutral"


# -- allowed actions -------------------------------------------------------------------------------------

def test_allowed_actions_and_the_journal_refuses_the_same(tmp_path):
    J, jr, b = _journal(tmp_path)
    c = _done(J, jr, b, "C", J.CONTROL)
    a = _done(J, jr, b, "A", J.ACCEPTED_AUTO)
    q = jr.ensure_job("wf", "m", b["id"], "Q", "Q", ["Q.D"], {}, "fp", state=J.QUEUED)
    assert R.allowed(c, "accept") and R.allowed(c, "reject") and not R.allowed(c, "set_control")
    assert not R.allowed(a, "accept") and R.allowed(a, "reject") and R.allowed(a, "set_control")
    assert not R.allowed(q, "reprocess") and R.allowed(c, "reprocess")
    # an accepted report is not accepted again: the journal says no as the panel does
    assert not jr.review(a.id, True) and jr.job(a.id).state == J.ACCEPTED_AUTO
    jr.delete([c.id])
    c = jr.job(c.id)
    assert not R.allowed(c, "accept") and not jr.review(c.id, True)          # deleted: nothing to decide
    jr.restore([c.id])
    assert jr.review(c.id, False) and R.allowed(jr.job(c.id), "accept")      # rejected: may be accepted
    jr.update_job(c.id, review_pending={"user": "a"})
    assert not any(R.allowed(jr.job(c.id), act) for act in R.ACTIONS)        # being made again
    import pytest
    with pytest.raises(ValueError):
        R.allowed(a, "fly")


# -- decisions and deletes, with undo ----------------------------------------------------------------------

def test_accept_and_undo_without_qt(tmp_path):
    J, jr, b = _journal(tmp_path)
    c = _done(J, jr, b, "C", J.CONTROL)
    a = _done(J, jr, b, "A", J.ACCEPTED_AUTO)
    rv = R.Review(jr)
    out = rv.decide([c.id, a.id], True, "fine")                    # A is accepted already: left as it is
    assert [j.id for j in out.decided] == [c.id] and not out.regenerated
    assert jr.job(c.id).state == J.ACCEPTED_MANUAL and jr.job(c.id).export_pending
    assert out.undo.kind == "review" and out.undo.until == jr.job(c.id).deliver_after > time.time()
    done = rv.undo(out.undo)
    assert done.ok and done.ids == [c.id] and jr.job(c.id).state == J.CONTROL and not jr.job(c.id).export_pending
    assert not rv.decide([a.id], True).decided and rv.decide([a.id], True).undo is None


def test_an_accept_delivered_meanwhile_is_not_undone(tmp_path):
    J, jr, b = _journal(tmp_path)
    c = _done(J, jr, b, "C", J.CONTROL)
    rv = R.Review(jr, grace=0)
    out = rv.decide([c.id], True)
    assert out.undo.until == 0                                      # no undo time: the watcher may deliver now
    done = rv.undo(out.undo)
    assert not done.ok and "Not undone" in done.text and jr.job(c.id).state == J.ACCEPTED_MANUAL


def test_an_edited_report_accepted_is_made_again_first(tmp_path):
    J, jr, b = _journal(tmp_path)
    c = _done(J, jr, b, "C", J.CONTROL, project_path=str(tmp_path / "p.gcws"), edited=1)
    out = R.Review(jr).decide([c.id], True)
    assert not out.decided and [j.id for j in out.regenerated] == [c.id] and out.undo is None
    assert jr.job(c.id).state == J.QUEUED and jr.job(c.id).review_pending


def test_prepare_can_skip_a_report(tmp_path):
    J, jr, b = _journal(tmp_path)
    c = _done(J, jr, b, "C", J.CONTROL)
    seen = []
    out = R.Review(jr).decide([c.id], False, "bad", prepare=lambda jid: seen.append(jid) and False)
    assert seen == [c.id] and not out.decided and jr.job(c.id).state == J.CONTROL


def test_undo_of_a_delete_puts_queued_samples_back_through_the_journal(tmp_path):
    """The delete took a waiting sample out of the queue (REMOVED); undo shows it again and puts it back
    as it was - REMOVED is left only this way or by "Process again"."""
    J, jr, b = _journal(tmp_path)
    w = jr.ensure_job("wf", "m", b["id"], "W", "W", ["W.D"], {}, "fp")
    c = _done(J, jr, b, "C", J.CONTROL)
    rv = R.Review(jr)
    token = rv.delete([w.id, c.id])
    assert jr.job(w.id).state == J.REMOVED and jr.job(w.id).deleted and jr.job(c.id).deleted
    assert rv.delete([w.id]) is None                               # already deleted
    done = rv.undo(token)
    assert done.ok and sorted(done.ids) == sorted([w.id, c.id])
    assert (jr.job(w.id).state, jr.job(w.id).reason, jr.job(w.id).deleted) == (w.state, w.reason, 0)
    assert jr.job(c.id).state == J.CONTROL and not jr.job(c.id).deleted
    # the journal's own step: only back from REMOVED, only into a queue state, only when shown
    assert not jr.unremove(c.id, J.WAITING)
    jr.delete([w.id])
    assert not jr.unremove(w.id, J.WAITING) and not jr.unremove(w.id, J.ACCEPTED_MANUAL)


def test_undo_of_a_batch_delete(tmp_path):
    J, jr, b = _journal(tmp_path)
    w = jr.ensure_job("wf", "m", b["id"], "W", "W", ["W.D"], {}, "fp")
    single = _done(J, jr, b, "S", J.CONTROL)
    jr.delete([single.id])                                         # deleted on its own before
    rv = R.Review(jr)
    token = rv.delete_batch(b["id"])
    assert jr.batch_by_id(b["id"])["deleted"] and jr.job(w.id).state == J.REMOVED
    assert rv.delete_batch(b["id"]) is None
    rv.undo(token)
    assert not jr.batch_by_id(b["id"])["deleted"] and jr.job(w.id).state == J.WAITING
    assert jr.job(single.id).deleted                               # stays deleted
