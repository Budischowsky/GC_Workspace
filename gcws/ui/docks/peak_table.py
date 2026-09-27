"""Peak / substance table dock."""
from __future__ import annotations

import csv
from datetime import datetime

from PySide6.QtCore import QSettings, QSortFilterProxyModel, Qt, Signal as QtSignal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMenu, QTableView, QToolBar, QVBoxLayout, QWidget)

from gcws.core.ident import Identification
from gcws.ui.icons import icon
from gcws.ui.models.peak_filter import FilterState, value_predicate, value_test  # noqa: F401 (re-export)
from gcws.ui.models.peak_table import COLUMN_KEYS, COLUMNS, PeakTableModel
from gcws.ui.undo import IdentCommand


#: columns the value filter can use, and its operators
FILTER_COLUMNS = [("conc", "Conc."), ("corr_area", "Corr. area"), ("area", "Area"), ("area_pct", "Area %"),
                  ("height", "Height"), ("mg_dm2", "mg/dm²"), ("score", "Score"), ("rt", "RT")]
FILTER_OPS = ["<", "≤", "=", "≥", ">", "between", "outside"]


def parse_number(text: str):
    t = (text or "").strip().replace(" ", "").replace("'", "")
    if t.count(",") == 1 and "." not in t:
        t = t.replace(",", ".")                   # decimal comma
    else:
        t = t.replace(",", "")                    # thousands separators
    try:
        return float(t)
    except ValueError:
        return None


class SortProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(Qt.UserRole)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterKeyColumn(-1)
        self.hide_predicate = None            # callable(source_row) -> True hides the row
        self.value_filter = None              # callable(source_row) -> False hides the row

    def filterAcceptsRow(self, row, parent):
        if self.hide_predicate is not None and self.hide_predicate(row):
            return False
        if self.value_filter is not None and not self.value_filter(row):
            return False
        return super().filterAcceptsRow(row, parent)

    def lessThan(self, a, b):
        va, vb = a.data(Qt.UserRole), b.data(Qt.UserRole)
        try:
            return va < vb
        except TypeError:
            return str(va) < str(vb)


