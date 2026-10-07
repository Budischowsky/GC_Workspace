"""Report²: the report of the reports.

How many samples the automation processed, which reports were accepted automatically or by the
analyst and which need control, grouped by batch folder. "To do" holds the open batches; a batch
whose reports are all accepted and delivered moves to the "Archive" by itself ("Reopen" brings it
back). The chips above the list filter it; a sample expands to its number of red and yellow
substances. Selecting a report shows a preview (its PDF, or its Word report converted once by
Microsoft Word). Accept is one click, reject takes a reason, and both can be undone for a few
seconds. Right-click a sample or a batch for everything else; "Delete" only hides (View > Show
deleted reports brings it back). Everything is read from the automation journal every few seconds (the watcher writes it).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QItemSelectionModel, QRect, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QComboBox, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSplitter,
                               QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from gcws.automation import journal as J
from gcws.ui import theme
from gcws.ui.widgets.chips import Chip as _Chip

PERIODS = {"all": ("All", None), "today": ("Today", 1), "week": ("7 days", 7), "month": ("30 days", 30),
           "year": ("12 months", 365)}
LEVEL = {J.ACCEPTED_AUTO: "ok", J.ACCEPTED_MANUAL: "ok", J.CONTROL: "warn", J.REJECTED: "bad",
         J.FAILED: "bad", J.NOT_PROCESSED: "bad", J.WAITING: "neutral", J.QUEUED: "info", J.PROCESSING: "info",
         J.REMOVED: "neutral"}
FILE_LABELS = {"docx": "Word", "xlsx": "Excel", "pdf": "PDF", "dd": "Double determination",
               "batch_docx": "Batch Word", "batch_pdf": "Batch PDF", "batch_xlsx": "Batch summary"}
WAITING_STATES = (J.WAITING, J.QUEUED, J.PROCESSING)
#: the filter chips: key -> (label, level)
FILTERS = {"all": ("All", "info"), "control": ("Control needed", "warn"), "accepted": ("Accepted", "ok"),
           "waiting": ("Waiting", "neutral"), "not_processed": ("Not processed", "bad"),
           "failed": ("Failed", "bad"), "rejected": ("Rejected", "bad"), "removed": ("Removed", "neutral")}
BUCKET_LEVEL = {"control": "warn", "accepted": "ok", "waiting": "info", "not_processed": "bad", "failed": "bad",
                "rejected": "bad", "removed": "neutral"}
WORST = ("bad", "warn", "info", "ok", "neutral")
ROLE_KIND = Qt.UserRole + 1                     # "batch", "job" or "finding"
ROLE_BAR = Qt.UserRole + 2                      # a batch's progress: [(level, count), ...]
COLUMNS = ["Sample", "Status", "Findings", "Delivered", "Processed"]


def bucket(job: J.Job) -> str:
    """The filter chip a report counts for."""
    if job.review_pending or job.state == J.CONTROL:
        return "control"
    if job.state in J.ACCEPTED:
        return "accepted"
    return {J.NOT_PROCESSED: "not_processed", J.FAILED: "failed", J.REJECTED: "rejected",
            J.REMOVED: "removed"}.get(job.state, "waiting")


def can_accept(job: J.Job) -> bool:
    return not job.deleted and not job.review_pending and job.state in (J.CONTROL, J.REJECTED)


def can_reject(job: J.Job) -> bool:
    return not job.deleted and not job.review_pending and job.state in (J.CONTROL, *J.ACCEPTED)


def can_reprocess(job: J.Job) -> bool:
    """Process or report again: a workflow does it, so never an entry accepted by hand (accept it again in
    Replicates instead)."""
    return not job.deleted and not job.review_pending and job.state not in (J.QUEUED, J.PROCESSING) \
        and job.workflow_id != J.MANUAL_WORKFLOW


def delivered(job: J.Job) -> tuple[str, str]:
    """The Delivered column of a report: (text, level)."""
    if job.state not in (J.CONTROL, *J.ACCEPTED):
        return "", "neutral"
    if job.export_pending:
        return "pending", "info"
    return {"done": ("✓", "ok"), "partial": ("partly", "warn"), "error": ("failed", "bad")}.get(
        job.export_state or "", ("", "neutral"))


def activity(job: J.Job) -> float:
    return max(float(job.reviewed_at or 0), float(job.finished or 0), float(job.created or 0))


_DOTS: dict = {}


def _dot(level: str) -> QIcon:
    color = theme.status_color(level)
    if color.name() not in _DOTS:
        pm = QPixmap(12, 12)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(2, 2, 8, 8)
        p.end()
        _DOTS[color.name()] = QIcon(pm)
    return _DOTS[color.name()]


class _BatchBar(QStyledItemDelegate):
    """The status of a batch row: one bar coloured by how many of its reports are in which state."""

    def paint(self, painter, option, index):
        parts = index.data(ROLE_BAR)
        if not parts:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        r = opt.rect.adjusted(4, 0, -4, 0)
        total = sum(n for _, n in parts) or 1
        width = min(64, max(24, r.width() // 3))
        bar = QRect(r.left(), r.center().y() - 3, width, 7)
        painter.save()
        x = bar.left()
        for i, (level, n) in enumerate(parts):
            seg = round(width * n / total) if i < len(parts) - 1 else bar.right() + 1 - x
            painter.fillRect(QRect(x, bar.top(), seg, bar.height()), theme.status_color(level))
            x += seg
        selected = bool(opt.state & QStyle.State_Selected)
        painter.setPen(opt.palette.color(QPalette.HighlightedText if selected else QPalette.Text))
        tr = QRect(bar.right() + 6, r.top(), max(0, r.right() - bar.right() - 6), r.height())
        painter.drawText(tr, Qt.AlignVCenter | Qt.AlignLeft, opt.fontMetrics.elidedText(text, Qt.ElideRight,
                                                                                         tr.width()))
        painter.restore()


class Report2Dock(QWidget):
    openProject = Signal(str)                    # path of a job's .gcws project
    openDetermination = Signal(str)              # Report² job id, resolved by the main window
    _converted = Signal(str, str)                # Word report, error ("" = the preview PDF is written)

    #: seconds Word may take for a preview before it is given up
    WORD_TIMEOUT = 120.0

    def __init__(self, parent=None, journal: Optional[J.Journal] = None, poll_ms: int = 3000):
        super().__init__(parent)
        self._journal = journal
        self.jobs: dict[str, J.Job] = {}         # the reports of the current view (before the chip filter)
        self._batches: dict = {}
        self._by_batch: dict = {}
        self._items: dict[str, QTreeWidgetItem] = {}
        self._expanded: dict[str, bool] = {}
        self.current: Optional[str] = None
        self.prepare_review = None               # optional main-window callback: save current job first
        self.mode = "todo"
        self.filter = "all"
        self._undo_fn: Optional[Callable[[], str]] = None
        self._deliver: set = set()               # accepted here without a watcher: delivered after the undo time
        self._building = False
        self._stamp = None
        self._refreshed = 0.0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        # To do / Archive and the filters
        f = QHBoxLayout()
        self.b_todo = QToolButton()
        self.b_archive = QToolButton()
        modes = QButtonGroup(self)
        for b, mode in ((self.b_todo, "todo"), (self.b_archive, "archive")):
            b.setCheckable(True)
            theme.set_primary(b)
            modes.addButton(b)
            b.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
            f.addWidget(b)
        self.b_todo.setChecked(True)
        self.b_todo.setToolTip("Batches with reports still waiting, needing control or not delivered")
        self.b_archive.setToolTip("Batches whose reports are all accepted and delivered")
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
        lay.addLayout(f)
        # chips (filters), the watcher, rules
        c = QHBoxLayout()
        self.chips: dict[str, _Chip] = {}
        for key in FILTERS:
            chip = _Chip()
            chip.clicked.connect(lambda k=key: self.set_filter(k))
            self.chips[key] = chip
            c.addWidget(chip)
        c.addStretch(1)
        self.watcher = theme.chip("", "neutral")
        self.watcher.setToolTip("The background watcher processes new data")
        c.addWidget(self.watcher)
        self.start_btn = QPushButton("Start watcher")
        self.start_btn.clicked.connect(self._start_watcher)
        c.addWidget(self.start_btn)
        rules = QPushButton("Rules...")
        rules.setToolTip("The rules that decide 'Control needed' (default rules and per workflow)")
        rules.clicked.connect(self.edit_rules)
        c.addWidget(rules)
        self.b_view = QToolButton()
        self.b_view.setText("View")
        self.b_view.setPopupMode(QToolButton.InstantPopup)
        vm = QMenu(self.b_view)
        self.a_show_deleted = vm.addAction("Show deleted reports")
        self.a_show_deleted.setCheckable(True)
        self.a_show_deleted.toggled.connect(lambda *_: self.refresh())
        vm.addAction("Reject reasons...", self.edit_reasons)
        self.b_view.setMenu(vm)
        c.addWidget(self.b_view)
        lay.addLayout(c)
        # the list
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.Interactive)
        for col in (2, 3, 4):
            h.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        h.setStretchLastSection(False)
        self.tree.setColumnWidth(1, 175)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setTextElideMode(Qt.ElideMiddle)             # batch names differ at the end
        self.tree.setItemDelegateForColumn(1, _BatchBar(self.tree))
        self.tree.itemSelectionChanged.connect(self._picked)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.itemExpanded.connect(lambda it: self._remember(it, True))
        self.tree.itemCollapsed.connect(lambda it: self._remember(it, False))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.installEventFilter(self)
        self.empty = theme.hint("")
        self.empty.setAlignment(Qt.AlignCenter)
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.tree)
        self.list_stack.addWidget(self.empty)
        # the details of the selected report
        self.detail = QWidget()
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(6, 0, 0, 0)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dl.addWidget(self.title)
        self.reasons = QLabel()
        self.reasons.setWordWrap(True)
        dl.addWidget(self.reasons)
        acts = QHBoxLayout()
        self._build_actions()
        self.b_accept = QToolButton()
        self.b_accept.setDefaultAction(self.a_accept)
        self.b_accept.setPopupMode(QToolButton.MenuButtonPopup)
        am = QMenu(self.b_accept)
        am.addAction(self.a_accept_comment)
        self.b_accept.setMenu(am)
        theme.set_primary(self.b_accept)
        self.b_accept.setMinimumWidth(self.b_accept.sizeHint().width() + 16)   # room for the menu arrow
        self.b_reject = QToolButton()
        self.b_reject.setText("Reject")
        self.b_reject.setToolTip("Reject the selected reports with a reason (R)")
        self.b_reject.setPopupMode(QToolButton.InstantPopup)
        self.reject_menu = QMenu("Reject", self.b_reject)
        self.reject_menu.aboutToShow.connect(lambda: self._fill_reject_menu(self.reject_menu, self.reject))
        self.b_reject.setMenu(self.reject_menu)
        self.b_open = QToolButton()
        self.b_open.setText("Open report")
        self.b_open.setPopupMode(QToolButton.InstantPopup)
        self.b_project = QPushButton("Open in GC Workspace")
        self.b_project.setToolTip("Open the sample as processed (runs, integration, identifications, ISTDs) to "
                                  "check or correct it; then 'Report again' (Ctrl+O)")
        self.b_project.clicked.connect(self.open_project)
        for b in (self.b_accept, self.b_reject, self.b_open, self.b_project):
            acts.addWidget(b)
        acts.addStretch(1)
        self.b_preview = QToolButton()
        self.b_preview.setText("Preview")
        self.b_preview.setCheckable(True)
        self.b_preview.setToolTip("Show the PDF of the selected report here")
        self.b_preview.setChecked(QSettings().value("report2/preview", True, type=bool))
        self.b_preview.toggled.connect(self._preview_toggled)
        acts.addWidget(self.b_preview)
        dl.addLayout(acts)
        # the preview: the report's PDF (read into memory, so the file is never held open)
        self.preview = QStackedWidget()
        self.preview_note = theme.hint("")
        self.preview_note.setAlignment(Qt.AlignCenter)
        self.preview.addWidget(self.preview_note)
        self.pdf_view = None
        self._pdf_doc = None
        self._preview_path = None
        self._preview_job = None
        self._converting: Optional[str] = None   # the Word report being converted for the preview
        self._pending: Optional[str] = None      # the next one (only the latest request waits)
        self._preview_errors: dict = {}          # Word report -> why no preview could be made
        self._converted.connect(self._preview_converted)
        dl.addWidget(self.preview, 1)
        dl.addStretch(0)                                   # takes the room while the preview is off
        self.body = body = QSplitter(Qt.Horizontal)
        body.addWidget(self.list_stack)
        body.addWidget(self.detail)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.splitterMoved.connect(lambda *_: QSettings().setValue("report2/split", body.sizes()))
        self._sized = False
        lay.addWidget(body, 1)
        # the message bar ("Accepted S12 - Undo")
        self.bar = QFrame()
        self.bar.setObjectName("card")
        self.bar.setProperty("level", "info")
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(8, 3, 4, 3)
        self.bar_text = QLabel()
        bl.addWidget(self.bar_text, 1)
        self.b_undo = QPushButton("Undo")
        self.b_undo.clicked.connect(self.undo)
        bl.addWidget(self.b_undo)
        close = QToolButton()
        close.setText("✕")
        close.setAutoRaise(True)
        close.clicked.connect(self.dismiss_message)
        bl.addWidget(close)
        self.bar.hide()
        lay.addWidget(self.bar)
        self._bar_timer = QTimer(self)
        self._bar_timer.setSingleShot(True)
        self._bar_timer.setInterval(int(J.UNDO_GRACE * 1000))
        self._bar_timer.timeout.connect(self.dismiss_message)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.deliver_due)
        self.timer = QTimer(self)
        self.timer.setInterval(poll_ms)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self._show_detail(None)
        self.refresh()
        self._update_watcher()

    def _build_actions(self):
        """The actions on the selected reports (buttons, right-click menu and keys)."""
        def act(text, slot, tip="", key=""):
            a = QAction(text, self)
            a.triggered.connect(lambda *_: slot())
            if tip:
                a.setToolTip(tip)
            if key:                                        # shown in the menus; the list handles the key itself
                a.setShortcut(QKeySequence(key))
                a.setShortcutContext(Qt.WidgetShortcut)
            return a
        self.a_accept = act("Accept", lambda: self.review(True), "Accept the selected reports (A)", "A")
        self.a_accept_comment = act("Accept with comment...", self.accept_with_comment)
        self.a_replicates = act("Open in Replicates / results", lambda: self.open_determination(self.current))
        self.a_project = act("Open in GC Workspace", self.open_project, key="Ctrl+O")
        self.a_rereport = act("Report again from the (edited) project", lambda: self.reprocess("rereport"))
        self.a_reprocess = act("Process again from the raw data", lambda: self.reprocess("full"))
        self.a_noblank = act("Process without a blank...", self.process_without_blank)
        self.a_remove = act("Remove from the queue...", self.remove_from_queue,
                            "The sample cannot be processed: the watcher skips it and the batch report no "
                            "longer waits for it ('Process again' brings it back)")
        self.a_export = act("Deliver to the target folders now", self.export_now)
        self.a_history = act("Show history...", self.show_history, key="H")
        self.a_folder = act("Open the job folder", self.open_folder)
        self.a_copy = act("Copy sample name", self.copy_names)
        self.a_delete = act("Delete...", self.delete_selected,
                            "Hide in Report² (nothing on disk is deleted; View > Show deleted reports)", "Del")
        self.a_restore = act("Restore", self.restore_selected)

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._sized:                    # the list gets the larger part, not the PDF's size hint
            self._sized = True
            saved = [int(v) for v in (QSettings().value("report2/split") or []) if str(v).isdigit()]
            total = max(2, self.body.width())
            self.body.setSizes(saved if len(saved) == 2 and sum(saved) else [total * 58 // 100, total * 42 // 100])

    # -- data --------------------------------------------------------------------------------------

    @property
    def journal(self) -> J.Journal:
        if self._journal is None:
            self._journal = J.Journal()
        return self._journal

    @property
    def show_deleted(self) -> bool:
        return self.a_show_deleted.isChecked()

    @staticmethod
    def _is_pair(job: J.Job) -> bool:
        return not job.is_batch and len(job.members or []) == 2 and bool(job.project_path) and \
            Path(job.project_path).is_file()

    def visible_pairs(self) -> list[J.Job]:
        """Processed A/B jobs under the current Report² filters, in batch and sample order."""
        return sorted((j for j in self.jobs.values() if self._is_pair(j) and not j.review_pending and
                       not j.deleted and j.state in (J.CONTROL, *J.ACCEPTED)),
                      key=lambda j: (self._batches.get(j.batch_id, {}).get("name", "").casefold(),
                                     j.group_name.casefold()))

    def _stamp_now(self):
        con = self.journal.con
        a = con.execute("SELECT COUNT(*), MAX(COALESCE(finished,0)), MAX(COALESCE(reviewed_at,0)), "
                        "MAX(COALESCE(created,0)), SUM(LENGTH(state)), SUM(deleted), SUM(export_pending), "
                        "SUM(LENGTH(COALESCE(export_state,'')))  FROM jobs").fetchone()
        b = con.execute("SELECT COUNT(*), SUM(deleted), MAX(COALESCE(reopened,0)), "
                        "SUM(LENGTH(COALESCE(plan_json,''))) FROM batches").fetchone()
        return tuple(a) + tuple(b) + (con.execute("SELECT MAX(id) FROM events").fetchone()[0],)

    def _poll(self):
        if not self.isVisible():
            return
        try:
            stamp = self._stamp_now()
        except Exception:  # noqa: BLE001 - the watcher may be writing
            return
        if stamp != self._stamp or time.time() - self._refreshed > 60:      # "5 min ago" moves on
            self.refresh()
        self._update_watcher()

    def _update_watcher(self):
        from gcws.automation.control import WatcherControl
        try:
            st = WatcherControl().status(self.journal)
        except Exception:  # noqa: BLE001
            st = {"state": "stopped"}
        state = st.get("state", "stopped")
        level = {"running": "ok", "processing": "ok", "paused": "warn"}.get(state, "bad")
        theme.set_chip(self.watcher, f"● Watcher {state}", level)
        self.watcher.setToolTip("New data is not processed while the watcher is not running" if level == "bad"
                                else "The background watcher processes new data")
        self.start_btn.setVisible(state in ("stopped", "not responding"))

    def set_mode(self, mode: str):
        """"todo" (open batches) or "archive" (batches accepted and delivered)."""
        self.mode = mode
        (self.b_todo if mode == "todo" else self.b_archive).setChecked(True)
        self.refresh()

    def set_filter(self, key: str):
        """Show only the reports of one chip; the chip clicked again shows all."""
        self.filter = "all" if key == self.filter else key
        self.refresh()

    def refresh(self):
        from gcws.automation import workflow as W
        cur_wf = self.workflow.currentData()
        self.workflow.blockSignals(True)
        self.workflow.clear()
        self.workflow.addItem("All workflows", "")
        for w in W.list_workflows():
            self.workflow.addItem(w.name, w.id)
        if self.journal.jobs(workflow_id=J.MANUAL_WORKFLOW):
            self.workflow.addItem("Accepted by hand", J.MANUAL_WORKFLOW)
        self.workflow.setCurrentIndex(max(0, self.workflow.findData(cur_wf)))
        self.workflow.blockSignals(False)
        days = PERIODS[self.period.currentData() or "all"][1]
        since = time.time() - days * 86400 if days else 0
        text = self.search.text().strip().casefold()
        self._batches = {b["id"]: b for b in self.journal.batches()}
        by_batch: dict = {}
        for j in self.journal.jobs(workflow_id=self.workflow.currentData() or None):
            by_batch.setdefault(j.batch_id, []).append(j)
        self._by_batch = by_batch
        view, n_batches = [], {"todo": 0, "archive": 0}
        for bid, jobs in by_batch.items():
            b = self._batches.get(bid, {})
            if b.get("deleted") and not self.show_deleted:
                continue
            if not self.show_deleted and all(j.deleted for j in jobs):
                continue                               # nothing left to show (only deleted reports)
            mode = "archive" if J.batch_closed(b, jobs) else "todo"
            n_batches[mode] += 1
            if mode != self.mode:
                continue
            if mode == "archive" and since and max(activity(j) for j in jobs) < since:
                continue
            name_hit = text in (b.get("name") or "").casefold()
            for j in jobs:
                if j.deleted and not self.show_deleted:
                    continue
                if mode == "todo" and since and float(j.created or 0) < since:
                    continue
                if text and not name_hit and text not in (j.group_name or "").casefold():
                    continue
                view.append(j)
        self.jobs = {j.id: j for j in view}
        self.b_todo.setText(f"To do ({n_batches['todo']})")
        self.b_archive.setText(f"Archive ({n_batches['archive']})")
        # the chips count the samples of the view (a batch report is not a sample)
        counts = {k: 0 for k in FILTERS}
        auto = manual = 0
        for j in view:
            if j.is_batch or j.deleted:
                continue
            counts["all"] += 1
            counts[bucket(j)] += 1
            auto += j.state == J.ACCEPTED_AUTO
            manual += j.state == J.ACCEPTED_MANUAL
        for key, (label, level) in FILTERS.items():
            n = counts[key]
            shown = n or key in ("all", "control", "accepted") or key == self.filter
            lvl = level if n or key == "all" else "neutral"
            theme.set_chip(self.chips[key], f"{label} {n}" if shown else "", lvl)
            self.chips[key].setProperty("selected", key == self.filter)
            self.chips[key].style().unpolish(self.chips[key])
            self.chips[key].style().polish(self.chips[key])
        self.chips["accepted"].setToolTip(f"{auto} automatic, {manual} by the analyst")
        shown = [j for j in view if self.filter == "all" or bucket(j) == self.filter]
        self._fill(shown)
        if not shown:
            self.empty.setText(self._empty_text(bool(text) or bool(since)))
        self.list_stack.setCurrentIndex(0 if shown else 1)
        try:
            self._stamp = self._stamp_now()
        except Exception:  # noqa: BLE001
            self._stamp = None
        self._refreshed = time.time()
        self._show_detail(self.journal.job(self.current) if self.current else None)

    def _empty_text(self, narrowed: bool) -> str:
        if self.mode == "archive":
            return "No archived batches match." if narrowed else \
                "No batch is archived yet: a batch moves here when all its reports are accepted and delivered."
        if self.filter == "control":
            return "Nothing needs control."
        if self.filter != "all" or narrowed:
            return "No reports match the filters."
        return "Nothing to do: every batch is accepted and delivered.\nOlder batches are in the Archive."

    # -- the list ------------------------------------------------------------------------------------

    def _fill(self, jobs: list):
        self._building = True
        selected = {self._key(it) for it in self.tree.selectedItems()}
        current = self._key(self.tree.currentItem()) if self.tree.currentItem() is not None else None
        scroll = self.tree.verticalScrollBar().value()
        self.tree.clear()
        self._items = {}
        groups: dict = {}
        for j in jobs:
            groups.setdefault(j.batch_id, []).append(j)
        # newest batch first; the order does not jump when a report is decided
        order = sorted(groups, key=lambda bid: -float(self._batches.get(bid, {}).get("first_seen") or
                                                      min(float(j.created or 0) for j in groups[bid])))
        for bid in order:
            top = self._batch_item(bid)
            self.tree.addTopLevelItem(top)
            for j in sorted(groups[bid], key=lambda j: (j.is_batch, float(j.created or 0))):
                top.addChild(self._job_item(j))
        for key, it in self._items.items():
            default = key.startswith("b:") and (self.mode == "todo" or bool(self.search.text().strip()))
            it.setExpanded(self._expanded.get(key, default))
            if key in selected:
                it.setSelected(True)
        if self.current and f"j:{self.current}" in self._items and not selected:
            self._items[f"j:{self.current}"].setSelected(True)
        if current in self._items:
            self.tree.setCurrentItem(self._items[current], 0, QItemSelectionModel.NoUpdate)
        self.tree.verticalScrollBar().setValue(scroll)
        self._building = False

    @staticmethod
    def _key(item: Optional[QTreeWidgetItem]) -> Optional[str]:
        if item is None:
            return None
        kind = item.data(0, ROLE_KIND)
        return f"b:{item.data(0, Qt.UserRole)}" if kind == "batch" else \
            f"j:{item.data(0, Qt.UserRole)}" if kind == "job" else None

    def _remember(self, item, expanded: bool):
        key = self._key(item)
        if key and not self._building:
            self._expanded[key] = expanded

    def _batch_item(self, bid) -> QTreeWidgetItem:
        b = self._batches.get(bid, {})
        live = [j for j in self._by_batch.get(bid, []) if not j.deleted and j.state != J.REMOVED]
        samples = [j for j in live if not j.is_batch]
        n = {k: 0 for k in BUCKET_LEVEL}
        for j in samples:
            n[bucket(j)] += 1
        parts = [(BUCKET_LEVEL[k], v) for k, v in n.items() if v]
        bad = n["failed"] + n["not_processed"] + n["rejected"]
        text = f"{n['accepted']}/{len(samples)} accepted"
        text += f" · {n['control']} control" if n["control"] else ""
        text += f" · {bad} failed/rejected" if bad else ""
        text += f" · {n['waiting']} waiting" if n["waiting"] else ""
        accepted = [j for j in live if j.state in J.ACCEPTED]
        sent = sum(delivered(j)[0] == "✓" for j in accepted)
        last = max((activity(j) for j in self._by_batch.get(bid, [])), default=0)
        findings = sum(len(j.findings or []) for j in samples)
        deleted = bool(b.get("deleted"))
        it = QTreeWidgetItem([b.get("name", "?"), "Deleted" if deleted else text, str(findings) if findings else "",
                              f"{sent}/{len(accepted)}" if accepted else "", J.ago(last)])
        it.setData(0, ROLE_KIND, "batch")
        it.setData(0, Qt.UserRole, bid)
        if not deleted:
            it.setData(1, ROLE_BAR, parts or [("neutral", 1)])
        worst = next((lv for lv in WORST if any(p[0] == lv for p in parts)), "neutral")
        it.setIcon(0, _dot("neutral" if deleted else worst))
        it.setToolTip(0, b.get("folder", ""))
        it.setToolTip(1, text)
        it.setToolTip(4, J.when(last))
        f = it.font(0)
        f.setBold(True)
        f.setItalic(deleted)
        it.setFont(0, f)
        self._items[f"b:{bid}"] = it
        return it

    def _job_item(self, j: J.Job) -> QTreeWidgetItem:
        label = j.group_name
        if self._is_pair(j):
            label += " — " + " / ".join(Path(m).stem for m in j.members)
        status = "Updating report" if j.review_pending else J.STATE_LABELS.get(j.state, j.state)
        if j.deleted:
            status = f"Deleted ({status.lower()})"
        sent, sent_level = delivered(j)
        n = len(j.findings or [])
        it = QTreeWidgetItem([label, status, str(n) if n else "", sent, J.ago(j.finished or j.created)])
        it.setData(0, ROLE_KIND, "job")
        it.setData(0, Qt.UserRole, j.id)
        it.setToolTip(4, J.when(j.finished or j.created))
        if j.workflow_id == J.MANUAL_WORKFLOW:
            it.setToolTip(0, "Accepted by hand in Replicates (no workflow processed it)")
            it.setToolTip(3, "Not delivered: no workflow delivers an entry accepted by hand")
        if j.deleted or j.state == J.REMOVED:
            for c in range(len(COLUMNS)):
                it.setForeground(c, theme.status_color("neutral"))
            if j.deleted:
                f = it.font(0)
                f.setItalic(True)
                it.setFont(0, f)
        else:
            it.setBackground(1, theme.status_brush(LEVEL.get(j.state, "neutral")))
            if sent:
                it.setForeground(3, theme.status_color(sent_level))
        if j.reason:
            it.setToolTip(1, j.reason)
        if n:
            it.setToolTip(2, "\n".join(f.get("text") or "" for f in j.findings))
        # expanded: how many substances the double determination marked red and yellow (not every finding)
        features = (j.evidence or {}).get("features") or []
        if features:
            for light, label, level in (("red", "Red - to decide", "bad"), ("yellow", "Yellow - to check", "warn")):
                rows = [f for f in features if f.get("light") == light]
                child = QTreeWidgetItem([label, str(len(rows))])
                child.setData(0, ROLE_KIND, "finding")
                child.setFlags(Qt.ItemIsEnabled)
                child.setIcon(0, _dot(level if rows else "neutral"))
                if rows:
                    child.setBackground(1, theme.status_brush(level))
                    names = [f"{f.get('rt'):.3f}  {f.get('name') or f.get('feature_id') or ''}"
                             if isinstance(f.get("rt"), (int, float)) else str(f.get("name") or "") for f in rows]
                    child.setToolTip(0, "\n".join(names[:25] + ([f"... {len(names) - 25} more"]
                                                                  if len(names) > 25 else [])))
                it.addChild(child)
        self._items[f"j:{j.id}"] = it
        return it

    def _rows(self) -> list[str]:
        """The job ids of the list, top to bottom."""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            out += [top.child(k).data(0, Qt.UserRole) for k in range(top.childCount())]
        return out

    def _next_control(self, after: str) -> Optional[str]:
        """The next report needing control below ``after`` (from the top again at the end)."""
        rows = self._rows()
        if after not in rows:
            return None
        i = rows.index(after)
        for jid in rows[i + 1:] + rows[:i]:
            j = self.jobs.get(jid)
            if j is not None and j.state == J.CONTROL and not j.review_pending and not j.deleted:
                return jid
        return None

    # -- selection and details ----------------------------------------------------------------------

    def selected_jobs(self) -> list[J.Job]:
        """The selected reports (rows of samples and batch reports), top to bottom."""
        ids = [it.data(0, Qt.UserRole) for it in self.tree.selectedItems() if it.data(0, ROLE_KIND) == "job"]
        rows = self._rows()
        ids.sort(key=lambda i: rows.index(i) if i in rows else len(rows))
        if not ids and self.current:
            ids = [self.current]
        return [j for j in (self.journal.job(i) for i in ids) if j is not None]

    def selected_batch(self) -> Optional[int]:
        items = [it for it in self.tree.selectedItems() if it.data(0, ROLE_KIND) == "batch"]
        return items[0].data(0, Qt.UserRole) if len(items) == 1 else None

    def _picked(self):
        if self._building:
            return
        jobs = [it for it in self.tree.selectedItems() if it.data(0, ROLE_KIND) == "job"]
        cur = self.tree.currentItem()
        it = cur if cur in jobs else (jobs[-1] if jobs else None)
        self.current = it.data(0, Qt.UserRole) if it is not None else None
        self._show_detail(self.journal.job(self.current) if self.current else None)

    def reveal(self, job_id: str):
        """Show the report ``job_id``: To do or Archive as its batch is, the filters set so that it is in
        the list, and selected."""
        job = self.journal.job(job_id)
        if job is None:
            return
        jobs = self.journal.jobs(batch_id=job.batch_id)
        mode = "archive" if J.batch_closed(self.journal.batch_by_id(job.batch_id), jobs) else "todo"
        if self.workflow.currentData() not in ("", job.workflow_id):
            self.workflow.setCurrentIndex(0)
        if self.filter != "all" and bucket(job) != self.filter:
            self.filter = "all"
        text = self.search.text().strip().casefold()
        if text and text not in (job.group_name or "").casefold():
            self.search.clear()
        if mode != self.mode:
            self.set_mode(mode)
        else:
            self.refresh()
        self.select(job_id)

    def select(self, job_id: str):
        self.current = job_id
        it = self._items.get(f"j:{job_id}")
        self._building = True
        self.tree.clearSelection()
        self._building = False
        if it is not None:
            self._building = True
            it.setSelected(True)
            self.tree.setCurrentItem(it, 0, QItemSelectionModel.NoUpdate)
            self.tree.scrollToItem(it)
            self._building = False
        self._show_detail(self.journal.job(job_id))

    def _show_detail(self, job: Optional[J.Job]):
        self.current = job.id if job is not None else None
        jobs = self.selected_jobs() if job is not None else []
        if job is not None and len(jobs) <= 1:
            jobs = [job]
        self._update_actions(jobs)
        menu = QMenu(self.b_open)
        self.b_open.setMenu(menu)
        if job is None:
            bid = self.selected_batch()
            if bid is not None:
                b = self._batches.get(bid, {})
                it = self._items.get(f"b:{bid}")
                self.title.setText(f"<b>{b.get('name', '')}</b><br>{it.text(1) if it else ''}<br>"
                                   f"batch folder {b.get('folder', '')}")
                self.reasons.setText("Right-click the batch for its actions.")
                report = next((j for j in self._by_batch.get(bid, []) if j.is_batch and not j.deleted), None)
            else:
                self.title.setText("Select a report to see why it needs control.")
                self.reasons.setText("")
                report = None
            self.b_open.setEnabled(False)
            self._update_preview(report)
            return
        if len(jobs) > 1:
            self.title.setText(f"<b>{len(jobs)} reports selected</b><br>" +
                               ", ".join(j.group_name for j in jobs[:6]) + (" ..." if len(jobs) > 6 else ""))
            self.reasons.setText("Accept, Reject and the right-click menu act on all of them.")
        else:
            b = self._batches.get(job.batch_id, {})
            text = f"<b>{job.group_name}</b> &nbsp; {theme.chip_html(job.label, LEVEL.get(job.state, 'neutral'))}"
            if job.deleted:
                text += " " + theme.chip_html("Deleted", "neutral")
            text += f"<br>batch folder {b.get('folder', '')} · revision {job.revision}"
            if job.reviewer:
                text += f"<br>{job.label.lower()} by {job.reviewer}, {J.when(job.reviewed_at)}" + \
                        (f": {job.comment}" if job.comment else "")
            if job.reason:
                text += f"<br>{job.reason}"
            self.title.setText(text)
            self.reasons.setText("")                  # the red / yellow counts are in the list
        for node, files in (job.files or {}).items():
            for fmt, path in files.items():
                menu.addAction(FILE_LABELS.get(fmt, fmt), lambda p=path: self.open_path(p)).setToolTip(str(path))
        self.b_open.setEnabled(not menu.isEmpty() and len(jobs) == 1)
        self._update_preview(job if len(jobs) == 1 else None)

    @staticmethod
    def _pdf_of(job: Optional[J.Job]) -> Optional[str]:
        for files in ((job.files or {}) if job is not None else {}).values():
            for fmt in ("pdf", "batch_pdf"):
                if fmt in files:
                    return files[fmt]
        return None

    @staticmethod
    def _docx_of(job: Optional[J.Job]) -> Optional[str]:
        for files in ((job.files or {}) if job is not None else {}).values():
            for fmt in ("docx", "batch_docx"):
                if fmt in files:
                    return files[fmt]
        return None

    @staticmethod
    def preview_pdf(docx) -> Path:
        """Where the preview of a Word report is kept: beside it in the job folder (never delivered), or
        in the automation folder when the job folder cannot be written."""
        docx = Path(docx)
        if os.access(docx.parent, os.W_OK):
            return docx.with_name(docx.stem + ".preview.pdf")
        from gcws.automation import store
        key = hashlib.sha1(str(docx).casefold().encode("utf-8")).hexdigest()[:16]
        return store.root() / "preview" / f"{docx.stem}.{key}.pdf"

    @classmethod
    def _fresh_preview(cls, docx) -> Optional[Path]:
        pdf = cls.preview_pdf(docx)
        try:
            return pdf if pdf.stat().st_mtime >= Path(docx).stat().st_mtime else None
        except OSError:
            return None

    def _convert(self, docx: str):
        """Make the preview PDF of a Word report with Microsoft Word, in a thread (one at a time)."""
        if docx == self._converting:
            return
        if self._converting is not None:
            self._pending = docx
            return
        self._converting = docx
        target = self.preview_pdf(docx)

        def work():
            from gcws.report import service as RS
            err = ""
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.stem + ".part.pdf")
                RS.docx_to_pdf(Path(docx), tmp)
                stamp = max(time.time(), Path(docx).stat().st_mtime)   # newer than its Word report, even
                os.utime(tmp, (stamp, stamp))                         # when that one's clock runs ahead
                os.replace(tmp, target)
            except Exception as exc:  # noqa: BLE001 - no Word, a damaged file, ...
                err = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            self._converted.emit(docx, err)

        threading.Thread(target=work, daemon=True, name="report2-preview").start()
        QTimer.singleShot(int(self.WORD_TIMEOUT * 1000), lambda d=docx: self._convert_timeout(d))

    def _convert_timeout(self, docx: str):
        if self._converting == docx:
            self._preview_converted(docx, f"Word did not answer within {self.WORD_TIMEOUT:.0f} s")

    def _preview_converted(self, docx: str, err: str):
        if self._converting != docx:
            return                                     # given up already (timeout)
        self._converting = None
        if err:
            self._preview_errors[docx] = err
        if self._docx_of(self._preview_job) == docx:
            self._update_preview(self._preview_job)
        nxt, self._pending = self._pending, None
        if nxt and nxt != docx and self._fresh_preview(nxt) is None:
            self._convert(nxt)

    def _preview_toggled(self, on: bool):
        QSettings().setValue("report2/preview", on)
        self._show_detail(self._job())

    def _update_preview(self, job: Optional[J.Job]):
        """The PDF of ``job`` in the panel (a batch row: its batch report)."""
        self._preview_job = job
        self.preview.setVisible(self.b_preview.isChecked())
        if not self.b_preview.isChecked():
            return
        path = self._pdf_of(job)
        if path is None or not Path(path).is_file():
            path = None
            docx = self._docx_of(job)
            if docx and Path(docx).is_file():
                fresh = self._fresh_preview(docx)
                if fresh is not None:
                    path = str(fresh)
                elif docx in self._preview_errors:
                    self._note(f"No preview: Word could not convert the report ({self._preview_errors[docx]}).\n"
                               "Open report shows it in Word.")
                    return
                else:
                    self._convert(docx)
                    self._note("Making a preview of the Word report with Microsoft Word ...\n"
                               "(the first time only; it is kept with the job)")
                    return
        if path is None:
            self._note("Select a report to preview it." if job is None else
                       "This report has no PDF and no Word file to preview. Open report shows it in Excel.")
            return
        if path == self._preview_path:
            return
        from PySide6.QtPdf import QPdfDocument
        if self.pdf_view is None:
            from PySide6.QtPdfWidgets import QPdfView
            self._pdf_doc = QPdfDocument(self)
            self.pdf_view = QPdfView()
            self.pdf_view.setDocument(self._pdf_doc)
            self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
            self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            self.preview.addWidget(self.pdf_view)
        self._pdf_doc.close()
        buf = QBuffer(self._pdf_doc)
        try:
            buf.setData(Path(path).read_bytes())
        except OSError as exc:
            self._note(f"The PDF could not be read: {exc}")
            return
        buf.open(QIODevice.ReadOnly)
        self._pdf_doc.load(buf)
        if self._pdf_doc.status() == QPdfDocument.Status.Error or self._pdf_doc.pageCount() == 0:
            self._note("The PDF could not be opened.")
            return
        self._preview_path = path
        self.preview.setCurrentWidget(self.pdf_view)

    def _note(self, text: str):
        self._preview_path = None
        self.preview_note.setText(text)
        self.preview.setCurrentWidget(self.preview_note)

    def _update_actions(self, jobs: list):
        one = jobs[0] if len(jobs) == 1 else None
        live = [j for j in jobs if not j.deleted]
        self.a_accept.setEnabled(any(can_accept(j) for j in jobs))
        self.a_accept_comment.setEnabled(self.a_accept.isEnabled())
        self.b_reject.setEnabled(any(can_reject(j) for j in jobs))
        self.b_project.setEnabled(one is not None and bool(one.project_path) and Path(one.project_path).exists())
        self.a_project.setEnabled(self.b_project.isEnabled())
        self.a_replicates.setEnabled(one is not None and self._is_pair(one) and not one.review_pending)
        self.a_rereport.setEnabled(any(bool(j.project_path) and not j.is_batch and can_reprocess(j) for j in jobs))
        self.a_reprocess.setEnabled(any(can_reprocess(j) for j in jobs))
        self.a_noblank.setEnabled(one is not None and one.state == J.NOT_PROCESSED and not one.deleted)
        self.a_remove.setEnabled(any(j.state in J.REMOVABLE for j in live))
        self.a_export.setEnabled(one is not None and not one.deleted and one.workflow_id != J.MANUAL_WORKFLOW and
                                 one.state in (J.CONTROL, J.ACCEPTED_AUTO, J.ACCEPTED_MANUAL))
        self.a_history.setEnabled(one is not None)
        self.a_folder.setEnabled(one is not None and bool(one.job_dir))
        self.a_copy.setEnabled(bool(jobs))
        self.a_delete.setEnabled(bool(live))
        self.a_restore.setEnabled(any(j.deleted for j in jobs))

    # -- right-click menus and keys -----------------------------------------------------------------

    def _tree_menu(self, pos):
        menu = self._tree_context_menu(pos)
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _tree_context_menu(self, pos) -> Optional[QMenu]:
        item = self.tree.itemAt(pos)
        if item is None:
            return None
        if item.data(0, ROLE_KIND) == "finding":
            item = item.parent()
        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
            self.tree.setCurrentItem(item, 0, QItemSelectionModel.NoUpdate)
            self._picked()
        if item.data(0, ROLE_KIND) == "batch":
            return self._batch_menu(item.data(0, Qt.UserRole))
        return self._sample_menu()

    def _sample_menu(self) -> QMenu:
        jobs = self.selected_jobs()
        menu = QMenu(self.tree)
        if len(jobs) == 1:
            files = QMenu("Open report", menu)
            for node, fs in (jobs[0].files or {}).items():
                for fmt, path in fs.items():
                    files.addAction(FILE_LABELS.get(fmt, fmt), lambda p=path: self.open_path(p))
            files.setEnabled(not files.isEmpty())
            menu.addMenu(files)
            menu.addAction(self.a_project)
            if self.a_replicates.isEnabled():
                menu.addAction(self.a_replicates)
            menu.addSeparator()
        menu.addAction(self.a_accept)
        menu.addAction(self.a_accept_comment)
        rm = menu.addMenu("Reject")
        rm.aboutToShow.connect(lambda: self._fill_reject_menu(rm, self.reject))
        rm.setEnabled(any(can_reject(j) for j in jobs))
        menu.addSeparator()
        again = menu.addMenu("Process again")
        for a in (self.a_rereport, self.a_reprocess, self.a_noblank):
            again.addAction(a)
        again.setEnabled(any(a.isEnabled() for a in again.actions()))
        menu.addAction(self.a_remove)
        menu.addAction(self.a_export)
        menu.addSeparator()
        menu.addAction(self.a_history)
        menu.addAction(self.a_folder)
        menu.addAction(self.a_copy)
        menu.addSeparator()
        menu.addAction(self.a_restore if all(j.deleted for j in jobs) else self.a_delete)
        return menu

    def _batch_menu(self, bid) -> QMenu:
        b = self._batches.get(bid, {})
        jobs = [j for j in self._by_batch.get(bid, []) if not j.deleted]
        report = next((j for j in jobs if j.is_batch), None)
        menu = QMenu(self.tree)
        files = QMenu("Open batch report", menu)
        for node, fs in ((report.files or {}) if report else {}).items():
            for fmt, path in fs.items():
                files.addAction(FILE_LABELS.get(fmt, fmt), lambda p=path: self.open_path(p))
        files.setEnabled(not files.isEmpty())
        menu.addMenu(files)
        menu.addAction("Open batch folder", lambda: self.open_batch_folder(bid)).setEnabled(bool(b.get("folder")))
        local = b.get("local_folder") or ""
        if local:                                      # the workflow copies the runs to this PC
            menu.addAction("Open the local copy", lambda: self.open_path(local)).setEnabled(os.path.isdir(local))
        menu.addSeparator()
        n = sum(j.state == J.CONTROL and can_accept(j) for j in jobs)
        menu.addAction(f'Accept all "control needed" ({n})...',
                       lambda: self.accept_batch(bid)).setEnabled(bool(n) and not b.get("deleted"))
        rm = menu.addMenu("Reject batch")
        rm.aboutToShow.connect(lambda: self._fill_reject_menu(rm, lambda reason: self.reject_batch(bid, reason)))
        rm.setEnabled(any(can_reject(j) for j in jobs) and not b.get("deleted"))
        menu.addAction("Process batch again...", lambda: self.reprocess_batch(bid)).setEnabled(
            any(can_reprocess(j) and not j.is_batch for j in jobs) and not b.get("deleted"))
        menu.addSeparator()
        if self.mode == "archive":
            menu.addAction("Reopen", lambda: self.reopen_batch(bid))
        if b.get("deleted"):
            menu.addAction("Restore batch", lambda: self.restore_batch(bid))
        else:
            menu.addAction("Delete batch...", lambda: self.delete_batch(bid))
        return menu

    def _fill_reject_menu(self, menu: QMenu, slot):
        from gcws.automation import rules as RU
        menu.clear()
        for reason in RU.load_reject_reasons():
            menu.addAction(reason, lambda r=reason: slot(r))
        menu.addSeparator()
        menu.addAction("Other...", lambda: slot(None))

    def eventFilter(self, obj, ev):
        if obj is self.tree and ev.type() in (QEvent.ShortcutOverride, QEvent.KeyPress):
            slot = self._key_slot(ev)
            if slot is not None:
                ev.accept()
                if ev.type() == QEvent.KeyPress:
                    slot()
                return True
        return super().eventFilter(obj, ev)

    def _key_slot(self, ev) -> Optional[Callable]:
        """A (accept), R (reject), Enter (open report), Ctrl+O (GC Workspace), H (history), Del (delete),
        Ctrl+Z (undo the last accept / reject / delete)."""
        key, mods = ev.key(), ev.modifiers() & ~Qt.KeypadModifier
        if mods == Qt.NoModifier:
            return {Qt.Key_A: (lambda: self.review(True)) if self.a_accept.isEnabled() else None,
                    Qt.Key_R: self._popup_reject if self.b_reject.isEnabled() else None,
                    Qt.Key_Return: self.open_default, Qt.Key_Enter: self.open_default,
                    Qt.Key_H: self.show_history if self.a_history.isEnabled() else None,
                    Qt.Key_Delete: self.delete_selected if self.a_delete.isEnabled() else None}.get(key)
        if mods == Qt.ControlModifier:
            if key == Qt.Key_O and self.b_project.isEnabled():
                return self.open_project
            if key == Qt.Key_Z and self._undo_fn is not None:
                return self.undo
        return None

    def _popup_reject(self):
        self.reject_menu.exec(self.b_reject.mapToGlobal(self.b_reject.rect().bottomLeft()))

    def _double_clicked(self, item, _col):
        kind = item.data(0, ROLE_KIND)
        if kind == "batch":
            report = next((j for j in self._by_batch.get(item.data(0, Qt.UserRole), []) if j.is_batch), None)
            path = self._default_file(report) if report is not None else None
            if path:
                self.open_path(path)
            else:
                item.setExpanded(not item.isExpanded())
        elif kind == "job":
            self.open_default()

    # -- messages and undo ---------------------------------------------------------------------------

    def notify(self, text: str, undo: Optional[Callable[[], str]] = None, level: str = "info"):
        """A message under the list for a few seconds, with "Undo" when ``undo`` is given."""
        self._undo_fn = undo
        self.bar_text.setText(text)
        self.bar.setProperty("level", level)
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)
        self.b_undo.setVisible(undo is not None)
        self.bar.show()
        self._bar_timer.start()

    def dismiss_message(self):
        """The message ends: what it offered to undo is final now (and delivered)."""
        self._bar_timer.stop()
        self.bar.hide()
        self._undo_fn = None
        self.deliver_due()

    def undo(self) -> bool:
        fn, self._undo_fn = self._undo_fn, None
        if fn is None:
            return False
        text = fn()
        self.refresh()
        self.notify(text or "Undone.")
        return bool(text) and not text.startswith("Not undone")

    def deliver_due(self):
        """Deliver what was accepted here while no watcher runs (once it can no longer be undone)."""
        from gcws.automation import export
        from gcws.automation import workflow as W
        ids, self._deliver = self._deliver, set()
        for jid in ids:
            job = self.journal.job(jid)
            if job is None or not job.export_pending or job.state not in J.ACCEPTED:
                continue
            wf = W.find(job.workflow_id)
            if wf is not None:
                try:
                    export.deliver(self.journal, wf, job)
                except Exception as exc:  # noqa: BLE001
                    self.journal.update_job(jid, export_state="error", export_pending=0)
                    self.journal.event("error", f"{job.group_name}: delivery failed: {exc}", job_id=jid)
        if ids:
            self.refresh()

    # -- actions -------------------------------------------------------------------------------------

    def _job(self) -> Optional[J.Job]:
        return self.journal.job(self.current) if self.current else None

    @staticmethod
    def _names(jobs: list) -> str:
        return jobs[0].group_name + (f" and {len(jobs) - 1} more" if len(jobs) > 1 else "")

    def review(self, accept: bool, comment: str = "", job_ids: Optional[list] = None) -> bool:
        """Accept or reject the selected reports (or ``job_ids``): one click, the comment is optional.
        Undo is offered for a few seconds; then the next report needing control is selected."""
        ids = list(job_ids) if job_ids is not None else [j.id for j in self.selected_jobs()]
        self.dismiss_message()                    # the previous decision is final now
        allowed = can_accept if accept else can_reject
        done, before, regenerated = [], [], 0
        for jid in ids:
            job = self.journal.job(jid)
            if job is None or not allowed(job):
                continue
            if self.prepare_review is not None and not self.prepare_review(jid):
                continue
            job = self.journal.job(jid)
            snap = {k: job.row.get(k) for k in J.Journal.REVIEW_FIELDS} | {"revision": job.revision}
            if accept and job.edited:
                ok = self.journal.accept_edited(jid, comment)
                regenerated += ok
            else:
                ok = self.journal.review(jid, accept, comment, grace=J.UNDO_GRACE if accept else 0)
                if ok:
                    before.append((jid, snap))
            if ok:
                done.append(job)
        if not done:
            self.refresh()
            self.notify("The report changed meanwhile (processed again?); look at it once more.", level="warn")
            return False
        if accept and not self._watcher_running():
            self._deliver.update(jid for jid, _ in before)
        nxt = self._next_control(ids[-1]) if len(ids) == 1 else None
        self.refresh()
        if nxt and nxt in self.jobs:
            self.select(nxt)
        verb = "Accepted" if accept else "Rejected"
        text = f"{verb} {self._names(done)}" + (f" ({comment})" if comment and not accept else "")
        if regenerated:
            text += " - the edited report is made again"
        self.notify(text + ".", self._undo_review(before) if before else None)
        return True

    def _undo_review(self, before: list) -> Callable[[], str]:
        def undo() -> str:
            ok = [jid for jid, snap in before if self.journal.undo_review(jid, snap)]
            self._deliver.difference_update(ok)
            if ok:
                self.current = ok[0]
                return f"Undone: {len(ok)} report(s) back as before."
            return "Not undone: the report was delivered meanwhile; reject it instead."
        return undo

    def accept_with_comment(self) -> bool:
        from gcws.ui.dialogs.report2 import ReviewDialog
        jobs = [j for j in self.selected_jobs() if can_accept(j)]
        if not jobs:
            return False
        dlg = ReviewDialog(True, self._names(jobs), sum(len(j.findings or []) for j in jobs), self)
        if not dlg.exec():
            return False
        return self.review(True, dlg.text(), [j.id for j in jobs])

    def reject(self, reason: Optional[str] = None, job_ids: Optional[list] = None) -> bool:
        """Reject with one of the reasons; ``None`` ("Other...") asks for a comment (optional)."""
        if reason is None:
            from gcws.ui.dialogs.report2 import ReviewDialog
            jobs = [j for j in (self.selected_jobs() if job_ids is None else
                                [self.journal.job(i) for i in job_ids]) if j is not None and can_reject(j)]
            if not jobs:
                return False
            dlg = ReviewDialog(False, self._names(jobs), sum(len(j.findings or []) for j in jobs), self)
            if not dlg.exec():
                return False
            reason, job_ids = dlg.text(), [j.id for j in jobs]
        return self.review(False, reason, job_ids)

    def reprocess(self, mode: str = "full", override: Optional[dict] = None) -> bool:
        jobs = [j for j in self.selected_jobs() if can_reprocess(j) and (mode != "rereport" or j.project_path)]
        ok = [j for j in jobs if self.journal.request(j.id, mode, override)]
        if not ok:
            self.notify("This report is already being processed.", level="warn")
        elif not self._watcher_running():
            self.notify(f"Queued {self._names(ok)} - start the watcher to process it.", level="warn")
        self.refresh()
        return bool(ok)

    def remove_from_queue(self, confirm: bool = True) -> bool:
        jobs = [j for j in self.selected_jobs() if j.state in J.REMOVABLE and not j.deleted]
        if not jobs:
            return False
        if confirm and QMessageBox.question(
                self, "Remove from the queue", f"Remove {self._names(jobs)} from the queue? The watcher skips "
                "it and the batch report no longer waits for it. 'Process again' brings it back.") != QMessageBox.Yes:
            return False
        ok = bool(self.journal.remove([j.id for j in jobs]))
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
        ok = self.journal.request(job.id, "full", {"allow_no_blank": True, "by": current_user()})
        self.refresh()
        return ok

    def delete_selected(self, confirm: bool = True) -> list:
        """Hide the selected reports (restorable: View > Show deleted reports, or Undo)."""
        jobs = [j for j in self.selected_jobs() if not j.deleted]
        if not jobs:
            return []
        if confirm and QMessageBox.question(
                self, "Delete from Report²", f"Delete {self._names(jobs)} from Report²?\n\nNothing on disk is "
                "deleted: the reports, the project and the delivered files stay. A sample still in the queue is "
                "removed from it. View > Show deleted reports brings it back.") != QMessageBox.Yes:
            return []
        ids = self.journal.delete([j.id for j in jobs])
        self.current = None
        self.refresh()
        self.notify(f"Deleted {self._names(jobs)}.", lambda: (self.journal.restore(ids), "Restored.")[1])
        return ids

    def restore_selected(self) -> list:
        ids = self.journal.restore([j.id for j in self.selected_jobs() if j.deleted])
        self.refresh()
        return ids

    def _batch_jobs(self, bid) -> list:
        return [j for j in self.journal.jobs(batch_id=bid) if not j.deleted]

    def accept_batch(self, bid, confirm: bool = True) -> bool:
        jobs = [j for j in self._batch_jobs(bid) if j.state == J.CONTROL and can_accept(j)]
        name = self._batches.get(bid, {}).get("name", "")
        if not jobs:
            return False
        if confirm and QMessageBox.question(
                self, "Accept the batch", f"Accept the {len(jobs)} report(s) of {name} that need control?") \
                != QMessageBox.Yes:
            return False
        return self.review(True, "", [j.id for j in jobs])

    def reject_batch(self, bid, reason: Optional[str], confirm: bool = True) -> bool:
        jobs = [j for j in self._batch_jobs(bid) if can_reject(j)]
        name = self._batches.get(bid, {}).get("name", "")
        if not jobs:
            return False
        if confirm and QMessageBox.question(
                self, "Reject the batch", f"Reject the {len(jobs)} report(s) of {name}" +
                (f" ({reason})" if reason else "") + "?") != QMessageBox.Yes:
            return False
        return self.reject(reason, [j.id for j in jobs])

    def reprocess_batch(self, bid, confirm: bool = True) -> bool:
        jobs = [j for j in self._batch_jobs(bid) if can_reprocess(j) and not j.is_batch]
        name = self._batches.get(bid, {}).get("name", "")
        if not jobs:
            return False
        if confirm and QMessageBox.question(
                self, "Process the batch again", f"Process the {len(jobs)} sample(s) of {name} again from the raw "
                "data? Their reports need a new decision.") != QMessageBox.Yes:
            return False
        ok = [j for j in jobs if self.journal.request(j.id, "full")]
        self.refresh()
        if ok and not self._watcher_running():
            self.notify(f"Queued {len(ok)} sample(s) - start the watcher to process them.", level="warn")
        return bool(ok)

    def delete_batch(self, bid, confirm: bool = True) -> bool:
        name = self._batches.get(bid, {}).get("name", "")
        if confirm and QMessageBox.question(
                self, "Delete from Report²", f"Delete the batch {name} with all its reports from Report²?\n\n"
                "Nothing on disk is deleted. The watcher no longer looks at this folder. View > Show deleted "
                "reports brings it back.") != QMessageBox.Yes:
            return False
        ok = self.journal.delete_batch(bid)
        self.refresh()
        if ok:
            self.notify(f"Deleted the batch {name}.", lambda: (self.journal.restore_batch(bid), "Restored.")[1])
        return ok

    def restore_batch(self, bid) -> bool:
        ok = self.journal.restore_batch(bid)
        self.refresh()
        return ok

    def reopen_batch(self, bid) -> bool:
        ok = self.journal.reopen_batch(bid)
        if ok:
            self._expanded[f"b:{bid}"] = True
            self.set_mode("todo")
            self.notify(f"{self._batches.get(bid, {}).get('name', '')} is back in To do.")
        return ok

    def export_now(self) -> list:
        from gcws.automation import export
        from gcws.automation import workflow as W
        job = self._job()
        if job is None:
            return []
        wf = W.find(job.workflow_id)
        if wf is None:
            return []
        self._deliver.discard(job.id)
        lines = export.deliver(self.journal, wf, job)
        self.refresh()
        return lines

    def open_determination(self, job_id: Optional[str]):
        job = self.jobs.get(job_id) or (self.journal.job(job_id) if job_id else None)
        if job is not None and self._is_pair(job) and not job.review_pending:
            self.select(job_id)
            self.openDetermination.emit(job_id)

    def open_project(self):
        job = self._job()
        if job is not None and job.project_path:
            self.openProject.emit(job.project_path)

    def open_path(self, path):
        try:
            os.startfile(str(path))
        except OSError as exc:
            QMessageBox.warning(self, "Report²", str(exc))

    @staticmethod
    def _default_file(job: J.Job) -> Optional[str]:
        for files in (job.files or {}).values():
            for fmt in ("docx", "batch_docx", "pdf", "batch_pdf", "xlsx", "batch_xlsx"):
                if fmt in files:
                    return files[fmt]
        return None

    def open_default(self):
        job = self._job()
        path = self._default_file(job) if job is not None else None
        if path:
            self.open_path(path)

    def open_folder(self):
        job = self._job()
        if job is not None and job.job_dir:
            self.open_path(job.job_dir)

    def open_batch_folder(self, bid):
        folder = self._batches.get(bid, {}).get("folder")
        if folder:
            self.open_path(folder)

    def copy_names(self):
        QApplication.clipboard().setText("\n".join(j.group_name for j in self.selected_jobs()))

    def show_history(self):
        """The history and the files of the selected report, in a window that does not block."""
        from gcws.ui.dialogs.report2 import HistoryDialog
        job = self._job()
        if job is None:
            return None
        events = [(J.when(e["ts"]), e.get("user") or "", e.get("text") or "")
                  for e in self.journal.events(job_id=job.id)]
        sent = {}
        for e in self.journal.exports(job.id):
            if e.get("state") == "done" and e.get("fmt") != "register":
                sent.setdefault((e.get("report_node"), e.get("fmt")), []).append(e.get("dst"))
        files = [(f"{FILE_LABELS.get(fmt, fmt)}: {Path(path).name}", str(Path(path).parent),
                  "; ".join(sent.get((node, fmt), [])))
                 for node, fs in (job.files or {}).items() for fmt, path in fs.items()]
        dlg = HistoryDialog(job.group_name, events, files, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.files.cellDoubleClicked.connect(lambda r, _c: self.open_path(Path(files[r][1]) / files[r][0].split(": ", 1)[1]))
        dlg.show()
        return dlg

    def edit_rules(self):
        from gcws.automation import rules as RU
        from gcws.ui.dialogs.report2 import RulesDialog
        dlg = RulesDialog(RU.load_default_rules(), self, allow_default=False)
        if dlg.exec():
            RU.save_default_rules(dlg.rules())

    def edit_reasons(self):
        from gcws.automation import rules as RU
        from gcws.ui.dialogs.report2 import ReasonsDialog
        dlg = ReasonsDialog(RU.load_reject_reasons(), self)
        if dlg.exec():
            RU.save_reject_reasons(dlg.reasons())

    def _watcher_running(self) -> bool:
        from gcws.automation.control import WatcherControl
        try:
            return WatcherControl().status(self.journal).get("state") not in ("stopped", "not responding")
        except Exception:  # noqa: BLE001
            return False

    def _start_watcher(self):
        from gcws.automation.control import WatcherControl
        WatcherControl().start()
        theme.set_chip(self.watcher, "● Watcher starting ...", "info")
