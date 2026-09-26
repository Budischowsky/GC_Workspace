"""Method > Save current settings as Method... / Load Method..."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
                               QSplitter, QVBoxLayout, QWidget)

from gcws.core import proc_method as PM


class SaveMethodDialog(QDialog):
    def __init__(self, win, parent=None):
        super().__init__(parent or win)
        self.win = win
        self.setWindowTitle("Save current settings as Method")
        self.name = QComboBox()
        self.name.setEditable(True)
        self.name.addItems(PM.names())
        from PySide6.QtCore import QSettings
        self.name.setEditText(QSettings().value("method/current", "") or "")
        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("What the method is for, e.g. 'NIAS EtOH 95 % migrates, DB-5 column'")
        self.comment.setMaximumHeight(80)
        f = QFormLayout()
        f.addRow("Name", self.name)
        f.addRow("Comment", self.comment)
        note = QLabel("Saved: " + "; ".join(PM.SECTIONS.values()) + ". Not saved: what belongs to single runs "
                      "(ISTD peak bindings, manual integration, blank assignments).")
        note.setObjectName("hint")
        note.setWordWrap(True)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addWidget(note)
        lay.addWidget(bb)
        self.resize(520, 260)
        self.saved = None

    def _save(self):
        name = self.name.currentText().strip()
        if not name:
            QMessageBox.information(self, "Save method", "Please enter a name.")
            return
        if name in PM.names() and QMessageBox.question(
                self, "Save method", f"Replace the saved method '{name}'?") != QMessageBox.Yes:
            return
        method = PM.collect(self.win, name, self.comment.toPlainText().strip())
        self.saved = PM.save(method)
        from PySide6.QtCore import QSettings
        QSettings().setValue("method/current", name)
        self.win.ws.log("Processing method saved", "", name, "", str(self.saved))
        self.accept()


class LoadMethodDialog(QDialog):
    """Choose a saved method and which of its parts to apply."""

    def __init__(self, win, parent=None):
        super().__init__(parent or win)
        self.win = win
        self.setWindowTitle("Load Method")
        self.resize(820, 480)
        self.list = QListWidget()
        self.list.currentTextChanged.connect(self._show)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.checks = {k: QCheckBox(v) for k, v in PM.SECTIONS.items()}
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.details)
        bl.addWidget(QLabel("Apply:"))
        for c in self.checks.values():
            bl.addWidget(c)
        bl.addStretch(1)
        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(box)
        split.setSizes([260, 560])
        imp = QPushButton("Import...")
        imp.setToolTip("A method file from a colleague (.json)")
        imp.clicked.connect(self.import_file)
        exp = QPushButton("Export...")
        exp.clicked.connect(self.export_file)
        delete = QPushButton("Delete")
        delete.clicked.connect(self.delete)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Load")
        bb.accepted.connect(self._load)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        for b in (imp, exp, delete):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(bb)
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addLayout(row)
        self.method = None
        self.applied: list[str] = []
        self._fill()

    def _fill(self, select: str = ""):
        from PySide6.QtCore import QSettings
        self.list.clear()
        names = PM.names()
        self.list.addItems(names)
        want = select or QSettings().value("method/current", "") or ""
        items = self.list.findItems(want, Qt.MatchExactly)
        self.list.setCurrentItem(items[0] if items else self.list.item(0))
        if not names:
            self.details.setText("No method saved yet: Method > Save current settings as Method...")

    def _show(self, name: str):
        self.method = None
        if not name:
            return
        try:
            self.method = PM.load(name)
        except KeyError:
            return
        self.details.setText(f"<b>{name}</b><br>" + PM.summary(self.method).replace("\n", "<br>"))
        present = self.method.get("sections") or {}
        for k, c in self.checks.items():
            c.setEnabled(k in present)
            c.setChecked(k in present)

    def selected_sections(self) -> list[str]:
        return [k for k, c in self.checks.items() if c.isChecked() and c.isEnabled()]

    def _load(self):
        if self.method is None:
            return
        self.applied = PM.apply(self.win, self.method, self.selected_sections())
        self.accept()

    def import_file(self, path: str = ""):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Import a method", "", "Processing method (*.json)")
        if not path:
            return
        try:
            data = PM.read(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Import method", str(exc))
            return
        PM.save(data)
        self._fill(data["name"])

    def export_file(self, path: str = ""):
        if self.method is None:
            return
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Export the method", f"{self.method['name']}.json",
                                                  "Processing method (*.json)")
        if path:
            PM.save(self.method, path)

    def delete(self):
        if self.method is None:
            return
        name = self.method["name"]
        if QMessageBox.question(self, "Delete method", f"Delete the saved method '{name}'?") != QMessageBox.Yes:
            return
        PM.delete(name)
        self._fill()
