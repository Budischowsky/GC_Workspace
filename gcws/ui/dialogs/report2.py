"""Report² dialogs: the rules that decide "Control needed", a comment to an accept / reject, the reasons
offered when a report is rejected, and the history of a report."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QScrollArea,
                               QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from gcws.automation import rules as RU
from gcws.ui import theme

#: parameters shown per rule: key -> (label, kind, extra)
PARAMS = {
    "manual_check": {"only_above_limit": ("Only substances at or above the reporting limit", "bool", None),
                     "artefacts": ("Artefacts (found in one determination only) count", "bool", None)},
    "istd_qc": {"area_diff": ("Compare the ISTD areas of the determinations", "bool", None),
                "max_area_diff_pct": ("Largest difference (%)", "float", (1, 1000)),
                "area_window": ("Check a fixed ISTD area window", "bool", None),
                "area_low": ("Lowest area", "float", (0, 1e12)), "area_high": ("Highest area", "float", (0, 1e12)),
                "detection": ("Uncertain automatic ISTD detection counts", "bool", None)},
    "substance_above": {"mgkg": ("Concentration (mg/kg)", "float", (0, 1e6)),
                        "pattern": ("Only names / CAS matching (e.g. *phthalat*; 80-05-7)", "text", None)},
    "unidentified_over": {"max": ("Allowed unidentified substances", "int", (0, 1000))},
    "feature_review": {"yellow": ("List the yellow substances too (made consistent automatically)", "bool", None)},
}


class RulesDialog(QDialog):
    """Which rules send a report to *Control needed* (and their parameters)."""

    def __init__(self, rules: list, parent=None, allow_default: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Report² - rules")
        self.setMinimumSize(620, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(theme.hint("A report is accepted automatically when no rule finds anything. A rule set to "
                                 "'control needed' sends it to the analyst; 'note only' lists the finding but "
                                 "accepts the report."))
        body = QWidget()
        bl = QVBoxLayout(body)
        self._rows = []
        for r in rules:
            title, text = RU.RULES[r.id][0], RU.RULES[r.id][1]
            box = QGroupBox()
            box.setTitle(title)
            box.setCheckable(True)
            box.setChecked(r.enabled)
            gl = QFormLayout(box)
            lab = QLabel(text)
            lab.setWordWrap(True)
            lab.setObjectName("hint")
            gl.addRow(lab)
            level = QComboBox()
            level.addItem("Control needed", RU.CONTROL)
            level.addItem("Note only", "info")
            level.setCurrentIndex(0 if r.level == RU.CONTROL else 1)
            gl.addRow("Finding means", level)
            widgets = {}
            for key, (label, kind, rng) in PARAMS.get(r.id, {}).items():
                v = r.p(key)
                if kind == "bool":
                    w = QCheckBox(label)
                    w.setChecked(bool(v))
                    gl.addRow("", w)
                elif kind == "int":
                    w = QSpinBox()
                    w.setRange(*rng)
                    w.setValue(int(v or 0))
                    gl.addRow(label, w)
                elif kind == "float":
                    w = QDoubleSpinBox()
                    w.setRange(*rng)
                    w.setDecimals(4 if (v or 0) < 100 else 0)
                    w.setValue(float(v or 0))
                    gl.addRow(label, w)
                else:
                    w = QLineEdit(str(v or ""))
                    gl.addRow(label, w)
                widgets[key] = (kind, w)
            bl.addWidget(box)
            self._rows.append((r.id, box, level, widgets))
        bl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        self.as_default = QCheckBox("Also make these the default rules for new Report² steps")
        self.as_default.setVisible(allow_default)
        lay.addWidget(self.as_default)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._defaults)
        lay.addWidget(bb)

    def _defaults(self):
        for (rid, box, level, widgets), r in zip(self._rows, RU.default_rules()):
            box.setChecked(r.enabled)
            level.setCurrentIndex(0)
            for key, (kind, w) in widgets.items():
                v = r.p(key)
                if kind == "bool":
                    w.setChecked(bool(v))
                elif kind in ("int", "float"):
                    w.setValue(v or 0)
                else:
                    w.setText(str(v or ""))

    def rules(self) -> list:
        out = []
        for rid, box, level, widgets in self._rows:
            params = {}
            for key, (kind, w) in widgets.items():
                params[key] = w.isChecked() if kind == "bool" else (w.value() if kind in ("int", "float")
                                                                   else w.text().strip())
            out.append(RU.Rule(rid, box.isChecked(), level.currentData(), params))
        return out

    def _ok(self):
        if self.as_default.isChecked():
            RU.save_default_rules(self.rules())
        self.accept()


class ReviewDialog(QDialog):
    """Accept or reject a report with a comment (optional): the analyst's name is taken from Windows."""

    def __init__(self, accept: bool, sample: str, findings: int, parent=None):
        super().__init__(parent)
        from gcws.core.audit import current_user
        self.setWindowTitle("Accept report" if accept else "Reject report")
        lay = QVBoxLayout(self)
        verb = "Accept" if accept else "Reject"
        lay.addWidget(QLabel(f"<b>{verb}</b> the report of <b>{sample}</b>" +
                             (f" with {findings} finding(s)" if findings else "") + f" as <b>{current_user()}</b>."))
        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("Comment (optional: what was checked, why it is rejected)")
        self.comment.setFixedHeight(90)
        lay.addWidget(self.comment)
        self.required = False
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(verb)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def text(self) -> str:
        return self.comment.toPlainText().strip()

    def _ok(self):
        self.accept()


class ReasonsDialog(QDialog):
    """The reasons Report² offers when a report is rejected, one per line."""

    def __init__(self, reasons: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Report² - reject reasons")
        self.setMinimumSize(380, 300)
        lay = QVBoxLayout(self)
        lay.addWidget(theme.hint("One reason per line. They are offered under Reject; 'Other...' asks for a "
                                 "comment instead."))
        self.edit = QPlainTextEdit("\n".join(reasons))
        lay.addWidget(self.edit, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.RestoreDefaults).setText("Defaults")
        bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(
            lambda: self.edit.setPlainText("\n".join(RU.DEFAULT_REJECT_REASONS)))
        lay.addWidget(bb)

    def reasons(self) -> list:
        return [r.strip() for r in self.edit.toPlainText().splitlines() if r.strip()]


def _table(headers: list, rows: list) -> QTableWidget:
    t = QTableWidget(len(rows), len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.Stretch)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setWordWrap(True)
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            t.setItem(r, c, QTableWidgetItem(str(v)))
    t.resizeColumnsToContents()
    return t


class HistoryDialog(QDialog):
    """What happened to one report (who processed, accepted, rejected or delivered it, and when) and its
    files with where they were delivered. Not modal: Report² stays usable."""

    def __init__(self, title: str, events: list, files: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Report² - history of {title}")
        self.setMinimumSize(640, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>History</b>"))
        self.history = _table(["When", "Who", "What"], events)
        lay.addWidget(self.history, 2)
        lay.addWidget(QLabel("<b>Files</b>"))
        self.files = _table(["File", "Where", "Delivered to"], files)
        lay.addWidget(self.files, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)
