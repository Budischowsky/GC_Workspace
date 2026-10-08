"""Report template window: the analyst designs the Template report and sees it with real data.

Columns come from the catalogue of every Peaks-panel and double-determination value (grouped, filterable,
greyed with the reason when the current quantification cannot fill them); the report columns are an
ordered list with their header, decimals and double-determination view. Header, row rules and extras are
on their own tabs. The preview below shows the active run or replicate group as the report will look.
Templates are kept by name (``gcws.report.template``); "Use in method" makes the template the method's
(one undoable step), "Save to method" also writes it into the current processing method file."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QFont
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QSplitter,
                               QStyledItemDelegate, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from gcws.report import catalog as C
from gcws.report import template as TP
from gcws.ui import theme

UNSAVED = "(not saved)"
C_FIELD, C_HEADER, C_DEC, C_VIEW = range(4)
FIELD_ROLE = Qt.UserRole
VALUE_ROLE = Qt.UserRole + 1


class _ColumnsDelegate(QStyledItemDelegate):
    """Decimals as a spin box ("auto" = the column's default), the double-determination view as a list."""

    def createEditor(self, parent, option, index):
        if index.column() == C_DEC:
            box = QSpinBox(parent)
            box.setRange(-1, 8)
            box.setSpecialValueText("auto")
            return box
        if index.column() == C_VIEW:
            combo = QComboBox(parent)
            field = C.get(index.sibling(index.row(), C_FIELD).data(FIELD_ROLE))
            for v in C.views_for(field) if field is not None else ["mean"]:
                combo.addItem(TP.VIEWS[v], v)
            return combo
        return super().createEditor(parent, option, index)

    def setEditorData(self, editor, index):
        if isinstance(editor, QSpinBox):
            v = index.data(VALUE_ROLE)
            editor.setValue(-1 if v is None else int(v))
        elif isinstance(editor, QComboBox):
            i = editor.findData(index.data(VALUE_ROLE))
            editor.setCurrentIndex(max(0, i))
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QSpinBox):
            v = editor.value()
            model.setData(index, None if v < 0 else v, VALUE_ROLE)
            model.setData(index, "auto" if v < 0 else str(v), Qt.DisplayRole)
        elif isinstance(editor, QComboBox):
            model.setData(index, editor.currentData(), VALUE_ROLE)
            model.setData(index, editor.currentText(), Qt.DisplayRole)
        else:
            super().setModelData(editor, model, index)


