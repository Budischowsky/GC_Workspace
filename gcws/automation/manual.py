"""Report² entries of double determinations accepted by hand.

A pair no workflow processed (a project the analyst made) is listed in Report² when it is accepted
in the Replicates panel: its report and a copy of its project go into a job folder, and the entry
stands, accepted by the analyst, under the batch of the runs' sequence folder (the workflow's batch
when a workflow watches that folder, else one of its own). The watcher never processes or delivers
such an entry; accepting the pair again replaces it (the Replicates panel asks first when the name
is taken).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from gcws.automation import journal as J

#: the workflow id (and method node) of the entries accepted by hand
MANUAL = J.MANUAL_WORKFLOW
LABEL = "Accepted by hand"


def is_manual(job) -> bool:
    return job is not None and job.workflow_id == MANUAL


def batch_for(journal: J.Journal, folder) -> dict:
    """The batch of ``folder`` in Report²: a workflow's (the oldest one not deleted), else its own."""
    key = os.path.normcase(os.path.abspath(str(folder)))
    for b in journal.batches():
        if b.get("folder_key") == key and not b.get("deleted") and b.get("workflow_id") != MANUAL:
            return b
    b = journal.batch(MANUAL, Path(folder))
    if b.get("deleted"):                         # deleted once: shown again for the new entry
        journal.update_batch(b["id"], deleted=0)
        b = journal.batch_by_id(b["id"])
    return b


def clash(journal: J.Journal, batch_id: int, name: str) -> Optional[J.Job]:
    """The sample of the batch (not deleted) that has the name ``name`` already (case does not matter)."""
    want = name.strip().casefold()
    return next((j for j in journal.jobs(batch_id=batch_id, include_batch=False)
                 if not j.deleted and (j.group_name or "").strip().casefold() == want), None)


def free_name(journal: J.Journal, batch_id: int, name: str) -> str:
    """``name (2)``, ``name (3)``, ...: the first one the batch does not have yet."""
    n = 2
    while clash(journal, batch_id, f"{name} ({n})") is not None:
        n += 1
    return f"{name} ({n})"


def group_key(name: str) -> str:
    return "manual:" + name.strip().casefold()


def entry(journal: J.Journal, batch_id: int, name: str) -> Optional[J.Job]:
    """The entry accepted by hand under ``name`` in the batch, also a deleted one."""
    return journal.find_job(MANUAL, MANUAL, batch_id, group_key(name))


def record(journal: J.Journal, *, job_id: str, batch_id: int, name: str, members: list, files: dict,
           project_path, job_dir, evidence: dict, findings: list, summary: dict, reviewer: str,
           replace: Optional[str] = None) -> J.Job:
    """List the accepted pair in Report²: a new entry ``job_id``, or the entry ``replace`` (accepted
    by hand) at its next revision."""
    return journal.record_manual(job_id, workflow_id=MANUAL, method_node=MANUAL, batch_id=batch_id,
                                 group_key=group_key(name), group_name=name, members=members, files=files,
                                 project_path=str(project_path), job_dir=str(job_dir), evidence=evidence,
                                 findings=findings, summary=summary, reviewer=reviewer, replace=replace)
