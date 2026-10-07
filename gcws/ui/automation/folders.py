"""The Folders tab of the Automation panel: every watched folder with its batch folders and runs, their
local copies, and what became of each sample.

Built from :func:`gcws.automation.overview.overview` (the journal and the watcher's last look), so a slow
network drive is never read here.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QMenu, QMessageBox, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from gcws.automation import journal as J
from gcws.automation import overview as OV
from gcws.ui import theme

COLUMNS = ["Name", "Watched folder", "Local copy", "Sample", "State", "Why"]
ROLE_ITEM = Qt.UserRole + 10


class FoldersView(QWidget):
    showJob = Signal(str)                                  # a Report² job id
    addToQueue = Signal(object)                            # [OV.Item, ...] (batches or runs)

    def __init__(self, journal: Callable[[], J.Journal], parent=None):
        super().__init__(parent)
        self._journal = journal
        self._expanded: dict[str, bool] = {}
        self._signature = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setTextElideMode(Qt.ElideMiddle)
        h = self.tree.header()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setStretchLastSection(True)
        for col, width in enumerate((260, 200, 170, 150, 130)):
            self.tree.setColumnWidth(col, width)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemSelectionChanged.connect(self._buttons)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.open_folder(it))
        self.tree.itemExpanded.connect(lambda it: self._expanded.__setitem__(self._key(it), True))
        self.tree.itemCollapsed.connect(lambda it: self._expanded.__setitem__(self._key(it), False))
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.b_add = QPushButton("Add to queue")
        self.b_add.setToolTip("Process the selected samples or batch folders now: without waiting for the quiet "
                              "time, also runs that were there before watching started or were processed already")
        self.b_add.clicked.connect(lambda: self.add_selected())
        self.b_open = QPushButton("Open folder")
        self.b_open.clicked.connect(lambda: self.open_folder())
        self.b_local = QPushButton("Open local copy")
        self.b_local.clicked.connect(lambda: self.open_local())
        self.b_report = QPushButton("Show in Report²")
        self.b_report.clicked.connect(lambda: self.show_in_report2())
        for b in (self.b_add, self.b_open, self.b_local, self.b_report):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(theme.hint("What the watcher saw at its last look in each watched folder, and the local copies. "
                                 "Hover a row for the whole text; right-click for more."))
        self._buttons()

    @property
    def journal(self) -> J.Journal:
        return self._journal()

    # -- building ----------------------------------------------------------------------------------------

    @staticmethod
    def _key(it: QTreeWidgetItem) -> str:
        item = it.data(0, ROLE_ITEM)
        if item is None:
            return ""
        return f"{item.kind}:{item.workflow_id}:{item.path or item.local_path or item.name}"

    def refresh(self, workflows=None) -> list:
        from gcws.automation import workflow as W
        try:
            items = OV.overview(self.journal, W.list_workflows() if workflows is None else workflows)
        except Exception:  # noqa: BLE001 - the watcher may be writing
            return []
        signature = repr(items)
        if signature == self._signature:
            return items
        self._signature = signature
        selected = {self._key(it) for it in self.tree.selectedItems()}
        scroll = self.tree.verticalScrollBar().value()
        self.tree.clear()
        for item in items:
            top = self._item(item)
            self.tree.addTopLevelItem(top)
            self._restore(top, selected)
        self.tree.verticalScrollBar().setValue(scroll)
        if not items:
            empty = QTreeWidgetItem(["No workflow watches a folder yet: add one in the Workflows tab."])
            empty.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(empty)
        self._buttons()
        return items

    def _restore(self, it: QTreeWidgetItem, selected: set):
        """Open and selected as before the list was built again (a workflow is open the first time)."""
        key = self._key(it)
        it.setExpanded(self._expanded.get(key, it.data(0, ROLE_ITEM).kind == "workflow"))
        if key in selected:
            it.setSelected(True)
        for i in range(it.childCount()):
            self._restore(it.child(i), selected)

    def _item(self, item: OV.Item) -> QTreeWidgetItem:
        it = QTreeWidgetItem([item.name, item.watched, item.local, item.sample, item.state, item.why])
        it.setData(0, ROLE_ITEM, item)
        for c, text in enumerate((item.path or item.local_path or item.name, item.watched, item.local, item.sample,
                                  item.state, item.why)):
            if text:
                it.setToolTip(c, text)
        if item.kind == "file":
            for c in range(len(COLUMNS)):
                it.setForeground(c, theme.status_color("neutral"))
        elif item.state and item.kind in ("run", "batch"):
            it.setBackground(4, theme.status_brush(item.level))
        if item.kind in ("workflow", "batch"):
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
        for child in item.children:
            it.addChild(self._item(child))
        return it

    # -- selection and actions -------------------------------------------------------------------------

    def selected(self) -> list[OV.Item]:
        return [it.data(0, ROLE_ITEM) for it in self.tree.selectedItems() if it.data(0, ROLE_ITEM) is not None]

    def _current(self, it: Optional[QTreeWidgetItem] = None) -> Optional[OV.Item]:
        if it is not None:
            return it.data(0, ROLE_ITEM)
        items = self.selected()
        return items[0] if items else None

    @staticmethod
    def addable(item: OV.Item) -> bool:
        return item.kind in ("batch", "run") and bool(item.path) and item.watched not in ("—", "other file")

    def _buttons(self):
        items = self.selected()
        one = items[0] if len(items) == 1 else None
        self.b_add.setEnabled(any(self.addable(i) for i in items))
        self.b_open.setEnabled(one is not None and bool(one.path or one.local_path))
        self.b_local.setEnabled(one is not None and bool(one.local_path) and os.path.exists(one.local_path))
        self.b_report.setEnabled(one is not None and bool(one.job_id))

    def _menu(self, pos):
        menu = self.context_menu(pos)
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def context_menu(self, pos) -> Optional[QMenu]:
        it = self.tree.itemAt(pos)
        if it is None or it.data(0, ROLE_ITEM) is None:
            return None
        if not it.isSelected():
            self.tree.clearSelection()
            it.setSelected(True)
        self._buttons()
        menu = QMenu(self.tree)
        if not self.b_add.isHidden():
            menu.addAction("Add to queue", self.add_selected).setEnabled(self.b_add.isEnabled())
            menu.addSeparator()
        menu.addAction("Open folder", self.open_folder).setEnabled(self.b_open.isEnabled())
        menu.addAction("Open local copy", self.open_local).setEnabled(self.b_local.isEnabled())
        menu.addAction("Show in Report²", self.show_in_report2).setEnabled(self.b_report.isEnabled())
        return menu

    def add_selected(self) -> list:
        items = [i for i in self.selected() if self.addable(i)]
        if items:
            self.addToQueue.emit(items)
        return items

    def _start(self, path: str):
        try:
            os.startfile(path)
        except OSError as exc:
            QMessageBox.warning(self, "Folders", str(exc))

    def open_folder(self, it: Optional[QTreeWidgetItem] = None):
        item = self._current(it)
        if item is None:
            return
        path = item.path or item.local_path
        if item.kind in ("run", "file") and path:
            path = os.path.dirname(path)
        if path:
            self._start(path)

    def open_local(self):
        item = self._current()
        if item is not None and item.local_path and os.path.exists(item.local_path):
            path = item.local_path if os.path.isdir(item.local_path) and item.kind != "run" else \
                os.path.dirname(item.local_path)
            self._start(path)

    def show_in_report2(self):
        item = self._current()
        if item is not None and item.job_id:
            self.showJob.emit(item.job_id)
