"""Double determination at a glance.

Pick determination A and B (the partner is suggested from the run names),
press *Compare*: summary cards, a verdict per substance in plain language and
a mirror plot (A up, B down) show whether the two determinations agree. The
difference limit is the report parameter ``duplicate_max_reldiff``, so what
is flagged here is exactly what the report flags. Choosing A and B keeps the
replicate group in step, so the reports use the same pair.
"""
from __future__ import annotations

import copy
import uuid

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from gcws.core.model import FID
from gcws.quant import duplicate_view as DV
from gcws.ui import theme
from gcws.ui.icons import color_chip

ICON = {"ok": "✔", "warn": "⚠", "bad": "✖", "info": "ℹ", "neutral": "·"}


class _Card(QFrame):
    def __init__(self, caption: str, level: str):
        super().__init__()
        self.setObjectName("card")
        self.setProperty("level", level)
        self.value = QLabel("–")
        self.value.setStyleSheet(f"font-size:16pt; font-weight:600; color:{theme.status_color(level).name()};")
        cap = QLabel(caption)
        cap.setObjectName("hint")
        cap.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(0)
        lay.addWidget(self.value)
        lay.addWidget(cap)

    def set(self, text):
        self.value.setText(str(text))


class DuplicatePage(QWidget):
    reportRequested = QtSignal(str, str)        # kind, group id

    def __init__(self, ws, set_groups, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.set_groups = set_groups            # callable(groups, text): undoable group change
        self.rows: list[dict] = []
        self.verdicts: list = []
        self.members: list[str] = []
        self._loading = False

        self.a = QComboBox()
        self.b = QComboBox()
        for c in (self.a, self.b):
            c.setMinimumContentsLength(26)
            c.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.a.activated.connect(lambda *_: self._a_picked())
        self.b.activated.connect(lambda *_: self.compare(sync=True))
        swap = QToolButton()
        swap.setText("⇄")
        swap.setToolTip("Swap A and B")
        swap.clicked.connect(self._swap)
        self.b_compare = QPushButton("Compare")
        theme.set_primary(self.b_compare)
        self.b_compare.clicked.connect(lambda: self.compare(sync=True))
        more = QToolButton()
        more.setText("3+ determinations…")
        more.setToolTip("Triplicates and more: the Groups (N-fold) tab")
        more.clicked.connect(self._to_groups_tab)
        pick = QHBoxLayout()
        for w in (QLabel("A"), self.a, swap, QLabel("B"), self.b):
            pick.addWidget(w)
        pick.addWidget(self.b_compare)
        pick.addStretch(1)
        pick.addWidget(more)

        self.limit = QDoubleSpinBox()
        self.limit.setRange(0.0, 200.0)
        self.limit.setDecimals(1)
        self.limit.setSuffix(" %")
        self.limit.setToolTip("Maximum relative difference |A−B| / mean. This is the report parameter "
                              "'Duplicate difference limit': changing it changes the report too.")
        self.limit.editingFinished.connect(self._limit_changed)
        self.only_problems = QCheckBox("Only substances that need attention")
        self.only_problems.toggled.connect(lambda *_: self._fill_table())
        lim = QHBoxLayout()
        lim.addWidget(QLabel("Accept a difference up to"))
        lim.addWidget(self.limit)
        lim.addWidget(theme.hint("(report parameter)", False))
        lim.addStretch(1)
        lim.addWidget(self.only_problems)

        self.cards = {
            "confirmed": _Card("confirmed in both", "ok"),
            "deviating": _Card("difference above the limit", "warn"),
            "only_a": _Card("only in A (artefact)", "bad"),
            "only_b": _Card("only in B (artefact)", "bad"),
            "conflicts": _Card("identification differs", "bad"),
            "mean": _Card("mean difference", "accent"),
        }
        cards = QHBoxLayout()
        cards.setSpacing(6)
        for c in self.cards.values():
            cards.addWidget(c, 1)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setObjectName("chip")

        self.table = QTableWidget(0, 0)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._row_selected)
        self.table.cellDoubleClicked.connect(self._open_row)

        self.mirror = pg.PlotWidget()
        self.mirror.setMenuEnabled(False)
        self.mirror.showGrid(x=True, y=True, alpha=theme.PLOT["grid_alpha"])
        self.mirror.setLabel("bottom", "RT (FID)", units="min")
        self.mirror.setLabel("left", "A  ↑   normalised   ↓  B")
        self.mirror.getAxis("left").setWidth(52)
        self.cursor = pg.InfiniteLine(angle=90, movable=False,
                                      pen=pg.mkPen(theme.ACCENT, width=1, style=Qt.DashLine))
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.mirror)
        split.setSizes([320, 220])

        buttons = QHBoxLayout()
        for kind, label in (("nias", "NIAS report..."), ("fingerprint", "Fingerprint report..."),
                            ("total_extraction", "Total extraction report...")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=kind: self._report(k))
            buttons.addWidget(b)
        buttons.addStretch(1)
        exp = QPushButton("Export...")
        exp.clicked.connect(self.export)
        buttons.addWidget(exp)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)
        lay.addLayout(pick)
        lay.addLayout(lim)
        lay.addLayout(cards)
        lay.addWidget(self.banner)
        lay.addWidget(split, 1)
        lay.addLayout(buttons)

        ws.runAdded.connect(lambda *_: self.refresh_choices())
        ws.runRemoved.connect(lambda *_: self.refresh_choices())
        ws.runChanged.connect(lambda *_: self.refresh_choices())
        ws.quantChanged.connect(self._quant_changed)
        self.refresh_choices()

    # -- choices ---------------------------------------------------------------------

    def _to_groups_tab(self):
        p = self.parent()
        while p is not None and not hasattr(p, "tabs"):
            p = p.parent()
        if p is not None:
            p.tabs.setCurrentIndex(1)

    def _candidates(self):
        return [s for s in self.ws.states() if s.role in ("sample", "standard")]

    def refresh_choices(self):
        cur_a, cur_b = self.a.currentData(), self.b.currentData()
        self._loading = True
        for combo in (self.a, self.b):
            combo.clear()
            for st in self._candidates():
                combo.addItem(color_chip(st.color), st.name, st.id)
        self.b.addItem("— single determination —", "")
        self._loading = False
        ids = [s.id for s in self._candidates()]
        if cur_a in ids:
            self._select(self.a, cur_a)
            self._select(self.b, cur_b if (cur_b in ids or cur_b == "") else DV.suggest_partner(self.ws, cur_a))
        elif ids:
            self.set_pair(self._default_a())
        self.limit.blockSignals(True)
        self.limit.setValue(DV.limits(self.ws)[0])
        self.limit.blockSignals(False)

    def _default_a(self):
        act = self.ws.active
        if act is not None and act.role in ("sample", "standard"):
            return act.id
        c = self._candidates()
        return c[0].id if c else None

    @staticmethod
    def _select(combo, data):
        i = combo.findData(data if data is not None else "")
        combo.setCurrentIndex(max(0, i))

    def set_pair(self, a, b=None, compare=True):
        """Show the double determination of ``a`` (partner ``b`` or the suggested one)."""
        if a is None:
            return
        self._select(self.a, a)
        partner = b if b is not None else DV.suggest_partner(self.ws, a)
        self._select(self.b, partner or "")
        if compare:
            self.compare(sync=b is not None)

    def _a_picked(self):
        a = self.a.currentData()
        self._select(self.b, DV.suggest_partner(self.ws, a) or "")
        self.compare(sync=True)

    def _swap(self):
        a, b = self.a.currentData(), self.b.currentData()
        if not b:
            return
        self._select(self.a, b)
        self._select(self.b, a)
        self.compare(sync=True)

    # -- groups -------------------------------------------------------------------------

    def group(self):
        """The replicate group holding exactly the chosen determinations, if any."""
        want = [m for m in (self.a.currentData(), self.b.currentData()) if m]
        for g in self.ws.replicate_groups:
            if [m for m in g["members"] if m in self.ws.runs] == want:
                return g
        return None

    def _sync_group(self, members):
        if not members or self.group() is not None:
            return
        groups = copy.deepcopy(self.ws.replicate_groups)
        target = next((g for g in groups if members[0] in g["members"] and len(g["members"]) <= 2), None)
        if target is None:
            from gcws.io.sequence import replicate_stem
            name = replicate_stem(self.ws.runs[members[0]].run.path.name) or self.ws.runs[members[0]].name
            target = {"id": uuid.uuid4().hex[:8], "name": name, "members": [], "policy": "all"}
            groups.append(target)
        for g in groups:                        # a run belongs to one determination group
            if g is not target:
                g["members"] = [m for m in g["members"] if m not in members]
        groups = [g for g in groups if g["members"] or g is target]
        target["members"] = list(members)
        target["policy"] = "all"
        names = " / ".join(self.ws.runs[m].name for m in members)
        self.set_groups(groups, f"double determination {names}")

    # -- compute ---------------------------------------------------------------------------

    def _quant_changed(self):
        if self.isVisible() and self.members:
            self.compare(sync=False)
        self.limit.blockSignals(True)
        self.limit.setValue(DV.limits(self.ws)[0])
        self.limit.blockSignals(False)

    def labels(self):
        from gcws.io.sequence import replicate_label
        out = []
        for i, m in enumerate(self.members):
            lab = replicate_label(self.ws.runs[m].run.path.name) if m in self.ws.runs else ""
            out.append(lab if lab and lab not in out else "AB"[i] if i < 2 else str(i + 1))
        return tuple(out) if len(out) == 2 else ("A", "B")

    def compare(self, sync: bool = False):
        if self._loading:
            return
        a, b = self.a.currentData(), self.b.currentData()
        self.members = [m for m in (a, b) if m]
        if not self.members:
            self._show([], [], ["load the determinations and give them role Sample"])
            return
        if sync:
            self._sync_group(self.members)
        rows, problems = DV.compute(self.ws, self.members, "all")
        limit, rl = DV.limits(self.ws)
        labels = self.labels()
        unit = self.ws.quant_unit()
        verdicts = [DV.plain_verdict(r, limit, rl, labels, unit) for r in rows]
        self._show(rows, verdicts, problems)

    def _show(self, rows, verdicts, problems):
        self.rows, self.verdicts = rows, verdicts
        limit = DV.limits(self.ws)[0]
        labels = self.labels()
        if problems:
            for c in self.cards.values():
                c.set("–")
            self.banner.setText("Cannot compare yet: " + "; ".join(problems))
            self.banner.setProperty("level", "warn")
        else:
            s = DV.summarize(rows, verdicts, limit, labels)
            self.cards["confirmed"].set(s.confirmed)
            self.cards["deviating"].set(s.deviating)
            self.cards["only_a"].set(s.only_a)
            self.cards["only_b"].set(s.only_b)
            self.cards["conflicts"].set(s.conflicts)
            self.cards["mean"].set("–" if s.mean_reldiff is None else f"{s.mean_reldiff:.1f} %")
            self.banner.setText(("✔  " if s.level == "ok" else "⚠  ") + s.text if len(self.members) == 2 else
                                "Single determination: choose a partner B to compare.")
            self.banner.setProperty("level", s.level if len(self.members) == 2 else "neutral")
        self.cards["only_a"].findChild(QLabel, "hint").setText(f"only in {labels[0]} (artefact)")
        self.cards["only_b"].findChild(QLabel, "hint").setText(f"only in {labels[1]} (artefact)")
        theme._repolish(self.banner)
        self._fill_table()
        self._draw_mirror()

    def _fill_table(self):
        labels = self.labels()
        unit = self.ws.quant_unit()
        names = [self.ws.runs[m].name for m in self.members if m in self.ws.runs]
        headers = ["", "RT [min]", "Substance", "CAS", f"{labels[0]} [{unit}]", f"{labels[1]} [{unit}]",
                   f"Mean [{unit}]", "Diff. %", "Verdict", "Notes"]
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        for i, n in enumerate(names[:2]):
            self.table.horizontalHeaderItem(4 + i).setToolTip(n)
        self.table.setRowCount(0)
        for k, (row, v) in enumerate(zip(self.rows, self.verdicts)):
            if self.only_problems.isChecked() and v.level in ("ok", "neutral"):
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            notes = "; ".join(x for x in (DV.english(row.get("review", "")),) if x)
            vals = [ICON.get(v.level, ""), row.get("rt"), row.get("name", ""), row.get("cas", ""), row.get("c1"),
                    row.get("c2"), row.get("mean"), row.get("reldiff"), v.text, notes]
            for c, val in enumerate(vals):
                it = QTableWidgetItem()
                if isinstance(val, float):
                    it.setData(Qt.DisplayRole, round(val, {1: 3, 7: 1}.get(c, 4)))
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    it.setText("" if val is None else str(val))
                it.setData(Qt.UserRole, k)
                it.setToolTip(v.detail)
                if c in (0, 8):
                    it.setBackground(theme.status_brush(v.level))
                    it.setForeground(QBrush(theme.status_color(v.level)))
                self.table.setItem(r, c, it)
        self.table.resizeColumnsToContents()
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        self.table.setColumnWidth(2, min(260, max(140, self.table.columnWidth(2))))
        hh.setStretchLastSection(True)
        self.table.setSortingEnabled(True)

    # -- mirror plot -----------------------------------------------------------------------

    def _trace(self, rid):
        st = self.ws.runs.get(rid)
        sig = st.run.signal(FID) if st is not None else None
        if sig is None:
            return None
        from gcws.integration.autoparams import _integration_start
        t0 = _integration_start(sig.rt, self.ws.method_for(st, FID)) or float(sig.rt[0])
        sel = sig.y[sig.rt >= t0]
        if sel.size == 0:
            return None
        base = float(np.percentile(sel, 1))
        top = float(sel.max()) - base or 1.0
        return sig.rt, (sig.y - base) / top * 100.0, st.color

    def _draw_mirror(self):
        self.mirror.clear()
        self.mirror.addItem(self.cursor, ignoreBounds=True)
        traces = [self._trace(m) for m in self.members[:2]]
        for sign, tr in zip((1, -1), traces):
            if tr is None:
                continue
            rt, y, color = tr
            self.mirror.plot(rt, sign * y, pen=pg.mkPen(color, width=1.2))
        self.mirror.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(theme.BORDER_STRONG)), ignoreBounds=True)
        spots = []
        for row, v in zip(self.rows, self.verdicts):
            color = theme.status_color(v.level)
            pts = []
            for sign, key, tr in ((1, "source1", traces[0]), (-1, "source2", traces[1] if len(traces) > 1 else None)):
                src = row.get(key)
                if src is None or tr is None:
                    continue
                y = float(np.interp(src["rt"], tr[0], tr[1])) + 4.0
                pts.append((src["rt"], sign * min(y, 104.0)))
            for x, y in pts:
                spots.append({"pos": (x, y), "brush": pg.mkBrush(color), "pen": pg.mkPen(None), "size": 7})
            if len(pts) == 2:
                self.mirror.plot([p[0] for p in pts], [p[1] for p in pts], pen=pg.mkPen(color, width=0.8))
        if spots:
            self.mirror.addItem(pg.ScatterPlotItem(spots=spots))
        self.mirror.setYRange(-110, 110, padding=0)

    # -- navigation ----------------------------------------------------------------------------

    def _current_row(self):
        items = self.table.selectedItems()
        if not items:
            return None
        k = items[0].data(Qt.UserRole)
        return self.rows[k] if k is not None and 0 <= k < len(self.rows) else None

    def _row_selected(self):
        row = self._current_row()
        if row is None:
            return
        rt = row.get("rt")
        if rt is not None:
            self.cursor.setPos(rt)
            self.mirror.setXRange(rt - 0.4, rt + 0.4, padding=0)
            self._navigate(row, prefer_b=False)

    def _open_row(self, r, c):
        k = self.table.item(r, 0).data(Qt.UserRole)
        if k is not None:
            self._navigate(self.rows[k], prefer_b=c == 5)

    def _navigate(self, row, prefer_b=False):
        order = [("source2", 1), ("source1", 0)] if prefer_b else [("source1", 0), ("source2", 1)]
        for key, i in order:
            src = row.get(key)
            if src is None or i >= len(self.members):
                continue
            rid = self.members[i]
            self.ws.set_active(rid)
            if self.ws.signal_key != FID:
                self.ws.set_signal_key(FID)
            res = self.ws.result(rid, FID)
            if res is not None and res.peaks:
                j = min(range(len(res.peaks)), key=lambda q: abs(res.peaks[q].apex_rt - src["rt"]))
                if abs(res.peaks[j].apex_rt - src["rt"]) < 0.05:
                    self.ws.select_peak(j)
            return

    # -- output ------------------------------------------------------------------------------------

    def _limit_changed(self):
        from gcws.quant.nias_bridge import make_settings, settings_dict
        new = float(self.limit.value())
        if abs(new - DV.limits(self.ws)[0]) < 1e-9:
            return
        q = copy.deepcopy(self.ws.quant)
        s = make_settings(q.get("settings"))
        s.duplicate_max_reldiff = new
        q["settings"] = settings_dict(s)
        self.ws.push_quant(f"duplicate difference limit = {new:g} %", q)

    def _report(self, kind):
        self._sync_group(self.members)
        g = self.group()
        if g is not None:
            self.reportRequested.emit(kind, g["id"])

    def export(self):
        if not self.rows:
            return
        names = "_".join(self.ws.runs[m].name for m in self.members if m in self.ws.runs)[:80]
        path, _ = QFileDialog.getSaveFileName(self, "Export double determination", f"{names}_duplicate.xlsx",
                                              "Excel (*.xlsx)")
        if not path:
            return
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill
        wb = Workbook()
        sh = wb.active
        sh.title = "Double determination"
        labels = self.labels()
        unit = self.ws.quant_unit()
        sh.append([f"{labels[i]}: {self.ws.runs[m].name}" for i, m in enumerate(self.members) if m in self.ws.runs])
        sh.append(["Difference limit %", DV.limits(self.ws)[0]])
        sh.append([])
        sh.append(["RT [min]", "Substance", "CAS", f"{labels[0]} [{unit}]", f"{labels[1]} [{unit}]",
                   f"Mean [{unit}]", "Diff. %", "Verdict", "Explanation"])
        fills = {lvl: PatternFill("solid", fgColor=theme.LEVELS[lvl][1].lstrip("#")) for lvl in theme.LEVELS}
        for row, v in zip(self.rows, self.verdicts):
            sh.append([row.get("rt"), row.get("name"), row.get("cas"), row.get("c1"), row.get("c2"), row.get("mean"),
                       row.get("reldiff"), v.text, v.detail])
            sh.cell(sh.max_row, 8).fill = fills.get(v.level, fills["neutral"])
        wb.save(path)
        self.ws.message.emit(f"Double determination exported: {path}")
