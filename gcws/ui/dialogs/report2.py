"""Report² dialogs: the rules that decide "Control needed", and accepting / rejecting a report."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QLabel, QLineEdit, QPlainTextEdit, QScrollArea, QSpinBox, QVBoxLayout,
                               QWidget)

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
    """Accept or reject a report: the analyst's name is taken from Windows, a comment is asked for."""

    def __init__(self, accept: bool, sample: str, findings: int, parent=None):
        super().__init__(parent)
        from gcws.core.audit import current_user
        self.setWindowTitle("Accept report" if accept else "Reject report")
        lay = QVBoxLayout(self)
        verb = "Accept" if accept else "Reject"
        lay.addWidget(QLabel(f"<b>{verb}</b> the report of <b>{sample}</b>" +
                             (f" with {findings} finding(s)" if findings else "") + f" as <b>{current_user()}</b>."))
        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("Comment (what was checked, why it is rejected)")
        self.comment.setFixedHeight(90)
        lay.addWidget(self.comment)
        self.required = bool(findings) or not accept
        self.note = theme.hint("A comment is required." if self.required else "")
        lay.addWidget(self.note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(verb)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def text(self) -> str:
        return self.comment.toPlainText().strip()

    def _ok(self):
        if self.required and not self.text():
            self.note.setText("Please enter a comment.")
            theme.set_chip(self.note, "Please enter a comment.", "bad")
            return
        self.accept()
