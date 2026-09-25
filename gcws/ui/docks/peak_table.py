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
from gcws.ui.models.peak_table import COLUMNS, PeakTableModel
from gcws.ui.undo import IdentCommand


class SortProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(Qt.UserRole)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterKeyColumn(-1)

    def lessThan(self, a, b):
        va, vb = a.data(Qt.UserRole), b.data(Qt.UserRole)
        try:
            return va < vb
        except TypeError:
            return str(va) < str(vb)


class PeakTable(QWidget):
    searchRequested = QtSignal()
    integrateRequested = QtSignal(bool)       # all runs?

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
        tb.setIconSize(tb.iconSize() * 0.8)
        a = tb.addAction(icon("integrate"), "Integrate")
        a.setToolTip("Re-integrate the active chromatogram with its method")
        a.triggered.connect(lambda: self.integrateRequested.emit(False))
        a = tb.addAction(icon("integrate", "#8e44ad"), "Integrate all")
        a.setToolTip("Re-integrate all loaded chromatograms")
        a.triggered.connect(lambda: self.integrateRequested.emit(True))
        tb.addSeparator()
        self.search_action = tb.addAction(icon("search"), "Library search")
        self.search_action.setToolTip("Automatic library search (EI Atlas) for all peaks")
        self.search_action.triggered.connect(self.searchRequested.emit)
        tb.addSeparator()
        exp = tb.addAction("Export...")
        exp.triggered.connect(self.export)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter...")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)
        self.info = QLabel()
        self.info.setObjectName("hint")
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(tb)
        top.addStretch(1)
        top.addWidget(self.info)
        top.addWidget(self.filter)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        lay.addLayout(top)
        lay.addWidget(self.view, 1)

        copy = QAction("Copy", self.view)
        copy.setShortcut(QKeySequence.Copy)
        copy.setShortcutContext(Qt.WidgetShortcut)
        copy.triggered.connect(self.copy)
        self.view.addAction(copy)

        self._restore_columns()
        self._syncing = False
        for sig in (ws.activeRunChanged, ws.signalKeyChanged, ws.identsChanged, ws.quantChanged):
            sig.connect(lambda *_: self.reload())
        ws.resultChanged.connect(self._on_result)
        ws.selectionChanged.connect(self._on_selection)
        ws.runRemoved.connect(lambda *_: self.reload())
        self.reload()

    # -- data ----------------------------------------------------------------

    def _on_result(self, run_id, key):
        if run_id == self.ws.active_id and key == self.ws.signal_key:
            self.reload()

    def reload(self):
        self._syncing = True
        self.model.reload()
        self._syncing = False
        n = len(self.model.rows)
        idn = sum(1 for r in self.model.rows if r.ident and r.ident.name and
                  not r.ident.name.lower().startswith("unknown"))
        extra = f"  •  {len(self.model.orphans)} orphaned IDs" if self.model.orphans else ""
        self.info.setText(f"{n} peaks  •  {idn} identified{extra}")
        self._on_selection(self.ws.active_id, self.ws.selected)

    def _row_changed(self, current, previous):
        if self._syncing or not current.isValid():
            return
        src = self.proxy.mapToSource(current)
        if src.row() != self.ws.selected:
            self.ws.select_peak(src.row())

    def _on_selection(self, run_id, index):
        if index < 0 or index >= self.model.rowCount():
            return
        src = self.model.index(index, 0)
        prox = self.proxy.mapFromSource(src)
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
        m.addAction("Choose columns...").triggered.connect(lambda: self._header_menu(None))
        m.exec(self.view.viewport().mapToGlobal(pos))

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

    def _restore_columns(self):
        s = QSettings()
        hidden = s.value("table/hidden", None)
        if hidden is None:
            hidden = [c.key for c in COLUMNS if not c.default]
        hidden = list(hidden) if isinstance(hidden, (list, tuple)) else [hidden]
        for i, c in enumerate(COLUMNS):
            self.view.setColumnHidden(i, c.key in hidden)
        state = s.value("table/header")
        if state is not None:
            self.view.horizontalHeader().restoreState(state)
            for i, c in enumerate(COLUMNS):
                self.view.setColumnHidden(i, c.key in hidden)
        else:
            for i, c in enumerate(COLUMNS):
                self.view.setColumnWidth(i, 220 if c.key == "name" else (90 if c.key == "cas" else 70))

    def save_columns(self):
        s = QSettings()
        s.setValue("table/hidden", [c.key for i, c in enumerate(COLUMNS) if self.view.isColumnHidden(i)])
        s.setValue("table/header", self.view.horizontalHeader().saveState())

    def _header_menu(self, pos):
        m = QMenu(self)
        for i, c in enumerate(COLUMNS):
            a = m.addAction(self.model.headerData(i, Qt.Horizontal))
            a.setCheckable(True)
            a.setChecked(not self.view.isColumnHidden(i))
            a.toggled.connect(lambda on, i=i: (self.view.setColumnHidden(i, not on), self.save_columns()))
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
