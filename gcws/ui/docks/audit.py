"""Audit trail dock."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

COLS = ("When", "User", "Action", "Chromatogram", "Detail", "Before", "After", "Reason")


class AuditDock(QWidget):
    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.table)
        ws.audit.listeners.append(self._add)
        self.reload()

    def reload(self):
        self.table.setRowCount(0)
        for rec in self.ws.audit.records:
            self._add(rec)

    def _add(self, rec):
        r = self.table.rowCount()
        self.table.insertRow(r)
        vals = (rec.timestamp.replace("T", " "), rec.user, rec.action, rec.run, rec.detail, rec.before,
                rec.after, rec.reason)
        for c, v in enumerate(vals):
            it = QTableWidgetItem(str(v))
            it.setToolTip(str(v))
            self.table.setItem(r, c, it)
        self.table.scrollToBottom()
