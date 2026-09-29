"""GC Workspace Watcher: the background process that watches folders and processes new data.

``python -m gcws --watch`` (or *Automation > Start watcher*). It has a tray icon and keeps
running when the main window is closed. Every enabled workflow's folder is looked at every
*x* minutes; finished samples are processed one at a time in a separate job process (a crash
or a hanging Office program cannot stop the watcher), judged by Report² and delivered to the
target folders. One watcher runs per data folder (a local socket keeps a second one out); the
GUI talks to it through that socket and reads everything else from the journal.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from gcws import paths
from gcws.automation import journal as J
from gcws.automation import planner as PN
from gcws.automation import rules as RU
from gcws.automation import scanner as SC
from gcws.automation import store
from gcws.automation import workflow as W

log = logging.getLogger("gcws.watcher")

BACKOFF_S = (120, 600, 1800)          # retries of a job that could not read its files
HEARTBEAT_S = 10


def server_name() -> str:
    """The local socket of the watcher of this data folder (one per user and data folder)."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    digest = hashlib.sha1(str(paths.DATA).casefold().encode("utf-8")).hexdigest()[:10]
    return f"gcws-watcher-{store.safe_name(user, 30)}-{digest}"


def python_exe(windowless: bool = True) -> str:
    exe = Path(sys.executable)
    if windowless and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        return str(exe.parent / "pythonw.exe")
    return str(exe)


def blank_requirement(node: W.Node, method: dict) -> str:
    """The method step's blank requirement; "auto" follows the method's blank subtraction source."""
    req = node.p("require_blank") or "auto"
    if req != "auto":
        return req
    source = (((method or {}).get("sections") or {}).get("blank") or {}).get("source", "blank")
    return {"blank_istd": "blank_istd", "both": "both"}.get(source, "blank")


# -- launching job processes -----------------------------------------------------------------------

class ProcessLauncher(QObject):
    """One job process at a time (``python -m gcws --process-job spec.json``)."""
    finished = Signal(str, int, str)                 # job id, exit code, output tail

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: Optional[QProcess] = None
        self.job_id = ""

    def running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def start(self, job_id: str, spec_path: Path, kind: str = "job") -> None:
        p = QProcess(self)
        p.setProgram(python_exe())
        p.setArguments(["-m", "gcws", "--batch-report" if kind == "batch" else "--process-job", str(spec_path)])
        p.setWorkingDirectory(str(paths.ROOT))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("QT_QPA_PLATFORM", "offscreen")
        env.insert("GCWS_DATA", str(paths.DATA))
        env.insert("PYTHONUTF8", "1")
        p.setProcessEnvironment(env)
        p.setProcessChannelMode(QProcess.MergedChannels)
        p.finished.connect(lambda code, status: self._done(p, code, status))
        p.errorOccurred.connect(lambda err: self._failed(p, err))
        self.proc, self.job_id = p, job_id
        p.start()

    def _done(self, p, code, status):
        if p is not self.proc:
            return
        tail = bytes(p.readAll()).decode("utf-8", "replace")[-2000:]
        self.proc = None
        self.finished.emit(self.job_id, int(code) if status == QProcess.NormalExit else -1, tail)

    def _failed(self, p, err):
        if err == QProcess.FailedToStart and p is self.proc:
            self.proc = None
            self.finished.emit(self.job_id, -2, f"the job process did not start ({p.errorString()})")

    def kill(self) -> None:
        if self.running():
            self.proc.kill()


# -- the watcher ------------------------------------------------------------------------------------

