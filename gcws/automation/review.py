"""The Report² entry: what a report's journal row means and what may be done with it, without Qt.

The journal (:mod:`gcws.automation.journal`) stores the rows and their state transitions; this module
answers the questions every view asks of them - which filter chip a report counts for, its colour, whether
it is delivered, whether it is done, whether its batch is closed (Report²'s Archive) - and which actions the
analyst may take on it (:func:`allowed`; the journal's verbs refuse the same). Report², the automation
overview, the Automation panel and the main window all ask here, so they always agree.

:class:`Review` takes the analyst's decisions (accept, reject) and deletes, each with an :class:`Undo`
token: plain data the panel keeps while it offers "Undo", and gives back to :meth:`Review.undo`.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from gcws.automation import journal as J

#: the colour of a journal state ("ok", "warn", "info", "bad", "neutral"; :func:`gcws.ui.theme.status_brush`)
LEVEL = {J.ACCEPTED_AUTO: "ok", J.ACCEPTED_MANUAL: "ok", J.CONTROL: "warn", J.REJECTED: "bad",
         J.FAILED: "bad", J.NOT_PROCESSED: "bad", J.WAITING: "neutral", J.QUEUED: "info", J.PROCESSING: "info",
         J.REMOVED: "neutral"}
#: the filter chips' colours, by :func:`bucket`
BUCKET_LEVEL = {"control": "warn", "accepted": "ok", "waiting": "info", "not_processed": "bad", "failed": "bad",
                "rejected": "bad", "removed": "neutral"}
#: levels from the worst on (:func:`worst`)
WORST = ("bad", "warn", "info", "ok", "neutral")


def level(job: J.Job) -> str:
    """The colour of a report's state."""
    return LEVEL.get(job.state, "neutral")


def worst(levels) -> str:
    levels = set(levels)
    return next((lv for lv in WORST if lv in levels), "neutral")


def bucket(job: J.Job) -> str:
    """The filter chip a report counts for."""
    if job.review_pending or job.state == J.CONTROL:
        return "control"
    if job.state in J.ACCEPTED:
        return "accepted"
    return {J.NOT_PROCESSED: "not_processed", J.FAILED: "failed", J.REJECTED: "rejected",
            J.REMOVED: "removed"}.get(job.state, "waiting")


def delivered(job: J.Job) -> tuple[str, str]:
    """The Delivered column of a report: (text, level)."""
    if job.state not in J.DELIVERABLE:
        return "", "neutral"
    if job.export_pending:
        return "pending", "info"
    return {"done": ("✓", "ok"), "partial": ("partly", "warn"), "error": ("failed", "bad")}.get(
        job.export_state or "", ("", "neutral"))


def is_done(job: J.Job) -> bool:
    """A report is done when it is accepted and delivered (an entry accepted by hand, which no workflow
    delivers, when it is accepted). Everything else is not done yet - waiting, in work, needing control,
    failed, not processed, rejected, accepted but not (or only partly) delivered."""
    if job.state not in J.ACCEPTED or job.review_pending or job.deleted:
        return False
    if job.workflow_id == J.MANUAL_WORKFLOW:
        return True
    return not job.export_pending and job.export_state == "done"


#: the analyst's actions on a report (:func:`allowed`)
ACTIONS = ("accept", "reject", "set_control", "reprocess")


def allowed(job: J.Job, action: str) -> bool:
    """May the analyst take ``action`` on this report? Never on a deleted one, nor while it is being made
    again after an edit.

    - accept: from control needed or rejected (an edited report is made again first, then accepted);
    - reject: from control needed or accepted;
    - set_control (Set status > Control needed): an accepted or rejected sample report is checked again;
    - reprocess (Process or report again): anything not queued or being processed, made by a workflow (an
      entry accepted by hand is accepted again in Replicates instead).
    """
    if job.deleted or job.review_pending:
        return False
    if action == "accept":
        return job.state in J.ACCEPT_FROM
    if action == "reject":
        return job.state in J.REJECT_FROM
    if action == "set_control":
        return not job.is_batch and job.state in J.SET_CONTROL_FROM
    if action == "reprocess":
        return job.state not in (J.QUEUED, J.PROCESSING) and job.workflow_id != J.MANUAL_WORKFLOW
    raise ValueError(f"unknown action {action!r}; one of {ACTIONS}")


def batch_closed(batch: dict, jobs: list) -> bool:
    """True when every report of the batch is done (:func:`is_done`; Report² archives it). Deleted reports
    and those removed from the queue do not count. A batch with more samples to come is open, and a
    reopened batch stays open until one of its reports is decided again."""
    live = [j for j in jobs if not j.deleted and j.state != J.REMOVED]
    if not live or not all(is_done(j) for j in live):
        return False
    try:
        plan = json.loads(batch.get("plan_json") or "null")
    except ValueError:
        plan = None
    if isinstance(plan, dict) and not plan.get("complete", True):
        return False
    reopened = float(batch.get("reopened") or 0)
    return not reopened or max(float(j.reviewed_at or j.finished or 0) for j in live) > reopened


# -- decisions and deletes, with undo -------------------------------------------------------------------

@dataclass(frozen=True)
class Undo:
    """What a decision or a delete changed, to take it back with :meth:`Review.undo`."""
    kind: str                           # "review" or "delete"
    before: tuple                       # review: (job id, review fields + revision); delete: (job id, state, reason)
    restore: tuple = ()                 # delete: the job ids it hid
    batch_id: Optional[int] = None      # a batch delete: the batch shown again
    until: float = 0.0                  # when Undo ends (an accept is delivered then); 0: after the undo time


