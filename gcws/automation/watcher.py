"""GC Workspace Watcher: the background process that watches folders and processes new data.

``python -m gcws --watch`` (or *Automation > Start watcher*). It has a tray icon and keeps
running when the main window is closed. Every enabled workflow's folder is looked at every
*x* minutes; finished samples are processed one at a time in a separate job process (a crash
or a hanging Office program cannot stop the watcher), judged by Report² and delivered to the
target folders. One watcher runs per data folder (a lock held by its process keeps a second one
out, also while it is busy); the GUI sends commands through a local socket and reads everything
else, the watcher's state too, from the journal.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from gcws import paths
from gcws.automation import journal as J
from gcws.automation import localcopy as LC
from gcws.automation import planner as PN
from gcws.automation import runner as RN
from gcws.automation import scanner as SC
from gcws.automation import store
from gcws.automation import workflow as W

log = logging.getLogger("gcws.watcher")

BACKOFF_S = RN.BACKOFF_S
HEARTBEAT_S = 10
_missing_sample_files = RN.missing_sample_files
blank_requirement = RN.blank_requirement


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


# -- launching job processes -----------------------------------------------------------------------

class ProcessLauncher(QObject):
    """One job process at a time (``python -m gcws --process-job spec.json``)."""
    finished = Signal(str, int, str)                 # job id, exit code, output tail

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: Optional[QProcess] = None
        self.job_id = ""
        self._ended: Optional[QProcess] = None       # the process that ended last (deleted at the next start)

    def running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def _drop_ended(self) -> None:
        """A watcher runs for days: a finished process is not kept. Not deleteLater: a launcher closed
        before the event loop ran again (GC Workspace or the watcher quitting) deleted it twice - abort."""
        old, self._ended = self._ended, None
        if old is not None:
            import shiboken6
            try:
                shiboken6.delete(old)
            except RuntimeError:
                pass

    def start(self, job_id: str, spec_path: Path, kind: str = "job") -> None:
        self._drop_ended()
        p = QProcess(self)
        p.setProgram(python_exe())
        flag = {"batch": "--batch-report", "copy": "--copy-runs"}.get(kind, "--process-job")
        p.setArguments(["-m", "gcws", flag, str(spec_path)])
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
        if p.processId():
            store.end_with_this_process(p.processId())  # a watcher that crashes takes its job with it

    def _done(self, p, code, status):
        if p is not self.proc:
            return
        try:
            tail = bytes(p.readAll()).decode("utf-8", "replace")[-2000:]
        except RuntimeError:                       # being deleted: the program quits
            tail = ""
        self.proc, self._ended = None, p
        try:
            self.finished.emit(self.job_id, int(code) if status == QProcess.NormalExit else -1, tail)
        except RuntimeError:                       # the program quits: this launcher is gone already
            pass

    def _failed(self, p, err):
        if err == QProcess.FailedToStart and p is self.proc:
            self.proc, self._ended = None, p
            why = p.errorString()
            self.finished.emit(self.job_id, -2, f"the job process did not start ({why})")

    def kill(self) -> None:
        if self.running():
            self.proc.kill()


# -- the watcher ------------------------------------------------------------------------------------

class WatcherCore(QObject):
    """Scanning, planning, the job queue and delivery. No widgets (the tray is in :class:`WatcherApp`).

    With ``threaded`` the watched folders are looked at in a thread of its own (with its own journal
    connection, like another process writing): a slow network drive then never holds up the control
    socket, the heartbeat or the job queue."""
    changed = Signal()
    notify = Signal(str, str)                        # title, text
    looked = Signal()                                # a look in the looking thread is done

    def __init__(self, journal: Optional[J.Journal] = None, launcher=None, clock=time.time, parent=None,
                 tick_ms: int = 5000, copier=None, threaded: bool = False):
        super().__init__(parent)
        self._main_thread = threading.get_ident()
        self._local = threading.local()
        self.journal = journal or J.Journal()
        self.looks = SC.LookCache()                  # what earlier looks saw (finished runs are not read again)
        self.threaded = threaded
        self.looking = False                         # a look is under way in the looking thread
        self._look_queue: queue.Queue = queue.Queue()
        self._looker: Optional[threading.Thread] = None
        self._copy_lock = threading.Lock()
        self.looked.connect(self.tick)
        self.launcher = launcher or ProcessLauncher(self)
        self.launcher.finished.connect(self._job_finished)
        # the local copy runs in its own process beside the jobs
        self.copier = copier or ProcessLauncher(self)
        self.copier.finished.connect(self._copy_finished)
        self.copying: Optional[dict] = None          # {"batch_id", "workflow_id", "name", "started", "out_dir"}
        self._copy_queue: dict[int, dict] = {}       # batch id -> copy spec
        self._unreachable: set[str] = set()          # workflows whose watched folder cannot be reached
        self.clock = clock
        self.paused = False
        self.workflows: dict[str, W.Workflow] = {}
        self._stamp: dict[str, float] = {}
        self._last_scan: dict[str, float] = {}
        self._scan_now = False
        self._methods: dict[str, dict] = {}
        self._methods_sig: Optional[tuple] = None    # the processing method files the cache was read from
        self.current: Optional[dict] = None          # {"job": id, "started": t, "timeout": s}
        self.started = clock()
        self.timer = QTimer(self)
        self.timer.setInterval(tick_ms)
        self.timer.timeout.connect(self.tick)
        self.hb = QTimer(self)
        self.hb.setInterval(HEARTBEAT_S * 1000)
        self.hb.timeout.connect(self.heartbeat)

    @property
    def journal(self) -> J.Journal:
        """The journal connection of the calling thread (the looking thread has its own)."""
        if threading.get_ident() == self._main_thread:
            return self._journal
        jr = getattr(self._local, "journal", None)
        if jr is None:
            jr = self._local.journal = J.Journal(self._journal.path)
        return jr

    @journal.setter
    def journal(self, value: J.Journal) -> None:
        self._journal = value

    def start(self) -> None:
        self.reload()
        self.heartbeat()
        self.journal.event("info", "Watcher started" + (" (paused)" if self.paused else ""))
        self.recover()
        self.timer.start()
        self.hb.start()
        QTimer.singleShot(0, self.tick)

    def stop(self) -> None:
        self.timer.stop()
        self.hb.stop()
        self.journal.heartbeat("stopped", "", "")
        self.journal.event("info", "Watcher stopped")

    def recover(self) -> list[str]:
        """Samples left "processing" by a watcher (or GC Workspace) that stopped go back to the queue."""
        return self.journal.recover_orphans(keep=[self.current["job"]] if self.current else [])

    def status(self) -> str:
        return "paused" if self.paused else ("processing" if self.current else "running")

    def heartbeat(self) -> None:
        cur = self.current["job"] if self.current else ""
        try:
            self.journal.heartbeat(self.status(), cur, self.copying_text(), self.started)
        except Exception as exc:  # noqa: BLE001 - the journal may be busy for a moment
            log.warning("heartbeat: %s", exc)

    # -- workflows ------------------------------------------------------------------------------

    def reload(self) -> None:
        """Read the workflow files again (only the changed ones)."""
        sig = self._methods_signature()
        if sig != self._methods_sig:
            # a processing method saved, added or deleted: the next jobs take it, and the workflows
            # are checked again (one refused for a missing method starts once it is there)
            self._methods_sig = sig
            self._methods.clear()
            self._stamp.clear()
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
            # the folders are not checked here: a network drive that is not there yet (VPN) is
            # waited for by scan(), not a reason to refuse the workflow
            problems = W.errors(W.validate(wf, method_names=self._method_names(), word=True, check_paths=False,
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

    @staticmethod
    def _methods_signature() -> tuple:
        from gcws.core import proc_method as PM
        try:
            with os.scandir(PM.folder()) as it:
                return tuple(sorted((e.name, st.st_size, st.st_mtime_ns) for e in it if e.name.endswith(".json")
                                    for st in (e.stat(),)))
        except OSError:
            return ()

    def _method_names(self) -> list[str]:
        from gcws.core import proc_method as PM
        return PM.names()

    def method(self, name: str) -> dict:
        # also called from the looking thread while reload() may clear the cache: never read it back
        method = self._methods.get(name)
        if method is None:
            from gcws.core import proc_method as PM
            try:
                method = PM.load(name)
            except KeyError:
                method = {}
            self._methods[name] = method
        return method

    # -- the loop -------------------------------------------------------------------------------------

    def scan_now(self) -> None:
        self._scan_now = True
        QTimer.singleShot(0, self.tick)

    def tick(self) -> None:
        now = self.clock()
        try:
            self.reload()
            if not self.looking:
                due = []
                for wf in list(self.workflows.values()):
                    interval = max(1.0, float(wf.source.p("interval_min") or 5)) * 60
                    if self._scan_now or now - self._last_scan.get(wf.id, 0) >= interval:
                        self._last_scan[wf.id] = now
                        due.append(wf)
                self._scan_now = False
                if due:
                    self.look(due, now)
            self.recover()
            self.deliver_pending()
            self.check_timeout(now)
            if not self.paused:
                self.pump(now)
                self.pump_copies(now)
        except Exception:  # noqa: BLE001 - the watcher keeps going
            import traceback
            log.error(traceback.format_exc())
        self.changed.emit()

    # -- scanning and planning ------------------------------------------------------------------------

    def look(self, workflows: list, now: float) -> None:
        """Look at the watched folders of ``workflows``: at once, or in the looking thread."""
        if not self.threaded:
            for wf in workflows:
                self.scan(wf, now)
            return
        if self._looker is None or not self._looker.is_alive():
            self._looker = threading.Thread(target=self._look_loop, name="gcws-look", daemon=True)
            self._looker.start()
        self.looking = True
        self._look_queue.put((workflows, now))

    def _look_loop(self) -> None:
        while True:
            workflows, now = self._look_queue.get()
            try:
                for wf in workflows:
                    try:
                        self.scan(wf, now)
                    except Exception:  # noqa: BLE001 - the watcher keeps going
                        import traceback
                        log.error(traceback.format_exc())
                    # the interval counts from the end of a look: a slow drive is not looked at nonstop
                    # (0: a finished copy asked for the next look at once)
                    if self._last_scan.get(wf.id, 0):
                        self._last_scan[wf.id] = max(self._last_scan[wf.id], self.clock())
            finally:
                self.looking = False
                self.looked.emit()                     # the main thread starts what became ready

    def scan(self, wf: W.Workflow, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        src = wf.source
        root = src.p("folder")
        cfg = SC.Readiness(int(src.p("stable_scans") or 2), float(src.p("min_age_min") or 0) * 60,
                           float(src.p("quiet_min") or 30) * 60)
        if not os.path.isdir(root):                    # e.g. a network drive without the VPN
            if wf.id not in self._unreachable:
                self._unreachable.add(wf.id)
                self.journal.event("warning", f"Workflow '{wf.name}': the watched folder {root} cannot be reached; "
                                   "looking again at every check", workflow_id=wf.id)
            last = store.read_json(store.listing_path(wf.id)) or {}
            self._write_listing(wf, dict(last, root=root, reachable=False, checked=now))
            self._scan_requested(wf, set(), now, cfg)  # samples added from elsewhere (e.g. this PC) go on
            return
        if wf.id in self._unreachable:
            self._unreachable.discard(wf.id)
            self.journal.event("info", f"Workflow '{wf.name}': the watched folder {root} can be reached again",
                               workflow_id=wf.id)
        first = self.journal.first_scan(wf.id, root)
        census = self.journal.census(wf.id, root)
        depth = int(src.p("depth") or 0)
        known = self.journal.batch_keys(wf.id)
        skipped, seen = [], set()
        folders = SC.batch_folders(root, depth, src.p("pattern") or "*", float(src.p("ignore_older_days") or 0),
                                   now, known=None if census else known, skipped=skipped, seen=seen)
        if census:
            # what is there at the first look and too old to look into is recorded as there before
            # watching; afterwards a folder the journal does not know is new data, however old its files
            for folder, why in skipped:
                if why == "old":
                    self._baseline_folder(wf, folder)
        listing = [{"path": str(f), "name": f.name, "status": why} for f, why in skipped]
        for folder in folders:
            # runs lying loose above the batch folders count since this version: those there already
            # when it first looks are not processed (like everything at a workflow's first look)
            loose = census and SC.folder_key(folder) not in known and len(folder.relative_to(root).parts) < depth
            info = self._scan_batch(wf, folder, now, cfg,
                                    baseline_new=(first and not src.p("process_existing")) or loose)
            listing.append(dict(info or {}, path=str(folder), name=folder.name, status="batch"))
        self._scan_requested(wf, {SC.folder_key(f) for f in folders}, now, cfg)
        self._check_missing(wf, root, seen)
        self._write_listing(wf, {"root": root, "reachable": True, "scanned": now, "checked": now,
                                 "folders": sorted(listing, key=lambda d: d["path"].casefold())})

    def _scan_requested(self, wf: W.Workflow, done: set, now: float, cfg: SC.Readiness) -> None:
        """Batch folders with samples the analyst added to the queue that the look at the watched folder
        did not cover (too old, a name the workflow skips, outside the watched folder), and folders
        outside it until their batch is closed."""
        for b in self.journal.batches(wf.id):
            if b["folder_key"] in done or b.get("deleted"):
                continue
            if b.get("missing") and not self.journal.forced(b):
                continue                               # gone; added to the queue again: looked at once it is back
            if not os.path.isdir(b["folder"]):
                if not b.get("missing") and os.path.isdir(Path(b["folder"]).parent):
                    self._gone(wf, b)                  # deleted (its drive is there): off the queue and the list
                continue
            if self.journal.forced(b) or (b.get("manual") and not J.batch_closed(
                    b, self.journal.jobs(workflow_id=wf.id, batch_id=b["id"]))):
                self._scan_batch(wf, Path(b["folder"]), now, cfg, baseline_new=False)

    def _write_listing(self, wf: W.Workflow, listing: dict) -> None:
        try:
            store.atomic_write_json(store.listing_path(wf.id), dict(listing, workflow_id=wf.id, workflow=wf.name))
        except OSError as exc:
            log.warning("listing of %s not written: %s", wf.name, exc)

    def _baseline_folder(self, wf: W.Workflow, folder: Path) -> None:
        """A batch folder too old to look into at the workflow's first look: its runs (names only, nothing
        is read) are recorded as there before watching, so that only runs added later are processed."""
        from gcws.io import sequence as SQ
        b = self.journal.batch(wf.id, folder)
        runs = self.journal.runs(b["id"])
        try:
            with os.scandir(folder) as it:
                entries = [e for e in it if SC._is_run(e)]
        except OSError:
            return
        for e in entries:
            stem = Path(e.name).stem.casefold()
            if stem not in runs:
                self.journal.upsert_run(b["id"], stem, path=e.path, baseline=1, state="baseline",
                                        role=SQ.classify_role(e.name))
        if not b.get("folder_birth"):
            self.journal.update_batch(b["id"], folder_birth=SC.birth(folder))

    def _check_missing(self, wf: W.Workflow, root, seen: set) -> None:
        """Batch folders that are gone from the watched folder (their parent was listed, they were not):
        marked, so that they are processed again when they are put back."""
        for b in self.journal.batches(wf.id):
            if b.get("missing") or b["folder_key"] in seen:
                continue
            parent = SC.folder_key(Path(b["folder"]).parent)
            if "listed:" + parent not in seen or not store.is_inside(b["folder"], root):
                continue                               # not looked at this time (or not below the folder)
            self._gone(wf, b)

    def _gone(self, wf: W.Workflow, b: dict) -> None:
        self.journal.batch_gone(b["id"])
        if not b.get("deleted"):
            self.journal.event("info", f"{b['name']}: the batch folder was removed; its samples left the queue",
                               workflow_id=wf.id, batch_id=b["id"])

    def _scan_batch(self, wf, folder: Path, now: float, cfg: SC.Readiness, baseline_new: bool) -> None:
        from gcws.io import sequence as SQ
        b = self.journal.batch(wf.id, folder)
        born, recorded = SC.birth(folder), b.get("folder_birth")
        if b.get("missing") or (recorded and born and abs(float(recorded) - born) > 1e-3):
            # removed and put back, or deleted and copied in again: new data, processed again
            self.journal.reset_batch(b["id"])
            self.journal.event("info", f"{b['name']}: the batch folder was put in again; its samples are "
                               "processed again", workflow_id=wf.id, batch_id=b["id"])
            b = self.journal.batch_by_id(b["id"])
        if born and b.get("folder_birth") != born:
            self.journal.update_batch(b["id"], folder_birth=born)
        if b.get("deleted"):
            return {"batch_id": b["id"]}               # deleted in Report²: not looked at any more
        prev = self.journal.runs(b["id"])
        obs = SC.observe(folder, self.looks, now)
        seq = self.looks.sequence(folder, [o.name for o in obs], now)
        changed = any(prev.get(o.stem, {}).get("fingerprint") != o.fingerprint for o in obs)
        last_change = now if changed or not b.get("last_change") else float(b["last_change"])
        quiet = now - last_change >= cfg.quiet_s
        order = [s for s in seq.stems] + sorted((o.stem for o in obs if o.stem not in seq.stems),
                                               key=lambda s: SQ.order_key(s))
        present_stems = {o.stem for o in obs}
        present = {}
        readded, unchanged = set(), set()
        for o in obs:
            i = order.index(o.stem) if o.stem in order else -1
            successor = i >= 0 and any(s in present_stems for s in order[i + 1:])
            state, count = SC.readiness(prev.get(o.stem), o, now, cfg, successor_started=successor,
                                        seq_finished=seq.finished, folder_quiet=quiet)
            row = prev.get(o.stem)
            fields = {"path": str(o.path), "fingerprint": o.fingerprint, "stable_count": count, "state": state,
                      "marker": int(o.marker), "role": SQ.classify_role(o.name), "gone": None}
            if row is None or row.get("fingerprint") != o.fingerprint:
                fields["last_change"] = now
            if row is None and baseline_new:
                fields["baseline"] = 1
            if o.birth is not None:
                fields["birth"] = o.birth
                if row is not None and row.get("birth") and row.get("state") == "ready" and \
                        abs(float(row["birth"]) - o.birth) > 1e-3:
                    # a finished run deleted and copied in again: its sample is processed again
                    fields.update(readded=int(row.get("readded") or 0) + 1, baseline=0)
                    readded.add(o.name)
                    self.journal.event("info", f"{b['name']}: {o.name} was put in again; its sample is processed "
                                       "again", workflow_id=wf.id, batch_id=b["id"])
            if row is None or any(row.get(k) != v for k, v in fields.items()):
                self.journal.upsert_run(b["id"], o.stem, **fields)     # written only when something changed
            present[o.stem] = {"name": o.name, "ready": state == "ready"}
            if not o.busy and row is not None and row.get("fingerprint") == o.fingerprint:
                unchanged.add(o.stem)
        if obs or os.path.isdir(folder):
            # runs deleted from the folder: kept in the journal (one put in again is recognised), not listed
            for stem in set(prev) - present_stems:
                if not prev[stem].get("gone"):
                    self.journal.upsert_run(b["id"], stem, gone=now)
        newest = max((o.mtime for o in obs), default=0.0)
        if not quiet and obs and not changed and all(v["ready"] for v in present.values()) and \
                now - newest >= cfg.quiet_s:
            quiet = True                               # copied or moved in: nothing written for the quiet time
        if readded:
            self.journal.put_in_again([j.id for j in self.journal.jobs(workflow_id=wf.id, batch_id=b["id"])
                                       if set(j.members or []) & readded])
        runs = self.journal.runs(b["id"])
        baseline = {s for s, r in runs.items() if r.get("baseline")}
        looked = {"last_change": last_change, "has_log": int(bool(seq.lines)), "seq_completed": int(seq.finished)}
        if any(b.get(k) != v for k, v in looked.items()):
            self.journal.update_batch(b["id"], **looked)
        fps = {s: (r.get("fingerprint") or "") + (f"#{r['readded']}" if r.get("readded") else "")
               for s, r in runs.items()}
        names = {s: r.get("path") and Path(r["path"]).name for s, r in runs.items()}
        # a method fed by the local copy sees a run once it is copied
        copied = {s for s, r in runs.items() if r.get("copied_fp") and r.get("copied_fp") == r.get("fingerprint")}
        local_present = {s: dict(v, ready=v["ready"] and s in copied, copying=v["ready"] and s not in copied)
                         for s, v in present.items()}
        needed = set()                                 # runs a sample processed from the copy needs
        force = self.journal.forced(b)                 # run stems (or "*") the analyst added to the queue
        pending = set()
        # runs deleted from the folder (kept in the journal): a processed sample of theirs keeps its report
        gone = {Path(r.get("path") or s).name.casefold() for s, r in runs.items() if r.get("gone")}
        for m in wf.methods():
            method = self.method(m.p("method"))
            via, edges = wf.feed(m.id)
            plan = PN.plan_batch(present if via is None else local_present, seq, quiet=quiet,
                                 require=blank_requirement(m, method), baseline=baseline)
            forced = {}
            if force:
                # a run counts as finished when it did not change since the last look
                fpres = {s: dict(v, ready=v["ready"] or s in unchanged) for s, v in present.items()}
                if via is not None:
                    fpres = {s: dict(v, ready=v["ready"] and s in copied, copying=v["ready"] and s not in copied)
                             for s, v in fpres.items()}
                fplan = PN.plan_batch(fpres, seq, quiet=True, require=blank_requirement(m, method))
                forced = {g.key: g for g in fplan.groups if "*" in force or set(g.members) & force}
            groups = [forced.get(g.key, g) for g in plan.groups if not b.get("manual") or g.key in forced]
            groups += [g for k, g in forced.items() if k not in {x.key for x in plan.groups}]
            for g in groups:
                if g.state == PN.BASELINE:
                    continue
                if not all(W.passes(e.filter, {"name": g.name, "batch": folder.name}) for e in edges):
                    continue
                if gone and g.key not in forced:
                    cur = self.journal.find_job(wf.id, m.id, b["id"], g.key)
                    used = list(cur.members or []) + [x for v in (cur.blanks or {}).values() for x in v] \
                        if cur is not None else []
                    if cur is not None and cur.state in J.DONE and {str(x).casefold() for x in used} & gone:
                        # a run or blank of it was deleted (or moved away): its report and decision stay;
                        # put back unchanged nothing happens, copied in anew it is processed again
                        continue
                if via is not None:
                    needed.update(g.members, *g.blanks.values())
                blanks = {k: [names.get(s) or s for s in v if s in names] for k, v in g.blanks.items()}
                members = [names.get(s) or s for s in g.members]
                fp = hashlib.sha1(json.dumps([[fps.get(s, "") for s in g.members],
                                              [fps.get(s, "") for v in g.blanks.values() for s in v]]).encode()
                                  ).hexdigest()[:16]
                job = self.journal.ensure_job(wf.id, m.id, b["id"], g.key, g.name, members, blanks, fp,
                                              reason=g.reason)
                if g.key in forced:
                    if job.state == J.REMOVED:             # removed from the queue after it was added
                        continue
                    if g.state == PN.WAITING:              # a run still being written or copied
                        pending.update(g.members)
                        self.journal.update_job(job.id, reason="added to the queue; " + g.reason)
                    else:
                        self.journal.enqueue(job.id)
                    continue
                if job.state == J.WAITING:
                    if g.state == PN.READY:
                        self.journal.transition(job.id, J.WAITING, J.QUEUED, queued_at=now, reason="")
                    elif g.state == PN.NOT_PROCESSED:
                        if self.journal.transition(job.id, J.WAITING, J.NOT_PROCESSED, reason=g.reason):
                            self.journal.event("warning", f"{g.name}: not processed - {g.reason}", job_id=job.id,
                                               workflow_id=wf.id, batch_id=b["id"])
                            self.notify.emit("Not processed", f"{g.name}: {g.reason}")
                    elif (job.reason or "") != (g.reason or ""):
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
            if b.get("manual"):                        # outside the watched folder: only what was added
                complete = not pending and all(j.state not in (J.WAITING, J.QUEUED, J.PROCESSING) for j in
                                               self.journal.jobs(workflow_id=wf.id, batch_id=b["id"],
                                                                 include_batch=False) if j.method_node == m.id)
            plan_json = json.dumps({"complete": complete, "groups": [
                {"key": g.key, "name": g.name, "state": "removed" if g.key in removed else g.state,
                 "reason": g.reason} for g in plan.groups]}, default=str)
            if plan_json != b.get("plan_json"):
                self.journal.update_batch(b["id"], plan_json=plan_json)
                b["plan_json"] = plan_json
            if complete:
                self._batch_report(wf, m, b, folder)
        if force:
            left = ({"*"} if pending else set()) if "*" in force else force & pending
            if left != force:
                self.journal.update_batch(b["id"], force_json=json.dumps(sorted(left)) if left else None)
        if wf.copy_step is not None:
            self._plan_copy(wf, wf.copy_step, b, folder, runs, present_stems, needed)
        return {"batch_id": b["id"], "has_log": bool(seq.lines), "quiet": quiet,
                "quiet_in": 0 if quiet else max(0.0, cfg.quiet_s - (now - last_change)),
                "other": self._other_files(folder)}

    def _other_files(self, folder: Path, limit: int = 40) -> list[str]:
        names = self.looks.other_names(folder)
        if names is None:
            return SC.other_files(folder, limit)
        return names[:limit] + ([f"... {len(names) - limit} more"] if len(names) > limit else [])

    # -- the local copy --------------------------------------------------------------------------------

    def _plan_copy(self, wf: W.Workflow, cp: W.Node, b: dict, folder: Path, runs: dict, present: set,
                   needed: set) -> None:
        """Queues a copy pass of the batch folder when finished runs, or the files beside them, are not
        yet in the local folder. Runs that were there before watching started are copied only when a
        sample needs them."""
        if self.copying is not None and self.copying["batch_id"] == b["id"]:
            return                                     # being copied: looked at again afterwards
        if not all(W.passes(e.filter, {"batch": folder.name}) for e in wf.incoming(cp.id)):
            return
        todo = [s for s in sorted(present) if runs[s].get("state") == "ready"
                and runs[s].get("copied_fp") != runs[s].get("fingerprint")
                and (not runs[s].get("baseline") or s in needed)]
        has_copies = any(runs[s].get("copied_fp") for s in present)
        extras = LC.extras_signature(folder)
        if not todo and (not has_copies or extras == (b.get("copied_extras") or "")):
            with self._copy_lock:
                self._copy_queue.pop(b["id"], None)
            return
        dst = LC.local_batch(cp.p("folder"), wf.source.p("folder"), folder)
        if b.get("local_folder") != str(dst):
            self.journal.update_batch(b["id"], local_folder=str(dst))
        spec = {"batch_id": b["id"], "workflow_id": wf.id, "name": b.get("name") or folder.name, "src": str(folder),
                "dst": str(dst), "extras": True,
                "runs": [{"stem": s, "name": Path(runs[s]["path"]).name, "fingerprint": runs[s]["fingerprint"]}
                         for s in todo]}
        with self._copy_lock:
            self._copy_queue[b["id"]] = spec

    def pump_copies(self, now: Optional[float] = None) -> None:
        """Starts the next copy pass (one at a time, beside the job process)."""
        now = self.clock() if now is None else now
        with self._copy_lock:
            if self.copying is not None or self.copier.running() or not self._copy_queue:
                return
            bid = next(iter(self._copy_queue))
            spec = self._copy_queue.pop(bid)
        out = store.root() / "copies" / f"batch{bid}"
        try:
            (out / "result.json").unlink()
        except OSError:
            pass
        spec_path = store.atomic_write_json(out / "spec.json", dict(spec, out_dir=str(out)))
        self.copying = {"batch_id": bid, "workflow_id": spec["workflow_id"], "name": spec["name"],
                        "dst": spec["dst"], "runs": [r["stem"] for r in spec["runs"]], "started": now,
                        "out_dir": str(out)}
        self.heartbeat()
        self.copier.start(f"copy-{bid}", spec_path, "copy")

    def copying_text(self) -> str:
        return f"copying {self.copying['name']} to the local folder" if self.copying else ""

    def _copy_finished(self, _id: str, code: int, tail: str) -> None:
        cur, self.copying = self.copying or {}, None
        bid = cur.get("batch_id")
        if bid is None:
            return
        res = store.read_json(Path(cur["out_dir"]) / "result.json")
        if not isinstance(res, dict):
            last = tail.strip().splitlines()[-1] if tail.strip() else ""
            why = "the copy took too long" if cur.get("timed_out") else \
                "the copy process ended without a result" + (f": {last}" if last else "")
            res = {"copied": {}, "failed": {s: why for s in cur.get("runs") or []}}
        runs = self.journal.runs(bid)
        for stem, fp in (res.get("copied") or {}).items():
            if stem in runs:
                self.journal.upsert_run(bid, stem, copied_fp=fp, copy_error="")
        for stem, why in (res.get("failed") or {}).items():
            if stem not in runs:
                continue
            if (runs[stem].get("copy_error") or "") != why:      # each new problem is logged once
                self.journal.event("warning", f"{cur['name']}: {Path(runs[stem]['path']).name} could not be copied "
                                   f"to the local folder - {why}", workflow_id=cur["workflow_id"], batch_id=bid)
            self.journal.upsert_run(bid, stem, copy_error=why)
        if res.get("extras") is not None:
            self.journal.update_batch(bid, copied_extras=res["extras"])
        n = len(res.get("copied") or {})
        if n:
            self.journal.event("info", f"{cur['name']}: {n} run(s) copied to {cur['dst']} "
                               f"({(res.get('bytes') or 0) / 1e6:.1f} MB, {res.get('seconds') or 0:.0f} s)",
                               workflow_id=cur["workflow_id"], batch_id=bid)
            self._last_scan[cur["workflow_id"]] = 0     # plan again at the next tick
        self.heartbeat()
        self.changed.emit()

    # -- the batch report ------------------------------------------------------------------------------

    def _batch_report(self, wf: W.Workflow, m: W.Node, b: dict, folder: Path) -> None:
        reports = [r for r, _ in wf.report_nodes(m.id) if set(r.p("formats") or []) & set(W.BATCH_FORMATS)]
        if not reports:
            return
        # a report deleted in Report² (hidden) can no longer be decided: it does not hold the batch up
        jobs = [j for j in self.journal.jobs(workflow_id=wf.id, batch_id=b["id"], include_batch=False)
                if j.method_node == m.id and j.state != J.REMOVED and not j.deleted]
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
        if self.current and not self.current.get("timed_out") and now - self.current["started"] > self.current["timeout"]:
            self.journal.event("error", f"Job {self.current['job']} stopped after "
                               f"{self.current['timeout'] / 60:.0f} min", job_id=self.current["job"])
            self.current["timed_out"] = True
            self.launcher.kill()
        if self.copying and not self.copying.get("timed_out") and now - self.copying["started"] > LC.TIMEOUT_S:
            self.journal.event("error", f"Copying {self.copying['name']} stopped after {LC.TIMEOUT_S // 60} min",
                               workflow_id=self.copying["workflow_id"], batch_id=self.copying["batch_id"])
            self.copying["timed_out"] = True
            self.copier.kill()

    def pump(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        if self.current is not None or self.launcher.running():
            return
        for job in self.journal.jobs(states=[J.QUEUED]):
            if float(job.not_before or 0) > now:
                continue
            wf = self.workflows.get(job.workflow_id)
            if wf is None or wf.node(job.method_node) is None:
                if job.review_pending:
                    self._cannot_start_review(job, "workflow or method is unavailable", now)
                continue
            try:
                spec, kind = self.spec_for(wf, job)
            except Exception as exc:  # noqa: BLE001
                if job.review_pending:
                    self._cannot_start_review(job, str(exc), now)
                else:
                    self.journal.transition(job.id, J.QUEUED, J.PROCESSING)
                    self.journal.transition(job.id, J.PROCESSING, J.FAILED, reason=f"cannot start: {exc}",
                                            finished=now)
                continue
            spec_path = store.atomic_write_json(Path(spec["out_dir"]) / "spec.json", spec)
            if not self.journal.transition(job.id, J.QUEUED, J.PROCESSING, started=now, job_dir=spec["out_dir"],
                                           reason="", pid=os.getpid()):
                continue
            RN.clear_result(spec["out_dir"])
            timeout = float(wf.node(job.method_node).p("timeout_min") or 30) * 60
            self.current = {"job": job.id, "started": now, "timeout": timeout, "kind": kind,
                            "out_dir": spec["out_dir"]}
            self.journal.event("info", f"{job.group_name}: processing", job_id=job.id, workflow_id=wf.id,
                               batch_id=job.batch_id)
            self.heartbeat()
            self.launcher.start(job.id, spec_path, kind)
            return

    def _cannot_start_review(self, job: J.Job, reason: str, now: float) -> None:
        RN.cannot_start(self.journal, job, reason, now)

    def spec_for(self, wf: W.Workflow, job: J.Job) -> tuple[dict, str]:
        return RN.job_spec(self.journal, wf, job, self.method)

    def _job_finished(self, job_id: str, code: int, tail: str) -> None:
        cur, self.current = self.current or {}, None
        job = self.journal.job(job_id)
        if job is None:
            return
        out = RN.finish(self.journal, job_id, Path(cur.get("out_dir") or job.job_dir or ""), tail,
                        bool(cur.get("timed_out")), self.clock())
        if out is None or out["retry"]:
            return
        if out["state"] == J.CONTROL and not job.is_batch:
            self.notify.emit("Report² - control needed", f"{job.group_name}: {len(out['findings'])} finding(s)")
        elif out["state"] == J.FAILED:
            self.notify.emit("Processing failed", f"{job.group_name}: {out['reason']}")
        self.deliver_pending()
        if not self.paused:
            self.pump()                                # the next sample at once
        self.changed.emit()

    def deliver_pending(self) -> None:
        from gcws.automation import export
        now = self.clock()
        for job in self.journal.to_deliver():
            if float(job.deliver_after or 0) > now:
                continue                               # nothing to deliver, or the analyst may still undo
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
                "copying": c.copying_text(), "workflows": [w.name for w in c.workflows.values()]}

    def quit(self):
        from PySide6.QtWidgets import QApplication
        # what ends now is not taken in (a job killed here is no failure) and starts nothing new
        for launcher, slot in ((self.core.launcher, self.core._job_finished),
                               (self.core.copier, self.core._copy_finished)):
            try:
                launcher.finished.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        if self.core.copier.running():
            self.core.copier.kill()                    # copied again at the next start
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
    # one watcher per data folder: the lock lives as long as this process, also while it is busy
    from gcws.automation.control import WatcherControl
    if WatcherControl().running() or not store.take_lock(server_name()):
        log.info("a watcher is already running for %s", paths.DATA)
        return 0
    QCoreApplication.setOrganizationName("GCWorkspace")
    QCoreApplication.setApplicationName("GC Workspace Watcher")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(paths.DATA))
    app = QApplication.instance() or QApplication(list(argv))
    app.setQuitOnLastWindowClosed(False)
    from gcws.ui import theme
    theme.apply(app)
    core = WatcherCore(threaded=True)
    core.paused = "--paused" in argv
    watcher = WatcherApp(core)
    if not watcher.listen():
        log.error("control socket %s not available", server_name())
    core.start()
    log.info("watcher started for %s", paths.DATA)
    return app.exec()