class ReportTemplateDialog(QDialog):
    def __init__(self, ws, parent=None, *, group_for_preview=None, method_name=None):
        super().__init__(parent)
        self.setWindowTitle("Report template")
        self.ws = ws
        self._group_for_preview = group_for_preview
        self._method_name = method_name or (lambda: "")
        self._loading = False
        self._filling = False
        self._cache: dict = {}
        self.name = ""
        self.tpl = TP.preset("Empty")
        self._baseline = None
        self._focus_edit = None
        self.last_table = None

        top = QHBoxLayout()
        top.addWidget(QLabel("Template"))
        self.templates = QComboBox()
        self.templates.setMinimumWidth(220)
        self.templates.setToolTip("Saved report templates: choosing one opens it here")
        self.templates.activated.connect(self._chosen)
        top.addWidget(self.templates, 1)
        self.new_btn = QToolButton()
        self.new_btn.setText("New from")
        self.new_btn.setPopupMode(QToolButton.InstantPopup)
        self.new_btn.setToolTip("Start from a built-in layout or from the method's template")
        menu = QMenu(self.new_btn)
        for name in TP.PRESETS:
            menu.addAction(f"{name} layout" if name != "Empty" else "Empty template",
                           lambda n=name: self.new_from(n))
        menu.addSeparator()
        menu.addAction("The method's template", lambda: self.new_from(None))
        self.new_btn.setMenu(menu)
        top.addWidget(self.new_btn)
        self.save_btn = QPushButton("Save")
        self.save_btn.setToolTip("Save the template under its name")
        self.save_btn.clicked.connect(lambda: self.save())
        self.save_as_btn = QPushButton("Save as...")
        self.save_as_btn.clicked.connect(lambda: self.save_as())
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(lambda: self.delete())
        for b in (self.save_btn, self.save_as_btn, self.delete_btn):
            top.addWidget(b)
        self.changed_chip = theme.chip()
        self.method_chip = theme.chip()
        top.addWidget(self.changed_chip)
        top.addWidget(self.method_chip)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._columns_tab(), "Columns")
        self.tabs.addTab(self._header_tab(), "Header")
        self.tabs.addTab(self._rows_tab(), "Rows")
        self.tabs.addTab(self._extras_tab(), "Extras")

        prev = QWidget()
        pl = QVBoxLayout(prev)
        pl.setContentsMargins(0, 0, 0, 0)
        ph = QHBoxLayout()
        title = QLabel("Preview")
        title.setObjectName("title")
        ph.addWidget(title)
        self.r_run = QRadioButton("Active run")
        self.r_run.setToolTip("The active chromatogram as a single determination")
        self.r_group = QRadioButton("Active replicate group")
        self.r_group.setToolTip("The replicate group of the active run (double or N-fold determination)")
        self.r_group.setChecked(True)
        grp = QButtonGroup(self)
        for r in (self.r_run, self.r_group):
            grp.addButton(r)
            r.toggled.connect(lambda on: on and self.refresh_preview(now=True))
            ph.addWidget(r)
        self.what = theme.hint(wrap=False)
        ph.addWidget(self.what, 1)
        pl.addLayout(ph)
        self.warnings = QLabel()
        self.warnings.setObjectName("warning")
        self.warnings.setWordWrap(True)
        pl.addWidget(self.warnings)
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        pl.addWidget(self.preview, 1)

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tabs)
        split.addWidget(prev)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 380])

        bottom = QHBoxLayout()
        self.word_btn = QPushButton("Word preview...")
        self.word_btn.setToolTip("Make the report of the previewed determinations and show its Word pages")
        self.word_btn.clicked.connect(lambda: self.word_preview())
        self.use_btn = QPushButton("Use in method")
        self.use_btn.setToolTip("The method reports with this template (Run Method, automation, report buttons). "
                                "Undo reverts it; save the processing method to keep it.")
        self.use_btn.clicked.connect(lambda: self.use_in_method())
        self.save_method_btn = QPushButton("Save to method")
        self.save_method_btn.setToolTip("Use the template in the method and save it into the current processing "
                                        "method file")
        self.save_method_btn.clicked.connect(lambda: self.save_to_method())
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bottom.addWidget(self.word_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.use_btn)
        bottom.addWidget(self.save_method_btn)
        bottom.addWidget(close)
        theme.set_primary(self.use_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)
        self.resize(1100, 820)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._render)
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(0)
        self._sync_timer.timeout.connect(self._sync_columns)
        for sig in ("quantChanged", "replicatesChanged", "resultChanged", "identsChanged", "activeRunChanged"):
            s = getattr(ws, sig, None)
            if s is not None:
                s.connect(self._data_changed)
        if TP.of(ws.quant) is not None:
            self.open_template(TP.of(ws.quant), from_method=True)
        else:
            self.new_from(TP.starter_for(ws.quant))

    # -- tabs ----------------------------------------------------------------------------------

    def _columns_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        head = QLabel("Available")
        head.setObjectName("title")
        left.addWidget(head)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter columns...")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter_palette)
        left.addWidget(self.filter)
        self.fields_tree = QTreeWidget()
        self.fields_tree.setHeaderHidden(True)
        self.fields_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.fields_tree.itemDoubleClicked.connect(lambda it, _c: self._add_items([it]))
        left.addWidget(self.fields_tree, 1)
        add = QPushButton("Add  →")
        add.setToolTip("Add the selected columns to the report (double-click works too)")
        add.clicked.connect(lambda: self._add_items(self.fields_tree.selectedItems()))
        left.addWidget(add)
        lay.addLayout(left, 2)

        right = QVBoxLayout()
        head = QLabel("Report columns (report order)")
        head.setObjectName("title")
        right.addWidget(head)
        self.cols = QTreeWidget()
        self.cols.setColumnCount(4)
        self.cols.setHeaderLabels(["Column", "Header", "Decimals", "Double determination"])
        self.cols.setRootIsDecorated(False)
        self.cols.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.cols.setDragDropMode(QAbstractItemView.InternalMove)
        self.cols.setDefaultDropAction(Qt.MoveAction)
        self.cols.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                  | QAbstractItemView.SelectedClicked)
        self.cols.setItemDelegate(_ColumnsDelegate(self.cols))
        self.cols.header().setSectionResizeMode(C_HEADER, QHeaderView.Stretch)
        self.cols.itemChanged.connect(lambda *_: self._columns_edited())
        self.cols.model().rowsMoved.connect(lambda *_: self._columns_edited())
        self.cols.model().rowsInserted.connect(lambda *_: self._columns_edited())
        right.addWidget(self.cols, 1)
        h = QHBoxLayout()
        for text, tip, slot in (("▲  Up", "Move the selected columns left in the report", lambda: self.move(-1)),
                                ("▼  Down", "Move the selected columns right in the report", lambda: self.move(1)),
                                ("Remove", "Remove the selected columns", lambda: self.remove_selected())):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            h.addWidget(b)
        h.addStretch(1)
        right.addLayout(h)
        right.addWidget(theme.hint("Double-click a header, decimals or view to change it; drag to reorder. "
                                   "Double determination: the mean, each determination (A, B), both, or "
                                   "'A / B' in one cell. A single determination shows one column."))
        lay.addLayout(right, 3)
        return w

    def _header_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QFormLayout()
        self.title_edit = QLineEdit()
        self.subtitle_edit = QLineEdit()
        for e in (self.title_edit, self.subtitle_edit):
            e.textEdited.connect(lambda *_: self._header_edited())
            e.installEventFilter(self)
        f.addRow("Title (red band)", self.title_edit)
        f.addRow("Subtitle", self.subtitle_edit)
        lay.addLayout(f)
        self.lines = QTableWidget(0, 3)
        self.lines.setHorizontalHeaderLabels(["Line", "Label", "Value"])
        self.lines.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.lines.verticalHeader().setVisible(False)
        self.lines.itemChanged.connect(lambda *_: self._header_edited())
        lay.addWidget(self.lines, 1)
        h = QHBoxLayout()
        b_add = QPushButton("Add field")
        b_add.setToolTip("A label and its value on the last header line")
        b_add.clicked.connect(lambda: self.add_header_field(new_line=False))
        b_line = QPushButton("Add line")
        b_line.setToolTip("A label and its value on a new header line")
        b_line.clicked.connect(lambda: self.add_header_field(new_line=True))
        b_del = QPushButton("Remove field")
        b_del.clicked.connect(lambda: self.remove_header_field())
        ins = QToolButton()
        ins.setText("Insert placeholder")
        ins.setPopupMode(QToolButton.InstantPopup)
        pm = QMenu(ins)
        for k, desc in TP.PLACEHOLDERS.items():
            pm.addAction(f"{{{k}}}  {desc}", lambda k=k: self.insert_placeholder(k))
        ins.setMenu(pm)
        for b in (b_add, b_line, b_del, ins):
            h.addWidget(b)
        h.addStretch(1)
        lay.addLayout(h)
        lay.addWidget(theme.hint("Placeholders in curly brackets are filled in the report, e.g. {sample}, "
                                 "{determination}, {migrate}. Fields with the same line number share a line."))
        return w

    def _rows_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.only_reported = QCheckBox("Only rows ticked Report in the double determination")
        self.skip_istd = QCheckBox("Leave out the internal standards")
        self.skip_nameless = QCheckBox("Leave out rows without name and CAS")
        self.skip_sums = QCheckBox("Leave out library sum rows ('Sum of ...')")
        self.unidentified = QComboBox()
        self.unidentified.addItem("Report them", "report")
        self.unidentified.addItem("Leave them out", "hide")
        self.min_score = QDoubleSpinBox()
        self.min_score.setRange(0, 100)
        self.min_score.setDecimals(0)
        self.min_score.setSpecialValueText("Off")
        self.limit_mode = QComboBox()
        self.limit_mode.addItem("Off", "off")
        self.limit_mode.addItem("The method's reporting limit", "method")
        self.limit_mode.addItem("This value", "value")
        self.limit_value = QDoubleSpinBox()
        self.limit_value.setDecimals(6)
        self.limit_value.setRange(0, 1e9)
        self.limit_field = QComboBox()
        self.limit_field.setToolTip("The concentration the limit is compared with")
        self.sort = QComboBox()
        for k, v in TP.SORTS.items():
            self.sort.addItem(v, k)
        self.category_sums = QCheckBox("NIAS category sums (styrene oligomers, hydrocarbons, siloxanes, "
                                       "cyclic polyester oligomers)")
        self.repeated_sums = QCheckBox("Sums of substances found more than once")
        self.empty_text = QLineEdit()
        limit = QHBoxLayout()
        limit.addWidget(self.limit_mode)
        limit.addWidget(self.limit_value)
        limit.addWidget(QLabel("on"))
        limit.addWidget(self.limit_field, 1)
        f.addRow(self.only_reported)
        f.addRow(self.skip_istd)
        f.addRow(self.skip_nameless)
        f.addRow(self.skip_sums)
        f.addRow("Unidentified substances", self.unidentified)
        f.addRow("Minimum score", self.min_score)
        f.addRow("Reporting limit", limit)
        f.addRow("Sort by", self.sort)
        f.addRow(self.category_sums)
        f.addRow(self.repeated_sums)
        f.addRow("Text when nothing is reported", self.empty_text)
        for box in (self.only_reported, self.skip_istd, self.skip_nameless, self.skip_sums, self.category_sums,
                    self.repeated_sums):
            box.toggled.connect(lambda *_: self._form_edited())
        for combo in (self.unidentified, self.limit_mode, self.limit_field, self.sort):
            combo.activated.connect(lambda *_: self._form_edited())
        for spin in (self.min_score, self.limit_value):
            spin.valueChanged.connect(lambda *_: self._form_edited())
        self.empty_text.textEdited.connect(lambda *_: self._form_edited())
        return w

    def _extras_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.orientation = QComboBox()
        self.orientation.addItem("Portrait", "portrait")
        self.orientation.addItem("Landscape", "landscape")
        self.word = QCheckBox("Word document (.docx) next to the workbook")
        self.sml_bold = QComboBox()
        self.sml_bold.setToolTip("Bold the value when the substance has no SML or exceeds it")
        self.footnotes = QCheckBox("Footnote markers from CASINFO.xlsx")
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("One note per line, printed below the table")
        self.notes.setMaximumHeight(70)
        sheets = QGroupBox("Further sheets of the workbook")
        sl = QHBoxLayout(sheets)
        self.sheet_det = QCheckBox("Determinations")
        self.sheet_calc = QCheckBox("Calculation")
        self.sheet_audit = QCheckBox("Audit")
        for b in (self.sheet_det, self.sheet_calc, self.sheet_audit):
            sl.addWidget(b)
        sl.addStretch(1)
        self.audit_page = QCheckBox("Audit Trail page in the Word document")
        self.hide_empty = QCheckBox("Hide columns without any value")
        self.record_seen = QCheckBox("Count the reported substances in the unknown register")
        self.on_method_run = QCheckBox("Create the report when the method runs (Run Method, automation)")
        self.default_report = QCheckBox("Report buttons preview this report")
        self.file_suffix = QLineEdit()
        self.file_suffix.setPlaceholderText("_<template name>_Report")
        self.file_suffix.setToolTip("End of the file name after the sample number")
        f.addRow("Page", self.orientation)
        f.addRow(self.word)
        f.addRow("Bold above the SML", self.sml_bold)
        f.addRow(self.footnotes)
        f.addRow("Notes", self.notes)
        f.addRow(sheets)
        f.addRow(self.audit_page)
        f.addRow(self.hide_empty)
        f.addRow(self.record_seen)
        f.addRow(self.on_method_run)
        f.addRow(self.default_report)
        f.addRow("File name ending", self.file_suffix)
        for box in (self.word, self.footnotes, self.sheet_det, self.sheet_calc, self.sheet_audit, self.audit_page,
                    self.hide_empty, self.record_seen, self.on_method_run, self.default_report):
            box.toggled.connect(lambda *_: self._form_edited())
        for combo in (self.orientation, self.sml_bold):
            combo.activated.connect(lambda *_: self._form_edited())
        self.notes.textChanged.connect(lambda: self._form_edited())
        self.file_suffix.textEdited.connect(lambda *_: self._form_edited())
        return w

    # -- the template ---------------------------------------------------------------------------

    def open_template(self, tpl: dict, *, from_method: bool = False) -> None:
        """Show ``tpl`` (its saved version, if any, is the baseline for "changed")."""
        self.tpl = TP.normalise(tpl)
        self.name = self.tpl["name"]
        try:
            self._baseline = TP.load(self.name) if self.name else None
        except KeyError:
            self._baseline = None
        if self._baseline is None and from_method and TP.of(self.ws.quant) is not None:
            self._baseline = TP.of(self.ws.quant)
        self._load_widgets()
        self.refresh_preview(now=True)

    def current(self) -> dict:
        return copy.deepcopy(self.tpl)

    def is_modified(self) -> bool:
        return self._baseline is None or TP.differs(self.tpl, self._baseline)

    def in_method(self) -> bool:
        mine = TP.of(self.ws.quant)
        return mine is not None and not TP.differs(mine, self.tpl)

    def new_from(self, preset) -> None:
        """Start from a built-in layout (``preset``) or from the method's template (None)."""
        if preset is None:
            tpl = TP.of(self.ws.quant)
            if tpl is None:
                QMessageBox.information(self, "Report template", "The method has no report template yet.")
                return
            self.open_template(tpl, from_method=True)
            return
        tpl = TP.preset(preset)
        tpl["name"] = ""
        self.open_template(tpl)

    def save(self, name=None) -> bool:
        name = name or self.name
        if not name:
            return self.save_as()
        tpl = TP.stamped(self.tpl, name)
        TP.save(tpl)
        self.tpl, self.name, self._baseline = tpl, name, copy.deepcopy(tpl)
        self._refresh_names()
        self._update_state()
        return True

    def save_as(self, name=None) -> bool:
        if name is None:
            name, ok = QInputDialog.getText(self, "Save report template", "Name:", text=self.name)
            if not ok or not name.strip():
                return False
            if name.strip() in TP.names() and name.strip() != self.name and QMessageBox.question(
                    self, "Save report template", f"Replace the template '{name.strip()}'?") != QMessageBox.Yes:
                return False
        return self.save(name.strip())

    def delete(self, confirm: bool = True) -> bool:
        if not self.name or self.name not in TP.names():
            return False
        if confirm and QMessageBox.question(self, "Delete report template",
                                            f"Delete the saved template '{self.name}'?") != QMessageBox.Yes:
            return False
        TP.delete(self.name)
        self._baseline = None
        self._refresh_names()
        self._update_state()
        return True

    def use_in_method(self) -> None:
        """The method reports with this template: one undoable step."""
        if self.in_method():
            return
        self.ws.push_quant(f"Report template '{self.name or 'unnamed'}' in the method",
                           TP.applied(self.ws.quant, self.tpl), "report template")
        self._update_state()

    def save_to_method(self, name=None) -> bool:
        """Use the template in the method and save it into the processing method file ``name`` (default:
        the current method)."""
        from gcws.core import proc_method as PM
        name = name or self._method_name()
        if not name:
            QMessageBox.information(self, "Report template", "No processing method is loaded: save one first "
                                    "(Method > Save processing method...).")
            return False
        try:
            method = PM.load(name)
        except KeyError:
            QMessageBox.warning(self, "Report template", f"The processing method '{name}' was not found.")
            return False
        self.use_in_method()
        method.setdefault("sections", {})[TP.QUANT_KEY] = copy.deepcopy(self.tpl)
        PM.save(method)
        self.ws.log(f"Report template saved to the processing method '{name}'", "", "report template")
        self._update_state()
        return True

    # -- widgets <-> template --------------------------------------------------------------------

    def _refresh_names(self):
        was, self._loading = self._loading, True
        self.templates.clear()
        names = TP.names()
        if not self.name or self.name not in names:
            self.templates.addItem(self.name or UNSAVED, None)
        for n in names:
            self.templates.addItem(n, n)
        i = self.templates.findData(self.name) if self.name in names else 0
        self.templates.setCurrentIndex(max(0, i))
        self.delete_btn.setEnabled(self.name in names)
        self._loading = was

    def _chosen(self, index):
        name = self.templates.itemData(index)
        if name and name != self.name:
            try:
                self.open_template(TP.load(name))
            except KeyError:
                self._refresh_names()

    def _load_widgets(self):
        self._loading = True
        t = self.tpl
        self._refresh_names()
        self._fill_palette()
        self._fill_columns()
        h = t["header"]
        self.title_edit.setText(h["title"])
        self.subtitle_edit.setText(h["subtitle"])
        self.lines.blockSignals(True)
        self.lines.setRowCount(0)
        for i, line in enumerate(h["lines"], 1):
            for p in line:
                self._add_line_row(i, p["label"], p["value"])
        self.lines.blockSignals(False)
        r = t["rows"]
        self.only_reported.setChecked(r["only_reported"])
        self.skip_istd.setChecked(r["skip_istd"])
        self.skip_nameless.setChecked(r["skip_nameless"])
        self.skip_sums.setChecked(r["skip_library_sums"])
        self.unidentified.setCurrentIndex(max(0, self.unidentified.findData(r["unidentified"])))
        self.min_score.setValue(r["min_score"] or 0)
        lim = r["limit"]
        mode = "method" if lim["use_method"] else ("value" if lim["value"] else "off")
        self.limit_mode.setCurrentIndex(self.limit_mode.findData(mode))
        self.limit_value.setValue(lim["value"] or 0.0)
        self.sort.setCurrentIndex(max(0, self.sort.findData(r["sort"])))
        self.category_sums.setChecked(r["category_sums"])
        self.repeated_sums.setChecked(r["repeated_sums"])
        self.empty_text.setText(r["empty_text"])
        e = t["extras"]
        self.orientation.setCurrentIndex(max(0, self.orientation.findData(e["orientation"])))
        self.word.setChecked(e["word"])
        self.footnotes.setChecked(e["footnotes"])
        self.notes.setPlainText("\n".join(e["notes"]))
        self.sheet_det.setChecked(e["sheets"]["determinations"])
        self.sheet_calc.setChecked(e["sheets"]["calculation"])
        self.sheet_audit.setChecked(e["sheets"]["audit"])
        self.audit_page.setChecked(e["audit_page"])
        self.hide_empty.setChecked(e["hide_empty_columns"])
        self.record_seen.setChecked(e["record_seen"])
        self.on_method_run.setChecked(e["on_method_run"])
        self.default_report.setChecked(e["default_report"])
        self.file_suffix.setText(e["file_suffix"])
        self._fill_conc_combos()
        self._loading = False
        self._update_state()

    def _conc_fields(self) -> list:
        """Concentration fields for the limit and the SML bold: the template's first, then the mode's."""
        keys = [c["field"] for c in self.tpl["columns"] if (C.get(c["field"]) or C.get("rt")).group == "Concentration"]
        info = C.Info.of_quant(self.ws.quant)
        keys += [f.key for f in C.by_group()["Concentration"] if C.availability(f, info)[0]]
        return list(dict.fromkeys(keys))

    def _fill_conc_combos(self):
        lim, bold = self.tpl["rows"]["limit"]["field"], self.tpl["extras"]["sml_bold_field"]
        for combo, current, off in ((self.limit_field, lim, "First concentration column"),
                                    (self.sml_bold, bold, "Off")):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(off, "")
            for k in list(dict.fromkeys(self._conc_fields() + ([current] if current else []))):
                f = C.get(k)
                combo.addItem(C.header_of(f, "", self.ws.quant) if f else k, k)
            combo.setCurrentIndex(max(0, combo.findData(current)))
            combo.blockSignals(False)
        self.limit_value.setEnabled(self.limit_mode.currentData() == "value")
        self.limit_field.setEnabled(self.limit_mode.currentData() != "off")

    def _fill_palette(self):
        from PySide6.QtGui import QPalette
        info = C.Info.of_quant(self.ws.quant)
        muted = self.fields_tree.palette().color(QPalette.Disabled, QPalette.Text)
        self.fields_tree.clear()
        bold = QFont()
        bold.setBold(True)
        for group, fields in C.by_group().items():
            top = QTreeWidgetItem([group])
            top.setFont(0, bold)
            top.setFlags(Qt.ItemIsEnabled)
            self.fields_tree.addTopLevelItem(top)
            for f in fields:
                it = QTreeWidgetItem([f.label if not f.key.startswith("conc:") else f"Conc. {f.label}"])
                it.setData(0, FIELD_ROLE, f.key)
                ok, why = C.availability(f, info)
                tip = f.tip + ("" if ok else ("\n" if f.tip else "") + "Not in this quantification: " + why)
                it.setToolTip(0, tip)
                if not ok:
                    it.setForeground(0, QBrush(muted))
                top.addChild(it)
            top.setExpanded(True)
        self._filter_palette(self.filter.text())

    def _filter_palette(self, text):
        text = text.strip().lower()
        for i in range(self.fields_tree.topLevelItemCount()):
            top = self.fields_tree.topLevelItem(i)
            shown = 0
            for j in range(top.childCount()):
                it = top.child(j)
                hide = bool(text) and text not in it.text(0).lower() and text not in top.text(0).lower()
                it.setHidden(hide)
                shown += not hide
            top.setHidden(shown == 0)

    def _column_item(self, col: dict) -> QTreeWidgetItem:
        f = C.get(col["field"])
        label = (f"Conc. {f.label}" if f.key.startswith("conc:") else f.label) if f else col["field"]
        dec = col["decimals"]
        it = QTreeWidgetItem([label, col["header"] or (C.header_of(f, "", self.ws.quant) if f else ""),
                             "auto" if dec is None else str(dec), TP.VIEWS.get(col["view"], col["view"])])
        it.setData(C_FIELD, FIELD_ROLE, col["field"])
        it.setData(C_HEADER, VALUE_ROLE, col["header"])
        it.setData(C_DEC, VALUE_ROLE, dec)
        it.setData(C_VIEW, VALUE_ROLE, col["view"])
        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable | Qt.ItemIsDragEnabled)
        if f is not None:
            ok, why = C.availability(f, C.Info.of_quant(self.ws.quant))
            it.setToolTip(C_FIELD, f.tip if ok else f"⚠ {why}: left out of this quantification's report")
            if not ok:
                it.setText(C_FIELD, "⚠ " + label)
            if not f.numeric:
                it.setText(C_DEC, "")
        return it

    def _fill_columns(self):
        self._filling = True
        try:
            self.cols.clear()
            for col in self.tpl["columns"]:
                self.cols.addTopLevelItem(self._column_item(col))
        finally:
            self._filling = False
        for c in (C_FIELD, C_DEC, C_VIEW):
            self.cols.resizeColumnToContents(c)

    def _read_columns(self) -> list:
        out = []
        for i in range(self.cols.topLevelItemCount()):
            it = self.cols.topLevelItem(i)
            key = it.data(C_FIELD, FIELD_ROLE)
            f = C.get(key)
            header = it.text(C_HEADER)
            if f is not None and header == C.header_of(f, "", self.ws.quant):
                header = ""                              # the default follows the unit
            out.append({"field": key, "header": header, "decimals": it.data(C_DEC, VALUE_ROLE),
                        "view": it.data(C_VIEW, VALUE_ROLE) or "mean"})
        return out

    def _columns_edited(self):
        if self._loading or self._filling:
            return
        self._sync_timer.start()                           # after a drop has finished

    def _sync_columns(self):
        cols = self._read_columns()
        if cols != self.tpl["columns"]:
            self.tpl["columns"] = TP.normalise({"columns": cols})["columns"]
            self._changed()

    def _add_items(self, items):
        keys = [it.data(0, FIELD_ROLE) for it in items if it.data(0, FIELD_ROLE)]
        for k in keys:
            self.add_field(k)

    def add_field(self, key: str, view: str = "mean") -> None:
        f = C.get(key)
        if f is None:
            return
        self.tpl["columns"].append({"field": key, "header": "", "decimals": None,
                                    "view": view if view in C.views_for(f) else "mean"})
        self._fill_columns()
        self.cols.setCurrentItem(self.cols.topLevelItem(self.cols.topLevelItemCount() - 1))
        self._changed()

    def _selected_rows(self) -> list:
        return sorted(self.cols.indexOfTopLevelItem(it) for it in self.cols.selectedItems())

    def remove_selected(self) -> None:
        rows = set(self._selected_rows())
        if not rows:
            return
        self.tpl["columns"] = [c for i, c in enumerate(self.tpl["columns"]) if i not in rows]
        self._fill_columns()
        self._changed()

    def move(self, delta: int) -> None:
        rows = self._selected_rows()
        cols = self.tpl["columns"]
        if not rows or (delta < 0 and rows[0] == 0) or (delta > 0 and rows[-1] == len(cols) - 1):
            return
        order = list(range(len(cols)))
        for r in (rows if delta < 0 else reversed(rows)):
            order[r], order[r + delta] = order[r + delta], order[r]
        self.tpl["columns"] = [cols[i] for i in order]
        self._fill_columns()
        for r in rows:
            self.cols.topLevelItem(r + delta).setSelected(True)
        self._changed()

    def set_column(self, row: int, *, header=None, decimals="keep", view=None) -> None:
        """Change one report column (what the cell editors do)."""
        col = self.tpl["columns"][row]
        if header is not None:
            col["header"] = header
        if decimals != "keep":
            col["decimals"] = decimals
        if view is not None:
            col["view"] = view
        self.tpl = TP.normalise(self.tpl)
        self._fill_columns()
        self._changed()

    def _add_line_row(self, line: int, label: str, value: str):
        r = self.lines.rowCount()
        self.lines.insertRow(r)
        for c, v in enumerate((str(line), label, value)):
            self.lines.setItem(r, c, QTableWidgetItem(v))

    def add_header_field(self, new_line: bool = False, label: str = "", value: str = "") -> None:
        last = max([int(p) for p in self._line_numbers()] or [0])
        self._add_line_row(last + 1 if (new_line or not last) else last, label, value)
        self._header_edited()

    def remove_header_field(self) -> None:
        rows = sorted({i.row() for i in self.lines.selectedIndexes()}, reverse=True)
        for r in rows:
            self.lines.removeRow(r)
        self._header_edited()

    def _line_numbers(self):
        out = []
        for r in range(self.lines.rowCount()):
            it = self.lines.item(r, 0)
            try:
                out.append(int((it.text() if it else "0") or 0))
            except ValueError:
                out.append(0)
        return out

    def insert_placeholder(self, name: str) -> None:
        text = "{" + name + "}"
        w = self._focus_edit
        if isinstance(w, QLineEdit):
            w.insert(text)
            self._header_edited()
            return
        it = self.lines.currentItem()
        if it is None or it.column() == 0:
            self.add_header_field(new_line=False, label=name.replace("_", " ").capitalize() + ":", value=text)
            return
        it.setText(it.text() + text)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.FocusIn and isinstance(obj, QLineEdit):
            self._focus_edit = obj
        return super().eventFilter(obj, event)

    def _header_edited(self):
        if self._loading:
            return
        lines: dict = {}
        for r, n in enumerate(self._line_numbers()):
            label = self.lines.item(r, 1).text() if self.lines.item(r, 1) else ""
            value = self.lines.item(r, 2).text() if self.lines.item(r, 2) else ""
            lines.setdefault(n, []).append({"label": label, "value": value})
        self.tpl["header"] = {"title": self.title_edit.text(), "subtitle": self.subtitle_edit.text(),
                              "lines": [lines[k] for k in sorted(lines)]}
        self._changed()

    def _form_edited(self):
        if self._loading:
            return
        r, e = self.tpl["rows"], self.tpl["extras"]
        r.update(only_reported=self.only_reported.isChecked(), skip_istd=self.skip_istd.isChecked(),
                 skip_nameless=self.skip_nameless.isChecked(), skip_library_sums=self.skip_sums.isChecked(),
                 unidentified=self.unidentified.currentData(), min_score=self.min_score.value() or None,
                 sort=self.sort.currentData(), category_sums=self.category_sums.isChecked(),
                 repeated_sums=self.repeated_sums.isChecked(), empty_text=self.empty_text.text())
        mode = self.limit_mode.currentData()
        r["limit"] = {"field": self.limit_field.currentData() or "", "use_method": mode == "method",
                      "value": self.limit_value.value() if mode == "value" else None}
        e.update(orientation=self.orientation.currentData(), word=self.word.isChecked(),
                 sml_bold_field=self.sml_bold.currentData() or "", footnotes=self.footnotes.isChecked(),
                 notes=[x for x in self.notes.toPlainText().splitlines() if x.strip()],
                 sheets={"determinations": self.sheet_det.isChecked(), "calculation": self.sheet_calc.isChecked(),
                         "audit": self.sheet_audit.isChecked()},
                 audit_page=self.audit_page.isChecked(), hide_empty_columns=self.hide_empty.isChecked(),
                 record_seen=self.record_seen.isChecked(), on_method_run=self.on_method_run.isChecked(),
                 default_report=self.default_report.isChecked(), file_suffix=self.file_suffix.text())
        self.tpl = TP.normalise(self.tpl)
        self.limit_value.setEnabled(mode == "value")
        self.limit_field.setEnabled(mode != "off")
        self._changed()

    def _changed(self):
        self._update_state()
        self.refresh_preview()

    def _update_state(self):
        modified = self.is_modified()
        theme.set_chip(self.changed_chip, "not saved" if self._baseline is None else
                       ("● changed" if modified else ""), "warn")
        theme.set_chip(self.method_chip, "In method" if self.in_method() else "", "ok")
        self.save_btn.setEnabled(modified or not self.name)
        name = self._method_name()
        self.save_method_btn.setText(f"Save to method '{name}'" if name else "Save to method")
        self.save_method_btn.setEnabled(bool(name))
        self.use_btn.setEnabled(not self.in_method())

    # -- preview -------------------------------------------------------------------------------

    def preview_members(self):
        """``(members, group)`` the preview shows."""
        ws = self.ws
        rid = getattr(ws, "active_id", None)
        if self.r_group.isChecked():
            g = self._group_for_preview() if self._group_for_preview else None
            if g is None and rid:
                g = next((g for g in ws.replicate_groups if rid in g["members"]), None)
            if g is not None and g.get("members"):
                return [m for m in g["members"] if m in ws.runs], g
        if rid and rid in ws.runs and ws.runs[rid].role == "sample":
            return [rid], {"id": "", "name": ws.runs[rid].name, "members": [rid], "policy": "all"}
        return [], None

    def _fields(self) -> set:
        t = self.tpl
        return {c["field"] for c in t["columns"]} | {t["rows"]["limit"]["field"], t["extras"]["sml_bold_field"],
                                                     "conc:mg_kg"} - {""}

    def _data_changed(self, *_):
        self._cache.clear()
        if self.isVisible():
            self.refresh_preview()

    def report_data(self):
        """The (cached) data of the previewed determinations, or None."""
        from gcws.report import template_data as TD
        members, group = self.preview_members()
        if not members:
            return None
        key = (tuple(members), (group or {}).get("policy"), id(self.ws.quant_result), frozenset(self._fields()))
        if key not in self._cache:
            self._cache.clear()
            self._cache[key] = TD.collect(self.ws, members, group, fields=self._fields(),
                                          method_name=self._method_name(), template_name=self.name)
        return self._cache[key]

    def refresh_preview(self, now: bool = False) -> None:
        if now:
            self._timer.stop()
            self._render()
        else:
            self._timer.start()

    def _render(self):
        from gcws.report import table as TB
        from gcws.report.template_html import to_html
        try:
            data = self.report_data()
        except Exception as exc:  # noqa: BLE001 - the window stays usable
            self.preview.setHtml("")
            self.warnings.setText(f"No preview: {exc}")
            return
        self.warnings.setVisible(True)
        if data is None:
            self.what.setText("")
            self.warnings.setText("Activate a sample (or a replicate group) to see the report here.")
            self.preview.setHtml("")
            return
        table = TB.build(data, self.tpl)
        self.what.setText(f"{data.values.get('samples', '')} — {data.values.get('determination', '')}")
        self.warnings.setText("\n".join("⚠ " + w for w in table.warnings[:6]))
        self.warnings.setVisible(bool(table.warnings))
        self.preview.setHtml(to_html(table))
        self.last_table = table

    def word_preview(self) -> None:
        """The report of the previewed determinations through the main window (its Word pages)."""
        members, group = self.preview_members()
        parent = self.parent()
        if not members or parent is None or not hasattr(parent, "report"):
            QMessageBox.information(self, "Report template", "Activate a sample to preview its report.")
            return
        parent.report("template", (group or {}).get("id") or None, preview=True, single=len(members) == 1
                      and not (group or {}).get("id"), template=self.current())

    def showEvent(self, event):
        super().showEvent(event)
        self._fill_palette()
        self._update_state()
        self.refresh_preview(now=True)