@dataclass
class Decision:
    decided: list = field(default_factory=list)       # the reports accepted or rejected (as they were)
    regenerated: list = field(default_factory=list)   # edited reports accepted: queued to be made again first
    undo: Optional[Undo] = None                       # takes the decided ones back


@dataclass
class Undone:
    ok: bool
    text: str                           # what to tell the analyst
    ids: list = field(default_factory=list)   # the reports taken back


class Review:
    """The analyst's decisions and deletes in Report², on a journal."""

    def __init__(self, journal: J.Journal, grace: float = J.UNDO_GRACE):
        self.journal = journal
        self.grace = grace

    def decide(self, job_ids: Iterable[str], accept: bool, comment: str = "",
               prepare: Optional[Callable[[str], bool]] = None) -> Decision:
        """Accept or reject the reports :func:`allowed` to be. An edited report accepted is made again first
        (it comes back as control needed: :meth:`Journal.update_edited`); an accept is delivered once the
        undo time is over. ``prepare(job_id)``, if given, runs first (the main window saves an open project)
        and False skips that report."""
        out, before, until = Decision(), [], []
        for jid in job_ids:
            job = self.journal.job(jid)
            if job is None or not allowed(job, "accept" if accept else "reject"):
                continue
            if prepare is not None and not prepare(jid):
                continue
            job = self.journal.job(jid)
            if accept and job.edited:
                if self.journal.update_edited(jid, comment):
                    out.regenerated.append(job)
                continue
            snap = {k: job.row.get(k) for k in J.Journal.REVIEW_FIELDS} | {"revision": job.revision}
            if self.journal.review(jid, accept, comment, grace=self.grace if accept else 0):
                out.decided.append(job)
                before.append((jid, snap))
                after = self.journal.job(jid)
                if after is not None and after.deliver_after:
                    until.append(float(after.deliver_after))
        if before:
            out.undo = Undo("review", tuple(before), until=min(until) if until else 0.0)
        return out

    def delete(self, job_ids: Iterable[str]) -> Optional[Undo]:
        """Hide reports in Report² (one still in the queue is removed from it)."""
        jobs = [j for j in (self.journal.job(i) for i in job_ids) if j is not None and not j.deleted]
        ids = self.journal.delete([j.id for j in jobs])
        if not ids:
            return None
        return Undo("delete", tuple((j.id, j.state, j.reason) for j in jobs if j.id in ids), tuple(ids))

    def delete_batch(self, batch_id: int) -> Optional[Undo]:
        """Hide a batch folder with all its reports."""
        jobs = [j for j in self.journal.jobs(batch_id=batch_id) if not j.deleted]
        if not self.journal.delete_batch(batch_id):
            return None
        return Undo("delete", tuple((j.id, j.state, j.reason) for j in jobs), batch_id=batch_id)

    def undo(self, token: Undo) -> Undone:
        """Take a decision or a delete back. A decision only while its report is as the decision left it
        and not delivered yet; a delete shows the reports again, back in the queue as they were."""
        if token.kind == "review":
            ids = [jid for jid, snap in token.before if self.journal.undo_review(jid, snap)]
            if ids:
                return Undone(True, f"Undone: {len(ids)} report(s) back as before.", ids)
            return Undone(False, "Not undone: the report was delivered meanwhile; reject it instead.")
        if token.batch_id is not None:
            self.journal.restore_batch(token.batch_id)
        else:
            self.journal.restore(token.restore)
        for jid, state, reason in token.before:
            self.journal.unremove(jid, state, reason)
        return Undone(True, "Restored.", [jid for jid, _, _ in token.before])


# -- delivery ------------------------------------------------------------------------------------------

def deliver_due(journal: J.Journal, *, only: Optional[Iterable[str]] = None, final: bool = False,
                now: Optional[float] = None, find: Optional[Callable] = None) -> list[str]:
    """Deliver the reports waiting for it (accepted, or needing control) whose undo time is over; returns
    the ids delivered. A delivery that fails is marked "error" (and logged), the others go on.

    ``only``: just these reports - a Report² panel without a watcher delivers what was decided there, never
    what someone else accepted in a shared journal. ``final``: their undo already ended (the panel's message
    is gone), so they go now. ``find(workflow_id)``: the workflow (default: as saved)."""
    from gcws.automation import export
    from gcws.automation import workflow as W
    now = time.time() if now is None else now
    find = find or W.find
    if only is None:
        jobs = journal.to_deliver()
    else:
        jobs = [j for j in (journal.job(i) for i in only)
                if j is not None and j.export_pending and j.state in J.DELIVERABLE]
    done = []
    for job in jobs:
        if not final and float(job.deliver_after or 0) > now:
            continue                                   # the analyst may still undo it
        wf = find(job.workflow_id)
        if wf is None:
            continue
        try:
            export.deliver(journal, wf, job)
            done.append(job.id)
        except Exception as exc:  # noqa: BLE001
            journal.update_job(job.id, export_state="error", export_pending=0)
            journal.event("error", f"{job.group_name}: delivery failed: {exc}", job_id=job.id,
                          workflow_id=job.workflow_id, batch_id=job.batch_id)
    return done
