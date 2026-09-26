"""Chromatogram overlay and peak-zoom plots.

Each plot shows the working signal (``ws.signal_key``) of the loaded runs and,
with "FID + MS" switched on, a companion pane with the other detector aligned
in time (FID always on top, MS below; the x axes are linked).
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget

from gcws.core.keys import is_fid
from gcws.integration.method import EventKind
from gcws.ms.spectra import ScanRequest, from_ms, to_ms
from gcws.ui import theme
from gcws.ui.plot.items import LabelsItem, PeaksItem
from gcws.ui.plot.tools import ToolViewBox

theme.configure_plots()

#: fixed width of the y axes so stacked panes share the same x pixels
AXIS_WIDTH = 62


def _pen(color, width=1.0, alpha=255, style=Qt.SolidLine):
    c = QColor(color)
    c.setAlpha(alpha)
    pen = QPen(c)
    pen.setWidthF(width)
    pen.setCosmetic(True)
    pen.setStyle(style)
    return pen


def nearest_curve(vb, curves: dict, x: float, y: float, default):
    """Id of the curve passing nearest to (x, y) within 12 px, else ``default``."""
    px = abs(vb.viewPixelSize()[1]) or 1e-12
    best = None
    for rid, curve in curves.items():
        xs, ys = curve.getData()
        if xs is None or len(xs) < 2 or not (xs[0] <= x <= xs[-1]):
            continue
        d = abs(float(np.interp(x, xs, ys)) - y) / px
        if best is None or d < best[0]:
            best = (d, rid)
    if best is not None and best[0] <= 12:
        return best[1]
    return default


class ChromPlot(QWidget):
    """Overlay of all visible runs; the active run is drawn bold with its peaks."""
    spectrumRequested = QtSignal(object)        # ScanRequest (MS time axis)
    componentClicked = QtSignal(str, object)    # run id, deconvoluted Component

    def __init__(self, ws, tools, detail: bool = False, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.detail = detail
        self._prefix = "zoom" if detail else "chrom"
        self.vb = ToolViewBox(tools)
        self.vb.spectrumRequested.connect(self._spectrum_request)
        self._ms_regions_ms: list = []
        self.plot = pg.PlotWidget(viewBox=self.vb)
        self.plot.setLabel("bottom", "RT", units="min")
        self.plot.getAxis("left").enableAutoSIPrefix(True)
        self.plot.getAxis("left").setWidth(AXIS_WIDTH)
        self.plot.showGrid(x=True, y=True, alpha=theme.PLOT["grid_alpha"])
        self.plot.setMenuEnabled(False)
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.peaks = PeaksItem()
        self.labels = LabelsItem()
        self.vb.addItem(self.peaks)
        self.vb.addItem(self.labels, ignoreBounds=True)
        self.event_lines: list = []
        self.regions: list = []
        self.ms_regions: list = []
        self.cursor = pg.InfiniteLine(angle=90, movable=False, pen=_pen(theme.PLOT["cursor"], 1, 140, Qt.DotLine))
        self.vb.addItem(self.cursor, ignoreBounds=True)
        self.cursor_label = pg.TextItem("", color=theme.PLOT["cursor_text"], anchor=(0, 1))
        self.vb.addItem(self.cursor_label, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)
        self.vb.on_reset = self.default_view

        from gcws.ui.plot.companion import CompanionPane
        self.companion = CompanionPane(ws, tools, self)
        self.companion.plot.getAxis("left").setWidth(AXIS_WIDTH)
        self.companion.spectrumRequested.connect(self.spectrumRequested.emit)
        s = QSettings()
        self.companion.key = s.value(f"{self._prefix}/companion_key", "TIC") or "TIC"
        self.companion.keyChanged.connect(lambda k: QSettings().setValue(f"{self._prefix}/companion_key", k))
        self.split = QSplitter(Qt.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(self.plot)
        self.split.addWidget(self.companion)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.split.splitterMoved.connect(
            lambda *_: QSettings().setValue(f"{self._prefix}/split", self.split.sizes()))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.title = QLabel()
        self.title.setObjectName("hint")
        bar.addWidget(self.title, 1)
        self.dual = QCheckBox("FID + MS")
        self.dual.setToolTip("Show the other detector below / above, aligned by the FID-MS delay")
        self.dual.setChecked(s.value(f"{self._prefix}/dual", True, type=bool))
        self.dual.toggled.connect(self._dual_toggled)
        bar.addWidget(self.dual)
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
        lay.addWidget(self.split, 1)
        sizes = s.value(f"{self._prefix}/split")
        if sizes:
            try:
                self.split.setSizes([int(v) for v in sizes])
            except (TypeError, ValueError):
                pass

        ws.runAdded.connect(lambda *_: self.refresh(autorange=True))
        ws.runRemoved.connect(lambda *_: self.refresh())
        ws.runChanged.connect(lambda *_: self.refresh())
        ws.activeRunChanged.connect(lambda *_: self.refresh(autorange=detail))
        ws.signalKeyChanged.connect(lambda *_: self.refresh(autorange=True))
        ws.resultChanged.connect(self._on_result)
        ws.identsChanged.connect(lambda *_: self.refresh_labels())
        ws.selectionChanged.connect(self._on_selection)
        ws.methodChanged.connect(lambda *_: self.refresh_events())
        ws.deconvChanged.connect(lambda *_: self.refresh_markers())
        self._markers = None
        self._update_dual()

    # -- dual view ---------------------------------------------------------------

    def dual_on(self) -> bool:
        return self.dual.isChecked() and self.companion.available()

    def _dual_toggled(self, on):
        QSettings().setValue(f"{self._prefix}/dual", on)
        self._update_dual()
        if on:
            self.companion.refresh()

    def _update_dual(self):
        avail = self.companion.available()
        self.dual.setVisible(avail)
        on = avail and self.dual.isChecked()
        self.companion.setVisible(on)
        fid_primary = is_fid(self.ws.signal_key)
        top, bottom = (self.plot, self.companion) if fid_primary else (self.companion, self.plot)
        if self.split.indexOf(top) != 0:
            self.split.insertWidget(0, top)
        # only the lower pane carries the time axis
        pi_top = top.getPlotItem() if top is self.plot else top.plot.getPlotItem()
        pi_bottom = bottom.getPlotItem() if bottom is self.plot else bottom.plot.getPlotItem()
        pi_bottom.showAxis("bottom")
        if on:
            pi_top.hideAxis("bottom")
        else:
            self.plot.getPlotItem().showAxis("bottom")

    # -- transforms ---------------------------------------------------------

    def _transform(self, st, sig, rank: int, n: int, key: str | None = None) -> tuple[float, float]:
        if self.detail:
            return (1.0, 0.0)
        key = key or self.ws.signal_key
        sc, off = 1.0, 0.0
        if self.norm.isChecked():
            m = self.ws.method_for(st, key)
            from gcws.integration.autoparams import _integration_start
            t0 = _integration_start(sig.rt, m) or float(sig.rt[0])
            sel = sig.y[sig.rt >= t0]
            base = float(np.percentile(sel, 1)) if sel.size else 0.0
            top = float(sel.max()) if sel.size else 1.0
            sc = 100.0 / max(top - base, 1e-12)
            off = -base * sc
        if self.stack.isChecked() and n > 1:
            span = 100.0 if self.norm.isChecked() else self._span(key)
            off += rank * 0.12 * span
        return (sc, off)

    def _span(self, key: str | None = None) -> float:
        key = key or self.ws.signal_key
        spans = []
        for st in self.ws.states():
            s = st.run.signal(self.ws.effective_key(st, key))
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
            sig = st.run.signal(self.ws.effective_key(st, key))
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
        self._update_dual()
        if self.dual_on():
            self.companion.refresh()
        if autorange:
            self.default_view()
        self.refresh_markers()
        name = active.name if active else "no chromatogram loaded"
        self.title.setText(f"{key}  -  {name}" if active else name)

    def _on_result(self, run_id: str, key: str):
        if self.ws.active_id != run_id:
            return
        if key == self.ws.signal_key:
            self.refresh_active()
            self.refresh_events()
        if self.dual_on() and key == self.companion.key:
            self.companion.refresh_peaks()

    def refresh_markers(self):
        """Triangles at deconvoluted components that have no integrated peak (whole-run deconvolution)."""
        for vb in (self.vb, self.companion.vb):
            if self._markers is not None and self._markers.getViewBox() is vb:
                vb.removeItem(self._markers)
        self._markers = None
        st = self.ws.active
        if st is None or st.run.ms is None or self.detail:
            return
        from gcws.ms import deconv_cache as DC
        comps = DC.hidden_components(self.ws, st, self.ws.signal_key, DC.settings_of(self.ws))
        if not comps:
            return
        on_companion = self.dual_on() and is_fid(self.ws.signal_key)
        vb = self.companion.vb if on_companion else self.vb
        curves = self.companion.curves if on_companion else self.curves
        curve = curves.get(st.id)
        if curve is None:
            return
        xs, ys = curve.xData, curve.yData
        if xs is None or len(xs) < 2:
            return
        shift = st.delay_value if is_fid(self.ws.signal_key) else 0.0
        spots = []
        for c in comps:
            x = c.rt + shift
            y = float(np.interp(x, xs, ys))
            spots.append({"pos": (x, y), "data": c, "symbol": "t", "size": 11,
                          "brush": pg.mkBrush(theme.qcolor(theme.WARN, 200)), "pen": pg.mkPen("w", width=0.8)})
        tip = (lambda x, y, data: f"Deconvoluted component without a peak\n{data.rt:.3f} min (MS), model m/z "
               f"{data.model_mz}, quality {data.quality:.0f}\nclick: its spectrum")
        self._markers = pg.ScatterPlotItem(spots=spots, hoverable=True, tip=tip)
        self._markers.setZValue(30)
        self._markers.sigClicked.connect(lambda _item, pts, _ev: pts and self.componentClicked.emit(
            st.id, pts[0].data()))
        vb.addItem(self._markers, ignoreBounds=True)

    def _on_selection(self, run_id: str, index: int):
        self.refresh_active()
        if self.dual_on():
            self.companion.refresh_peaks()
        if self.detail:
            self.zoom_to_selected()

    def refresh_active(self):
        st = self.ws.active
        res = self.ws.active_result() if st else None
        sig = st.run.signal(self.ws.effective_key(st)) if st else None
        if st is None or sig is None or res is None:
            self.peaks.set_data(np.zeros(0), np.zeros(0), [], "#000")
            self.labels.set_labels([])
            return
        muted = self.ws.blank_level_peaks(st.id) if hasattr(self.ws, "blank_level_peaks") else set()
        self.peaks.set_data(sig.rt, sig.y, res.peaks, st.color, self.ws.selected, self.vb.transform, muted=muted)
        self.refresh_labels()

    def refresh_labels(self, *_):
        st = self.ws.active
        res = self.ws.active_result() if st else None
        mode = self.label_mode.currentIndex()
        if st is None or res is None or mode == 3:
            self.labels.set_labels([])
            return
        sig = st.run.signal(self.ws.effective_key(st))
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
        sig = st.run.signal(self.ws.effective_key(st))
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
                                          brush=pg.mkBrush(*theme.PLOT["off_region"]), pen=pg.mkPen(None))
                reg.setZValue(-10)
                self.vb.addItem(reg, ignoreBounds=True)
                self.regions.append(reg)
                off_start = None
                continue
            line = pg.InfiniteLine(e.time, angle=90, movable=False,
                                   pen=_pen(theme.PLOT["event"], 1, 140, Qt.DashLine),
                                   label=e.kind.value, labelOpts={"position": 0.95, "color": theme.PLOT["event"],
                                                                  "rotateAxis": (1, 0), "anchors": [(0, 0), (0, 0)]})
            self.vb.addItem(line, ignoreBounds=True)
            self.event_lines.append(line)
        if off_start is not None:
            reg = pg.LinearRegionItem((off_start, float(sig.rt[-1])), movable=False,
                                      brush=pg.mkBrush(*theme.PLOT["off_region"]), pen=pg.mkPen(None))
            self.vb.addItem(reg, ignoreBounds=True)
            self.regions.append(reg)

    # -- spectra --------------------------------------------------------------

    def _spectrum_request(self, t0, t1, bg, xy):
        """Right-click / right-drag in this plot -> spectrum request in MS time."""
        rid = nearest_curve(self.vb, self.curves, *xy, self.ws.active_id) if xy is not None else self.ws.active_id
        st = self.ws.runs.get(rid) if rid else None
        if st is None or st.run.ms is None:
            self.ws.message.emit("No MS data for a spectrum here")
            return
        key, d = self.ws.signal_key, st.delay_value
        conv = lambda t: None if t is None else to_ms(t, key, d)
        bg_ms = (conv(bg[0]), conv(bg[1])) if bg is not None else None
        self.spectrumRequested.emit(ScanRequest(st.id, conv(t0), conv(t1), bg_ms))

    def _run_at(self, x: float, y: float):
        return nearest_curve(self.vb, self.curves, x, y, self.ws.active_id)

    def set_ms_regions(self, regions):
        """Shaded apex/background scan ranges: [(t0, t1, colour)] on the MS time axis."""
        self._ms_regions_ms = list(regions)
        for it in self.ms_regions:
            self.vb.removeItem(it)
        self.ms_regions = []
        st = self.ws.active
        d = st.delay_value if st is not None else 0.0
        key = self.ws.signal_key
        mapped = []
        for t0, t1, color in regions:
            t0, t1 = from_ms(t0, key, d), from_ms(t1, key, d)
            mapped.append((t0, t1, color))
            c = QColor(color)
            reg = pg.LinearRegionItem((t0, t1), movable=False, brush=pg.mkBrush(c.red(), c.green(), c.blue(), 45),
                                      pen=pg.mkPen(None))
            reg.setZValue(-5)
            self.vb.addItem(reg, ignoreBounds=True)
            self.ms_regions.append(reg)
        self.companion.set_regions(mapped)

    # -- view --------------------------------------------------------------------

    def default_view(self):
        """Whole run on x; y scaled to the integrated part (the solvent front is
        usually far higher than everything the analyst works on)."""
        if self.detail and self.ws.selected_peak() is not None:
            self.zoom_to_selected()
            return
        st = self.ws.active
        sig = st.run.signal(self.ws.effective_key(st)) if st else None
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
        if self.dual_on():
            self.companion.fit_y()

    def zoom_to_selected(self):
        p = self.ws.selected_peak()
        st = self.ws.active
        if p is None or st is None:
            return
        sig = st.run.signal(self.ws.effective_key(st))
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

    # -- cursor -------------------------------------------------------------------

    def _mouse_moved(self, pos):
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        self.set_cursor(self.vb.mapSceneToView(pos).x())

    def set_cursor(self, x: float):
        """Cursor line at time ``x`` (primary axis) in both panes, with a readout."""
        self.cursor.setPos(x)
        self.companion.set_cursor(x)
        st = self.ws.active
        key = self.ws.signal_key
        txt = f"{x:.3f} min"
        if st is not None:
            if self.dual_on():
                other = "MS" if is_fid(key) else "FID"
                txt += f"  ({other} {x - self.companion.shift(st):.3f})"
            sig = st.run.signal(self.ws.effective_key(st, key))
            if sig is not None and sig.rt[0] <= x <= sig.rt[-1]:
                txt += f"   {np.interp(x, sig.rt, sig.y):.4g}"
        self.cursor_label.setText(txt)
        vr = self.vb.viewRange()
        self.cursor_label.setPos(x, vr[1][1])
