"""Choosing the members (and their order) of a replicate group."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem,
                               QVBoxLayout)


class MembersDialog(QDialog):
    def __init__(self, ws, group, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Determinations of {group['name']}")
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        members = [m for m in group["members"] if m in ws.runs]
        others = [s.id for s in ws.states() if s.id not in members]
        for rid in members + others:
            st = ws.runs[rid]
            it = QListWidgetItem(f"{st.name}   [{st.role}]")
            it.setData(Qt.UserRole, rid)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if rid in members else Qt.Unchecked)
            self.list.addItem(it)
        note = QLabel("Tick the determinations of this sample; drag to set the order "
                      "(determination 1, 2, 3 ... in the report).")
        note.setWordWrap(True)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(note)
        lay.addWidget(self.list)
        lay.addWidget(bb)
        self.resize(460, 420)

    def members(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]
