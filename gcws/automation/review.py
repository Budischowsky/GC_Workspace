"""The Report² entry: what a report's journal row means and what may be done with it, without Qt.

The journal (:mod:`gcws.automation.journal`) stores the rows and their state transitions; this module
answers the questions every view asks of them - which filter chip a report counts for, its colour, whether
it is delivered, whether it is done, whether its batch is closed (Report²'s Archive) - and which actions the
analyst may take on it (:func:`allowed`; the journal's verbs refuse the same). Report², the automation
overview, the Automation panel and the main window all ask here, so they always agree.
"""
from __future__ import annotations

import json

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
