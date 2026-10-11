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
