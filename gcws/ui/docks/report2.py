"""Report²: the report of the reports.

How many samples the automation processed, which reports were accepted automatically or by the
analyst, and which need control - grouped by batch folder. Selecting a report shows why it needs
control (the findings of the Report² rules), its files and its history; the analyst opens it
(Word, Excel, PDF, or the project in GC Workspace), accepts or rejects it, or has it processed
again. Everything is read from the automation journal every few seconds (the watcher writes it).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
                               QMessageBox, QPushButton, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from gcws.automation import journal as J
from gcws.ui import theme

PERIODS = {"all": ("All", None), "today": ("Today", 1), "week": ("7 days", 7), "month": ("30 days", 30)}
LEVEL = {J.ACCEPTED_AUTO: "ok", J.ACCEPTED_MANUAL: "ok", J.CONTROL: "warn", J.REJECTED: "bad",
         J.FAILED: "bad", J.NOT_PROCESSED: "bad", J.WAITING: "neutral", J.QUEUED: "info", J.PROCESSING: "info",
         J.REMOVED: "neutral"}
FILE_LABELS = {"docx": "Word", "xlsx": "Excel", "pdf": "PDF", "dd": "Double determination",
               "batch_docx": "Batch Word", "batch_pdf": "Batch PDF", "batch_xlsx": "Batch summary"}


class Report2Dock(QWidget):
    openProject = Signal(str)                    # path of a job's .gcws project

    def __init__(self, parent=None, journal: Optional[J.Journal] = None, poll_ms: int = 3000):
        super().__init__(parent)
        self._journal = journal
        self.jobs: dict[str, J.Job] = {}
        self.current: Optional[str] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        # watcher banner
        self.banner = QFrame()
        self.banner.setObjectName("card")
        self.banner.setProperty("level", "warn")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(8, 4, 8, 4)
        self.banner_text = QLabel("The watcher is not running: new data is not processed.")
        bl.addWidget(self.banner_text, 1)
        self.start_btn = QPushButton("Start watcher")
        self.start_btn.clicked.connect(self._start_watcher)
        bl.addWidget(self.start_btn)
        lay.addWidget(self.banner)
        # filters
        f = QHBoxLayout()
        self.workflow = QComboBox()
        self.workflow.addItem("All workflows", "")
        self.workflow.activated.connect(lambda *_: self.refresh())
        self.period = QComboBox()
        for k, (label, _) in PERIODS.items():
            self.period.addItem(label, k)
        self.period.activated.connect(lambda *_: self.refresh())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search sample or batch")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda *_: self.refresh())
        f.addWidget(self.workflow)
        f.addWidget(self.period)
        f.addWidget(self.search, 1)
        rules = QPushButton("Rules...")
        rules.setToolTip("The rules that decide 'Control needed' (default rules and per workflow)")
        rules.clicked.connect(self.edit_rules)
        f.addWidget(rules)
        lay.addLayout(f)
        # counters
        c = QHBoxLayout()
        self.chips: dict[str, QLabel] = {}
        for key, level in (("processed", "info"), ("accepted", "ok"), ("control", "warn"),
                           ("not_processed", "bad"), ("waiting", "neutral"), ("failed", "bad"),
                           ("rejected", "bad")):
            self.chips[key] = theme.chip("", level)
            c.addWidget(self.chips[key])
        c.addStretch(1)
        lay.addLayout(c)
        # the two areas
        areas = QSplitter(Qt.Horizontal)
        self.control = self._tree()
        self.accepted = self._tree()
        box_c = QGroupBox("Control needed")
        QVBoxLayout(box_c).addWidget(self.control)
        box_a = QGroupBox("Accepted")
        QVBoxLayout(box_a).addWidget(self.accepted)
        areas.addWidget(box_c)
        areas.addWidget(box_a)
        self.others = QTabWidget()
        self.pending = self._tree()
        self.problems = self._tree()
        self.others.addTab(self.pending, "Waiting / processing")
        self.others.addTab(self.problems, "Not processed / failed / rejected")
        top = QSplitter(Qt.Vertical)
        top.addWidget(areas)
        top.addWidget(self.others)
        top.setStretchFactor(0, 3)
        # details
        self.detail = QWidget()
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel()
        self.title.setWordWrap(True)
        dl.addWidget(self.title)
        acts = QHBoxLayout()
        self.b_accept = QPushButton("Accept...")
        theme.set_primary(self.b_accept)
        self.b_accept.clicked.connect(lambda: self.review(True))
        self.b_reject = QPushButton("Reject...")
        self.b_reject.clicked.connect(lambda: self.review(False))
        self.b_open = QToolButton()
        self.b_open.setText("Open report")
        self.b_open.setPopupMode(QToolButton.InstantPopup)
        self.b_project = QPushButton("Open in GC Workspace")
        self.b_project.setToolTip("Open the sample as processed (runs, integration, identifications, ISTDs) to "
                                  "check or correct it; then 'Report again'")
        self.b_project.clicked.connect(self.open_project)
        self.b_more = QToolButton()
        self.b_more.setText("More")
        self.b_more.setPopupMode(QToolButton.InstantPopup)
        more = QMenu(self.b_more)
        self.a_rereport = more.addAction("Report again from the (edited) project", lambda: self.reprocess("rereport"))
        self.a_reprocess = more.addAction("Process again from the raw data", lambda: self.reprocess("full"))
        self.a_noblank = more.addAction("Process without a blank...", self.process_without_blank)
        self.a_remove = more.addAction("Remove from the queue...", self.remove_from_queue)
        self.a_remove.setToolTip("The sample cannot be processed: the watcher skips it and the batch report no "
                                 "longer waits for it ('Process again' brings it back)")
        more.addSeparator()
        self.a_folder = more.addAction("Open the job folder", self.open_folder)
        self.a_export = more.addAction("Deliver to the target folders now", self.export_now)
        self.b_more.setMenu(more)
        for b in (self.b_accept, self.b_reject, self.b_open, self.b_project, self.b_more):
            acts.addWidget(b)
        acts.addStretch(1)
        dl.addLayout(acts)
        self.tabs = QTabWidget()
        self.findings = QTableWidget(0, 5)
        self.findings.setHorizontalHeaderLabels(["Rule", "Determination", "Substance", "RT", "Finding"])
        self.findings.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.findings.verticalHeader().setVisible(False)
        self.findings.setEditTriggers(QTableWidget.NoEditTriggers)
        self.files = QTableWidget(0, 3)
        self.files.setHorizontalHeaderLabels(["File", "Where", "Delivered to"])
        self.files.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.files.verticalHeader().setVisible(False)
        self.files.setEditTriggers(QTableWidget.NoEditTriggers)
        self.files.cellDoubleClicked.connect(self._open_file_row)
        self.history = QTableWidget(0, 3)
        self.history.setHorizontalHeaderLabels(["When", "Who", "What"])
        self.history.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.history.verticalHeader().setVisible(False)
        self.history.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tabs.addTab(self.findings, "Findings")
        self.tabs.addTab(self.files, "Files")
        self.tabs.addTab(self.history, "History")
        dl.addWidget(self.tabs, 1)
        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(self.detail)
        main.setStretchFactor(0, 3)
        main.setStretchFactor(1, 2)
        lay.addWidget(main, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(poll_ms)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self._stamp = None
        self._show_detail(None)
        self.refresh()

    # -- data --------------------------------------------------------------------------------------

    @property
    def journal(self) -> J.Journal:
        if self._journal is None:
            self._journal = J.Journal()
        return self._journal

    def _tree(self) -> QTreeWidget:
        t = QTreeWidget()
        t.setColumnCount(4)
        t.setHeaderLabels(["Sample", "Status", "Findings", "Processed"])
        t.header().setSectionResizeMode(0, QHeaderView.Stretch)
        t.setRootIsDecorated(True)
        t.itemSelectionChanged.connect(lambda t=t: self._picked(t))
        t.itemDoubleClicked.connect(lambda *_: self.open_default())
        return t

    def _poll(self):
        if not self.isVisible():
            return
        try:
            n = self.journal.con.execute("SELECT COUNT(*), MAX(COALESCE(finished,0)), MAX(COALESCE(reviewed_at,0)), "
                                         "MAX(COALESCE(created,0)), SUM(LENGTH(state)) FROM jobs").fetchone()
            stamp = tuple(n) + (self.journal.con.execute("SELECT MAX(id) FROM events").fetchone()[0],)
        except Exception:  # noqa: BLE001 - the watcher may be writing
            return
        if stamp != self._stamp:
            self.refresh()
        self._update_banner()

    def _update_banner(self):
        from gcws.automation.control import WatcherControl
        try:
            st = WatcherControl().status(self.journal)
        except Exception:  # noqa: BLE001
            st = {"state": "stopped"}
        state = st.get("state", "stopped")
        self.banner.setVisible(state in ("stopped", "not responding"))
        self.banner_text.setText("The watcher is not running: new data is not processed." if state == "stopped"
                                 else "The watcher does not answer.")

    def _filtered(self) -> list[J.Job]:
        wid = self.workflow.currentData() or None
        days = PERIODS[self.period.currentData() or "all"][1]
        since = time.time() - days * 86400 if days else 0
        text = self.search.text().strip().casefold()
        batches = {b["id"]: b for b in self.journal.batches()}
        out = []
        for j in self.journal.jobs(workflow_id=wid):
            if since and float(j.created or 0) < since:
                continue
            b = batches.get(j.batch_id, {})
            if text and text not in (j.group_name or "").casefold() and text not in (b.get("name") or "").casefold():
                continue
            out.append(j)
        self._batches = batches
        return out

    def refresh(self):
        from gcws.automation import workflow as W
        cur_wf = self.workflow.currentData()
        wfs = W.list_workflows()
        self.workflow.blockSignals(True)
        self.workflow.clear()
        self.workflow.addItem("All workflows", "")
        for w in wfs:
            self.workflow.addItem(w.name, w.id)
        self.workflow.setCurrentIndex(max(0, self.workflow.findData(cur_wf)))
        self.workflow.blockSignals(False)
        jobs = self._filtered()
        self.jobs = {j.id: j for j in jobs}
        samples = [j for j in jobs if not j.is_batch]
        count = lambda *states: sum(1 for j in samples if j.state in states)
        processed = count(*J.DONE)
        auto, manual = count(J.ACCEPTED_AUTO), count(J.ACCEPTED_MANUAL)
        theme.set_chip(self.chips["processed"], f"Processed {processed}", "info")
        theme.set_chip(self.chips["accepted"], f"Accepted {auto + manual} ({auto} automatic, {manual} analyst)", "ok")
        theme.set_chip(self.chips["control"], f"Control needed {count(J.CONTROL)}", "warn" if count(J.CONTROL)
                       else "neutral")
        for key, states, label in (("not_processed", (J.NOT_PROCESSED,), "Not processed"),
                                   ("waiting", (J.WAITING, J.QUEUED, J.PROCESSING), "Waiting"),
                                   ("failed", (J.FAILED,), "Failed"), ("rejected", (J.REJECTED,), "Rejected")):
            n = count(*states)
            theme.set_chip(self.chips[key], f"{label} {n}" if n else "", "bad" if key != "waiting" else "neutral")
        self._fill(self.control, [j for j in jobs if j.state == J.CONTROL])
        self._fill(self.accepted, [j for j in jobs if j.state in J.ACCEPTED])
        self._fill(self.pending, [j for j in jobs if j.state in (J.WAITING, J.QUEUED, J.PROCESSING)])
        self._fill(self.problems, [j for j in jobs if j.state in (J.NOT_PROCESSED, J.FAILED, J.REJECTED, J.REMOVED)])
        self.others.setTabText(1, f"Not processed / failed / rejected / removed "
                                  f"({count(J.NOT_PROCESSED, J.FAILED, J.REJECTED, J.REMOVED)})")
        self.others.setTabText(0, f"Waiting / processing ({count(J.WAITING, J.QUEUED, J.PROCESSING)})")
        try:
            self._stamp = None
            n = self.journal.con.execute("SELECT COUNT(*), MAX(COALESCE(finished,0)), MAX(COALESCE(reviewed_at,0)), "
                                         "MAX(COALESCE(created,0)), SUM(LENGTH(state)) FROM jobs").fetchone()
            self._stamp = tuple(n) + (self.journal.con.execute("SELECT MAX(id) FROM events").fetchone()[0],)
        except Exception:  # noqa: BLE001
            pass
        self._show_detail(self.jobs.get(self.current))

    def _fill(self, tree: QTreeWidget, jobs: list):
        keep = self.current
        tree.blockSignals(True)
        tree.clear()
        by_batch: dict = {}
        for j in jobs:
            by_batch.setdefault(j.batch_id, []).append(j)
        for bid, items in sorted(by_batch.items(), key=lambda kv: -max(float(j.created or 0) for j in kv[1])):
            b = self._batches.get(bid, {})
            top = QTreeWidgetItem([b.get("name", "?"), "", "", ""])
            top.setToolTip(0, b.get("folder", ""))
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            top.setData(0, Qt.UserRole, "")
            tree.addTopLevelItem(top)
            for j in sorted(items, key=lambda j: (j.is_batch, float(j.created or 0))):
                n = len(j.findings or [])
                it = QTreeWidgetItem([j.group_name, J.STATE_LABELS.get(j.state, j.state),
                                      str(n) if n else "", J.when(j.finished or j.created)])
                it.setData(0, Qt.UserRole, j.id)
                it.setBackground(1, theme.status_brush(LEVEL.get(j.state, "neutral")))
                if j.state == J.REMOVED:
                    for c in range(4):
                        it.setForeground(c, theme.status_color("neutral"))
                if j.reason:
                    it.setToolTip(1, j.reason)
                top.addChild(it)
                if j.id == keep:
                    it.setSelected(True)
            top.setExpanded(True)
        tree.blockSignals(False)

    # -- selection and details ----------------------------------------------------------------------

    def _picked(self, tree):
        items = tree.selectedItems()
        jid = items[0].data(0, Qt.UserRole) if items else ""
        if not jid:
            return
        for t in (self.control, self.accepted, self.pending, self.problems):
            if t is not tree:
                t.blockSignals(True)
                t.clearSelection()
                t.blockSignals(False)
        self.select(jid)

    def select(self, job_id: str):
        self.current = job_id
        self._show_detail(self.journal.job(job_id))

    def _show_detail(self, job: Optional[J.Job]):
        self.current = job.id if job is not None else None
        self.detail.setEnabled(job is not None)
        self.findings.setRowCount(0)
        self.files.setRowCount(0)
        self.history.setRowCount(0)
        if job is None:
            self.title.setText("Select a report to see why it needs control, its files and its history.")
            return
        b = self._batches.get(job.batch_id, {}) if hasattr(self, "_batches") else {}
        text = f"<b>{job.group_name}</b> &nbsp; {theme.chip_html(job.label, LEVEL.get(job.state, 'neutral'))}"
        text += f"<br>batch folder {b.get('folder', '')} · revision {job.revision}"
        if job.reviewer:
            text += f"<br>{job.label.lower()} by {job.reviewer}, {J.when(job.reviewed_at)}" + \
                    (f": {job.comment}" if job.comment else "")
        if job.reason:
            text += f"<br>{job.reason}"
        self.title.setText(text)
        for f in job.findings or []:
            r = self.findings.rowCount()
            self.findings.insertRow(r)
            from gcws.automation.rules import RULES
            vals = [RULES.get(f.get("rule"), (f.get("rule", ""),))[0], f.get("member") or "", f.get("substance") or "",
                    f"{f['rt']:.3f}" if f.get("rt") is not None else "", f.get("text") or ""]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 4:
                    it.setBackground(theme.status_brush("warn" if f.get("level") == "control" else "neutral"))
                self.findings.setItem(r, c, it)
        delivered = {}
        for e in self.journal.exports(job.id):
            if e.get("state") == "done" and e.get("fmt") != "register":
                delivered.setdefault((e.get("report_node"), e.get("fmt")), []).append(e.get("dst"))
        menu = QMenu(self.b_open)
        for node, files in (job.files or {}).items():
            for fmt, path in files.items():
                r = self.files.rowCount()
                self.files.insertRow(r)
                self.files.setItem(r, 0, QTableWidgetItem(f"{FILE_LABELS.get(fmt, fmt)}: {Path(path).name}"))
                self.files.setItem(r, 1, QTableWidgetItem(str(Path(path).parent)))
                self.files.setItem(r, 2, QTableWidgetItem("; ".join(delivered.get((node, fmt), []))))
                self.files.item(r, 0).setData(Qt.UserRole, path)
                menu.addAction(FILE_LABELS.get(fmt, fmt), lambda p=path: self.open_path(p))
        self.b_open.setMenu(menu)
        self.b_open.setEnabled(not menu.isEmpty())
        for e in self.journal.events(job_id=job.id):
            r = self.history.rowCount()
            self.history.insertRow(r)
            for c, v in enumerate((J.when(e["ts"]), e.get("user") or "", e.get("text") or "")):
                self.history.setItem(r, c, QTableWidgetItem(v))
        done = job.state in J.DONE
        self.b_accept.setEnabled(job.state in (J.CONTROL, J.REJECTED) or (job.is_batch and done))
        self.b_reject.setEnabled(done and job.state != J.REJECTED)
        self.b_project.setEnabled(bool(job.project_path) and Path(job.project_path).exists())
        self.a_rereport.setEnabled(bool(job.project_path) and not job.is_batch)
        self.a_reprocess.setEnabled(job.state not in (J.QUEUED, J.PROCESSING))
        self.a_noblank.setEnabled(job.state == J.NOT_PROCESSED)
        self.a_remove.setEnabled(job.state in J.REMOVABLE)
        self.a_export.setEnabled(job.state in (J.CONTROL, J.ACCEPTED_AUTO, J.ACCEPTED_MANUAL))
        self.a_folder.setEnabled(bool(job.job_dir))

    # -- actions -------------------------------------------------------------------------------------

    def _job(self) -> Optional[J.Job]:
        return self.journal.job(self.current) if self.current else None

    def review(self, accept: bool, comment: Optional[str] = None) -> bool:
        from gcws.ui.dialogs.report2 import ReviewDialog
        job = self._job()
        if job is None:
            return False
        if comment is None:
            dlg = ReviewDialog(accept, job.group_name, len(job.findings or []), self)
            if not dlg.exec():
                return False
            comment = dlg.text()
        ok = self.journal.review(job.id, accept, comment)
        if not ok:
            QMessageBox.information(self, "Report²", "The report changed meanwhile (processed again?); "
                                    "look at it once more.")
        elif accept and not self._watcher_running():
            self.export_now()
        self.refresh()
        return ok

    def reprocess(self, mode: str = "full", override: Optional[dict] = None) -> bool:
        job = self._job()
        if job is None:
            return False
        ok = self.journal.request(job.id, mode, override)
        if not ok:
            QMessageBox.information(self, "Report²", "This report is already being processed.")
        elif not self._watcher_running():
            self.banner_text.setText("Queued - start the watcher to process it.")
        self.refresh()
        return ok

    def remove_from_queue(self, confirm: bool = True) -> bool:
        job = self._job()
        if job is None or job.state not in J.REMOVABLE:
            return False
        if confirm and QMessageBox.question(
                self, "Remove from the queue", f"Remove '{job.group_name}' from the queue? The watcher skips it "
                "and the batch report no longer waits for it. 'Process again' brings it back.") != QMessageBox.Yes:
            return False
        ok = bool(self.journal.remove([job.id]))
        self.refresh()
        return ok

    def process_without_blank(self, confirm: bool = True) -> bool:
        from gcws.core.audit import current_user
        job = self._job()
        if job is None:
            return False
        if confirm and QMessageBox.question(
                self, "Process without a blank",
                f"{job.group_name} has no blank from its batch folder.\n\nProcess it anyway? The report will need "
                "control ('Processed without a blank').") != QMessageBox.Yes:
            return False
        return self.reprocess("full", {"allow_no_blank": True, "by": current_user()})

    def export_now(self) -> list:
        from gcws.automation import export
        from gcws.automation import workflow as W
        job = self._job()
        if job is None:
            return []
        wf = W.find(job.workflow_id)
        if wf is None:
            return []
        lines = export.deliver(self.journal, wf, job)
        self.refresh()
        return lines

    def open_project(self):
        job = self._job()
        if job is not None and job.project_path:
            self.openProject.emit(job.project_path)

    def open_path(self, path):
        try:
            os.startfile(str(path))
        except OSError as exc:
            QMessageBox.warning(self, "Report²", str(exc))

    def open_default(self):
        job = self._job()
        if job is None:
            return
        for files in (job.files or {}).values():
            for fmt in ("docx", "batch_docx", "pdf", "xlsx"):
                if fmt in files:
                    self.open_path(files[fmt])
                    return

    def _open_file_row(self, row, _col):
        it = self.files.item(row, 0)
        if it is not None:
            self.open_path(it.data(Qt.UserRole))

    def open_folder(self):
        job = self._job()
        if job is not None and job.job_dir:
            self.open_path(job.job_dir)

    def edit_rules(self):
        from gcws.automation import rules as RU
        from gcws.ui.dialogs.report2 import RulesDialog
        dlg = RulesDialog(RU.load_default_rules(), self, allow_default=False)
        if dlg.exec():
            RU.save_default_rules(dlg.rules())

    def _watcher_running(self) -> bool:
        from gcws.automation.control import WatcherControl
        try:
            return WatcherControl().status(self.journal).get("state") not in ("stopped", "not responding")
        except Exception:  # noqa: BLE001
            return False

    def _start_watcher(self):
        from gcws.automation.control import WatcherControl
        WatcherControl().start()
        self.banner_text.setText("Starting the watcher ...")
