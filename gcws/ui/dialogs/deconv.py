"""Spectral deconvolution around the selected peak (AMDIS-style, numpy)."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from gcws.core.events import ManualEvent, ManualKind as K
from gcws.core.keys import is_fid
from gcws.ms.spectra import ms_times
from gcws.ui.docks.spectrum import StickPlot
from gcws.ui.undo import ManualEventsCommand

COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2"]


class DeconvolutionDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        import gc_deconv
        self.gd = gc_deconv
        self.win = win
        self.ws = win.ws
        self.st = self.ws.active
        self.peak = self.ws.selected_peak()
        self.key = self.ws.signal_key
        self.setWindowTitle(f"Deconvolution - {self.st.name}, RT {self.peak.apex_rt:.3f}")
        self.resize(1150, 720)
        p = gc_deconv.DeconvParams()
        self.win_spin = QDoubleSpinBox()
        self.win_spin.setDecimals(3)
        self.win_spin.setValue(p.window)
        self.noise = QDoubleSpinBox()
        self.noise.setValue(p.noise_factor)
        self.shape = QDoubleSpinBox()
        self.shape.setDecimals(2)
        self.shape.setMaximum(1.0)
        self.shape.setValue(p.shape_r)
        self.min_ions = QSpinBox()
        self.min_ions.setValue(p.min_ions)
        run = QPushButton("Deconvolute")
        run.clicked.connect(self.run)
        f = QFormLayout()
        f.addRow("Window (± min)", self.win_spin)
        f.addRow("Noise factor", self.noise)
        f.addRow("Min. profile correlation", self.shape)
        f.addRow("Min. ions", self.min_ions)
        f.addRow("", run)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["RT (MS)", "Model m/z", "Purity", "Ions", "S/N", "Area", "In peak"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.currentCellChanged.connect(lambda *_: self._show())
        self.profiles = pg.PlotWidget()
        self.profiles.setLabel("bottom", "MS RT", units="min")
        self.profiles.setMenuEnabled(False)
        self.spec = StickPlot()
        split_btn = QPushButton("Split the peak between the components")
        split_btn.setToolTip("Drop lines at the midpoints between the component apexes inside the peak "
                             "(manual SPLIT events, undoable)")
        split_btn.clicked.connect(self.split)
        use = QPushButton("Search this component (EI Atlas)")
        use.clicked.connect(self.search)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addLayout(f)
        ll.addWidget(self.table, 1)
        h = QHBoxLayout()
        h.addWidget(split_btn)
        h.addWidget(use)
        ll.addLayout(h)
        right = QSplitter()
        right.setOrientation(pg.QtCore.Qt.Vertical)
        right.addWidget(self.profiles)
        right.addWidget(self.spec)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([520, 630])
        self.note = QLabel()
        self.note.setStyleSheet("color:#666;")
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addWidget(self.note)
        self.comps = []
        self.run()

    def params(self):
        return self.gd.DeconvParams(window=self.win_spin.value(), noise_factor=self.noise.value(),
                                    shape_r=self.shape.value(), min_ions=self.min_ions.value())

    def run(self):
        t0, t1, ta = ms_times(self.peak, self.key, self.st.delay_value)
        self.t0, self.t1 = t0, t1
        try:
            self.comps = self.gd.deconvolute(self.st.run.ms_source, ta, self.params())
        except Exception as exc:  # noqa: BLE001
            self.note.setText(f"Deconvolution failed: {exc}")
            self.comps = []
        self.table.setRowCount(0)
        self.profiles.clear()
        ms = self.st.run.ms
        sl = ms.scans_between(ta - self.win_spin.value(), ta + self.win_spin.value())
        self.profiles.plot(ms.rt[sl], ms.tic()[sl], pen=pg.mkPen("#999999"))
        reg = pg.LinearRegionItem((t0, t1), movable=False, brush=pg.mkBrush(0, 0, 0, 20))
        self.profiles.addItem(reg)
        for i, c in enumerate(self.comps):
            r = self.table.rowCount()
            self.table.insertRow(r)
            inside = "yes" if t0 <= c.rt <= t1 else ""
            for col, v in enumerate((f"{c.rt:.4f}", c.model_mz, f"{c.purity:.2f}", c.n_ions, f"{c.s_n:.0f}",
                                     f"{c.area:,.0f}", inside)):
                self.table.setItem(r, col, QTableWidgetItem(str(v)))
            if len(c.profile_rt):
                scale = 1.0
                self.profiles.plot(c.profile_rt, np.asarray(c.profile_y) * scale,
                                   pen=pg.mkPen(COLORS[i % len(COLORS)], width=2))
        n_in = sum(1 for c in self.comps if t0 <= c.rt <= t1)
        self.note.setText(f"{len(self.comps)} components in the window, {n_in} inside the integrated peak.")
        if self.comps:
            self.table.setCurrentCell(0, 0)

    def _show(self):
        r = self.table.currentRow()
        if not (0 <= r < len(self.comps)):
            return
        c = self.comps[r]
        mz = np.array([m for m, _ in c.spectrum], float)
        ab = np.array([v for _, v in c.spectrum], float)
        self.spec.show_spectrum(mz, ab, title=f"component {c.rt:.3f} min (model m/z {c.model_mz})")

    def split(self):
        inside = sorted((c for c in self.comps if self.t0 <= c.rt <= self.t1), key=lambda c: c.rt)
        if len(inside) < 2:
            self.note.setText("Fewer than two components inside the peak - nothing to split.")
            return
        shift = self.st.delay_value if is_fid(self.key) else 0.0
        events = list(self.st.events(self.key))
        for a, b in zip(inside, inside[1:]):
            events.append(ManualEvent(K.SPLIT, (a.rt + b.rt) / 2 + shift, comment="deconvolution"))
        self.st.undo.push(ManualEventsCommand(self.ws, self.st.id, self.key, events,
                                              f"split peak {self.peak.apex_rt:.3f} into {len(inside)} components"))
        self.accept()

    def search(self):
        r = self.table.currentRow()
        if not (0 <= r < len(self.comps)):
            return
        c = self.comps[r]
        points = [(int(m), float(v)) for m, v in c.spectrum]
        self.win.atlas_hits(points, f"{self.st.name} component {c.rt:.3f}")
