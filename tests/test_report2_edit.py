"""The Report² edit session (gcws.automation.edit_session), without Qt: a report bound to the open
project, what counts as a report change, saving it back to control, a stale report, and which Report²
report a double determination belongs to."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from gcws.automation import edit_session as ES
from gcws.automation import journal as J
from gcws.core import project as P


def _report(tmp_path, state=J.ACCEPTED_AUTO, members=("A.D", "B.D"), key="pair", project="pair.gcws"):
    """The journal of ``tmp_path`` with one more processed report, and its project file."""
    jr = J.Journal(tmp_path / "journal.sqlite")
    batch = jr.batch("wf", tmp_path)
    job = jr.ensure_job("wf", "method", batch["id"], key, key.title(), list(members), {}, "fp", state=J.QUEUED)
    path = tmp_path / project
    path.write_text("original")
    jr.transition(job.id, J.QUEUED, J.PROCESSING)
    jr.transition(job.id, J.PROCESSING, state, project_path=str(path))
    return jr, jr.job(job.id), path


def _project(monkeypatch, data: dict):
    """P.to_dict gives ``data``; P.save writes "edited" into the file. Returns the list of saved paths."""
    saved = []
    monkeypatch.setattr(P, "to_dict", lambda ws, path: data)
    monkeypatch.setattr(P, "save", lambda ws, path: (saved.append(path), Path(path).write_text("edited"), path)[2])
    return saved


def _session(jr):
    return ES.EditSession(lambda: jr)


# -- what is a report change -------------------------------------------------------------------------------

def test_report_inputs_leave_out_display_state_and_who_accepted():
    a = {"quant": {"limit": 1}, "replicate_groups": [{"id": "g", "signoff": {"by": "me"}}],
         "runs": [{"id": "a", "name": "A", "color": "#111111"}]}
    b = {"quant": {"limit": 1}, "replicate_groups": [{"id": "g", "signoff": {"by": "you"}}],
         "runs": [{"id": "a", "name": "A", "color": "#222222"}]}
    assert ES.report_inputs(a) == ES.report_inputs(b)
    renamed = dict(b, runs=[{"id": "a", "name": "New"}])
    assert ES.report_inputs(a) != ES.report_inputs(renamed)


def test_members_match_by_file_name_whatever_the_case_and_order(tmp_path):
    jr, job, _ = _report(tmp_path, members=("A.D", "b.d"))
    assert ES.member_key(["B.D", "a.d"]) == ES.member_key(job.members)
    assert ES.same_members(job, ["b.D", "a.D"]) and not ES.same_members(job, ["A.D"])
    assert not ES.same_members(None, ["A.D", "B.D"])
    assert ES.runs_of(job, {"r1": "a.d", "r2": "B.D"}) == ["r1", "r2"]
    assert ES.runs_of(job, {"r1": "A.D"}) is None


# -- binding and saving ------------------------------------------------------------------------------------

def test_bind_tells_the_window_and_clear_unbinds(tmp_path):
    jr, job, path = _report(tmp_path)
    told = []
    s = ES.EditSession(lambda: jr, lambda: told.append(s.bound))
    s.bind(job.id, job.revision, str(path), {"quant": {}})
    assert s.bound == (job.id, 1, path) and s.job_id == job.id and s.path == path and s.job().id == job.id
    assert s.is_editing(job.id, path) and not s.is_editing(job.id, tmp_path / "other.gcws")
    s.clear()
    assert s.bound is None and s.job() is None and told == [(job.id, 1, path), None]


def test_a_saved_change_returns_an_accepted_report_to_control(tmp_path, monkeypatch):
    jr, job, path = _report(tmp_path)
    s = _session(jr)
    s.bind(job.id, 1, path, {"quant": {"limit": 1}})
    _project(monkeypatch, {"quant": {"limit": 2}})
    ws = SimpleNamespace(dirty=True, project_path=path)
    assert s.unsaved(ws) and s.changed(ws)
    assert s.save(ws) is True
    assert path.read_text() == "edited" and not ws.dirty and ws.project_path == path
    assert jr.job(job.id).state == J.CONTROL and jr.job(job.id).edited == 1
    assert not s.changed(ws) and not s.unsaved(ws)          # the saved project is the new baseline
    # Accept made a new revision of it: the reviewed project can be left without saving
    assert jr.accept_edited(job.id, user="analyst")
    assert s.save(ws) is False


def test_a_run_rename_is_a_report_change(tmp_path, monkeypatch):
    jr, job, path = _report(tmp_path)
    s = _session(jr)
    s.bind(job.id, 1, path, {"runs": [{"id": "a", "name": "Old"}]})
    _project(monkeypatch, {"runs": [{"id": "a", "name": "New"}]})
    assert s.save(SimpleNamespace(dirty=False, project_path=path))
    assert path.read_text() == "edited" and jr.job(job.id).state == J.CONTROL and jr.job(job.id).edited == 1


def test_display_changes_are_saved_without_sending_the_report_back(tmp_path, monkeypatch):
    jr, job, path = _report(tmp_path)
    s = _session(jr)
    s.bind(job.id, 1, path, {"runs": [{"id": "a", "color": "#111111"}]})
    _project(monkeypatch, {"runs": [{"id": "a", "color": "#222222"}]})
    ws = SimpleNamespace(dirty=True, project_path=path)
    assert s.save(ws) is False
    assert path.read_text() == "edited" and jr.job(job.id).state == J.ACCEPTED_AUTO and not jr.job(job.id).edited


def test_a_rejected_report_stays_rejected_but_edited_and_can_be_queued(tmp_path, monkeypatch):
    jr, job, path = _report(tmp_path, state=J.CONTROL)
    assert jr.review(job.id, False, "bad data", user="reviewer")
    s = _session(jr)
    s.bind(job.id, 1, path, {"quant": {"limit": 1}})
    _project(monkeypatch, {"quant": {"limit": 2}})
    assert s.save(SimpleNamespace(dirty=False, project_path=path))
    saved = jr.job(job.id)
    assert path.read_text() == "edited"
    assert saved.state == J.REJECTED and saved.edited == 1
    assert saved.reviewer == "reviewer" and saved.comment == "bad data"
    assert jr.accept_edited(job.id, user="analyst")
    queued = jr.job(job.id)
    assert queued.state == J.QUEUED and queued.review_pending["user"] == "analyst"


def test_a_stale_report_is_never_written_over(tmp_path, monkeypatch):
    jr, job, path = _report(tmp_path, state=J.CONTROL)
    s = _session(jr)
    s.bind(job.id, 1, path, {"runs": [{"id": "a", "color": "#111111"}]})
    jr.update_job(job.id, revision=2, project_path=str(path))
    saved = _project(monkeypatch, {"runs": [{"id": "a", "color": "#222222"}]})
    with pytest.raises(ES.Stale):
        s.save(SimpleNamespace(dirty=True, project_path=path))
    assert saved == [] and path.read_text() == "original"
    assert s.save(SimpleNamespace(dirty=True, project_path=path), job_id="another") is False   # not this one


def test_an_entry_accepted_by_hand_is_not_marked_edited(tmp_path, monkeypatch):
    from gcws.automation import manual as MA
    jr, job, path = _report(tmp_path)
    jr.update_job(job.id, workflow_id=MA.MANUAL)
    s = _session(jr)
    s.bind(job.id, 1, path, {"quant": {"limit": 1}})
    _project(monkeypatch, {"quant": {"limit": 2}})
    assert s.save(SimpleNamespace(dirty=False, project_path=path))
    assert path.read_text() == "edited" and not jr.job(job.id).edited


def test_follow_binds_the_new_revision_and_its_project(tmp_path, monkeypatch):
    jr, job, path = _report(tmp_path)
    s = _session(jr)
    s.bind(job.id, 1, path, {"quant": {"limit": 1}})
    newer = tmp_path / "r2" / "pair.gcws"
    newer.parent.mkdir()
    newer.write_text("regenerated")
    jr.update_job(job.id, revision=2, project_path=str(newer))
    _project(monkeypatch, {"quant": {"limit": 3}})
    ws = SimpleNamespace(dirty=False, project_path=path)
    assert s.follow(ws)
    assert s.bound == (job.id, 2, newer) and ws.project_path == newer and not s.changed(ws)
    newer.unlink()
    assert not s.follow(ws) and s.bound == (job.id, 2, newer)


# -- which report ------------------------------------------------------------------------------------------

def test_find_for_the_bound_report_or_the_newest_pair_of_this_project(tmp_path):
    jr, first, path = _report(tmp_path, key="first")
    _, second, _ = _report(tmp_path, key="second")           # the same project file and runs, made later
    _, other, _ = _report(tmp_path, key="other", project="other.gcws")
    s = _session(jr)
    assert s.find_for(path, ["a.d", "B.D"]).id == second.id
    assert s.find_for(path, ["A.D"]) is None and s.find_for(None, ["A.D", "B.D"]) is None
    jr.update_job(second.id, deleted=1)
    assert s.find_for(path, ["A.D", "B.D"]).id == first.id
    s.bind(other.id, 1, tmp_path / "other.gcws", {})
    assert s.find_for(path, ["A.D", "B.D"]).id == other.id    # bound: that report, wherever the project is
    assert s.find_for(path, ["A.D", "C.D"]) is None
