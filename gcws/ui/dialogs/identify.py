"""Library search dialogs: start, compound review, single-spectrum hit list."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QRadioButton,
                               QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from gcws.ms.spectra import MODES
from gcws.ui.docks.spectrum import StickPlot


class SearchStartDialog(QDialog):
    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Automatic library search")
        self.ws = ws
        from gcws.identify.service import search_methods
        self.store = search_methods()
        self.method = QComboBox()
        self.method.addItems(self.store.names())
        gc_method = ws.active.run.meta.method if ws.active and ws.active.run.meta else ""
        self.method.setCurrentText(self.store.for_gc_method(gc_method).name)
        self.active_only = QRadioButton("Active chromatogram")
        self.all_runs = QRadioButton(f"All loaded chromatograms ({len(ws.states())})")
        self.active_only.setChecked(True)
        self.mode = QComboBox()
        for k, v in MODES.items():
            self.mode.addItem(v, k)
        self.skip = QCheckBox("Skip peaks that already have a name")
        self.rescan = QCheckBox("Only peaks with a score below")
        self.rescan_limit = QSpinBox()
        self.rescan_limit.setRange(0, 100)
        self.rescan_limit.setValue(80)
        self.review = QCheckBox("Review hits before applying (compound table)")
        self.review.setChecked(True)
        from PySide6.QtCore import QSettings
        has_ms = any(s.run.ms is not None for s in ws.states())
        has_fid = any(s.run.fid is not None for s in ws.states())
        self.target_tic = QRadioButton("TIC peaks (the qualitative trace)")
        self.target_fid = QRadioButton("FID peaks (each spectrum from the MS at the FID peak's delay-corrected time)")
        self.target_tic.setEnabled(has_ms)
        self.target_fid.setEnabled(has_fid and has_ms)
        want = QSettings().value("search/target", "TIC")
        (self.target_fid if (want == "FID" and has_fid) or not has_ms else self.target_tic).setChecked(True)
        self.transfer = QCheckBox("Give the names also to the FID peaks at the same time (for the report)")
        self.transfer.setChecked(QSettings().value("search/transfer", True, type=bool))
        self.transfer.setEnabled(has_fid)
        self.target_tic.toggled.connect(lambda on: self.transfer.setVisible(on))
        self.transfer.setVisible(self.target_tic.isChecked())
        f = QFormLayout()
        f.addRow("Search method", self.method)
        f.addRow("Peaks", self.target_tic)
        f.addRow("", self.transfer)
        f.addRow("", self.target_fid)
        f.addRow("Scope", self.active_only)
        f.addRow("", self.all_runs)
        f.addRow("Spectrum", self.mode)
        f.addRow("", self.skip)
        r = QHBoxLayout()
        r.addWidget(self.rescan)
        r.addWidget(self.rescan_limit)
        r.addStretch(1)
        f.addRow("", r)
        f.addRow("", self.review)
        note = QLabel("ISTD peaks and names entered by hand are never overwritten. The search runs "
                      "locally in EI Atlas (started without a window if necessary).")
        note.setWordWrap(True)
        note.setObjectName("hint")
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Search")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addWidget(note)
        lay.addWidget(bb)

    def values(self) -> dict:
        from PySide6.QtCore import QSettings
        target = "TIC" if self.target_tic.isChecked() else "FID"
        QSettings().setValue("search/target", target)
        QSettings().setValue("search/transfer", self.transfer.isChecked())
        return {"method": self.store.get(self.method.currentText()),
                "target": target,
                "transfer": target == "TIC" and self.transfer.isChecked() and self.transfer.isEnabled(),
                "all": self.all_runs.isChecked(),
                "mode": self.mode.currentData(),
                "skip": self.skip.isChecked(),
                "rescan": self.rescan_limit.value() if self.rescan.isChecked() else None,
                "review": self.review.isChecked()}


class CompoundReview(QDialog):
    """Peak list with the hits of each peak and a head-to-tail comparison."""

    def __init__(self, items, min_score: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Library search - review")
        self.resize(1200, 720)
        self.items = items
        self.min_score = min_score
        self.peaks = QTableWidget(len(items), 6)
        self.peaks.setHorizontalHeaderLabels(["Apply", "Chromatogram", "RT", "Before", "Top hit", "Score"])
        self.peaks.verticalHeader().setVisible(False)
        self.peaks.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.peaks.setSelectionMode(QAbstractItemView.SingleSelection)
        self.peaks.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        for r, it in enumerate(items):
            job = it.job
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if job.apply else Qt.Unchecked)
            self.peaks.setItem(r, 0, chk)
            top = job.top or {}
            vals = [job.label, f"{job.rt:.3f}", job.before[0], top.get("name", job.error or "no hit"),
                    top.get("score")]
            for c, v in enumerate(vals, 1):
                cell = QTableWidgetItem("" if v is None else str(v))
                cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if c == 5 and v is not None and float(v) < min_score:
                    cell.setForeground(QColor("#b03a2e"))
                self.peaks.setItem(r, c, cell)
        self.peaks.resizeColumnsToContents()
        self.peaks.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.peaks.currentCellChanged.connect(lambda *_: self._show_peak())
        self.hits = QTableWidget(0, 5)
        self.hits.setHorizontalHeaderLabels(["Name", "CAS", "Score", "Formula", "Library"])
        self.hits.verticalHeader().setVisible(False)
        self.hits.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.hits.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hits.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.hits.currentCellChanged.connect(lambda *_: self._show_hit())
        choose = QPushButton("Use selected hit for this peak")
        choose.clicked.connect(self._choose)
        self.plot = StickPlot()
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.plot, 3)
        rl.addWidget(self.hits, 2)
        rl.addWidget(choose)
        split = QSplitter()
        split.addWidget(self.peaks)
        split.addWidget(right)
        split.setSizes([560, 640])
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Apply")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        info = QLabel(f"Hits below the quality limit ({min_score}) become 'possible derivative of ...' or "
                      f"'unknown' unless you pick a hit explicitly.")
        info.setObjectName("hint")
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addWidget(info)
        lay.addWidget(bb)
        if items:
            self.peaks.setCurrentCell(0, 1)

    def _show_peak(self):
        r = self.peaks.currentRow()
        self.hits.setRowCount(0)
        if r < 0:
            return
        job = self.items[r].job
        for h in job.hits:
            i = self.hits.rowCount()
            self.hits.insertRow(i)
            for c, v in enumerate((h.get("name"), h.get("cas"), h.get("score"), h.get("formula"), h.get("library"))):
                self.hits.setItem(i, c, QTableWidgetItem("" if v is None else str(v)))
        if job.hits:
            self.hits.setCurrentCell(job.chosen or 0, 0)
        else:
            self._plot(job, None)

    def _plot(self, job, hit):
        mz = np.array([p[0] for p in job.spectrum])
        ab = np.array([p[1] for p in job.spectrum])
        self.plot.show_spectrum(mz, ab, ref=(hit or {}).get("peaks"),
                                title=f"RT {job.rt:.3f}" + (f" vs {hit.get('name')}" if hit else ""))

    def _show_hit(self):
        r = self.peaks.currentRow()
        h = self.hits.currentRow()
        if r < 0:
            return
        job = self.items[r].job
        self._plot(job, job.hits[h] if 0 <= h < len(job.hits) else None)

    def _choose(self):
        r, h = self.peaks.currentRow(), self.hits.currentRow()
        if r < 0 or h < 0:
            return
        job = self.items[r].job
        job.chosen = h
        self.peaks.item(r, 4).setText(job.hits[h].get("name", "") + "  (chosen)")
        self.peaks.item(r, 5).setText(str(job.hits[h].get("score", "")))
        self.peaks.item(r, 0).setCheckState(Qt.Checked)

    def _accept(self):
        for r, it in enumerate(self.items):
            it.job.apply = self.peaks.item(r, 0).checkState() == Qt.Checked
        self.accept()


class BlanksDialog(QDialog):
    def __init__(self, ws, run_id, parent=None):
        super().__init__(parent)
        st = ws.runs[run_id]
        self.setWindowTitle(f"Blanks for {st.name}")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Chromatogram", "Blank", "Blank + ISTD"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.ids = []
        for other in ws.states():
            if other.id == run_id:
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.ids.append(other.id)
            self.table.setItem(r, 0, QTableWidgetItem(other.name))
            for c, lst in ((1, st.blanks), (2, st.blanks_istd)):
                it = QTableWidgetItem()
                it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                it.setCheckState(Qt.Checked if other.id in lst else Qt.Unchecked)
                self.table.setItem(r, c, it)
        note = QLabel("Suggested from the injection order: the solvent blank injected after the sample and the "
                      "Blank+ISTD injected before it. With several blanks of one kind the larger matched area is "
                      "subtracted.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addWidget(note)
        lay.addWidget(bb)
        self.resize(560, 360)

    def values(self):
        b, bi = [], []
        for r, rid in enumerate(self.ids):
            if self.table.item(r, 1).checkState() == Qt.Checked:
                b.append(rid)
            if self.table.item(r, 2).checkState() == Qt.Checked:
                bi.append(rid)
        return b, bi


class AtlasHitsDialog(QDialog):
    """EI Atlas hit list for one spectrum, with head-to-tail plot and 'Assign hit'."""

    def __init__(self, points, name, method, parent=None, on_assign=None):
        super().__init__(parent)
        self.setWindowTitle(f"EI Atlas - {name}")
        self.resize(980, 640)
        self.points = points
        self.on_assign = on_assign
        self.method = method
        self.hits_data = []
        self.plot = StickPlot()
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Name", "CAS", "Score", "Fwd", "Rev", "Library"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.currentCellChanged.connect(lambda *_: self._show())
        self.status = QLabel("Searching ...")
        assign = QPushButton("Assign hit to peak")
        assign.clicked.connect(self._assign)
        assign.setEnabled(on_assign is not None)
        lay = QVBoxLayout(self)
        lay.addWidget(self.plot, 3)
        lay.addWidget(self.table, 2)
        h = QHBoxLayout()
        h.addWidget(self.status, 1)
        h.addWidget(assign)
        lay.addLayout(h)
        self._show_unknown()
        from gcws.ui.workers import submit
        submit(self._search, on_done=self._done, on_error=lambda e: self.status.setText(f"Error: {e.splitlines()[0]}"))

    def _search(self):
        import gc_atlas
        import gc_search_method as SM
        from gcws.identify.service import prepare_server
        base = prepare_server(self.method)
        masses = [m for m, _ in self.points]
        rng = SM.mz_range(self.method, (int(min(masses)), int(max(masses)) + 1))
        result = gc_atlas.request(base, "/api/analyze", {
            "text": gc_atlas.msp_text({"spectrum": self.points, "name": self.windowTitle()}),
            "settings": SM.to_api_settings(self.method, rng)})
        return result.get("hits") or []

    def _done(self, hits):
        self.hits_data = hits
        self.status.setText(f"{len(hits)} hits  -  score = EI Atlas similarity (0-100), not a NIST match factor")
        for h in hits:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, k in enumerate(("name", "cas", "score", "forward", "reverse", "library")):
                v = h.get(k)
                self.table.setItem(r, c, QTableWidgetItem("" if v is None else
                                                          (f"{v:.1f}" if isinstance(v, float) else str(v))))
        if hits:
            self.table.setCurrentCell(0, 0)

    def _show_unknown(self):
        mz = np.array([p[0] for p in self.points])
        ab = np.array([p[1] for p in self.points])
        self.plot.show_spectrum(mz, ab, title="unknown")

    def _show(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(self.hits_data):
            return
        h = self.hits_data[r]
        mz = np.array([p[0] for p in self.points])
        ab = np.array([p[1] for p in self.points])
        self.plot.show_spectrum(mz, ab, ref=h.get("peaks"), title=f"unknown (blue) vs {h.get('name')} (red)")

    def _assign(self):
        r = self.table.currentRow()
        if r < 0 or self.on_assign is None:
            return
        self.on_assign(self.hits_data, r)
        self.accept()