class PeakTable(QWidget):
    searchRequested = QtSignal()
    integrateRequested = QtSignal(bool)       # all runs?
    deleteRequested = QtSignal(list)

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.model = PeakTableModel(ws, self)
        self.model.on_edit = self._edit
        self.context_actions: list = []       # main-window actions for the row menu
        self.proxy = SortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(1, Qt.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(20)
        self.view.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        hh = self.view.horizontalHeader()
        hh.setSectionsMovable(True)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        hh.setSectionResizeMode(QHeaderView.Interactive)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        self.view.selectionModel().currentRowChanged.connect(self._row_changed)

        tb = QToolBar()
        self.delete_action = QAction("Delete peak(s)", self.view)
        self.delete_action.setShortcut(QKeySequence(Qt.Key_Delete))
        self.delete_action.setShortcutContext(Qt.WidgetShortcut)
        self.delete_action.triggered.connect(
            lambda: self.deleteRequested.emit([r.peak.apex_rt for r in self.selected_rows()]))
        self.view.addAction(self.delete_action)
        tb.addAction(self.delete_action)
        tb.setIconSize(tb.iconSize() * 0.8)
        a = tb.addAction(icon("integrate"), "Integrate")
        a.setToolTip("Re-integrate the active chromatogram with its method")
        a.triggered.connect(lambda: self.integrateRequested.emit(False))
        a = tb.addAction(icon("integrate", "#8e44ad"), "Integrate all")
        a.setToolTip("Re-integrate all loaded chromatograms")
        a.triggered.connect(lambda: self.integrateRequested.emit(True))
        tb.addSeparator()
        self.search_action = tb.addAction(icon("search"), "Library search")
        self.search_action.setToolTip("Automatic library search of all peaks in your libraries")
        self.search_action.triggered.connect(self.searchRequested.emit)
        tb.addSeparator()
        exp = tb.addAction("Export...")
        exp.triggered.connect(self.export)
        cols = tb.addAction("Columns...")
        cols.setToolTip("Choose and order the table columns")
        cols.triggered.connect(self.choose_columns)
        self.hide_blank = tb.addAction("Hide blank peaks")
        self.hide_blank.setCheckable(True)
        self.hide_blank.setToolTip("Hide peaks that are at blank level (sample area below the ratio limit "
                                   "x the blank peak's area)")
        self.hide_blank.toggled.connect(self._hide_blank_toggled)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter...")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)
        self.info = QLabel()
        self.info.setObjectName("hint")
        from gcws.ui import theme
        self.banner = theme.chip("", "warn")          # the working signal is not available for this run
        self.banner.setWordWrap(True)
        source = QHBoxLayout()
        source.setContentsMargins(0, 0, 0, 0)
        source.addLayout(self._build_source_switch())
        source.addStretch(1)
        source.addWidget(self.info)
        source.addWidget(self.filter)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(tb)
        top.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        lay.addLayout(source)
        lay.addLayout(top)
        lay.addLayout(self._build_value_filter())
        lay.addWidget(self.banner)
        lay.addWidget(self.view, 1)

        copy = QAction("Copy", self.view)
        copy.setShortcut(QKeySequence.Copy)
        copy.setShortcutContext(Qt.WidgetShortcut)
        copy.triggered.connect(self.copy)
        self.view.addAction(copy)

        self._restore_columns()
        self._syncing = False
        for sig in (ws.activeRunChanged, ws.signalKeyChanged, ws.identsChanged, ws.quantChanged, ws.runChanged):
            sig.connect(lambda *_: self.reload())
        ws.resultChanged.connect(self._on_result)
        ws.selectionChanged.connect(self._on_selection)
        ws.runRemoved.connect(lambda *_: self.reload())
        ws.hints.updated.connect(lambda rid: self.model.refresh_column("class_hint") if rid == ws.active_id else None)
        self.reload()

    # -- data ----------------------------------------------------------------

    def _on_result(self, run_id, key):
        st = self.ws.active
        if st is None:
            return
        if (run_id == st.id and key in (self.ws.signal_key, self.ws.active_key))                 or run_id in st.blanks + st.blanks_istd:
            self.reload()

    def reload(self):
        self._syncing = True
        self.model.reload()
        self._syncing = False
        from gcws.ui import theme
        theme.set_chip(self.banner, self.ws.derived_note(self.ws.active), "warn")
        self.proxy.invalidateFilter()
        self._update_info()
        self._on_selection(self.ws.active_id, self.ws.selected)

    def _update_info(self):
        n = len(self.model.rows)
        idn = sum(1 for r in self.model.rows if r.ident and r.ident.name and
                  not r.ident.name.lower().startswith("unknown"))
        extra = f"  •  {len(self.model.orphans)} orphaned IDs" if self.model.orphans else ""
        hide = self.proxy.hide_predicate
        blank_hidden = sum(1 for r in range(n) if hide(r)) if hide is not None else 0
        if blank_hidden:
            extra += f"  •  {blank_hidden} blank peaks hidden"
        shown = self.proxy.rowCount()
        head = f"{shown} of {n} peaks shown" if shown != n else f"{n} peaks"
        self.info.setText(f"{head}  •  {idn} identified{extra}")
        self.info.setToolTip("Unassigned identifications retained for review / undo:\n" + "\n".join(
            f"{i.apex_rt:.4f} min: {i.name or 'unnamed'} ({i.cas or 'no CAS'})"
            for i in self.model.orphans) if self.model.orphans else "")

    # -- which chromatogram the table lists ------------------------------------------

    def _build_source_switch(self):
        from PySide6.QtWidgets import QButtonGroup, QToolButton
        row = QHBoxLayout()
        row.setSpacing(0)
        self.source_group = QButtonGroup(self)
        self.source_group.setExclusive(True)
        self.source_buttons = []
        for i in (0, 1):
            b = QToolButton()
            b.setCheckable(True)
            b.setObjectName("segment")
            b.setToolTip(f"List the peaks of Chromatogram {i + 1}")
            self.source_group.addButton(b, i)
            self.source_buttons.append(b)
            row.addWidget(b)
        self.source_group.idClicked.connect(lambda i: self.ws.set_table_panel(i))
        self.ws.panelsChanged.connect(self._sync_source)
        self._sync_source()
        return row

    def _sync_source(self):
        for i, b in enumerate(self.source_buttons):
            b.setText(f"Chromatogram {i + 1} · {self.ws.panel_key(i).replace(' - Blank', ' − Blank')}")
            b.setChecked(self.ws.table_panel == i)

    # -- value filter ------------------------------------------------------------

    def _build_value_filter(self):
        from PySide6.QtWidgets import QComboBox, QToolButton
        row = QHBoxLayout()
        row.setContentsMargins(4, 0, 0, 0)
        row.setSpacing(4)
        lab = QLabel("Show only peaks with")
        lab.setObjectName("hint")
        self.vf_column = QComboBox()
        for key, label in FILTER_COLUMNS:
            self.vf_column.addItem(label, key)
        self.vf_op = QComboBox()
        self.vf_op.addItems(FILTER_OPS)
        self.vf_op.setToolTip("between: both limits included; outside: below the lower or above the upper")
        self.vf_a = QLineEdit()
        self.vf_a.setPlaceholderText("value")
        self.vf_a.setMaximumWidth(110)
        self.vf_and = QLabel("and")
        self.vf_b = QLineEdit()
        self.vf_b.setPlaceholderText("value")
        self.vf_b.setMaximumWidth(110)
        self.vf_clear = QToolButton()
        self.vf_clear.setText("Clear")
        self.vf_clear.setToolTip("Show all peaks again")
        self.vf_clear.clicked.connect(self.clear_value_filter)
        self.vf_state = QLabel()
        self.vf_state.setObjectName("hint")
        for w in (lab, self.vf_column, self.vf_op, self.vf_a, self.vf_and, self.vf_b, self.vf_clear, self.vf_state):
            row.addWidget(w)
        row.addStretch(1)
        import json
        try:
            saved = json.loads(QSettings().value("table/value_filter", "") or "{}")
        except (TypeError, ValueError):
            saved = {}
        self.vf_column.setCurrentIndex(max(0, self.vf_column.findData(saved.get("column", "conc"))))
        self.vf_op.setCurrentText(saved.get("op", ">"))
        self.vf_a.setText(saved.get("a", ""))
        self.vf_b.setText(saved.get("b", ""))
        for sig in (self.vf_column.currentIndexChanged, self.vf_op.currentIndexChanged,
                    self.vf_a.textChanged, self.vf_b.textChanged):
            sig.connect(lambda *_: self._apply_value_filter())
        self._apply_value_filter(save=False)
        return row

    def clear_value_filter(self):
        self.vf_a.clear()
        self.vf_b.clear()

    def filter_state(self) -> FilterState:
        """The table's current filters (value condition, text, hidden blank peaks)."""
        op = self.vf_op.currentText()
        return FilterState(self.vf_column.currentData(), op, parse_number(self.vf_a.text()),
                           parse_number(self.vf_b.text()), self.filter.text(), self.hide_blank.isChecked())

    def shown_indices(self) -> set[int]:
        """Peak indices (active run, table signal) the table shows now."""
        return {self.model.rows[self.proxy.mapToSource(self.proxy.index(r, 0)).row()].index
                for r in range(self.proxy.rowCount())}

    def set_value_filter(self, column: str, op: str, a, b=None) -> None:
        """Programmatic filter, e.g. ``set_value_filter("conc", ">", 0.05)``."""
        self.vf_column.setCurrentIndex(max(0, self.vf_column.findData(column)))
        self.vf_op.setCurrentText(op)
        self.vf_a.setText("" if a is None else str(a))
        self.vf_b.setText("" if b is None else str(b))
        self._apply_value_filter()

    def _apply_value_filter(self, save: bool = True):
        import json
        key, op = self.vf_column.currentData(), self.vf_op.currentText()
        two = op in ("between", "outside")
        self.vf_and.setVisible(two)
        self.vf_b.setVisible(two)
        a, b = parse_number(self.vf_a.text()), parse_number(self.vf_b.text())
        active = a is not None and (b is not None or not two)
        for e, v in ((self.vf_a, a), (self.vf_b, b)):
            e.setProperty("invalid", bool(e.text().strip()) and v is None)
            e.setToolTip("not a number" if e.property("invalid") else "")
            e.style().unpolish(e)
            e.style().polish(e)
        pred = value_predicate(FilterState(key, op, a, b), self.ws) if active else None
        self.proxy.value_filter = (lambda row, pred=pred: pred(self.model.rows[row])) if pred else None
        self.vf_clear.setEnabled(active)
        self.vf_state.setText("(peaks without a value are hidden)" if active else "")
        self.proxy.invalidateFilter()
        self._update_info()
        if save:
            QSettings().setValue("table/value_filter", json.dumps(
                {"column": key, "op": op, "a": self.vf_a.text(), "b": self.vf_b.text()}))

    def _row_changed(self, current, previous):
        if self._syncing or not current.isValid():
            return
        src = self.proxy.mapToSource(current)
        if src.row() != self.ws.selected:
            self.ws.select_peak(src.row())
            self.ws.peakFocusRequested.emit(src.row())     # picked in the list: the chromatograms zoom to it

    def _on_selection(self, run_id, index):
        if index < 0 or index >= self.model.rowCount():
            return
        src = self.model.index(index, 0)
        prox = self.proxy.mapFromSource(src)
        if not prox.isValid():                # row filtered out
            self.view.viewport().update()
            return
        if self.view.currentIndex().row() != prox.row():
            self._syncing = True
            self.view.setCurrentIndex(prox)
            self.view.scrollTo(prox)
            self._syncing = False
        self.view.viewport().update()

    def selected_rows(self):
        rows = {self.proxy.mapToSource(i).row() for i in self.view.selectionModel().selectedRows()}
        return [self.model.rows[r] for r in sorted(rows)]

    # -- editing ---------------------------------------------------------------

    def _edit(self, row, key, value):
        st = self.ws.active
        if st is None:
            return
        old = row.ident
        ident = Identification(apex_rt=row.peak.apex_rt, name=old.name if old else "",
                               cas=old.cas if old else "", score=None, status="Manual",
                               source="manual", manual=True, hits=list(old.hits) if old else [],
                               istd=old.istd if old else "")
        if key == "name":
            if old and old.name == value:
                return
            ident.name = value
        elif key == "cas":
            if old and old.cas == value:
                return
            ident.cas = value
        cmd = IdentCommand(self.ws, st.id, self.ws.signal_key, [(row.peak.apex_rt, ident)],
                           f"{key} of peak {row.peak.apex_rt:.3f} = {value!r}")
        self.ws.undo_group.activeStack().push(cmd)

    def _menu(self, pos):
        rows = self.selected_rows()
        m = QMenu(self)
        if rows:
            m.addAction(self.delete_action)
            m.addAction("Copy").triggered.connect(self.copy)
            if any(r.ident for r in rows):
                m.addAction("Clear identification").triggered.connect(lambda: self._clear(rows))
            if len(rows) == 1 and rows[0].ident and len(rows[0].ident.hits) > 1:
                sub = m.addMenu("Use library hit")
                for h in rows[0].ident.hits[:10]:
                    label = f"{h.get('name', '')}  ({h.get('cas', '') or '-'})  {h.get('score', '')}"
                    sub.addAction(label).triggered.connect(lambda _=False, hit=h, r=rows[0]: self._use_hit(r, hit))
            for act in self.context_actions:
                m.addAction(act)
        m.addSeparator()
        m.addAction("Choose columns...").triggered.connect(self.choose_columns)
        m.exec(self.view.viewport().mapToGlobal(pos))

    def _hide_blank_toggled(self, on):
        if on:
            def hidden(row):
                st = self.ws.active
                if st is None:
                    return False
                m = self.ws.blank_matches(st.id).get(row)
                return m is not None and m.status == "blank"
            self.proxy.hide_predicate = hidden
        else:
            self.proxy.hide_predicate = None
        self.proxy.invalidateFilter()
        self._update_info()

    def set_context_actions(self, actions) -> None:
        self.context_actions = list(actions)

    def _clear(self, rows):
        st = self.ws.active
        changes = [(r.peak.apex_rt, None) for r in rows if r.ident]
        self.ws.undo_group.activeStack().push(
            IdentCommand(self.ws, st.id, self.ws.signal_key, changes, f"clear {len(changes)} identification(s)"))

    def _use_hit(self, row, hit):
        st = self.ws.active
        old = row.ident
        ident = Identification(apex_rt=row.peak.apex_rt, name=hit.get("name", ""), cas=hit.get("cas", "") or "",
                               score=hit.get("score"), status="Accepted (analyst)", formula=hit.get("formula", ""),
                               library=hit.get("library", ""), hits=list(old.hits) if old else [hit],
                               source=old.source if old else "", method=old.method if old else "",
                               searched_at=old.searched_at if old else "", manual=True,
                               istd=old.istd if old else "")
        self.ws.undo_group.activeStack().push(
            IdentCommand(self.ws, st.id, self.ws.signal_key, [(row.peak.apex_rt, ident)],
                         f"peak {row.peak.apex_rt:.3f}: use hit {ident.name}"))

    # -- columns -------------------------------------------------------------------

    # Column layout is stored by column *key* (order, hidden, widths), never by
    # index, so adding a column in a later version cannot shift the others.

    @staticmethod
    def default_width(key: str) -> int:
        return {"name": 220, "cas": 90, "status": 110, "qstatus": 110}.get(key, 72)

    def shown_keys(self) -> list[str]:
        hh = self.view.horizontalHeader()
        return [COLUMNS[hh.logicalIndex(v)].key for v in range(hh.count())
                if not self.view.isColumnHidden(hh.logicalIndex(v))]

    def layout_state(self) -> dict:
        hh = self.view.horizontalHeader()
        order = [COLUMNS[hh.logicalIndex(v)].key for v in range(hh.count())]
        widths = dict(self._widths)
        for i, c in enumerate(COLUMNS):
            if not self.view.isColumnHidden(i) and hh.sectionSize(i) > 0:
                widths[c.key] = hh.sectionSize(i)
        return {"order": order, "shown": self.shown_keys(), "widths": widths}

    def apply_layout(self, state: dict) -> None:
        known = set(state.get("order") or [])
        shown = [k for k in state.get("shown") or [] if k in COLUMN_KEYS]
        # columns this layout has never seen (new in this version) follow their default
        shown += [c.key for c in COLUMNS if c.key not in known and c.default and c.key not in shown]
        self._widths = {k: int(v) for k, v in (state.get("widths") or {}).items()}
        self.set_shown(shown)

    def set_shown(self, keys: list[str]) -> None:
        hh = self.view.horizontalHeader()
        keys = [k for k in keys if k in COLUMN_KEYS]
        for pos, key in enumerate(keys):
            logical = COLUMN_KEYS.index(key)
            hh.moveSection(hh.visualIndex(logical), pos)
        for i, c in enumerate(COLUMNS):
            hide = c.key not in keys
            self.view.setColumnHidden(i, hide)
            if not hide:
                self.view.setColumnWidth(i, self._widths.get(c.key) or self.default_width(c.key))

    def _restore_columns(self):
        import json
        s = QSettings()
        self._widths: dict[str, int] = {}
        raw = s.value("table/layout")
        state = None
        if raw:
            try:
                state = json.loads(raw)
            except (TypeError, ValueError):
                state = None
        if state is None:                            # older settings: hidden keys only
            hidden = s.value("table/hidden", None)
            if hidden is None:
                hidden = [c.key for c in COLUMNS if not c.default]
            hidden = list(hidden) if isinstance(hidden, (list, tuple)) else [hidden]
            state = {"order": COLUMN_KEYS, "shown": [k for k in COLUMN_KEYS if k not in hidden]}
            s.remove("table/header")                 # index based; would misalign new columns
        self.apply_layout(state)

    def save_columns(self):
        import json
        s = QSettings()
        state = self.layout_state()
        s.setValue("table/layout", json.dumps(state))
        s.setValue("table/hidden", [k for k in COLUMN_KEYS if k not in state["shown"]])

    def choose_columns(self):
        from gcws.ui.dialogs.columns import ColumnChooserDialog
        cols = [(c.key, self.model.headerData(i, Qt.Horizontal) or c.header, c.tip) for i, c in enumerate(COLUMNS)]
        dlg = ColumnChooserDialog(cols, self.shown_keys(), [c.key for c in COLUMNS if c.default], self)
        if dlg.exec() == ColumnChooserDialog.Accepted:
            self._widths.update(self.layout_state()["widths"])
            self.set_shown(dlg.shown_keys())
            self.save_columns()

    def _toggle_column(self, i: int, on: bool) -> None:
        keys = self.shown_keys()
        key = COLUMNS[i].key
        if on and key not in keys:
            keys.append(key)
        elif not on:
            if len(keys) <= 1:
                return
            keys = [k for k in keys if k != key]
        self._widths.update(self.layout_state()["widths"])
        self.set_shown(keys)
        self.save_columns()

    def _header_menu(self, pos):
        m = QMenu(self)
        m.addAction("Choose columns...", self.choose_columns)
        m.addSeparator()
        for i, c in enumerate(COLUMNS):
            a = m.addAction(self.model.headerData(i, Qt.Horizontal))
            a.setCheckable(True)
            a.setChecked(not self.view.isColumnHidden(i))
            a.toggled.connect(lambda on, i=i: self._toggle_column(i, on))
        from PySide6.QtGui import QCursor
        m.exec(QCursor.pos())

    # -- export ------------------------------------------------------------------

    def _visible_columns(self):
        hh = self.view.horizontalHeader()
        cols = [hh.logicalIndex(v) for v in range(hh.count())]
        return [c for c in cols if not self.view.isColumnHidden(c)]

    def table_rows(self, only_selected=False):
        cols = self._visible_columns()
        rows = [[self.model.headerData(c, Qt.Horizontal) for c in cols]]
        prows = range(self.proxy.rowCount())
        sel = {i.row() for i in self.view.selectionModel().selectedRows()} if only_selected else None
        for r in prows:
            if sel is not None and r not in sel:
                continue
            rows.append([self.proxy.index(r, c).data() for c in cols])
        return rows

    def copy(self):
        rows = self.table_rows(only_selected=bool(self.view.selectionModel().selectedRows()))
        QGuiApplication.clipboard().setText("\n".join("\t".join(str(v) for v in r) for r in rows))

    def export(self):
        st = self.ws.active
        if st is None:
            return
        default = f"{st.name}_{self.ws.signal_key.replace(' ', '')}_peaks.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Export peak table", default,
                                              "Excel (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        rows = self.table_rows()
        if path.lower().endswith(".csv"):
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                csv.writer(fh, delimiter=";").writerows(rows)
        else:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "Peaks"
            ws.append([f"{st.name}  ({self.ws.signal_key})  exported {datetime.now():%Y-%m-%d %H:%M}"])
            for r in rows:
                ws.append([_num(v) for v in r])
            wb.save(path)
        self.ws.message.emit(f"Exported {len(rows) - 1} peaks to {path}")


def _num(v):
    if isinstance(v, str):
        t = v.replace(",", "")
        try:
            return float(t) if t and any(ch.isdigit() for ch in t) and t.replace(".", "", 1).lstrip("-").isdigit() else v
        except ValueError:
            return v
    return v
