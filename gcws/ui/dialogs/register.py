"""Unknown register: file the displayed spectrum, browse and edit entries."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from gcws.ui.docks.spectrum import StickPlot


def db_path() -> Path:
    import gc_register as R
    return R.default_db_path()


class SaveUnknownDialog(QDialog):
    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        import gc_register as R
        self.setWindowTitle("Register unknown")
        self.item = item
        ions = R.significant_ions(item.get("spectrum") or [])
        mz = "/".join(str(m) for m, _, _ in sorted(ions, key=lambda x: x[2])[:6])
        self.name = QLineEdit(item.get("assigned_name", ""))
        self.cas = QLineEdit()
        self.note = QLineEdit()
        self.status = QComboBox()
        self.status.addItems(list(R.ENTRY_STATUSES))
        self.with_tic = QCheckBox("Store the chromatogram around the peak")
        self.with_tic.setChecked(True)
        f = QFormLayout()
        f.addRow("Sample", QLabel(item.get("sample_name", "")))
        f.addRow("RT", QLabel(f"{item.get('rt', 0):.3f} min"))
        f.addRow("Significant ions", QLabel(mz))
        f.addRow("Substance name (optional)", self.name)
        f.addRow("CAS (optional)", self.cas)
        f.addRow("Note", self.note)
        f.addRow("Status", self.status)
        f.addRow("", self.with_tic)
        plot = StickPlot()
        spec = item.get("spectrum") or []
        plot.show_spectrum(np.array([p[0] for p in spec]), np.array([p[1] for p in spec]), title="spectrum to file")
        plot.setMinimumHeight(220)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addWidget(plot)
        lay.addWidget(QLabel(f"Register: {db_path()}"))
        lay.addWidget(bb)
        self.resize(620, 560)

    def result_item(self) -> dict:
        out = dict(self.item)
        out.update(assigned_name=self.name.text().strip(), assigned_cas=self.cas.text().strip(),
                   note=self.note.text().strip(), status=self.status.currentText())
        return out


def save_unknown(win) -> None:
    import gc_export
    import gc_register as R
    ws = win.ws
    st, peak = win.spectrum.target_peak()
    points = win.spectrum.points() if win.spectrum.spec is not None else []
    if st is None or peak is None or not points:
        QMessageBox.information(win, "Register unknown", "Select a peak with a mass spectrum first (a scan "
                                                         "spectrum must lie inside an integrated peak).")
        return
    if not R.significant_ions(points):
        QMessageBox.warning(win, "Register unknown", "The spectrum has no significant ions.")
        return
    spec = win.spectrum.spec
    quant = ws.quant_rows(st.id)
    res = ws.active_result()
    idx = res.peaks.index(peak) if res is not None and peak in res.peaks else -1
    ident = st.ident_set(ws.signal_key).for_peak(peak)
    mig = ws.quant.get("migration") or {}
    item = {
        "sample": st.run.path.name, "sample_name": st.name, "report_type": "GC Workspace",
        "rt": float(peak.apex_rt), "si": ident.score if ident else None,
        "name_raw": ident.name if ident else "", "area_pct": peak.area_pct,
        "conc_kg": quant.get(idx, {}).get("conc") if ws.quant.get("mode") == "nias_mgkg" else None,
        "spectrum": points, "source_file": str(st.run.path),
        "apex_scan": spec.apex_scans[len(spec.apex_scans) // 2] if spec.apex_scans else None,
        "bg_scan": spec.bg_scans[0] if spec.bg_scans else None, "bounds_rule": spec.mode,
        "deconvoluted": spec.mode == "deconvoluted", "kind": "component" if spec.mode == "deconvoluted" else "measured",
        "manual": True, "analyst": mig.get("analyst", ""), "simulant": mig.get("simulant", ""),
        "date": (st.run.meta.acquired or "")[:10] if st.run.meta else "",
    }
    dlg = SaveUnknownDialog(item, win)
    if dlg.exec() != QDialog.Accepted:
        return
    confirmed = dlg.result_item()
    if dlg.with_tic.isChecked() and st.run.ms is not None:
        ms = st.run.ms
        from gcws.core.keys import is_fid
        t = peak.apex_rt - (st.delay_value if is_fid(ws.signal_key) else 0.0)
        sl = ms.scans_between(t - 0.6, t + 0.6)
        confirmed["tic"] = ([float(x) for x in ms.rt[sl]], [int(v) for v in ms.stored_tic[sl]])
    try:
        written = gc_export.write_unknowns(db_path(), [confirmed])
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(win, "Register unknown", str(exc))
        return
    ws.log("Unknown registered", st.name, f"RT {peak.apex_rt:.3f}", "", str(written))
    win.statusBar().showMessage(f"Register: {written.get('entries', 0)} new entry, "
                                f"{written.get('sightings', 0)} sighting(s)", 8000)


COLUMNS = (("unknown_id", "ID"), ("label", "Label"), ("assigned_name", "Name"), ("assigned_cas", "CAS"),
           ("status", "Status"), ("n_sightings", "Sightings"), ("rt_mean", "RT"), ("ranked_mz", "m/z"),
           ("note", "Note"))
C_MARK = 0                        # the mark box; the register columns follow


class RegisterWindow(QDialog):
    """Find unknowns (text, sample name, m/z values), look them up in the libraries or NIST,
    mark them and share them as MSP."""

    def __init__(self, parent=None):
        super().__init__(parent)
        import gc_register as R
        from gcws.identify import register_search as RS
        self.R, self.RS = R, RS
        self.win = parent
        self.setWindowTitle("Unknown register")
        self.resize(1300, 780)
        self.con = R.connect(db_path())
        R.create_schema(self.con)
        self.marked: set[int] = set()
        self._spectra: dict = {}                  # entry id -> best spectrum (m/z search cache)
        self.mode = QComboBox()
        for k, v in RS.MODES.items():
            self.mode.addItem(v, k)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.search = QLineEdit()
        self.search.textChanged.connect(lambda *_: self._timer.start())
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.reload)
        from PySide6.QtWidgets import QDoubleSpinBox
        self.min_rel = QDoubleSpinBox()
        self.min_rel.setRange(0.1, 100)
        self.min_rel.setValue(5.0)
        self.min_rel.setSuffix(" %")
        self.min_rel.setPrefix("each ≥ ")
        self.min_rel.setToolTip("Every given ion must reach this share of the base peak")
        self.min_rel.valueChanged.connect(lambda *_: self._timer.start())
        self.base_first = QCheckBox("first = base peak")
        self.base_first.setToolTip("The first m/z given must be the base peak of the spectrum")
        self.base_first.toggled.connect(lambda *_: self._timer.start())
        self.only_spec = QCheckBox("only with spectrum")
        self.only_spec.toggled.connect(self.reload)
        folder = QPushButton("Open folder")
        folder.clicked.connect(lambda: os.startfile(str(db_path().parent)))
        top = QHBoxLayout()
        top.addWidget(QLabel("Find"))
        top.addWidget(self.mode)
        top.addWidget(self.search, 1)
        top.addWidget(self.min_rel)
        top.addWidget(self.base_first)
        top.addWidget(self.only_spec)
        top.addWidget(folder)

        self.table = QTableWidget(0, len(COLUMNS) + 1)
        self.table.setHorizontalHeaderLabels(["✓"] + [c[1] for c in COLUMNS])
        self.table.horizontalHeaderItem(C_MARK).setToolTip("Marked entries are exported / searched together")
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.currentCellChanged.connect(lambda *_: self._show())
        self.table.itemChanged.connect(self._mark_changed)
        mark_all = QPushButton("Mark all shown")
        mark_all.clicked.connect(lambda: self.set_marks(self.shown_ids(), True))
        mark_sel = QPushButton("Mark selected")
        mark_sel.clicked.connect(lambda: self.set_marks(self.selected_ids(), True))
        clear = QPushButton("Clear marks")
        clear.clicked.connect(lambda: self.set_marks(list(self.marked), False))
        export = QPushButton("Export to MSP...")
        export.setToolTip("The marked entries (or the selected ones when none is marked) as an MSP file, to "
                          "share with colleagues or search in another program")
        export.clicked.connect(self.export_msp)
        copy = QPushButton("Copy MSP")
        copy.setToolTip("The marked (or selected) entries as MSP text on the clipboard")
        copy.clicked.connect(self.copy_msp)
        left_buttons = QHBoxLayout()
        for b in (mark_all, mark_sel, clear):
            left_buttons.addWidget(b)
        left_buttons.addStretch(1)
        left_buttons.addWidget(copy)
        left_buttons.addWidget(export)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.table, 1)
        ll.addLayout(left_buttons)

        self.plot = StickPlot()
        self.sightings = QTableWidget(0, 5)
        self.sightings.setHorizontalHeaderLabels(["Sample", "RT", "Report", "Date", "mg/kg"])
        self.sightings.verticalHeader().setVisible(False)
        lib = QPushButton("Library search")
        lib.setToolTip("Hit list of this unknown in your libraries (default search method)")
        lib.clicked.connect(self.library_search)
        own = QPushButton("Own library")
        own.setToolTip("Search this unknown in the library chosen for 'Own library' (Identify > Own library "
                       "search options...)")
        own.clicked.connect(self.own_library_search)
        nist = QPushButton("NIST search")
        nist.setToolTip("Send this unknown's spectrum to NIST MS Search")
        nist.clicked.connect(self.nist_search)
        to_lib = QPushButton("Add to library...")
        to_lib.setToolTip("Store this spectrum in one of your libraries (Edit library)")
        to_lib.clicked.connect(self.add_to_library)
        edit = QPushButton("Edit...")
        edit.clicked.connect(self._edit)
        delete = QPushButton("Delete entry")
        delete.clicked.connect(self._delete)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.plot, 3)
        rl.addWidget(self.sightings, 2)
        h = QHBoxLayout()
        for b in (lib, own, nist, to_lib):
            h.addWidget(b)
        h.addStretch(1)
        h.addWidget(edit)
        h.addWidget(delete)
        rl.addLayout(h)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([740, 560])
        self.status = QLabel()
        self.status.setObjectName("hint")
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addWidget(self.status)
        self.entries = []
        self._mode_changed()

    # -- finding -----------------------------------------------------------------------------

    def _mode_changed(self, *_):
        mode = self.mode.currentData()
        self.search.setPlaceholderText({"text": "ID, name, CAS, m/z list, note ...",
                                        "sample": "sample name (part of it), e.g. 26016605 or GIOSUN",
                                        "mz": "ions, e.g. 149 167 279 (every one must be in the spectrum)"}[mode])
        for w in (self.min_rel, self.base_first):
            w.setVisible(mode == "mz")
        self.reload()

    def reload(self):
        if self.con is None:
            return
        self.entries = self.RS.search(self.con, self.mode.currentData(), self.search.text(),
                                      min_rel=self.min_rel.value(), base_first=self.base_first.isChecked(),
                                      with_spectra_only=self.only_spec.isChecked(), spectra=self._spectra)
        cur = self._current_id()
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for e in self.entries:
            r = self.table.rowCount()
            self.table.insertRow(r)
            mark = QTableWidgetItem()
            mark.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            mark.setCheckState(Qt.Checked if e["entry_id"] in self.marked else Qt.Unchecked)
            mark.setData(Qt.UserRole, e["entry_id"])
            self.table.setItem(r, C_MARK, mark)
            for c, (key, _) in enumerate(COLUMNS, 1):
                v = e.get(key)
                it = QTableWidgetItem()
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    it.setData(Qt.DisplayRole, round(v, 3) if isinstance(v, float) else v)
                else:
                    it.setText("" if v is None else str(v))
                it.setData(Qt.UserRole, e["entry_id"])
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        if cur is not None:
            self.select_entry(cur)
        counts = self.R.register_counts(self.con)
        self._status(f"{len(self.entries)} shown, {len(self.marked)} marked  -  {db_path()}  -  "
                     + ", ".join(f"{k}: {v}" for k, v in counts.items()))

    def _status(self, text):
        self.status.setText(text)

    def select_entry(self, entry_id) -> bool:
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.UserRole) == entry_id:
                self.table.setCurrentCell(r, 1)
                return True
        return False

    # -- marking -------------------------------------------------------------------------------

    def _mark_changed(self, item):
        if item.column() != C_MARK:
            return
        eid = item.data(Qt.UserRole)
        (self.marked.add if item.checkState() == Qt.Checked else self.marked.discard)(eid)
        self._status(f"{len(self.entries)} shown, {len(self.marked)} marked")

    def set_marks(self, ids, on: bool):
        ids = set(ids)
        self.marked = (self.marked | ids) if on else (self.marked - ids)
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it = self.table.item(r, C_MARK)
            it.setCheckState(Qt.Checked if it.data(Qt.UserRole) in self.marked else Qt.Unchecked)
        self.table.blockSignals(False)
        self._status(f"{len(self.entries)} shown, {len(self.marked)} marked")

    def shown_ids(self) -> list[int]:
        return [self.table.item(r, 0).data(Qt.UserRole) for r in range(self.table.rowCount())]

    def selected_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).data(Qt.UserRole) for r in rows]

    def _share_ids(self) -> list[int]:
        """What export / copy act on: the marked entries, else the selected ones."""
        return sorted(self.marked) if self.marked else self.selected_ids()

    # -- sharing ------------------------------------------------------------------------------------

    def export_msp(self, path: str = ""):
        ids = self._share_ids()
        if not ids:
            QMessageBox.information(self, "Export to MSP", "Mark or select the unknowns to export.")
            return
        if not path:
            from PySide6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getSaveFileName(self, "Export unknowns", str(db_path().parent / "unknowns.msp"),
                                                  "MSP spectra (*.msp)")
        if not path:
            return
        n = self.RS.export_msp(self.con, ids, path)
        skipped = len(ids) - n
        self._status(f"{n} unknowns written to {path}" + (f" ({skipped} without spectrum left out)" if skipped else ""))
        return n

    def copy_msp(self):
        from PySide6.QtGui import QGuiApplication
        ids = self._share_ids()
        text = self.RS.msp_text(self.con, ids) if ids else ""
        if text:
            QGuiApplication.clipboard().setText(text)
            self._status(f"{text.count('Num Peaks')} unknowns copied as MSP")

    # -- looking up ----------------------------------------------------------------------------------

    def current_spectrum(self):
        eid = self._current_id()
        if eid is None:
            return None, "", []
        row = self.R.entry_row(self.con, eid) or {}
        name = " ".join(x for x in (row.get("unknown_id"), row.get("assigned_name") or row.get("label")) if x)
        return eid, name or f"#{eid}", self.R.best_spectrum_for_entry(self.con, eid) or []

    def library_search(self, method=None):
        from gcws.identify.service import search_methods
        from gcws.ui.dialogs.identify import AtlasHitsDialog
        eid, name, spec = self.current_spectrum()
        if not spec:
            QMessageBox.information(self, "Library search", "Select an unknown with a spectrum.")
            return None
        points = [(float(m), float(a)) for m, a in spec]
        method = method or search_methods().for_gc_method("")
        dlg = AtlasHitsDialog(points, name, method, self, on_assign=lambda hits, i, eid=eid: self._assign(eid, hits[i]))
        dlg.show()
        return dlg

    def own_library_search(self):
        from gcws.ui.dialogs import own_search as OS
        opts = OS.load_options()
        if opts["library"] not in OS.libraries():
            dlg = OS.OwnSearchOptionsDialog(self)
            if dlg.exec() != QDialog.Accepted or not dlg.values()["library"]:
                return None
            opts = OS.load_options()
        return self.library_search(OS.method_from_options(opts))

    def _assign(self, eid, hit):
        """A library hit becomes the entry's name and CAS (status: in progress)."""
        fields = {"assigned_name": str(hit.get("name") or ""), "assigned_cas": str(hit.get("cas") or "")}
        row = self.R.entry_row(self.con, eid) or {}
        if (row.get("status") or "offen") == "offen":
            fields["status"] = "in Arbeit"
        note = (row.get("note") or "").strip()
        tag = f"library hit {hit.get('name')} ({hit.get('library', '')}, score {hit.get('score', '')})"
        fields["note"] = f"{note}; {tag}" if note else tag
        self.R.update_entry(self.con, eid, **fields)
        self.reload()

    def nist_search(self):
        import gc_nist
        eid, name, spec = self.current_spectrum()
        if not spec:
            QMessageBox.information(self, "NIST MS Search", "Select an unknown with a spectrum.")
            return
        row = self.R.entry_row(self.con, eid) or {}
        try:
            gc_nist.search_spectrum([(float(m), int(a)) for m, a in spec], name, row.get("rt_mean"))
            self._status(f"{name} sent to NIST MS Search")
        except Exception as exc:  # noqa: BLE001 - shown to the analyst
            QMessageBox.warning(self, "NIST MS Search", str(exc))

    def add_to_library(self):
        from gcws.ui.dialogs.library_edit import EditLibraryDialog
        eid, name, spec = self.current_spectrum()
        if not spec or self.win is None:
            return
        row = self.R.entry_row(self.con, eid) or {}
        rt = row.get("rt_mean")
        entry = {"peaks": [(float(m), float(a)) for m, a in spec], "name": row.get("assigned_name") or "",
                 "cas": row.get("assigned_cas") or "", "rt": round(rt, 3) if isinstance(rt, float) else "",
                 "source": f"unknown register {row.get('unknown_id', '')}", "note": "from the unknown register"}
        dlg = EditLibraryDialog(self.win, entry)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()

    def _current_id(self):
        r = self.table.currentRow()
        it = self.table.item(r, 0) if r >= 0 else None
        return it.data(Qt.UserRole) if it else None

    def _show(self):
        eid = self._current_id()
        self.sightings.setRowCount(0)
        if eid is None:
            return
        spec = self.R.best_spectrum_for_entry(self.con, eid)
        points = spec or []
        ions = self.RS.parse_mz(self.search.text()) if self.mode.currentData() == "mz" else []
        title = str(self.table.item(self.table.currentRow(), 1).text())
        if ions:
            title += "  -  searched: " + ", ".join(str(m) for m in ions)
        self.plot.show_spectrum(np.array([p[0] for p in points]), np.array([p[1] for p in points]), title=title)
        for s in self.R.entry_spectra(self.con, eid):
            r = self.sightings.rowCount()
            self.sightings.insertRow(r)
            for c, key in enumerate(("sample", "rt", "report_type", "date_text", "conc_kg")):
                v = s.get(key)
                self.sightings.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))

    def _edit(self):
        eid = self._current_id()
        if eid is None:
            return
        row = self.R.entry_row(self.con, eid) or {}
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit {row.get('unknown_id', '')}")
        edits = {k: QLineEdit(str(row.get(k) or "")) for k in ("label", "assigned_name", "assigned_cas", "note")}
        status = QComboBox()
        status.addItems(list(self.R.ENTRY_STATUSES))
        status.setCurrentText(str(row.get("status") or "offen"))
        f = QFormLayout(dlg)
        for k, label in (("label", "Label"), ("assigned_name", "Name"), ("assigned_cas", "CAS"), ("note", "Note")):
            f.addRow(label, edits[k])
        f.addRow("Status", status)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec():
            fields = {k: e.text().strip() for k, e in edits.items()}
            fields["status"] = status.currentText()
            self.R.update_entry(self.con, eid, **fields)
            self.reload()

    def _delete(self):
        eid = self._current_id()
        if eid is None:
            return
        if QMessageBox.question(self, "Delete", "Delete this register entry and its sightings?") != QMessageBox.Yes:
            return
        self.R.delete_entry(self.con, eid)
        self.marked.discard(eid)
        self._spectra.pop(eid, None)
        self.reload()

    def closeEvent(self, ev):
        self._timer.stop()                      # a pending search must not run on the closed register
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass
        self.con = None
        super().closeEvent(ev)
