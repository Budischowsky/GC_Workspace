"""Replicates / results: the list of every determination group beside its results.

On the left, every replicate group of the project (and the pairs the run names suggest, in italics)
with its number of determinations and how many red rows are still open. Selecting a pair or a single
determination opens the double-determination page; a group of three or more opens the worksheet of
its means. Ctrl+F3 goes to the next group that still needs a decision.
"""
from __future__ import annotations

import copy
import uuid

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QBrush, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QMenu, QScrollArea, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from gcws.quant import duplicate_view as DV
from gcws.quant.replicates import POLICIES
from gcws.ui import theme
from gcws.ui.undo import ValueCommand

#: list columns
L_NAME, L_N, L_OPEN = range(3)
PAIR_PAGE, GROUP_PAGE = range(2)


def _scrolled(widget: QWidget, vertical: bool = True) -> QScrollArea:
    """Wrap a page so the dock can be made narrow (the page scrolls instead). ``vertical=False``:
    only sideways, so a page with its own table and plots fills the height and never scrolls twice."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    if not vertical:
        area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setFrameShape(QScrollArea.NoFrame)
    area.setWidget(widget)
    return area


class ReplicatesDock(QWidget):
    """The list of determination groups, the double-determination page and the N-fold worksheet."""
    reportRequested = QtSignal(str, str)       # kind, group id
    previewRequested = QtSignal(str, str)      # kind, group id
    report2Requested = QtSignal()

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.rows = []
        self.summary: dict[str, tuple[int, int]] = {}      # group id -> (open red, yellow) of its last comparison
        self._group_id = None                              # the group shown on the N-fold page
        self._syncing = False
        from gcws.ui.docks.duplicate import DuplicatePage
        self.duplicate = DuplicatePage(ws, self._set_groups)
        self.duplicate.reportRequested.connect(self.reportRequested.emit)
        self.duplicate.previewRequested.connect(self.previewRequested.emit)
        self.duplicate.report2Requested.connect(self.report2Requested.emit)
        self.duplicate.summaryChanged.connect(self._pair_summary)

        # -- the list ------------------------------------------------------------------------
        self.list = QTreeWidget()
        self.list.setColumnCount(3)
        self.list.setHeaderLabels(["Name", "N", "Open"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.list.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(L_NAME, QHeaderView.Stretch)
        for c in (L_N, L_OPEN):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.list.headerItem().setToolTip(L_N, "Number of determinations")
        self.list.headerItem().setToolTip(L_OPEN, "Red rows still open at the last comparison (– not compared yet)")
        self.list.setToolTip("Every replicate group of the project; italics: suggested from the run names")
        self.list.currentItemChanged.connect(lambda cur, _prev: self._picked(cur))
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._group_menu)
        add = QToolButton()
        add.setText("+")
        add.setToolTip("Suggest groups from the run names, or make one by hand")
        add.setPopupMode(QToolButton.InstantPopup)
        am = QMenu(add)
        am.addAction("Suggest", self.suggest).setToolTip(
            "Group runs with the same sample number that differ only by _A/_B/_C")
        am.addAction("New...", self.new_group)
        add.setMenu(am)
        head = QHBoxLayout()
        head.addWidget(QLabel("Determinations"))
        head.addStretch(1)
        head.addWidget(add)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addLayout(head)
        ll.addWidget(self.list, 1)
        self.list.setHeaderHidden(False)

        # -- the N-fold worksheet ------------------------------------------------------------
        self.sheet = QTableWidget(0, 0)
        self.sheet.verticalHeader().setVisible(False)
        self.sheet.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sheet.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sheet.setSortingEnabled(True)
        self.sheet.itemSelectionChanged.connect(self._sheet_row)
        self.info = QLabel()
        self.info.setObjectName("hint")
        self.info.setWordWrap(True)
        from gcws.ui.widgets.report_button import report_button
        buttons = QHBoxLayout()
        self.b_report = report_button(self, lambda: self._preview(), lambda kind: self._report(kind),
                                      lambda: self.export())
        buttons.addWidget(self.b_report)
        buttons.addStretch(1)
        group_page = QWidget()
        gl = QVBoxLayout(group_page)
        gl.setContentsMargins(4, 4, 4, 4)
        gl.addWidget(self.info)
        gl.addWidget(self.sheet, 1)
        gl.addLayout(buttons)

        self.stack = QStackedWidget()
        self.stack.addWidget(_scrolled(self.duplicate, vertical=False))
        self.stack.addWidget(group_page)
        self.split = QSplitter()
        self.split.addWidget(left)
        self.split.addWidget(self.stack)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([200, 1000])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.split)
        QShortcut(QKeySequence("Ctrl+F3"), self, activated=self.next_open_group,
                  context=Qt.WidgetWithChildrenShortcut)

        ws.replicatesChanged.connect(self.refresh)
        ws.quantChanged.connect(lambda: self.refresh_sheet() if self.stack.currentIndex() == GROUP_PAGE else None)
        ws.runRemoved.connect(lambda *_: self.refresh())
        ws.runChanged.connect(lambda *_: self.refresh())
        self.refresh()

    # -- groups ----------------------------------------------------------------

    def _set_groups(self, groups, text):
        ws = self.ws

        def setter(v):
            ws.replicate_groups = copy.deepcopy(v)
            ws.replicatesChanged.emit()

        stack = ws.undo_group.activeStack() or ws.project_undo
        stack.push(ValueCommand(text, lambda: ws.replicate_groups, setter, groups,
                                lambda t, o, n: ws.log(t, "", "replicate groups")))

    def _group(self, gid):
        return next((g for g in self.ws.replicate_groups if g["id"] == gid), None) if gid else None

    def current_group(self):
        """The group shown: the N-fold group, else the pair of the double-determination page."""
        if self.stack.currentIndex() == GROUP_PAGE:
            return self._group(self._group_id)
        g = self.duplicate.group()
        if g is not None:
            return g
        it = self.list.currentItem()
        data = it.data(0, Qt.UserRole) if it is not None else None
        return self._group(data[1]) if data and data[0] == "group" else None

    def _members(self, g) -> list[str]:
        return [m for m in g["members"] if m in self.ws.runs]

    def _suggested(self) -> list[list[str]]:
        """Pairs and groups the run names suggest that are not a group yet."""
        from gcws.quant.grouping import for_workspace
        try:
            groups, _added = for_workspace(self.ws)
        except Exception:  # noqa: BLE001 - a suggestion must never break the panel
            return []
        have = {tuple(g["members"]) for g in self.ws.replicate_groups}
        taken = {m for g in self.ws.replicate_groups for m in g["members"]}
        return [g["members"] for g in groups if len(g["members"]) >= 2 and tuple(g["members"]) not in have
                and not taken & set(g["members"])]

    def refresh(self):
        """Rebuild the list; the selection stays on the same group."""
        cur = self.list.currentItem()
        keep = cur.data(0, Qt.UserRole) if cur is not None else None
        self._syncing = True
        try:
            self.list.clear()
            for g in self.ws.replicate_groups:
                names = [self.ws.runs[m].name for m in self._members(g)]
                it = QTreeWidgetItem([g["name"], str(len(names)), ""])
                it.setData(0, Qt.UserRole, ("group", g["id"]))
                it.setToolTip(L_NAME, "\n".join(names) or "no determination loaded")
                it.setTextAlignment(L_N, Qt.AlignCenter)
                it.setTextAlignment(L_OPEN, Qt.AlignCenter)
                self.list.addTopLevelItem(it)
                self._show_summary(it, g)
            font = QFont()
            font.setItalic(True)
            for members in self._suggested():
                names = [self.ws.runs[m].name for m in members]
                from gcws.quant.grouping import group_name
                it = QTreeWidgetItem([group_name(self.ws.runs[members[0]].run.path.stem), str(len(members)), ""])
                it.setData(0, Qt.UserRole, ("suggest", tuple(members)))
                it.setFont(L_NAME, font)
                it.setForeground(L_NAME, QBrush(theme.status_color("neutral")))
                it.setToolTip(L_NAME, "Suggested from the run names - select it to compare:\n" + "\n".join(names))
                it.setTextAlignment(L_N, Qt.AlignCenter)
                self.list.addTopLevelItem(it)
            target = self._item_for(keep) if keep else None
            if target is None:
                g = self.duplicate.group() if self.stack.currentIndex() == PAIR_PAGE else self._group(self._group_id)
                target = self._item_for(("group", g["id"])) if g else None
            if target is not None:
                self.list.setCurrentItem(target)
        finally:
            self._syncing = False
        if self.stack.currentIndex() == GROUP_PAGE:
            self.refresh_sheet()

    def _item_for(self, data):
        for i in range(self.list.topLevelItemCount()):
            it = self.list.topLevelItem(i)
            if it.data(0, Qt.UserRole) == data:
                return it
        return None

    def _show_summary(self, it, g) -> None:
        s = self.summary.get(g["id"])
        if s is None:
            it.setText(L_OPEN, "–")
            it.setForeground(L_OPEN, QBrush(theme.status_color("neutral")))
            it.setToolTip(L_OPEN, "Not compared yet")
            return
        n_open, n_yellow = s
        it.setText(L_OPEN, str(n_open) if n_open else "✔")
        it.setForeground(L_OPEN, QBrush(theme.status_color("bad" if n_open else "ok")))
        it.setToolTip(L_OPEN, f"{n_open} red row(s) open, {n_yellow} yellow" if n_open else
                      f"Nothing to decide; {n_yellow} yellow for a quick look")

    def _pair_summary(self) -> None:
        """The double-determination page compared or changed: keep its count and select its group."""
        g = self.duplicate.group()
        if g is None:
            return
        n_open, _decided = self.duplicate.decision_counts()
        self.summary[g["id"]] = (n_open, self.duplicate.counts.get("yellow", 0))
        it = self._item_for(("group", g["id"]))
        if it is None:
            return
        self._show_summary(it, g)
        if self.stack.currentIndex() == PAIR_PAGE and self.list.currentItem() is not it:
            self._syncing = True
            try:
                self.list.setCurrentItem(it)
            finally:
                self._syncing = False

    def _picked(self, it) -> None:
        """A list entry was selected: a pair or single on the double-determination page, else the worksheet."""
        if self._syncing or it is None:
            return
        kind, ref = it.data(0, Qt.UserRole)
        if kind == "suggest":
            self.show_pair(*ref[:2])               # comparing makes it a group (one undo step)
            return
        g = self._group(ref)
        if g is None:
            return
        members = self._members(g)
        if len(members) <= 2:
            if members:
                self.show_pair(members[0], members[1] if len(members) > 1 else "")
            return
        self.show_group(g["id"])

    def show_group(self, gid: str) -> None:
        """The worksheet of a group of three or more determinations."""
        self._group_id = gid
        self.stack.setCurrentIndex(GROUP_PAGE)
        it = self._item_for(("group", gid))
        if it is not None and self.list.currentItem() is not it:
            self._syncing = True
            try:
                self.list.setCurrentItem(it)
            finally:
                self._syncing = False
        self.refresh_sheet()

    def show_nfold(self) -> None:
        """Groups of three or more: the first such group (or the empty worksheet with a hint)."""
        g = next((g for g in self.ws.replicate_groups if len(self._members(g)) >= 3), None)
        if g is not None:
            self.show_group(g["id"])
        else:
            self._group_id = None
            self.stack.setCurrentIndex(GROUP_PAGE)
            self.refresh_sheet()

    def next_open_group(self) -> None:
        """Ctrl+F3: the next group after the selected one with open red rows or not compared yet."""
        n = self.list.topLevelItemCount()
        if not n:
            return
        cur = self.list.indexOfTopLevelItem(self.list.currentItem()) if self.list.currentItem() else -1
        for step in range(1, n + 1):
            it = self.list.topLevelItem((cur + step) % n)
            kind, ref = it.data(0, Qt.UserRole)
            s = self.summary.get(ref) if kind == "group" else None
            if s is None or s[0] > 0:
                self.list.setCurrentItem(it)
                return

    def suggest(self):
        from gcws.quant.grouping import for_workspace
        groups, added = for_workspace(self.ws)
        if added:
            self._set_groups(groups, f"suggest {added} replicate group(s)")

    def new_group(self):
        name, ok = QInputDialog.getText(self, "Replicate group", "Name:")
        if not ok or not name.strip():
            return
        act = self.ws.active
        members = [act.id] if act is not None and act.role in ("sample", "standard") else []
        group = {"id": uuid.uuid4().hex[:8], "name": name.strip(), "members": members, "policy": "all"}
        from gcws.ui.dialogs.replicates import MembersDialog
        dlg = MembersDialog(self.ws, group, self)
        if not dlg.exec() or not dlg.members():
            return                               # cancelled: no empty group is left behind
        group["members"] = dlg.members()
        groups = copy.deepcopy(self.ws.replicate_groups)
        groups.append(group)
        self._set_groups(groups, f"new replicate group {name.strip()}")
        it = self._item_for(("group", group["id"]))
        if it is not None:
            self.list.setCurrentItem(it)

    def _listed_group(self):
        it = self.list.currentItem()
        data = it.data(0, Qt.UserRole) if it is not None else None
        return self._group(data[1]) if data and data[0] == "group" else None

    def edit_members(self):
        g = self._listed_group()
        if g is None:
            return
        from gcws.ui.dialogs.replicates import MembersDialog
        dlg = MembersDialog(self.ws, g, self)
        if dlg.exec():
            groups = copy.deepcopy(self.ws.replicate_groups)
            for x in groups:
                if x["id"] == g["id"]:
                    x["members"] = dlg.members()
            self._set_groups(groups, f"members of {g['name']}")

    def delete_group(self):
        g = self._listed_group()
        if g is None:
            return
        groups = [x for x in copy.deepcopy(self.ws.replicate_groups) if x["id"] != g["id"]]
        self.summary.pop(g["id"], None)
        self._set_groups(groups, f"delete replicate group {g['name']}")

    def _group_menu(self, pos):
        it = self.list.itemAt(pos)
        if it is not None and it is not self.list.currentItem():
            self.list.setCurrentItem(it)
        m = QMenu(self)
        m.addAction("Suggest", self.suggest)
        m.addAction("New...", self.new_group)
        g = self._listed_group()
        if g is not None:
            m.addSeparator()
            m.addAction("Rename...", self._rename)
            m.addAction("Members...", self.edit_members)
            rule = m.addMenu("Validity rule")
            rule.setToolTip("When a peak counts as valid in a group of three or more")
            for key, text in POLICIES.items():
                a = rule.addAction(text, lambda k=key: self.set_policy(k))
                a.setCheckable(True)
                a.setChecked(g.get("policy", "all") == key)
            m.addAction("Delete", self.delete_group)
        m.exec(self.list.viewport().mapToGlobal(pos))

    def _rename(self):
        g = self._listed_group()
        if g is None:
            return
        name, ok = QInputDialog.getText(self, "Rename", "Name:", text=g["name"])
        if ok and name.strip():
            groups = copy.deepcopy(self.ws.replicate_groups)
            for x in groups:
                if x["id"] == g["id"]:
                    x["name"] = name.strip()
            self._set_groups(groups, f"rename group to {name.strip()}")

    def set_policy(self, key: str) -> None:
        """The validity rule of the selected group (one undo step)."""
        g = self._listed_group()
        if g is None or key not in POLICIES or g.get("policy", "all") == key:
            return
        groups = copy.deepcopy(self.ws.replicate_groups)
        for x in groups:
            if x["id"] == g["id"]:
                x["policy"] = key
        self._set_groups(groups, f"validity rule of {g['name']}")

    # -- worksheet -------------------------------------------------------------------

    def compute(self, g):
        members = self._members(g)
        rows, problems = DV.compute(self.ws, members, g.get("policy", "all"))
        return members, rows, ("Cannot compute: " + "; ".join(problems)) if problems else ""

    def refresh_sheet(self):
        g = self._group(self._group_id)
        self.sheet.setSortingEnabled(False)
        self.sheet.setRowCount(0)
        self.rows = []
        if g is None:
            self.sheet.setColumnCount(0)
            self.info.setText("Groups of three or more determinations show their means here. Make one with + > "
                              "Suggest or + > New... in the list.")
            return
        if self.ws.quant_result is None:
            self.ws.recompute_quant()
        members, rows, err = self.compute(g)
        self.rows = rows
        n = len(members)
        unit = self.ws.quant_unit()
        from gcws.io.sequence import replicate_label
        letters = []
        for k, m in enumerate(members):
            lab = replicate_label(self.ws.runs[m].run.path.name)
            letters.append(lab if lab and lab not in letters else chr(65 + k))
        headers = ["RT", "Name", "CAS", f"Mean [{unit}]"] + [f"{letters[k]} [{unit}]" for k in range(n)] + \
                  (["Rel. diff %"] if n == 2 else ["SD", "RSD %"]) + ["Status", "ID status", "Review"]
        self.sheet.setColumnCount(len(headers))
        self.sheet.setHorizontalHeaderLabels(headers)
        names = [self.ws.runs[m].name for m in members]
        rule = POLICIES.get(g.get("policy", "all"), "")
        self.info.setText(err or f"{g['name']}: {n} determination(s) - " + ", ".join(names)
                          + (f". Valid when: {rule.lower()}." if rule else ""))
        for row_index, r in enumerate(rows):
            i = self.sheet.rowCount()
            self.sheet.insertRow(i)
            cs = r.get("cs") or []
            vals = [r["rt"], r["name"], r["cas"], r["mean"]] + [cs[k] if k < len(cs) else None for k in range(n)]
            vals += ([r.get("reldiff")] if n == 2 else [r.get("sd"), r.get("rsd")])
            vals += [DV.english(r["status"]), DV.english(r["id_status"]), DV.english(r["review"])]
            for c, v in enumerate(vals):
                it = QTableWidgetItem()
                if isinstance(v, float):
                    it.setData(Qt.DisplayRole, round(v, 6) if c else round(v, 4))
                elif v is None:
                    it.setText("")
                else:
                    it.setText(str(v))
                status = r["status"]
                level = ("bad" if "conflict" in status.lower() else "neutral" if status.startswith("Artefact")
                         else "ok" if status.startswith("Valid") else None)
                if level:
                    it.setBackground(theme.status_brush(level))
                it.setData(Qt.UserRole, row_index)
                self.sheet.setItem(i, c, it)
        self.sheet.resizeColumnsToContents()
        self.sheet.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.sheet.setColumnWidth(1, 260)
        self.sheet.setSortingEnabled(True)

    def _sheet_row(self):
        items = self.sheet.selectedItems()
        g = self._group(self._group_id)
        if not items or g is None:
            return
        k = items[0].data(Qt.UserRole)
        if k is None or not (0 <= k < len(self.rows)):
            return
        self.duplicate.members = self._members(g)
        self.duplicate._navigate(self.rows[k])

    def show_pair(self, a, b=None, processed=False):
        """Open the double-determination page for run ``a`` (and partner ``b``)."""
        self.stack.setCurrentIndex(PAIR_PAGE)
        self.duplicate.refresh_choices()
        self.duplicate.set_pair(a, b, processed=processed)

    def export(self):
        g = self._group(self._group_id)
        if g is None or not self.rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export worksheet", f"{g['name']}_replicates.xlsx",
                                              "Excel (*.xlsx)")
        if not path:
            return
        from openpyxl import Workbook
        from gcws.core.text import excel_safe
        wb = Workbook()
        sh = wb.active
        sh.title = "Replicates"
        sh.append([self.sheet.horizontalHeaderItem(c).text() for c in range(self.sheet.columnCount())])
        for r in range(self.sheet.rowCount()):
            row = []
            for c in range(self.sheet.columnCount()):
                it = self.sheet.item(r, c)
                v = it.data(Qt.DisplayRole) if it else None
                row.append(excel_safe(v))
            sh.append(row)
        wb.save(path)
        self.ws.message.emit(f"Worksheet exported: {path}")

    def _report(self, kind):
        g = self.current_group()
        if g is not None:
            self.reportRequested.emit(kind, g["id"])

    def _preview(self):
        """The report that fits the quantification (HS-Screening on the TIC, else NIAS), as a preview."""
        from gcws.quant.service import quant_detector
        g = self.current_group()
        if g is not None:
            self.previewRequested.emit("hs_screening" if quant_detector(self.ws.quant) == "TIC" else "nias",
                                       g["id"])
