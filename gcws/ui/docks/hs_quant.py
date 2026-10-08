"""Separate HS settings; never writes the NIAS settings or standard definitions."""
import copy

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QComboBox, QDoubleSpinBox,
                               QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
                               QPushButton, QHBoxLayout, QCheckBox, QMessageBox, QListWidget, QListWidgetItem)

from gcws.quant.hs import UNITS, default_defs, external, manual, report_units

CALIBRATIONS = (("internal", "Internal standards in each sample"),
                ("external", "External: calibration runs"),
                ("manual", "External: entered areas"))
NOTES = {"internal": "TIC area ÷ mean activated ISTD area × mean ISTD amount = µg/HS.\n",
         "external": "TIC area ÷ mean ISTD area of the calibration runs × mean ISTD amount = µg/HS.\n"
                     "Tick the calibration vials (ISTD mix) below and bind the standards there; the samples "
                     "contain no ISTD.\n",
         "manual": "TIC area ÷ mean entered ISTD area × mean ISTD amount = µg/HS.\n"
                   "Enter the TIC area of each standard (from a calibration measured before); the samples "
                   "contain no ISTD.\n"}
#: the order of the calibration-run list: calibration runs first, then likely ISTD-mix vials
ROLE_ORDER = {"standard": 0, "blank_istd": 1, "sample": 2, "blank": 3, "ladder": 4}


def _fmt(value) -> str:
    if value is None:
        return ""
    return f"{value:.6g}" if isinstance(value, float) else str(value)


