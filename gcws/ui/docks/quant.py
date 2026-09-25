"""Quantification panel: mode and unit, NIAS parameters, ISTDs, migration."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from gcws.quant.service import MODES, UNITS
from gcws.ui.undo import ValueCommand


def _item(text, editable=False):
    it = QTableWidgetItem("" if text is None else str(text))
    if not editable:
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


class QuantDock(QScrollArea):
    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        lay = QVBoxLayout(body)
        self._loading = False

        mode_box = QGroupBox("Quantification")
        f = QFormLayout(mode_box)
        self.mode = QComboBox()
        for k, v in MODES.items():
            self.mode.addItem(v, k)
        self.mode.activated.connect(self._mode_changed)
        self.unit = QComboBox()
        self.unit.addItems(UNITS)
        self.unit.setEditable(True)
        self.unit.activated.connect(self._mode_changed)
        self.istd_conc = QDoubleSpinBox()
        self.istd_conc.setDecimals(6)
        self.istd_conc.setMaximum(1e9)
        self.istd_conc.editingFinished.connect(self._mode_changed)
        self.mode_note = QLabel()
        self.mode_note.setWordWrap(True)
        self.mode_note.setObjectName("hint")
        f.addRow("Mode", self.mode)
        f.addRow("Unit", self.unit)
        f.addRow("ISTD concentration", self.istd_conc)
        f.addRow("", self.mode_note)
        lay.addWidget(mode_box)

        par = QGroupBox("NIAS parameters")
        pl = QVBoxLayout(par)
        self.params = QTableWidget(0, 3)
        self.params.setHorizontalHeaderLabels(["Parameter", "Value", "Unit"])
        self.params.verticalHeader().setVisible(False)
        self.params.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.params.itemChanged.connect(self._param_edited)
        mig = QPushButton("Migration conditions...")
        mig.clicked.connect(self.edit_migration)
        pl.addWidget(self.params)
        pl.addWidget(mig)
        lay.addWidget(par)

        istd = QGroupBox("Internal standards")
        il = QVBoxLayout(istd)
        self.defs = QTableWidget(0, 5)
        self.defs.setHorizontalHeaderLabels(["Code", "Name", "Conc.", "Target RT", "Quantify"])
        self.defs.verticalHeader().setVisible(False)
        self.defs.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.mean_area = QCheckBox("Factor from the mean of the ISTD areas")
        h = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(self._add_def)
        rem = QPushButton("Remove")
        rem.clicked.connect(self._remove_def)
        apply_defs = QPushButton("Apply")
        apply_defs.clicked.connect(self._apply_defs)
        for b in (add, rem):
            h.addWidget(b)
        h.addStretch(1)
        h.addWidget(apply_defs)
        il.addWidget(self.defs)
        il.addWidget(self.mean_area)
        il.addLayout(h)
        lay.addWidget(istd)

        run = QGroupBox("ISTDs in the active chromatogram")
        rl = QVBoxLayout(run)
        self.bound = QTableWidget(0, 6)
        self.bound.setHorizontalHeaderLabels(["Code", "Name", "RT", "Area", "Deviation %", "Status"])
        self.bound.verticalHeader().setVisible(False)
        self.bound.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.bind_box = QComboBox()
        bind = QPushButton("Bind selected peak")
        bind.clicked.connect(self._bind_selected)
        unbind = QPushButton("Unbind")
        unbind.clicked.connect(self._unbind)
        auto = QPushButton("Automatic")
        auto.setToolTip("Find the ISTDs by name and target RT again")
        auto.clicked.connect(self._auto_bind)
        h = QHBoxLayout()
        h.addWidget(self.bind_box)
        h.addWidget(bind)
        h.addWidget(unbind)
        h.addWidget(auto)
        h.addStretch(1)
        self.factor = QLabel()
        rl.addWidget(self.bound)
        rl.addLayout(h)
        rl.addWidget(self.factor)
        lay.addWidget(run)
        lay.addStretch(1)

        ws.quantChanged.connect(self.refresh)
        ws.activeRunChanged.connect(lambda *_: self.refresh())
        self.refresh()

    # -- helpers -----------------------------------------------------------------

    def _settings(self):
        from gcws.quant.nias_bridge import make_settings
        return make_settings(self.ws.quant.get("settings"))

    def _defs(self):
        import gc_fid
        q = self.ws.quant
        return gc_fid.normalise_istd_defs(q["istd_defs"]) if q.get("istd_defs") else \
            gc_fid.default_istd_defs(self._settings())

    def _push_quant(self, text, new_quant):
        self.ws.push_quant(text, new_quant)

    # -- refresh -------------------------------------------------------------------

    def refresh(self):
        import gc_fid
        self._loading = True
        q = self.ws.quant
        self.mode.setCurrentIndex(max(0, self.mode.findData(q.get("mode", "nias_mgkg"))))
        mode = self.mode.currentData()
        self.unit.setCurrentText(q.get("unit", UNITS[0]))
        self.unit.setEnabled(mode == "istd_conc")
        self.istd_conc.setEnabled(mode == "istd_conc")
        self.istd_conc.setValue(float(q.get("istd_conc_value") or 0))
        self.mode_note.setText({
            "nias_mgkg": "mg/dm² = corrected FID area × mean ISTD factor; mg/kg = mg/dm² × O/V. "
                         "Blank correction from the assigned blanks (larger of Blank / Blank+ISTD).",
            "istd_conc": "c = corrected area / mean ISTD area × ISTD concentration (unit as entered).",
            "total_ugl": "c [µg/L] = corrected area / mean ISTD area × c(ISTD in the extract) "
                         "from ISTD amount and extract volume.",
            "area_pct": "Area % of all integrated peaks (solvent excluded).",
        }[mode])
        s = self._settings()
        self.params.setRowCount(0)
        for key, label, unit in gc_fid.PARAMETER_LAYOUT:
            if key is None:
                continue
            r = self.params.rowCount()
            self.params.insertRow(r)
            self.params.setItem(r, 0, _item(label))
            it = _item(getattr(s, key, ""), True)
            it.setData(Qt.UserRole, key)
            self.params.setItem(r, 1, it)
            self.params.setItem(r, 2, _item(unit))
        self.params.resizeRowsToContents()
        defs = self._defs()
        self.defs.setRowCount(0)
        for d in defs:
            self._def_row(d)
        opts = gc_fid.normalise_istd_options(q.get("istd_options") or {})
        self.mean_area.setChecked(opts["use_mean_area"])
        self.bind_box.clear()
        for d in defs:
            self.bind_box.addItem(f"{d['code']}  {d['name']}", d["code"])
        self._refresh_bound()
        self._loading = False

    def _def_row(self, d):
        r = self.defs.rowCount()
        self.defs.insertRow(r)
        for c, v in enumerate((d.get("code"), d.get("name"), d.get("concentration"), d.get("target_rt"))):
            self.defs.setItem(r, c, _item("" if v is None else v, True))
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        chk.setCheckState(Qt.Checked if d.get("quantify", True) else Qt.Unchecked)
        self.defs.setItem(r, 4, chk)

    def _refresh_bound(self):
        self.bound.setRowCount(0)
        st = self.ws.active
        sample = self.ws.quant_result.samples.get(st.id) if (st and self.ws.quant_result) else None
        if sample is None:
            self.factor.setText("Quantification applies to chromatograms with role Sample (FID).")
            return
        for std in sample.standards:
            r = self.bound.rowCount()
            self.bound.insertRow(r)
            dev = std.get("deviation")
            vals = (std.get("code"), std.get("name"), f"{std['fid_rt']:.3f}" if std.get("fid_rt") else "",
                    f"{std['fid_area']:,.0f}" if std.get("fid_area") else "",
                    f"{dev:+.1f}" if dev is not None else "", std.get("status", ""))
            for c, v in enumerate(vals):
                self.bound.setItem(r, c, _item(v))
        mf = sample.mean_factor
        self.factor.setText(f"Mean factor: {mf:.6g}" if mf else "No usable ISTD - no concentrations.")
        err = self.ws.quant_result.errors.get(st.id)
        if err:
            self.factor.setText(self.factor.text() + f"\nError: {err}")

    # -- edits -------------------------------------------------------------------------

    def _mode_changed(self, *_):
        if self._loading:
            return
        q = copy.deepcopy(self.ws.quant)
        q["mode"] = self.mode.currentData()
        q["unit"] = self.unit.currentText()
        q["istd_conc_value"] = self.istd_conc.value()
        if q != self.ws.quant:
            self._push_quant(f"quantification mode {MODES[q['mode']]}", q)

    def _param_edited(self, item):
        if self._loading or item.column() != 1:
            return
        import gc_fid
        key = item.data(Qt.UserRole)
        s = self._settings()
        try:
            gc_fid.apply_setting(s, key, item.text().replace(",", "."))
        except ValueError as exc:
            QMessageBox.warning(self, "Parameter", str(exc))
            self.refresh()
            return
        q = copy.deepcopy(self.ws.quant)
        q.setdefault("settings", {})[key] = getattr(s, key)
        self._push_quant(f"parameter {key} = {getattr(s, key)}", q)

    def _collect_defs(self):
        out = []
        for r in range(self.defs.rowCount()):
            get = lambda c: (self.defs.item(r, c).text().strip() if self.defs.item(r, c) else "")
            out.append({"code": get(0), "name": get(1), "concentration": get(2).replace(",", ".") or None,
                        "target_rt": get(3).replace(",", ".") or None,
                        "quantify": self.defs.item(r, 4).checkState() == Qt.Checked})
        return out

    def _add_def(self):
        import gc_fid
        code = gc_fid.next_istd_code(self._collect_defs())
        self._def_row({"code": code, "name": "", "quantify": True})

    def _remove_def(self):
        for r in sorted({i.row() for i in self.defs.selectedIndexes()}, reverse=True):
            self.defs.removeRow(r)

    def _apply_defs(self):
        import gc_fid
        q = copy.deepcopy(self.ws.quant)
        q["istd_defs"] = gc_fid.normalise_istd_defs(self._collect_defs())
        q["istd_options"] = {"use_mean_area": self.mean_area.isChecked(),
                             "reference": (q.get("istd_options") or {}).get("reference", "")}
        self._push_quant("ISTD definitions", q)

    def _set_binding(self, code, rt):
        st = self.ws.active
        if st is None or not code:
            return
        q = copy.deepcopy(self.ws.quant)
        b = q.setdefault("istd_bindings", {}).setdefault(st.id, {})
        if rt is ...:
            b.pop(code, None)
        else:
            b[code] = rt
        self._push_quant(f"ISTD {code} in {st.name}: " + ("automatic" if rt is ... else
                                                         ("unbound" if rt is None else f"{rt:.3f}")), q)

    def _bind_selected(self):
        p = self.ws.selected_peak()
        if p is None:
            QMessageBox.information(self, "ISTD", "Select the ISTD peak in the chromatogram or table first.")
            return
        self._set_binding(self.bind_box.currentData(), round(p.apex_rt, 4))

    def _unbind(self):
        self._set_binding(self.bind_box.currentData(), None)

    def _auto_bind(self):
        self._set_binding(self.bind_box.currentData(), ...)

    def edit_migration(self):
        dlg = MigrationDialog(self.ws.quant.get("migration") or {}, self._settings(), self)
        if dlg.exec() != QDialog.Accepted:
            return
        from gcws.report.legacy_api import main_script
        main = main_script()
        meta = dlg.metadata
        s = self._settings()
        try:
            main.apply_migration_metadata_to_settings(s, meta)
        except ValueError as exc:
            QMessageBox.warning(self, "Migration conditions", str(exc))
            return
        from gcws.quant.nias_bridge import settings_dict
        q = copy.deepcopy(self.ws.quant)
        q["migration"] = meta
        q["settings"] = settings_dict(s)
        self._push_quant("migration conditions", q)


class MigrationDialog(QDialog):
    FIELDS = [("analyst", "Analyst"), ("migration_cell", "Migration cell"), ("occupancy", "Occupancy"),
              ("simulant", "Simulant"), ("temperature", "Temperature"), ("duration", "Duration"),
              ("cell_area_dm2", "Cell area (dm²)"), ("occupancy_factor", "Coverage factor"),
              ("volume_ml", "Volume (mL)"), ("ov_ratio", "Surface/volume (dm²/kg)"),
              ("syneris_summary_report_no", "Syneris summary report no. (optional)")]

    def __init__(self, initial: dict, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Migration conditions")
        from gcws.report.legacy_api import main_script
        from gcws.ui.dialogs.preferences import load_settings
        self.main = main_script()
        values = self.main.migration_metadata_from_settings(settings)
        values.update((load_settings().get("last_migration_metadata") or {}))
        values.update(initial or {})
        self.values = values
        self.edits = {}
        f = QFormLayout()
        for key, label in self.FIELDS:
            e = QLineEdit(str(values.get(key, "") if values.get(key) is not None else ""))
            self.edits[key] = e
            f.addRow(label, e)
        note = QLabel("These values are calculation inputs (cell area, coverage, O/V) and appear in the report "
                      "next to 'Migrate'. Texts are reported as entered.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addWidget(note)
        lay.addWidget(bb)
        self.metadata = None

    def _ok(self):
        data = {**self.values, **{k: e.text() for k, e in self.edits.items()}}
        try:
            self.metadata = self.main.validate_migration_metadata(data)
        except ValueError as exc:
            QMessageBox.warning(self, "Migration conditions", str(exc))
            return
        from gcws.ui.dialogs.preferences import load_settings, save_settings
        s = load_settings()
        s["last_migration_metadata"] = {k: v for k, v in self.metadata.items() if k != "schema_version"}
        save_settings(s)
        self.accept()
