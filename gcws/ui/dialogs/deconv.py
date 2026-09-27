"""Deconvolution: split a FID or TIC peak into its components, explore components.

The window opens on the selected peak ("peak first"). It shows the working
trace (FID or TIC, also "- Blank") around the peak with the fitted curves of
the deconvoluted components, the cut points and the relative areas
(:mod:`gcws.ms.peak_split`). The checked components are the ones a split
uses; weak ones (low S/N, almost no share of the signal) are listed but not
checked. *Split* replaces the peak by one fragment per component with the
original total area (undoable, replayed on every re-integration).

The visible range and the whole run list the components of a longer stretch,
computed in the background (the whole run is cached and marks components
without a peak in the chromatograms). For any component: add it as a peak,
use its spectrum for the selected peak, or open its library hits.

The MS components come from the original NIAS engine (:mod:`gcws.ms.deconv`).
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtCore import Signal as QtSignal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                               QFrame, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton,
                               QSpinBox, QSplitter, QTableView, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from gcws.core.events import ManualEvent, ManualKind as K
from gcws.core.keys import is_derived, is_fid
from gcws.ms import deconv as D
from gcws.ms import deconv_cache as DC
from gcws.ms import peak_split as PS
from gcws.ms.assignment import override_key
from gcws.ms.spectra import ms_times
from gcws.ui import theme, workers
from gcws.ui.docks.interpretation_view import InterpretationView
from gcws.ui.docks.spectrum import StickPlot
from gcws.ui.undo import ManualEventsCommand

SCOPES = (("peak", "Selected peak"), ("range", "Visible range"), ("run", "Whole run"))
PRESET_GROUPS = (("Resolution", ("high", "medium", "low")), ("Sensitivity", ("high", "medium", "low")),
                 ("Shape", ("strict", "medium", "loose")))
PROFILE_LIMIT = 40       # component profiles drawn for a range or the whole run


def component_color(index: int) -> str:
    return theme.RUN_COLORS[index % len(theme.RUN_COLORS)]


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


# -- component list ---------------------------------------------------------------------------------

@dataclass
class Row:
    comp: object
    number: int                    # the component's number in the plot (1-based)
    candidate: Optional[int]       # index into the split plan's candidates (checkable rows)
    reason: str = ""               # why a candidate is not checked by default
    hint: str = ""                 # substance-class hint of the interpretation


class ComponentModel(QAbstractTableModel):
    COLUMNS = ("#", "RT MS", "Model m/z", "Purity", "S/N", "Ions", "Share %", "Area", "Class hint")
    FORMATS = {1: "{:.4f}", 3: "{:.2f}", 4: "{:.0f}", 6: "{:.1f}", 7: "{:,.0f}"}
    SORT_ROLE = Qt.UserRole + 1
    toggled = QtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[Row] = []
        self.plan: Optional[PS.SplitPlan] = None
        self.checked: set[int] = set()

    def reset(self, rows: list[Row], plan=None) -> None:
        self.beginResetModel()
        self.rows = rows
        self.plan = plan
        self.checked = set(plan.checked) if plan is not None else set()
        self.endResetModel()

    def set_plan(self, plan) -> None:
        self.plan = plan
        self.checked = set(plan.checked)
        self._changed()

    def set_hint(self, row: Row, text: str) -> None:
        if row.hint != text:
            row.hint = text
            self._changed()

    def _changed(self) -> None:
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.COLUMNS) - 1))

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLUMNS[section]
        if orientation == Qt.Horizontal and role == Qt.ToolTipRole and section == 6:
            return "Share of the peak area the split gives this component"
        return None

    def flags(self, index):
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0 and self.rows[index.row()].candidate is not None:
            f |= Qt.ItemIsUserCheckable
        return f

    def _values(self, row: Row) -> tuple:
        c = row.comp
        share = area = None
        if self.plan is not None and row.candidate is not None:
            share, area = self.plan.share_of(row.candidate), self.plan.area_of(row.candidate)
        return (row.number, float(c.rt), int(c.model_mz), float(c.purity), float(c.s_n), int(c.n_ions),
                None if share is None else 100.0 * share, area, row.hint)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = self.rows[index.row()], index.column()
        if role == Qt.DisplayRole:
            value = self._values(row)[col]
            if value is None:
                return ""
            fmt = self.FORMATS.get(col)
            return fmt.format(value) if fmt else str(value)
        if role == self.SORT_ROLE:
            value = self._values(row)[col]
            if col == 8:
                return value or ""
            return -1e300 if value is None else float(value)
        if role == Qt.CheckStateRole and col == 0 and row.candidate is not None:
            return Qt.Checked if row.candidate in self.checked else Qt.Unchecked
        if role == Qt.DecorationRole and col == 0:
            return QColor(component_color(row.number - 1))
        if role == Qt.ForegroundRole and (row.reason or (row.candidate is None and self.plan is not None)):
            return theme.qcolor(theme.FAINT)
        if role == Qt.ToolTipRole:
            if row.reason:
                return f"Not checked by default: {row.reason}"
            if row.candidate is None and self.plan is not None:
                return "Outside the integrated peak"
        if role == Qt.TextAlignmentRole and col != 8:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.UserRole:
            return row
        return None

    def setData(self, index, value, role=Qt.EditRole):
        row = self.rows[index.row()]
        if role != Qt.CheckStateRole or index.column() != 0 or row.candidate is None:
            return False
        if Qt.CheckState(value) == Qt.Checked:
            self.checked.add(row.candidate)
        else:
            self.checked.discard(row.candidate)
        self.dataChanged.emit(index, index)
        self.toggled.emit()
        return True


# -- trace preview ----------------------------------------------------------------------------------

class TracePreview(pg.PlotWidget):
    """The working trace around the peak with the fitted component curves, or the TIC of a range."""

    def __init__(self):
        super().__init__()
        self.setMenuEnabled(False)
        self.getPlotItem().hideButtons()
        self.showGrid(x=True, y=True, alpha=theme.PLOT["grid_alpha"])
        self.curves: dict[int, object] = {}      # component index -> its curve item
        self._last = None
        theme.register_plot(self, self.redraw)

    def redraw(self) -> None:
        if self._last is not None:
            getattr(self, self._last[0])(*self._last[1])

    def reset(self) -> None:
        self._last = None
        self.curves = {}
        self.clear()

    def show_peak(self, sig, plan: PS.SplitPlan, selected: Optional[int] = None) -> None:
        self._last = ("show_peak", (sig, plan, selected))
        self.clear()
        self.curves = {}
        pk = plan.peak
        width = max(pk.end - pk.start, 1e-3)
        sl = sig.window(pk.start - 0.6 * width, pk.end + 0.6 * width)
        self.setLabel("bottom", f"{plan.signal_name} RT", units="min")
        self.plot(sig.rt[sl], sig.y[sl], pen=pg.mkPen(theme.PLOT["secondary"], width=1.4))
        if plan.t.size:
            t, fitted, first = plan.dense()
            base = pk.baseline.eval(t)
            self.plot(t, base, pen=pg.mkPen(theme.PLOT["baseline"], width=1.2, style=Qt.DashLine))
            for i in range(len(plan.candidates)):
                color = component_color(i)
                curve = fitted.get(i, first.get(i))
                if curve is None or not curve.max() > 0:
                    continue
                if i in fitted:
                    top = base + fitted[i]
                    low = pg.PlotCurveItem(t, base, pen=pg.mkPen(None))
                    high = pg.PlotCurveItem(t, top, pen=pg.mkPen(color, width=2.4 if i == selected else 1.4))
                    fill = pg.FillBetweenItem(low, high, brush=theme.qcolor(color, 120 if i == selected else 60))
                    for item in (low, fill, high):
                        self.addItem(item)
                else:
                    top = base + curve
                    high = self.plot(t, top, pen=pg.mkPen(color, width=2.0 if i == selected else 1.1,
                                                          style=Qt.DotLine))
                self.curves[i] = high
                k = int(np.argmax(top - base))
                label = pg.TextItem(str(i + 1), color=color, anchor=(0.5, 1.0))
                label.setPos(float(t[k]), float(top[k]))
                self.addItem(label)
            if fitted:
                total = np.sum(list(fitted.values()), axis=0)
                self.plot(t, base + total, pen=pg.mkPen(theme.PLOT["fg"], width=1.0, style=Qt.DashLine))
            if plan.ok:
                for x in plan.points:
                    self.addItem(pg.InfiniteLine(x, angle=90, pen=pg.mkPen(theme.qcolor(theme.PLOT["drop"]),
                                                                          width=1.2, style=Qt.DashLine)))
        self.enableAutoRange()

    def show_range(self, ms, comps: list, selected: Optional[int] = None) -> None:
        self._last = ("show_range", (ms, comps, selected))
        self.clear()
        self.curves = {}
        self.setLabel("bottom", "MS RT", units="min")
        profiled = [c for c in comps if len(c.profile_rt)]
        if not profiled:
            return
        lo = min(float(c.profile_rt[0]) for c in profiled)
        hi = max(float(c.profile_rt[-1]) for c in profiled)
        sl = ms.scans_between(lo, hi)
        self.plot(ms.rt[sl], ms.tic()[sl], pen=pg.mkPen(theme.PLOT["secondary"], width=1.0))
        shown = set(sorted(range(len(comps)), key=lambda i: comps[i].area, reverse=True)[:PROFILE_LIMIT])
        if selected is not None:
            shown.add(selected)
        for i in sorted(shown):
            c = comps[i]
            if len(c.profile_rt):
                self.curves[i] = self.plot(c.profile_rt, np.asarray(c.profile_y),
                                           pen=pg.mkPen(component_color(i), width=2.6 if i == selected else 1.3))

    def focus(self, rt: float) -> None:
        self.setXRange(rt - 0.15, rt + 0.15, padding=0)
        vb = self.getViewBox()
        vb.setAutoVisible(y=True)
        vb.enableAutoRange(axis="y")


# -- the window -------------------------------------------------------------------------------------

class DeconvolutionDialog(QDialog):
    def __init__(self, win, scope: str = "peak"):
        super().__init__(win)
        self.win = win
        self.ws = win.ws
        self.st = self.ws.active
        self.key = self.ws.active_key
        self.peak = self.ws.selected_peak()
        self.comps: list = []                    # components of the last calculation (after the solvent cut)
        self.plan: Optional[PS.SplitPlan] = None
        self.result = None                       # DeconvResult of the peak window
        self._where = ""
        self._interps: dict = {}
        self._job = None
        self._cancel = threading.Event()
        self._busy = False
        self._closed = False
        self._result_context = None
        self._applying = False
        self._links: list = []
        self.setWindowTitle(f"Deconvolution - {self.st.name}")
        self.resize(1320, 820)
        self._build(DC.settings_of(self.ws), scope)
        self._connect()
        cached = DC.whole_run(self.st, self.settings())
        if self.current_scope() == "run" and cached is not None:
            self._result_context = self._context()
            self._show_components(cached, None, None, "the whole run (cached)")
        elif self.current_scope() == "peak":
            self.run()
        else:
            self._set_message("Press Deconvolute to find the components.")

    # -- layout ------------------------------------------------------------------------------------

    def _build(self, s: D.DeconvSettings, scope: str) -> None:
        title = QLabel(self._title_text())
        font = title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 1)
        title.setFont(font)
        self.scope = QButtonGroup(self)
        self.scope.setExclusive(True)
        segments = QHBoxLayout()
        segments.setSpacing(0)
        want = scope if (scope != "peak" or self.peak is not None) else "run"
        for i, (k, label) in enumerate(SCOPES):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setProperty("scope", k)
            b.setChecked(k == want)
            self.scope.addButton(b, i)
            segments.addWidget(b)
        self.scope.button(0).setEnabled(self.peak is not None)
        self.scope.button(0).setToolTip("Components of the selected peak: fit, relative areas and split")
        self.scope.button(1).setToolTip("Components of the range shown in Chromatogram 1 (background)")
        self.scope.button(2).setToolTip("Components of the whole run (background, cached); components "
                                        "without a peak are marked in the chromatograms")
        self.settings_button = QToolButton()
        self.settings_button.setText("Settings")
        self.settings_button.setCheckable(True)
        self.settings_button.setArrowType(Qt.DownArrow)
        self.settings_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.run_button = QPushButton("Deconvolute")
        self.run_button.setToolTip("Find the components again with the current settings")
        self.run_button.clicked.connect(self.run)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumWidth(160)
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(lambda: self._invalidate("Calculation cancelled."))
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.progress)
        header.addWidget(self.cancel_button)
        header.addSpacing(12)
        header.addLayout(segments)
        header.addSpacing(12)
        header.addWidget(self.settings_button)
        header.addWidget(self.run_button)

        # settings (hidden until "Settings" is pressed)
        self.settings_panel = QFrame()
        self.settings_panel.setFrameShape(QFrame.StyledPanel)
        panel = QVBoxLayout(self.settings_panel)
        presets = QHBoxLayout()
        presets.addWidget(QLabel("Presets:"))
        self.presets = {}
        for group, labels in PRESET_GROUPS:
            c = QComboBox()
            c.addItems(labels)
            c.setCurrentText(_preset_key(s, group))
            c.activated.connect(lambda _i, g=group: self._apply_preset(g))
            self.presets[group] = c
            presets.addSpacing(8)
            presets.addWidget(QLabel(group))
            presets.addWidget(c)
        presets.addStretch(1)
        save = QPushButton("Save as default")
        save.setToolTip("Use these settings for the deconvoluted spectrum mode and the library search")
        save.clicked.connect(self.save_default)
        presets.addWidget(save)
        self.win_spin = self._dspin(s.window, 0.05, 2.0, 3, " min", 0.05)
        self.noise = self._dspin(s.noise_factor, 1.0, 20.0, 1, " σ", 0.5)
        self.shape = self._dspin(s.shape_r, 0.3, 0.999, 2, "", 0.05)
        self.apex_tol = self._dspin(s.apex_tol, 0.2, 3.0, 2, " scans", 0.1)
        self.min_ions = QSpinBox()
        self.min_ions.setRange(2, 50)
        self.min_ions.setValue(s.min_ions)
        advanced = (("Window ±", self.win_spin), ("Noise factor", self.noise), ("Min. profile r", self.shape),
                    ("Apex tolerance", self.apex_tol), ("Min. ions", self.min_ions))
        row = QHBoxLayout()
        row.addWidget(QLabel("Advanced:"))
        for label, w in advanced:
            row.addWidget(QLabel(label))
            row.addWidget(w)
            row.addSpacing(8)
        row.addStretch(1)
        panel.addLayout(presets)
        panel.addLayout(row)
        self.settings_panel.setVisible(False)
        self.settings_button.toggled.connect(self._toggle_settings)

        # left: trace and component list
        self.trace = TracePreview()
        self.model = ComponentModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(ComponentModel.SORT_ROLE)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(1, Qt.AscendingOrder)
        self.neighbours = QCheckBox("Show components outside the peak")
        list_box = QWidget()
        lb = QVBoxLayout(list_box)
        lb.setContentsMargins(0, 0, 0, 0)
        lb.addWidget(self.table, 1)
        lb.addWidget(self.neighbours)
        left = QSplitter(Qt.Vertical)
        left.addWidget(self.trace)
        left.addWidget(list_box)
        left.setSizes([430, 280])

        # right: the selected component
        self.spec = StickPlot()
        self.interp = InterpretationView()
        tabs = QTabWidget()
        tabs.addTab(self.spec, "Spectrum")
        tabs.addTab(self.interp, "Interpretation")
        self.b_search = QPushButton("Library hits...")
        self.b_search.setToolTip("Search the selected component's spectrum in your libraries")
        self.b_search.clicked.connect(self.search)
        self.b_pin = QPushButton("Use for peak spectrum")
        self.b_pin.setToolTip("The selected peak uses this component's spectrum for library search and register "
                              "(Automatic in the Scans tab releases it)")
        self.b_pin.clicked.connect(self.pin)
        spec_buttons = QHBoxLayout()
        spec_buttons.addWidget(self.b_search)
        spec_buttons.addWidget(self.b_pin)
        spec_buttons.addStretch(1)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(tabs, 1)
        rl.addLayout(spec_buttons)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([760, 560])

        # footer
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.b_add = QPushButton("Add as peaks")
        self.b_add.setToolTip("Integrate the selected components as new peaks (undoable)")
        self.b_add.clicked.connect(self.add_peaks)
        self.b_split = QPushButton("Split peak")
        theme.set_primary(self.b_split)
        self.b_split.setEnabled(False)
        self.b_split.clicked.connect(self.split)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        footer = QHBoxLayout()
        footer.addWidget(self.note, 1)
        footer.addWidget(self.b_add)
        footer.addWidget(self.b_split)
        footer.addWidget(close)
        self._action_buttons = [self.b_add, self.b_pin, self.b_search]
        for b in self._action_buttons:
            b.setEnabled(False)

        lay = QVBoxLayout(self)
        lay.addLayout(header)
        lay.addWidget(self.settings_panel)
        lay.addWidget(split, 1)
        lay.addLayout(footer)
        self._rerun = QTimer(self)
        self._rerun.setSingleShot(True)
        self._rerun.setInterval(300)
        self._rerun.timeout.connect(self._auto_run)

    def _title_text(self) -> str:
        what = "no peak selected"
        if self.peak is not None:
            p = self.peak
            what = f"peak {p.apex_rt:.3f} min ({p.start:.3f}–{p.end:.3f})"
        return f"{self.st.name} · {self.key} · {what}"

    def _connect(self) -> None:
        for w in (self.win_spin, self.noise, self.shape, self.apex_tol, self.min_ions):
            w.valueChanged.connect(self._settings_changed)
        self.scope.buttonClicked.connect(lambda *_: self._scope_changed())
        self.model.toggled.connect(self._checks_changed)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._show_selected())
        self.neighbours.toggled.connect(lambda *_: self._fill_rows())
        self.finished.connect(self._finished)
        links = ((self.ws.activeRunChanged, lambda *_: self._invalidate("Sample changed; reopen the deconvolution.")),
                 (self.ws.signalKeyChanged, lambda *_: self._invalidate("Signal changed; reopen the deconvolution.")),
                 (self.ws.solventCutChanged, lambda *_: self._invalidate()),
                 (self.ws.runRemoved, lambda rid: self._invalidate() if rid == self.st.id else None),
                 (self.ws.resultChanged, self._result_changed))
        for signal, slot in links:
            signal.connect(slot)
            self._links.append((signal, slot))

    @staticmethod
    def _dspin(v, lo, hi, dec, suffix, step):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setSuffix(suffix)
        s.setSingleStep(step)
        s.setValue(v)
        return s

    def _toggle_settings(self, on: bool) -> None:
        self.settings_panel.setVisible(on)
        self.settings_button.setArrowType(Qt.UpArrow if on else Qt.DownArrow)

    # -- settings ----------------------------------------------------------------------------------

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
        self._set_message("Settings saved: used by the deconvoluted spectrum mode and the library search.")

    def current_scope(self) -> str:
        b = self.scope.checkedButton()
        return b.property("scope") if b is not None else "peak"

    def _settings_changed(self, *_):
        self._invalidate("Settings changed.")
        if self.current_scope() == "peak":
            self._rerun.start()
        else:
            self._set_message("Settings changed; press Deconvolute.")

    def _scope_changed(self):
        self._invalidate()
        scope = self.current_scope()
        cached = DC.whole_run(self.st, self.settings())
        if scope == "peak":
            self.run()
        elif scope == "run" and cached is not None:
            self._result_context = self._context()
            self._show_components(cached, None, None, "the whole run (cached)")
        else:
            self._set_message("Press Deconvolute to find the components.")

    def _auto_run(self):
        if not self._closed and self.current_scope() == "peak":
            self.run()

    # -- running -----------------------------------------------------------------------------------

    def _context(self):
        return (self.settings().to_dict(), self.current_scope(), self.ws.active_id, self.ws.active_key,
                self.st.delay_value, self.ws.solvent_cut(self.st, "TIC"),
                id(self.ws.result(self.st.id, self.key)))

    def _result_changed(self, rid, key):
        if not self._applying and rid == self.st.id and key == self.key:
            self._invalidate("Peak integration changed; reopen the deconvolution for the current peak.")

    def _finished(self, _result):
        self._closed = True
        self._cancel.set()
        self._busy = False
        self._rerun.stop()
        for signal, slot in self._links:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._links = []

    def _set_busy(self, on: bool) -> None:
        self._busy = on
        self.progress.setVisible(on)
        self.cancel_button.setVisible(on)
        self.cancel_button.setEnabled(on)

    def _set_message(self, text: str) -> None:
        self.note.setText(text)

    def _invalidate(self, message="Settings or sample changed; run the deconvolution again."):
        self._cancel.set()
        if self._closed:
            return
        self._set_busy(False)
        self._result_context = None
        self.comps = []
        self.plan = None
        self._interps = {}
        self.model.reset([])
        self.trace.reset()
        self.spec.show_spectrum(None, None)
        self.interp.show_result(None, "")
        for button in self._action_buttons + [self.b_split]:
            button.setEnabled(False)
        self.b_split.setText("Split peak")
        self.b_split.setVisible(self.current_scope() == "peak")
        self._set_message(message)

    def _peak_current(self) -> bool:
        res = self.ws.result(self.st.id, self.key)
        return res is not None and any(p is self.peak for p in res.peaks)

    def _riders(self) -> list[tuple[float, float]]:
        res = self.ws.result(self.st.id, self.key)
        index = next((i for i, p in enumerate(res.peaks) if p is self.peak), None) if res is not None else None
        if index is None:
            return []
        return [(p.start, p.end) for p in res.peaks if p.parent == index]

    def run(self):
        self._rerun.stop()
        self._invalidate("Deconvoluting ...")
        if self.ws.active_id != self.st.id or self.ws.active_key != self.key:
            self._set_message("Sample or signal changed; reopen the deconvolution.")
            return
        scope = self.current_scope()
        if scope == "peak" and (self.peak is None or not self._peak_current()):
            self._set_message("Peak integration changed; reopen the deconvolution for the current peak.")
            return
        st, key, peak = self.st, self.key, self.peak
        ms = st.run.ms
        if ms is None or not ms.n_scans:
            self._set_message("No MS scans available.")
            return
        settings = self.settings()
        cut = self.ws.solvent_cut(st, "TIC")
        if scope == "range":
            (x0, x1), _ = self.win.chrom.vb.viewRange()
            shift = st.delay_value if is_fid(self.win.chrom.frame_key()) else 0.0
            t0, t1 = max(float(ms.rt[0]), x0 - shift), min(float(ms.rt[-1]), x1 - shift)
        else:
            t0, t1 = float(ms.rt[0]), float(ms.rt[-1])
        if cut is not None:
            t0 = max(t0, cut)
        if scope == "peak":
            sig = st.run.signal(key)
            ta = ms_times(peak, key, st.delay_value)[2]
            riders = self._riders()
            delay = st.delay_value
            cached = DC.cached_window(st, ta, settings)
        context = self._context()
        cancel = self._cancel = threading.Event()
        self._set_busy(True)
        # Destruction also cancels callbacks without accessing a deleted Qt wrapper.
        self.destroyed.connect(lambda *_: cancel.set())

        def job(progress=None):
            if cancel.is_set():
                return None
            if scope == "peak":
                res = cached if cached is not None else D.deconvolute_window(ms, ta, settings)
                comps = [c for c in res.components if cut is None or c.rt >= cut]
                return res, PS.plan_split(sig, peak, key, delay, comps, riders=riders)
            return D.deconvolute_range(ms, t0, t1, settings, progress=progress, cancel=cancel.is_set)

        def current():
            return not cancel.is_set() and not self._closed and context == self._context()

        def done(value):
            if not current():
                if not cancel.is_set() and not self._closed:
                    self._invalidate()
                return
            self._set_busy(False)
            self._result_context = context
            if scope == "peak":
                res, plan = value
                DC.store_window(st, ta, settings, res)
                self._show_components(res.components, res, plan, f"the window {res.t0:.2f}-{res.t1:.2f} min")
                return
            if scope == "run":
                DC.store_whole_run(st, settings, value)
                self.ws.deconvChanged.emit(st.id)
            self._show_components(value, None, None, f"{t0:.2f}-{t1:.2f} min")

        def failed(error):
            if current():
                self._invalidate("Deconvolution failed: " + error.splitlines()[0])
            elif not cancel.is_set() and not self._closed:
                self._invalidate()

        self._job = workers.submit(job, with_progress=True, on_done=done, on_error=failed,
                                   on_progress=lambda message: self._set_message(message) if current() else None)

    # -- showing -----------------------------------------------------------------------------------

    def _show_components(self, comps, res, plan, where):
        cut = self.ws.solvent_cut(self.st, "TIC")
        self.comps = [c for c in comps if cut is None or c.rt >= cut]
        self.result = res
        self.plan = plan
        self._where = where
        self._interps = {}
        self._fill_rows()
        for button in self._action_buttons:
            button.setEnabled(bool(self.comps))
        self._update_footer()

    def _fill_rows(self):
        rows = []
        if self.plan is not None:
            for i, cand in enumerate(self.plan.candidates):
                rows.append(Row(cand.component, i + 1, i, cand.reason))
            if self.neighbours.isChecked():
                inside = {id(c.component) for c in self.plan.candidates}
                for c in self.comps:
                    if id(c) not in inside:
                        rows.append(Row(c, len(rows) + 1, None))
        else:
            rows = [Row(c, i + 1, None) for i, c in enumerate(self.comps)]
        peak = self.plan is not None
        self.neighbours.setVisible(peak)
        self.b_split.setVisible(peak)
        for col in (6, 7):
            self.table.setColumnHidden(col, not peak)
        self.model.reset(rows, self.plan)
        if self.plan is not None:
            for row in rows[:8]:
                it = self._interpretation(row.comp)
                row.hint = it.classes[0].label if it and it.classes else ""
        self._draw()
        if rows:
            first = next((r for r, row in enumerate(rows) if row.candidate in self.model.checked), 0)
            self.table.selectRow(self.proxy.mapFromSource(self.model.index(first, 0)).row())

    def _draw(self, selected: Optional[int] = None):
        if self.plan is not None:
            self.trace.show_peak(self.st.run.signal(self.key), self.plan, selected)
        elif self.comps:
            self.trace.show_range(self.st.run.ms, self.comps, selected)
        else:
            self.trace.reset()

    def _update_footer(self):
        plan = self.plan
        if plan is None:
            text = f"{len(self.comps)} components in {self._where}; NIAS engine"
            if len(self.comps) > PROFILE_LIMIT:
                text += f"; profiles: the largest {PROFILE_LIMIT} plus the selected component"
            self.b_split.setEnabled(False)
            self.b_split.setText("Split peak")
            self.b_split.setToolTip("Choose 'Selected peak' to split a peak into its components")
        else:
            text = plan.summary()
            if plan.problem:
                text += " · " + plan.problem
            elif plan.basis == "fit":
                text += f" · relative areas from the {plan.signal_name} signal"
            n = len(plan.checked)
            self.b_split.setText(f"Split into {n} peaks" if plan.ok else "Split peak")
            self.b_split.setEnabled(plan.ok and not self._busy and self._result_context is not None)
            self.b_split.setToolTip(plan.problem or f"Replace the peak by {n} fragments; the original total area "
                                                    "is kept (undoable)")
        self._set_message(text)

    def _checks_changed(self):
        if self.plan is None:
            return
        self.plan = PS.replan(self.plan, sorted(self.model.checked))
        self.model.set_plan(self.plan)
        rows = self._selected_rows()
        self._draw(self._index_of(rows[0]) if rows else None)
        self._update_footer()

    def _interpretation(self, comp):
        key = id(comp)
        if key not in self._interps:
            from gcws.ms.interpret import Context, interpret
            mz = np.array([m for m, _ in comp.spectrum], float)
            ab = np.array([v for _, v in comp.spectrum], float)
            ms = self.st.run.ms
            self._interps[key] = interpret(mz, ab, Context(rt_ms=comp.rt, mass_range=ms.mass_range(),
                                                           min_abundance=ms.min_abundance())) if ab.size else None
        return self._interps[key]

    def _index_of(self, row: Row) -> Optional[int]:
        """The component's index in the plot (candidate index, or index into ``comps``)."""
        if self.plan is not None:
            return row.candidate
        return row.number - 1

    def _show_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        row = rows[0]
        c = row.comp
        it = self._interpretation(c)
        self.model.set_hint(row, it.classes[0].label if it and it.classes else "")
        mz = np.array([m for m, _ in c.spectrum], float)
        ab = np.array([v for _, v in c.spectrum], float)
        self.spec.show_spectrum(mz, ab, title=f"component {row.number}: {c.rt:.3f} min (MS), model m/z {c.model_mz}",
                                marks=it.marks() if it else None)
        self.interp.show_result(it, f"Component {row.number}, {c.rt:.3f} min")
        self._draw(self._index_of(row))
        if self.plan is None:
            self.trace.focus(float(c.rt))

    # -- selection ---------------------------------------------------------------------------------

    def _selected_rows(self) -> list[Row]:
        rows = sorted({self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.rows[r] for r in rows if r < len(self.model.rows)]

    def _selected(self):
        return [row.comp for row in self._selected_rows()]

    def _checked(self):
        """Components the split uses, in RT order."""
        if self.plan is None:
            return []
        return [self.plan.candidates[i].component for i in self.plan.checked]

    def set_checked(self, components) -> None:
        """Check exactly ``components`` (candidates of the peak)."""
        wanted = {id(c) for c in components}
        for r, row in enumerate(self.model.rows):
            if row.candidate is not None:
                on = id(row.comp) in wanted
                if (row.candidate in self.model.checked) != on:
                    self.model.setData(self.model.index(r, 0), Qt.Checked if on else Qt.Unchecked,
                                       Qt.CheckStateRole)

    # -- actions -----------------------------------------------------------------------------------

    def _ready(self):
        if self._closed or self._busy or self._result_context is None or self._result_context != self._context():
            self._set_message("Results are unavailable or obsolete; run the deconvolution again.")
            return False
        return True

    def split(self):
        if self.peak is None or self.plan is None or not self._ready():
            return
        plan = self.plan
        if not plan.ok:
            self._set_message("Peak was not split: " + plan.problem)
            return
        from gcws.integration.engine import integrate
        events = list(self.st.events(self.key))
        try:
            event = plan.event()
            events.append(event)
            sig = self.st.run.signal(self.key)
            method = (self.ws._derived_method(self.st, self.key, sig) if is_derived(self.key)
                      else self.ws.method_for(self.st, self.key))
            preview = integrate(sig, method, events, t_min=self.ws.solvent_cut(self.st, self.key))
            reason = dict(preview.unresolved).get(event.uid)
            if reason:
                self._set_message("Peak was not split: " + reason)
                return
        except ValueError as exc:
            self._set_message("Peak was not split: " + str(exc))
            return
        how = (f"fitted to the {plan.signal_name} signal" if plan.basis == "fit"
               else "MS component proportions")
        self._applying = True
        try:
            self.st.undo.push(ManualEventsCommand(
                self.ws, self.st.id, self.key, events,
                f"split peak {self.peak.apex_rt:.3f} into {len(plan.checked)} components ({how})"))
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
        self.ws.message.emit(f"Split into {len(indices)} components ({how}); original area preserved. "
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
        added = 0
        for c in sel:
            y = np.asarray(c.profile_y)
            if y.size == 0 or y.max() <= 0:
                continue
            on = np.flatnonzero(y >= 0.05 * y.max())
            a, b = float(c.profile_rt[on[0]]), float(c.profile_rt[on[-1]])
            events.append(ManualEvent(K.ADD_PEAK, a + shift, b + shift, comment=f"deconvolution m/z {c.model_mz}"))
            added += 1
        if not added:
            self._set_message("The selected components have no elution profile.")
            return
        self.st.undo.push(ManualEventsCommand(self.ws, self.st.id, self.key, events,
                                              f"add {added} deconvoluted component(s) as peaks"))
        self._set_message(f"{added} peak(s) added (undo with Ctrl+Z).")

    def pin(self):
        if not self._ready():
            return
        sel = self._selected()
        if not sel or self.peak is None:
            self._set_message("Select a peak in the chromatogram and a component here.")
            return
        c = sel[0]
        rt_key = override_key(self.key, self.peak)
        self.st.spectrum_overrides[rt_key] = {"component": {"rt": float(c.rt), "model_mz": int(c.model_mz),
                                                            "spectrum": [[int(m), float(v)] for m, v in c.spectrum]}}
        self.ws.log("Spectrum: deconvoluted component", self.st.name,
                    f"peak {self.peak.apex_rt:.3f}: component {c.rt:.3f} min, model m/z {c.model_mz}")
        self.ws.spectrumChanged.emit(self.st.id)
        self.ws.selectionChanged.emit(self.st.id, self.ws.selected)
        self._set_message(f"Peak {self.peak.apex_rt:.3f} now uses the spectrum of the component at {c.rt:.3f} min "
                          "(Automatic in the Scans tab releases it).")

    def search(self):
        sel = self._selected()
        if not sel:
            return
        c = sel[0]
        self.win.spectrum.show_component(self.st.id, c)
        points = [(int(m), float(v)) for m, v in c.spectrum]
        self.win.atlas_hits(points, f"{self.st.name} component {c.rt:.3f}")
