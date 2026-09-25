"""Chromatogram overlay and peak-zoom plots."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from gcws.integration.method import EventKind
from gcws.ui.plot.items import LabelsItem, PeaksItem
from gcws.ui.plot.tools import ToolViewBox

pg.setConfigOptions(antialias=True, background="w", foreground="#333333")


def _pen(color, width=1.0, alpha=255, style=Qt.SolidLine):
    c = QColor(color)
    c.setAlpha(alpha)
    pen = QPen(c)
    pen.setWidthF(width)
    pen.setCosmetic(True)
    pen.setStyle(style)
    return pen


class ChromPlot(QWidget):
    """Overlay of all visible runs; the active run is drawn bold with its peaks."""

    def __init__(self, ws, tools, detail: bool = False, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.detail = detail
        self.vb = ToolViewBox(tools)
        self.plot = pg.PlotWidget(viewBox=self.vb)
        self.plot.setLabel("bottom", "RT", units="min")
        self.plot.getAxis("left").enableAutoSIPrefix(True)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setMenuEnabled(False)
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.peaks = PeaksItem()
        self.labels = LabelsItem()
        self.vb.addItem(self.peaks)
        self.vb.addItem(self.labels, ignoreBounds=True)
        self.event_lines: list = []
        self.regions: list = []
        self.ms_regions: list = []
        self.cursor = pg.InfiniteLine(angle=90, movable=False, pen=_pen("#999999", 1, 120, Qt.DotLine))
        self.vb.addItem(self.cursor, ignoreBounds=True)
        self.cursor_label = pg.TextItem("", color="#555555", anchor=(0, 1))
        self.vb.addItem(self.cursor_label, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)
        self.vb.on_reset = self.default_view

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.title = QLabel()
        self.title.setStyleSheet("color:#555;")
        bar.addWidget(self.title, 1)
        if not detail:
            self.norm = QCheckBox("Normalize")
            self.norm.setToolTip("Scale every trace to its own maximum (after the integration start)")
            self.stack = QCheckBox("Stack")
            self.stack.setToolTip("Offset the traces vertically")
            self.others = QCheckBox("Overlay")
            self.others.setChecked(True)
            self.others.setToolTip("Show the other loaded chromatograms")
            for w in (self.others, self.norm, self.stack):
                w.toggled.connect(self.refresh)
                bar.addWidget(w)
        self.label_mode = QComboBox()
        self.label_mode.addItems(["Labels: RT", "Labels: #", "Labels: name", "Labels: off"])
        self.label_mode.currentIndexChanged.connect(self.refresh_labels)
        bar.addWidget(self.label_mode)
        lay.addLayout(bar)
        lay.addWidget(self.plot, 1)

        ws.runAdded.connect(lambda *_: self.refresh(autorange=True))
        ws.runRemoved.connect(lambda *_: self.refresh())
        ws.runChanged.connect(lambda *_: self.refresh())
        ws.activeRunChanged.connect(lambda *_: self.refresh(autorange=detail))
        ws.signalKeyChanged.connect(lambda *_: self.refresh(autorange=True))
        ws.resultChanged.connect(self._on_result)
        ws.identsChanged.connect(lambda *_: self.refresh_labels())
        ws.selectionChanged.connect(self._on_selection)
        ws.methodChanged.connect(lambda *_: self.refresh_events())

    # -- transforms ---------------------------------------------------------

    def _transform(self, st, sig, rank: int, n: int) -> tuple[float, float]:
        if self.detail:
            return (1.0, 0.0)
        sc, off = 1.0, 0.0
        if self.norm.isChecked():
            m = self.ws.method_for(st, self.ws.signal_key)
            from gcws.integration.autoparams import _integration_start
            t0 = _integration_start(sig.rt, m) or float(sig.rt[0])
            sel = sig.y[sig.rt >= t0]
            base = float(np.percentile(sel, 1)) if sel.size else 0.0
            top = float(sel.max()) if sel.size else 1.0
            sc = 100.0 / max(top - base, 1e-12)
            off = -base * sc
        if self.stack.isChecked() and n > 1:
            span = 100.0 if self.norm.isChecked() else self._span()
            off += rank * 0.12 * span
        return (sc, off)

    def _span(self) -> float:
        spans = []
        for st in self.ws.states():
            s = st.run.signal(self.ws.signal_key)
            if s is not None and s.n:
                spans.append(float(np.percentile(s.y, 99.5) - np.percentile(s.y, 1)))
        return max(spans) if spans else 1.0

    # -- drawing -------------------------------------------------------------

    def refresh(self, *_, autorange: bool = False):
        key = self.ws.signal_key
        active = self.ws.active
        states = [active] if (self.detail and active) else self.ws.states()
        if not self.detail and not self.others.isChecked():
            states = [active] if active else []
        shown = set()
        visible_states = [s for s in states if s is not None and (s.visible or s is active)]
        n = len(visible_states)
        for rank, st in enumerate(visible_states):
            sig = st.run.signal(key)
            if sig is None:
                continue
            sc, off = self._transform(st, sig, rank, n)
            is_active = active is not None and st.id == active.id
            curve = self.curves.get(st.id)
            if curve is None:
                curve = pg.PlotDataItem()
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                self.vb.addItem(curve)
                self.curves[st.id] = curve
            curve.setData(sig.rt, sig.y * sc + off)
            curve.setPen(_pen(st.color, 1.8 if is_active else 1.0, 255 if is_active else 150))
            curve.setZValue(10 if is_active else 1)
            if is_active:
                self.vb.transform = (sc, off)
            shown.add(st.id)
        for rid in list(self.curves):
            if rid not in shown:
                self.vb.removeItem(self.curves.pop(rid))
        self.refresh_active()
        self.refresh_events()
        if autorange:
            self.default_view()
        name = active.name if active else "no chromatogram loaded"
        self.title.setText(f"{key}  -  {name}" if active else name)

    def _on_result(self, run_id: str, key: str):
        if key == self.ws.signal_key and self.ws.active_id == run_id:
            self.refresh_active()
            self.refresh_events()

    def _on_selection(self, run_id: str, index: int):
        self.refresh_active()
        if self.detail:
            self.zoom_to_selected()

    def refresh_active(self):
        st = self.ws.active
        res = self.ws.active_result() if st else None
        sig = st.run.signal(self.ws.signal_key) if st else None
        if st is None or sig is None or res is None:
            self.peaks.set_data(np.zeros(0), np.zeros(0), [], "#000")
            self.labels.set_labels([])
            return
        self.peaks.set_data(sig.rt, sig.y, res.peaks, st.color, self.ws.selected, self.vb.transform)
        self.refresh_labels()

    def refresh_labels(self, *_):
        st = self.ws.active
        res = self.ws.active_result() if st else None
        mode = self.label_mode.currentIndex()
        if st is None or res is None or mode == 3:
            self.labels.set_labels([])
            return
        sig = st.run.signal(self.ws.signal_key)
        sc, off = self.vb.transform
        idents, _ = st.ident_set(self.ws.signal_key).bind(res.peaks)
        out = []
        for i, p in enumerate(res.peaks):
            y = float(np.interp(p.apex_rt, sig.rt, sig.y)) * sc + off
            if mode == 0:
                text = f"{p.apex_rt:.3f}"
            elif mode == 1:
                text = str(p.number)
            else:
                ident = idents.get(i)
                text = ident.name[:28] if ident and ident.name else f"{p.apex_rt:.3f}"
            out.append((p.apex_rt, y, text, i == self.ws.selected))
        self.labels.set_labels(out)

    def refresh_events(self):
        for it in self.event_lines + self.regions:
            self.vb.removeItem(it)
        self.event_lines, self.regions = [], []
        st = self.ws.active
        if st is None:
            return
        sig = st.run.signal(self.ws.signal_key)
        if sig is None:
            return
        m = self.ws.method_for(st, self.ws.signal_key)
        off_start = None
        for e in m.events():
            if e.kind == EventKind.INTEGRATOR_OFF:
                off_start = e.time
                continue
            if e.kind == EventKind.INTEGRATOR_ON and off_start is not None:
                reg = pg.LinearRegionItem((off_start, e.time), movable=False,
                                          brush=pg.mkBrush(120, 120, 120, 28), pen=pg.mkPen(None))
                reg.setZValue(-10)
                self.vb.addItem(reg, ignoreBounds=True)
                self.regions.append(reg)
                off_start = None
                continue
            line = pg.InfiniteLine(e.time, angle=90, movable=False,
                                   pen=_pen("#8e44ad", 1, 140, Qt.DashLine),
                                   label=e.kind.value, labelOpts={"position": 0.95, "color": "#8e44ad",
                                                                  "rotateAxis": (1, 0), "anchors": [(0, 0), (0, 0)]})
            self.vb.addItem(line, ignoreBounds=True)
            self.event_lines.append(line)
        if off_start is not None:
            reg = pg.LinearRegionItem((off_start, float(sig.rt[-1])), movable=False,
                                      brush=pg.mkBrush(120, 120, 120, 28), pen=pg.mkPen(None))
            self.vb.addItem(reg, ignoreBounds=True)
            self.regions.append(reg)

    def set_ms_regions(self, regions):
        """Shaded apex/background scan ranges (list of (t0, t1, color))."""
        for it in self.ms_regions:
            self.vb.removeItem(it)
        self.ms_regions = []
        for t0, t1, color in regions:
            c = QColor(color)
            reg = pg.LinearRegionItem((t0, t1), movable=False, brush=pg.mkBrush(c.red(), c.green(), c.blue(), 45),
                                      pen=pg.mkPen(None))
            reg.setZValue(-5)
            self.vb.addItem(reg, ignoreBounds=True)
            self.ms_regions.append(reg)

    def default_view(self):
        """Whole run on x; y scaled to the integrated part (the solvent front is
        usually far higher than everything the analyst works on)."""
        if self.detail and self.ws.selected_peak() is not None:
            self.zoom_to_selected()
            return
        st = self.ws.active
        sig = st.run.signal(self.ws.signal_key) if st else None
        if sig is None or sig.n < 2:
            self.vb.enableAutoRange()
            return
        from gcws.integration.autoparams import _integration_start
        t_from = _integration_start(sig.rt, self.ws.method_for(st, self.ws.signal_key))
        lo_y, hi_y = None, None
        for rid, curve in self.curves.items():
            x, y = curve.getData()
            if x is None or len(x) == 0:
                continue
            sel = y[x >= (t_from or x[0])] if t_from else y
            if sel.size == 0:
                continue
            a, b = float(np.min(sel)), float(np.max(sel))
            lo_y = a if lo_y is None else min(lo_y, a)
            hi_y = b if hi_y is None else max(hi_y, b)
        if lo_y is None:
            self.vb.enableAutoRange()
            return
        pad = 0.05 * (hi_y - lo_y or 1.0)
        self.vb.setRange(xRange=(float(sig.rt[0]), float(sig.rt[-1])), yRange=(lo_y - pad, hi_y + pad),
                         padding=0)

    def zoom_to_selected(self):
        p = self.ws.selected_peak()
        st = self.ws.active
        if p is None or st is None:
            return
        sig = st.run.signal(self.ws.signal_key)
        w = max(p.end - p.start, 0.02)
        t0, t1 = p.start - 1.5 * w, p.end + 1.5 * w
        sl = sig.window(t0, t1)
        if sl.stop - sl.start < 2:
            return
        seg = sig.y[sl]
        lo, hi = float(min(seg.min(), p.baseline.y0, p.baseline.y1)), float(seg.max())
        pad = 0.08 * (hi - lo or 1.0)
        self.vb.setRange(xRange=(t0, t1), yRange=(lo - pad, hi + pad), padding=0)

    def zoom_to(self, t0, t1):
        self.vb.setXRange(t0, t1, padding=0.02)

    def _mouse_moved(self, pos):
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        p = self.vb.mapSceneToView(pos)
        self.cursor.setPos(p.x())
        st = self.ws.active
        txt = f"{p.x():.3f} min"
        if st is not None:
            sig = st.run.signal(self.ws.signal_key)
            if sig is not None and sig.rt[0] <= p.x() <= sig.rt[-1]:
                txt += f"   {np.interp(p.x(), sig.rt, sig.y):.4g}"
        self.cursor_label.setText(txt)
        vr = self.vb.viewRange()
        self.cursor_label.setPos(p.x(), vr[1][1])
