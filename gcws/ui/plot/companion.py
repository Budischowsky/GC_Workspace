"""Companion trace: the other detector of the same runs, aligned in time.

Under an FID chromatogram it shows an MS trace (TIC, BPC, an EIC ...), above
an MS chromatogram the FID. Each run's trace is shifted by that run's FID-MS
delay so a compound sits at the same x in both panes; the x axes are linked.
The pane is read-only: a left click selects the primary peak at that time (or
shows the scan spectrum there when no peak is integrated), right-click and
right-drag request spectra exactly as in the primary plot.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal as QtSignal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from gcws.core.keys import is_fid
from gcws.ms.spectra import ScanRequest, to_ms
from gcws.ui import theme
from gcws.ui.plot.items import PeaksItem
from gcws.ui.plot.tools import ToolViewBox


def _pen(color, width=1.0, alpha=255, style=Qt.SolidLine):
    c = QColor(color)
    c.setAlpha(alpha)
    pen = pg.mkPen(c, width=width, style=style)
    pen.setCosmetic(True)
    return pen


class CompanionPane(QWidget):
    spectrumRequested = QtSignal(object)        # ScanRequest (MS time axis)
    keyChanged = QtSignal(str)

    def __init__(self, ws, tools, primary, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.tools = tools
        self.primary = primary
        self.key = "TIC"
        self.vb = ToolViewBox(tools, interactive=False)
        self.vb.clicked.connect(self._clicked)
        self.vb.spectrumRequested.connect(self._spectrum_request)
        self.vb.on_reset = primary.default_view
        self.plot = pg.PlotWidget(viewBox=self.vb)
        self.plot.setMenuEnabled(False)
        self.plot.getAxis("left").enableAutoSIPrefix(True)
        self.plot.showGrid(x=True, y=True, alpha=theme.PLOT["grid_alpha"])
        self.plot.setLabel("bottom", "RT", units="min")
        self.vb.setXLink(primary.vb)
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.peaks = PeaksItem()
        self.vb.addItem(self.peaks)
        self.cursor = pg.InfiniteLine(angle=90, movable=False, pen=_pen(theme.PLOT["cursor"], 1, 140, Qt.DotLine))
        self.vb.addItem(self.cursor, ignoreBounds=True)
        self.regions: list = []
        self._fit = QTimer(self)
        self._fit.setSingleShot(True)
        self._fit.setInterval(0)
        self._fit.timeout.connect(self.fit_y)
        self.vb.sigXRangeChanged.connect(lambda *_: self._fit.start())
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)

        self.title = QLabel()
        self.title.setObjectName("hint")
        self.combo = QComboBox()
        self.combo.setToolTip("Trace shown in this pane")
        self.combo.activated.connect(self._picked)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 0, 4, 0)
        bar.addWidget(self.title, 1)
        bar.addWidget(self.combo)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        lay.addLayout(bar)
        lay.addWidget(self.plot, 1)

    # -- keys -----------------------------------------------------------------

    def options(self) -> list[str]:
        """Traces of the *other* detector available for the active run."""
        st = self.ws.active
        if st is None:
            return []
        primary_fid = is_fid(self.ws.signal_key)
        keys = self.ws.signals_for(st) if hasattr(self.ws, "signals_for") else st.run.available_signals()
        return [k for k in keys if is_fid(k) != primary_fid]

    def available(self) -> bool:
        return bool(self.options())

    def _sync_combo(self):
        opts = self.options()
        if self.key not in opts:
            prefer = [k for k in opts if k in ("TIC", "FID")]
            self.key = prefer[0] if prefer else (opts[0] if opts else self.key)
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(opts)
        if any(not is_fid(k) for k in opts):
            self.combo.addItem("EIC ...")
        self.combo.setCurrentText(self.key)
        self.combo.blockSignals(False)
        self.combo.setVisible(len(opts) > 1 or "EIC ..." in [self.combo.itemText(i) for i in range(self.combo.count())])

    def _picked(self, i):
        text = self.combo.itemText(i)
        if text == "EIC ...":
            from PySide6.QtWidgets import QInputDialog
            import re
            val, ok = QInputDialog.getText(self, "Extracted ion chromatogram", "m/z (several: summed, e.g. 149, 57):")
            masses = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", val)] if ok else []
            if not masses:
                self._sync_combo()
                return
            from gcws.core.model import eic_key
            text = eic_key(masses)
            st = self.ws.active
            if st is not None:
                st.run.signal(text)            # compute and cache so it is listed
        self.set_key(text)

    def set_key(self, key: str):
        self.key = key
        self.refresh()
        self.keyChanged.emit(key)

    # -- geometry ---------------------------------------------------------------

    def shift(self, st) -> float:
        """x offset of this pane's trace for run ``st`` (onto the primary time axis)."""
        primary_fid = is_fid(self.ws.signal_key)
        mine_fid = is_fid(self.key)
        if primary_fid and not mine_fid:
            return st.delay_value
        if mine_fid and not primary_fid:
            return -st.delay_value
        return 0.0

    # -- drawing -----------------------------------------------------------------

    def refresh(self, *_):
        self._sync_combo()
        active = self.ws.active
        states = [active] if (self.primary.detail and active) else self.ws.states()
        if not self.primary.detail and not self.primary.others.isChecked():
            states = [active] if active else []
        visible = [s for s in states if s is not None and (s.visible or s is active)]
        shown = set()
        n = len(visible)
        for rank, st in enumerate(visible):
            sig = st.run.signal(self.key)
            if sig is None:
                continue
            sc, off = self.primary._transform(st, sig, rank, n, key=self.key)
            is_active = active is not None and st.id == active.id
            curve = self.curves.get(st.id)
            if curve is None:
                curve = pg.PlotDataItem()
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                self.vb.addItem(curve)
                self.curves[st.id] = curve
            curve.setData(sig.rt + self.shift(st), sig.y * sc + off)
            curve.setPen(_pen(st.color, 1.6 if is_active else 1.0, 255 if is_active else 140))
            curve.setZValue(10 if is_active else 1)
            if is_active:
                self._transform = (sc, off)
            shown.add(st.id)
        for rid in list(self.curves):
            if rid not in shown:
                self.vb.removeItem(self.curves.pop(rid))
        self.refresh_peaks()
        d = active.delay_value if active is not None else 0.0
        if is_fid(self.key) != is_fid(self.ws.signal_key):
            frame = "MS" if is_fid(self.key) else "FID"
            self.title.setText(f"{self.key}  ·  shifted {self.shift(active) if active else 0.0:+.3f} min "
                               f"onto the {frame} time axis")
        else:
            self.title.setText(self.key)
        self._fit.start()

    def refresh_peaks(self, *_):
        st = self.ws.active
        sig = st.run.signal(self.key) if st is not None else None
        res = self.ws.result(st.id, self.key) if sig is not None else None
        if st is None or sig is None or res is None:
            self.peaks.set_data(np.zeros(0), np.zeros(0), [], "#000")
            return
        dx = self.shift(st)
        sel = -1
        p = self.ws.selected_peak()
        if p is not None:
            t = p.apex_rt - dx                      # the primary apex on this trace's own axis
            hit = res.peak_at(t)
            sel = res.peaks.index(hit) if hit is not None else -1
        self.peaks.set_data(sig.rt, sig.y, res.peaks, st.color, sel, getattr(self, "_transform", (1.0, 0.0)), dx=dx)

    def fit_y(self):
        """y range from the visible part after the integration start (the solvent front is ignored)."""
        (x0, x1), _ = self.vb.viewRange()
        st = self.ws.active
        t_from = None
        if st is not None:
            sig = st.run.signal(self.key)
            if sig is not None:
                from gcws.integration.autoparams import _integration_start
                t = _integration_start(sig.rt, self.ws.method_for(st, self.key))
                t_from = t + self.shift(st) if t is not None else None
        lo = hi = None
        for curve in self.curves.values():
            xs, ys = curve.getData()
            if xs is None or len(xs) == 0:
                continue
            m = (xs >= x0) & (xs <= x1)
            if t_from is not None and (xs[m] >= t_from).any():
                m &= xs >= t_from
            if not m.any():
                continue
            a, b = float(np.min(ys[m])), float(np.max(ys[m]))
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
        if lo is None:
            return
        pad = 0.06 * (hi - lo or 1.0)
        self.vb.setYRange(lo - pad, hi + pad, padding=0)

    def set_regions(self, regions):
        """Apex/background shading, [(t0, t1, colour)] already on the primary axis."""
        for it in self.regions:
            self.vb.removeItem(it)
        self.regions = []
        for t0, t1, color in regions:
            c = QColor(color)
            reg = pg.LinearRegionItem((t0, t1), movable=False, brush=pg.mkBrush(c.red(), c.green(), c.blue(), 45),
                                      pen=pg.mkPen(None))
            reg.setZValue(-5)
            self.vb.addItem(reg, ignoreBounds=True)
            self.regions.append(reg)

    # -- mouse ---------------------------------------------------------------------

    def _mouse_moved(self, pos):
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        x = self.vb.mapSceneToView(pos).x()
        self.primary.set_cursor(x)

    def set_cursor(self, x: float):
        self.cursor.setPos(x)

    def _clicked(self, x, y):
        idx = self.tools.peak_index_at(x)
        if idx >= 0:
            self.ws.select_peak(idx)
            return
        self._spectrum_request(x, x, None, (x, y))

    def _spectrum_request(self, t0, t1, bg, xy):
        from gcws.ui.plot.chrom import nearest_curve
        rid = nearest_curve(self.vb, self.curves, *xy, self.ws.active_id) if xy is not None else self.ws.active_id
        st = self.ws.runs.get(rid) if rid else None
        if st is None or st.run.ms is None:
            self.ws.message.emit("No MS data for a spectrum here")
            return
        key, d = self.ws.signal_key, st.delay_value
        conv = lambda t: None if t is None else to_ms(t, key, d)
        bg_ms = (conv(bg[0]), conv(bg[1])) if bg is not None else None
        self.spectrumRequested.emit(ScanRequest(st.id, conv(t0), conv(t1), bg_ms))
