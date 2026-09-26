"""Replicate groups and the summary worksheet (means of the determinations)."""
from __future__ import annotations

import copy
import uuid

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from gcws.quant import duplicate_view as DV
from gcws.quant.replicates import POLICIES
from gcws.ui import theme
from gcws.ui.undo import ValueCommand


def _scrolled(widget: QWidget) -> QScrollArea:
    """Wrap a page so the dock can be made narrow (the page scrolls instead)."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.NoFrame)
    area.setWidget(widget)
    return area


class ReplicatesDock(QWidget):
    """Double determination (A/B) page and the general N-fold replicate groups."""
    reportRequested = QtSignal(str, str)       # kind, group id
    previewRequested = QtSignal(str, str)      # kind, group id

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.rows = []
        from gcws.ui.docks.duplicate import DuplicatePage
        self.duplicate = DuplicatePage(ws, self._set_groups)
        self.duplicate.reportRequested.connect(self.reportRequested.emit)
        self.duplicate.previewRequested.connect(self.previewRequested.emit)
        self.groups = QListWidget()
        self.groups.setToolTip("Replicate groups: the determinations of one sample")
        self.groups.currentRowChanged.connect(lambda *_: self.refresh_sheet())
        self.groups.setContextMenuPolicy(Qt.CustomContextMenu)
        self.groups.customContextMenuRequested.connect(self._group_menu)
        suggest = QPushButton("Suggest")
        suggest.setToolTip("Group runs with the same sample number that differ only by _A/_B/_C")
        suggest.clicked.connect(self.suggest)
        new = QPushButton("New...")
        new.clicked.connect(self.new_group)
        edit = QPushButton("Members...")
        edit.clicked.connect(self.edit_members)
        delete = QPushButton("Delete")
        delete.clicked.connect(self.delete_group)
        self.policy = QComboBox()
        for k, v in POLICIES.items():
            self.policy.addItem(v, k)
        self.policy.activated.connect(self._policy_changed)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Groups"))
        ll.addWidget(self.groups, 1)
        h = QHBoxLayout()
        for b in (suggest, new, edit, delete):
            h.addWidget(b)
        ll.addLayout(h)
        ll.addWidget(QLabel("A peak counts as valid when"))
        ll.addWidget(self.policy)

        self.sheet = QTableWidget(0, 0)
        self.sheet.verticalHeader().setVisible(False)
        self.sheet.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sheet.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sheet.setSortingEnabled(True)
        self.sheet.itemSelectionChanged.connect(self._sheet_row)
        self.info = QLabel()
        self.info.setObjectName("hint")
        self.info.setWordWrap(True)
        buttons = QHBoxLayout()
        for kind, label in (("nias", "NIAS report..."), ("fingerprint", "Fingerprint report..."),
                            ("total_extraction", "Total extraction report...")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=kind: self._report(k))
            buttons.addWidget(b)
        exp = QPushButton("Export worksheet...")
        exp.clicked.connect(self.export)
        buttons.addStretch(1)
        buttons.addWidget(exp)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.info)
        rl.addWidget(self.sheet, 1)
        rl.addLayout(buttons)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([260, 900])
        groups_page = QWidget()
        gl = QVBoxLayout(groups_page)
        gl.setContentsMargins(2, 2, 2, 2)
        gl.addWidget(split)
        self.tabs = QTabWidget()
        self.tabs.addTab(_scrolled(self.duplicate), "Double determination")
        self.tabs.addTab(_scrolled(groups_page), "Groups (N-fold)")
        self.tabs.currentChanged.connect(lambda i: self.duplicate.compare() if i == 0 else self.refresh_sheet())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.tabs)

        ws.replicatesChanged.connect(self.refresh)
        ws.quantChanged.connect(self.refresh_sheet)
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

    def current_group(self):
        i = self.groups.currentRow()
        return self.ws.replicate_groups[i] if 0 <= i < len(self.ws.replicate_groups) else None

    def refresh(self):
        cur = self.groups.currentRow()
        self.groups.blockSignals(True)
        self.groups.clear()
        for g in self.ws.replicate_groups:
            names = [self.ws.runs[m].name for m in g["members"] if m in self.ws.runs]
            it = QListWidgetItem(f"{g['name']}  ({len(names)})")
            it.setToolTip("\n".join(names))
            self.groups.addItem(it)
        self.groups.blockSignals(False)
        if self.ws.replicate_groups:
            self.groups.setCurrentRow(min(max(cur, 0), len(self.ws.replicate_groups) - 1))
        self.refresh_sheet()

    def suggest(self):
        from gcws.io.sequence import sample_number, replicate_stem
        groups = copy.deepcopy(self.ws.replicate_groups)
        taken = {m for g in groups for m in g["members"]}
        order = self.ws.ordered_ids_by_injection()
        added = 0
        for ids in self.ws.suggest_replicate_groups():
            # a run waiting alone in a single-determination group joins its partner
            if any(len(g["members"]) == 1 and g["members"][0] in ids for g in groups):
                groups = [g for g in groups if not (len(g["members"]) == 1 and g["members"][0] in ids)]
                taken = {m for g in groups for m in g["members"]}
            ids = [i for i in order if i in ids and i not in taken]
            if len(ids) < 2:
                continue
            import re
            first = self.ws.runs[ids[0]].run.path.stem
            name = re.sub(r"^\d+[_\- ]+", "", first)            # injection prefix
            name = re.sub(r"[_\- ]+([A-Za-z]|\d{1,2})$", "", name)  # replicate letter
            groups.append({"id": uuid.uuid4().hex[:8], "name": name, "members": ids, "policy": "all"})
            taken.update(ids)
            added += 1
        for st in self.ws.states():                  # samples without partner: single determination
            if st.role == "sample" and st.id not in taken:
                groups.append({"id": uuid.uuid4().hex[:8], "name": st.name, "members": [st.id], "policy": "all"})
                added += 1
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
        self.groups.setCurrentRow(len(groups) - 1)

    def edit_members(self):
        g = self.current_group()
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
        g = self.current_group()
        if g is None:
            return
        groups = [x for x in copy.deepcopy(self.ws.replicate_groups) if x["id"] != g["id"]]
        self._set_groups(groups, f"delete replicate group {g['name']}")

    def _group_menu(self, pos):
        g = self.current_group()
        if g is None:
            return
        m = QMenu(self)
        m.addAction("Rename...", self._rename)
        m.addAction("Members...", self.edit_members)
        m.addAction("Delete", self.delete_group)
        m.exec(self.groups.mapToGlobal(pos))

    def _rename(self):
        g = self.current_group()
        name, ok = QInputDialog.getText(self, "Rename", "Name:", text=g["name"])
        if ok and name.strip():
            groups = copy.deepcopy(self.ws.replicate_groups)
            for x in groups:
                if x["id"] == g["id"]:
                    x["name"] = name.strip()
            self._set_groups(groups, f"rename group to {name.strip()}")

    def _policy_changed(self, *_):
        g = self.current_group()
        if g is None:
            return
        groups = copy.deepcopy(self.ws.replicate_groups)
        for x in groups:
            if x["id"] == g["id"]:
                x["policy"] = self.policy.currentData()
        self._set_groups(groups, f"validity rule of {g['name']}")

    # -- worksheet -------------------------------------------------------------------

    def compute(self, g):
        members = [m for m in g["members"] if m in self.ws.runs]
        rows, problems = DV.compute(self.ws, members, g.get("policy", "all"))
        return members, rows, ("Cannot compute: " + "; ".join(problems)) if problems else ""

    def refresh_sheet(self):
        g = self.current_group()
        self.sheet.setSortingEnabled(False)
        self.sheet.setRowCount(0)
        if g is None:
            self.sheet.setColumnCount(0)
            self.info.setText("Create replicate groups (Suggest) to see the averaged results.")
            return
        self.policy.setCurrentIndex(max(0, self.policy.findData(g.get("policy", "all"))))
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
        self.info.setText(err or f"{g['name']}: {n} determination(s) - " + ", ".join(names))
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
        g = self.current_group()
        if not items or g is None:
            return
        k = items[0].data(Qt.UserRole)
        if k is None or not (0 <= k < len(self.rows)):
            return
        self.duplicate.members = [m for m in g["members"] if m in self.ws.runs]
        self.duplicate._navigate(self.rows[k])

    def show_pair(self, a, b=None):
        """Open the double-determination tab for run ``a`` (and partner ``b``)."""
        self.tabs.setCurrentIndex(0)
        self.duplicate.refresh_choices()
        self.duplicate.set_pair(a, b)

    def export(self):
        g = self.current_group()
        if g is None or not self.rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export worksheet", f"{g['name']}_replicates.xlsx",
                                              "Excel (*.xlsx)")
        if not path:
            return
        from openpyxl import Workbook
        wb = Workbook()
        sh = wb.active
        sh.title = "Replicates"
        sh.append([self.sheet.horizontalHeaderItem(c).text() for c in range(self.sheet.columnCount())])
        for r in range(self.sheet.rowCount()):
            row = []
            for c in range(self.sheet.columnCount()):
                it = self.sheet.item(r, c)
                v = it.data(Qt.DisplayRole) if it else None
                row.append(v)
            sh.append(row)
        wb.save(path)
        self.ws.message.emit(f"Worksheet exported: {path}")

    def _report(self, kind):
        g = self.current_group()
        if g is not None:
            self.reportRequested.emit(kind, g["id"])
