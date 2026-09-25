"""Integration method editor: parameters, timed events and manual events."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton,
                               QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from gcws.core.model import FID, parse_key
from gcws.integration.method import CHOICE_EVENTS, EventKind, IntegrationMethod, TimedEvent, VALUE_EVENTS
from gcws.ui.icons import icon
from gcws.ui.undo import ManualEventsCommand, SetMethodCommand


class AutoSpin(QWidget):
    """Numeric field with an 'Auto' checkbox (None = automatic)."""

    def __init__(self, decimals=4, maximum=1e12, suffix="", step=None):
        super().__init__()
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(decimals)
        self.spin.setMaximum(maximum)
        self.spin.setSuffix(suffix)
        if step:
            self.spin.setSingleStep(step)
        self.auto = QCheckBox("Auto")
        self.hint = QLabel()
        self.hint.setStyleSheet("color:#777;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.auto)
        lay.addWidget(self.spin, 1)
        lay.addWidget(self.hint)
        self.auto.toggled.connect(lambda on: self.spin.setEnabled(not on))

    def set(self, value, resolved=None):
        self.auto.setChecked(value is None)
        if value is not None:
            self.spin.setValue(float(value))
        elif resolved is not None:
            self.spin.setValue(float(resolved))
        self.hint.setText("" if resolved is None else f"({resolved:.4g})")

    def get(self):
        return None if self.auto.isChecked() else float(self.spin.value())


class EventsDock(QWidget):
    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.method: IntegrationMethod | None = None
        self._loading = False

        self.method_box = QComboBox()
        self.method_box.setToolTip("Stored integration methods")
        self.method_box.activated.connect(self._pick_method)
        save = QPushButton("Save as...")
        save.clicked.connect(self._save_as)
        top = QHBoxLayout()
        top.addWidget(QLabel("Method:"))
        top.addWidget(self.method_box, 1)
        top.addWidget(save)

        # parameters
        self.pw = AutoSpin(4, 5, " min", 0.001)
        self.slope = AutoSpin(2, 1e6, " ×σ", 0.5)
        self.threshold = AutoSpin(1, 1e12)
        self.smooth = QSpinBox()
        self.smooth.setRange(0, 201)
        self.smooth.setSpecialValueText("Auto")
        self.area_reject = QDoubleSpinBox()
        self.area_reject.setMaximum(1e15)
        self.area_reject.setDecimals(0)
        self.height_reject = QDoubleSpinBox()
        self.height_reject.setMaximum(1e15)
        self.height_reject.setDecimals(0)
        self.min_sn = QDoubleSpinBox()
        self.min_sn.setMaximum(1e6)
        self.valley_depth = AutoSpin(1, 1e12)
        self.baseline_mode = QComboBox()
        self.baseline_mode.addItems(["drop", "valley"])
        self.baseline_mode.setToolTip("drop: common baseline with drop lines; valley: baseline at every valley")
        self.skim = QComboBox()
        self.skim.addItems(["none", "tangent", "exponential", "auto"])
        self.tail_ratio = QDoubleSpinBox()
        self.tail_ratio.setRange(0, 1000)
        self.front_ratio = QDoubleSpinBox()
        self.front_ratio.setRange(0, 1000)
        self.valley_ratio = QDoubleSpinBox()
        self.valley_ratio.setRange(0, 1000)
        self.shoulders = QComboBox()
        self.shoulders.addItems(["off", "drop", "tangent"])
        self.negative = QCheckBox("Detect negative peaks")
        self.tracking = QCheckBox("Baseline tracking (end peaks only at the baseline)")
        self.area_factor = QDoubleSpinBox()
        self.area_factor.setRange(1e-9, 1e9)
        self.area_factor.setDecimals(4)
        self.area_factor.setToolTip("Reported area = integral [signal x s] x factor (10 = ChemStation FID units)")

        form = QFormLayout()
        form.addRow("Peak width (FWHM)", self.pw)
        form.addRow("Slope sensitivity", self.slope)
        form.addRow("Threshold (height)", self.threshold)
        form.addRow("Smoothing window", self.smooth)
        form.addRow("Min. valley depth", self.valley_depth)
        form.addRow("Area reject", self.area_reject)
        form.addRow("Height reject", self.height_reject)
        form.addRow("Min. S/N", self.min_sn)
        form.addRow("Baseline", self.baseline_mode)
        form.addRow("Skim mode", self.skim)
        form.addRow("Tail skim height ratio", self.tail_ratio)
        form.addRow("Front skim height ratio", self.front_ratio)
        form.addRow("Skim valley ratio", self.valley_ratio)
        form.addRow("Shoulders", self.shoulders)
        form.addRow("", self.negative)
        form.addRow("", self.tracking)
        form.addRow("Area factor", self.area_factor)
        params = QWidget()
        params.setLayout(form)

        # timed events
        self.timed = QTableWidget(0, 3)
        self.timed.setHorizontalHeaderLabels(["Time [min]", "Event", "Value"])
        self.timed.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.timed.verticalHeader().setVisible(False)
        add = QPushButton("Add event")
        add.clicked.connect(self._add_timed)
        rem = QPushButton("Remove")
        rem.clicked.connect(lambda: [self.timed.removeRow(r) for r in
                                     sorted({i.row() for i in self.timed.selectedIndexes()}, reverse=True)])
        tl = QVBoxLayout()
        tl.addWidget(self.timed, 1)
        h = QHBoxLayout()
        h.addWidget(add)
        h.addWidget(rem)
        h.addStretch(1)
        tl.addLayout(h)
        timed = QWidget()
        timed.setLayout(tl)

        # manual events
        self.manual = QTableWidget(0, 5)
        self.manual.setHorizontalHeaderLabels(["On", "Event", "User", "When", "Comment"])
        self.manual.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.manual.verticalHeader().setVisible(False)
        self.manual.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.manual.itemChanged.connect(self._manual_item_changed)
        mdel = QPushButton("Delete selected")
        mdel.clicked.connect(self._delete_manual)
        mclr = QPushButton("Remove all manual changes")
        mclr.clicked.connect(self._clear_manual)
        ml = QVBoxLayout()
        ml.addWidget(self.manual, 1)
        h = QHBoxLayout()
        h.addWidget(mdel)
        h.addWidget(mclr)
        h.addStretch(1)
        ml.addLayout(h)
        manual = QWidget()
        manual.setLayout(ml)

        self.tabs = QTabWidget()
        self.tabs.addTab(params, "Parameters")
        self.tabs.addTab(timed, "Timed events")
        self.tabs.addTab(manual, "Manual events")

        apply_run = QPushButton(icon("integrate"), "Apply to active")
        apply_run.setToolTip("Integrate the active chromatogram with these settings")
        apply_run.clicked.connect(lambda: self._apply(False))
        apply_all = QPushButton(icon("integrate", "#8e44ad"), "Apply to all")
        apply_all.setToolTip("Use these settings for all loaded chromatograms")
        apply_all.clicked.connect(lambda: self._apply(True))
        auto = QPushButton(icon("autoparam"), "Auto parameters")
        auto.setToolTip("Reset width, slope, threshold and smoothing to automatic")
        auto.clicked.connect(self._auto)
        bottom = QHBoxLayout()
        bottom.addWidget(auto)
        bottom.addStretch(1)
        bottom.addWidget(apply_run)
        bottom.addWidget(apply_all)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addLayout(top)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(bottom)

        for sig in (ws.activeRunChanged, ws.signalKeyChanged, ws.methodChanged):
            sig.connect(lambda *_: self.load())
        ws.resultChanged.connect(self._on_result)
        self.load()

    # -- load / collect --------------------------------------------------------

    def _kind(self):
        return FID if parse_key(self.ws.signal_key)[0] == FID else "TIC"

    def load(self):
        st = self.ws.active
        self._loading = True
        self.method_box.clear()
        self.method_box.addItems(self.ws.methods.names())
        if st is None:
            self._loading = False
            return
        m = self.ws.method_for(st, self.ws.signal_key)
        self.method = m.copy()
        self.method_box.setCurrentText(m.name)
        res = st.results.get(self.ws.signal_key)
        r = res.resolved if res else None
        self.pw.set(m.peak_width, r.peak_width if r else None)
        self.slope.set(m.slope_sensitivity, r.slope_mult if r else None)
        self.threshold.set(m.threshold, r.threshold if r else None)
        self.smooth.setValue(m.smoothing_window or 0)
        self.valley_depth.set(m.min_valley_depth, (r.threshold if r else None))
        self.area_reject.setValue(m.area_reject)
        self.height_reject.setValue(m.height_reject)
        self.min_sn.setValue(m.min_sn)
        self.baseline_mode.setCurrentText(m.baseline_mode)
        self.skim.setCurrentText(m.skim_mode)
        self.tail_ratio.setValue(m.tail_skim_ratio)
        self.front_ratio.setValue(m.front_skim_ratio)
        self.valley_ratio.setValue(m.skim_valley_ratio)
        self.shoulders.setCurrentText(m.shoulders)
        self.negative.setChecked(m.negative_peaks)
        self.tracking.setChecked(m.baseline_tracking)
        self.area_factor.setValue(m.area_unit_factor)
        self.timed.setRowCount(0)
        for e in m.timed_events:
            self._timed_row(e)
        self._load_manual(st, res)
        self._loading = False

    def _on_result(self, rid, key):
        st = self.ws.active
        if st is None or rid != st.id or key != self.ws.signal_key:
            return
        self._loading = True
        self._load_manual(st, st.results.get(key))
        self._loading = False

    def _load_manual(self, st, res):
        self.manual.blockSignals(True)
        self.manual.setRowCount(0)
        unresolved = dict(res.unresolved) if res else {}
        for e in st.events(self.ws.signal_key):
            r = self.manual.rowCount()
            self.manual.insertRow(r)
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            on.setCheckState(Qt.Checked if e.enabled else Qt.Unchecked)
            on.setData(Qt.UserRole, e.uid)
            self.manual.setItem(r, 0, on)
            desc = e.describe()
            if e.uid in unresolved:
                desc += f"   ⚠ {unresolved[e.uid]}"
            item = QTableWidgetItem(desc)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if e.uid in unresolved:
                item.setForeground(Qt.darkRed)
            self.manual.setItem(r, 1, item)
            for c, v in ((2, e.user), (3, e.timestamp.replace("T", " "))):
                it = QTableWidgetItem(v)
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.manual.setItem(r, c, it)
            self.manual.setItem(r, 4, QTableWidgetItem(e.comment))
        self.manual.resizeColumnsToContents()
        self.manual.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.manual.blockSignals(False)

    def _timed_row(self, e: TimedEvent):
        r = self.timed.rowCount()
        self.timed.insertRow(r)
        t = QDoubleSpinBox()
        t.setDecimals(3)
        t.setMaximum(1e4)
        t.setValue(e.time)
        self.timed.setCellWidget(r, 0, t)
        kind = QComboBox()
        kind.addItems([k.value for k in EventKind])
        kind.setCurrentText(e.kind.value)
        self.timed.setCellWidget(r, 1, kind)
        self.timed.setItem(r, 2, QTableWidgetItem("" if e.value is None else str(e.value)))

    def _add_timed(self):
        st = self.ws.active
        t = 0.0
        sel = self.ws.selected_peak()
        if sel is not None:
            t = round(sel.apex_rt, 3)
        self._timed_row(TimedEvent(t, EventKind.INTEGRATOR_OFF))

    def collect(self) -> IntegrationMethod:
        m = (self.method or IntegrationMethod()).copy()
        m.peak_width = self.pw.get()
        m.slope_sensitivity = self.slope.get()
        m.threshold = self.threshold.get()
        m.smoothing_window = self.smooth.value() or None
        m.min_valley_depth = self.valley_depth.get()
        m.area_reject = self.area_reject.value()
        m.height_reject = self.height_reject.value()
        m.min_sn = self.min_sn.value()
        m.baseline_mode = self.baseline_mode.currentText()
        m.skim_mode = self.skim.currentText()
        m.tail_skim_ratio = self.tail_ratio.value()
        m.front_skim_ratio = self.front_ratio.value()
        m.skim_valley_ratio = self.valley_ratio.value()
        m.shoulders = self.shoulders.currentText()
        m.negative_peaks = self.negative.isChecked()
        m.baseline_tracking = self.tracking.isChecked()
        m.area_unit_factor = self.area_factor.value()
        events = []
        for r in range(self.timed.rowCount()):
            t = self.timed.cellWidget(r, 0).value()
            kind = EventKind(self.timed.cellWidget(r, 1).currentText())
            raw = (self.timed.item(r, 2).text() if self.timed.item(r, 2) else "").strip()
            value = None
            if kind in VALUE_EVENTS and raw:
                try:
                    value = float(raw)
                except ValueError:
                    value = None
            elif kind in CHOICE_EVENTS:
                value = raw if raw in CHOICE_EVENTS[kind] else CHOICE_EVENTS[kind][0]
            events.append(TimedEvent(t, kind, value))
        m.timed_events = sorted(events, key=lambda e: e.time)
        return m

    # -- actions -----------------------------------------------------------------

    def _apply(self, all_runs: bool):
        st = self.ws.active
        if st is None:
            return
        m = self.collect()
        kind = self._kind()
        ids = [s.id for s in self.ws.states()] if all_runs else [st.id]
        text = f"method '{m.name}' ({kind}) for {'all runs' if all_runs else st.name}"
        # recorded on the active run's undo stack even when it changes all runs,
        # so Ctrl+Z right after "Apply to all" reverts it
        st.undo.push(SetMethodCommand(self.ws, ids, kind, m, text))

    def _auto(self):
        for w in (self.pw, self.slope, self.threshold, self.valley_depth):
            w.auto.setChecked(True)
        self.smooth.setValue(0)

    def _pick_method(self, *_):
        if self._loading:
            return
        name = self.method_box.currentText()
        m = self.ws.methods.get(name)
        self.method = m
        st = self.ws.active
        if st is not None:
            st.undo.push(SetMethodCommand(self.ws, [st.id], self._kind(), m, f"method '{name}' for {st.name}"))

    def _save_as(self):
        m = self.collect()
        name, ok = QInputDialog.getText(self, "Save method", "Method name:", text=m.name)
        if not ok or not name.strip():
            return
        m.name = name.strip()
        path = self.ws.methods.save(m)
        self.method = m
        self.load()
        self.method_box.setCurrentText(m.name)
        self.ws.message.emit(f"Method saved: {path}")

    def _manual_item_changed(self, item):
        if self._loading or item.column() not in (0, 4):
            return
        st = self.ws.active
        if st is None:
            return
        key = self.ws.signal_key
        uid = self.manual.item(item.row(), 0).data(Qt.UserRole)
        events = list(st.events(key))
        for i, e in enumerate(events):
            if e.uid == uid:
                if item.column() == 0:
                    events[i] = e.with_(enabled=item.checkState() == Qt.Checked)
                    text = f"{'enable' if events[i].enabled else 'disable'}: {e.describe()}"
                else:
                    events[i] = e.with_(comment=item.text())
                    text = f"comment on {e.describe()}"
                st.undo.push(ManualEventsCommand(self.ws, st.id, key, events, text))
                return

    def _delete_manual(self):
        st = self.ws.active
        if st is None:
            return
        rows = sorted({i.row() for i in self.manual.selectedIndexes()})
        uids = {self.manual.item(r, 0).data(Qt.UserRole) for r in rows}
        if not uids:
            return
        key = self.ws.signal_key
        events = [e for e in st.events(key) if e.uid not in uids]
        st.undo.push(ManualEventsCommand(self.ws, st.id, key, events, f"delete {len(uids)} manual event(s)"))

    def _clear_manual(self):
        st = self.ws.active
        if st is None or not st.events(self.ws.signal_key):
            return
        if QMessageBox.question(self, "Manual events", "Remove all manual integration changes of this "
                                "chromatogram? (Undo is possible.)") != QMessageBox.Yes:
            return
        st.undo.push(ManualEventsCommand(self.ws, st.id, self.ws.signal_key, [], "remove all manual events"))
