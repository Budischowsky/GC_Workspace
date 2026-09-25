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
    st, peak = ws.active, ws.selected_peak()
    points = win.spectrum.points() if win.spectrum.spec is not None else []
    if st is None or peak is None or not points:
        QMessageBox.information(win, "Register unknown", "Select a peak with a mass spectrum first.")
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
        t = peak.apex_rt - (st.delay_value if ws.signal_key == "FID" else 0.0)
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


class RegisterWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        import gc_register as R
        self.R = R
        self.setWindowTitle("Unknown register")
        self.resize(1250, 760)
        self.con = R.connect(db_path())
        R.create_schema(self.con)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search ID, name, CAS, m/z, note ...")
        self.search.textChanged.connect(lambda *_: self._timer.start())
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.reload)
        self.only_spec = QCheckBox("only with spectrum")
        self.only_spec.toggled.connect(self.reload)
        folder = QPushButton("Open folder")
        folder.clicked.connect(lambda: os.startfile(str(db_path().parent)))
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.only_spec)
        top.addWidget(folder)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([c[1] for c in COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.currentCellChanged.connect(lambda *_: self._show())
        self.plot = StickPlot()
        self.sightings = QTableWidget(0, 5)
        self.sightings.setHorizontalHeaderLabels(["Sample", "RT", "Report", "Date", "mg/kg"])
        self.sightings.verticalHeader().setVisible(False)
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
        h.addStretch(1)
        h.addWidget(edit)
        h.addWidget(delete)
        rl.addLayout(h)
        split = QSplitter()
        split.addWidget(self.table)
        split.addWidget(right)
        split.setSizes([700, 550])
        self.status = QLabel()
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addWidget(self.status)
        self.entries = []
        self.reload()

    def reload(self):
        self.entries = self.R.browse_entries(self.con, search=self.search.text(),
                                             with_spectra_only=self.only_spec.isChecked())
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for e in self.entries:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, (key, _) in enumerate(COLUMNS):
                v = e.get(key)
                it = QTableWidgetItem()
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    it.setData(Qt.DisplayRole, round(v, 3) if isinstance(v, float) else v)
                else:
                    it.setText("" if v is None else str(v))
                it.setData(Qt.UserRole, e["entry_id"])
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        counts = self.R.register_counts(self.con)
        self.status.setText(f"{db_path()}  -  " + ", ".join(f"{k}: {v}" for k, v in counts.items()))

    def _current_id(self):
        it = self.table.item(self.table.currentRow(), 0)
        return it.data(Qt.UserRole) if it else None

    def _show(self):
        eid = self._current_id()
        self.sightings.setRowCount(0)
        if eid is None:
            return
        spec = self.R.best_spectrum_for_entry(self.con, eid)
        points = spec or []
        self.plot.show_spectrum(np.array([p[0] for p in points]), np.array([p[1] for p in points]),
                                title=str(self.table.item(self.table.currentRow(), 1).text()))
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
        self.reload()

    def closeEvent(self, ev):
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(ev)
