"""Add samples to the automation queue by hand (Automation panel > Queue > Add samples...)."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QVBoxLayout)

from gcws.automation import scanner as SC
from gcws.ui import theme
from gcws.ui.automation.node_dialogs import _folder_row


def folder_samples(folder) -> list[tuple[str, list[str], list[str]]]:
    """The samples of a batch folder as the watcher plans them: ``(name, member runs, blanks)``. Only the
    folder's names are read."""
    from gcws.automation import planner as PN
    from gcws.io import sequence as SQ
    try:
        with os.scandir(folder) as it:
            names = sorted((e.name for e in it if SC._is_run(e)), key=str.casefold)
    except OSError:
        return []
    present = {Path(n).stem.casefold(): {"name": n, "ready": True} for n in names}
    seq = SQ.read_sequence(Path(folder), present=names)
    plan = PN.plan_batch(present, seq, quiet=True, require="none")
    out = []
    for g in plan.groups:
        members = [present[m]["name"] for m in g.members if m in present]
        blanks = [present[s]["name"] for v in g.blanks.values() for s in v if s in present]
        if members:
            out.append((g.name, members, blanks))
    return out


class AddSamplesDialog(QDialog):
    """Choose a workflow, a batch folder and its samples; ``values()`` -> (workflow id, folder, run names)."""

    def __init__(self, workflows: list, folder: str = "", workflow_id: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add samples to the queue")
        self.setMinimumWidth(560)
        self.workflows = workflows
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.workflow = QComboBox()
        for wf in workflows:
            self.workflow.addItem(wf.name, wf.id)
        self.workflow.setCurrentIndex(max(0, self.workflow.findData(workflow_id)))
        form.addRow("Workflow", self.workflow)
        self.folder = QLineEdit(folder)
        self.folder.setPlaceholderText("the batch folder with the runs")
        self.folder.editingFinished.connect(self.load)
        self.folder.textChanged.connect(lambda *_: self.load() if os.path.isdir(self.folder.text().strip()) else None)
        form.addRow("Batch folder", _folder_row(self.folder, self))
        lay.addLayout(form)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        self.note = QLabel()
        self.note.setObjectName("hint")
        row = QHBoxLayout()
        b_all = QPushButton("Select all")
        b_all.clicked.connect(lambda: self._tick(True))
        b_none = QPushButton("Select none")
        b_none.clicked.connect(lambda: self._tick(False))
        row.addWidget(b_all)
        row.addWidget(b_none)
        row.addWidget(self.note, 1)
        lay.addLayout(row)
        lay.addWidget(theme.hint("The watcher processes the ticked samples at its next look, with the blanks of "
                                 "the same folder: without waiting for the quiet time, and also samples that were "
                                 "there before watching started or were processed already (as a new revision)."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Add to queue")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.ok = bb.button(QDialogButtonBox.Ok)
        lay.addWidget(bb)
        self._loaded = None
        self.load()

    def load(self):
        folder = self.folder.text().strip()
        if folder == self._loaded:
            return
        self._loaded = folder
        self.list.clear()
        samples = folder_samples(folder) if folder and os.path.isdir(folder) else []
        for name, members, blanks in samples:
            it = QListWidgetItem(f"{name}   ({', '.join(members)})")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setData(Qt.UserRole, members)
            it.setToolTip("Blanks: " + (", ".join(blanks) if blanks else "none in this folder"))
            self.list.addItem(it)
        self.note.setText(f"{len(samples)} sample(s)" if samples else
                          ("No runs in this folder." if folder else "Choose a batch folder."))
        self.ok.setEnabled(bool(samples))

    def _tick(self, on: bool):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def values(self) -> tuple[str, str, list[str]]:
        runs = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.Checked:
                runs += it.data(Qt.UserRole)
        return self.workflow.currentData() or "", self.folder.text().strip(), runs
