"""Running one job: what the job process gets (its spec) and what becomes of its result.

Shared by the watcher, which processes the queue, and GC Workspace, which makes an edited report again
itself as soon as the analyst accepts it (:class:`LocalJobs`), so that this never waits for the watcher.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from gcws.automation import journal as J
from gcws.automation import localcopy as LC
from gcws.automation import rules as RU
from gcws.automation import store
from gcws.automation import workflow as W

BACKOFF_S = (120, 600, 1800)          # retries of a job that could not read its files


def blank_requirement(node: W.Node, method: dict) -> str:
    """The method step's blank requirement; "auto" follows the method's blank subtraction source."""
    req = node.p("require_blank") or "auto"
    if req != "auto":
        return req
    source = (((method or {}).get("sections") or {}).get("blank") or {}).get("source", "blank")
    return {"blank_istd": "blank_istd", "both": "both"}.get(source, "blank")


def missing_sample_files(out_dir: Path, files: dict) -> list[str]:
    """Formats the workflow requested but the regenerated revision did not produce."""
    try:
        spec = json.loads((out_dir / "spec.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["job specification"]
    missing = []
    for report in spec.get("reports") or []:
        produced = files.get(report.get("node")) or {}
        wanted = (set(report.get("formats") or []) & set(W.SAMPLE_FORMATS)) | {"xlsx"}
        for fmt in sorted(wanted):
            path = produced.get(fmt)
            if not path or not Path(path).is_file():
                missing.append(f"{report.get('kind', 'report')} {fmt}")
    return missing


def load_method(name: str) -> dict:
    from gcws.core import proc_method as PM
    try:
        return PM.load(name)
    except KeyError:
        return {}


def job_spec(journal: J.Journal, wf: W.Workflow, job: J.Job, method: Callable[[str], dict] = load_method
             ) -> tuple[dict, str]:
    """``(spec, kind)`` of a job: what its process does ("job": a sample, "batch": the batch report)."""
    m = wf.node(job.method_node)
    b = journal.batch_by_id(job.batch_id)
    out_dir = store.jobs_dir() / job.id / f"r{job.revision}"
    out_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    for r, _ in wf.report_nodes(m.id):
        if any(x["node"] == r.id for x in reports):
            continue
        reports.append({"node": r.id, "kind": r.p("kind"), "keep_middle": bool(r.p("keep_middle")),
                        "formats": [f for f in r.p("formats") or [] if f in W.SAMPLE_FORMATS],
                        "batch_formats": [f for f in r.p("formats") or [] if f in W.BATCH_FORMATS]})
    if job.is_batch:
        entries = []
        members = {j.id: j for j in journal.jobs(workflow_id=wf.id, batch_id=job.batch_id, include_batch=False)}
        rep = next((r for r in reports if r["batch_formats"]), None)
        for jid in job.members or []:
            j = members.get(jid)
            if j is None:
                continue
            files = (j.files or {}).get(rep["node"], {}) if rep else {}
            entries.append({"name": j.group_name, "state": j.state, "reviewer": j.reviewer or "",
                            "reviewed": J.when(j.reviewed_at), "comment": j.comment or "",
                            "findings": j.findings or [], "xlsx": files.get("xlsx"), "report": files.get("docx")
                            or files.get("xlsx") or ""})
        return {"job_id": job.id, "out_dir": str(out_dir), "batch_name": b.get("name", ""), "entries": entries,
                "formats": rep["batch_formats"] if rep else [], "report_node": rep["node"] if rep else ""}, \
            "batch"
    meth = method(m.p("method"))
    review = wf.review_node(m.id)
    rules = RU.to_list(RU.rules_for(review.params)) if review is not None else None
    prev_project = ""
    if job.mode == "rereport":
        prev_project = job.project_path or ""
    # fed by the local copy: the runs are read from there (the job copies a missing one itself)
    batch_folder, source_folder = b.get("folder", ""), ""
    via, _ = wf.feed(m.id)
    if via is not None and batch_folder:
        source_folder = batch_folder
        batch_folder = str(LC.local_batch(via.p("folder"), wf.source.p("folder"), batch_folder))
    spec = {"job_id": job.id, "revision": job.revision, "mode": job.mode or "full", "workflow_id": wf.id,
            "method_node": m.id, "method": meth, "batch_folder": batch_folder, "source_folder": source_folder,
            "group": {"key": job.group_key, "name": job.group_name, "members": job.members or []},
            "blanks": job.blanks or {}, "reports": reports, "rules": rules,
            "auto_accept": bool(review.p("auto_accept")) if review is not None else True,
            "has_review": review is not None, "out_dir": str(out_dir), "project_path": prev_project,
            "require_blank": blank_requirement(m, meth), "search": bool(m.p("search")),
            "istd_detect": bool(m.p("istd_detect")), "min_confidence": m.p("min_confidence") or "high",
            "override": job.override or {}}
    return spec, "job"


def finish(journal: J.Journal, job_id: str, out_dir: Path, tail: str = "", timed_out: bool = False,
           now: Optional[float] = None) -> Optional[dict]:
    """Take in the result of a job process: the job's new state, findings and files. Returns
    ``{"state", "retry", "reason", "findings"}`` (None: the job is gone)."""
    from gcws.automation import pipeline as PL
    now = time.time() if now is None else now
    job = journal.job(job_id)
    if job is None:
        return None
    out_dir = Path(out_dir or job.job_dir or "")
    res = PL.read_result(out_dir) if str(out_dir) else None
    if res is None:
        reason = "the job process ended without a result" + (" (timeout)" if timed_out else "")
        logtail = ""
        try:
            logtail = (out_dir / "job.log").read_text(encoding="utf-8", errors="replace")[-600:]
        except OSError:
            logtail = tail[-600:]
        res = {"state": PL.FAILED, "reason": reason + (": " + logtail.strip().splitlines()[-1]
                                                       if logtail.strip() else "")}
    state = res.get("state")
    fields = {"finished": now, "reason": res.get("reason", ""), "files": res.get("files") or {},
              "findings": res.get("findings") or [], "evidence": res.get("evidence") or {},
              "summary": {"warnings": res.get("warnings") or [], "timings": res.get("timings") or {}},
              "pid": None}
    if res.get("project"):
        fields["project_path"] = res["project"]
    if job.is_batch and state == PL.ACCEPTED_AUTO:
        # accepted when every sample with a report is accepted (samples not processed are only listed)
        members = [m for m in (journal.job(j) for j in job.members or []) if m is not None]
        reported = [m for m in members if m.state != J.NOT_PROCESSED]
        accepted = bool(reported) and all(m.state in J.ACCEPTED for m in reported)
        state = J.ACCEPTED_AUTO if accepted else J.CONTROL
    if state == PL.RETRY:
        attempts = int(job.attempts or 0) + 1
        if attempts <= len(BACKOFF_S):
            journal.transition(job_id, J.PROCESSING, J.QUEUED, attempts=attempts, pid=None,
                               not_before=now + BACKOFF_S[attempts - 1], reason=res.get("reason", ""))
            journal.event("warning", f"{job.group_name}: {res.get('reason', '')}; trying again in "
                          f"{BACKOFF_S[attempts - 1] // 60} min", job_id=job_id)
            return {"state": J.QUEUED, "retry": True, "reason": res.get("reason", ""), "findings": []}
        state = PL.FAILED
    pending = job.review_pending if not job.is_batch else None
    if pending:
        old_keys = {tuple(k) for k in pending.get("findings") or []}
        new_keys = {J.finding_key(f) for f in fields["findings"] if f.get("level", "control") == "control"}
        missing = missing_sample_files(out_dir, fields["files"])
        generated = (state in (J.CONTROL, J.ACCEPTED_AUTO) and bool(fields["files"])
                     and not (fields["evidence"] or {}).get("errors") and not missing)
        fields["review_pending"] = None
        if not generated:
            state = J.CONTROL
            fields.update(reason="Edited report could not be regenerated: " +
                          (res.get("reason") or ("missing report files: " + ", ".join(missing) if missing else "")
                           or "; ".join(res.get("warnings") or []) or "no report was made"),
                          files=job.files or {}, findings=job.findings or [], evidence=job.evidence or {},
                          edited=1, export_pending=0)
        elif new_keys - old_keys and not pending.get("keep"):
            state = J.CONTROL
            fields.update(reason="New findings in the regenerated report; review them before accepting",
                          edited=0, export_pending=0)
        else:
            # the analyst accepted the edited report: it stays accepted (new findings are listed with it)
            state = J.ACCEPTED_MANUAL
            new = len(new_keys - old_keys)
            fields.update(reason=f"the updated report has {new} new finding(s)" if new else "", edited=0,
                          reviewer=pending.get("user") or "", comment=pending.get("comment") or "",
                          reviewed_at=now, export_pending=1)
            journal.event("info", f"{job.group_name}: updated report accepted by {fields['reviewer']}"
                          + (f" ({new} new finding(s))" if new else ""), job_id=job.id,
                          workflow_id=job.workflow_id, batch_id=job.batch_id, user=fields["reviewer"])
    if state not in (J.CONTROL, J.ACCEPTED_AUTO, J.ACCEPTED_MANUAL, J.NOT_PROCESSED, J.FAILED):
        state = J.FAILED
    if state in (J.CONTROL, J.ACCEPTED_AUTO) and not pending:
        fields["export_pending"] = 1
    journal.transition(job_id, J.PROCESSING, state, **fields)
    text = {J.CONTROL: "control needed", J.ACCEPTED_AUTO: "accepted automatically",
            J.ACCEPTED_MANUAL: "accepted by analyst",
            J.NOT_PROCESSED: "not processed", J.FAILED: "failed"}[state]
    level = "info" if state in (J.ACCEPTED_AUTO, J.ACCEPTED_MANUAL, J.CONTROL) else "error"
    journal.event(level, f"{job.group_name}: {text}" + (f" - {res.get('reason')}" if res.get("reason") else ""),
                  job_id=job_id, workflow_id=job.workflow_id, batch_id=job.batch_id)
    return {"state": state, "retry": False, "reason": fields["reason"], "findings": fields["findings"]}


def cannot_start(journal: J.Journal, job: J.Job, reason: str, now: Optional[float] = None) -> None:
    """An updated report whose job cannot even start goes back to control with the reason."""
    if journal.transition(job.id, J.QUEUED, J.PROCESSING):
        journal.transition(job.id, J.PROCESSING, J.CONTROL, reason=f"cannot start: {reason}",
                           finished=time.time() if now is None else now, edited=1, review_pending=None,
                           export_pending=0)
        journal.event("error", f"{job.group_name}: updated report cannot start: {reason}",
                      job_id=job.id, workflow_id=job.workflow_id, batch_id=job.batch_id)


class LocalJobs(QObject):
    """GC Workspace makes an edited report again itself, as soon as the analyst accepts it: one job at a
    time, each in its own process like the watcher's, so the analyst never waits for the watcher (or for
    it to be started). A job left when GC Workspace closes goes back to the queue."""
    finished = Signal(str, str)                              # job id, its state afterwards

    def __init__(self, journal: Callable[[], J.Journal], launcher=None, parent=None):
        super().__init__(parent)
        self._journal = journal
        if launcher is None:
            from gcws.automation.watcher import ProcessLauncher
            launcher = ProcessLauncher(self)
        self.launcher = launcher
        self.launcher.finished.connect(self._done)
        self.todo: list[str] = []
        self.current: Optional[dict] = None

    @property
    def journal(self) -> J.Journal:
        return self._journal()

    def busy(self, job_id: str) -> bool:
        return job_id in self.todo or (self.current or {}).get("job") == job_id

    def run(self, job_id: str) -> bool:
        """Make the report of the queued job ``job_id`` (after the one being made)."""
        if not self.busy(job_id):
            self.todo.append(job_id)
        self._next()
        return self.busy(job_id)

    def _next(self):
        while self.current is None and not self.launcher.running() and self.todo:
            jid = self.todo.pop(0)
            job = self.journal.job(jid)
            if job is None or job.state != J.QUEUED:
                continue                                     # the watcher took it meanwhile
            wf = W.find(job.workflow_id)
            if wf is None or wf.node(job.method_node) is None:
                cannot_start(self.journal, job, "workflow or method is unavailable")
                self.finished.emit(jid, J.CONTROL)
                continue
            try:
                spec, kind = job_spec(self.journal, wf, job)
            except Exception as exc:  # noqa: BLE001
                cannot_start(self.journal, job, str(exc))
                self.finished.emit(jid, J.CONTROL)
                continue
            spec_path = store.atomic_write_json(Path(spec["out_dir"]) / "spec.json", spec)
            if not self.journal.transition(jid, J.QUEUED, J.PROCESSING, started=time.time(), pid=os.getpid(),
                                           job_dir=spec["out_dir"], reason=""):
                continue
            self.journal.event("info", f"{job.group_name}: making the updated report in GC Workspace", job_id=jid,
                               workflow_id=job.workflow_id, batch_id=job.batch_id)
            self.current = {"job": jid, "out_dir": spec["out_dir"], "timed_out": False}
            timeout = float(wf.node(job.method_node).p("timeout_min") or 30) * 60
            QTimer.singleShot(int(timeout * 1000), lambda j=jid: self._timeout(j))
            self.launcher.start(jid, spec_path, kind)

    def _timeout(self, job_id: str):
        if (self.current or {}).get("job") == job_id:
            self.current["timed_out"] = True
            self.launcher.kill()

    def _done(self, job_id: str, code: int, tail: str):
        cur, self.current = self.current, None
        if cur is None or cur.get("job") != job_id:
            return                                           # stopped meanwhile
        out = finish(self.journal, job_id, Path(cur["out_dir"]), tail, bool(cur.get("timed_out")))
        self.finished.emit(job_id, (out or {}).get("state", ""))
        self._next()

    def stop(self):
        """GC Workspace closes: the report being made goes back to the queue (a watcher makes it)."""
        cur, self.current = self.current, None
        self.todo.clear()
        if cur is None:
            return
        self.launcher.kill()
        self.journal.transition(cur["job"], J.PROCESSING, J.QUEUED, pid=None,
                                reason="GC Workspace was closed while it made the report")
