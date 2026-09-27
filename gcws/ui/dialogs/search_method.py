"""Editor for library search methods (shared format with NIAS).

In sequential mode the ticked libraries are searched from the top of the list
down, and the search stops at the first library with a hit at or above the
stop score: the list order is the search order (drag a library or use the
buttons to move it).

"Fast search" lets the automatic library search take all peaks at once
(``gcws.libsearch.fast``): the same hits and scores, in a fraction of the time.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QVBoxLayout)

MODE_LABELS = {"combined": "Combined - all libraries at once, best hits overall",
               "sequential": "Sequential - top to bottom, stop at the first library with a hit ≥ stop score"}
#: the engine accepts stop scores 0-99
MAX_STOP_SCORE = 99
FAST_TIP = ("Fast search: the automatic library search compares all peaks with the libraries at once "
            "instead of one after the other. Hits, scores and their order are exactly those of the normal "
            "search - it is only faster (several times with large libraries and many peaks).")


def order_summary(method) -> str:
    """One line on how ``method`` goes through its libraries."""
    libs = method.enabled_libraries()
    if not libs:
        return "No library ticked in this search method."
    from gcws.identify.service import is_fast
    fast = "  ·  Fast search" if is_fast(method) else ""
    if method.mode == "sequential":
        return (f"Sequential, stop at score {method.stop_score}:  "
                + "  →  ".join(f"{i}. {name}" for i, name in enumerate(libs, 1)) + fast)
    return f"Combined: {len(libs)} librar{'y' if len(libs) == 1 else 'ies'} searched together." + fast


class SearchMethodDialog(QDialog):
    def __init__(self, parent=None, name: str = ""):
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
        self.fast = QCheckBox("Fast search")
        self.fast.setToolTip(FAST_TIP)
        fast_hint = QLabel("all peaks at once - same hits and scores, much faster")
        fast_hint.setObjectName("hint")
        fast_hint.setToolTip(FAST_TIP)
        fast_row = QHBoxLayout()
        fast_row.addWidget(self.fast)
        fast_row.addWidget(fast_hint, 1)
        self.mode = QComboBox()
        for key, label in MODE_LABELS.items():
            self.mode.addItem(label, key)
        self.top_n = QSpinBox()
        self.top_n.setRange(1, SM.MAX_TOP_N)
        self.min_score = QSpinBox()
        self.min_score.setRange(0, 100)
        self.stop_score = QSpinBox()
        self.stop_score.setRange(0, MAX_STOP_SCORE)
        self.stop_score.setToolTip("Sequential search: a library with a hit at or above this score ends the "
                                   "search; the libraries below it are not searched")
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
        self.libs.setDragDropMode(QAbstractItemView.InternalMove)
        self.libs.setDefaultDropAction(Qt.MoveAction)
        self.libs.setSelectionMode(QAbstractItemView.SingleSelection)
        self.libs.setToolTip("Tick the libraries to search. Drag a library (or use the buttons) to change the "
                             "order: sequential search goes from the top down.")
        self.libs.model().rowsMoved.connect(lambda *_: self._renumber())
        self.libs.itemChanged.connect(lambda *_: self._renumber())
        order = QHBoxLayout()
        for text, where, tip in (("▲ Up", "up", "Search this library one place earlier"),
                                 ("▼ Down", "down", "Search this library one place later"),
                                 ("To top", "top", "Search this library first"),
                                 ("To bottom", "bottom", "Search this library last")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, w=where: self.move_library(w))
            order.addWidget(b)
        order.addStretch(1)
        self.order_hint = QLabel()
        self.order_hint.setObjectName("hint")
        self.order_hint.setWordWrap(True)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        refresh = QPushButton("Update the list")
        refresh.setToolTip("Take over the libraries added or removed under Libraries... (new ones switched off)")
        refresh.clicked.connect(self._refresh_libs)
        manage = QPushButton("Libraries...")
        manage.setToolTip("Add or remove the libraries on this PC")
        manage.clicked.connect(self._manage_libs)
        lib_buttons = QHBoxLayout()
        lib_buttons.addWidget(refresh)
        lib_buttons.addWidget(manage)
        lib_buttons.addStretch(1)
        mz = QHBoxLayout()
        mz.addWidget(self.mz_auto)
        mz.addWidget(self.min_mz)
        mz.addWidget(QLabel("-"))
        mz.addWidget(self.max_mz)
        f = QFormLayout()
        f.addRow("Algorithm", self.algorithm)
        f.addRow("Speed", fast_row)
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
        f.addRow("Search order", order)
        f.addRow("", self.order_hint)
        f.addRow("", lib_buttons)
        self.saved = QLabel()
        self.saved.setObjectName("hint")
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.saved, 1)
        bottom.addWidget(bb)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(f)
        lay.addLayout(bottom)
        self.resize(620, 780)
        if name in self.store.methods:
            self.names.setCurrentText(name)
        self._load(self.names.currentText())
        self._mode_changed()

    def _load(self, name):
        if not name or name not in self.store.methods:
            return
        m = self.store.get(name)
        self.current = m
        self.algorithm.setCurrentText(m.algorithm)
        self.mode.setCurrentIndex(max(0, self.mode.findData(m.mode)))
        self.top_n.setValue(m.top_n)
        self.min_score.setValue(m.min_score)
        self.stop_score.setValue(min(m.stop_score, MAX_STOP_SCORE))
        self.mz_auto.setChecked(m.mz_auto)
        self.min_mz.setValue(m.min_mz)
        self.max_mz.setValue(m.max_mz)
        self.threshold.setValue(m.threshold)
        self.hydro.setChecked(m.hydrocarbons)
        self.dedupe.setChecked(m.dedupe)
        self.require_cas.setChecked(m.require_cas)
        self.name_include.setText(m.name_include)
        self.name_exclude.setText(m.name_exclude)
        from gcws.identify.service import is_fast
        self.fast.setChecked(is_fast(name))
        self._fill_libs(m.libraries)

    def _fill_libs(self, entries):
        self.libs.blockSignals(True)
        self.libs.clear()
        for e in entries:
            it = QListWidgetItem(e.name)
            it.setData(Qt.UserRole, e.name)
            it.setFlags((it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled) & ~Qt.ItemIsDropEnabled)
            it.setCheckState(Qt.Checked if e.enabled else Qt.Unchecked)
            self.libs.addItem(it)
        self.libs.blockSignals(False)
        self._renumber()

    def library_order(self) -> list[str]:
        """Library names as listed (the search order), ticked or not."""
        return [self.libs.item(i).data(Qt.UserRole) for i in range(self.libs.count())]

    def _renumber(self):
        """Ticked libraries show their place in the search order."""
        self.libs.blockSignals(True)
        rank = 0
        for i in range(self.libs.count()):
            it = self.libs.item(i)
            name = it.data(Qt.UserRole)
            if it.checkState() == Qt.Checked:
                rank += 1
                it.setText(f"{rank}.  {name}")
            else:
                it.setText(f"      {name}")
        self.libs.blockSignals(False)

    def move_library(self, where: str) -> None:
        """Move the selected library up, down, to the top or to the bottom of the search order."""
        row = self.libs.currentRow()
        if row < 0:
            return
        target = {"up": row - 1, "down": row + 1, "top": 0, "bottom": self.libs.count() - 1}[where]
        target = max(0, min(self.libs.count() - 1, target))
        if target == row:
            return
        self.libs.blockSignals(True)
        it = self.libs.takeItem(row)
        self.libs.insertItem(target, it)
        self.libs.blockSignals(False)
        self.libs.setCurrentRow(target)
        self._renumber()

    def _mode_changed(self, *_):
        sequential = self.mode.currentData() == "sequential"
        self.stop_score.setEnabled(sequential)
        self.order_hint.setText(
            "Sequential: the ticked libraries are searched from the top down; the search stops at the first "
            "library with a hit at or above the stop score." if sequential else
            "Combined: all ticked libraries are searched together, the order does not matter. Switch the "
            "library order to Sequential to search them one after the other in this order.")

    def _manage_libs(self):
        from gcws.ui.dialogs.libraries import LibraryManagerDialog
        LibraryManagerDialog(self).exec()
        self._refresh_libs()

    def _refresh_libs(self):
        from gcws.identify.service import adapt_library_names
        from gcws.libsearch import service as LS
        try:
            status = LS.status()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Libraries", str(exc))
            return
        m = self._collect()
        adapt_library_names(m, [x["name"] for x in self.SM.available_libraries(status)])
        self.SM.reconcile(m, status)
        self._fill_libs(m.libraries)

    def _collect(self):
        m = self.current.copy() if self.current else self.SM.SearchMethod()
        m.algorithm = self.algorithm.currentText()
        m.mode = self.mode.currentData()
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
            name = it.data(Qt.UserRole)
            e = old.get(name) or self.SM.LibraryEntry(name)
            e.enabled = it.checkState() == Qt.Checked
            m.libraries.append(e)
        return m

    def _save(self):
        from gcws.identify.service import set_fast
        m = self._collect()
        self.store.put(m)
        if self.store.save() and set_fast(m.name, self.fast.isChecked()):
            self.current = m
            self.saved.setText(f"Saved {datetime.now():%H:%M:%S}")
        else:
            self.saved.setText("Could not save the search methods")

    def _new(self):
        name, ok = QInputDialog.getText(self, "New method", "Name:")
        if ok and name.strip():
            from gcws.identify.service import set_fast
            m = self._collect()
            m.name = name.strip()
            self.store.put(m)
            self.store.save()
            set_fast(m.name, self.fast.isChecked())
            self.names.addItem(m.name)
            self.names.setCurrentText(m.name)

    def _delete(self):
        from gcws.identify.service import set_fast
        name = self.names.currentText()
        try:
            self.store.delete(name)
        except ValueError as exc:
            QMessageBox.information(self, "Delete", str(exc))
            return
        self.store.save()
        set_fast(name, False)
        self.names.clear()
        self.names.addItems(self.store.names())
