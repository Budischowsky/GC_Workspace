"""Retention index from an n-alkane ladder (Kovats, temperature programmed)."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from gcws.core.model import FID
from gcws.ui.undo import ValueCommand


class RetentionIndexDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ws = win.ws
        self.setWindowTitle("Retention index - alkane ladder")
        ri = copy.deepcopy(self.ws.quant.get("ri") or {})
        self.run = QComboBox()
        for st in self.ws.states():
            self.run.addItem(st.name + ("  [ladder]" if st.role == "ladder" else ""), st.id)
        ladder_runs = [i for i in range(self.run.count()) if self.ws.runs[self.run.itemData(i)].role == "ladder"]
        if ladder_runs:
            self.run.setCurrentIndex(ladder_runs[0])
        from_run = QPushButton("Read n-alkanes from identifications")
        from_run.clicked.connect(self._from_run)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["C", "RT [min]"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for n, rt in sorted((int(k), v) for k, v in (ri.get("ladder") or {}).items()):
            self._row(n, rt)
        add = QPushButton("Add row")
        add.clicked.connect(lambda: self._row(0, 0.0))
        rem = QPushButton("Remove")
        rem.clicked.connect(lambda: [self.table.removeRow(r) for r in
                                     sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)])
        self.report_ri = QCheckBox("Report RI column")
        self.report_ri.setChecked(bool(ri.get("report_ri")))
        self.replace_rt = QCheckBox("Replace RT by RI in the reports")
        self.replace_rt.setChecked(bool(ri.get("replace_rt")))
        self.problems = QLabel()
        self.problems.setWordWrap(True)
        self.problems.setObjectName("warning")
        f = QFormLayout()
        h = QHBoxLayout()
        h.addWidget(self.run, 1)
        h.addWidget(from_run)
        f.addRow("Ladder chromatogram", h)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addWidget(self.table, 1)
        h2 = QHBoxLayout()
        h2.addWidget(add)
        h2.addWidget(rem)
        h2.addStretch(1)
        lay.addLayout(h2)
        lay.addWidget(self.report_ri)
        lay.addWidget(self.replace_rt)
        lay.addWidget(self.problems)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(460, 560)
        self._check()
        self.table.itemChanged.connect(lambda *_: self._check())

    def _row(self, n, rt):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(str(n)))
        self.table.setItem(r, 1, QTableWidgetItem(f"{rt:.4f}"))

    def ladder(self) -> dict:
        out = {}
        for r in range(self.table.rowCount()):
            try:
                n = int(self.table.item(r, 0).text())
                rt = float(self.table.item(r, 1).text().replace(",", "."))
            except (ValueError, AttributeError):
                continue
            if n > 0 and rt > 0:
                out[n] = rt
        return dict(sorted(out.items()))

    def _from_run(self):
        import gc_qc
        rid = self.run.currentData()
        st = self.ws.runs.get(rid)
        res = self.ws.result(rid, FID) or self.ws.result(rid)
        if st is None or res is None:
            return
        key = FID if FID in st.results else self.ws.signal_key
        idents, _ = st.ident_set(key).bind(res.peaks)
        rows = [{"name": idents[i].name, "rt": p.apex_rt, "area": p.area} for i, p in enumerate(res.peaks)
                if i in idents and idents[i].name]
        ladder = gc_qc.alkane_ladder(rows)
        self.table.setRowCount(0)
        for n, rt in ladder.items():
            self._row(n, rt)
        self._check()

    def _check(self):
        import gc_qc
        probs = gc_qc.ladder_problems(self.ladder())
        self.problems.setText("\n".join(probs))

    def _ok(self):
        ws = self.ws
        q = copy.deepcopy(ws.quant)
        q["ri"] = {"ladder": {str(k): v for k, v in self.ladder().items()},
                   "report_ri": self.report_ri.isChecked(),
                   "replace_rt": self.replace_rt.isChecked() and self.report_ri.isChecked()}

        def setter(v):
            ws.quant = copy.deepcopy(v)
            ws.recompute_quant()

        (ws.undo_group.activeStack() or ws.project_undo).push(
            ValueCommand("alkane ladder / retention index", lambda: ws.quant, setter, q,
                         lambda t, o, n: ws.log(t, "", f"{len(q['ri']['ladder'])} ladder points")))
        self.accept()
