"""Choosing the members (and their order) of a replicate group."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QDialog, QDialogButtonBox, QListWidget, QListWidgetItem, QVBoxLayout

from gcws.io.sequence import ROLE_LABELS
from gcws.ui import theme
from gcws.ui.icons import color_chip


class MembersDialog(QDialog):
    """Samples and standards only; ticked runs are the determinations, in list order."""

    def __init__(self, ws, group, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Determinations of {group['name']}")
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        members = [m for m in group["members"] if m in ws.runs]
        others = [s.id for s in ws.states() if s.id not in members and s.role in ("sample", "standard")]
        for rid in members + others:
            st = ws.runs[rid]
            role = "" if st.role == "sample" else f"   ({ROLE_LABELS.get(st.role, st.role)})"
            it = QListWidgetItem(color_chip(st.color), st.name + role)
            it.setData(Qt.UserRole, rid)
            it.setData(Qt.UserRole + 1, st.name + role)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if rid in members else Qt.Unchecked)
            self.list.addItem(it)
        self.list.itemChanged.connect(lambda *_: self._number())
        self.list.model().rowsMoved.connect(lambda *_: self._number())
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        theme.set_primary(bb.button(QDialogButtonBox.Ok))
        lay = QVBoxLayout(self)
        lay.addWidget(theme.hint("Tick the determinations of this sample and drag them into order: the first is "
                                 "determination A (1), the second B (2), ..."))
        lay.addWidget(self.list)
        lay.addWidget(bb)
        self.resize(480, 420)
        self._number()

    def _number(self):
        self.list.blockSignals(True)
        k = 0
        for i in range(self.list.count()):
            it = self.list.item(i)
            base = it.data(Qt.UserRole + 1)
            if it.checkState() == Qt.Checked:
                k += 1
                it.setText(f"{chr(64 + k) if k <= 26 else k}  ·  {base}")
            else:
                it.setText(base)
        self.list.blockSignals(False)

    def members(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]
