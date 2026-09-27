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
        self.mode_form = f
        self.mode = QComboBox()
        for k, v in MODES.items():
            self.mode.addItem(v, k)
        self.mode.activated.connect(self._mode_changed)
        # the unit and ISTD concentration only exist for the "Internal standard concentration" mode;
        # the other modes have a fixed unit (shown read-only)
        self.unit = QComboBox()
        self.unit.addItems(UNITS)
        self.unit.setEditable(True)
        self.unit.setToolTip("Unit of the ISTD concentration below; the results come out in the same unit")
        self.istd_conc = QDoubleSpinBox()
        self.istd_conc.setDecimals(6)
        self.istd_conc.setMaximum(1e9)
        self.istd_conc.setToolTip("Concentration of the internal standard in the analysed solution")
        self.result_unit = QLabel()
        self.mode_note = QLabel()
        self.mode_note.setWordWrap(True)
        self.mode_note.setObjectName("hint")
        self.mode_note.setTextFormat(Qt.RichText)
        f.addRow("Mode", self.mode)
        f.addRow("ISTD concentration", self.istd_conc)
        f.addRow("Unit", self.unit)
        f.addRow("Result unit", self.result_unit)
        f.addRow(self.mode_note)
        lay.addWidget(mode_box)
        # typed values are committed after a short pause (and on Enter / leaving the field)
        from PySide6.QtCore import QTimer
        self._commit = QTimer(self)
        self._commit.setSingleShot(True)
        self._commit.setInterval(600)
        self._commit.timeout.connect(self._mode_changed)
        self.unit.currentTextChanged.connect(lambda *_: None if self._loading else self._commit.start())
        self.unit.lineEdit().editingFinished.connect(self._mode_changed)
        self.istd_conc.valueChanged.connect(lambda *_: None if self._loading else self._commit.start())
        self.istd_conc.editingFinished.connect(self._mode_changed)

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
        self.defs.itemChanged.connect(self._def_edited)
        self.mean_area = QCheckBox("Factor from the mean of the ISTD areas")
        self.mean_area.toggled.connect(lambda *_: None if self._loading else self._apply_defs())
        h = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(self._add_def)
        rem = QPushButton("Remove")
        rem.clicked.connect(self._remove_def)
        for b in (add, rem):
            h.addWidget(b)
        h.addStretch(1)
        self.defs_note = QLabel("Changes apply at once (Undo reverts them). Conc. = ISTD concentration in "
                                "mg/mL for the NIAS factor; once this table is set, the FC17/BBP/DNNP "
                                "concentrations of the NIAS parameters no longer apply.")
        self.defs_note.setWordWrap(True)
        self.defs_note.setObjectName("hint")
        il.addWidget(self.defs)
        il.addWidget(self.mean_area)
        self.rrt_box = QWidget()
        rf = QFormLayout(self.rrt_box)
        rf.setContentsMargins(0, 0, 0, 0)
        self.rrt_reference = QComboBox()
        self.rrt_reference.setToolTip("One reference for all samples; uses each sample's measured ISTD RT")
        self.rrt_reference.activated.connect(self._rrt_changed)
        rf.addRow("RRT reference ISTD", self.rrt_reference)
        self.rrt_status = QLabel()
        self.rrt_status.setWordWrap(True)
        rf.addRow(self.rrt_status)
        il.addWidget(self.rrt_box)
        il.addLayout(h)
        il.addWidget(self.defs_note)
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
        self.legacy_groups = (par, istd, run)
        from gcws.ui.docks.hs_quant import HSQuantPanel
        self.hs_panel = HSQuantPanel(ws, self)
        lay.addWidget(self.hs_panel)
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
        self.rrt_box.setVisible(mode == "nias_mgkg")
        hs = mode == "hs_screening"
        for group in self.legacy_groups:
            group.setVisible(not hs)
        self.hs_panel.setVisible(hs)
        own = mode == "istd_conc"
        self.mode_form.setRowVisible(self.unit, own)
        self.mode_form.setRowVisible(self.istd_conc, own)
        self.mode_form.setRowVisible(self.result_unit, not own and not hs)
        unit = q.get("unit", UNITS[0]) or UNITS[0]
        pending = self._commit.isActive()           # never overwrite a value that is still being typed
        if not (pending or self.unit.hasFocus() or self.unit.lineEdit().hasFocus()):
            self.unit.setCurrentText(unit)
        if not (pending or self.istd_conc.hasFocus()):
            self.istd_conc.setValue(float(q.get("istd_conc_value") or 0))
        self.istd_conc.setSuffix(f" {unit}")
        from gcws.quant.service import mode_unit
        self.result_unit.setText(f"<b>{mode_unit(q)}</b> (fixed by the mode)")
        self.mode_note.setText({
            "hs_screening": "<b>HS-Screening:</b> MS-only quantification from TIC peak areas. "
                            "Configure the seven HS standards and sample amount below.",
            "nias_mgkg": "<b>What it computes:</b> mg/kg food simulant.<br>"
                         "mg/dm² = blank-corrected FID area × mean ISTD factor; mg/kg = mg/dm² × O/V.<br>"
                         "The factor comes from the Internal standards table (concentration, area) and the "
                         "migration conditions (cell area, coverage, O/V). Blank correction: the larger of "
                         "the Blank / Blank+ISTD areas.",
            "istd_conc": "<b>What it computes:</b> the concentration of every peak relative to the internal "
                         "standard,<br>c(substance) = corrected area ÷ ISTD area × c(ISTD).<br>"
                         "Enter the ISTD concentration in the analysed solution and its unit; the results come "
                         "out in that unit (e.g. 10 µg/mL ISTD → results in µg/mL). The ISTD area is the mean "
                         "of the quantifying ISTDs found in the run (or the reference ISTD). No response "
                         "factors: every substance is assumed to respond like the ISTD.",
            "total_ugl": "<b>What it computes:</b> µg/L in the extract (total extraction).<br>"
                         "c = corrected area ÷ mean ISTD area × c(ISTD in the extract), where c(ISTD) follows "
                         "from the ISTD amount and the extract volume of the NIAS parameters.",
            "area_pct": "<b>What it computes:</b> the area % of every peak among all integrated peaks "
                        "(solvent excluded). No ISTD needed.",
        }[mode])
        if hs:
            self.hs_panel.refresh()
            self._loading = False
            return
        s = self._settings()
        from PySide6.QtWidgets import QAbstractItemView
        editing = QAbstractItemView.EditingState
        if self.params.state() == editing or self.defs.state() == editing:
            self._loading = False          # a cell is being typed in: rebuild after the edit
            return
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
        self.rrt_reference.clear()
        self.rrt_reference.addItem("Not selected", "")
        for d in defs:
            self.rrt_reference.addItem(f"{d['code']}  {d['name']}", d["code"])
        reference = q.get("rrt_reference") or ""
        index = self.rrt_reference.findData(reference)
        if index < 0:
            self.rrt_reference.addItem(f"{reference} (removed)", reference)
            index = self.rrt_reference.count() - 1
        self.rrt_reference.setCurrentIndex(index)
        from gcws.quant.service import rrt_reference
        st = self.ws.active
        sample = self.ws.quant_result.samples.get(st.id) if st and self.ws.quant_result else None
        self.rrt_status.setText(rrt_reference(sample, q, st)[1])
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

    def _rrt_changed(self, *_):
        if not self._loading:
            q = copy.deepcopy(self.ws.quant)
            q["rrt_reference"] = self.rrt_reference.currentData() or ""
            if q != self.ws.quant:
                self._push_quant("RRT reference ISTD", q)

    def _mode_changed(self, *_):
        if self._loading:
            return
        self._commit.stop()
        q = copy.deepcopy(self.ws.quant)
        q["mode"] = self.mode.currentData()
        if q["mode"] != "hs_screening":
            q["unit"] = self.unit.currentText().strip() or UNITS[0]
            q["istd_conc_value"] = self.istd_conc.value()
        if q == self.ws.quant:
            return
        old = self.ws.quant
        if q["mode"] != old.get("mode"):
            text = f"quantification mode {MODES[q['mode']]}"
        else:
            text = f"ISTD concentration {q['istd_conc_value']:g} {q['unit']}"
        self._push_quant(text, q)

    def _def_edited(self, item):
        if not self._loading:
            self._apply_defs()

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
        self._loading = True
        self._def_row({"code": code, "name": "", "quantify": True})
        self._loading = False
        self._apply_defs()

    def _remove_def(self):
        rows = sorted({i.row() for i in self.defs.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self._loading = True
        for r in rows:
            self.defs.removeRow(r)
        self._loading = False
        self._apply_defs()

    def _apply_defs(self):
        import gc_fid
        q = copy.deepcopy(self.ws.quant)
        q["istd_defs"] = gc_fid.normalise_istd_defs(self._collect_defs())
        q["istd_options"] = {"use_mean_area": self.mean_area.isChecked(),
                             "reference": (q.get("istd_options") or {}).get("reference", "")}
        if q != self.ws.quant:
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
        if self.ws.quant.get("mode") == "hs_screening":
            self.hs_panel.bind()
            return
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
    """Migration conditions. The cell and the occupancy texts follow from the cell area and the
    coverage factor (``gcws.quant.migration``); the numbers start from the parameter table."""
    FIELDS = [("analyst", "Analyst"), ("simulant", "Simulant"), ("temperature", "Temperature"),
              ("duration", "Duration"), ("cell_area_dm2", "Cell area (dm²)"), ("occupancy_factor", "Coverage factor"),
              ("volume_ml", "Volume (mL)"), ("ov_ratio", "Surface/volume (dm²/kg)"),
              ("syneris_summary_report_no", "Syneris summary report no. (optional)")]
    #: remembered from the last conditions entered (texts only - numbers come from the parameters)
    REMEMBERED = ("analyst", "simulant", "temperature", "duration", "volume_ml", "syneris_summary_report_no")

    def __init__(self, initial: dict, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Migration conditions")
        from gcws.quant import migration as MG
        from gcws.report.legacy_api import main_script
        from gcws.ui.dialogs.preferences import load_settings
        self.main = main_script()
        last = load_settings().get("last_migration_metadata") or {}
        values = {k: v for k, v in last.items() if k in self.REMEMBERED}
        values.update(initial or {})
        values.update(self.main.migration_metadata_from_settings(settings))   # today's parameter table
        self.values = values
        self.edits = {}
        f = QFormLayout()
        for key, label in self.FIELDS:
            v = values.get(key)
            text = "" if v is None else str(v)
            if key == "simulant":
                e = QComboBox()
                e.setEditable(True)
                e.addItems(MG.SIMULANTS + [MG.OTHER])
                e.setCurrentIndex(-1)
                e.setEditText(text)
                e.lineEdit().setPlaceholderText("choose, or type another simulant")
                e.activated.connect(lambda i, e=e: self._simulant_picked(e))
            else:
                e = QLineEdit(text)
            self.edits[key] = e
            f.addRow(label, e)
        self.derived = QLabel()
        self.derived.setObjectName("hint")
        for key in ("cell_area_dm2", "occupancy_factor"):
            self.edits[key].textChanged.connect(self._update_derived)
        f.addRow("", self.derived)
        note = QLabel(MG.CELL_NOTE + " Cell area, coverage and surface/volume are calculation inputs; they "
                      "change the NIAS parameters too.")
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
        self._update_derived()

    def _simulant_picked(self, combo):
        from gcws.quant import migration as MG
        if combo.currentText() == MG.OTHER:
            combo.setEditText("")
            combo.lineEdit().setFocus()

    def _update_derived(self):
        from gcws.quant import migration as MG
        cell = MG.cell_text(self.edits["cell_area_dm2"].text())
        occ = MG.occupancy_text(self.edits["occupancy_factor"].text())
        self.derived.setText(f"Reported as: {cell or '–'}, {occ or '–'}")

    def field_text(self, key: str) -> str:
        e = self.edits[key]
        return (e.currentText() if isinstance(e, QComboBox) else e.text()).strip()

    def _ok(self):
        from gcws.quant import migration as MG
        data = {**self.values, **{k: self.field_text(k) for k in self.edits}}
        if data.get("simulant") == MG.OTHER:
            data["simulant"] = ""
        try:
            self.metadata = self.main.validate_migration_metadata(MG.complete(data))
        except ValueError as exc:
            QMessageBox.warning(self, "Migration conditions", str(exc))
            return
        from gcws.ui.dialogs.preferences import load_settings, save_settings
        s = load_settings()
        s["last_migration_metadata"] = {k: v for k, v in self.metadata.items() if k in self.REMEMBERED}
        save_settings(s)
        self.accept()
