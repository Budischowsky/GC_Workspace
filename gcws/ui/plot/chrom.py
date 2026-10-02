"""Chromatogram 1 and 2.

Each panel shows one signal of the loaded runs -- FID, TIC, BPC or an EIC,
optionally minus the assigned blank -- with the active run drawn bold with its
peaks. The two panels share one time axis: Chromatogram 1's detector sets it,
and a panel showing the other detector draws every run shifted by that run's
FID-MS delay, so a compound sits at the same x in both. Zoom and pan are kept
in sync by ViewLink, including relative intensity gestures across detector units.

Integration tools work on the signal of the panel they are used in. The peak
table lists the peaks of one of the panels (``ws.table_panel``); a click in
the other panel selects the table's peak at that time.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt, QTimer, Signal as QtSignal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QInputDialog, QToolButton, QVBoxLayout,
                               QSizePolicy, QWidget)

from gcws.core.keys import base_key, is_derived, is_fid
from gcws.integration.method import EventKind
from gcws.ms.spectra import ScanRequest, from_ms, to_ms
from gcws.ui import theme
from gcws.ui.plot.items import LabelsItem, PeaksItem
from gcws.ui.plot.tools import ToolViewBox

theme.configure_plots()

#: fixed width of the y axes so stacked panels share the same x pixels
AXIS_WIDTH = 62
EIC_ITEM = "EIC ..."


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


def frame_offset(frame_key: str, key: str, delay: float) -> float:
    """x offset that puts a ``key`` trace onto the time axis of ``frame_key``'s detector
    (FID time = MS time + delay)."""
    if is_fid(frame_key) == is_fid(key):
        return 0.0
    return delay if is_fid(frame_key) else -delay


class ViewLink:
    """Shared time and relative intensity gestures across different detector units."""

    def __init__(self, panels):
        self.panels = list(panels)
        self.manual_y = False
        self.busy = False
        self._reset_timer = QTimer(self.panels[0].ws)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(self.reset)
        for p in self.panels:
            p.link = self
            p.vb.sigXRangeChanged.connect(lambda vb, rng, src=p: self.time_changed(src, rng))
        self.panels[0].ws.runAdded.connect(lambda *_: self._reset_timer.start(0))
        self.panels[0].ws.solventCutChanged.connect(self.reset)

    def time_changed(self, src, rng):
        # A load's deferred fit must not overwrite a newer zoom or axis gesture.
        self._reset_timer.stop()
        if self.busy:
            return
        self.busy = True
        try:
            for p in self.panels:
                if p is not src:
                    p.vb.setXRange(*rng, padding=0)
                p.linked_x_changed()
        finally:
            self.busy = False

    def intensity_changed(self, src, old, new):
        if self.busy or old == new or old[1] <= old[0]:
            return
        self.manual_y = True
        span = old[1] - old[0]
        lo, hi = (new[0] - old[0]) / span, (new[1] - old[0]) / span
        for p in self.panels:
            p._fit.stop()
            if p is not src:
                a, b = p.vb.viewRange()[1]
                p.vb.setYRange(a + lo * (b - a), a + hi * (b - a), padding=0)

    def fit_intensity(self):
        self.manual_y = False
        for p in self.panels:
            p._fit.stop()
            p.fit_y()

    def reset(self):
        self._reset_timer.stop()
        ranges = [r for r in (p.data_x_range() for p in self.panels) if r is not None]
        if ranges:
            self.panels[0].vb.setXRange(min(r[0] for r in ranges), max(r[1] for r in ranges), padding=0)
        self.fit_intensity()


class ChromPanel(QWidget):
    """One chromatogram panel (``index`` 0 = Chromatogram 1, 1 = Chromatogram 2)."""
    spectrumRequested = QtSignal(object)        # ScanRequest (MS time axis)
    componentClicked = QtSignal(str, object)    # run id, deconvoluted Component
    cursorMoved = QtSignal(float)               # x on the shared axis
    resetRequested = QtSignal()                 # double-click: full view in every panel
    exportRequested = QtSignal(int)             # panel index

    def __init__(self, ws, tools, index: int, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.tools = tools
        self.index = index
        self._prefix = f"chrom{index + 1}"
        self._loading = False
        self.link = None
        s = QSettings()
        self.vb = ToolViewBox(tools)
        self.vb.panel = self
        self.vb.spectrumRequested.connect(self._spectrum_request)
        self.vb.on_reset = self.resetRequested.emit
        self.plot = pg.PlotWidget(viewBox=self.vb)
        self.plot.hideButtons()
        self.plot.getPlotItem().layout.setContentsMargins(0, 0, 0, 0)
        self.plot.getPlotItem().layout.setSpacing(0)
        self.plot.setLabel("bottom", "RT", units="min")
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot.getAxis("left").enableAutoSIPrefix(True)
        self.plot.getAxis("left").setWidth(AXIS_WIDTH)
        for side in ("bottom", "left"):             # a drag on an axis moves the time window
            self.plot.getAxis(side).setCursor(Qt.SizeHorCursor)
            self.plot.getAxis(side).setToolTip("Drag to move the chromatogram left / right")
        self.plot.getAxis("left").setCursor(Qt.SizeVerCursor)
        self.plot.getAxis("left").setToolTip(
            "Drag: move up/down · right-drag or wheel: intensity (baseline stays) · double-click: fit")
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
        self._ms_regions_ms: list = []
        self.cursor = pg.InfiniteLine(angle=90, movable=False, pen=_pen(theme.PLOT["cursor"], 1, 140, Qt.DotLine))
        self.vb.addItem(self.cursor, ignoreBounds=True)
        self.cursor_label = pg.TextItem("", color=theme.PLOT["cursor_text"], anchor=(0, 1))
        self.vb.addItem(self.cursor_label, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)
        self._markers = None
        self._deconv_markers = None
        theme.register_plot(self.plot, self._theme_changed)
        self._fit = QTimer(self)
        self._fit.setSingleShot(True)
        self._fit.setInterval(0)
        self._fit.timeout.connect(self._auto_fit)

        # -- header: signal, blank switch, display options ------------------------------
        self.signal = QComboBox()
        self.signal.setToolTip("Signal shown in this chromatogram")
        self.signal.setMinimumWidth(96)
        self.signal.activated.connect(self._signal_picked)
        self.blank = QCheckBox("subtract blank", self)
        self.blank.hide()
        self.blank.setToolTip("Show and integrate this signal minus the assigned blank "
                              "(settings: Quantify > Blank subtraction settings)")
        self.blank.toggled.connect(self._blank_toggled)
        self.cut = QCheckBox("Solvent cut", self)
        self.cut.hide()
        self.cut.toggled.connect(lambda on: None if self._loading else self.ws.set_solvent_cut(on, key=self.key))
        self.table_chip = theme.chip("", "accent")
        self.table_chip.setToolTip("The peak table lists the peaks of this chromatogram")
        from gcws.ui.plot.overlay import ElidedLabel
        self.title = ElidedLabel()
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.title.setObjectName("hint")
        self.others = QCheckBox("Overlay")
        self.others.setToolTip("Show the other loaded chromatograms")
        self.others.setChecked(s.value(f"{self._prefix}/overlay", True, type=bool))
        self.norm = QCheckBox("Normalize")
        self.norm.setToolTip("Scale every trace to its own maximum (after the integration start)")
        self.norm.setChecked(s.value(f"{self._prefix}/normalize", False, type=bool))
        self.stack = QCheckBox("Stack")
        self.stack.setToolTip("Offset the traces vertically")
        self.stack.setChecked(s.value(f"{self._prefix}/stack", False, type=bool))
        for name, w in (("overlay", self.others), ("normalize", self.norm), ("stack", self.stack)):
            w.toggled.connect(lambda on, n=name: (QSettings().setValue(f"{self._prefix}/{n}", on),
                                                  self._display_changed(n)))
        self.label_mode = QComboBox()
        self.label_mode.addItems(["Labels: RT", "Labels: #", "Labels: name", "Labels: off"])
        self.label_mode.setCurrentIndex(s.value(f"{self._prefix}/labels", 0, type=int))
        self.label_mode.currentIndexChanged.connect(
            lambda i: (QSettings().setValue(f"{self._prefix}/labels", i), self.refresh_labels()))
        self.export_btn = QToolButton()
        self.export_btn.setText("Export...")
        self.export_btn.setToolTip("Save this chromatogram as a picture (PNG, SVG, PDF ...)")
        self.export_btn.clicked.connect(lambda: self.exportRequested.emit(self.index))
        from gcws.ui.plot.overlay import PlotOverlay
        self.controls = PlotOverlay(self.plot, [self.signal, self.title, self.table_chip, self.others,
                                                self.norm, self.stack, self.label_mode, self.export_btn])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.plot, 1)

        ws.runAdded.connect(lambda *_: (self.sync_header(), self.refresh()))
        ws.runRemoved.connect(lambda *_: (self.sync_header(), self.refresh()))
        ws.runChanged.connect(lambda *_: (self.sync_header(), self.refresh()))
        ws.activeRunChanged.connect(lambda *_: (self.sync_header(), self.refresh(), self._auto_fit()))
        ws.panelsChanged.connect(self._panels_changed)
        ws.signalKeyChanged.connect(lambda *_: (self.sync_header(), self.refresh_active(), self.refresh_markers()))
        ws.resultChanged.connect(self._on_result)
        ws.identsChanged.connect(lambda *_: self.refresh_labels())
        ws.selectionChanged.connect(lambda *_: self.refresh_active())
        ws.methodChanged.connect(lambda *_: self.refresh_events())
        ws.deconvChanged.connect(lambda *_: self.refresh_markers())
        ws.solventCutChanged.connect(lambda: (self.sync_header(), self.refresh()))
        self._key_shown = self.key
        self.sync_header()

    # -- keys and time frames --------------------------------------------------------------

    def _auto_fit(self):
        if self.link is None or not self.link.manual_y:
            self.fit_y()

    def _display_changed(self, name):
        self.refresh()
        if name in ("normalize", "stack"):
            self.fit_y()
        else:
            self._auto_fit()

    @property
    def key(self) -> str:
        return self.ws.panel_key(self.index)

    def run_key(self, st) -> str:
        """The signal drawn for run ``st`` (the base trace when it cannot be blank-subtracted)."""
        return self.ws.effective_key(st, self.key)

    def frame_key(self) -> str:
        """Chromatogram 1's signal sets the detector time of the shared axis."""
        return self.ws.panel_key(0)

    def shift(self, st) -> float:
        """x offset of this panel's trace of run ``st`` on the shared axis."""
        return frame_offset(self.frame_key(), self.key, st.delay_value) if st is not None else 0.0

    def dx(self) -> float:
        return self.shift(self.ws.active)

    def tool_key(self) -> str:
        return self.key

    def is_table(self) -> bool:
        return self.ws.table_panel == self.index

    def selected_index(self) -> int:
        """Index (in this panel's peaks of the active run) of the peak selected in the table."""
        st = self.ws.active
        p = self.ws.selected_peak()
        if st is None or p is None:
            return -1
        if self.run_key(st) == self.ws.active_key:
            return self.ws.selected
        res = self.ws.result(st.id, self.key)
        if res is None:
            return -1
        t = p.apex_rt + frame_offset(self.key, self.ws.active_key, st.delay_value)
        hit = res.peak_at(t)
        return res.peaks.index(hit) if hit is not None else -1

    # -- header ------------------------------------------------------------------------------

    def sync_header(self):
        self._loading = True
        keys = []
        for st in self.ws.states():
            for k in self.ws.signals_for(st):
                if not is_derived(k) and k not in keys:
                    keys.append(k)
        cur = self.ws.panel_keys[self.index]
        if cur not in keys:
            keys.append(cur)
        self.signal.clear()
        self.signal.addItems(keys)
        if any(st.run.ms is not None for st in self.ws.states()):
            self.signal.addItem(EIC_ITEM)
        self.signal.setCurrentText(cur)
        self.blank.setChecked(self.ws.panel_blank[self.index])
        enabled, end = self.ws.solvent_cut_settings(self.key)
        self.cut.setChecked(enabled)
        axis = "FID" if is_fid(self.key) else "MS"
        self.cut.setToolTip(f"Exclude solvent before {end:g} min ({axis} time). "
                            "Change the detector's end time under Chromatogramm.")
        self.blank.setEnabled(self.ws.panel_blank[self.index]
                              or any(self.ws.blank_ids(st) for st in self.ws.states()))
        theme.set_chip(self.table_chip, "▦ Peak table" if self.is_table() else "", "accent")
        st = self.ws.active
        note = ""
        if st is not None and self.run_key(st) != self.key:
            note = f"   (no blank for this run: {base_key(self.key)} shown)"
        self.title.setText((st.name + note) if st is not None else "no chromatogram loaded")
        self._loading = False
        self.controls.reposition()

    def _signal_picked(self, i):
        if self._loading:
            return
        text = self.signal.itemText(i)
        if text == EIC_ITEM:
            text = self.ask_eic()
            if not text:
                self.sync_header()
                return
        self.set_signal(text)

    def set_signal(self, key: str) -> None:
        """Show ``key`` (base signal; the blank switch stays as it is)."""
        self.ws.set_panel(self.index, key=base_key(key))

    def ask_eic(self) -> str:
        import re
        val, ok = QInputDialog.getText(self, "Extracted ion chromatogram", "m/z (several: summed, e.g. 149, 57):")
        masses = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", val)] if ok else []
        if not masses:
            return ""
        from gcws.core.model import eic_key
        key = eic_key(masses)
        for st in self.ws.states():
            st.run.signal(key)                 # compute and cache so it is listed
        return key

    def _blank_toggled(self, on):
        if self._loading:
            return
        self.ws.set_panel(self.index, blank=on)
        if on and not any(self.run_key(st) == self.key for st in self.ws.states()):
            self.ws.message.emit("No blank assigned to the loaded chromatograms (Quantify > Assign blanks)")

    def _panels_changed(self):
        self.sync_header()
        changed = self.key != self._key_shown
        self._key_shown = self.key
        self.refresh(fit=changed)
        self.set_ms_regions(self._ms_regions_ms)

    # -- transforms -----------------------------------------------------------------------------

    def _transform(self, st, sig, rank: int, n: int, key: str) -> tuple[float, float]:
        sc, off = 1.0, 0.0
        if self.norm.isChecked():
            m = self.ws.method_for(st, key)
            from gcws.integration.autoparams import _integration_start
            t0 = _integration_start(sig.rt, m, self.ws.solvent_cut(st, key))
            t0 = float(sig.rt[0]) if t0 is None else t0
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
            s = st.run.signal(self.run_key(st))
            if s is not None and s.n:
                cut = self.ws.solvent_cut(st, self.run_key(st))
                y = s.y if cut is None else s.y[s.rt >= cut]
                if y.size:
                    spans.append(float(np.percentile(y, 99.5) - np.percentile(y, 1)))
        return max(spans) if spans else 1.0

    # -- drawing -------------------------------------------------------------------------------

    def visible_states(self):
        active = self.ws.active
        states = self.ws.states() if self.others.isChecked() else ([active] if active else [])
        return [s for s in states if s is not None and (s.visible or s is active)]

    def refresh(self, *_, autorange: bool = False, fit: bool = False):
        active = self.ws.active
        shown = set()
        visible = self.visible_states()
        n = len(visible)
        self.vb.transform = (1.0, 0.0)
        for rank, st in enumerate(visible):
            k = self.run_key(st)
            sig = st.run.signal(k)
            if sig is None:
                continue
            sc, off = self._transform(st, sig, rank, n, k)
            is_active = active is not None and st.id == active.id
            curve = self.curves.get(st.id)
            if curve is None:
                curve = pg.PlotDataItem()
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                self.vb.addItem(curve)
                self.curves[st.id] = curve
            cut = self.ws.solvent_cut(st, k)
            start = 0 if cut is None else int(np.searchsorted(sig.rt, cut))
            curve.setData(sig.rt[start:] + self.shift(st), sig.y[start:] * sc + off)
            curve.setPen(_pen(st.color, 1.8 if is_active else 1.0, 255 if is_active else 150))
            curve.setShadowPen(theme.glow_pen(st.color, 6.0 if is_active else 4.0, 70 if is_active else 32))
            curve.setZValue(10 if is_active else 1)
            if is_active:
                self.vb.transform = (sc, off)
            shown.add(st.id)
        for rid in list(self.curves):
            if rid not in shown:
                self.vb.removeItem(self.curves.pop(rid))
        self.refresh_active()
        self.refresh_events()
        self.refresh_markers()
        if autorange:
            self.default_view()
        elif fit:
            self.fit_y()

    def _on_result(self, run_id: str, key: str):
        st = self.ws.active
        if st is None or run_id != st.id:
            return
        if key in (self.key, self.run_key(st)):
            self.refresh_active()
            self.refresh_events()
        elif key == self.ws.active_key:
            self.refresh_active()            # the selection maps onto the table's peaks
        if key == self.ws.signal_key or is_fid(key):
            self.refresh_markers()

    def _theme_changed(self):
        """Theme switch: the items that took their colours when made, then a redraw."""
        self.cursor.setPen(_pen(theme.PLOT["cursor"], 1, 140, Qt.DotLine))
        self.cursor_label.setColor(theme.PLOT["cursor_text"])
        pen = QPen(QColor(theme.PLOT["baseline"]))
        pen.setCosmetic(True)
        pen.setWidthF(1.5)
        pen.setStyle(Qt.DashLine)
        self.vb.preview.setPen(pen)
        self.refresh()

    def refresh_active(self):
        st = self.ws.active
        k = self.run_key(st) if st is not None else self.key
        res = self.ws.result(st.id, k) if st is not None else None
        sig = st.run.signal(k) if st is not None else None
        if st is None or sig is None or res is None or st.id not in self.curves:
            self.peaks.set_data(np.zeros(0), np.zeros(0), [], "#000")
            self.labels.set_labels([])
            return
        muted = self.ws.blank_level_peaks(st.id, k)
        self.peaks.set_data(sig.rt, sig.y, res.peaks, st.color, self.selected_index(), self.vb.transform,
                            dx=self.shift(st), muted=muted)
        self.refresh_labels()

    def refresh_labels(self, *_):
        st = self.ws.active
        k = self.run_key(st) if st is not None else self.key
        res = self.ws.result(st.id, k) if st is not None else None
        mode = self.label_mode.currentIndex()
        if st is None or res is None or mode == 3 or st.id not in self.curves:
            self.labels.set_labels([])
            return
        sig = st.run.signal(k)
        sc, off = self.vb.transform
        dx = self.shift(st)
        idents, _ = st.ident_set(k).bind(res.peaks)
        sel = self.selected_index()
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
            out.append((p.apex_rt + dx, y, text, i == sel))
        self.labels.set_labels(out)

    def refresh_events(self):
        for it in self.event_lines + self.regions:
            self.vb.removeItem(it)
        self.event_lines, self.regions = [], []
        st = self.ws.active
        if st is None:
            return
        k = self.run_key(st)
        sig = st.run.signal(k)
        if sig is None:
            return
        dx = self.shift(st)
        m = self.ws.method_for(st, k)
        off_start = None
        for e in m.events():
            if e.kind == EventKind.INTEGRATOR_OFF:
                off_start = e.time
                continue
            if e.kind == EventKind.INTEGRATOR_ON and off_start is not None:
                reg = pg.LinearRegionItem((off_start + dx, e.time + dx), movable=False,
                                          brush=pg.mkBrush(*theme.PLOT["off_region"]), pen=pg.mkPen(None))
                reg.setZValue(-10)
                self.vb.addItem(reg, ignoreBounds=True)
                self.regions.append(reg)
                off_start = None
                continue
            line = pg.InfiniteLine(e.time + dx, angle=90, movable=False,
                                   pen=_pen(theme.PLOT["event"], 1, 140, Qt.DashLine),
                                   label=e.kind.value, labelOpts={"position": 0.95, "color": theme.PLOT["event"],
                                                                  "rotateAxis": (1, 0), "anchors": [(0, 0), (0, 0)]})
            self.vb.addItem(line, ignoreBounds=True)
            self.event_lines.append(line)
        if off_start is not None:
            reg = pg.LinearRegionItem((off_start + dx, float(sig.rt[-1]) + dx), movable=False,
                                      brush=pg.mkBrush(*theme.PLOT["off_region"]), pen=pg.mkPen(None))
            self.vb.addItem(reg, ignoreBounds=True)
            self.regions.append(reg)

    def refresh_markers(self):
        """Draw TIC markers for FID splits and whole-run components without a table peak."""
        if self._markers is not None:
            self.vb.removeItem(self._markers)
        self._markers = None
        if self._deconv_markers is not None:
            self.vb.removeItem(self._deconv_markers)
        self._deconv_markers = None
        st = self.ws.active
        if st is None or st.run.ms is None:
            return
        self._refresh_deconv_markers(st)
        other = self.ws.panel_key(1 - self.index)
        if is_fid(self.key) and not (self.index == 0 and is_fid(other)):
            return
        from gcws.ms import deconv_cache as DC
        comps = DC.hidden_components(self.ws, st, self.ws.signal_key, DC.settings_of(self.ws))
        curve = self.curves.get(st.id)
        if not comps or curve is None:
            return
        xs, ys = curve.xData, curve.yData
        if xs is None or len(xs) < 2:
            return
        shift = frame_offset(self.frame_key(), "TIC", st.delay_value)       # components are in MS time
        spots = []
        for c in comps:
            x = c.rt + shift
            spots.append({"pos": (x, float(np.interp(x, xs, ys))), "data": c, "symbol": "t", "size": 11,
                          "brush": pg.mkBrush(theme.qcolor(theme.WARN, 200)), "pen": pg.mkPen(theme.PLOT["bg"], width=0.8)})
        tip = (lambda x, y, data: f"Deconvoluted component without a peak\n{data.rt:.3f} min (MS), model m/z "
               f"{data.model_mz}, purity {data.purity:.2f}\nclick: its spectrum")
        self._markers = pg.ScatterPlotItem(spots=spots, hoverable=True, tip=tip)
        self._markers.setZValue(30)
        self._markers.sigClicked.connect(lambda _item, pts, _ev: pts and self.componentClicked.emit(
            st.id, pts[0].data()))
        self.vb.addItem(self._markers, ignoreBounds=True)

    def _refresh_deconv_markers(self, st):
        """Show FID split component apices on the TIC trace in aligned MS time."""
        if base_key(self.key) != "TIC":
            return
        fid_key = next((self.ws.panel_key(i) for i in range(2)
                        if is_fid(self.ws.panel_key(i))), None)
        if fid_key is None:
            return
        res = self.ws.result(st.id, self.ws.effective_key(st, fid_key))
        curve = self.curves.get(st.id)
        if res is None or curve is None:
            return
        xs, ys = curve.xData, curve.yData
        if xs is None or len(xs) < 2:
            return
        from gcws.ms.deconv import allocated_component
        shift = frame_offset(self.frame_key(), "TIC", st.delay_value)
        spots = []
        for peak in res.peaks:
            component = allocated_component(st.run.ms, peak)
            if component is None:
                continue
            x = component.rt + shift
            if not xs[0] <= x <= xs[-1]:
                continue
            spots.append({"pos": (x, float(np.interp(x, xs, ys))), "data": component,
                          "symbol": "d", "size": 12,
                          "brush": pg.mkBrush(theme.qcolor(theme.ACCENT, 235)),
                          "pen": pg.mkPen(theme.PLOT["bg"], width=1.0)})
        if not spots:
            return
        tip = (lambda x, y, data: f"Deconvoluted peak from FID\n{data.rt:.3f} min (MS), "
               f"model m/z {data.model_mz}\nclick: its spectrum")
        self._deconv_markers = pg.ScatterPlotItem(spots=spots, hoverable=True, tip=tip)
        self._deconv_markers.setZValue(31)
        self._deconv_markers.sigClicked.connect(lambda _item, pts, _ev: pts and self.componentClicked.emit(
            st.id, pts[0].data()))
        self.vb.addItem(self._deconv_markers, ignoreBounds=True)

    # -- spectra ---------------------------------------------------------------------------------

    def _spectrum_request(self, t0, t1, bg, xy):
        """Right-click / right-drag in this panel -> spectrum request in MS time."""
        rid = nearest_curve(self.vb, self.curves, *xy, self.ws.active_id) if xy is not None else self.ws.active_id
        st = self.ws.runs.get(rid) if rid else None
        if st is None or st.run.ms is None:
            self.ws.message.emit("No MS data for a spectrum here")
            return
        frame, d = self.frame_key(), st.delay_value
        conv = lambda t: None if t is None else to_ms(t, frame, d)
        bg_ms = (conv(bg[0]), conv(bg[1])) if bg is not None else None
        self.spectrumRequested.emit(ScanRequest(st.id, conv(t0), conv(t1), bg_ms))

    def set_ms_regions(self, regions):
        """Shaded apex/background scan ranges: [(t0, t1, colour)] on the MS time axis."""
        self._ms_regions_ms = list(regions)
        for it in self.ms_regions:
            self.vb.removeItem(it)
        self.ms_regions = []
        st = self.ws.active
        d = st.delay_value if st is not None else 0.0
        frame = self.frame_key()
        for t0, t1, color in regions:
            c = QColor(color)
            reg = pg.LinearRegionItem((from_ms(t0, frame, d), from_ms(t1, frame, d)), movable=False,
                                      brush=pg.mkBrush(c.red(), c.green(), c.blue(), 45), pen=pg.mkPen(None))
            reg.setZValue(-5)
            self.vb.addItem(reg, ignoreBounds=True)
            self.ms_regions.append(reg)

    # -- view ------------------------------------------------------------------------------------

    def data_x_range(self):
        st = self.ws.active
        sig = st.run.signal(self.run_key(st)) if st is not None else None
        if sig is None or sig.n < 2:
            return None
        dx = self.shift(st)
        cut = self.ws.solvent_cut(st, self.run_key(st))
        start = float(sig.rt[0]) if cut is None else max(float(sig.rt[0]), cut)
        end = float(sig.rt[-1])
        return (start + dx, end + dx) if start < end else None

    def _t_from(self):
        """Start of the integrated part on the shared axis (the solvent front is left out of the y fit)."""
        st = self.ws.active
        sig = st.run.signal(self.run_key(st)) if st is not None else None
        if sig is None:
            return None
        from gcws.integration.autoparams import _integration_start
        t = _integration_start(sig.rt, self.ws.method_for(st, self.run_key(st)),
                               self.ws.solvent_cut(st, self.run_key(st)))
        return t + self.shift(st) if t is not None else None

    def fit_y(self):
        """y range from the visible part after the integration start."""
        (x0, x1), _ = self.vb.viewRange()
        t_from = self._t_from()
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
        span = hi - lo or 1.0
        # Reserve only the headroom required by visible, rotated peak labels.
        height = max(self.vb.height(), 80)
        bottom = lo - 0.02 * span
        upper = hi + 0.02 * span
        font = QFont()
        font.setPixelSize(10)
        metrics = QFontMetricsF(font)
        for x, y, text, bold in self.labels.labels:
            if x0 <= x <= x1:
                font.setBold(bold)
                metrics = QFontMetricsF(font)
                pixels = min(metrics.horizontalAdvance(text) + 7, height * 0.65)
                upper = max(upper, bottom + (y - bottom) / (1 - pixels / height))
        self.vb.setYRange(bottom, upper, padding=0)

    def linked_x_changed(self):
        """The other panel moved the shared time axis: fit this panel's intensity to it."""
        self._fit.start()

    def default_view(self):
        """Whole run on x; y fitted to the integrated part."""
        xr = self.data_x_range()
        if xr is None:
            self.vb.enableAutoRange()
            return
        self.vb.setXRange(*xr, padding=0)
        self.fit_y()

    def zoom_to_selected(self):
        """Zoom the shared axis to the table's selected peak (with some room around it)."""
        p = self.ws.selected_peak()
        st = self.ws.active
        if p is None or st is None:
            return
        off = frame_offset(self.frame_key(), self.ws.active_key, st.delay_value)
        w = max(p.end - p.start, 0.02)
        self.vb.setXRange(p.start - 1.5 * w + off, p.end + 1.5 * w + off, padding=0)
        if self.link:
            self.link.fit_intensity()
        else:
            self.fit_y()

    def zoom_to(self, t0, t1):
        self.vb.setXRange(t0, t1, padding=0.02)

    # -- cursor ------------------------------------------------------------------------------------

    def _mouse_moved(self, pos):
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        x = self.vb.mapSceneToView(pos).x()
        self.set_cursor(x)
        self.cursorMoved.emit(x)

    def set_cursor(self, x: float):
        """Cursor line at ``x`` (shared axis) with this trace's own time and value."""
        self.cursor.setPos(x)
        st = self.ws.active
        dx = self.dx()
        t = x - dx
        txt = f"{t:.3f} min"
        if dx:
            txt += f"  ({'FID' if is_fid(self.key) else 'MS'} time)"
        if st is not None:
            sig = st.run.signal(self.run_key(st))
            cut = self.ws.solvent_cut(st, self.run_key(st))
            if sig is not None and sig.rt[0] <= t <= sig.rt[-1] and (cut is None or t >= cut):
                txt += f"   {np.interp(t, sig.rt, sig.y):.4g}"
        self.cursor_label.setText(txt)
        vr = self.vb.viewRange()
        self.cursor_label.setPos(x, vr[1][1])
