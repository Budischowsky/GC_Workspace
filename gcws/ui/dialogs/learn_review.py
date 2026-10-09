"""Report² > View > Learning review...: the disagreements between the program and the analysts' evaluations
(gcws.learn.review) with their evidence. A verdict is saved at once; "Program right" and "Both acceptable" stop the
disagreement from counting in the next baseline and fit. The window is not modal."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from gcws.ui import theme

TYPES = {"missing_peak": "Peak not found", "not_reported": "Not reported by the program",
         "extra_reported": "Reported only by the program", "name_differs": "Different name",
         "kept_but_program_removes": "Program removes as blank", "removed_but_program_keeps": "Analyst removed it"}
VERDICT_TEXT = {"analyst": "Analyst right", "program": "Program right", "both": "Both acceptable"}
HEADERS = ["Verdict", "Type", "Batch", "Run", "RT", "Analyst", "Program"]


def _learn_dir():
    from gcws import paths
    return paths.DATA / "learn"


def _fmt(v) -> str:
    if v is None or v == "":
        return "–"
    return f"{v:g}" if isinstance(v, float) else str(v)


class LearningReview(QDialog):
    def __init__(self, parent=None, out_dir=None):
        super().__init__(parent)
        self.setWindowTitle("Learning review")
        self.setModal(False)
        self.resize(1000, 620)
        self.out_dir = out_dir or _learn_dir()
        lay = QVBoxLayout(self)
        self.hint = theme.hint("No review list yet. Run  python -m gcws.learn review <training folder>  first; "
                               "it writes learn/review/items.json in the data folder.")
        lay.addWidget(self.hint)
        bar = QHBoxLayout()
        self.type_filter = QComboBox()
        self.type_filter.addItem("All types", "")
        for key, text in TYPES.items():
            self.type_filter.addItem(text, key)
        self.type_filter.currentIndexChanged.connect(self.fill)
        bar.addWidget(self.type_filter)
        self.open_only = QCheckBox("Only without a verdict")
        self.open_only.toggled.connect(self.fill)
        bar.addWidget(self.open_only)
        bar.addStretch(1)
        self.count = QLabel()
        bar.addWidget(self.count)
        lay.addLayout(bar)
        split = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self.show_selected)
        split.addWidget(self.table)
        lower = QWidget()
        ll = QVBoxLayout(lower)
        ll.setContentsMargins(0, 0, 0, 0)
        self.evidence = QPlainTextEdit()
        self.evidence.setReadOnly(True)
        ll.addWidget(self.evidence)
        row = QHBoxLayout()
        row.addWidget(QLabel("Note"))
        self.note = QLineEdit()
        row.addWidget(self.note, 1)
        self.b_analyst = QPushButton("Analyst right")
        self.b_analyst.setToolTip("The analyst is right: the program should change (the disagreement keeps counting)")
        self.b_program = QPushButton("Program right")
        self.b_program.setToolTip("The program is right: the disagreement no longer counts")
        self.b_both = QPushButton("Both acceptable")
        self.b_both.setToolTip("Both are acceptable: the disagreement no longer counts")
        for b, verdict in ((self.b_analyst, "analyst"), (self.b_program, "program"), (self.b_both, "both")):
            b.clicked.connect(lambda _=False, v=verdict: self.decide(v))
            row.addWidget(b)
        ll.addLayout(row)
        split.addWidget(lower)
        split.setSizes([400, 220])
        lay.addWidget(split, 1)
        self.items: list = []
        self.shown: list = []
        self.reload()

    def reload(self) -> None:
        from gcws.learn.review import load_items, load_verdicts
        items = load_items(self.out_dir)
        self.hint.setVisible(items is None)
        self.items = items or []
        self.verdicts = load_verdicts(self.out_dir)
        self.fill()

    def fill(self, *_) -> None:
        kind = self.type_filter.currentData()
        self.shown = [i for i in self.items if (not kind or i.type == kind)
                      and not (self.open_only.isChecked() and i.id in self.verdicts)]
        self.table.setRowCount(len(self.shown))
        for r, it in enumerate(self.shown):
            a, p = it.analyst_side, it.program_side
            analyst = " ".join(filter(None, [a.get("label") or a.get("decision", ""), a.get("cas", "")]))
            program = " ".join(filter(None, [p.get("report_line") or p.get("name") or "", p.get("report_cas") or
                                             p.get("cas") or ""]))
            v = self.verdicts.get(it.id, {}).get("verdict", "")
            for c, text in enumerate([VERDICT_TEXT.get(v, ""), TYPES.get(it.type, it.type), it.batch, it.run,
                                      _fmt(it.rt), f"{it.analyst}: {analyst}", program]):
                self.table.setItem(r, c, QTableWidgetItem(text))
        self.count.setText(f"{len(self.shown)} of {len(self.items)}")
        self.evidence.clear()

    def current(self) -> Optional[object]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.shown[rows[0].row()] if rows else None

    def show_selected(self) -> None:
        it = self.current()
        if it is None:
            self.evidence.clear()
            return
        lines = [f"{TYPES.get(it.type, it.type)}  -  {it.batch} / {it.run} ({it.analyst}), RT {_fmt(it.rt)} min", "",
                 "Analyst:"]
        lines += [f"  {k}: {_fmt(v)}" for k, v in it.analyst_side.items()] or ["  (no row)"]
        lines += ["", "Program:"]
        for k, v in it.program_side.items():
            if k == "hits":
                lines += [f"  hit {n}: {h.get('name')} ({h.get('cas')}) {_fmt(h.get('score'))}"
                          for n, h in enumerate(v or [], 1)]
            else:
                lines.append(f"  {k}: {_fmt(v)}")
        v = self.verdicts.get(it.id)
        if v:
            lines += ["", f"Verdict: {VERDICT_TEXT.get(v['verdict'], v['verdict'])} ({v.get('by', '')}, "
                          f"{v.get('at', '')}) {v.get('note', '')}"]
        self.evidence.setPlainText("\n".join(lines))
        self.note.setText((v or {}).get("note", ""))

    def decide(self, verdict: str) -> None:
        from gcws.learn.review import load_verdicts, save_verdict
        it = self.current()
        if it is None:
            return
        save_verdict(self.out_dir, it.id, verdict, self.note.text().strip())
        self.verdicts = load_verdicts(self.out_dir)
        row = self.table.currentRow()
        self.table.item(row, 0).setText(VERDICT_TEXT[verdict])
        self.show_selected()
