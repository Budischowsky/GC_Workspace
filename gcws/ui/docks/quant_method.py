"""Quant method editor of the Quantification panel (mode "Extraction (quant method)").

A solid (g) or a foil (dm²) is extracted into a volume (mL) spiked with the internal standards of the
panel's ISTD table (stock concentration in mg/mL). The results come out in two units: Conc. 1 (the
peak table's Conc. and the double determination) and Conc. 2, both in the Quantification report.
Methods are kept by name (``gcws.quant.qmethod``); every change is one undoable step."""
from __future__ import annotations

import copy

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog,
                               QLabel, QMessageBox, QPushButton, QVBoxLayout)

from gcws.quant import extraction as EX
from gcws.quant import qmethod as QM
from gcws.quant import units as U

UNSAVED = "(not saved)"


def _spin(decimals, maximum, suffix="", special=""):
    box = QDoubleSpinBox()
    box.setDecimals(decimals)
    box.setMaximum(maximum)
    if suffix:
        box.setSuffix(" " + suffix)
    if special:
        box.setSpecialValueText(special)
    return box


class QuantMethodPanel(QGroupBox):
    def __init__(self, ws, parent=None):
        super().__init__("Quant method", parent)
        self.ws = ws
        self._loading = False
        lay = QVBoxLayout(self)
        h = QHBoxLayout()
        self.methods = QComboBox()
        self.methods.setToolTip("Saved quant methods: choosing one applies it (Undo reverts it)")
        self.methods.activated.connect(self._chosen)
        self.save_btn = QPushButton("Save")
        self.save_btn.setToolTip("Save the method under its name")
        self.save_btn.clicked.connect(lambda: self.save())
        self.save_as_btn = QPushButton("Save as...")
        self.save_as_btn.clicked.connect(lambda: self.save_as())
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(lambda: self.delete())
        h.addWidget(self.methods, 1)
        for b in (self.save_btn, self.save_as_btn, self.delete_btn):
            h.addWidget(b)
        lay.addLayout(h)
        self.modified = QLabel()
        self.modified.setObjectName("hint")
        lay.addWidget(self.modified)

        f = QFormLayout()
        self.form = f
        self.sample_type = QComboBox()
        for k, v in U.SAMPLE_TYPES.items():
            self.sample_type.addItem(v, k)
        self.sample_type.setToolTip("Solid: results per sample mass (mg/kg, µg/g, ...); foil: per sample area "
                                    "(mg/dm², µg/dm²). Both give the extract units (mg/mL, µg/L).")
        self.amount = _spin(4, 1e6)
        self.amount.setToolTip("Sample amount of the method; a run can have its own below")
        self.volume = _spin(3, 1e6, "mL")
        self.volume.setToolTip("Volume of the extract (solvent) the sample is extracted into")
        self.spike = _spin(3, 1e6, "µL")
        self.spike.setToolTip("Volume of the standard solution added; standard amount [mg] = stock conc. "
                              "[mg/mL] × spiked volume [µL] ÷ 1000")
        self.unit1, self.unit2 = QComboBox(), QComboBox()
        self.unit1.setToolTip("Conc. 1: the Conc. column of the peak table, the double determination and the report")
        self.unit2.setToolTip("Conc. 2: the second concentration column of the Quantification report")
        self.limit = _spin(6, 1e9, special="Off")
        self.limit.setToolTip("Substances below this Conc. 1 are left out of the report (Off: all are reported)")
        self.run_amount = _spin(4, 1e6, special="Method amount")
        self.run_amount.setToolTip("The active run's own sample amount (e.g. its weighed mass); "
                                   "'Method amount' uses the method's")
        f.addRow("Sample", self.sample_type)
        f.addRow("Sample amount", self.amount)
        f.addRow("Extract volume", self.volume)
        f.addRow("Spiked standard", self.spike)
        f.addRow("Conc. 1", self.unit1)
        f.addRow("Conc. 2", self.unit2)
        f.addRow("Reporting limit", self.limit)
        f.addRow("Active run amount", self.run_amount)
        lay.addLayout(f)
        self.calc = QLabel()
        self.calc.setWordWrap(True)
        self.calc.setObjectName("hint")
        lay.addWidget(self.calc)
        nias = QPushButton("NIAS standards")
        nias.setToolTip("Fill the Internal standards table with the NIAS standards and their stock concentrations")
        nias.clicked.connect(self.use_nias_standards)
        h2 = QHBoxLayout()
        h2.addWidget(nias)
        h2.addStretch(1)
        lay.addLayout(h2)

        self._commit = QTimer(self)
        self._commit.setSingleShot(True)
        self._commit.setInterval(600)
        self._commit.timeout.connect(self.apply)
        for box in (self.amount, self.volume, self.spike, self.limit, self.run_amount):
            box.valueChanged.connect(lambda *_: None if self._loading else self._commit.start())
            box.editingFinished.connect(lambda: None if self._loading else self.apply())
        for combo in (self.sample_type, self.unit1, self.unit2):
            combo.activated.connect(lambda *_: None if self._loading else self.apply())

    # -- state ---------------------------------------------------------------------

    def method(self) -> dict:
        return EX.of(self.ws.quant)

    def _saved(self, name):
        try:
            return QM.load(name) if name else None
        except KeyError:
            return None

    def is_modified(self) -> bool:
        """The method differs from its saved file (or has never been saved)."""
        m = self.method()
        saved = self._saved(m["name"])
        if saved is None:
            return True
        mine = QM.collect(self.ws.quant, m["name"])
        return any(mine[k] != (EX.normalise(saved[k]) | {"name": m["name"]} if k == "method" else saved.get(k))
                   for k in ("method", "istd_defs", "istd_options"))

    def refresh(self):
        self._loading = True
        m = self.method()
        names = QM.names()
        self.methods.clear()
        if not m["name"] or m["name"] not in names:
            self.methods.addItem(m["name"] or UNSAVED, None)
        for n in names:
            self.methods.addItem(n, n)
        self.methods.setCurrentIndex(max(0, self.methods.findText(m["name"] or UNSAVED)))
        self.delete_btn.setEnabled(m["name"] in names)
        self.modified.setText("Changed since it was saved" if m["name"] and self.is_modified() else
                              "" if m["name"] else "Not saved yet: Save as... keeps it under a name")
        self.sample_type.setCurrentIndex(max(0, self.sample_type.findData(m["sample_type"])))
        basis = U.AMOUNT_BASIS[m["sample_type"]]
        suffix = " g" if basis == "mass_g" else " dm²"
        pending = self._commit.isActive()
        for box, value in ((self.amount, m["amount"]), (self.volume, m["extract_volume_ml"]),
                           (self.spike, m["spike_ul"]), (self.limit, m["reporting_limit"])):
            if not (pending or box.hasFocus()):
                box.setValue(value)
        self.amount.setSuffix(suffix)
        self.run_amount.setSuffix(suffix)
        self.limit.setSuffix(" " + m["units"][0])
        allowed = U.allowed(m["sample_type"])
        for combo, unit in ((self.unit1, m["units"][0]), (self.unit2, m["units"][1])):
            combo.clear()
            combo.addItems(allowed)
            combo.setCurrentText(unit)
        st = self.ws.active
        own = ((self.ws.quant.get("method_samples") or {}).get(st.id) or {}).get("amount") if st else None
        self.run_amount.setEnabled(st is not None)
        if not (pending or self.run_amount.hasFocus()):
            self.run_amount.setValue(float(own or 0))
        self.calc.setText(self._calc_text(m, st))
        self._loading = False

    def _calc_text(self, m, st) -> str:
        what = "mass (g)" if m["sample_type"] == "solid" else "area (dm²)"
        text = (f"Standard amount = stock conc. × {m['spike_ul']:g} µL ÷ 1000; factor = standard amount ÷ standard "
                f"area (mean of the quantifying standards, or the reference); substance mass = corrected area × "
                f"factor; {m['units'][0]} and {m['units'][1]} = mass ÷ sample {what} or ÷ {m['extract_volume_ml']:g} mL "
                "extract.")
        res = self.ws.quant_result
        sample = res.samples.get(st.id) if (st is not None and res is not None) else None
        info = (getattr(sample, "meta", None) or {}).get("extraction") if sample is not None else None
        if info:
            amount = info["basis"].get(U.AMOUNT_BASIS[m["sample_type"]])
            lines = [f"{st.name}: sample {amount:g}" + (" g" if m["sample_type"] == "solid" else " dm²")
                     if amount else f"{st.name}: no sample amount"]
            lines.append(info["factor_text"] or info["problem"])
            if info["missing"]:
                lines.append(info["missing"])
            text += "\n" + "\n".join(lines)
        return text

    # -- edits ---------------------------------------------------------------------

    def _push(self, text, q):
        if q != self.ws.quant:
            self.ws.push_quant(text, q)

    def apply(self):
        """The fields into ``quant["method"]`` (and the active run's amount) as one undo step."""
        if self._loading:
            return
        self._commit.stop()
        q = copy.deepcopy(self.ws.quant)
        m = EX.of(q)
        old_type = m["sample_type"]
        m["sample_type"] = self.sample_type.currentData()
        m["amount"] = self.amount.value()
        m["extract_volume_ml"] = self.volume.value()
        m["spike_ul"] = self.spike.value()
        m["reporting_limit"] = self.limit.value()
        units = [self.unit1.currentText(), self.unit2.currentText()]
        if m["sample_type"] != old_type:
            # the same kind of unit for the new sample (mg/kg -> mg/dm², ...) where it exists
            swap = {"mg/g": "mg/dm²", "mg/kg": "mg/dm²", "µg/g": "µg/dm²", "µg/kg": "µg/dm²",
                    "mg/dm²": "mg/kg", "µg/dm²": "µg/g"}
            units = [swap.get(u, u) for u in units]
        if units[0] == units[1]:
            units[1] = ""
        m["units"] = units
        q["method"] = EX.normalise(m)
        st = self.ws.active
        if st is not None:
            samples = q.setdefault("method_samples", {})
            if self.run_amount.value() > 0:
                samples[st.id] = {"amount": self.run_amount.value()}
            else:
                samples.pop(st.id, None)
        self._push("quant method", q)
        self.refresh()

    def use_nias_standards(self):
        q = copy.deepcopy(self.ws.quant)
        q["istd_defs"] = EX.nias_standards()
        q["istd_options"] = {"use_mean_area": True, "reference": ""}
        self._push("quant method: NIAS standards", q)

    def _chosen(self, index):
        name = self.methods.itemData(index)
        if self._loading or not name or name == self.method()["name"] and not self.is_modified():
            return
        try:
            data = QM.load(name)
        except KeyError:
            return
        self._push(f"quant method {name}", QM.applied(self.ws.quant, data))

    def save(self, name=None):
        name = name or self.method()["name"]
        if not name:
            return self.save_as()
        data = QM.collect(self.ws.quant, name)
        QM.save(data)
        q = copy.deepcopy(self.ws.quant)
        q["method"] = data["method"]
        if q != self.ws.quant:
            self.ws.push_quant(f"quant method saved as {name}", q)
        self.refresh()
        return name

    def save_as(self, name=None):
        if name is None:
            name, ok = QInputDialog.getText(self, "Save quant method", "Name:", text=self.method()["name"])
            if not ok:
                return None
        name = (name or "").strip()
        if not name:
            return None
        if name in QM.names() and name != self.method()["name"]:
            if QMessageBox.question(self, "Save quant method", f"Overwrite the quant method {name}?") \
                    != QMessageBox.Yes:
                return None
        return self.save(name)

    def delete(self, confirm=True):
        name = self.method()["name"]
        if not name or name not in QM.names():
            return False
        if confirm and QMessageBox.question(self, "Delete quant method", f"Delete the quant method {name}? "
                                            "The current settings stay.") != QMessageBox.Yes:
            return False
        QM.delete(name)
        q = copy.deepcopy(self.ws.quant)
        q.setdefault("method", {})["name"] = ""
        self.ws.push_quant(f"quant method {name} deleted", q)
        self.refresh()
        return True
