"""Quantify > Detect internal standards: the automatic ISTD detection with its evidence.

Shows per defined standard the peak found, its RT against the target, the evidence (library
names, spectrum match with the learned reference, retention time) and a confidence. The
checked standards are bound in one undoable step; optionally their target RTs are moved to
where they were found (a shortened column).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from gcws.quant import istd_detect as ID
from gcws.ui import theme

LEVEL = {"high": "ok", "medium": "warn", "low": "bad"}


class DetectIstdDialog(QDialog):
    COLS = ["", "Code", "Name", "Found RT", "Target RT", "Shift", "Area", "Confidence", "Evidence"]

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.setWindowTitle("Detect internal standards")
        self.resize(980, 460)
        lay = QVBoxLayout(self)
        lay.addWidget(theme.hint(
            "Each standard of the ISTD table is looked for by its library names, by its spectrum (learned with "
            "'Learn spectrum' in the Quantification panel) and by its retention time, allowing a common shift "
            "of the run. Check the standards to bind; high confidence is checked already."))
        top = QHBoxLayout()
        top.addWidget(QLabel("Chromatograms"))
        self.scope = QComboBox()
        self.scope.addItem("Active chromatogram", "active")
        self.scope.addItem("All loaded samples", "all")
        self.scope.currentIndexChanged.connect(self.run)
        top.addWidget(self.scope)
        top.addStretch(1)
        self.summary = QLabel()
        top.addWidget(self.summary)
        lay.addLayout(top)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(len(self.COLS) - 1, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table, 1)
        self.update_targets = QCheckBox("Also move the target RTs to where the standards were found")
        lay.addWidget(self.update_targets)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Bind checked")
        bb.accepted.connect(self.apply)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.results: dict = {}
        self.run()

    def run_ids(self) -> list[str]:
        if self.scope.currentData() == "all":
            from gcws.quant.hs import standards_in
            hs = self.ws.quant.get("mode") == "hs_screening"
            return [s.id for s in self.ws.states() if s.role in ("sample", "standard")
                    and (not hs or standards_in(self.ws.quant.get("hs", {}), s.role))]
        return [self.ws.active_id] if self.ws.active_id else []

    def run(self, *_):
        self.results = {}
        self.table.setRowCount(0)
        shifts = []
        for rid in self.run_ids():
            try:
                res = ID.detect(self.ws, rid)
            except Exception as exc:  # noqa: BLE001 - shown in the table
                self.summary.setText(f"{self.ws.runs[rid].name}: {exc}")
                continue
            self.results[rid] = res
            shifts.append(res.shift)
            for code, c in res.best.items():
                self._row(rid, code, res.names.get(code, ""), c)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(len(self.COLS) - 1, QHeaderView.Stretch)
        if shifts:
            self.summary.setText("Run shift: " + ", ".join(f"{s:+.3f}" for s in shifts) + " min")

    def _row(self, rid, code, name, c):
        r = self.table.rowCount()
        self.table.insertRow(r)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled if c is not None else Qt.NoItemFlags)
        chk.setCheckState(Qt.Checked if c is not None and c.confidence == "high" else Qt.Unchecked)
        chk.setData(Qt.UserRole, (rid, code))
        self.table.setItem(r, 0, chk)
        label = code if len(self.results) <= 1 else f"{code}  ({self.ws.runs[rid].name})"
        vals = [label, name]
        if c is None:
            vals += ["not found", "", "", "", "none", ""]
        else:
            vals += [f"{c.rt:.3f}", "" if c.target_rt is None else f"{c.target_rt:.3f}",
                     "" if c.target_rt is None else f"{c.rt - c.target_rt:+.3f}", f"{c.area:,.0f}", c.confidence,
                     "; ".join(c.evidence)]
        for col, v in enumerate(vals, 1):
            it = QTableWidgetItem(v)
            if col == 7:
                it.setBackground(theme.status_brush(LEVEL.get(v, "neutral")))
            if col == 8 and c is not None:
                it.setToolTip("\n".join(c.evidence) + f"\nscore {c.score:.2f}")
            self.table.setItem(r, col, it)

    def chosen(self) -> dict:
        """``{run id: {code: RT on the binding axis}}`` of the checked rows."""
        out: dict = {}
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it.checkState() != Qt.Checked:
                continue
            rid, code = it.data(Qt.UserRole)
            c = self.results[rid].best.get(code)
            if c is not None:
                out.setdefault(rid, {})[code] = c.rt
        return out

    def apply(self):
        found = self.chosen()
        if found:
            n = sum(len(v) for v in found.values())
            self.ws.push_quant(f"ISTD detection: {n} standard(s) bound", ID.with_bindings(
                self.ws, found, self.update_targets.isChecked()), "ISTD bindings (automatic detection)")
        self.accept()