class HSQuantPanel(QWidget):
    #: (run id, role): a run ticked as calibration run (role Standard) or unticked
    roleRequested = QtSignal(str, str)

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws, self.loading = ws, False
        layout = QVBoxLayout(self)
        self.note = note = QLabel()
        note.setWordWrap(True)
        layout.addWidget(note)
        self.form = form = QFormLayout()
        self.calibration = QComboBox()
        for key, label in CALIBRATIONS:
            self.calibration.addItem(label, key)
        self.calibration.activated.connect(self._calibration_picked)
        form.addRow("Calibration", self.calibration)
        # the calibration runs: ticking one makes it a Standard run
        self.cal_runs = QListWidget()
        self.cal_runs.setToolTip("Tick the runs that hold the calibration standards (they get role Standard; "
                                 "several are averaged). Double-click: show the run to bind its standards.")
        self.cal_runs.setMaximumHeight(5 * self.cal_runs.fontMetrics().height() + 16)
        self.cal_runs.itemChanged.connect(self._cal_run_ticked)
        self.cal_runs.itemDoubleClicked.connect(lambda it: self.go_to_run(it.data(Qt.UserRole)))
        self.b_go = QPushButton("Go to run")
        self.b_go.setToolTip("Show the selected calibration run, to bind its standards")
        self.b_go.clicked.connect(lambda: self.go_to_run())
        cal = QVBoxLayout()
        cal.setContentsMargins(0, 0, 0, 0)
        cal.addWidget(self.cal_runs)
        go = QHBoxLayout()
        go.addStretch(1)
        go.addWidget(self.b_go)
        cal.addLayout(go)
        self.cal_box = QWidget()
        self.cal_box.setLayout(cal)
        form.addRow("Calibration runs", self.cal_box)
        self.unit = QComboBox()
        self.unit.addItems(UNITS)
        self.unit.activated.connect(self.save_inputs)
        form.addRow("Result unit", self.unit)
        self.report_units = [QComboBox(), QComboBox()]
        for i, combo in enumerate(self.report_units, 1):
            combo.addItems(UNITS)
            combo.setToolTip(f"Conc. {i} of the HS report (single and double determination)")
            combo.activated.connect(self.save_inputs)
            form.addRow(f"Report Conc. {i}", combo)
        self.mean = QCheckBox("Use mean of activated ISTD areas")
        self.mean.toggled.connect(self.save_inputs)
        form.addRow(self.mean)
        self.blank = QCheckBox("Subtract matching Blank / Blank+ISTD (larger area)")
        self.blank.toggled.connect(self.save_inputs)
        form.addRow(self.blank)
        self.sample_name = QLabel()
        form.addRow("Active sample", self.sample_name)
        self.area, self.mass = QDoubleSpinBox(), QDoubleSpinBox()
        for box in (self.area, self.mass):
            box.setDecimals(6)
            box.setRange(0, 1e9)
            box.setSpecialValueText("Not entered")
            box.editingFinished.connect(self.save_inputs)
        form.addRow("Sample area (dm²)", self.area)
        form.addRow("Sample mass (g)", self.mass)
        layout.addLayout(form)
        self.defs = QTableWidget(7, 6)
        self.defs.setHorizontalHeaderLabels(["Code", "Name", "µg/HS", "Target RT", "Active", "TIC area"])
        self.defs.horizontalHeaderItem(5).setToolTip("External: entered areas - the TIC area of the standard "
                                                     "in the calibration")
        self.defs.setMinimumHeight(self.defs.horizontalHeader().height() + 7 * self.defs.verticalHeader().defaultSectionSize() + 8)
        self.defs.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.defs.itemChanged.connect(self.save_defs)
        layout.addWidget(self.defs)
        self.bound = QTableWidget(0, 5)
        self.bound.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.bound)
        self.bind_row = QWidget()
        row = QHBoxLayout(self.bind_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.codes = QComboBox()
        row.addWidget(self.codes)
        self.bind_buttons = [self.codes]
        for label, fn in (("Bind selected TIC peak", lambda: self.bind()), ("Unbind", lambda: self.binding(None)),
                          ("Automatic", lambda: self.binding(...)), ("Detect...", self._detect),
                          ("Learn spectrum", self._learn)):
            button = QPushButton(label)
            button.clicked.connect(fn)
            row.addWidget(button)
            self.bind_buttons.append(button)
        layout.addWidget(self.bind_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        ws.runAdded.connect(lambda *_: self.refresh() if self.isVisible() else None)
        ws.runRemoved.connect(lambda *_: self.refresh() if self.isVisible() else None)

    def config(self):
        return self.ws.quant.get("hs", {})

    def mode(self) -> str:
        cfg = self.config()
        return "manual" if manual(cfg) else "external" if external(cfg) else "internal"

    def _detect(self):
        from gcws.ui.dialogs.istd_detect import DetectIstdDialog
        if self.ws.active is not None:
            DetectIstdDialog(self.ws, self).exec()

    def _learn(self):
        dock = self.parent()
        while dock is not None and not hasattr(dock, "learn_spectrum"):
            dock = dock.parent()
        if dock is not None:
            dock.learn_spectrum(self.codes.currentData())

    def push(self, cfg, label):
        q = copy.deepcopy(self.ws.quant)
        q["hs"] = cfg
        if q != self.ws.quant:
            self.ws.push_quant(label, q)

    # -- calibration runs ---------------------------------------------------------------

    def calibration_runs(self) -> list:
        return [st for st in self.ws.states() if st.role == "standard"]

    def _fill_cal_runs(self):
        from gcws.io.sequence import ROLE_LABELS
        cur = self.cal_runs.currentItem()
        keep = cur.data(Qt.UserRole) if cur is not None else None
        self.cal_runs.blockSignals(True)
        self.cal_runs.clear()
        states = sorted(self.ws.states(), key=lambda st: (ROLE_ORDER.get(st.role, 5), st.name))
        for st in states:
            it = QListWidgetItem(f"{st.name}  ·  {ROLE_LABELS.get(st.role, st.role)}")
            it.setData(Qt.UserRole, st.id)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if st.role == "standard" else Qt.Unchecked)
            self.cal_runs.addItem(it)
            if st.id == keep:
                self.cal_runs.setCurrentItem(it)
        if not states:
            it = QListWidgetItem("Load the calibration vials (ISTD mix) and the samples")
            it.setFlags(Qt.NoItemFlags)
            self.cal_runs.addItem(it)
        self.cal_runs.blockSignals(False)

    def _cal_run_ticked(self, item):
        rid = item.data(Qt.UserRole)
        st = self.ws.runs.get(rid) if rid else None
        if self.loading or st is None:
            return
        if item.checkState() == Qt.Checked:
            role = "standard"
        else:                                   # back to what its name says (never Standard)
            from gcws.io.sequence import classify_role
            role = classify_role(st.run.path.name)
        if role != st.role:
            self.roleRequested.emit(rid, role)

    def go_to_run(self, rid=None):
        """Make a calibration run the active run (the selected one, else the first)."""
        if rid is None:
            cur = self.cal_runs.currentItem()
            rid = cur.data(Qt.UserRole) if cur is not None else None
            if rid is None or (rid in self.ws.runs and self.ws.runs[rid].role != "standard"):
                runs = self.calibration_runs()
                rid = runs[0].id if runs else rid
        if rid and rid in self.ws.runs:
            self.ws.set_active(rid)

    # -- refresh ------------------------------------------------------------------------

    def refresh(self):
        editing = QAbstractItemView.EditingState
        if self.defs.state() == editing:
            return                              # a cell is being typed in: rebuilt after the edit
        self.loading = True
        try:
            self._refresh()
        finally:
            self.loading = False

    def _refresh(self):
        cfg = self.config()
        mode = self.mode()
        self.calibration.setCurrentIndex(self.calibration.findData(mode))
        self.note.setText(NOTES[mode] +
                          "Divide by sample area (dm²) or mass (g) for normalized results.\n"
                          "Define seven standards; their default amount is 1 µg per vial.")
        self.unit.setCurrentText(cfg.get("unit", UNITS[0]))
        for combo, u in zip(self.report_units, report_units(cfg)):
            combo.setCurrentText(u)
        self.mean.setChecked(cfg.get("use_mean_area", True))
        self.blank.setChecked(cfg.get("blank_correction", True))
        st = self.ws.active
        self.sample_name.setText(st.name if st else "Load a sample")
        self.form.setRowVisible(self.cal_box, mode == "external")
        if mode == "external":
            self._fill_cal_runs()
            self.b_go.setEnabled(bool(self.calibration_runs()))
        # the standards are bound in each sample (internal) or in the calibration runs (external)
        on_cal = st is not None and st.role == "standard"
        self.bind_row.setVisible(mode != "manual")
        for w in self.bind_buttons:
            w.setEnabled(mode == "internal" or (mode == "external" and on_cal))
        inputs = cfg.get("samples", {}).get(st.id, {}) if st else {}
        for key, box in (("area_dm2", self.area), ("mass_g", self.mass)):
            box.setEnabled(st is not None)
            box.setValue(float(inputs.get(key) or 0))
        selected = self.codes.currentData()
        self.codes.clear()
        self.defs.setColumnHidden(5, mode != "manual")
        self.defs.setColumnHidden(3, mode == "manual")         # entered areas: no peak is looked for
        for r, d in enumerate(cfg.get("istd_defs", default_defs())):
            self.codes.addItem(d["code"], d["code"])
            for c, key in enumerate(("code", "name", "concentration", "target_rt")):
                item = QTableWidgetItem(str(d.get(key)) if d.get(key) is not None else "")
                if c == 0:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.defs.setItem(r, c, item)
            item = QTableWidgetItem()
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if d.get("quantify", True) else Qt.Unchecked)
            self.defs.setItem(r, 4, item)
            self.defs.setItem(r, 5, QTableWidgetItem(_fmt(d.get("area"))))
        self.codes.setCurrentIndex(max(0, self.codes.findData(selected)))
        result = self.ws.quant_result
        sample = result.samples.get(st.id) if st and result else None
        hs_sample = sample is not None and getattr(sample, "mode", "") == "hs_screening"
        if mode == "external" and not on_cal:
            self._fill_matrix(cfg)
        elif mode == "manual":
            self.bound.setVisible(False)
        else:
            self._fill_bound(sample.standards if hs_sample else [])
        if hs_sample and result.errors.get(st.id):
            text = result.errors[st.id]
        elif hs_sample and mode == "external" and on_cal:
            text = (f"Calibration run: bind its seven standards here. External factor: {sample.mean_factor:.6g} "
                    "µg/area" if sample.mean_factor else "Calibration run: bind its seven standards here.")
        elif hs_sample and mode == "external":
            n = len(self.calibration_runs())
            text = f"External factor: {sample.mean_factor:.6g} µg/area (mean of {n} calibration run" \
                   f"{'s' if n != 1 else ''})"
        elif hs_sample and mode == "manual":
            text = f"External factor: {sample.mean_factor:.6g} µg/area (entered areas)"
        elif hs_sample:
            text = f"ISTD factor: {sample.mean_factor:.6g} µg/area"
        else:
            text = "Load and integrate a sample TIC to quantify HS screening."
        if mode == "external" and st is not None and not on_cal and self.calibration_runs():
            text += "\nThe standards are bound in a calibration run: Go to run."
        self.status.setText(text)

    def _fill_bound(self, standards):
        """Internal mode, or a calibration run: the standards found in the active run."""
        self.bound.setVisible(True)
        self.bound.setColumnCount(5)
        self.bound.setHorizontalHeaderLabels(["Code", "RT", "TIC area", "Active", "Status"])
        self.bound.setRowCount(0)
        for std in standards:
            r = self.bound.rowCount()
            self.bound.insertRow(r)
            values = [std["code"], std["rt"], std["area"], "Yes" if std.get("quantify", True) else "No", std["status"]]
            for c, val in enumerate(values):
                item = QTableWidgetItem("" if val is None else str(val))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.bound.setItem(r, c, item)

    def _fill_matrix(self, cfg):
        """External calibration from runs: each standard's TIC area in every calibration run and the mean."""
        from gcws.quant.hs import calibration
        try:
            standards = calibration(self.ws, cfg)[0]
        except (ValueError, TypeError, KeyError):
            standards = []
        runs = [name for name, _a in (standards[0].get("areas") or [])] if standards else []
        headers = ["Code", "µg/HS"] + runs + ["Mean area", "Active", "Status"]
        self.bound.setVisible(True)
        self.bound.setColumnCount(len(headers))
        self.bound.setHorizontalHeaderLabels(headers)
        self.bound.setRowCount(0)
        for std in standards:
            r = self.bound.rowCount()
            self.bound.insertRow(r)
            values = [std["code"], std.get("concentration")] + [a for _n, a in std.get("areas") or []] + \
                [std["area"], "Yes" if std.get("quantify", True) else "No", std["status"]]
            for c, val in enumerate(values):
                item = QTableWidgetItem(_fmt(val))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.bound.setItem(r, c, item)

    # -- changes ------------------------------------------------------------------------

    def _calibration_picked(self, *_):
        """Switching to entered areas with none entered yet takes the means of the calibration runs."""
        if self.loading:
            return
        cfg = copy.deepcopy(self.config())
        if self.calibration.currentData() == "manual" and not any(d.get("area") for d in cfg.get("istd_defs", [])):
            from gcws.quant.hs import calibration
            try:
                standards = calibration(self.ws, dict(cfg, calibration="external"))[0]
            except (ValueError, TypeError, KeyError):
                standards = []
            if standards and any(s.get("area") for s in standards):
                defs = copy.deepcopy(cfg.get("istd_defs", default_defs()))
                for d, s in zip(defs, standards):
                    if s.get("area"):
                        d["area"] = round(float(s["area"]), 6)
                cfg["istd_defs"] = defs
        self.save_inputs(cfg=cfg)

    def save_inputs(self, *_, cfg=None):
        if self.loading:
            return
        cfg = copy.deepcopy(self.config()) if cfg is None else cfg
        chosen = [c.currentText() for c in self.report_units]
        if chosen[0] == chosen[1]:
            chosen = chosen[:1]                     # report_units() completes it with another unit
        cfg.update(unit=self.unit.currentText(), use_mean_area=self.mean.isChecked(), report_units=report_units(
                   {"report_units": chosen}),
                   calibration=self.calibration.currentData(),
                   blank_correction=self.blank.isChecked())
        if self.ws.active:
            cfg.setdefault("samples", {})[self.ws.active.id] = dict(area_dm2=self.area.value(), mass_g=self.mass.value())
        self.push(cfg, "HS sample and calculation settings")

    def save_defs(self, *_):
        if self.loading:
            return
        import math
        defs = []
        try:
            for r in range(7):
                get = lambda c: (self.defs.item(r, c).text().strip() if self.defs.item(r, c) else "")
                amount = float(get(2).replace(",", "."))
                rt = float(get(3).replace(",", ".")) if get(3) else None
                area = float(get(5).replace(",", ".")) if get(5) else None
                if not math.isfinite(amount) or amount <= 0 or (rt is not None and (not math.isfinite(rt) or rt < 0)):
                    raise ValueError("Amounts must be positive; RT must be nonnegative")
                if area is not None and (not math.isfinite(area) or area <= 0):
                    raise ValueError("A TIC area must be positive (or empty)")
                d = dict(code=get(0), name=get(1), concentration=amount, target_rt=rt,
                         quantify=self.defs.item(r, 4).checkState() == Qt.Checked)
                if area is not None:
                    d["area"] = area
                defs.append(d)
        except ValueError as exc:
            QMessageBox.warning(self, "HS standards", str(exc))
            self.refresh()
            return
        cfg = copy.deepcopy(self.config())
        cfg["istd_defs"] = defs
        self.push(cfg, "HS internal standards")

    def istd_codes(self) -> list[tuple[str, str]]:
        """(code, name) of the HS standards, in table order."""
        return [(d["code"], d.get("name") or "") for d in self.config().get("istd_defs", default_defs())]

    def can_bind(self) -> str:
        """Why the standards cannot be bound in the active run ("" when they can)."""
        mode = self.mode()
        if mode == "manual":
            return "External calibration with entered areas: no standard is bound in a run."
        st = self.ws.active
        if mode == "external" and (st is None or st.role != "standard"):
            return "External calibration: the standards are bound in a calibration run (HS panel: Go to run)."
        return ""

    def bind(self, code=None) -> bool:
        """Bind the selected TIC peak to ``code`` (default: the code chosen in the panel)."""
        from gcws.core.keys import base_key
        why = self.can_bind()
        if why:
            self.ws.message.emit(why)
            return False
        p = self.ws.selected_peak()
        if p is None or base_key(self.ws.signal_key) != "TIC":
            QMessageBox.information(self, "HS standard", "Select an integrated TIC peak first.")
            return False
        if self.ws.signal_key != "TIC":
            i = self.ws.base_peak_index(self.ws.active_id, self.ws.signal_key, self.ws.selected)
            if i < 0:
                QMessageBox.information(self, "HS standard", "Select this standard in the raw TIC to bind it.")
                return False
            p = self.ws.result(self.ws.active_id, "TIC").peaks[i]
        self.binding(p.apex_rt, code)
        return True

    def binding(self, rt, code=None):
        code = code or self.codes.currentData()
        if self.ws.active is None or not code:
            return
        cfg = copy.deepcopy(self.config())
        bindings = cfg.setdefault("istd_bindings", {}).setdefault(self.ws.active.id, {})
        if rt is ...:
            bindings.pop(code, None)
        else:
            bindings[code] = rt
        self.push(cfg, "HS internal standard binding")
