"""Automation panel: the workflows (folder -> method -> Report² -> report -> folder), the watcher
that runs them in the background, and what it did last.

The chart of a workflow is edited in its own window (:class:`gcws.ui.automation.editor.WorkflowEditor`).
The watcher is a separate process; this panel starts, pauses and stops it and shows its state
from the automation journal, which it reads every few seconds while visible.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMenu,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from gcws.automation import journal as J
from gcws.automation import templates
from gcws.automation import workflow as W
from gcws.ui import theme

STATE_LEVEL = {"running": "ok", "processing": "info", "paused": "warn", "stopped": "neutral",
               "starting": "info", "not responding": "bad"}


class AutomationDock(QWidget):
    showReport2 = Signal()
    showJob = Signal(str)                        # a Report² job (Folders tab: Show in Report²)

    def __init__(self, parent=None, journal: Optional[J.Journal] = None, control=None, poll_ms: int = 3000):
        super().__init__(parent)
        self._journal = journal
        self._control = control
        self.editors: dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        # the watcher
        box = QGroupBox("Watcher (background processing)")
        bl = QVBoxLayout(box)
        row = QHBoxLayout()
        self.state = theme.chip("stopped", "neutral")
        self.state_text = QLabel()
        self.state_text.setObjectName("hint")
        row.addWidget(self.state)
        row.addWidget(self.state_text, 1)
        bl.addLayout(row)
        row = QHBoxLayout()
        self.b_start = QPushButton("Start")
        theme.set_primary(self.b_start)
        self.b_start.clicked.connect(self.start_watcher)
        self.b_pause = QPushButton("Pause")
        self.b_pause.clicked.connect(self.toggle_pause)
        self.b_scan = QPushButton("Check now")
        self.b_scan.clicked.connect(lambda: self._send("scan_now"))
        self.b_stop = QPushButton("Stop")
        self.b_stop.clicked.connect(self.stop_watcher)
        for b in (self.b_start, self.b_pause, self.b_scan, self.b_stop):
            row.addWidget(b)
        row.addStretch(1)
        self.autostart = QCheckBox("Start with Windows")
        self.autostart.setToolTip("A shortcut in your Startup folder starts the watcher when you log on")
        self.autostart.toggled.connect(self._autostart)
        row.addWidget(self.autostart)
        self.with_app = QCheckBox("Start with GC Workspace")
        self.with_app.setToolTip("Start the watcher when GC Workspace starts and a workflow is active")
        self.with_app.setChecked(QSettings().value("automation/start_with_app", True, type=bool))
        self.with_app.toggled.connect(lambda on: QSettings().setValue("automation/start_with_app", on))
        row.addWidget(self.with_app)
        bl.addLayout(row)
        bl.addWidget(theme.hint("The watcher keeps running when GC Workspace is closed (tray icon). It checks "
                                "the watched folders, processes every finished sample and hands the reports "
                                "to Report²."))
        lay.addWidget(box)
        # workflows, folders, queue and activity: one tab each
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(lambda *_: self._refresh_tab())
        lay.addWidget(self.tabs, 1)
        box = QWidget()
        wl = QVBoxLayout(box)
        wl.setContentsMargins(0, 4, 0, 0)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Active", "Name", "Watched folder", "Every", "Waiting", "To check"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        hh = self.table.horizontalHeader()
        for c in range(6):
            hh.setSectionResizeMode(c, QHeaderView.Stretch if c == 2 else QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(lambda r, c: self.edit() if c else None)
        self.table.itemChanged.connect(self._active_changed)
        wl.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.b_new = QToolButton()
        self.b_new.setText("New")
        self.b_new.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.b_new)
        for key, label in templates.TEMPLATES.items():
            menu.addAction(label, lambda k=key: self.new(k))
        self.b_new.setMenu(menu)
        row.addWidget(self.b_new)
        for label, fn in (("Edit chart...", self.edit), ("Duplicate", self.duplicate), ("Delete", self.delete),
                          ("Import...", self.import_), ("Export...", self.export)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        r2 = QPushButton("Report²")
        r2.clicked.connect(self.showReport2.emit)
        row.addWidget(r2)
        wl.addLayout(row)
        self.tabs.addTab(box, "Workflows")
        # what is in the watched folders and their local copies
        from gcws.ui.automation.folders import FoldersView
        self.folders = FoldersView(lambda: self.journal)
        self.folders.showJob.connect(self.showJob.emit)
        self.folders.addToQueue.connect(self.add_to_queue)
        self.tabs.addTab(self.folders, "Folders")
        # the queue: samples not processed yet, or whose processing could not finish
        box = QWidget()
        ql = QVBoxLayout(box)
        ql.setContentsMargins(0, 4, 0, 0)
        self.queue = QTableWidget(0, 5)
        self.queue.setHorizontalHeaderLabels(["Sample", "Batch", "Workflow", "State", "Why"])
        self.queue.verticalHeader().setVisible(False)
        self.queue.setSelectionBehavior(QTableWidget.SelectRows)
        self.queue.setSelectionMode(QTableWidget.ExtendedSelection)
        self.queue.setEditTriggers(QTableWidget.NoEditTriggers)
        self.queue.setWordWrap(False)
        qh = self.queue.horizontalHeader()
        for c in range(5):
            qh.setSectionResizeMode(c, QHeaderView.Stretch if c == 4 else QHeaderView.ResizeToContents)
        self.queue.itemSelectionChanged.connect(self._queue_buttons)
        ql.addWidget(self.queue, 1)
        row = QHBoxLayout()
        self.b_remove = QPushButton("Remove from queue")
        self.b_remove.setToolTip("Samples that cannot be processed: the watcher skips them and the batch report "
                                 "no longer waits for them ('Process again' brings them back)")
        self.b_remove.clicked.connect(lambda: self.remove_from_queue())
        self.b_again = QPushButton("Process again")
        self.b_again.clicked.connect(self.process_again)
        self.show_removed = QCheckBox("Show removed")
        self.show_removed.toggled.connect(lambda _on: self._refresh_queue())
        self.b_add = QPushButton("Add samples...")
        self.b_add.setToolTip("Choose a batch folder and the samples the watcher should process now")
        self.b_add.clicked.connect(lambda: self.add_samples())
        for w in (self.b_add, self.b_remove, self.b_again):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.show_removed)
        ql.addLayout(row)
        self._queue_page = box
        self.tabs.addTab(box, "Queue")
        # activity
        box = QWidget()
        al = QVBoxLayout(box)
        al.setContentsMargins(0, 4, 0, 0)
        self.log = QTableWidget(0, 3)
        self.log.setHorizontalHeaderLabels(["When", "", "What"])
        self.log.verticalHeader().setVisible(False)
        self.log.setEditTriggers(QTableWidget.NoEditTriggers)
        self.log.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        al.addWidget(self.log)
        self.tabs.addTab(box, "Activity")
        self.timer = QTimer(self)
        self.timer.setInterval(poll_ms)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self.status = {"state": "stopped"}
        self.refresh()

    # -- data ---------------------------------------------------------------------------------------

    @property
    def journal(self) -> J.Journal:
        if self._journal is None:
            self._journal = J.Journal()
        return self._journal

    @property
    def control(self):
        if self._control is None:
            from gcws.automation.control import WatcherControl
            self._control = WatcherControl()
        return self._control

    def _poll(self):
        if self.isVisible():
            self.refresh()

    def refresh(self):
        self._refresh_status()
        self._refresh_workflows()
        self._refresh_queue()
        self._refresh_log()
        self._refresh_tab()

    def _refresh_tab(self):
        """The Folders tab is built only while it is shown."""
        folders = getattr(self, "folders", None)
        if folders is not None and self.tabs.currentWidget() is folders:
            folders.refresh()

    def _refresh_status(self):
        try:
            self.status = self.control.status(self.journal)
        except Exception:  # noqa: BLE001
            self.status = {"state": "stopped"}
        state = self.status.get("state", "stopped")
        theme.set_chip(self.state, state, STATE_LEVEL.get(state, "neutral"))
        cur = self.status.get("current") or ""
        job = self.journal.job(cur) if cur else None
        hb = self.status.get("heartbeat")
        text = "; ".join(t for t in (f"processing {job.group_name}" if job else "",
                                     self.status.get("copying") or "") if t)
        if hb is not None and state != "stopped":
            text += ("; " if text else "") + f"last sign of life {hb:.0f} s ago"
        self.state_text.setText(text)
        running = state not in ("stopped", "not responding")
        self.b_start.setEnabled(not running)
        # a watcher that hangs is replaced, never joined by a second one
        self.b_start.setText("Restart" if state == "not responding" else "Start")
        self.b_pause.setEnabled(running)
        self.b_pause.setText("Resume" if state == "paused" else "Pause")
        self.b_scan.setEnabled(running)
        self.b_stop.setEnabled(running)
        from gcws.automation import autostart
        self.autostart.blockSignals(True)
        self.autostart.setChecked(autostart.is_installed())
        self.autostart.blockSignals(False)

    def _refresh_workflows(self):
        keep = self.selected_id()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for wf in W.list_workflows():
            counts = self.journal.counts(wf.id)
            r = self.table.rowCount()
            self.table.insertRow(r)
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            on.setCheckState(Qt.Checked if wf.enabled else Qt.Unchecked)
            on.setData(Qt.UserRole, wf.id)
            self.table.setItem(r, 0, on)
            src = wf.source
            vals = [wf.name, src.p("folder") if src else "", f"{src.p('interval_min')} min" if src else "",
                    str(counts.get(J.WAITING, 0) + counts.get(J.QUEUED, 0) + counts.get(J.PROCESSING, 0)),
                    str(counts.get(J.CONTROL, 0))]
            for c, v in enumerate(vals, 1):
                it = QTableWidgetItem(v)
                if c == 5 and counts.get(J.CONTROL):
                    it.setBackground(theme.status_brush("warn"))
                self.table.setItem(r, c, it)
            issues = W.errors(W.validate(wf, check_paths=False))
            if issues:
                self.table.item(r, 1).setToolTip("\n".join(i.text for i in issues))
                self.table.item(r, 1).setForeground(theme.status_color("bad"))
            if wf.id == keep:
                self.table.selectRow(r)
        self.table.blockSignals(False)

    def _refresh_queue(self):
        states = list(J.QUEUE) + ([J.REMOVED] if self.show_removed.isChecked() else [])
        try:
            jobs = self.journal.jobs(states=states)
            batches = {b["id"]: b for b in self.journal.batches()}
        except Exception:  # noqa: BLE001
            return
        keep = set(self.queue_selection())
        names = {wf.id: wf.name for wf in W.list_workflows()}
        self.queue.blockSignals(True)
        self.queue.setRowCount(0)
        level = {J.WAITING: "neutral", J.QUEUED: "info", J.PROCESSING: "info", J.FAILED: "bad",
                 J.NOT_PROCESSED: "bad", J.REMOVED: "neutral"}
        for j in jobs:
            r = self.queue.rowCount()
            self.queue.insertRow(r)
            vals = [j.group_name, batches.get(j.batch_id, {}).get("name", ""), names.get(j.workflow_id, ""),
                    j.label, j.reason or ""]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, j.id)
                if c == 4:
                    it.setToolTip(v)
                if j.state == J.REMOVED:
                    it.setForeground(theme.status_color("neutral"))
                self.queue.setItem(r, c, it)
            self.queue.item(r, 3).setBackground(theme.status_brush(level.get(j.state, "neutral")))
            if j.id in keep:
                self.queue.selectRow(r)
        # samples added by hand that the watcher has not taken up yet
        requests = [b for b in batches.values() if self.journal.forced(b) and not b.get("deleted")]
        stopped = self.status.get("state") in ("stopped", "not responding")
        for b in requests:
            r = self.queue.rowCount()
            self.queue.insertRow(r)
            force = sorted(self.journal.forced(b))
            vals = ["all samples" if "*" in force else ", ".join(force), b.get("name", ""),
                    names.get(b.get("workflow_id"), ""), "Requested",
                    "added by you; processed at the watcher's next look" + (" - start the watcher" if stopped else "")]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setToolTip(v)
                self.queue.setItem(r, c, it)
            self.queue.item(r, 3).setBackground(theme.status_brush("info"))
        self.queue.blockSignals(False)
        n = sum(1 for j in jobs if j.state != J.REMOVED) + len(requests)
        self.tabs.setTabText(self.tabs.indexOf(self._queue_page), f"Queue ({n})" if n else "Queue")
        self._queue_buttons()

    def queue_selection(self) -> list[str]:
        model = self.queue.selectionModel()
        rows = sorted({i.row() for i in model.selectedRows()}) if model else []
        return [self.queue.item(r, 0).data(Qt.UserRole) for r in rows if self.queue.item(r, 0)]

    def _queue_jobs(self) -> list:
        return [j for j in (self.journal.job(i) for i in self.queue_selection()) if j is not None]

    def _queue_buttons(self):
        jobs = self._queue_jobs() if self.queue.rowCount() else []
        self.b_remove.setEnabled(any(j.state in J.REMOVABLE for j in jobs))
        self.b_again.setEnabled(any(j.state not in (J.QUEUED, J.PROCESSING) for j in jobs))

    def remove_from_queue(self, confirm: bool = True) -> list[str]:
        jobs = [j for j in self._queue_jobs() if j.state in J.REMOVABLE]
        if not jobs:
            return []
        names = ", ".join(j.group_name for j in jobs[:5]) + (f" and {len(jobs) - 5} more" if len(jobs) > 5 else "")
        if confirm and QMessageBox.question(
                self, "Remove from queue", f"Remove {names} from the queue? The watcher skips "
                f"{'it' if len(jobs) == 1 else 'them'} and the batch report no longer waits. "
                "'Process again' brings them back.") != QMessageBox.Yes:
            return []
        done = self.journal.remove([j.id for j in jobs])
        self.control.send("scan_now", 500)
        self.refresh()
        return done

    # -- adding samples by hand ------------------------------------------------------------------------

    def add_samples(self, dialog=None) -> Optional[dict]:
        """Queue > Add samples...: a batch folder and its samples, for one of the active workflows."""
        from gcws.ui.automation.add_samples import AddSamplesDialog
        wfs = [w for w in W.list_workflows() if w.enabled and w.source is not None]
        if not wfs:
            QMessageBox.information(self, "Add samples", "No workflow is active: switch one on in the Workflows tab "
                                    "first; its method processes the samples.")
            return None
        dlg = dialog or AddSamplesDialog(wfs, workflow_id=self.selected_id() or "", parent=self)
        if dialog is None and not dlg.exec():
            return None
        wf_id, folder, runs = dlg.values()
        if not wf_id or not folder or not runs:
            return None
        return self.request(wf_id, folder, runs)

    def add_to_queue(self, items: list) -> list[dict]:
        """Folders tab > Add to queue: the selected batch folders (all their samples) and runs."""
        wanted: dict = {}
        for it in items:
            folder = it.path if it.kind == "batch" else str(Path(it.path).parent)
            runs = wanted.setdefault((it.workflow_id, folder), set())
            runs.add(None if it.kind == "batch" else Path(it.path).name)
        out = []
        for (wf_id, folder), runs in wanted.items():
            wf = W.find(wf_id)
            if wf is None or not wf.enabled:
                QMessageBox.information(self, "Add to queue", f"The workflow '{wf.name if wf else wf_id}' is not "
                                        "active: switch it on in the Workflows tab first.")
                continue
            b = self.request(wf_id, folder, None if None in runs else sorted(runs))
            if b is not None:
                out.append(b)
        return out

    def request(self, workflow_id: str, folder: str, runs) -> Optional[dict]:
        from gcws.automation import store
        wf = W.find(workflow_id)
        if wf is None or wf.source is None:
            return None
        outside = not store.is_inside(folder, wf.source.p("folder") or "")
        b = self.journal.request_samples(workflow_id, folder, runs, outside=outside)
        self.wake_watcher()
        self.tabs.setCurrentWidget(self._queue_page)
        self.refresh()
        return b

    def wake_watcher(self) -> bool:
        """The watcher looks at once; one that is not running is started (one that is busy looks when it
        is done)."""
        if self.control.send("scan_now", 800) is not None:
            return True
        if self.control.status(self.journal).get("state") != "stopped":
            self.state_text.setText("The watcher is busy; it picks the samples up at its next look.")
            return True
        ok = bool(self.control.start())
        self.state_text.setText("Starting the watcher ..." if ok else "The watcher could not be started.")
        QTimer.singleShot(1500, self.refresh)
        return ok

    def start_with_app(self) -> bool:
        """GC Workspace has started: the watcher is started too (Start with GC Workspace) when a workflow is
        active and none runs yet."""
        if not self.with_app.isChecked() or not any(w.enabled for w in W.list_workflows()):
            return False
        try:
            if self.control.status(self.journal).get("state") != "stopped":
                return False
            return bool(self.control.start())
        except Exception:  # noqa: BLE001 - GC Workspace starts anyway
            return False

    def process_again(self) -> list[str]:
        done = [j.id for j in self._queue_jobs() if j.state not in (J.QUEUED, J.PROCESSING)
                and self.journal.request(j.id)]
        self.refresh()
        return done

    def _refresh_log(self):
        try:
            events = self.journal.events(limit=60)
        except Exception:  # noqa: BLE001
            return
        self.log.setRowCount(0)
        for e in reversed(events):
            r = self.log.rowCount()
            self.log.insertRow(r)
            lvl = QTableWidgetItem("●")
            lvl.setForeground(theme.status_color({"error": "bad", "warning": "warn"}.get(e.get("level"), "ok")))
            self.log.setItem(r, 0, QTableWidgetItem(J.when(e["ts"])))
            self.log.setItem(r, 1, lvl)
            self.log.setItem(r, 2, QTableWidgetItem(e.get("text") or ""))
        self.log.resizeColumnToContents(0)
        self.log.resizeColumnToContents(1)

    # -- watcher ---------------------------------------------------------------------------------------

    def _send(self, cmd: str):
        reply = self.control.send(cmd)
        if reply is None and cmd != "quit":
            QMessageBox.information(self, "Watcher", "The watcher is not running.")
        QTimer.singleShot(300, self.refresh)
        return reply

    def start_watcher(self):
        if self.status.get("state") == "not responding":
            self.restart_watcher()
            return
        if not W.list_workflows() or not any(w.enabled for w in W.list_workflows()):
            if QMessageBox.question(self, "Watcher", "No workflow is active. Start the watcher anyway?") \
                    != QMessageBox.Yes:
                return
        if not self.control.start():
            QMessageBox.warning(self, "Watcher", "The watcher could not be started.")
        QTimer.singleShot(1500, self.refresh)

    def restart_watcher(self) -> bool:
        """The watcher does not answer: it is ended and started again."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok = self.control.restart(self.journal)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            QMessageBox.warning(self, "Watcher", "The watcher could not be restarted. End the pythonw process "
                                "\"GC Workspace Watcher\" in the Task Manager and start it again.")
        QTimer.singleShot(1500, self.refresh)
        return ok

    def toggle_pause(self):
        self._send("resume" if self.status.get("state") == "paused" else "pause")

    def stop_watcher(self):
        if self.status.get("current") and QMessageBox.question(
                self, "Watcher", "A sample is being processed. Stop anyway? It will be processed again at the next "
                                 "start.") != QMessageBox.Yes:
            return
        self._send("quit")

    def _autostart(self, on: bool):
        from gcws.automation import autostart
        try:
            autostart.install() if on else autostart.remove()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Start with Windows", str(exc))
        self._refresh_status()

    # -- workflows -------------------------------------------------------------------------------------

    def selected_id(self) -> Optional[str]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        it = self.table.item(rows[0].row(), 0)
        return it.data(Qt.UserRole) if it else None

    def _active_changed(self, item):
        if item.column() != 0:
            return
        wf = W.find(item.data(Qt.UserRole))
        if wf is None:
            return
        on = item.checkState() == Qt.Checked
        if on and W.errors(W.validate(wf)):
            QMessageBox.information(self, "Workflow", f"'{wf.name}' is not complete: open its chart and correct "
                                    "the errors first.")
            self.refresh()
            return
        wf.enabled = on
        wf.save()
        self.control.send("reload", 500)
        self.refresh()

    def new(self, kind: str = "nias"):
        from gcws.core import proc_method as PM
        names = PM.names()
        wf = templates.make(kind, method=names[0] if len(names) == 1 else "")
        wf.save()
        self.refresh()
        return self.open_editor(wf)

    def edit(self):
        wf = W.find(self.selected_id() or "")
        if wf is not None:
            return self.open_editor(wf)
        return None

    def open_editor(self, wf: W.Workflow):
        from gcws.ui.automation.editor import WorkflowEditor
        ed = self.editors.get(wf.id)
        if ed is None:
            ed = WorkflowEditor(wf, self.window())
            ed.setWindowFlag(Qt.Window, True)
            ed.saved.connect(lambda *_: (self.control.send("reload", 500), self.refresh()))
            ed.destroyed.connect(lambda *_, i=wf.id: self.editors.pop(i, None))
            self.editors[wf.id] = ed
        ed.show()
        ed.raise_()
        ed.activateWindow()
        return ed

    def duplicate(self):
        wf = W.find(self.selected_id() or "")
        if wf is None:
            return
        d = W.Workflow.from_dict(wf.to_dict())
        d.id, d.name, d.enabled, d.created = W.new_id("wf"), wf.name + " (copy)", False, ""
        d.save()
        self.refresh()

    def delete(self, confirm: bool = True):
        wf = W.find(self.selected_id() or "")
        if wf is None:
            return
        if confirm and QMessageBox.question(self, "Delete workflow", f"Delete the workflow '{wf.name}'? Reports "
                                            "already made stay in Report² and in the target folders.") \
                != QMessageBox.Yes:
            return
        W.delete(wf.id)
        self.control.send("reload", 500)
        self.refresh()

    def export(self):
        wf = W.find(self.selected_id() or "")
        if wf is None:
            return
        fn, _ = QFileDialog.getSaveFileName(self, "Export workflow", f"{wf.name}.gcwsflow.json", "Workflow (*.json)")
        if fn:
            Path(fn).write_text(json.dumps(wf.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    def import_(self, path=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Import workflow", "", "Workflow (*.json)")
        if not path:
            return None
        try:
            wf = W.load(path)
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Import workflow", str(exc))
            return None
        if W.find(wf.id) is not None:
            wf.id = W.new_id("wf")
        wf.enabled = False
        wf.save()
        self.refresh()
        return wf
