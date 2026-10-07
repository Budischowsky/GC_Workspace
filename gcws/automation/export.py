"""Copying finished reports to the target folders (the arrows decide what goes where).

A file is copied under a temporary name and then renamed, so a reader of the target folder
never sees half a report. Each file of a job revision is delivered once; the journal keeps
what went where. A target inside the watched raw-data folder is refused unless allowed.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from gcws.automation import journal as J
from gcws.automation import routing, store

DELIVERABLE = J.DELIVERABLE


def copy_atomic(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.gcws-tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return dst


def deliver(journal: J.Journal, wf, job: J.Job, force: bool = False) -> list[str]:
    """Copy what the arrows let through for ``job`` (not yet delivered in this revision, or delivered
    but no longer in the target folder). ``force``: deliver everything again (the analyst's "Deliver
    now"); a copy this revision delivered before is replaced, any other file follows the folder's rule.

    Returns a line per file (for the log). Updates the job's export state."""
    if job.state not in DELIVERABLE:
        return []
    batch = journal.batch_by_id(job.batch_id)
    src_root = (wf.source.p("folder") if wf.source else "") or ""
    ctx = {"status": job.state, "name": None if job.is_batch else job.group_name, "batch": batch.get("name", "")}
    tokens = {"batch": batch.get("name", ""), "sample": "" if job.is_batch else job.group_name,
              "date": datetime.now().strftime("%Y-%m-%d")}
    done = journal.delivered(job.id, job.revision)
    lines, errors = [], 0
    for d in routing.deliveries(wf, job.method_node, ctx, job.files or {}, tokens):
        key = (d.folder_node, d.report_node, d.fmt)
        before = done.get(key)
        if key in done and not force and (not before or Path(before).exists()):
            continue                                   # delivered and still there
        folder = wf.node(d.folder_node)
        try:
            if src_root and store.is_inside(d.dst, src_root) and not folder.p("allow_inside_source"):
                raise PermissionError("the target lies inside the watched raw-data folder")
            if not d.src.is_file():
                raise FileNotFoundError(f"{d.src.name} is missing")
            if before and Path(before).is_file() and Path(before).parent == d.dst.parent:
                dst = Path(before)                     # this revision's own copy: replaced
            else:
                dst = routing.resolve_collision(d.dst, folder.p("overwrite") or "version")
            if dst is None:
                journal.add_export(job.id, job.revision, d.folder_node, d.report_node, d.fmt, d.src, d.dst,
                                   "done", "kept the existing file")
                lines.append(f"kept {d.dst}")
                continue
            copy_atomic(d.src, dst)
            journal.add_export(job.id, job.revision, d.folder_node, d.report_node, d.fmt, d.src, dst)
            lines.append(f"{d.src.name} -> {dst.parent}")
        except OSError as exc:
            errors += 1
            journal.add_export(job.id, job.revision, d.folder_node, d.report_node, d.fmt, d.src, d.dst, "error",
                               str(exc))
            journal.event("error", f"{job.group_name}: {d.src.name} not copied to {d.dst.parent}: {exc}",
                          job_id=job.id, workflow_id=job.workflow_id, batch_id=job.batch_id)
    if lines:
        journal.event("info", f"{job.group_name}: delivered " + "; ".join(lines), job_id=job.id,
                      workflow_id=job.workflow_id, batch_id=job.batch_id)
    if job.state in (J.ACCEPTED_AUTO, J.ACCEPTED_MANUAL) and not job.is_batch:
        _register(journal, job)
    state = "error" if errors else ("done" if job.state != J.CONTROL else "partial")
    journal.update_job(job.id, export_state=state, export_pending=0)
    return lines


def _register(journal: J.Journal, job: J.Job) -> None:
    """The reported substances into the unknown register ("already reported"), once per revision."""
    if ("", "", "register") in journal.exported(job.id, job.revision):
        return
    from gcws.report import service as RS
    err = ""
    for node, rep in ((job.evidence or {}).get("reported") or {}).items():
        e = RS.record_seen(rep.get("kind", "nias"), rep.get("rows") or [], Path(rep.get("target", "")),
                           rep.get("sample_key", ""))
        err = err or e
    journal.add_export(job.id, job.revision, "", "", "register", "", "", "error" if err else "done", err)
