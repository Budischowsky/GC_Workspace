"""Mass spectrum panel: the selected peak's spectrum, or any scan / time range.

Sources feeding the panel:

* **peak** -- the selected integrated peak;
* **scan** -- a right-click (one scan) or right-drag (mean over a range) in any
  chromatogram; Shift+right-drag sets a background range that is subtracted
  from every scan spectrum until Escape clears it. Left / Right step one scan.
* **subtraction** -- a temporary analyst-selected apex minus baseline scan.

Library search, NIST, MSP export and the unknown register all work on the
spectrum on display, whatever its source.
"""
from __future__ import annotations

import numpy as np
import hashlib
import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt, Signal as QtSignal
from PySide6.QtGui import QAction, QColor, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QMenu, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from gcws.core.keys import is_fid
from gcws.ms.spectra import MODES, ScanRequest, extract, extract_range, from_ms, ms_times, to_ms
from gcws.ms.assignment import override_for, override_key
from gcws.ui import theme
from gcws.ui.docks.interpretation_view import InterpretationView


class StickPlot(pg.PlotWidget):
    ionClicked = QtSignal(int)                  # m/z of the bar clicked
    contextRequested = QtSignal(object)        # global screen position

    def __init__(self, *, embedded_title=False):
        super().__init__()
        self.setMenuEnabled(False)
        self.setLabel("bottom", "m/z")
        self.setLabel("left", "rel. abundance")
        self.showGrid(y=True, alpha=theme.PLOT["grid_alpha"])
        self.getPlotItem().getViewBox().setMouseMode(pg.ViewBox.RectMode)
        self.setToolTip("Click an ion to show its extracted ion chromatogram; drag to zoom, double-click resets")
        self.texts = []
        self._mz = np.zeros(0)
        self.embedded_title = embedded_title
        if embedded_title:
            from gcws.ui.plot.overlay import ElidedLabel, PlotOverlay
            self.caption = ElidedLabel()
            self.caption_overlay = PlotOverlay(self, [self.caption])
        self._last = None                           # the last drawing, redrawn on a theme switch
        self.scene().sigMouseClicked.connect(self._clicked)
        theme.register_plot(self, self._redraw)

    def _redraw(self):
        if self._last is not None:
            args, kw = self._last
            self.show_spectrum(*args, **kw)

    def _clicked(self, ev):
        if ev.button() == Qt.RightButton and not ev.double():
            self.contextRequested.emit(ev.screenPos())
            ev.accept()
            return
        if ev.button() != Qt.LeftButton or ev.double() or self._mz.size == 0:
            if ev.double():
                self.getPlotItem().getViewBox().autoRange()
            return
        x = self.getPlotItem().getViewBox().mapSceneToView(ev.scenePos()).x()
        i = int(np.argmin(np.abs(self._mz - x)))
        if abs(self._mz[i] - x) <= 0.7:
            self.ionClicked.emit(int(self._mz[i]))

    def show_spectrum(self, mz, ab, ref=None, title="", marks=None):
        """``marks``: optional {m/z: (label, level)} drawn above the bars (interpretation)."""
        self._last = ((mz, ab), dict(ref=ref, title=title, marks=marks))
        if self.embedded_title:
            self.caption.setText(title or "no spectrum")
            self.caption_overlay.reposition()
        self.clear()
        for t in self.texts:
            self.removeItem(t)
        self.texts = []
        self._mz = np.zeros(0)
        if mz is None or len(mz) == 0 or ab is None or len(ab) == 0 or np.max(ab) <= 0:
            self.setTitle(None if self.embedded_title else (title or "no spectrum"))
            return
        mz = np.asarray(mz, float)
        ab = np.asarray(ab, float)
        rel = ab / ab.max() * 100.0
        keep = np.isfinite(ab) & (ab > 0)
        full_mz = mz
        mz, rel = mz[keep], rel[keep]
        self._mz = mz
        self.addItem(pg.BarGraphItem(x=mz, height=rel, width=0.6, brush=theme.PLOT["spectrum"], pen=None))
        marks = marks or {}
        order = [i for i in np.argsort(rel)[::-1][:8] if int(mz[i]) not in marks]
        for i in order:
            t = pg.TextItem(str(int(mz[i])), color=theme.PLOT["spectrum"], anchor=(0.5, 1))
            t.setPos(float(mz[i]), float(rel[i]))
            self.addItem(t)
            self.texts.append(t)
        lookup = {int(m): r for m, r in zip(mz, rel)}
        x_lo, x_hi = float(min(full_mz)) - 5, float(max(full_mz)) + 5
        for m, (label, level) in marks.items():
            if int(m) not in lookup:
                continue
            color = theme.status_color(level).name() if level in theme.LEVELS else level
            y = lookup.get(int(m), 0.0)
            ax = 1.0 if m > x_hi - 0.12 * (x_hi - x_lo) else (0.0 if m < x_lo + 0.08 * (x_hi - x_lo) else 0.5)
            t = pg.TextItem(html=f'<span style="color:{color}; font-weight:600;">{label}</span>', anchor=(ax, 1))
            t.setPos(float(m), float(y) + 2)
            self.addItem(t)
            self.texts.append(t)
            if int(m) in lookup:
                self.addItem(pg.BarGraphItem(x=[float(m)], height=[y], width=0.6, brush=color, pen=None))
        if ref:
            rmz = np.array([p[0] for p in ref], float)
            rab = np.array([p[1] for p in ref], float)
            if rab.size and rab.max() > 0:
                rrel = rab / rab.max() * 100.0
                self.addItem(pg.BarGraphItem(x=rmz, y0=0, height=-rrel, width=0.6, brush=theme.PLOT["reference"],
                                             pen=None))
                for i in np.argsort(rrel)[::-1][:6]:
                    t = pg.TextItem(str(int(rmz[i])), color=theme.PLOT["reference"], anchor=(0.5, 0))
                    t.setPos(float(rmz[i]), float(-rrel[i]))
                    self.addItem(t)
                    self.texts.append(t)
        self.setTitle(None if self.embedded_title else title, size="9pt")
        vb = self.getPlotItem().getViewBox()
        vb.setRange(xRange=(x_lo, x_hi),
                    yRange=(-118 if ref else 0, 118), padding=0)


