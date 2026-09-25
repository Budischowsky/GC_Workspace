"""Editor for library search methods (shared format with NIAS)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget)


class SearchMethodDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        import gc_search_method as SM
        self.SM = SM
        self.setWindowTitle("Library search methods")
        self.store = SM.MethodStore()
        self.current = None
        self.names = QComboBox()
        self.names.addItems(self.store.names())
        self.names.currentTextChanged.connect(self._load)
        new = QPushButton("New...")
        new.clicked.connect(self._new)
        delete = QPushButton("Delete")
        delete.clicked.connect(self._delete)
        default = QPushButton("Set as default")
        default.clicked.connect(lambda: (self.store.set_default(self.names.currentText()), self.store.save()))
        top = QHBoxLayout()
        top.addWidget(self.names, 1)
        for b in (new, delete, default):
            top.addWidget(b)

        self.algorithm = QComboBox()
        self.algorithm.addItems(["pbm", "similarity"])
        self.mode = QComboBox()
        self.mode.addItems(["combined", "sequential"])
        self.top_n = QSpinBox()
        self.top_n.setRange(1, SM.MAX_TOP_N)
        self.min_score = QSpinBox()
        self.min_score.setRange(0, 100)
        self.stop_score = QSpinBox()
        self.stop_score.setRange(0, 100)
        self.mz_auto = QCheckBox("From the acquisition")
        self.min_mz = QSpinBox()
        self.min_mz.setRange(1, 2000)
        self.max_mz = QSpinBox()
        self.max_mz.setRange(1, 2000)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0, 999)
        self.hydro = QCheckBox("Report accepted non-aromatic hydrocarbons as 'Hydrocarbon'")
        self.dedupe = QCheckBox("Remove duplicate compounds from the hit list")
        self.require_cas = QCheckBox("Only hits with a CAS number")
        self.name_include = QLineEdit()
        self.name_exclude = QLineEdit()
        self.libs = QListWidget()
        refresh = QPushButton("Read libraries from EI Atlas")
        refresh.clicked.connect(self._refresh_libs)
        mz = QHBoxLayout()
        mz.addWidget(self.mz_auto)
        mz.addWidget(self.min_mz)
        mz.addWidget(QLabel("-"))
        mz.addWidget(self.max_mz)
        f = QFormLayout()
        f.addRow("Algorithm", self.algorithm)
        f.addRow("Library order", self.mode)
        f.addRow("Hits per peak", self.top_n)
        f.addRow("Quality limit (identified from)", self.min_score)
        f.addRow("Stop score (sequential)", self.stop_score)
        f.addRow("m/z range", mz)
        f.addRow("Intensity threshold", self.threshold)
        f.addRow("Name must contain", self.name_include)
        f.addRow("Name must not contain", self.name_exclude)
        f.addRow("", self.hydro)
        f.addRow("", self.dedupe)
        f.addRow("", self.require_cas)
        f.addRow("Libraries", self.libs)
        f.addRow("", refresh)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(f)
        lay.addWidget(bb)
        self.resize(560, 720)
        self._load(self.names.currentText())

    def _load(self, name):
        if not name or name not in self.store.methods:
            return
        m = self.store.get(name)
        self.current = m
        self.algorithm.setCurrentText(m.algorithm)
        self.mode.setCurrentText(m.mode)
        self.top_n.setValue(m.top_n)
        self.min_score.setValue(m.min_score)
        self.stop_score.setValue(m.stop_score)
        self.mz_auto.setChecked(m.mz_auto)
        self.min_mz.setValue(m.min_mz)
        self.max_mz.setValue(m.max_mz)
        self.threshold.setValue(m.threshold)
        self.hydro.setChecked(m.hydrocarbons)
        self.dedupe.setChecked(m.dedupe)
        self.require_cas.setChecked(m.require_cas)
        self.name_include.setText(m.name_include)
        self.name_exclude.setText(m.name_exclude)
        self._fill_libs(m.libraries)

    def _fill_libs(self, entries):
        self.libs.clear()
        for e in entries:
            it = QListWidgetItem(e.name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if e.enabled else Qt.Unchecked)
            self.libs.addItem(it)

    def _refresh_libs(self):
        import gc_atlas
        try:
            base = gc_atlas.ensure_server()
            gc_atlas.wait_ready(base)
            status = gc_atlas.request(base, "/api/status")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "EI Atlas", str(exc))
            return
        m = self._collect()
        self.SM.reconcile(m, status)
        self._fill_libs(m.libraries)

    def _collect(self):
        m = self.current.copy() if self.current else self.SM.SearchMethod()
        m.algorithm = self.algorithm.currentText()
        m.mode = self.mode.currentText()
        m.top_n = self.top_n.value()
        m.min_score = self.min_score.value()
        m.stop_score = self.stop_score.value()
        m.mz_auto = self.mz_auto.isChecked()
        m.min_mz, m.max_mz = self.min_mz.value(), self.max_mz.value()
        m.threshold = self.threshold.value()
        m.hydrocarbons = self.hydro.isChecked()
        m.dedupe = self.dedupe.isChecked()
        m.require_cas = self.require_cas.isChecked()
        m.name_include = self.name_include.text()
        m.name_exclude = self.name_exclude.text()
        old = {e.name: e for e in m.libraries}
        m.libraries = []
        for i in range(self.libs.count()):
            it = self.libs.item(i)
            e = old.get(it.text()) or self.SM.LibraryEntry(it.text())
            e.enabled = it.checkState() == Qt.Checked
            m.libraries.append(e)
        return m

    def _save(self):
        m = self._collect()
        self.store.put(m)
        if self.store.save():
            self.current = m
            self.window().statusBar() if hasattr(self.window(), "statusBar") else None

    def _new(self):
        name, ok = QInputDialog.getText(self, "New method", "Name:")
        if ok and name.strip():
            m = self._collect()
            m.name = name.strip()
            self.store.put(m)
            self.store.save()
            self.names.addItem(m.name)
            self.names.setCurrentText(m.name)

    def _delete(self):
        try:
            self.store.delete(self.names.currentText())
        except ValueError as exc:
            QMessageBox.information(self, "Delete", str(exc))
            return
        self.store.save()
        self.names.clear()
        self.names.addItems(self.store.names())
