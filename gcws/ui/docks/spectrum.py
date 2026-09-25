"""Mass spectrum of the selected peak, m/z table and library hits."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from gcws.core.keys import is_fid
from gcws.ms.spectra import MODES, extract, ms_times


class StickPlot(pg.PlotWidget):
    def __init__(self):
        super().__init__()
        self.setMenuEnabled(False)
        self.setLabel("bottom", "m/z")
        self.setLabel("left", "rel. abundance")
        self.showGrid(y=True, alpha=0.15)
        self.getPlotItem().getViewBox().setMouseMode(pg.ViewBox.RectMode)
        self.texts = []

    def show_spectrum(self, mz, ab, ref=None, title=""):
        self.clear()
        for t in self.texts:
            self.removeItem(t)
        self.texts = []
        if mz is None or len(mz) == 0:
            self.setTitle(title or "no spectrum")
            return
        ab = np.asarray(ab, float)
        rel = ab / ab.max() * 100.0
        self.addItem(pg.BarGraphItem(x=mz, height=rel, width=0.6, brush="#1f5f99", pen=None))
        order = np.argsort(rel)[::-1][:8]
        for i in order:
            t = pg.TextItem(str(int(mz[i])), color="#1f5f99", anchor=(0.5, 1))
            t.setPos(float(mz[i]), float(rel[i]))
            self.addItem(t)
            self.texts.append(t)
        if ref:
            rmz = np.array([p[0] for p in ref], float)
            rab = np.array([p[1] for p in ref], float)
            if rab.size and rab.max() > 0:
                rrel = rab / rab.max() * 100.0
                self.addItem(pg.BarGraphItem(x=rmz, y0=0, height=-rrel, width=0.6, brush="#b03a2e", pen=None))
                for i in np.argsort(rrel)[::-1][:6]:
                    t = pg.TextItem(str(int(rmz[i])), color="#b03a2e", anchor=(0.5, 0))
                    t.setPos(float(rmz[i]), float(-rrel[i]))
                    self.addItem(t)
                    self.texts.append(t)
        self.setTitle(title, size="9pt")
        vb = self.getPlotItem().getViewBox()
        vb.setRange(xRange=(float(min(mz)) - 5, float(max(mz)) + 5),
                    yRange=(-118 if ref else 0, 118), padding=0)


class SpectrumDock(QWidget):
    regionsChanged = QtSignal(list)           # [(t0, t1, colour)] on the displayed signal axis
    nistRequested = QtSignal(list, str)
    atlasRequested = QtSignal(list, str)
    registerRequested = QtSignal()
    investigateRequested = QtSignal()

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.spec = None
        self.mode = QComboBox()
        for k, v in MODES.items():
            self.mode.addItem(v, k)
        self.mode.currentIndexChanged.connect(lambda *_: self.refresh())
        b_atlas = QToolButton()
        b_atlas.setText("EI Atlas")
        b_atlas.setToolTip("Hit list of this spectrum in EI Atlas")
        b_atlas.clicked.connect(lambda: self._emit(self.atlasRequested))
        b_nist = QToolButton()
        b_nist.setText("NIST")
        b_nist.setToolTip("Send this spectrum to NIST MS Search")
        b_nist.clicked.connect(lambda: self._emit(self.nistRequested))
        b_copy = QToolButton()
        b_copy.setText("Copy MSP")
        b_copy.clicked.connect(self.copy_msp)
        b_save = QToolButton()
        b_save.setText("Save MSP...")
        b_save.clicked.connect(self.save_msp)
        b_res = QToolButton()
        b_res.setText("Investigate")
        b_res.setToolTip("Full EI Atlas investigation (native window)")
        b_res.clicked.connect(self.investigateRequested.emit)
        b_reg = QToolButton()
        b_reg.setText("Register unknown")
        b_reg.clicked.connect(self.registerRequested.emit)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.mode, 1)
        for b in (b_atlas, b_res, b_nist, b_copy, b_save, b_reg):
            top.addWidget(b)

        self.plot = StickPlot()
        self.info = QLabel()
        self.info.setStyleSheet("color:#666;")

        # scan selection
        self.scan_plot = pg.PlotWidget()
        self.scan_plot.setMenuEnabled(False)
        self.scan_plot.setMaximumHeight(140)
        self.scan_plot.setLabel("bottom", "MS RT", units="min")
        self.apex_reg = pg.LinearRegionItem(brush=pg.mkBrush(31, 95, 153, 50))
        self.bg_reg = pg.LinearRegionItem(brush=pg.mkBrush(176, 58, 46, 40))
        self.scan_curve = pg.PlotDataItem(pen=pg.mkPen("#555555"))
        for it in (self.scan_curve, self.apex_reg, self.bg_reg):
            self.scan_plot.addItem(it)
        use = QPushButton("Use these scans")
        use.setToolTip("Blue: scans averaged, red: background scans subtracted")
        use.clicked.connect(self._use_regions)
        auto = QPushButton("Automatic")
        auto.clicked.connect(self._clear_override)
        sl = QVBoxLayout()
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.scan_plot)
        h = QHBoxLayout()
        h.addWidget(QLabel("Drag the regions to choose apex (blue) and background (red) scans"))
        h.addStretch(1)
        h.addWidget(auto)
        h.addWidget(use)
        sl.addLayout(h)
        scans = QWidget()
        scans.setLayout(sl)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["m/z", "Abundance", "Rel. %"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.hits = QTableWidget(0, 6)
        self.hits.setHorizontalHeaderLabels(["Name", "CAS", "Score", "Fwd", "Rev", "Library"])
        self.hits.verticalHeader().setVisible(False)
        self.hits.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.hits.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hits.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.hits.currentCellChanged.connect(lambda *_: self._show_hit())

        self.tabs = QTabWidget()
        self.tabs.addTab(self.hits, "Library hits")
        self.tabs.addTab(self.table, "m/z table")
        self.tabs.addTab(scans, "Scans")
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.plot)
        split.addWidget(self.tabs)
        split.setSizes([420, 200])
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(top)
        lay.addWidget(self.info)
        lay.addWidget(split, 1)

        for sig in (ws.selectionChanged, ws.activeRunChanged, ws.signalKeyChanged, ws.identsChanged):
            sig.connect(lambda *_: self.refresh())
        ws.resultChanged.connect(lambda rid, key: self.refresh() if rid == ws.active_id else None)

    # -- data -----------------------------------------------------------------

    def current_mode(self) -> str:
        return self.mode.currentData()

    def refresh(self):
        st = self.ws.active
        peak = self.ws.selected_peak()
        self.hits.setRowCount(0)
        if st is None or peak is None or st.run.ms is None:
            self.spec = None
            self.plot.show_spectrum(None, None, title="select a peak" if st and st.run.ms else "no MS data")
            self.info.setText("")
            self.table.setRowCount(0)
            self.regionsChanged.emit([])
            return
        key = self.ws.signal_key
        override = st.spectrum_overrides.get(round(peak.apex_rt, 4))
        self.spec = extract(st.run, peak, key, st.delay_value, self.current_mode(), override=override)
        ident = st.ident_set(key).for_peak(peak)
        title = f"RT {peak.apex_rt:.3f}" + (f"  (MS {self.spec.rt:.3f})" if is_fid(key) else "")
        if ident and ident.name:
            title += f"  -  {ident.name}"
        self.plot.show_spectrum(self.spec.mz, self.spec.ab, title=title)
        self.info.setText(self.spec.note)
        self._fill_table()
        if ident is not None:
            for h in ident.hits:
                r = self.hits.rowCount()
                self.hits.insertRow(r)
                vals = [h.get("name", ""), h.get("cas", ""), h.get("score"), h.get("forward") or h.get("mf"),
                        h.get("reverse") or h.get("rmf"), h.get("library", "")]
                for c, v in enumerate(vals):
                    item = QTableWidgetItem("" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v)))
                    if c == 0 and ident.name and h.get("name") == ident.name:
                        item.setForeground(QColor("#1e8449"))
                    self.hits.setItem(r, c, item)
        self._update_regions(st, peak, key)

    def _fill_table(self):
        self.table.setRowCount(0)
        if self.spec is None or self.spec.ab.size == 0:
            return
        mx = self.spec.ab.max()
        for m, a in zip(self.spec.mz, self.spec.ab):
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, v in enumerate((str(int(m)), f"{a:.0f}", f"{100 * a / mx:.1f}")):
                it = QTableWidgetItem(v)
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, it)

    def _show_hit(self):
        r = self.hits.currentRow()
        st = self.ws.active
        peak = self.ws.selected_peak()
        if r < 0 or st is None or peak is None or self.spec is None:
            return
        ident = st.ident_set(self.ws.signal_key).for_peak(peak)
        if ident is None or r >= len(ident.hits):
            return
        h = ident.hits[r]
        ref = h.get("peaks") or []
        self.plot.show_spectrum(self.spec.mz, self.spec.ab, ref=ref,
                                title=f"unknown (blue) vs {h.get('name', '')} (red)")

    def _update_regions(self, st, peak, key):
        ms = st.run.ms
        t0, t1, ta = ms_times(peak, key, st.delay_value)
        w = max(t1 - t0, 0.02)
        sl = (ms.rt >= t0 - w) & (ms.rt <= t1 + w)
        sig = st.run.signal("TIC")
        self.scan_curve.setData(ms.rt[sl], sig.y[sl] if sig is not None else ms.tic()[sl])
        spec = self.spec
        regions = []
        shift = st.delay_value if is_fid(key) else 0.0
        if spec and spec.apex_scans:
            a0, a1 = float(ms.rt[min(spec.apex_scans)]), float(ms.rt[max(spec.apex_scans)])
            self.apex_reg.setRegion((a0 - 0.001, a1 + 0.001))
            regions.append((a0 + shift - 0.001, a1 + shift + 0.001, "#1f5f99"))
        if spec and spec.bg_scans:
            b0, b1 = float(ms.rt[min(spec.bg_scans)]), float(ms.rt[max(spec.bg_scans)])
            self.bg_reg.setRegion((b0 - 0.001, b1 + 0.001))
            pre = [s for s in spec.bg_scans if s < min(spec.apex_scans or [0])]
            post = [s for s in spec.bg_scans if s > max(spec.apex_scans or [0])]
            for grp in (pre, post):
                if grp:
                    regions.append((float(ms.rt[min(grp)]) + shift - 0.001,
                                    float(ms.rt[max(grp)]) + shift + 0.001, "#b03a2e"))
        self.scan_plot.getPlotItem().getViewBox().autoRange()
        self.regionsChanged.emit(regions)

    def _use_regions(self):
        st = self.ws.active
        peak = self.ws.selected_peak()
        if st is None or peak is None:
            return
        ms = st.run.ms
        a0, a1 = self.apex_reg.getRegion()
        b0, b1 = self.bg_reg.getRegion()
        apex = [int(s) for s in ms.scans_between(a0, a1)]
        bg = [int(s) for s in ms.scans_between(b0, b1) if s not in apex]
        if not apex:
            return
        st.spectrum_overrides[round(peak.apex_rt, 4)] = {"apex_scans": apex, "bg_scans": bg}
        self.ws.log("Spectrum scans", st.name, f"peak {peak.apex_rt:.3f}: apex {apex[0]}-{apex[-1]}, "
                                               f"{len(bg)} background scans")
        self.refresh()

    def _clear_override(self):
        st = self.ws.active
        peak = self.ws.selected_peak()
        if st is not None and peak is not None and st.spectrum_overrides.pop(round(peak.apex_rt, 4), None):
            self.ws.log("Spectrum scans", st.name, f"peak {peak.apex_rt:.3f}: automatic")
            self.refresh()

    # -- export ----------------------------------------------------------------

    def points(self):
        return self.spec.points(min_permille=0.0) if self.spec is not None else []

    def spectrum_name(self) -> str:
        st = self.ws.active
        p = self.ws.selected_peak()
        if st is None or p is None:
            return "GC unknown"
        return f"{st.name} RT {p.apex_rt:.3f}"

    def _emit(self, signal):
        pts = self.points()
        if pts:
            signal.emit(pts, self.spectrum_name())

    def msp(self) -> str:
        import gc_atlas
        p = self.ws.selected_peak()
        return gc_atlas.msp_text({"spectrum": self.points(), "rt": p.apex_rt if p else None,
                                  "name": self.spectrum_name()})

    def copy_msp(self):
        if self.points():
            QGuiApplication.clipboard().setText(self.msp())
            self.ws.message.emit("Spectrum copied as MSP")

    def save_msp(self):
        if not self.points():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save spectrum", self.spectrum_name().replace(" ", "_") + ".msp",
                                              "MSP (*.msp)")
        if path:
            with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
                fh.write(self.msp())
