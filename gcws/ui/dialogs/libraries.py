"""Identify > Libraries...: the spectral libraries on this PC that GC Workspace searches."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from gcws.libsearch import store

C_USE, C_NAME, C_KIND, C_COUNT, C_STATUS, C_PATH = range(6)


class LibraryManagerDialog(QDialog):
    """Add library files or folders, switch them on or off, remove them; see what loaded.

    Loading reads each library once into a search index (``DATA/libcache``); later starts
    read the index. Nothing is copied or changed in the libraries themselves.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Libraries")
        self.resize(980, 460)
        self.libs = store.load()
        self.info: dict[str, dict] = {}            # name -> engine source (count, status, error)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Use", "Name", "Type", "Spectra", "Status", "Location"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(C_PATH, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._use_changed)
        add_file = QPushButton("Add library file...")
        add_file.setToolTip("An .msp or Wiley/Shimadzu .lib file, or a file inside an Agilent .L or NIST folder")
        add_file.clicked.connect(self.add_file)
        add_folder = QPushButton("Add folder...")
        add_folder.setToolTip("An Agilent .L folder, a NIST library folder (mainlib, replib, a user library) or a "
                              "folder holding several libraries: every library in it is added")
        add_folder.clicked.connect(self.add_folder)
        atlas = QPushButton("Take over from EI Atlas")
        atlas.setToolTip("Add the libraries of an EI Atlas installation on this PC (once; EI Atlas is not needed "
                         "afterwards)")
        atlas.clicked.connect(self.import_atlas)
        remove = QPushButton("Remove")
        remove.setToolTip("Stop searching the selected libraries (the files stay where they are)")
        remove.clicked.connect(self.remove)
        self.load_btn = QPushButton("Load / check")
        self.load_btn.setToolTip("Read the libraries now (the first time builds the search index, which can take "
                                 "a few minutes for large libraries)")
        self.load_btn.clicked.connect(self.check)
        buttons = QHBoxLayout()
        for b in (add_file, add_folder, atlas, remove):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(self.load_btn)
        self.state = QLabel()
        self.state.setObjectName("hint")
        self.state.setWordWrap(True)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("GC Workspace searches these libraries. Which of them a search uses is set in the "
                             "search method (Identify > Search methods...)."))
        lay.addWidget(self.table, 1)
        lay.addLayout(buttons)
        lay.addWidget(self.state)
        lay.addWidget(bb)
        self._fill()

    # -- table ---------------------------------------------------------------------------

    def _fill(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for spec in self.libs:
            r = self.table.rowCount()
            self.table.insertRow(r)
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            use.setCheckState(Qt.Checked if spec.enabled else Qt.Unchecked)
            self.table.setItem(r, C_USE, use)
            info = self.info.get(spec.name, {})
            if not spec.exists():
                status = "not found"
            elif not spec.enabled:
                status = "off"
            else:
                status = info.get("status", "not loaded yet")
                if info.get("error"):
                    status += f": {info['error']}"
            count = info.get("count")
            for c, v in ((C_NAME, spec.name), (C_KIND, store.KINDS.get(spec.kind, spec.kind)),
                         (C_COUNT, "" if count is None else f"{count:,}"), (C_STATUS, status), (C_PATH, spec.path)):
                it = QTableWidgetItem(v)
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if c == C_COUNT:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, it)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(C_PATH, QHeaderView.Stretch)
        self.table.blockSignals(False)
        n = sum(1 for s in self.libs if s.enabled)
        if not self.libs:
            self.state.setText("No library yet: add the library files or folders on this PC.")
        elif not self.state.text() or self.state.text().startswith("No library"):
            self.state.setText(f"{n} of {len(self.libs)} libraries in use.")

    def _use_changed(self, item):
        if item.column() != C_USE:
            return
        self.libs[item.row()].enabled = item.checkState() == Qt.Checked
        self._save()

    def _save(self):
        store.save(self.libs)
        from gcws.libsearch import service
        service.reset()
        self._fill()

    # -- adding ---------------------------------------------------------------------------

    def _add(self, found, where: str):
        if not found:
            QMessageBox.information(self, "Libraries", f"No spectral library found in {where}.")
            return
        before = len(self.libs)
        self.libs = store.add(self.libs, found)
        self._save()
        added = len(self.libs) - before
        self.state.setText(f"{added} librar{'y' if added == 1 else 'ies'} added"
                           + (f", {len(found) - added} already listed" if added < len(found) else "")
                           + ". Press 'Load / check' to read them now (otherwise the next search does).")

    def add_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Add a library", "", "Spectral libraries (*.msp *.lib *.MSP *.LIB HEADER.IND header.ind "
                                       "*.dbu *.DBU nist.db NIST.DB nist.dbr);;All files (*)")
        if path:
            self._add(store.discover(path), path)

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Add a library folder (or a folder of libraries)")
        if path:
            self._add(store.discover(path), path)

    def import_atlas(self):
        self._add(store.atlas_libraries(), "the EI Atlas installation")

    def remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.libs[r]
        if rows:
            self._save()

    # -- loading ---------------------------------------------------------------------------

    def check(self):
        from gcws.libsearch import service
        from gcws.ui.workers import submit
        self.load_btn.setEnabled(False)
        self.state.setText("Loading the libraries ...")
        submit(service.status, with_progress=True, on_progress=self.state.setText,
               on_done=self._loaded, on_error=self._failed)

    def _loaded(self, status):
        self.load_btn.setEnabled(True)
        self.info = {s["name"]: s for s in status.get("libraries") or []}
        ready = sum(1 for s in self.info.values() if s.get("count"))
        self.state.setText(f"{ready} libraries ready, {status.get('count', 0):,} reference spectra.")
        self._fill()

    def _failed(self, text):
        self.load_btn.setEnabled(True)
        self.state.setText("Could not load: " + text.splitlines()[0])