class WatcherCore(QObject):
    """Scanning, planning, the job queue and delivery. No widgets (the tray is in :class:`WatcherApp`)."""
    changed = Signal()
    notify = Signal(str, str)                        # title, text

    def __init__(self, journal: Optional[J.Journal] = None, launcher=None, clock=time.time, parent=None,
                 tick_ms: int = 5000):
        super().__init__(parent)
        self.journal = journal or J.Journal()
        self.launcher = launcher or ProcessLauncher(self)
        self.launcher.finished.connect(self._job_finished)
        self.clock = clock
        self.paused = False
        self.workflows: dict[str, W.Workflow] = {}
        self._stamp: dict[str, float] = {}
        self._last_scan: dict[str, float] = {}
        self._scan_now = False
        self._methods: dict[str, dict] = {}
        self.current: Optional[dict] = None          # {"job": id, "started": t, "timeout": s}
        self.started = clock()
        self.timer = QTimer(self)
        self.timer.setInterval(tick_ms)
        self.timer.timeout.connect(self.tick)
        self.hb = QTimer(self)
        self.hb.setInterval(HEARTBEAT_S * 1000)
        self.hb.timeout.connect(self.heartbeat)

    def start(self) -> None:
        self.reload()
        self.heartbeat()
        self.journal.event("info", "Watcher started" + (" (paused)" if self.paused else ""))
        self.timer.start()
        self.hb.start()
        QTimer.singleShot(0, self.tick)

    def stop(self) -> None:
        self.timer.stop()
        self.hb.stop()
        self.journal.heartbeat("stopped", "", "")
        self.journal.event("info", "Watcher stopped")

    def status(self) -> str:
        return "paused" if self.paused else ("processing" if self.current else "running")

    def heartbeat(self) -> None:
        cur = self.current["job"] if self.current else ""
        try:
            self.journal.heartbeat(self.status(), cur, "", self.started)
        except Exception as exc:  # noqa: BLE001 - the journal may be busy for a moment
            log.warning("heartbeat: %s", exc)

    # -- workflows ------------------------------------------------------------------------------

    def reload(self) -> None:
        """Read the workflow files again (only the changed ones)."""
        folder = store.workflows_dir()
        seen = set()
        for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            wid = f.stem
            seen.add(wid)
            if self._stamp.get(wid) == mtime:
                continue
            self._stamp[wid] = mtime
            try:
                wf = W.load(f)
            except (OSError, ValueError, TypeError) as exc:
                self.journal.event("error", f"Workflow file {f.name} not readable: {exc}")
                continue
            self._methods.clear()
            if not wf.enabled:
                self.workflows.pop(wf.id, None)
                continue
            problems = W.errors(W.validate(wf, method_names=self._method_names(), word=True,
                                           method_loader=self.method))
            if problems:
                self.workflows.pop(wf.id, None)
                self.journal.event("error", f"Workflow '{wf.name}' not started: " + "; ".join(p.text for p in problems),
                                   workflow_id=wf.id)
                continue
            if wf.id not in self.workflows:
                self.journal.event("info", f"Workflow '{wf.name}' active: {wf.source.p('folder')}", workflow_id=wf.id)
            self.workflows[wf.id] = wf
        for wid in list(self.workflows):
            if wid not in seen:
                self.workflows.pop(wid, None)

    def _method_names(self) -> list[str]:
        from gcws.core import proc_method as PM
        return PM.names()

    def method(self, name: str) -> dict:
        if name not in self._methods:
            from gcws.core import proc_method as PM
            try:
                self._methods[name] = PM.load(name)
            except KeyError:
                self._methods[name] = {}
        return self._methods[name]

    # -- the loop -------------------------------------------------------------------------------------

    def scan_now(self) -> None:
        self._scan_now = True
        QTimer.singleShot(0, self.tick)

    def tick(self) -> None:
        now = self.clock()
        try:
            self.reload()
            for wf in list(self.workflows.values()):
                interval = max(1.0, float(wf.source.p("interval_min") or 5)) * 60
                if self._scan_now or now - self._last_scan.get(wf.id, 0) >= interval:
                    self._last_scan[wf.id] = now
                    self.scan(wf, now)
            self._scan_now = False
            self.deliver_pending()
            self.check_timeout(now)
            if not self.paused:
                self.pump(now)
        except Exception:  # noqa: BLE001 - the watcher keeps going
            import traceback
            log.error(traceback.format_exc())
        self.changed.emit()

    # -- scanning and planning ------------------------------------------------------------------------

    def scan(self, wf: W.Workflow, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        src = wf.source
        first = self.journal.first_scan(wf.id, src.p("folder"))
        cfg = SC.Readiness(int(src.p("stable_scans") or 2), float(src.p("min_age_min") or 0) * 60,
                           float(src.p("quiet_min") or 30) * 60)
        folders = SC.batch_folders(src.p("folder"), int(src.p("depth") or 0), src.p("pattern") or "*",
                                   float(src.p("ignore_older_days") or 0), now)
        for folder in folders:
            self._scan_batch(wf, folder, now, cfg, baseline_new=first and not src.p("process_existing"))

    def _scan_batch(self, wf, folder: Path, now: float, cfg: SC.Readiness, baseline_new: bool) -> None:
        from gcws.io import sequence as SQ
        b = self.journal.batch(wf.id, folder)
        prev = self.journal.runs(b["id"])
        obs = SC.observe(folder)
        seq = SQ.read_sequence(folder, present=[o.name for o in obs])
        changed = any(prev.get(o.stem, {}).get("fingerprint") != o.fingerprint for o in obs)
        last_change = now if changed or not b.get("last_change") else float(b["last_change"])
        quiet = now - last_change >= cfg.quiet_s
        order = [s for s in seq.stems] + sorted((o.stem for o in obs if o.stem not in seq.stems),
                                               key=lambda s: SQ.order_key(s))
        present_stems = {o.stem for o in obs}
        present = {}
        for o in obs:
            i = order.index(o.stem) if o.stem in order else -1
            successor = i >= 0 and any(s in present_stems for s in order[i + 1:])
            state, count = SC.readiness(prev.get(o.stem), o, now, cfg, successor_started=successor,
                                        seq_finished=seq.finished, folder_quiet=quiet)
            row = prev.get(o.stem)
            fields = {"path": str(o.path), "fingerprint": o.fingerprint, "stable_count": count, "state": state,
                      "marker": int(o.marker), "role": SQ.classify_role(o.name)}
            if row is None or row.get("fingerprint") != o.fingerprint:
                fields["last_change"] = now
            if row is None and baseline_new:
                fields["baseline"] = 1
            self.journal.upsert_run(b["id"], o.stem, **fields)
            present[o.stem] = {"name": o.name, "ready": state == "ready"}
        runs = self.journal.runs(b["id"])
        baseline = {s for s, r in runs.items() if r.get("baseline")}
        self.journal.update_batch(b["id"], last_change=last_change, has_log=int(bool(seq.lines)),
                                  seq_completed=int(seq.finished))
        fps = {s: r.get("fingerprint") or "" for s, r in runs.items()}
        names = {s: r.get("path") and Path(r["path"]).name for s, r in runs.items()}
        for m in wf.methods():
            method = self.method(m.p("method"))
            plan = PN.plan_batch(present, seq, quiet=quiet, require=blank_requirement(m, method), baseline=baseline)
            edge = wf.source_edge(m.id)
            for g in plan.groups:
                if g.state == PN.BASELINE:
                    continue
                if edge is not None and not W.passes(edge.filter, {"name": g.name, "batch": folder.name}):
                    continue
                blanks = {k: [names.get(s) or s for s in v if s in names] for k, v in g.blanks.items()}
                members = [names.get(s) or s for s in g.members]
                fp = hashlib.sha1(json.dumps([[fps.get(s, "") for s in g.members],
                                              [fps.get(s, "") for v in g.blanks.values() for s in v]]).encode()
                                  ).hexdigest()[:16]
                job = self.journal.ensure_job(wf.id, m.id, b["id"], g.key, g.name, members, blanks, fp,
                                              reason=g.reason)
                if job.state == J.WAITING:
                    if g.state == PN.READY:
                        self.journal.transition(job.id, J.WAITING, J.QUEUED, queued_at=now, reason="")
                    elif g.state == PN.NOT_PROCESSED:
                        if self.journal.transition(job.id, J.WAITING, J.NOT_PROCESSED, reason=g.reason):
                            self.journal.event("warning", f"{g.name}: not processed - {g.reason}", job_id=job.id,
                                               workflow_id=wf.id, batch_id=b["id"])
                            self.notify.emit("Not processed", f"{g.name}: {g.reason}")
                    else:
                        self.journal.update_job(job.id, reason=g.reason)
            if plan.finished:
                # planned samples whose runs never came (the sequence ended or was stopped)
                keys = {g.key for g in plan.groups}
                for j in self.journal.jobs(workflow_id=wf.id, batch_id=b["id"], states=[J.WAITING],
                                           include_batch=False):
                    if j.method_node == m.id and j.group_key not in keys and self.journal.transition(
                            j.id, J.WAITING, J.NOT_PROCESSED, reason="the sequence ended without these runs"):
                        self.journal.event("warning", f"{j.group_name}: not processed - the sequence ended without "
                                           "its runs", job_id=j.id, workflow_id=wf.id, batch_id=b["id"])
            # a sample the analyst removed from the queue is not waited for
            removed = {j.group_key for j in self.journal.jobs(workflow_id=wf.id, batch_id=b["id"],
                                                              states=[J.REMOVED], include_batch=False)
                       if j.method_node == m.id}
            complete = (plan.finished or bool(seq.lines)) and all(
                g.state != PN.WAITING or g.key in removed for g in plan.groups) if removed else plan.complete
            self.journal.update_batch(b["id"], plan={"complete": complete, "groups": [
                {"key": g.key, "name": g.name, "state": "removed" if g.key in removed else g.state,
                 "reason": g.reason} for g in plan.groups]})
            if complete:
                self._batch_report(wf, m, b, folder)

    # -- the batch report ------------------------------------------------------------------------------

    def _batch_report(self, wf: W.Workflow, m: W.Node, b: dict, folder: Path) -> None:
        reports = [r for r, _ in wf.report_nodes(m.id) if set(r.p("formats") or []) & set(W.BATCH_FORMATS)]
        if not reports:
            return
        jobs = [j for j in self.journal.jobs(workflow_id=wf.id, batch_id=b["id"], include_batch=False)
                if j.method_node == m.id and j.state != J.REMOVED]
        if not jobs:
            return
        states = {j.state for j in jobs}
        when = {r.p("batch_when") for r in reports}
        final = {J.ACCEPTED_AUTO, J.ACCEPTED_MANUAL, J.REJECTED, J.NOT_PROCESSED, J.FAILED, J.CONTROL}
        if not states <= final:
            return
        if "all_processed" not in when and not states <= {J.ACCEPTED_AUTO, J.ACCEPTED_MANUAL, J.REJECTED,
                                                          J.NOT_PROCESSED}:
            return                                     # a sample still needs the analyst
        fp = hashlib.sha1(json.dumps(sorted((j.id, j.revision, j.state) for j in jobs)).encode()).hexdigest()[:16]
        job = self.journal.ensure_job(wf.id, m.id, b["id"], J.BATCH_KEY, f"Batch {b['name']}", [j.id for j in jobs],
                                      {}, fp, state=J.QUEUED, reason="")
        if job.state == J.WAITING:
            self.journal.transition(job.id, J.WAITING, J.QUEUED, queued_at=self.clock())

    # -- the queue ---------------------------------------------------------------------------------------

    def check_timeout(self, now: float) -> None:
        if self.current and now - self.current["started"] > self.current["timeout"]:
            self.journal.event("error", f"Job {self.current['job']} stopped after "
                               f"{self.current['timeout'] / 60:.0f} min", job_id=self.current["job"])
            self.current["timed_out"] = True
            self.launcher.kill()

    def pump(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        if self.current is not None or self.launcher.running():
            return
        for job in self.journal.jobs(states=[J.QUEUED]):
            if float(job.not_before or 0) > now:
                continue
            wf = self.workflows.get(job.workflow_id)
            if wf is None or wf.node(job.method_node) is None:
                continue
            try:
                spec, kind = self.spec_for(wf, job)
            except Exception as exc:  # noqa: BLE001
                self.journal.transition(job.id, J.QUEUED, J.PROCESSING)
                self.journal.transition(job.id, J.PROCESSING, J.FAILED, reason=f"cannot start: {exc}", finished=now)
                continue
            spec_path = store.atomic_write_json(Path(spec["out_dir"]) / "spec.json", spec)
            if not self.journal.transition(job.id, J.QUEUED, J.PROCESSING, started=now, job_dir=spec["out_dir"],
                                           reason=""):
                continue
            timeout = float(wf.node(job.method_node).p("timeout_min") or 30) * 60
            self.current = {"job": job.id, "started": now, "timeout": timeout, "kind": kind,
                            "out_dir": spec["out_dir"]}
            self.journal.event("info", f"{job.group_name}: processing", job_id=job.id, workflow_id=wf.id,
                               batch_id=job.batch_id)
            self.heartbeat()
            self.launcher.start(job.id, spec_path, kind)
            return

    def spec_for(self, wf: W.Workflow, job: J.Job) -> tuple[dict, str]:
        m = wf.node(job.method_node)
        b = self.journal.batch_by_id(job.batch_id)
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
            members = {j.id: j for j in self.journal.jobs(workflow_id=wf.id, batch_id=job.batch_id,
                                                          include_batch=False)}
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
        method = self.method(m.p("method"))
        review = wf.review_node(m.id)
        rules = RU.to_list(RU.rules_for(review.params)) if review is not None else None
        prev_project = ""
        if job.mode == "rereport":
            prev_project = job.project_path or ""
        spec = {"job_id": job.id, "revision": job.revision, "mode": job.mode or "full", "workflow_id": wf.id,
                "method_node": m.id, "method": method, "batch_folder": b.get("folder", ""),
                "group": {"key": job.group_key, "name": job.group_name, "members": job.members or []},
                "blanks": job.blanks or {}, "reports": reports, "rules": rules,
                "auto_accept": bool(review.p("auto_accept")) if review is not None else True,
                "has_review": review is not None, "out_dir": str(out_dir), "project_path": prev_project,
                "require_blank": blank_requirement(m, method), "search": bool(m.p("search")),
                "istd_detect": bool(m.p("istd_detect")), "min_confidence": m.p("min_confidence") or "high",
                "override": job.override or {}}
        return spec, "job"

    def _job_finished(self, job_id: str, code: int, tail: str) -> None:
        from gcws.automation import pipeline as PL
        cur, self.current = self.current or {}, None
        now = self.clock()
        job = self.journal.job(job_id)
        if job is None:
            return
        out_dir = Path(cur.get("out_dir") or job.job_dir or "")
        res = PL.read_result(out_dir) if out_dir else None
        if res is None:
            reason = "the job process ended without a result" + (" (timeout)" if cur.get("timed_out") else "")
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
                  "summary": {"warnings": res.get("warnings") or [], "timings": res.get("timings") or {}}}
        if res.get("project"):
            fields["project_path"] = res["project"]
        if job.is_batch and state == PL.ACCEPTED_AUTO:
            # accepted when every sample with a report is accepted (samples not processed are only listed)
            members = [m for m in (self.journal.job(j) for j in job.members or []) if m is not None]
            reported = [m for m in members if m.state != J.NOT_PROCESSED]
            accepted = bool(reported) and all(m.state in J.ACCEPTED for m in reported)
            state = J.ACCEPTED_AUTO if accepted else J.CONTROL
        if state == PL.RETRY:
            attempts = int(job.attempts or 0) + 1
            if attempts <= len(BACKOFF_S):
                self.journal.transition(job_id, J.PROCESSING, J.QUEUED, attempts=attempts,
                                        not_before=now + BACKOFF_S[attempts - 1], reason=res.get("reason", ""))
                self.journal.event("warning", f"{job.group_name}: {res.get('reason', '')}; trying again in "
                                   f"{BACKOFF_S[attempts - 1] // 60} min", job_id=job_id)
                return
            state = PL.FAILED
        if state not in (J.CONTROL, J.ACCEPTED_AUTO, J.NOT_PROCESSED, J.FAILED):
            state = J.FAILED
        if state in (J.CONTROL, J.ACCEPTED_AUTO):
            fields["export_pending"] = 1
        self.journal.transition(job_id, J.PROCESSING, state, **fields)
        text = {J.CONTROL: "control needed", J.ACCEPTED_AUTO: "accepted automatically",
                J.NOT_PROCESSED: "not processed", J.FAILED: "failed"}[state]
        level = "info" if state in (J.ACCEPTED_AUTO, J.CONTROL) else "error"
        self.journal.event(level, f"{job.group_name}: {text}" + (f" - {res.get('reason')}" if res.get("reason") else ""),
                           job_id=job_id, workflow_id=job.workflow_id, batch_id=job.batch_id)
        if state == J.CONTROL and not job.is_batch:
            self.notify.emit("Report² - control needed", f"{job.group_name}: {len(fields['findings'])} finding(s)")
        elif state == J.FAILED:
            self.notify.emit("Processing failed", f"{job.group_name}: {fields['reason']}")
        self.deliver_pending()
        if not self.paused:
            self.pump()                                # the next sample at once
        self.changed.emit()

    def deliver_pending(self) -> None:
        from gcws.automation import export
        for job in self.journal.jobs(states=[J.CONTROL, J.ACCEPTED_AUTO, J.ACCEPTED_MANUAL]):
            if not job.export_pending:
                continue
            wf = self.workflows.get(job.workflow_id) or W.find(job.workflow_id)
            if wf is None:
                continue
            try:
                export.deliver(self.journal, wf, job)
            except Exception as exc:  # noqa: BLE001
                self.journal.update_job(job.id, export_state="error", export_pending=0)
                self.journal.event("error", f"{job.group_name}: delivery failed: {exc}", job_id=job.id)


# -- the process: tray icon and control socket -------------------------------------------------------

class WatcherApp(QObject):
    def __init__(self, core: WatcherCore, tray: bool = True, parent=None):
        super().__init__(parent)
        from PySide6.QtNetwork import QLocalServer
        self.core = core
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._connection)
        self.tray = None
        if tray:
            self._make_tray()
            core.changed.connect(self._update_tray)
            core.notify.connect(self._balloon)

    def listen(self) -> bool:
        from PySide6.QtNetwork import QLocalServer
        name = server_name()
        if not self.server.listen(name):
            QLocalServer.removeServer(name)            # left over from a crash
            return self.server.listen(name)
        return True

    def _connection(self):
        sock = self.server.nextPendingConnection()
        if sock is None:
            return
        sock.readyRead.connect(lambda s=sock: self._command(s))
        sock.disconnected.connect(sock.deleteLater)

    def _command(self, sock):
        while sock.canReadLine():
            try:
                msg = json.loads(bytes(sock.readLine()).decode("utf-8"))
            except ValueError:
                continue
            reply = self.handle(msg.get("cmd", ""))
            sock.write((json.dumps(reply) + "\n").encode("utf-8"))
            sock.flush()

    def handle(self, cmd: str) -> dict:
        c = self.core
        if cmd == "pause":
            c.paused = True
            c.journal.event("info", "Watcher paused")
        elif cmd == "resume":
            c.paused = False
            c.journal.event("info", "Watcher resumed")
            QTimer.singleShot(0, c.tick)
        elif cmd == "scan_now":
            c.scan_now()
        elif cmd == "reload":
            c._stamp.clear()
            QTimer.singleShot(0, c.tick)
        elif cmd == "quit":
            QTimer.singleShot(100, self.quit)
        c.heartbeat()
        return {"ok": True, "state": c.status(), "pid": os.getpid(), "current": (c.current or {}).get("job", ""),
                "workflows": [w.name for w in c.workflows.values()]}

    def quit(self):
        from PySide6.QtWidgets import QApplication
        if self.core.launcher.running():
            self.core.journal.event("warning", "Watcher closed while a job was running; it will be processed again")
            job = (self.core.current or {}).get("job")
            self.core.launcher.kill()
            if job:
                self.core.journal.transition(job, J.PROCESSING, J.QUEUED, reason="the watcher was closed")
        self.core.stop()
        if self.tray is not None:
            self.tray.hide()
        QApplication.instance().quit()

    # -- tray -------------------------------------------------------------------------------------

    def _make_tray(self):
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon
        from gcws.ui.icons import icon
        self.tray = QSystemTrayIcon(icon("watch"), self)
        menu = QMenu()
        self.a_status = QAction("Watcher", menu)
        self.a_status.setEnabled(False)
        menu.addAction(self.a_status)
        menu.addSeparator()
        self.a_pause = QAction("Pause", menu, checkable=True)
        self.a_pause.toggled.connect(lambda on: self.handle("pause" if on else "resume"))
        menu.addAction(self.a_pause)
        menu.addAction("Check the folders now", lambda: self.handle("scan_now"))
        menu.addSeparator()
        menu.addAction("Open GC Workspace (Report²)", self._open_gui)
        menu.addAction("Open the log", lambda: os.startfile(str(paths.logs_dir())))
        self.a_autostart = QAction("Start with Windows", menu, checkable=True)
        from gcws.automation import autostart
        self.a_autostart.setChecked(autostart.is_installed())
        self.a_autostart.toggled.connect(lambda on: autostart.install() if on else autostart.remove())
        menu.addAction(self.a_autostart)
        menu.addSeparator()
        menu.addAction("Quit the watcher", self.quit)
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._open_gui() if reason == QSystemTrayIcon.DoubleClick else None)
        self._update_tray()
        self.tray.show()

    def _update_tray(self):
        if self.tray is None:
            return
        counts = self.core.journal.counts()
        ctl = counts.get(J.CONTROL, 0)
        waiting = counts.get(J.WAITING, 0) + counts.get(J.QUEUED, 0)
        text = (f"GC Workspace Watcher - {self.core.status()}\n{len(self.core.workflows)} workflow(s), "
                f"{waiting} waiting, {ctl} to check")
        self.tray.setToolTip(text)
        self.a_status.setText(text.replace("\n", " · "))

    def _balloon(self, title, text):
        if self.tray is not None:
            self.tray.showMessage(title, text)

    def _open_gui(self):
        QProcess.startDetached(python_exe(), ["-m", "gcws", "--report2"], str(paths.ROOT))


def main(argv) -> int:
    """``--watch``: the watcher process."""
    from logging.handlers import RotatingFileHandler
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication
    paths.initialize()
    handler = RotatingFileHandler(str(paths.logs_dir() / "watcher.log"), maxBytes=2_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    QCoreApplication.setOrganizationName("GCWorkspace")
    QCoreApplication.setApplicationName("GC Workspace Watcher")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(paths.DATA))
    app = QApplication.instance() or QApplication(list(argv))
    app.setQuitOnLastWindowClosed(False)
    from gcws.ui import theme
    theme.apply(app)
    from gcws.automation.control import WatcherControl
    if WatcherControl().send("status", timeout=800) is not None:
        log.info("a watcher is already running for %s", paths.DATA)
        return 0
    core = WatcherCore()
    core.paused = "--paused" in argv
    watcher = WatcherApp(core)
    if not watcher.listen():
        log.error("control socket %s not available", server_name())
    core.start()
    log.info("watcher started for %s", paths.DATA)
    return app.exec()
