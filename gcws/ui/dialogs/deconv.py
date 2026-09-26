"""Spectral deconvolution (GC Workspace engine, see :mod:`gcws.ms.deconv`).

Scope: the selected peak's window, the visible range or the whole run (the
last two run in the background). The table shows every component with its
quality; a selected component's spectrum and interpretation are shown on
the right. Actions: split the peak between components, add components as
peaks, use a component's spectrum for the peak (pinned), EI Atlas search.
"""
from __future__ import annotations

import copy

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLineEdit, QPushButton,
                               QRadioButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from gcws.core.events import ManualEvent, ManualKind as K
from gcws.core.keys import is_fid
from gcws.ms import deconv as D
from gcws.ms import deconv_cache as DC
from gcws.ms.spectra import ms_times
from gcws.ui import theme, workers
from gcws.ui.docks.interpretation_view import InterpretationView
from gcws.ui.docks.spectrum import StickPlot
from gcws.ui.undo import ManualEventsCommand

COLS = ["RT (MS)", "Model m/z", "Quality", "Purity", "R²", "Ions", "S/N", "Area", "In peak", "Found", "Class hint"]


def _preset_key(settings: D.DeconvSettings, group: str) -> str:
    """The preset of ``group`` (Resolution/Sensitivity/Shape) closest to ``settings``."""
    best, dist = None, None
    for name, vals in D.PRESETS.items():
        if not name.startswith(group):
            continue
        d = sum(abs(float(getattr(settings, k)) - float(v)) for k, v in vals.items())
        if dist is None or d < dist:
            best, dist = name, d
    return best.split(" ", 1)[1] if best else "medium"