class SpectrumDock(QWidget):
    subtractionChanged = QtSignal(bool)
    regionsChanged = QtSignal(list)           # [(t0, t1, colour)] on the MS time axis
    nistRequested = QtSignal(list, str)
    atlasRequested = QtSignal(list, str)
    ownSearchRequested = QtSignal(list, str)     # search in the one library chosen on the Own library button
    registerRequested = QtSignal()
    investigateRequested = QtSignal()
    libraryRequested = QtSignal()                # add this spectrum to a library (Edit library)
    ionClicked = QtSignal(int)

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.spec = None
        self.source = "peak"                   # "peak" | "scan" | "component"
        self.scan_req: ScanRequest | None = None
        self.component = None                  # (run id, deconvoluted Component)
        self.bg_range: tuple[float, float] | None = None
        self.subtraction_state = "off"
        self.subtraction_run = None
        self.subtraction_apex = None
        self.subtraction_base = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.mode = QComboBox()
        for k, v in MODES.items():
            self.mode.addItem(v, k)
        self.mode.setToolTip("How the spectrum of a selected peak is formed")
        self.mode.currentIndexChanged.connect(lambda *_: self.refresh())
        self.mode.setParent(self)
        self.mode.hide()                       # the default mode; not offered in the menus
        # follows a blank-subtracted chromatogram trace (_sync_blank_box); not offered in the menus
        self.minus_blank = QCheckBox("subtract blank", self)
        self.minus_blank.hide()
        self.minus_blank.toggled.connect(lambda *_: self.refresh())
        self.own_menu = QMenu("Own library selection and options", self)
        self.own_menu.aboutToShow.connect(self._fill_own_menu)
        self.spectrum_actions = []
        for text, slot in (
            ("Library hits", self._library_hits),
            ("Own library", lambda: self._emit(self.ownSearchRequested)),
            ("Investigate", self.investigateRequested.emit),
            ("NIST", lambda: self._emit(self.nistRequested)),
            ("Copy MSP", self.copy_msp), ("Save MSP...", self.save_msp),
            ("Register unknown", self.registerRequested.emit),
            ("Add to library...", self.libraryRequested.emit),
        ):
            action = QAction(text, self)
            action.triggered.connect(slot)
            self.spectrum_actions.append(action)

        from gcws.ui.plot.overlay import ElidedLabel
        self.plot = StickPlot(embedded_title=True)
        self.plot.ionClicked.connect(self.ionClicked.emit)
        self.context_menu = QMenu(self.plot)
        self.plot.contextRequested.connect(self._context_menu)
        self.interp = None
        self._interp_cache: dict = {}
        self.interp_view = InterpretationView()
        self.info = ElidedLabel()
        self.info.setObjectName("hint")
        self.info.setWordWrap(False)

        # scan selection
        self.scan_plot = pg.PlotWidget()
        self.scan_plot.setMenuEnabled(False)
        self.scan_plot.setMaximumHeight(140)
        self.scan_plot.setLabel("bottom", "MS RT", units="min")
        theme.register_plot(self.scan_plot, self._scan_theme)
        self.apex_reg = pg.LinearRegionItem(brush=pg.mkBrush(theme.qcolor(theme.PLOT["apex_region"], 50)))
        self.bg_reg = pg.LinearRegionItem(brush=pg.mkBrush(theme.qcolor(theme.PLOT["bg_region"], 40)))
        self.scan_curve = pg.PlotDataItem(pen=pg.mkPen(theme.PLOT["secondary"]))
        for it in (self.scan_curve, self.apex_reg, self.bg_reg):
            self.scan_plot.addItem(it)
        use = self.use_scans = QPushButton("Use these scans")
        use.setToolTip("Blue: scans averaged, red: background scans subtracted")
        use.clicked.connect(self._use_regions)
        auto = self.auto_scans = QPushButton("Automatic")
        auto.setToolTip("Selected peak: automatic scan choice again")
        auto.clicked.connect(self._clear_override)
        sl = QVBoxLayout()
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.scan_plot)
        h = QHBoxLayout()
        h.addWidget(theme.hint("Drag the regions: blue averaged, red background scans"))
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
        self.tabs.addTab(self.interp_view, "Interpretation")
        self.tabs.addTab(self.hits, "Library hits")
        self.tabs.addTab(self.table, "m/z table")
        self.tabs.addTab(scans, "Scans")
        split = self.split = QSplitter(Qt.Vertical)
        split.addWidget(self.plot)
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setChildrenCollapsible(False)
        split.setCollapsible(1, True)
        split.setSizes([420, 0])
        self.details_height = QSettings().value("window/ms_details_height", 200, type=int)
        # Scan notes go into the caption tooltip, never on top of spectrum bars.
        self.info.setParent(self)
        self.info.hide()
        split.splitterMoved.connect(self._remember_details_height)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(split, 1)
        self.populate_menu(self.context_menu)
        self._sync_actions()

        ws.selectionChanged.connect(self._on_selection)
        ws.activeRunChanged.connect(self._on_active)
        self._last_key = ws.signal_key
        for sig in (ws.signalKeyChanged, ws.identsChanged):
            sig.connect(lambda *_: self.refresh())
        ws.resultChanged.connect(lambda rid, key: self.refresh() if rid == ws.active_id else None)
        ws.runRemoved.connect(self._on_removed)

    def populate_menu(self, menu):
        menu.setToolTipsVisible(True)
        for action in self.spectrum_actions:
            menu.addAction(action)
            if action.text() == "Own library":
                menu.addMenu(self.own_menu)
        menu.aboutToShow.connect(self._sync_actions)

    def _context_menu(self, pos):
        self._sync_actions()
        self.context_menu.popup(pos.toPoint())

    def _sync_actions(self, *_):
        if not hasattr(self, "spectrum_actions"):
            return
        self.use_scans.setEnabled(self.subtraction_state == "off")
        self.auto_scans.setEnabled(self.subtraction_state == "off")
        for action in self.spectrum_actions:
            action.setEnabled(bool(self.spec is not None and self.spec.ab.size))
        self._update_caption_tip()

    def _update_caption_tip(self):
        detail = self.plot.caption.text() + ("\n" + self.info.text() if self.info.text() else "")
        self.plot.caption.setProperty("detailTooltip", detail)
        self.plot.caption.setToolTip(detail)

    def _library_hits(self):
        self.show_details(self.hits)
        self._emit(self.atlasRequested)

    def show_details(self, widget=None):
        if widget is not None:
            self.tabs.setCurrentWidget(widget)
        if self.split.sizes()[1] == 0:
            available = max(1, self.split.height())
            detail = min(self.details_height, max(80, available - 100))
            self.split.setSizes([max(100, available - detail), detail])

    def _remember_details_height(self, *_):
        if self.split.sizes()[1] > 0:
            self.details_height = self.split.sizes()[1]

    def restore_details_height(self, height):
        self.details_height = height
        self.split.setSizes([max(1, self.split.height()), 0])

    # -- source switching --------------------------------------------------------

    def _on_selection(self, run_id, index):
        if self.subtraction_state != "off":
            return
        if index >= 0:
            self.source = "peak"
        if index >= 0 or self.source == "peak":
            self.refresh()

    def _on_active(self, *_):
        self._reset_subtraction()
        self.source = "peak"
        self.scan_req = None
        self.bg_range = None
        self.refresh()

    def _on_removed(self, run_id):
        if self.subtraction_run == run_id:
            self.back_to_peak()
        if self.scan_req is not None and self.scan_req.run_id == run_id:
            self.scan_req = None
            self.source = "peak"
            self.refresh()

    def show_range(self, req: ScanRequest) -> None:
        """Right-click / right-drag in a chromatogram (times on the MS axis)."""
        if self.subtraction_state in ("apex", "base"):
            self._pick_subtraction(req)
            return
        if self.subtraction_state == "done":
            self.back_to_peak()
        if req.t0 is None:                       # Shift+right-drag: background only
            self.bg_range = req.bg
            if self.source == "scan" and self.scan_req is not None:
                self.refresh()
            else:
                self.ws.message.emit(f"Background {req.bg[0]:.3f}-{req.bg[1]:.3f} min set: right-click a time "
                                     "for its background-subtracted spectrum")
                self._sync_actions()
            return
        self.scan_req = req
        self.source = "scan"
        self.refresh()

    def show_component(self, run_id: str, comp) -> None:
        """A deconvoluted component (e.g. a hidden one from the whole-run deconvolution)."""
        self._reset_subtraction()
        self.component = (run_id, comp)
        self.source = "component"
        self.refresh()

    def back_to_peak(self):
        self._reset_subtraction()
        self.source = "peak"
        self.refresh()

    def _reset_subtraction(self):
        self.subtraction_state = "off"
        self.subtraction_run = self.subtraction_apex = self.subtraction_base = None
        self.subtractionChanged.emit(False)
        self._sync_actions()

    def toggle_subtraction(self):
        if self.subtraction_state != "off":
            self.back_to_peak()
            return
        st = self.ws.active
        if st is None or st.run.ms is None or not st.run.ms.n_scans:
            self._reset_subtraction()
            self.ws.message.emit("No MS scans available for baseline subtraction")
            return
        self.subtraction_run = st.id
        self.subtraction_state = "apex"
        self.subtractionChanged.emit(True)
        self._sync_actions()
        self.ws.message.emit("Subtract baseline: right-click the apex in a chromatogram; Escape cancels")

    def _pick_subtraction(self, req):
        req = self._on_subtraction_run(req)
        if req is None:
            self.back_to_peak()
            self.ws.message.emit("Baseline subtraction cancelled: the sample changed")
            return
        if req.t0 is None or (req.t1 is not None and abs(req.t1 - req.t0) > 1e-9):
            self.ws.message.emit("Select one scan with a right-click, not a range")
            return
        ms = self.ws.runs[req.run_id].run.ms
        scan = ms.scan_at_rt(req.t0)
        if self.subtraction_state == "apex":
            self.subtraction_apex = scan
            self.subtraction_state = "base"
            t = float(ms.rt[scan])
            self.regionsChanged.emit([(t - .001, t + .001, theme.PLOT["apex_region"])])
            self.ws.message.emit(f"Apex scan {scan + 1}: now right-click the baseline")
        elif scan == self.subtraction_apex:
            self.ws.message.emit("Apex and baseline are the same scan; choose another baseline scan")
        else:
            self.subtraction_base = scan
            self.subtraction_state = "done"
            self.source = "subtraction"
            self.scan_req = ScanRequest(req.run_id, float(ms.rt[self.subtraction_apex]), None)
            self.refresh()
            self.ws.message.emit("Baseline subtracted; click Subtract baseline or press Escape to clear")

    def _on_subtraction_run(self, req):
        """``req`` for the run being subtracted. A click resolved to another overlaid curve keeps its
        axis position and is moved to the subtraction run (FID axes differ by each run's delay)."""
        if req.run_id == self.subtraction_run:
            return req
        own, other = self.ws.runs.get(self.subtraction_run), self.ws.runs.get(req.run_id)
        if own is None or own.run.ms is None or other is None:
            return None
        frame = req.frame or "TIC"
        move = lambda t: None if t is None else to_ms(from_ms(t, frame, other.delay_value), frame, own.delay_value)
        return ScanRequest(own.id, move(req.t0), move(req.t1), req.bg, req.frame)

    def _refresh_subtraction(self):
        st = self.ws.runs.get(self.subtraction_run)
        if st is None:
            self.back_to_peak()
            return
        ms = st.run.ms
        a, b = self.subtraction_apex, self.subtraction_base
        ta, tb = float(ms.rt[a]), float(ms.rt[b])
        self.spec = extract_range(st.run, ta, bg=(tb, tb))
        title = f"Apex scan {a + 1} ({ta:.3f} min) − baseline scan {b + 1} ({tb:.3f} min)"
        self.plot.show_spectrum(self.spec.mz, self.spec.ab, title=title, marks=self._marks())
        self.info.setText(self.spec.note)
        self._fill_table()
        self._sync_actions()
        self._show_scan_trace(st, self.spec)
        self._spectrum_changed()

    def clear_background(self):
        self.bg_range = None
        self.refresh()

    def step(self, delta: int) -> None:
        """Move a scan spectrum by ``delta`` scans (a range keeps its width)."""
        req = self.scan_req
        st = self.ws.runs.get(req.run_id) if req else None
        if self.source != "scan" or st is None or st.run.ms is None or not self.spec or not self.spec.apex_scans:
            return
        ms = st.run.ms
        a, b = min(self.spec.apex_scans), max(self.spec.apex_scans)
        a, b = a + delta, b + delta
        if a < 0 or b >= ms.n_scans:
            return
        t0, t1 = float(ms.rt[a]), float(ms.rt[b])
        self.scan_req = ScanRequest(req.run_id, t0, t1 if b > a else t0, None)
        self.refresh()

    def keyPressEvent(self, ev):
        if self.source == "scan" and ev.key() in (Qt.Key_Left, Qt.Key_Right):
            self.step(-1 if ev.key() == Qt.Key_Left else 1)
            ev.accept()
            return
        if ev.key() == Qt.Key_Escape and (self.source != "peak" or self.subtraction_state != "off"
                                          or self.bg_range is not None):
            if self.subtraction_state == "off":
                self.bg_range = None           # Escape also clears a Shift+right-drag background
            self.back_to_peak()
            ev.accept()
            return
        super().keyPressEvent(ev)

    def target_peak(self):
        """``(RunState, Peak | None)``: the active run and the peak the spectrum on display belongs to.

        In scan mode that is the integrated peak containing the scan time, if any.
        """
        st = self.ws.active
        if st is None:
            return None, None
        if self.source == "peak":
            return st, self.ws.selected_peak()
        req = self.scan_req
        res = self.ws.active_result()
        if self.source == "component":
            if self.component is None or self.component[0] != st.id or res is None or self.spec is None:
                return st, None
        elif req is None or req.run_id != st.id or res is None or self.spec is None:
            return st, None
        t = from_ms(self.spec.rt, self.ws.signal_key, st.delay_value)
        return st, res.peak_at(t)

    # -- data -----------------------------------------------------------------

    def current_mode(self) -> str:
        return self.mode.currentData()

    def refresh(self):
        self._sync_blank_box()
        self.hits.setRowCount(0)
        if self.source == "subtraction" and self.subtraction_state == "done":
            self._refresh_subtraction()
            return
        if self.source == "scan" and self.scan_req is not None:
            self._refresh_scan()
            return
        if self.source == "component" and self.component is not None:
            self._refresh_component()
            return
        self.source = "peak"
        self._sync_actions()
        st = self.ws.active
        peak = self.ws.selected_peak()
        if st is None or peak is None or st.run.ms is None:
            self.spec = None
            hint = "select a peak, or right-click a chromatogram" if st and st.run.ms else "no MS data"
            self.plot.show_spectrum(None, None, title=hint)
            self.info.setText("")
            self.table.setRowCount(0)
            self.regionsChanged.emit([])
            self._spectrum_changed()
            return
        key = self.ws.signal_key
        override = override_for(st, key, peak)
        from gcws.ms import deconv_cache as DC
        dsettings = DC.settings_of(self.ws)
        self.spec = self._minus_blank(st, extract(st.run, peak, key, st.delay_value, self.current_mode(),
                                                  override=override,
                                                  component=lambda: DC.for_peak(st, peak, key, dsettings)))
        if self.spec is None:
            self.plot.show_spectrum(None, None, title="no MS data")
            self.info.setText("")
            self.table.setRowCount(0)
            self.regionsChanged.emit([])
            self._spectrum_changed()
            return
        ident = st.ident_set(key).for_peak(peak)
        title = f"RT {peak.apex_rt:.3f}" + (f"  (MS {self.spec.rt:.3f})" if is_fid(key) else "")
        if ident and ident.name:
            title += f"  -  {ident.name}"
        self.plot.show_spectrum(self.spec.mz, self.spec.ab, title=title, marks=self._marks())
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
                        item.setForeground(QColor(theme.OK))
                    self.hits.setItem(r, c, item)
        self._update_regions(st, peak, key)
        self._spectrum_changed()

    def _refresh_scan(self):
        req = self.scan_req
        st = self.ws.runs.get(req.run_id)
        if st is None or st.run.ms is None or not st.run.ms.n_scans:
            self.scan_req = None
            self.source = "peak"
            self.refresh()
            return
        ms = st.run.ms
        self.spec = self._minus_blank(st, extract_range(st.run, req.t0, req.t1, self.bg_range))
        spec = self.spec
        n = len(spec.apex_scans)
        if n == 1:
            s = spec.apex_scans[0]
            what = f"Scan {s + 1}  ·  {ms.rt[s]:.3f} min"
        else:
            what = f"Mean of {n} scans  ·  {ms.rt[spec.apex_scans[0]]:.3f}-{ms.rt[spec.apex_scans[-1]]:.3f} min"
        if spec.ab.size:
            what += f"  ·  base m/z {int(spec.mz[int(np.argmax(spec.ab))])}"
        if is_fid(self.ws.signal_key):
            what += f"  (FID {from_ms(spec.rt, self.ws.signal_key, st.delay_value):.3f})"
        if st.id != self.ws.active_id:
            what = f"{st.name}:  " + what
        self.plot.show_spectrum(spec.mz, spec.ab, title=what, marks=self._marks())
        self.info.setText(spec.note)
        self._fill_table()
        self._sync_actions()
        self._show_scan_trace(st, spec)
        self._spectrum_changed()

    def _refresh_component(self):
        from gcws.ms.spectra import Spectrum
        rid, comp = self.component
        st = self.ws.runs.get(rid)
        if st is None or st.run.ms is None:
            self.component = None
            self.source = "peak"
            self.refresh()
            return
        mz = np.array([int(m) for m, _ in comp.spectrum], dtype=int)
        ab = np.array([float(v) for _, v in comp.spectrum], dtype=float)
        self.spec = Spectrum(mz, ab, float(comp.rt), "deconvoluted", [int(comp.apex_scan)], [],
                             f"deconvoluted component: model m/z {comp.model_mz}, purity {comp.purity:.2f}")
        what = f"Component {comp.rt:.3f} min (MS)  ·  model m/z {comp.model_mz}"
        self.plot.show_spectrum(mz, ab, title=what, marks=self._marks())
        self.info.setText(self.spec.note)
        self._fill_table()
        self._sync_actions()
        self.regionsChanged.emit([(float(comp.rt) - 0.004, float(comp.rt) + 0.004, theme.PLOT["apex_region"])])
        self._spectrum_changed()

    def _show_scan_trace(self, st, spec):
        ms = st.run.ms
        scans = spec.apex_scans + spec.bg_scans
        t0, t1 = float(ms.rt[min(scans)]), float(ms.rt[max(scans)])
        w = max(t1 - t0, 0.15)
        sl = (ms.rt >= t0 - w) & (ms.rt <= t1 + w)
        sig = st.run.signal("TIC")
        self.scan_curve.setData(ms.rt[sl], sig.y[sl] if sig is not None else ms.tic()[sl])
        a0, a1 = float(ms.rt[min(spec.apex_scans)]), float(ms.rt[max(spec.apex_scans)])
        self.apex_reg.setRegion((a0 - 0.001, a1 + 0.001))
        regions = [(a0 - 0.001, a1 + 0.001, theme.PLOT["apex_region"])]
        if spec.bg_scans:
            b0, b1 = float(ms.rt[min(spec.bg_scans)]), float(ms.rt[max(spec.bg_scans)])
            self.bg_reg.setRegion((b0 - 0.001, b1 + 0.001))
            regions.append((b0 - 0.001, b1 + 0.001, theme.PLOT["bg_region"]))
        self.bg_reg.setVisible(bool(spec.bg_scans))
        self.scan_plot.getPlotItem().getViewBox().autoRange()
        self.regionsChanged.emit(regions if st.id == self.ws.active_id else [])

    def _sync_blank_box(self):
        from gcws.core.keys import is_derived
        self.minus_blank.blockSignals(True)
        if self.ws.signal_key != self._last_key:          # a blank trace brings blank spectra with it
            self.minus_blank.setChecked(is_derived(self.ws.signal_key))
            self._last_key = self.ws.signal_key
        self.minus_blank.blockSignals(False)
        self._sync_actions()

    def _minus_blank(self, st, spec):
        """The spectrum minus the blank's spectrum at the same scans, when asked for."""
        if spec is None or not spec.apex_scans or not self.minus_blank.isChecked() or not self.ws.blank_ids(st):
            return spec
        if spec.mode == "deconvoluted":
            from dataclasses import replace
            return replace(spec, note=spec.note + "; normalized component: raw blank subtraction not applied")
        from gcws.ms.spectra import Spectrum, subtract
        bmz, bab = self.ws.blank_spectrum(st, spec.apex_scans)
        if bmz.size == 0:
            return spec
        mz, ab = subtract((spec.mz, spec.ab), (bmz, bab))
        names = ", ".join(self.ws.runs[b].name for b in self.ws.blank_ids(st))
        return Spectrum(mz, ab, spec.rt, spec.mode, spec.apex_scans, spec.bg_scans,
                        (spec.note + "; " if spec.note else "") + f"blank spectrum subtracted ({names})")

    def _marks(self):
        """{m/z: (label, level)} annotations for the stick plot from the interpretation."""
        self.interp = self._interpret()
        return self.interp.marks() if self.interp is not None else None

    def _interpret(self):
        """Interpretation of the spectrum on display (cached per spectrum and context)."""
        spec = self.spec
        if spec is None or spec.ab.size == 0:
            return None
        from gcws.ms.interpret import Context, interpret
        rid = self.scan_req.run_id if (self.source == "scan" and self.scan_req) else (
            self.component[0] if (self.source == "component" and self.component) else self.ws.active_id)
        st = self.ws.runs.get(rid) if rid else None
        if st is None or st.run.ms is None:
            return None
        ms = st.run.ms
        hit = None
        if self.source == "peak":
            peak = self.ws.selected_peak()
            ident = st.ident_set(self.ws.signal_key).for_peak(peak) if peak is not None else None
            if ident is not None and ident.hits:
                h = ident.hits[0]
                hit = {"name": h.get("name", ""), "cas": h.get("cas", ""), "formula": h.get("formula", ""),
                       "mw": h.get("mw")}
        ri = None
        ladder = (self.ws.quant.get("ri") or {}).get("ladder") or {}
        if ladder:
            try:
                import gc_qc
                ri = gc_qc.retention_index(spec.rt + st.delay_value, {int(k): float(v) for k, v in ladder.items()})
            except Exception:  # noqa: BLE001 - RI is optional context
                ri = None
        import json
        digest = hashlib.sha256(np.asarray(spec.mz, dtype=np.float64).tobytes()
                                + np.asarray(spec.ab, dtype=np.float64).tobytes()).digest()
        key = (rid, spec.mode, tuple(spec.apex_scans), tuple(spec.bg_scans), digest,
               spec.rt, ri, json.dumps(hit, sort_keys=True), ms.mass_range(), ms.min_abundance())
        res = self._interp_cache.get(key)
        if res is None:
            lo, hi = ms.mass_range()
            ctx = Context(rt_ms=spec.rt, ri=ri, mass_range=(lo, hi), min_abundance=ms.min_abundance(),
                          library_hit=hit)
            try:
                res = interpret(spec.mz, spec.ab, ctx)
            except Exception:  # noqa: BLE001 - never break the spectrum panel
                import logging
                logging.getLogger(__name__).exception("interpretation failed")
                return None
            if len(self._interp_cache) > 64:
                self._interp_cache.clear()
            self._interp_cache[key] = res
        return res

    def _spectrum_changed(self):
        """The spectrum on display changed: refresh the interpretation tab."""
        self._sync_actions()
        self.plot.caption_overlay.reposition()
        if self.spec is None or self.spec.ab.size == 0:
            self.interp = None
        label = self.plot.caption.text() if self.spec is not None else ""
        import re
        self.interp_view.show_result(self.interp, re.sub(r"<[^>]+>", "", label or ""))
        if self.hits.rowCount() == 0 and self.interp is not None and self.tabs.currentWidget() is self.hits:
            self.tabs.setCurrentWidget(self.interp_view)

    def _fill_table(self):
        self.table.setRowCount(0)
        if self.spec is None or self.spec.ab.size == 0 or self.spec.ab.max() <= 0:
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
        if r < 0 or st is None or peak is None or self.spec is None or self.source != "peak":
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
        self.bg_reg.setVisible(bool(self.spec and self.spec.bg_scans))
        if spec and spec.apex_scans:
            a0, a1 = float(ms.rt[min(spec.apex_scans)]), float(ms.rt[max(spec.apex_scans)])
            self.apex_reg.setRegion((a0 - 0.001, a1 + 0.001))
            regions.append((a0 - 0.001, a1 + 0.001, theme.PLOT["apex_region"]))
        if spec and spec.bg_scans:
            b0, b1 = float(ms.rt[min(spec.bg_scans)]), float(ms.rt[max(spec.bg_scans)])
            self.bg_reg.setRegion((b0 - 0.001, b1 + 0.001))
            pre = [s for s in spec.bg_scans if s < min(spec.apex_scans or [0])]
            post = [s for s in spec.bg_scans if s > max(spec.apex_scans or [0])]
            for grp in (pre, post):
                if grp:
                    regions.append((float(ms.rt[min(grp)]) - 0.001, float(ms.rt[max(grp)]) + 0.001,
                                    theme.PLOT["bg_region"]))
        self.scan_plot.getPlotItem().getViewBox().autoRange()
        self.regionsChanged.emit(regions)

    def _use_regions(self):
        st = self.ws.active
        a0, a1 = self.apex_reg.getRegion()
        b0, b1 = self.bg_reg.getRegion()
        if self.source == "scan" and self.scan_req is not None:
            self.scan_req = ScanRequest(self.scan_req.run_id, a0, a1, None)
            if self.bg_reg.isVisible() and b1 > b0:
                self.bg_range = (b0, b1)
            self.refresh()
            return
        peak = self.ws.selected_peak()
        if st is None or peak is None:
            return
        ms = st.run.ms
        apex = [int(s) for s in ms.scans_between(a0, a1)]
        bg = [int(s) for s in ms.scans_between(b0, b1) if s not in apex] if self.bg_reg.isVisible() else []
        if not apex:
            return
        st.spectrum_overrides[override_key(self.ws.signal_key, peak)] = {"apex_scans": apex, "bg_scans": bg}
        self.ws.log("Spectrum scans", st.name, f"peak {peak.apex_rt:.3f}: apex {apex[0]}-{apex[-1]}, "
                                               f"{len(bg)} background scans")
        self.ws.spectrumChanged.emit(st.id)
        self.refresh()

    def _clear_override(self):
        st = self.ws.active
        peak = self.ws.selected_peak()
        if st is not None and peak is not None and override_for(st, self.ws.signal_key, peak):
            # A tombstone prevents legacy RT overrides from reappearing after release.
            st.spectrum_overrides[override_key(self.ws.signal_key, peak)] = {}
            self.ws.log("Spectrum scans", st.name, f"peak {peak.apex_rt:.3f}: automatic")
            self.ws.spectrumChanged.emit(st.id)
            self.refresh()

    # -- export ----------------------------------------------------------------

    def points(self):
        return self.spec.points(min_permille=0.0) if self.spec is not None else []

    def spectrum_name(self) -> str:
        st = self.ws.active
        if self.source == "subtraction" and self.spec is not None:
            return (f"{st.name if st else 'GC'} apex scan {self.subtraction_apex + 1} "
                    f"minus baseline scan {self.subtraction_base + 1}")
        if self.source == "scan" and self.scan_req is not None and self.spec is not None:
            run = self.ws.runs.get(self.scan_req.run_id)
            name = run.name if run is not None else "GC"
            return f"{name} MS {self.spec.rt:.3f}"
        if self.source == "component" and self.component is not None and self.spec is not None:
            run = self.ws.runs.get(self.component[0])
            return f"{run.name if run is not None else 'GC'} component {self.spec.rt:.3f}"
        p = self.ws.selected_peak()
        if st is None or p is None:
            return "GC unknown"
        return f"{st.name} RT {p.apex_rt:.3f}"

    def _fill_own_menu(self):
        from gcws.ui.dialogs import own_search as OS
        self.own_menu.clear()
        chosen = OS.load_options()["library"]
        names = OS.libraries()
        for name in names:
            a = self.own_menu.addAction(name)
            a.setCheckable(True)
            a.setChecked(name == chosen)
            a.triggered.connect(lambda _=False, n=name: OS.save_options({"library": n}))
        if not names:
            self.own_menu.addAction("(no library - add one under Identify > Libraries...)").setEnabled(False)
        self.own_menu.addSeparator()
        self.own_menu.addAction("Options...", lambda: OS.OwnSearchOptionsDialog(self).exec())

    def _scan_theme(self):
        self.apex_reg.setBrush(pg.mkBrush(theme.qcolor(theme.PLOT["apex_region"], 50)))
        self.bg_reg.setBrush(pg.mkBrush(theme.qcolor(theme.PLOT["bg_region"], 40)))
        self.scan_curve.setPen(pg.mkPen(theme.PLOT["secondary"]))

    def _emit(self, signal):
        pts = self.points()
        if pts:
            signal.emit(pts, self.spectrum_name())

    def msp(self) -> str:
        import gc_atlas
        _st, p = self.target_peak()
        rt = p.apex_rt if p is not None else (self.spec.rt if self.spec is not None else None)
        return gc_atlas.msp_text({"spectrum": self.points(), "rt": rt, "name": self.spectrum_name()})

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
