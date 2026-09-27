"""Spectral deconvolution using the original NIAS engine.

Scope: the selected peak's window, the visible range or the whole run, all
computed in the background. The table shows every component with its
purity; a selected component's spectrum and interpretation are shown on
the right. Actions: split the peak between components, add components as
peaks, use a component's spectrum for the peak (pinned), EI Atlas search.
"""
from __future__ import annotations

import copy
import threading

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QComboBox, QDialog, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QPushButton,
                               QRadioButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from gcws.core.events import ManualEvent, ManualKind as K
from gcws.core.keys import is_fid, base_key
from gcws.ms import deconv as D
from gcws.ms import deconv_cache as DC
from gcws.ms.spectra import ms_times
from gcws.ms.assignment import override_key
from gcws.ui import theme, workers
from gcws.ui.docks.interpretation_view import InterpretationView
from gcws.ui.docks.spectrum import StickPlot
from gcws.ui.undo import ManualEventsCommand

COLS = ["Use / RT (MS)", "Model m/z", "Purity", "Ions", "S/N", "MS component area",
        "In peak", "Class hint", "Share (%)", "Allocated area"]


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
        self._interps: dict = {}
        self.result = None
        self._job = None
        self._cancel = threading.Event()
        self._busy = False
        self._closed = False
        self._result_context = None
        self._applying = False
        what = f"RT {self.peak.apex_rt:.3f}" if self.peak is not None else "whole run"
        self.setWindowTitle(f"Deconvolution - {self.st.name}, {what}")
        self.resize(1320, 800)
        s = DC.settings_of(self.ws)

        # -- settings --------------------------------------------------------------------------------
        self.presets = {}
        preset_box = QGroupBox("NIAS deconvolution settings")
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
        self.min_ions = QSpinBox()
        self.min_ions.setRange(2, 50)
        self.min_ions.setValue(s.min_ions)
        for label, w in (("Window (± min)", self.win_spin), ("Noise factor", self.noise),
                         ("Min. profile correlation", self.shape), ("Apex tolerance", self.apex_tol),
                         ("Min. ions", self.min_ions)):
            af.addRow(label, w)
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
        self.cancel_button = QPushButton("Cancel calculation")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(lambda: self._invalidate("Calculation cancelled."))
        rb_row.addWidget(self.cancel_button)
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
        # Keep the split decision visible before the optional diagnostic columns.
        self.table.horizontalHeader().moveSection(8, 3)
        self.table.horizontalHeader().moveSection(9, 4)
        self.table.itemSelectionChanged.connect(self._show)
        self.table.itemChanged.connect(lambda _item: self._preview_changed())
        self.table.setSortingEnabled(True)
        b_split = QPushButton("Split the peak")
        self.b_split = b_split
        b_split.setEnabled(False)
        b_split.setToolTip("Allocate the original FID/TIC total among checked components (undoable). "
                           "FID shares are estimates based on MS response.")
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
        self.preview = theme.hint("")
        self._action_buttons = [b_split, b_add, b_pin, b_search]

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
        ll.addWidget(self.preview)
        ll.addLayout(actions)
        ll.addWidget(self.note)

        self.profiles = pg.PlotWidget()
        self.profiles.setLabel("bottom", "MS RT", units="min")
        self.profiles.setMenuEnabled(False)
        theme.register_plot(self.profiles)
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
        for widget in (self.win_spin, self.noise, self.shape, self.apex_tol, self.min_ions):
            widget.valueChanged.connect(lambda *_: self._invalidate())
        self.scope.buttonClicked.connect(lambda *_: self._invalidate())
        self.ws.activeRunChanged.connect(lambda *_: self._invalidate())
        self.ws.signalKeyChanged.connect(lambda *_: self._invalidate())
        self.ws.solventCutChanged.connect(lambda *_: self._invalidate())
        self.ws.runRemoved.connect(lambda rid: self._invalidate() if rid == self.st.id else None)
        self.ws.resultChanged.connect(self._result_changed)
        self.finished.connect(self._finished)
        cached = DC.whole_run(self.st, DC.settings_of(self.ws))
        if self.current_scope() == "run" and cached is not None:
            self._result_context = self._context()
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
        widgets = {"apex_tol": self.apex_tol, "noise_factor": self.noise,
                   "shape_r": self.shape, "min_ions": self.min_ions}
        for k, v in vals.items():
            widgets[k].setValue(v)

    def settings(self) -> D.DeconvSettings:
        return D.DeconvSettings(window=self.win_spin.value(), noise_factor=self.noise.value(),
                                shape_r=self.shape.value(), min_ions=self.min_ions.value(),
                                apex_tol=self.apex_tol.value())

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

    def _context(self):
        return (self.settings().to_dict(), self.current_scope(), self.ws.active_id, self.ws.active_key,
                self.st.delay_value, self.ws.solvent_cut(self.st, "TIC"),
                id(self.ws.result(self.st.id, self.key)))

    def _result_changed(self, rid, key):
        if not self._applying and rid == self.st.id and key == self.key:
            self._invalidate("Peak integration changed; reopen deconvolution for the current peak.")

    def _finished(self, _result):
        self._closed = True
        self._cancel.set()
        self._busy = False

    def _invalidate(self, message="Settings or sample changed; run deconvolution again."):
        self._cancel.set()
        if self._closed:
            return
        self._busy = False
        self._result_context = None
        self.comps = []
        self._interps = {}
        with QSignalBlocker(self.table):
            self.table.setRowCount(0)
        self.profiles.clear()
        self._curves = []
        self.spec.show_spectrum(None, None)
        self.interp.show_result(None, "")
        self.cancel_button.setEnabled(False)
        for button in self._action_buttons:
            button.setEnabled(False)
        self.preview.setText("")
        self.note.setText(message)

    def run(self):
        self._invalidate("Deconvoluting ...")
        if self.ws.active_id != self.st.id or self.ws.active_key != self.key:
            self.note.setText("Sample or signal changed; reopen deconvolution.")
            return
        if self.peak is not None:
            res = self.ws.result(self.st.id, self.key)
            if res is None or not any(p is self.peak for p in res.peaks):
                self.note.setText("Peak integration changed; reopen deconvolution for the current peak.")
                return
        scope = self.current_scope()
        s = self.settings()
        ms = self.st.run.ms
        if ms is None or not ms.n_scans:
            self.note.setText("No MS scans available.")
            return
        if scope == "range":
            (x0, x1), _ = self.win.chrom.vb.viewRange()
            shift = self.st.delay_value if is_fid(self.win.chrom.frame_key()) else 0.0
            t0, t1 = max(float(ms.rt[0]), x0 - shift), min(float(ms.rt[-1]), x1 - shift)
        else:
            t0, t1 = float(ms.rt[0]), float(ms.rt[-1])
        cut = self.ws.solvent_cut(self.st, "TIC")
        if cut is not None:
            t0 = max(t0, cut)
        self.note.setText("Deconvoluting ...")
        st = self.st
        context = self._context()
        cancel = self._cancel = threading.Event()
        self._busy = True
        self.cancel_button.setEnabled(True)
        # Destruction also cancels callbacks without accessing a deleted Qt wrapper.
        self.destroyed.connect(lambda *_: cancel.set())
        ta = ms_times(self.peak, self.key, st.delay_value)[2] if self.peak is not None else None

        def job(progress=None):
            if cancel.is_set():
                return None
            if scope == "peak":
                return D.deconvolute_window(ms, ta, s)
            return D.deconvolute_range(ms, t0, t1, s, progress=progress, cancel=cancel.is_set)

        def current():
            return not cancel.is_set() and not self._closed and context == self._context()

        def done(value):
            if not current():
                if not cancel.is_set() and not self._closed:
                    self._invalidate()
                return
            self._busy = False
            self.cancel_button.setEnabled(False)
            self._result_context = context
            res = value if scope == "peak" else None
            comps = res.components if res is not None else value
            self.result = res
            if scope == "run":
                DC.store_whole_run(st, s, comps)
                self.ws.deconvChanged.emit(st.id)
            where = f"the window {res.t0:.2f}-{res.t1:.2f} min" if res else f"{t0:.2f}-{t1:.2f} min"
            self._show_components(comps, res, where)

        def failed(error):
            if current():
                self._invalidate("Deconvolution failed: " + error.splitlines()[0])
            elif not cancel.is_set() and not self._closed:
                self._invalidate()

        self._job = workers.submit(job, with_progress=True, on_done=done, on_error=failed,
                                   on_progress=lambda message: self.note.setText(message) if current() else None)

    def _show_components(self, comps, res, where):
        cut = self.ws.solvent_cut(self.st, "TIC")
        self.comps = [c for c in comps if cut is None or c.rt >= cut]
        blocker = QSignalBlocker(self.table)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        t0 = t1 = None
        if self.peak is not None:
            t0, t1, _ta = ms_times(self.peak, self.key, self.st.delay_value)
        self._interps = {}
        for i, c in enumerate(self.comps):
            inside = "yes" if t0 is not None and t0 <= c.rt <= t1 else ""
            vals = [c.rt, c.model_mz, c.purity, c.n_ions, c.s_n, c.area, inside, "", "", ""]
            r = self.table.rowCount()
            self.table.insertRow(r)
            for col, v in enumerate(vals):
                item = QTableWidgetItem()
                if isinstance(v, float):
                    item.setData(Qt.DisplayRole, round(v, {0: 4, 2: 2, 4: 0, 5: 0}.get(col, 3)))
                else:
                    item.setData(Qt.DisplayRole, v)
                item.setData(Qt.UserRole, i)
                if col == 0 and inside:
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Checked)
                self.table.setItem(r, col, item)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        del blocker
        self._plot_profiles(res)
        n_in = sum(1 for c in self.comps if t0 is not None and t0 <= c.rt <= t1)
        extra = f"; NIAS engine, {res.elapsed:.2f} s" if res is not None else "; NIAS engine"
        if len(self.comps) > 40:
            extra += "; profiles: largest 39 plus selected component"
        self.note.setText(f"{len(self.comps)} components in {where}"
                          + (f", {n_in} inside the integrated peak" if t0 is not None else "") + extra)
        for button in self._action_buttons:
            button.setEnabled(bool(self.comps))
        self._preview_changed()
        if self.comps:
            first = next((r for r in range(self.table.rowCount())
                          if self.table.item(r, 0).checkState() == Qt.Checked), 0)
            self.table.selectRow(first)

    def _plot_profiles(self, res, selected=None):
        self.profiles.clear()
        ms = self.st.run.ms
        if res is not None:
            self.profiles.plot(res.rt, res.tic, pen=pg.mkPen(theme.PLOT["secondary"], width=1.2), name="TIC")
        elif any(len(c.profile_rt) for c in self.comps):
            lo = min(float(c.profile_rt[0]) for c in self.comps if len(c.profile_rt))
            hi = max(float(c.profile_rt[-1]) for c in self.comps if len(c.profile_rt))
            sl = ms.scans_between(lo, hi)
            self.profiles.plot(ms.rt[sl], ms.tic()[sl], pen=pg.mkPen(theme.PLOT["secondary"], width=1.0), name="TIC")
        if self.peak is not None:
            t0, t1, _ = ms_times(self.peak, self.key, self.st.delay_value)
            self.profiles.addItem(pg.LinearRegionItem((t0, t1), movable=False,
                                                      brush=pg.mkBrush(*theme.PLOT["band"])))
        self._curves = []
        visible = set(sorted(range(len(self.comps)), key=lambda i: self.comps[i].area, reverse=True)[:39])
        if selected is not None:
            visible.add(selected)
        for i, c in enumerate(self.comps):
            if i not in visible or not len(c.profile_rt):
                self._curves.append(None)
                continue
            color = theme.RUN_COLORS[i % len(theme.RUN_COLORS)]
            self._curves.append(self.profiles.plot(c.profile_rt, np.asarray(c.profile_y),
                                                   pen=pg.mkPen(color, width=1.6)))

    # -- selection ------------------------------------------------------------------------------------------

    def _checked(self):
        return sorted((self.comps[self.table.item(r, 0).data(Qt.UserRole)]
                       for r in range(self.table.rowCount())
                       if self.table.item(r, 0).checkState() == Qt.Checked), key=lambda c: c.rt)

    def _preview_changed(self):
        from gcws.integration.deconv_split import share_exactly
        checked = self._checked()
        supported = base_key(self.key) in ("FID", "TIC")
        valid = False
        shares, areas = {}, {}
        if self.peak is not None and supported:
            try:
                weights = [c.area for c in checked]
                shares = dict(zip(map(id, checked), share_exactly(100.0, weights)))
                areas = dict(zip(map(id, checked), share_exactly(self.peak.area, weights)))
                valid = len(checked) >= 2
                text = (f"Parent total: {self.peak.area:,.6f} · {len(checked)} checked components · "
                        f"allocated total: {sum(areas.values()):,.6f}")
                if is_fid(self.key):
                    text += " · FID shares estimated from MS response"
            except ValueError as exc:
                text = str(exc) if checked else "Check at least two components inside the peak."
        else:
            text = "Proportional splitting supports FID and TIC peaks only."
        self.preview.setText(text)
        with QSignalBlocker(self.table):
            sorting = self.table.isSortingEnabled()
            self.table.setSortingEnabled(False)
            for row in range(self.table.rowCount()):
                comp = self.comps[self.table.item(row, 0).data(Qt.UserRole)]
                for col, values in ((8, shares), (9, areas)):
                    value = values.get(id(comp))
                    self.table.item(row, col).setData(Qt.DisplayRole, round(value, 6) if value is not None else "")
            self.table.setSortingEnabled(sorting)
        self.b_split.setEnabled(valid and not self._busy and self._result_context is not None)

    def _selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self.comps[self.table.item(r, 0).data(Qt.UserRole)] for r in rows]

    def _show(self):
        sel = self._selected()
        if not sel:
            return
        c = sel[0]
        k = next(i for i, item in enumerate(self.comps) if item is c)
        mz = np.array([m for m, _ in c.spectrum], float)
        ab = np.array([v for _, v in c.spectrum], float)
        if k not in self._interps:
            from gcws.ms.interpret import Context, interpret
            ms = self.st.run.ms
            self._interps[k] = interpret(mz, ab, Context(rt_ms=c.rt, mass_range=ms.mass_range(),
                                                        min_abundance=ms.min_abundance())) if ab.size else None
        it = self._interps[k]
        with QSignalBlocker(self.table):
            row = next(r for r in range(self.table.rowCount()) if self.table.item(r, 0).data(Qt.UserRole) == k)
            self.table.item(row, 7).setText(it.classes[0].label if it and it.classes else "")
        if len(c.profile_rt) and (k >= len(self._curves) or self._curves[k] is None):
            self._plot_profiles(self.result, selected=k)
        self.spec.show_spectrum(mz, ab, title=f"component {c.rt:.3f} min (model m/z {c.model_mz})",
                               marks=it.marks() if it else None)
        self.interp.show_result(it, f"Component {c.rt:.3f} min")
        for j, curve in enumerate(self._curves):
            if curve is not None:
                color = theme.RUN_COLORS[j % len(theme.RUN_COLORS)]
                curve.setPen(pg.mkPen(color, width=3.0 if j == k else 1.2))

    # -- actions -------------------------------------------------------------------------------------------

    def _ready(self):
        if self._closed or self._busy or self._result_context is None or self._result_context != self._context():
            self.note.setText("Results are unavailable or obsolete; run deconvolution again.")
            return False
        return True

    def split(self):
        if self.peak is None or not self._ready():
            return
        if base_key(self.key) not in ("FID", "TIC"):
            self.note.setText("Proportional splitting supports FID and TIC peaks only.")
            return
        inside = self._checked()
        if len(inside) < 2:
            self.note.setText("Check at least two components inside the peak - nothing to split.")
            return
        shift = self.st.delay_value if is_fid(self.key) else 0.0
        events = list(self.st.events(self.key))
        from gcws.integration.deconv_split import create_event
        from gcws.integration.engine import integrate
        from gcws.core.keys import is_derived
        try:
            if any(not c.spectrum or not any(a > 0 for _, a in c.spectrum) for c in inside):
                raise ValueError("Every selected component needs a nonempty spectrum")
            event = create_event(self.peak, inside, shift, signal_key=self.key)
            events.append(event)
            sig = self.st.run.signal(self.key)
            method = (self.ws._derived_method(self.st, self.key, sig) if is_derived(self.key)
                      else self.ws.method_for(self.st, self.key))
            preview = integrate(sig, method, events, t_min=self.ws.solvent_cut(self.st, self.key))
            reason = dict(preview.unresolved).get(event.uid)
            if reason:
                self.note.setText("Peak was not split: " + reason)
                return
        except ValueError as exc:
            self.note.setText("Peak was not split: " + str(exc))
            return
        self._applying = True
        try:
            self.st.undo.push(ManualEventsCommand(self.ws, self.st.id, self.key, events,
                                                  f"split peak {self.peak.apex_rt:.3f} into {len(inside)} components"))
        finally:
            self._applying = False
        result = self.ws.result(self.st.id, self.key)
        indices = [i for i, p in enumerate(result.peaks)
                   if p.extra.get("spectrum_id", "").startswith(event.uid + ":")]
        if indices:
            self.ws.select_peak(indices[0])
        from gcws.ui.models.peak_filter import visible_indices
        shown = visible_indices(self.ws, self.st.id, self.key, self.win.table.filter_state())
        hidden = len(set(indices) - shown)
        self.ws.message.emit(f"Split into {len(indices)} components; original area preserved. "
                             f"{hidden} fragment(s) hidden by the current table filter.")
        self.accept()

    def add_peaks(self):
        if not self._ready():
            return
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
        if not self._ready():
            return
        sel = self._selected()
        if not sel or self.peak is None:
            self.note.setText("Select a peak in the chromatogram and a component here.")
            return
        c = sel[0]
        rt_key = override_key(self.key, self.peak)
        self.st.spectrum_overrides[rt_key] = {"component": {"rt": float(c.rt), "model_mz": int(c.model_mz),
                                                            "spectrum": [[int(m), float(v)] for m, v in c.spectrum]}}
        self.ws.log("Spectrum: deconvoluted component", self.st.name,
                    f"peak {self.peak.apex_rt:.3f}: component {c.rt:.3f} min, model m/z {c.model_mz}")
        self.ws.spectrumChanged.emit(self.st.id)
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