class DeconvolutionDialog(QDialog):
    def __init__(self, win, scope: str = "peak"):
        super().__init__(win)
        self.win = win
        self.ws = win.ws
        self.st = self.ws.active
        self.peak = self.ws.selected_peak()
        self.key = self.ws.active_key
        self.comps: list = []
        self._interps: list = []
        self.result = None
        self._job = None
        what = f"RT {self.peak.apex_rt:.3f}" if self.peak is not None else "whole run"
        self.setWindowTitle(f"Deconvolution - {self.st.name}, {what}")
        self.resize(1320, 800)
        s = DC.settings_of(self.ws)

        # -- settings --------------------------------------------------------------------------------
        self.presets = {}
        preset_box = QGroupBox("Settings")
        pf = QFormLayout(preset_box)
        for group, labels in (("Resolution", ("high", "medium", "low")), ("Sensitivity", ("high", "medium", "low")),
                              ("Shape", ("strict", "medium", "loose"))):
            c = QComboBox()
            c.addItems(labels)
            c.setCurrentText(_preset_key(s, group))
            c.activated.connect(lambda _i, g=group: self._apply_preset(g))
            self.presets[group] = c
            pf.addRow(group, c)
        adv = QWidget()
        af = QFormLayout(adv)
        af.setContentsMargins(0, 0, 0, 0)
        adv.setVisible(False)
        adv_toggle = QPushButton("Advanced settings ▸")
        adv_toggle.setCheckable(True)
        adv_toggle.setFlat(True)
        adv_toggle.toggled.connect(lambda on: (adv.setVisible(on),
                                               adv_toggle.setText("Advanced settings ▾" if on else
                                                                  "Advanced settings ▸")))
        self.win_spin = self._dspin(s.window, 0.05, 2.0, 3, " min", 0.05)
        self.noise = self._dspin(s.noise_factor, 1.0, 20.0, 1, " σ", 0.5)
        self.shape = self._dspin(s.shape_r, 0.3, 0.999, 2, "", 0.05)
        self.apex_tol = self._dspin(s.apex_tol, 0.2, 3.0, 2, " scans", 0.1)
        self.min_sep = self._dspin(s.min_sep, 0.2, 3.0, 2, " scans", 0.1)
        self.min_ions = QSpinBox()
        self.min_ions.setRange(2, 50)
        self.min_ions.setValue(s.min_ions)
        self.smooth = QSpinBox()
        self.smooth.setRange(0, 21)
        self.smooth.setSingleStep(2)
        self.smooth.setSpecialValueText("auto")
        self.smooth.setValue(s.smoothing)
        self.baseline = QCheckBox("Fit a background per m/z")
        self.baseline.setChecked(s.baseline)
        self.residual = QCheckBox("Look for hidden components in the residual")
        self.residual.setChecked(s.residual_passes > 0)
        self.skew = QCheckBox("Correct the scan skew")
        self.skew.setChecked(s.skew)
        self.exclude = QLineEdit(", ".join(str(v) for v in s.exclude_model))
        self.exclude.setToolTip("Masses never used as model ions (air, water, column bleed)")
        for label, w in (("Window (± min)", self.win_spin), ("Noise factor", self.noise),
                         ("Min. profile correlation", self.shape), ("Apex tolerance", self.apex_tol),
                         ("Min. separation", self.min_sep), ("Min. ions", self.min_ions), ("Smoothing", self.smooth),
                         ("Not as model ion", self.exclude)):
            af.addRow(label, w)
        for w in (self.baseline, self.residual, self.skew):
            af.addRow(w)
        self.scope = QButtonGroup(self)
        scope_box = QGroupBox("Deconvolute")
        sl = QVBoxLayout(scope_box)
        want = scope if (scope != "peak" or self.peak is not None) else "run"
        for i, (k, label) in enumerate((("peak", "around the selected peak"), ("range", "the visible range"),
                                        ("run", "the whole run (background, ~10 s)"))):
            rb = QRadioButton(label)
            rb.setProperty("scope", k)
            self.scope.addButton(rb, i)
            sl.addWidget(rb)
            rb.setChecked(k == want)
        self.scope.button(0).setEnabled(self.peak is not None)
        run = QPushButton("Deconvolute")
        theme.set_primary(run)
        run.clicked.connect(self.run)
        save = QPushButton("Save as default")
        save.setToolTip("Use these settings for the deconvoluted spectrum mode and the library search")
        save.clicked.connect(self.save_default)
        rb_row = QHBoxLayout()
        rb_row.addWidget(run)
        rb_row.addWidget(save)
        rb_row.addStretch(1)

        # -- results ----------------------------------------------------------------------------------
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._show)
        self.table.setSortingEnabled(True)
        b_split = QPushButton("Split the peak")
        b_split.setToolTip("Drop lines at the midpoints between the component apexes inside the peak (undoable)")
        b_split.clicked.connect(self.split)
        b_add = QPushButton("Add selected as peaks")
        b_add.setToolTip("Integrate the selected components as new peaks (undoable)")
        b_add.clicked.connect(self.add_peaks)
        b_pin = QPushButton("Use spectrum for the peak")
        b_pin.setToolTip("Pin the selected component's spectrum to the selected peak (library search, register)")
        b_pin.clicked.connect(self.pin)
        b_search = QPushButton("EI Atlas...")
        b_search.clicked.connect(self.search)
        actions = QHBoxLayout()
        for b in (b_split, b_add, b_pin, b_search):
            actions.addWidget(b)
        self.note = theme.hint("")

        left = QWidget()
        ll = QVBoxLayout(left)
        top = QHBoxLayout()
        top.addWidget(preset_box, 1)
        top.addWidget(scope_box, 1)
        ll.addLayout(top)
        ll.addWidget(adv_toggle)
        ll.addWidget(adv)
        ll.addLayout(rb_row)
        ll.addWidget(self.table, 1)
        ll.addLayout(actions)
        ll.addWidget(self.note)

        self.profiles = pg.PlotWidget()
        self.profiles.setLabel("bottom", "MS RT", units="min")
        self.profiles.setMenuEnabled(False)
        self.profiles.showGrid(x=True, y=True, alpha=theme.PLOT["grid_alpha"])
        self.profiles.addLegend(offset=(-10, 10))
        self.spec = StickPlot()
        self.interp = InterpretationView()
        tabs = QTabWidget()
        tabs.addTab(self.spec, "Spectrum")
        tabs.addTab(self.interp, "Interpretation")
        right = QSplitter(Qt.Vertical)
        right.addWidget(self.profiles)
        right.addWidget(tabs)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([640, 680])
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        self._curves = []
        cached = DC.whole_run(self.st, DC.settings_of(self.ws))
        if self.current_scope() == "run" and cached is not None:
            self._show_components(cached, None, "the whole run (cached)")
        elif self.current_scope() == "peak":
            self.run()

    # -- settings -----------------------------------------------------------------------------------------

    @staticmethod
    def _dspin(v, lo, hi, dec, suffix, step):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setSuffix(suffix)
        s.setSingleStep(step)
        s.setValue(v)
        return s

    def _apply_preset(self, group):
        vals = D.PRESETS.get(f"{group} {self.presets[group].currentText()}", {})
        widgets = {"apex_tol": self.apex_tol, "min_sep": self.min_sep, "noise_factor": self.noise,
                   "shape_r": self.shape, "min_ions": self.min_ions}
        for k, v in vals.items():
            widgets[k].setValue(v)

    def settings(self) -> D.DeconvSettings:
        import re
        excl = tuple(int(v) for v in re.findall(r"\d+", self.exclude.text()))
        return D.DeconvSettings(window=self.win_spin.value(), noise_factor=self.noise.value(),
                                shape_r=self.shape.value(), min_ions=self.min_ions.value(),
                                apex_tol=self.apex_tol.value(), min_sep=self.min_sep.value(),
                                smoothing=self.smooth.value(), baseline=self.baseline.isChecked(),
                                residual_passes=1 if self.residual.isChecked() else 0, skew=self.skew.isChecked(),
                                exclude_model=excl)

    def save_default(self):
        q = copy.deepcopy(self.ws.quant)
        q["deconv"] = self.settings().to_dict()
        if q.get("deconv") != self.ws.quant.get("deconv"):
            self.ws.push_quant("deconvolution settings", q, "deconvolution")
        self.note.setText("Settings saved: used by the deconvoluted spectrum mode and the library search.")

    def current_scope(self) -> str:
        b = self.scope.checkedButton()
        return b.property("scope") if b is not None else "peak"

    # -- running --------------------------------------------------------------------------------------------

    def run(self):
        scope = self.current_scope()
        s = self.settings()
        ms = self.st.run.ms
        if scope == "peak" and self.peak is not None:
            _t0, _t1, ta = ms_times(self.peak, self.key, self.st.delay_value)
            try:
                res = D.deconvolute_window(ms, ta, s)
            except Exception as exc:  # noqa: BLE001 - shown to the analyst
                self.note.setText(f"Deconvolution failed: {exc}")
                return
            self.result = res
            self._show_components(res.components, res, f"the window {res.t0:.2f}-{res.t1:.2f} min")
            return
        if scope == "range":
            (x0, x1), _ = self.win.chrom.vb.viewRange()
            shift = self.st.delay_value if is_fid(self.key) else 0.0
            t0, t1 = max(float(ms.rt[0]), x0 - shift), min(float(ms.rt[-1]), x1 - shift)
        else:
            t0, t1 = float(ms.rt[0]), float(ms.rt[-1])
        self.note.setText("Deconvoluting ...")
        st = self.st

        def job(progress=None):
            return D.deconvolute_range(ms, t0, t1, s, progress=progress)

        def done(comps, scope=scope):
            if scope == "run":
                DC.store_whole_run(st, s, comps)
                self.ws.deconvChanged.emit(st.id)
            self._show_components(comps, None, f"{t0:.2f}-{t1:.2f} min")

        self._job = workers.submit(job, with_progress=True, on_done=done, on_progress=self.note.setText,
                                   on_error=lambda e: self.note.setText("Deconvolution failed: " + e.splitlines()[0]))

    def _show_components(self, comps, res, where):
        from gcws.ms.interpret import Context, interpret
        self.comps = list(comps)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        t0 = t1 = None
        if self.peak is not None:
            t0, t1, _ta = ms_times(self.peak, self.key, self.st.delay_value)
        ms = self.st.run.ms
        ctx = Context(mass_range=ms.mass_range(), min_abundance=ms.min_abundance())
        self._interps = []
        for i, c in enumerate(self.comps):
            spec = c.spectrum_dict()
            it = interpret(list(spec), list(spec.values()), ctx) if spec else None
            self._interps.append(it)
            hint = it.classes[0].label if it is not None and it.classes else ""
            inside = "yes" if t0 is not None and t0 <= c.rt <= t1 else ""
            vals = [c.rt, c.model_mz, c.quality, c.purity, c.r2, c.n_ions, c.s_n, c.area, inside,
                    "under a peak" if c.hidden else "", hint]
            r = self.table.rowCount()
            self.table.insertRow(r)
            for col, v in enumerate(vals):
                item = QTableWidgetItem()
                if isinstance(v, float):
                    item.setData(Qt.DisplayRole, round(v, {0: 4, 2: 0, 3: 2, 4: 2, 6: 0, 7: 0}.get(col, 3)))
                else:
                    item.setData(Qt.DisplayRole, v)
                item.setData(Qt.UserRole, i)
                if col == 2:
                    item.setBackground(theme.status_brush("ok" if c.quality >= 60 else "warn" if c.quality >= 40
                                                          else "neutral"))
                self.table.setItem(r, col, item)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self._plot_profiles(res)
        n_in = sum(1 for c in self.comps if t0 is not None and t0 <= c.rt <= t1)
        extra = (f"; noise factor K {res.noise_k:.2f}, skew {res.skew * 100:.2f} scans/100 u, {res.elapsed:.2f} s"
                 if res is not None else "")
        self.note.setText(f"{len(self.comps)} components in {where}"
                          + (f", {n_in} inside the integrated peak" if t0 is not None else "") + extra)
        if self.comps:
            self.table.selectRow(0)

    def _plot_profiles(self, res):
        self.profiles.clear()
        ms = self.st.run.ms
        if res is not None:
            self.profiles.plot(res.rt, res.tic, pen=pg.mkPen(theme.PLOT["secondary"], width=1.2), name="TIC")
            self.profiles.plot(res.rt, res.residual, pen=pg.mkPen(theme.BAD, width=1, style=Qt.DashLine),
                               name="residual")
        elif self.comps:
            lo = min(float(c.profile_rt[0]) for c in self.comps if len(c.profile_rt))
            hi = max(float(c.profile_rt[-1]) for c in self.comps if len(c.profile_rt))
            sl = ms.scans_between(lo, hi)
            self.profiles.plot(ms.rt[sl], ms.tic()[sl], pen=pg.mkPen(theme.PLOT["secondary"], width=1.0), name="TIC")
        if self.peak is not None:
            t0, t1, _ = ms_times(self.peak, self.key, self.st.delay_value)
            self.profiles.addItem(pg.LinearRegionItem((t0, t1), movable=False,
                                                      brush=pg.mkBrush(*theme.PLOT["band"])))
        self._curves = []
        for i, c in enumerate(self.comps):
            if not len(c.profile_rt):
                self._curves.append(None)
                continue
            color = theme.RUN_COLORS[i % len(theme.RUN_COLORS)]
            self._curves.append(self.profiles.plot(c.profile_rt, np.asarray(c.profile_y),
                                                   pen=pg.mkPen(color, width=1.6)))

    # -- selection ------------------------------------------------------------------------------------------

    def _selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self.comps[self.table.item(r, 0).data(Qt.UserRole)] for r in rows]

    def _show(self):
        sel = self._selected()
        if not sel:
            return
        c = sel[0]
        k = self.comps.index(c)
        mz = np.array([m for m, _ in c.spectrum], float)
        ab = np.array([v for _, v in c.spectrum], float)
        it = self._interps[k] if k < len(self._interps) else None
        self.spec.show_spectrum(mz, ab, title=f"component {c.rt:.3f} min (model m/z {c.model_mz}, quality "
                                               f"{c.quality:.0f})", marks=it.marks() if it else None)
        self.interp.show_result(it, f"Component {c.rt:.3f} min")
        for j, curve in enumerate(self._curves):
            if curve is not None:
                color = theme.RUN_COLORS[j % len(theme.RUN_COLORS)]
                curve.setPen(pg.mkPen(color, width=3.0 if j == k else 1.2))

    # -- actions -------------------------------------------------------------------------------------------

    def split(self):
        if self.peak is None:
            return
        t0, t1, _ = ms_times(self.peak, self.key, self.st.delay_value)
        inside = sorted((c for c in self.comps if t0 <= c.rt <= t1), key=lambda c: c.rt)
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

    def add_peaks(self):
        sel = self._selected()
        if not sel:
            return
        shift = self.st.delay_value if is_fid(self.key) else 0.0
        events = list(self.st.events(self.key))
        for c in sel:
            y = np.asarray(c.profile_y)
            if y.size == 0 or y.max() <= 0:
                continue
            on = np.flatnonzero(y >= 0.05 * y.max())
            a, b = float(c.profile_rt[on[0]]), float(c.profile_rt[on[-1]])
            events.append(ManualEvent(K.ADD_PEAK, a + shift, b + shift, comment=f"deconvolution m/z {c.model_mz}"))
        self.st.undo.push(ManualEventsCommand(self.ws, self.st.id, self.key, events,
                                              f"add {len(sel)} deconvoluted component(s) as peaks"))
        self.note.setText(f"{len(sel)} peak(s) added (undo with Ctrl+Z).")

    def pin(self):
        sel = self._selected()
        if not sel or self.peak is None:
            self.note.setText("Select a peak in the chromatogram and a component here.")
            return
        c = sel[0]
        rt_key = round(self.peak.apex_rt, 4)
        self.st.spectrum_overrides[rt_key] = {"component": {"rt": float(c.rt), "model_mz": int(c.model_mz),
                                                            "spectrum": [[int(m), float(v)] for m, v in c.spectrum],
                                                            "quality": float(c.quality)}}
        self.ws.log("Spectrum: deconvoluted component", self.st.name,
                    f"peak {self.peak.apex_rt:.3f}: component {c.rt:.3f} min, model m/z {c.model_mz}")
        self.ws.selectionChanged.emit(self.st.id, self.ws.selected)
        self.note.setText(f"Peak {self.peak.apex_rt:.3f} now uses the spectrum of the component at {c.rt:.3f} min "
                          "(Automatic in the Scans tab releases it).")

    def search(self):
        sel = self._selected()
        if not sel:
            return
        c = sel[0]
        points = [(int(m), float(v)) for m, v in c.spectrum]
        self.win.atlas_hits(points, f"{self.st.name} component {c.rt:.3f}")
