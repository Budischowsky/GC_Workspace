"""Separate HS settings; never writes the NIAS settings or standard definitions."""
import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QComboBox, QDoubleSpinBox,
                               QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
                               QPushButton, QHBoxLayout, QCheckBox, QMessageBox)

from gcws.quant.hs import UNITS, default_defs


class HSQuantPanel(QWidget):
    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws, self.loading = ws, False
        layout = QVBoxLayout(self)
        note = QLabel("TIC area ÷ mean activated ISTD area × mean ISTD amount = µg/HS.\n"
                      "Divide by sample area (dm²) or mass (g) for normalized results.\n"
                      "Define seven standards; their default amount is 1 µg per vial.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.unit = QComboBox()
        self.unit.addItems(UNITS)
        self.unit.activated.connect(self.save_inputs)
        form.addRow("Result unit", self.unit)
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
        self.defs = QTableWidget(7, 5)
        self.defs.setHorizontalHeaderLabels(["Code", "Name", "µg/HS", "Target RT", "Active"])
        self.defs.setMinimumHeight(self.defs.horizontalHeader().height() + 7 * self.defs.verticalHeader().defaultSectionSize() + 8)
        self.defs.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.defs.itemChanged.connect(self.save_defs)
        layout.addWidget(self.defs)
        self.bound = QTableWidget(0, 5)
        self.bound.setHorizontalHeaderLabels(["Code", "RT", "TIC area", "Active", "Status"])
        self.bound.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.bound)
        row = QHBoxLayout()
        self.codes = QComboBox()
        row.addWidget(self.codes)
        for label, fn in (("Bind selected TIC peak", lambda: self.bind()), ("Unbind", lambda: self.binding(None)),
                          ("Automatic", lambda: self.binding(...))):
            button = QPushButton(label)
            button.clicked.connect(fn)
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def config(self):
        return self.ws.quant.get("hs", {})

    def push(self, cfg, label):
        q = copy.deepcopy(self.ws.quant)
        q["hs"] = cfg
        if q != self.ws.quant:
            self.ws.push_quant(label, q)

    def refresh(self):
        self.loading = True
        cfg = self.config()
        self.unit.setCurrentText(cfg.get("unit", UNITS[0]))
        self.mean.setChecked(cfg.get("use_mean_area", True))
        self.blank.setChecked(cfg.get("blank_correction", True))
        st = self.ws.active
        self.sample_name.setText(st.name if st else "Load a sample")
        inputs = cfg.get("samples", {}).get(st.id, {}) if st else {}
        for key, box in (("area_dm2", self.area), ("mass_g", self.mass)):
            box.setEnabled(st is not None)
            box.setValue(float(inputs.get(key) or 0))
        selected = self.codes.currentData()
        self.codes.clear()
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
        self.codes.setCurrentIndex(max(0, self.codes.findData(selected)))
        result = self.ws.quant_result
        sample = result.samples.get(st.id) if st and result else None
        self.bound.setRowCount(0)
        if sample is not None and getattr(sample, "mode", "") == "hs_screening":
            for std in sample.standards:
                r = self.bound.rowCount()
                self.bound.insertRow(r)
                values = [std["code"], std["rt"], std["area"], "Yes" if std.get("quantify", True) else "No", std["status"]]
                for c, val in enumerate(values):
                    item = QTableWidgetItem("" if val is None else str(val))
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.bound.setItem(r, c, item)
            self.status.setText(result.errors.get(st.id) or f"ISTD factor: {sample.mean_factor:.6g} µg/area")
        else:
            self.status.setText("Load and integrate a sample TIC to quantify HS screening.")
        self.loading = False

    def save_inputs(self, *_):
        if self.loading:
            return
        cfg = copy.deepcopy(self.config())
        cfg.update(unit=self.unit.currentText(), use_mean_area=self.mean.isChecked(),
                   blank_correction=self.blank.isChecked())
        if self.ws.active:
            cfg.setdefault("samples", {})[self.ws.active.id] = dict(area_dm2=self.area.value(), mass_g=self.mass.value())
        self.push(cfg, "HS sample and calculation settings")

    def save_defs(self, *_):
        if self.loading:
            return
        defs = []
        try:
            for r in range(7):
                get = lambda c: self.defs.item(r, c).text().strip()
                amount = float(get(2).replace(",", "."))
                rt = float(get(3).replace(",", ".")) if get(3) else None
                import math
                if not math.isfinite(amount) or amount <= 0 or (rt is not None and (not math.isfinite(rt) or rt < 0)):
                    raise ValueError("Amounts must be positive; RT must be nonnegative")
                defs.append(dict(code=get(0), name=get(1), concentration=amount, target_rt=rt,
                                 quantify=self.defs.item(r, 4).checkState() == Qt.Checked))
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

    def bind(self, code=None) -> bool:
        """Bind the selected TIC peak to ``code`` (default: the code chosen in the panel)."""
        from gcws.core.keys import base_key
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
