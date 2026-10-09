"""Settings of one step or arrow of an automation workflow (double-click in the chart editor)."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from gcws.automation import workflow as W
from gcws.ui import theme


def _folder_row(edit: QLineEdit, parent) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(edit, 1)
    b = QPushButton("Browse...")

    def pick():
        d = QFileDialog.getExistingDirectory(parent, "Choose a folder", edit.text())
        if d:
            edit.setText(d.replace("/", "\\"))
    b.clicked.connect(pick)
    h.addWidget(b)
    return w


def _combo(items: dict, current) -> QComboBox:
    c = QComboBox()
    for k, v in items.items():
        c.addItem(v, k)
    i = c.findData(current)
    c.setCurrentIndex(max(0, i))
    return c


class NodeDialog(QDialog):
    """Parameters of a node; ``values()`` returns its new ``params``."""

    def __init__(self, node: W.Node, parent=None, method_names=None, source_folder: str = ""):
        super().__init__(parent)
        self.node = node
        self.setWindowTitle(f"{node.title} - settings")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)
        p = node.p
        self.w: dict = {}
        t = node.type
        if t == "source":
            self.w["folder"] = QLineEdit(p("folder") or "")
            form.addRow("Folder to watch", _folder_row(self.w["folder"], self))
            self.w["depth"] = _combo({0: "Runs lie directly in this folder", 1: "One folder per batch (subfolders)",
                                      2: "Two levels (e.g. month \\ batch)", 3: "Three levels"}, int(p("depth") or 0))
            form.addRow("Batch folders", self.w["depth"])
            self.w["pattern"] = QLineEdit(p("pattern") or "*")
            self.w["pattern"].setToolTip("Only batch folders whose name matches (e.g. 2601*; several with ;)")
            form.addRow("Folder names", self.w["pattern"])
            self.w["interval_min"] = self._spin(p("interval_min"), 1, 1440, " min")
            form.addRow("Check every", self.w["interval_min"])
            self.w["quiet_min"] = self._spin(p("quiet_min"), 1, 1440, " min")
            self.w["quiet_min"].setToolTip("Without a sequence log a batch is processed once nothing changed "
                                           "for this long; also ends a sequence that stopped early")
            form.addRow("Quiet time", self.w["quiet_min"])
            self.w["min_age_min"] = self._spin(p("min_age_min"), 0, 120, " min")
            form.addRow("A run must be at least", self.w["min_age_min"])
            self.w["stable_scans"] = self._spin(p("stable_scans"), 1, 10, " checks")
            form.addRow("Unchanged for", self.w["stable_scans"])
            self.w["ignore_older_days"] = self._spin(p("ignore_older_days"), 0, 3650, " days")
            self.w["ignore_older_days"].setToolTip("Batch folders not changed for longer are not looked at "
                                                   "(0 = all)")
            form.addRow("Skip folders older than", self.w["ignore_older_days"])
            self.w["process_existing"] = QCheckBox("Also process the runs already in the folder")
            self.w["process_existing"].setChecked(bool(p("process_existing")))
            form.addRow("", self.w["process_existing"])
            lay.addWidget(theme.hint("A run counts as finished when it did not change over the checks, is old "
                                     "enough and the instrument moved on (next run started, checksum.xml "
                                     "written, sequence completed) or the folder has been quiet."))
        elif t == "copy":
            self.w["folder"] = QLineEdit(p("folder") or "")
            form.addRow("Local folder", _folder_row(self.w["folder"], self))
            self.where = QLabel()
            self.where.setWordWrap(True)
            form.addRow("Copies go to", self.where)
            show = lambda: self.where.setText(
                f"{source_folder or '(watched folder)'}\\<batch folder>  →  "
                f"{self.w['folder'].text().strip() or '(local folder)'}\\<batch folder>")
            self.w["folder"].textChanged.connect(show)
            show()
            lay.addWidget(theme.hint("Every finished run is copied here, with the files beside it (sequence log), "
                                     "as soon as it is finished; the method then reads the copy. The copies are "
                                     "kept, nothing is deleted, and the watched folder is only read."))
        elif t == "method":
            self.w["method"] = QComboBox()
            self.w["method"].setEditable(False)
            for n in method_names or []:
                self.w["method"].addItem(n)
            if p("method") and self.w["method"].findText(p("method")) < 0:
                self.w["method"].addItem(p("method"))
            self.w["method"].setCurrentText(p("method") or "")
            form.addRow("Processing method", self.w["method"])
            self.w["search"] = QCheckBox("Library search of all peaks")
            self.w["search"].setChecked(bool(p("search")))
            form.addRow("", self.w["search"])
            self.w["istd_detect"] = QCheckBox("Automatic ISTD detection")
            self.w["istd_detect"].setChecked(bool(p("istd_detect")))
            form.addRow("", self.w["istd_detect"])
            self.w["min_confidence"] = _combo({"high": "High confidence only", "medium": "Medium or high"},
                                              p("min_confidence"))
            form.addRow("Bind detected ISTDs", self.w["min_confidence"])
            self.w["require_blank"] = _combo(W.BLANK_REQUIREMENTS, p("require_blank"))
            form.addRow("Blank needed", self.w["require_blank"])
            self.w["timeout_min"] = self._spin(p("timeout_min"), 5, 600, " min")
            form.addRow("Stop a sample after", self.w["timeout_min"])
            lay.addWidget(theme.hint("A sample is processed only with a blank from its own batch folder. "
                                     "Without one it is listed under 'Not processed' in Report²."))
        elif t == "report2":
            self.w["auto_accept"] = QCheckBox("Accept reports without findings automatically")
            self.w["auto_accept"].setChecked(bool(p("auto_accept")))
            form.addRow("", self.w["auto_accept"])
            self.w["notify"] = QCheckBox("Tray message when a report needs control")
            self.w["notify"].setChecked(bool(p("notify")))
            form.addRow("", self.w["notify"])
            self.rules = copy.deepcopy(p("rules"))
            self.rules_label = QLabel()
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(self.rules_label, 1)
            b = QPushButton("Rules...")
            b.clicked.connect(self._edit_rules)
            h.addWidget(b)
            form.addRow("Control needed when", row)
            self._show_rules()
        elif t == "report":
            self.w["kind"] = _combo(W.REPORT_KINDS, p("kind"))
            self.w["kind"].setToolTip("Template Report: the columns, header and rows of the processing method's "
                                      "report template (Method > Report template...)")
            form.addRow("Report", self.w["kind"])
            box = QGroupBox("Files")
            bl = QVBoxLayout(box)
            self.fmt: dict = {}
            for f, label in W.FORMATS.items():
                c = QCheckBox(label)
                c.setChecked(f in (p("formats") or []))
                self.fmt[f] = c
                bl.addWidget(c)
            lay.addWidget(box)
            self.w["batch_when"] = _combo({"all_accepted": "When every sample of the batch is accepted",
                                           "all_processed": "When every sample is processed"}, p("batch_when"))
            form.addRow("Batch report", self.w["batch_when"])
            self.w["keep_middle"] = QCheckBox("Keep the intermediate workbook")
            self.w["keep_middle"].setChecked(bool(p("keep_middle")))
            form.addRow("", self.w["keep_middle"])
        elif t == "folder":
            self.w["target"] = _combo({"path": "A fixed folder",
                                       "source": "Back into the source folder (batch report: the batch folder; "
                                                 "sample report: its first determination's .D folder)"},
                                      p("target") or "path")
            form.addRow("Target", self.w["target"])
            self.w["path"] = QLineEdit(p("path") or "")
            form.addRow("Target folder", _folder_row(self.w["path"], self))
            self.w["target"].currentIndexChanged.connect(
                lambda *_: self.w["path"].setEnabled(self.w["target"].currentData() != "source"))
            self.w["path"].setEnabled(self.w["target"].currentData() != "source")
            self.w["subfolder"] = QLineEdit(p("subfolder") or "")
            self.w["subfolder"].setToolTip("Placeholders: " + " ".join(W.SUBFOLDER_TOKENS))
            form.addRow("Subfolder", self.w["subfolder"])
            self.w["overwrite"] = _combo(W.OVERWRITE, p("overwrite"))
            form.addRow("File exists", self.w["overwrite"])
            self.w["allow_inside_source"] = QCheckBox("Allow a folder inside the watched raw-data folder")
            self.w["allow_inside_source"].setChecked(bool(p("allow_inside_source")))
            form.addRow("", self.w["allow_inside_source"])
            lay.addWidget(theme.hint("Placeholders in the subfolder: {batch} {sample} {kind} {status} {date} "
                                     "{workflow}. Empty = directly into the target folder. Back into the source "
                                     "folder: e.g. 'Auswertung' (the watcher ignores Auswertung and GCWS inside a "
                                     "run, so a report there does not start the processing again)."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    @staticmethod
    def _spin(value, lo, hi, suffix):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(int(value or lo))
        s.setSuffix(suffix)
        return s

    def _show_rules(self):
        from gcws.automation import rules as RU
        rules = RU.from_list(self.rules) if isinstance(self.rules, list) else RU.load_default_rules()
        on = [r.title for r in rules if r.enabled and r.level == RU.CONTROL]
        self.rules_label.setText(("the default rules: " if not isinstance(self.rules, list) else "")
                                 + (", ".join(on) if on else "no rule (everything is accepted)"))
        self.rules_label.setWordWrap(True)

    def _edit_rules(self):
        from gcws.automation import rules as RU
        from gcws.ui.dialogs.report2 import RulesDialog
        rules = RU.from_list(self.rules) if isinstance(self.rules, list) else RU.load_default_rules()
        dlg = RulesDialog(rules, self)
        if dlg.exec():
            self.rules = RU.to_list(dlg.rules())
            self._show_rules()

    def values(self) -> dict:
        out = dict(self.node.params)
        for k, w in self.w.items():
            if isinstance(w, QCheckBox):
                out[k] = w.isChecked()
            elif isinstance(w, QComboBox):
                out[k] = w.currentData() if w.currentData() is not None else w.currentText()
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                out[k] = w.value()
            elif isinstance(w, QLineEdit):
                out[k] = w.text().strip()
        if self.node.type == "report":
            out["formats"] = [f for f, c in self.fmt.items() if c.isChecked()]
        if self.node.type == "report2":
            out["rules"] = self.rules
        return out


class EdgeFilterDialog(QDialog):
    """What an arrow lets through."""

    def __init__(self, edge: W.Edge, wf: W.Workflow, parent=None):
        super().__init__(parent)
        self.edge = edge
        a, b = wf.node(edge.src), wf.node(edge.dst)
        self.setWindowTitle(f"Arrow {a.title} -> {b.title}")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.addWidget(theme.hint("Only what matches every chosen condition passes this arrow. Nothing chosen = "
                                 "everything passes."))
        f = edge.filter or {}
        self.status: dict = {}
        self.formats: dict = {}
        if (a.type, b.type) in (("report2", "report"), ("report", "folder"), ("method", "report")):
            box = QGroupBox("Report status")
            bl = QVBoxLayout(box)
            for k, label in W.STATUS_FILTERS.items():
                c = QCheckBox(label)
                c.setChecked(k in (f.get("status") or []))
                self.status[k] = c
                bl.addWidget(c)
            lay.addWidget(box)
        if b.type == "folder":
            box = QGroupBox("Files")
            bl = QVBoxLayout(box)
            produced = a.p("formats") or []
            for k, label in W.FORMATS.items():
                c = QCheckBox(label + ("" if k in produced else "  (not written by the report)"))
                c.setChecked(k in (f.get("formats") or []))
                self.formats[k] = c
                bl.addWidget(c)
            lay.addWidget(box)
        form = QFormLayout()
        self.name = QLineEdit(f.get("name") or "")
        self.name.setPlaceholderText("e.g. 2601*   (several with ;)")
        form.addRow("Sample names", self.name)
        self.batch = QLineEdit(f.get("batch") or "")
        self.batch.setPlaceholderText("batch folder names, e.g. *GIOSUN*")
        form.addRow("Batch folders", self.batch)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def values(self) -> dict:
        out = {}
        st = [k for k, c in self.status.items() if c.isChecked()]
        if st:
            out["status"] = st
        fm = [k for k, c in self.formats.items() if c.isChecked()]
        if fm:
            out["formats"] = fm
        if self.name.text().strip():
            out["name"] = self.name.text().strip()
        if self.batch.text().strip():
            out["batch"] = self.batch.text().strip()
        return out
