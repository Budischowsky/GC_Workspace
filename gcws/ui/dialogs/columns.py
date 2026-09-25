"""Classic column chooser: move column names between "Available" and "Shown".

The order of the "Shown" list is the column order of the table. Items move
with the arrow buttons, by double-click or by drag and drop; the filter box
narrows both lists.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QGridLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QVBoxLayout)

from gcws.ui import theme


class _List(QListWidget):
    def __init__(self, reorder: bool):
        super().__init__()
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setAlternatingRowColors(True)
        self.setMinimumWidth(210)
        self.reorder = reorder

    def keys(self) -> list[str]:
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]


class ColumnChooserDialog(QDialog):
    """``columns``: ``[(key, header, tip)]`` in their natural order."""

    def __init__(self, columns, shown, defaults, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose columns")
        self.columns = list(columns)
        self.defaults = list(defaults)
        self._meta = {k: (h, t) for k, h, t in self.columns}

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter column names...")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        self.available = _List(False)
        self.shown = _List(True)
        for lst in (self.available, self.shown):
            lst.itemDoubleClicked.connect(lambda _it, lst=lst: self._move(lst))
            lst.model().rowsInserted.connect(lambda *_: self._update())
            lst.model().rowsRemoved.connect(lambda *_: self._update())
            lst.itemSelectionChanged.connect(self._update)

        b_show = QPushButton("Show  →")
        b_show.setToolTip("Show the selected columns (double-click works too)")
        b_show.clicked.connect(lambda: self._move(self.available))
        b_hide = QPushButton("←  Hide")
        b_hide.setToolTip("Hide the selected columns")
        b_hide.clicked.connect(lambda: self._move(self.shown))
        b_up = QPushButton("↑  Up")
        b_up.clicked.connect(lambda: self._shift(-1))
        b_down = QPushButton("↓  Down")
        b_down.clicked.connect(lambda: self._shift(+1))
        b_top = QPushButton("⤒  First")
        b_top.clicked.connect(lambda: self._shift(-10 ** 6))
        b_bottom = QPushButton("⤓  Last")
        b_bottom.clicked.connect(lambda: self._shift(10 ** 6))
        b_reset = QPushButton("Reset to defaults")
        b_reset.clicked.connect(lambda: self._fill(self.defaults))
        self._buttons = {"show": b_show, "hide": b_hide, "up": b_up, "down": b_down, "top": b_top,
                         "bottom": b_bottom}

        mid = QVBoxLayout()
        mid.addStretch(1)
        mid.addWidget(b_show)
        mid.addWidget(b_hide)
        mid.addStretch(1)
        order = QVBoxLayout()
        for b in (b_top, b_up, b_down, b_bottom):
            order.addWidget(b)
        order.addStretch(1)

        grid = QGridLayout()
        head_a = QLabel("Available columns")
        head_a.setObjectName("title")
        head_s = QLabel("Shown columns (table order)")
        head_s.setObjectName("title")
        grid.addWidget(head_a, 0, 0)
        grid.addWidget(head_s, 0, 2)
        grid.addWidget(self.available, 1, 0)
        grid.addLayout(mid, 1, 1)
        grid.addWidget(self.shown, 1, 2)
        grid.addLayout(order, 1, 3)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)

        self.box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.box.accepted.connect(self.accept)
        self.box.rejected.connect(self.reject)
        self.box.addButton(b_reset, QDialogButtonBox.ResetRole)
        theme.set_primary(self.box.button(QDialogButtonBox.Ok))

        lay = QVBoxLayout(self)
        lay.addWidget(theme.hint("Move column names between the two lists with the arrows, a double-click or "
                                 "drag and drop. The order of the shown columns is the order in the table."))
        lay.addWidget(self.filter)
        lay.addLayout(grid, 1)
        lay.addWidget(self.box)
        self.resize(640, 520)
        self._fill(shown)

    # -- content ---------------------------------------------------------------

    def _item(self, key) -> QListWidgetItem:
        header, tip = self._meta[key]
        it = QListWidgetItem(header)
        it.setData(Qt.UserRole, key)
        if tip:
            it.setToolTip(tip)
        return it

    def _fill(self, shown):
        shown = [k for k in shown if k in self._meta]
        self.available.clear()
        self.shown.clear()
        for k in shown:
            self.shown.addItem(self._item(k))
        for k, _h, _t in self.columns:
            if k not in shown:
                self.available.addItem(self._item(k))
        self._filter(self.filter.text())
        self._update()

    def _filter(self, text):
        text = text.strip().lower()
        for lst in (self.available, self.shown):
            for i in range(lst.count()):
                it = lst.item(i)
                it.setHidden(bool(text) and text not in it.text().lower())

    def _move(self, source: _List):
        target = self.shown if source is self.available else self.available
        items = [it for it in source.selectedItems() if not it.isHidden()]
        if not items:
            return
        keys = [it.data(Qt.UserRole) for it in items]
        for it in items:
            source.takeItem(source.row(it))
        if target is self.shown:
            for k in keys:
                target.addItem(self._item(k))
        else:                                  # back to its natural position
            natural = [k for k, _h, _t in self.columns]
            for k in keys:
                pos = sum(1 for other in target.keys() if natural.index(other) < natural.index(k))
                target.insertItem(pos, self._item(k))
        target.clearSelection()
        for k in keys:
            for i in range(target.count()):
                if target.item(i).data(Qt.UserRole) == k:
                    target.item(i).setSelected(True)
        self._update()

    def _shift(self, delta: int):
        rows = sorted(self.shown.row(it) for it in self.shown.selectedItems())
        if not rows:
            return
        keys = self.shown.keys()
        chosen = [keys[r] for r in rows]
        rest = [k for k in keys if k not in chosen]
        first = max(0, min(len(rest), rows[0] + delta)) if abs(delta) < 10 ** 6 else (0 if delta < 0 else len(rest))
        new = rest[:first] + chosen + rest[first:]
        self.shown.clear()
        for k in new:
            self.shown.addItem(self._item(k))
        for i, k in enumerate(new):
            if k in chosen:
                self.shown.item(i).setSelected(True)
        self._filter(self.filter.text())

    def _update(self):
        self._buttons["show"].setEnabled(bool(self.available.selectedItems()))
        sel = bool(self.shown.selectedItems())
        for k in ("hide", "up", "down", "top", "bottom"):
            self._buttons[k].setEnabled(sel)
        self.box.button(QDialogButtonBox.Ok).setEnabled(self.shown.count() > 0)

    def shown_keys(self) -> list[str]:
        return self.shown.keys()
