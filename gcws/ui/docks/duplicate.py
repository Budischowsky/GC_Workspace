"""Double determination at a glance.

Pick determination A and B (the partner is suggested from the run names),
press *Compare*: chips counting the colours, a verdict per substance in plain language and
a mirror plot (A up, B down) show whether the two determinations agree. The
difference limit is the report parameter ``duplicate_max_reldiff``, so what
is flagged here is exactly what the report flags. Choosing A and B keeps the
replicate group in step, so the reports use the same pair.

The analyst decides what goes out: the *Report* box of each substance (default:
AutoLib's rule), the name and CAS (they become the identification of the peak
in both determinations), areas, concentrations and the mean, and a comment.
Every change is undoable, marked in the table and written to the audit trail;
the NIAS report uses exactly these rows and values.

With the feature pairing (``gcws.features``, the default) *Compare* pairs the peaks
by retention time and spectrum, fills gaps and sets one name per substance
automatically (one undo step), and every row gets a traffic light: green is
taken over, yellow was made consistent automatically, red needs the analyst.
The chips filter by colour and F3 (next open red) leads through the exceptions; the right-click menu
chooses a candidate name, removes a gap fill or takes over harmonised
boundaries.
"""
from __future__ import annotations

import copy
import uuid

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QTimer, Qt, Signal as QtSignal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QMenu, QPushButton, QSplitter,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from gcws.core.model import FID
from gcws.quant import duplicate_view as DV
from gcws.ui import theme, workers
from gcws.ui.icons import color_chip
from gcws.ui.widgets.cell_marks import EDITED_ROLE, EditedDelegate
from gcws.ui.widgets.chips import Chip, ElidedLabel

#: SNIP window (min) of the baseline removed from the mirror plot's traces: wider than any peak
BASELINE_WINDOW = 1.0
#: wider views (min) of the mirror plot scale to the substances without the internal standards
WIDE_VIEW = 3.0
WIDE_PERCENTILE = 90
KEYS_NOTE = ("Enter: report · Delete: not reported · type or F2: edit · Ctrl+C / Ctrl+V · Ctrl+D or drag the "
             "small square of the marking: copy down. Changes are marked, undoable and logged.")
ICON = {"ok": "✔", "warn": "⚠", "bad": "✖", "info": "ℹ", "neutral": "·", "decided": "◉"}
#: icon cell: True on a red row that still waits for the analyst (F3 goes there)
OPEN_ROLE = Qt.UserRole + 1
#: table column -> edited field
C_ICON, C_REPORT, C_RT, C_NAME, C_CAS, C_A1, C_A2, C_C1, C_C2, C_MEAN, C_DIFF, C_VERDICT, C_NOTES, C_COMMENT = range(14)
#: feature pairing only (appended, so the columns above keep their places)
C_FEATURE, C_SIM, C_HIT_A, C_HIT_B = range(14, 18)
#: the filter chips: key -> (label, level, tooltip)
FILTERS = {"all": ("All", "info", "Every substance"),
           "check": ("To check", "accent", "Red and yellow rows and the rows you changed"),
           "red": ("Red", "bad", "Your decision: found in one determination only, spectra differ, difference too "
                                 "large. F3: next open red row."),
           "yellow": ("Yellow", "warn", "Made consistent automatically: a quick look"),
           "green": ("Green", "ok", "Confirmed in both determinations"),
           "grey": ("Grey", "neutral", "Not reported anyway: below the reporting limit or at blank level")}
FIELD_OF = {C_REPORT: "report", C_NAME: "name", C_CAS: "cas", C_A1: "a1", C_A2: "a2", C_C1: "c1", C_C2: "c2",
            C_MEAN: "mean", C_COMMENT: "comment"}


class _AbsAxis(pg.AxisItem):
    """Intensity axis of the mirror plot: B is drawn downwards but has positive values too."""

    def tickStrings(self, values, scale, spacing):
        return super().tickStrings([abs(v) for v in values], scale, spacing)


class DuplicatePage(QWidget):
    reportRequested = QtSignal(str, str)        # kind, group id
    previewRequested = QtSignal(str, str)       # kind, group id
    report2Requested = QtSignal()

    def __init__(self, ws, set_groups, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.set_groups = set_groups            # callable(groups, text): undoable group change
        self.rows: list[dict] = []               # with the analyst's edits applied
        self.base_rows: list[dict] = []          # as merged from the integrations
        self.verdicts: list = []
        self._filling = False
        self.members: list[str] = []
        self._loading = False
        self._view_processed = False

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
        self.b_report2 = QPushButton("Load from Report²…")
        self.b_report2.setToolTip("Switch to a processed double determination shown in Report²")
        self.b_report2.clicked.connect(self.report2Requested.emit)
        more = QToolButton()
        more.setText("3+ determinations…")
        more.setToolTip("Triplicates and more: the Groups (N-fold) tab")
        more.clicked.connect(self._to_groups_tab)
        # Compare and Report² first: they stay in view in a narrow dock (choosing A or B compares anyway)
        pick = QHBoxLayout()
        for w in (self.b_compare, self.b_report2, QLabel("A"), self.a, swap, QLabel("B"), self.b):
            pick.addWidget(w)
        pick.addStretch(1)
        pick.addWidget(more)

        self.limit = QDoubleSpinBox()
        self.limit.setRange(0.0, 200.0)
        self.limit.setDecimals(1)
        self.limit.setSuffix(" %")
        self.limit.setToolTip("Maximum relative difference |A−B| / mean. This is the report parameter "
                              "'Duplicate difference limit': changing it changes the report too.")
        self.limit.editingFinished.connect(self._limit_changed)
        self.b_settings = QToolButton()
        self.b_settings.setText("Settings…")
        self.b_settings.setToolTip("Pairing (features or classic), gap filling, consensus name")
        self.b_settings.clicked.connect(self.edit_settings)
        # the chips count the rows of each colour and filter the list (clicked again: all rows)
        self.filter = "all"
        self.counts: dict[str, int] = {}
        self.chips: dict[str, Chip] = {}
        lim = QHBoxLayout()
        lim.setSpacing(4)
        for key, (label, level, tip) in FILTERS.items():
            chip = Chip()
            chip.setToolTip(tip)
            chip.clicked.connect(lambda k=key: self.set_filter(k))
            theme.set_chip(chip, label, level)
            self.chips[key] = chip
            lim.addWidget(chip)
        lim.addStretch(1)
        lim.addWidget(QLabel("Accept a difference up to"))
        lim.addWidget(self.limit)
        lim.addWidget(theme.hint("(report parameter)", False))
        lim.addWidget(self.b_settings)

        self.banner = ElidedLabel()               # one line; the whole text is its tooltip
        self.banner.setObjectName("chip")

        from gcws.ui.widgets.sheet_table import SheetTable
        self.table = SheetTable(0, 0)            # Excel-like: keys, Ctrl+C/V/D, fill handle
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._cell_edited)
        self.table.markRequested.connect(self._mark_rows)
        self.table.bulkEdit.connect(self._bulk_edit)
        self.table.setAlternatingRowColors(True)
        self.table.setItemDelegate(EditedDelegate(self.table))     # changed cells: a corner mark
        self._nav = QTimer(self)                 # arrow keys: jump to the peak once the cursor rests
        self._nav.setSingleShot(True)
        self._nav.setInterval(150)
        self._nav.timeout.connect(self._row_selected)
        self.table.currentItemChanged.connect(lambda *_: self._nav.start())
        self.table.cellDoubleClicked.connect(self._open_row)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        QShortcut(QKeySequence(Qt.Key_F3), self, activated=self.next_red)
        self.table_features = None                   # the FeatureTable of the rows (feature pairing)
        self._search = None                          # the running consensus search
        self._comparing = False
        self._refresh = QTimer(self)                 # a comparison asked for while one is running
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(0)
        self._refresh.timeout.connect(self._refresh_now)

        self.mirror = pg.PlotWidget(axisItems={"left": _AbsAxis("left")})
        self.mirror.setMenuEnabled(False)
        self.mirror.showGrid(x=True, y=True, alpha=theme.PLOT["grid_alpha"])
        self.mirror.setLabel("bottom", "RT (FID)", units="min")
        self.mirror.setLabel("left", "A  ↑   FID   ↓  B")
        self.mirror.getAxis("left").setWidth(62)
        self.mirror.getAxis("bottom").enableAutoSIPrefix(False)
        self._traces, self._marks = [], []
        vb = self.mirror.getViewBox()
        vb.setMouseEnabled(x=True, y=False)          # wheel / drag: time only, the intensity follows
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._fit_y)
        vb.sigXRangeChanged.connect(lambda *_: self._fit_timer.start())
        theme.register_plot(self.mirror, lambda: self._draw_mirror() if self.members else None)
        self.mirror.scene().sigMouseClicked.connect(lambda ev: self.full_view() if ev.double() else None)
        self.mirror.setToolTip("A up, B down, in FID signal units. Wheel or drag: time; double-click: "
                               "whole chromatogram")
        self.cursor = pg.InfiniteLine(angle=90, movable=False,
                                      pen=pg.mkPen(theme.ACCENT, width=1, style=Qt.DashLine))
        self.spec = pg.PlotWidget(axisItems={"left": _AbsAxis("left")})
        self.spec.setMenuEnabled(False)
        self.spec.showGrid(x=False, y=True, alpha=theme.PLOT["grid_alpha"])
        self.spec.setLabel("bottom", "m/z")
        self.spec.setLabel("left", "A  ↑   %   ↓  B")
        self.spec.getAxis("left").setWidth(62)
        self.spec.setToolTip("The spectra of the selected substance: A up, B down (co-eluting ions, "
                             "background subtracted), each scaled to its base peak")
        theme.register_plot(self.spec, lambda: self._draw_spectra())
        plots = QSplitter(Qt.Horizontal)
        plots.addWidget(self.mirror)
        plots.addWidget(self.spec)
        plots.setSizes([520, 300])
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(plots)
        split.setSizes([320, 220])

        buttons = QHBoxLayout()
        prev = QPushButton("NIAS report - preview")
        theme.set_primary(prev)
        prev.setToolTip("The NIAS report of this double determination with the rows and values chosen here")
        prev.clicked.connect(lambda: self._report("hs_screening" if self.quant_signal() == "TIC" else "nias", preview=True))
        buttons.addWidget(prev)
        for kind, label in (("nias", "NIAS report..."), ("fingerprint", "Fingerprint report..."),
                            ("total_extraction", "Total extraction report..."), ("hs_screening", "HS-Screening report...")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=kind: self._report(k))
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.b_bounds = QPushButton("Harmonise boundaries")
        self.b_bounds.setToolTip("Take over every proposed integration boundary (one undo step)")
        self.b_bounds.clicked.connect(lambda: self.apply_boundaries(None))
        self.b_bounds.setVisible(False)
        buttons.addWidget(self.b_bounds)
        reset_row = QPushButton("Reset row")
        reset_row.setToolTip("Undo the analyst's changes of the selected substance")
        reset_row.clicked.connect(self.reset_row)
        reset_all = QPushButton("Reset all")
        reset_all.setToolTip("Undo every change made in this double determination")
        reset_all.clicked.connect(self.reset_all)
        buttons.addWidget(reset_row)
        buttons.addWidget(reset_all)
        exp = QPushButton("Export...")
        exp.clicked.connect(self.export)
        buttons.addWidget(exp)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)
        self.edit_note = theme.hint("", False)
        keys = QToolButton()
        keys.setText("?")
        keys.setAutoRaise(True)
        keys.setToolTip(KEYS_NOTE)
        status = QHBoxLayout()
        status.addWidget(self.banner, 1)
        status.addWidget(self.edit_note)
        status.addWidget(keys)
        lay.addLayout(pick)
        lay.addLayout(lim)
        lay.addLayout(status)
        lay.addWidget(split, 1)
        lay.addLayout(buttons)

        ws.runAdded.connect(lambda *_: self.refresh_choices())
        ws.runRemoved.connect(lambda *_: self.refresh_choices())
        ws.runChanged.connect(lambda *_: self.refresh_choices())
        ws.quantChanged.connect(self._quant_changed)
        ws.replicatesChanged.connect(self._reapply)
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

    def set_pair(self, a, b=None, compare=True, processed=False):
        """Show the double determination of ``a`` (partner ``b`` or the suggested one)."""
        if a is None:
            return
        self._view_processed = processed
        self._select(self.a, a)
        partner = b if b is not None else DV.suggest_partner(self.ws, a)
        self._select(self.b, partner or "")
        if compare:
            self.compare(sync=b is not None and not processed)

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
        """A new quantification: compare again -- never inside a running comparison (the building
        of the feature table recomputes the quantification itself), and once for several changes."""
        if self.isVisible() and self.members:
            self._refresh.start()
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

    def edits_key(self):
        return "hs_edits:" + self.ws.quant_unit() if self.ws.quant.get("mode") == "hs_screening" else "edits"

    def quant_signal(self):
        from gcws.quant.service import quant_detector
        return quant_detector(self.ws.quant)

    def _refresh_now(self):
        if self._comparing:
            self._refresh.start(50)
        elif self.isVisible() and self.members:
            self.compare(sync=False)

    def compare(self, sync: bool = False):
        if sync:
            self._view_processed = False
        if self._comparing:                         # re-entered through a signal: once more afterwards
            self._refresh.start()
            return
        self._comparing = True
        window = self.window()
        activity = f"compare:{id(self)}"
        if hasattr(window, "begin_activity"):
            window.begin_activity(activity, "Comparing determinations…")
        try:
            self._compare(sync)
        finally:
            if hasattr(window, "end_activity"):
                window.end_activity(activity)
            self._comparing = False

    def _compare(self, sync: bool = False):
        key = self.quant_signal()
        self.mirror.setLabel("bottom", f"RT ({key})", units="min")
        self.mirror.setLabel("left", f"A  ↑   {key}   ↓  B")
        self.mirror.setToolTip(f"A up, B down, in {key} signal units. Wheel or drag: time; double-click: full view.")
        if self._loading:
            return
        a, b = self.a.currentData(), self.b.currentData()
        self.members = [m for m in (a, b) if m]
        if not self.members:
            self._show([], [], ["load the determinations and give them role Sample"])
            return
        if sync:
            self._sync_group(self.members)
        self.table_features = None
        if len(self.members) >= 2 and self.features_mode() and not DV.member_problems(self.ws, self.members):
            from gcws.features import service as SV
            window = self.window()
            progress = (lambda message: window.update_activity(f"compare:{id(self)}", message)) \
                if hasattr(window, "update_activity") else None
            if self.ws.quant_result is None:
                self.ws.recompute_quant()
            # a deliberate Compare makes the automatic changes (each only once); a refresh only shows
            stack = self._stack()
            before = (stack.count(), stack.index())
            table = SV.run(self.ws, self.members, apply_auto=None if sync else False, stack=stack,
                           progress=progress, search=False)
            if (stack.count(), stack.index()) != before:
                self.ws.recompute_quant()              # the gap-filled peaks get their concentrations now
            self.table_features = DV.features_table(self.ws, self.members, table)
        window = self.window()
        if hasattr(window, "update_activity"):
            window.update_activity(f"compare:{id(self)}", "Preparing comparison table…")
        rows, problems = DV.compute(self.ws, self.members, "all")
        limit, rl = DV.limits(self.ws)
        labels = self.labels()
        unit = self.ws.quant_unit()
        verdicts = [DV.plain_verdict(r, limit, rl, labels, unit) for r in rows]
        self.base_rows = rows
        if hasattr(window, "update_activity"):
            window.update_activity(f"compare:{id(self)}", "Drawing comparison results…")
        self._show(DV.apply_edits(rows, verdicts, self.edits(), self._tol()), verdicts, problems)
        if self.table_features is not None and not self._view_processed:
            self._start_consensus_search()

    # -- analyst edits ------------------------------------------------------------------------

    def _tol(self) -> float:
        from gcws.quant.nias_bridge import make_settings
        return float(getattr(make_settings(self.ws.quant.get("settings")), "rt_tolerance", 0.035) or 0.035)

    def edits(self) -> dict:
        g = self.group()
        return dict((g or {}).get(self.edits_key()) or {})

    def _reapply(self):
        if not self.base_rows or self._filling:
            return
        self.rows = DV.apply_edits(self.base_rows, self.verdicts, self.edits(), self._tol())
        if self.rows and self.rows[0].get("light"):
            self._show_lights(self.rows)
        elif self.rows:
            self._show_summary(self.rows, self.labels())
        theme._repolish(self.banner)
        self._fill_table()
        self._update_report_note()

    def _update_report_note(self):
        n = sum(1 for r in self.rows if r.get("report"))
        changed = sum(1 for r in self.rows if r.get("edited"))
        text = f"{n} of {len(self.rows)} in the report"
        if changed:
            text += f" · {changed} changed"
        self.edit_note.setText(text)
        self.edit_note.setToolTip(f"{n} of {len(self.rows)} substances go into the report"
                                  + (f"; {changed} changed by the analyst" if changed else ""))

    def _names(self) -> str:
        return " / ".join(self.ws.runs[m].name for m in self.members if m in self.ws.runs)

    def set_edit(self, row: dict, field: str, value) -> None:
        """Store (or with ``None`` remove) one analyst change of ``row`` in the replicate group."""
        self.set_edits([(row, field, value)])

    def set_edits(self, changes: list) -> None:
        """Several ``(row, field, value)`` changes as one undo step; each is logged."""
        from datetime import datetime
        from gcws.core.audit import current_user
        if not changes:
            return
        self._sync_group(self.members)
        g = self.group()
        if g is None:
            return
        groups = copy.deepcopy(self.ws.replicate_groups)
        tg = next(x for x in groups if x["id"] == g["id"])
        edits = tg.setdefault(self.edits_key(), {})
        logs = []
        for row, field, value in changes:
            key = row.get("edit_key") or DV.edit_key(row["rt"])
            e = edits.setdefault(key, {"rt": float(row["rt"])})
            before = row.get(field) if field not in ("a1", "a2", "c1", "c2", "mean", "report") else \
                row.get("edited", {}).get(field, row.get(field))
            if value is None:
                e.pop(field, None)
            else:
                e[field] = value
            e["by"], e["at"] = current_user(), datetime.now().isoformat(timespec="seconds")
            if not any(k in e for k in DV.NUMERIC_EDITS + ("report", "comment")):
                edits.pop(key, None)
            name = row.get("name") or f"RT {row['rt']:.3f}"
            logs.append((f"{name} (RT {row['rt']:.3f}): {field}", "" if before is None else str(before),
                         "reset" if value is None else str(value)))
        if len(changes) == 1:
            label = f"double determination: {changes[0][1]} of {name}"
        else:
            label = f"double determination: {len(changes)} cells changed"
        self.set_groups(groups, label)
        for what, before, after in logs:
            self.ws.log("Double determination changed", self._names(), what, before, after)

    def set_identity(self, row: dict, name: str | None = None, cas: str | None = None) -> None:
        """Name / CAS of a substance: the identification of its FID peak in both determinations."""
        self.set_identities([(row, name, cas)])

    def set_identities(self, changes: list) -> None:
        """``(row, name, cas)`` of several substances as one undo step (None keeps a value)."""
        from gcws.core.ident import Identification
        from gcws.ui.undo import IdentCommand, MultiCommand
        per_run: dict = {}                       # one command per run: each command snapshots the set
        for row, name, cas in changes:
            for key, i in (("source1", 0), ("source2", 1)):
                src = row.get(key)
                if src is None or i >= len(self.members) or self.members[i] not in self.ws.runs:
                    continue
                rid = self.members[i]
                st = self.ws.runs[rid]
                res = self.ws.result(rid, self.quant_signal())
                if res is None or not res.peaks:
                    continue
                j = min(range(len(res.peaks)), key=lambda q: abs(res.peaks[q].apex_rt - src["rt"]))
                peak = res.peaks[j]
                if abs(peak.apex_rt - src["rt"]) > 0.05:
                    continue
                old = st.ident_set(self.quant_signal()).for_peak(peak)
                ident = Identification(apex_rt=peak.apex_rt,
                                       name=name if name is not None else (old.name if old else ""),
                                       cas=cas if cas is not None else (old.cas if old else ""),
                                       score=old.score if old else None, status="Accepted (analyst)",
                                       formula=old.formula if old else "", library=old.library if old else "",
                                       hits=list(old.hits) if old else [], source="double determination",
                                       manual=True, istd=old.istd if old else "")
                what = "name" if name is not None else "CAS"
                value = name if name is not None else cas
                per_run.setdefault(rid, []).append((peak.apex_rt, ident,
                                                    f"{what} of peak {peak.apex_rt:.3f} = {value!r}"))
        cmds = [IdentCommand(self.ws, rid, self.quant_signal(), [(t, ident) for t, ident, _ in items],
                             items[0][2] if len(items) == 1 else f"{len(items)} names / CAS")
                for rid, items in per_run.items()]
        if not cmds:
            return
        if len(changes) == 1:
            _row, name, cas = changes[0]
            label = f"double determination: {'name' if name is not None else 'CAS'} = " \
                    f"{name if name is not None else cas}"
        else:
            label = f"double determination: {len(changes)} names / CAS changed"
        self._stack().push(MultiCommand(label, cmds))

    def _stack(self):
        return self.ws.undo_group.activeStack() or self.ws.project_undo

    def _row_index(self, visual_row: int):
        it = self.table.item(visual_row, 0)
        k = it.data(Qt.UserRole) if it is not None else None
        return k if k is not None and 0 <= k < len(self.rows) else None

    def _report_value(self, k: int, on: bool):
        """The stored report edit: None where ``on`` is what the default rule gives anyway."""
        return None if on == DV.default_report(self.base_rows[k], self.verdicts[k]) else on

    def _mark_rows(self, visual_rows: list, on: bool):
        """Enter / Delete: the marked substances go into the report, or not."""
        changes = []
        for r in visual_rows:
            k = self._row_index(r)
            if k is not None and bool(self.rows[k].get("report")) != on:
                changes.append((self.rows[k], "report", self._report_value(k, on)))
        self.set_edits(changes)

    def _bulk_edit(self, cells: list):
        """Paste, Ctrl+D and the fill handle: many cells as one undo step."""
        from gcws.ui.docks.peak_table import parse_number
        edits, idents, bad = [], [], []
        for r, c, value in cells:
            k, field = self._row_index(r), FIELD_OF.get(c)
            if k is None or field is None:
                continue
            row = self.rows[k]
            if field == "report":
                edits.append((row, "report", self._report_value(k, bool(value))))
            elif field in ("name", "cas"):
                text = str(value).strip()
                if field == "name" and not text:
                    continue
                if text != (row.get(field) or ""):
                    idents.append((row, text, None) if field == "name" else (row, None, text))
            elif field == "comment":
                edits.append((row, "comment", str(value).strip() or None))
            else:
                text = str(value).strip()
                number = parse_number(text) if text else None
                if text and number is None:
                    bad.append(text)
                    continue
                edits.append((row, field, number))
        if bad:
            self.ws.message.emit(f"not a number, left out: {', '.join(sorted(set(bad))[:5])}")
        if not edits and not idents:
            return
        stack = self._stack()
        stack.beginMacro(f"double determination: {len(edits) + len(idents)} cells changed")
        try:
            self.set_identities(idents)
            self.set_edits(edits)
        finally:
            stack.endMacro()

    def _cell_edited(self, item):
        if self._filling:
            return
        field = FIELD_OF.get(item.column())
        k = item.data(Qt.UserRole)
        if field is None or k is None or not (0 <= k < len(self.rows)):
            return
        row = self.rows[k]
        if field == "report":
            on = item.checkState() == Qt.Checked
            base = DV.default_report(self.base_rows[k], self.verdicts[k])
            self.set_edit(row, "report", None if on == base else on)
            return
        text = item.text().strip()
        if field in ("name", "cas"):
            if text != (row.get(field) or ""):
                if field == "name" and not text:
                    self._reapply()
                    return
                self.set_identity(row, **{field: text})
            return
        if field == "comment":
            self.set_edit(row, "comment", text or None)
            return
        from gcws.ui.docks.peak_table import parse_number
        value = parse_number(text) if text else None
        if text and value is None:
            self.ws.message.emit(f"'{text}' is not a number")
            self._reapply()
            return
        if value is None or value == row.get(field):
            self.set_edit(row, field, None if value is None else value)
            return
        self.set_edit(row, field, value)

    def reset_row(self, row=None):
        row = row if row is not None else self._current_row()
        if row is None or not row.get("edit_key"):
            return
        self._sync_group(self.members)
        g = self.group()
        groups = copy.deepcopy(self.ws.replicate_groups)
        tg = next(x for x in groups if x["id"] == g["id"])
        tg.get(self.edits_key(), {}).pop(row["edit_key"], None)
        self.set_groups(groups, f"double determination: reset {row.get('name') or row['rt']}")
        self.ws.log("Double determination changed", self._names(), f"{row.get('name')}: changes reset")

    def reset_all(self):
        g = self.group()
        if g is None or not g.get(self.edits_key()):
            return
        groups = copy.deepcopy(self.ws.replicate_groups)
        next(x for x in groups if x["id"] == g["id"])[self.edits_key()] = {}
        self.set_groups(groups, "double determination: all changes reset")
        self.ws.log("Double determination changed", self._names(), "all changes reset")

    def _show(self, rows, verdicts, problems):
        self.rows, self.verdicts = rows, verdicts
        labels = self.labels()
        if problems:
            self._update_chips()
            self.banner.setText("Cannot compare yet: " + "; ".join(problems))
            self.banner.setProperty("level", "warn")
        elif rows and rows[0].get("light"):
            self._show_lights(rows)
        else:
            self._show_summary(rows, labels)
        theme._repolish(self.banner)
        self._fill_table()
        self._update_report_note()
        self._draw_mirror()

    def _show_summary(self, rows, labels):
        """Chips and status line of the classic pairing."""
        s = DV.summarize(rows, self.verdicts, DV.limits(self.ws)[0], labels)
        self._update_chips()
        if len(self.members) != 2:
            self.banner.setText("Single determination: choose a partner B to compare.")
            self.banner.setProperty("level", "neutral")
            return
        level, text = s.level, s.text
        n_open, n_decided = self.decision_counts()
        if n_decided and not n_open:
            level, text = "ok", f"All decisions made ({n_decided} red decided by you): preview the report. " + text
        elif n_decided:
            text = f"Red: {n_open + n_decided} → {n_open} open · {n_decided} decided. " + text
        if s.mean_reldiff is not None:
            text += f" Mean difference {s.mean_reldiff:.1f} %."
        self.banner.setText(("✔  " if level == "ok" else "⚠  ") + text)
        self.banner.setProperty("level", level)

    def decision_counts(self) -> tuple[int, int]:
        """(red rows still open, red rows the analyst has decided)."""
        red = [r for r, v in zip(self.rows, self.verdicts) if v.level == "bad"]
        decided = sum(1 for r in red if r.get("decided"))
        return len(red) - decided, decided

    def _update_chips(self):
        """Count the rows of each colour on the chips; the chip of the current filter is marked."""
        lights = [DV.light_of(r, v) for r, v in zip(self.rows, self.verdicts)]
        n = {key: sum(1 for r, v, lt in zip(self.rows, self.verdicts, lights) if self._passes(key, r, v, lt))
             for key in FILTERS}
        self.counts = n
        n_open, n_decided = self.decision_counts()
        for key, (label, level, tip) in FILTERS.items():
            text = f"{label} {n[key]}"
            if key == "red" and n_decided:
                text += f" · {n_open} open"
            theme.set_chip(self.chips[key], text, level)
            self.chips[key].setProperty("selected", key == self.filter)
            theme._repolish(self.chips[key])
        lone = sum(1 for r, lt in zip(self.rows, lights) if lt == "red"
                   and str(r.get("status", "")).startswith("Artefact"))
        self.chips["red"].setToolTip(FILTERS["red"][2] + (f" {lone} of them found in one determination only."
                                                          if lone else ""))

    @staticmethod
    def _passes(key: str, row: dict, v, light: str | None = None) -> bool:
        light = light or DV.light_of(row, v)
        if key == "all":
            return True
        if key == "check":
            return light in ("red", "yellow") or bool(row.get("edited"))
        return light == key

    def set_filter(self, key: str):
        """Show only the rows of one chip; the chip clicked again shows all rows."""
        self.filter = "all" if key == self.filter or key not in FILTERS else key
        self._update_chips()
        self._fill_table()

    def _show_lights(self, rows):
        """Chips and status line of the feature pairing: how many rows are green, yellow, red, grey."""
        n = {c: sum(1 for r in rows if r.get("light") == c) for c in ("green", "yellow", "red", "grey")}
        diffs = [r["reldiff"] for r in rows if r.get("reldiff") is not None and r.get("light") != "grey"]
        self._update_chips()
        notes = list(getattr(self.table_features, "notes", []) or [])
        auto = sum(1 for r in rows if r.get("gapfill"))
        n_open, n_decided = self.decision_counts()
        if n_open and n_decided:
            text = (f"Red: {n['red']} → {n_open} open · {n_decided} decided (F3 = next open); "
                    f"{n['yellow']} were made consistent automatically (yellow), {n['green']} are confirmed.")
            level = "bad"
        elif n_open:
            text = (f"{n['red']} of {len(rows)} substances need your decision (red; F3 = next); "
                    f"{n['yellow']} were made consistent automatically (yellow), {n['green']} are confirmed.")
            level = "bad"
        elif n_decided:
            text = (f"All decisions made ({n_decided} red decided by you): preview the report. "
                    f"{n['yellow']} made consistent automatically, {n['green']} confirmed.")
            level = "ok"
        elif n["yellow"]:
            text = f"Nothing to decide: {n['yellow']} made consistent automatically (yellow, a quick look), " \
                   f"{n['green']} confirmed."
            level = "warn"
        else:
            text, level = f"Double determination consistent: {n['green']} substances confirmed.", "ok"
        if auto:
            text += f" {auto} gap fill(s)."
        if diffs:
            text += f" Mean difference {sum(diffs) / len(diffs):.1f} %."
        drift = [x for x in notes if "drift" in x]
        if drift:
            text += " " + "; ".join(drift) + "."
        other = [x for x in notes if "drift" not in x and "time map" not in x]
        if other:
            text += " " + "; ".join(other)
        self.banner.setText(("✔  " if level == "ok" else "⚠  ") + text)
        self.banner.setProperty("level", level)
        props = self.boundary_proposals()
        self.b_bounds.setVisible(bool(props))
        self.b_bounds.setText(f"Harmonise boundaries ({len({p.rt for p in props})})")

    def _fill_table(self):
        from PySide6.QtGui import QFont
        labels = self.labels()
        unit = self.ws.quant_unit()
        names = [self.ws.runs[m].name for m in self.members if m in self.ws.runs]
        headers = ["", "Report", "RT [min]", "Substance", "CAS", f"Area {labels[0]}", f"Area {labels[1]}",
                   f"{labels[0]} [{unit}]", f"{labels[1]} [{unit}]", f"Mean [{unit}]", "Diff. %", "Verdict", "Notes",
                   "Comment"]
        feature_rows = bool(self.rows) and bool(self.rows[0].get("feature_id"))
        if feature_rows:
            headers += ["Feature", "Similarity", f"Hit {labels[0]}", f"Hit {labels[1]}"]
        cur = self.table.currentItem()
        keep_cur = (cur.data(Qt.UserRole), cur.column()) if cur is not None else None
        keep_sel = {(i.data(Qt.UserRole), i.column()) for i in self.table.selectedItems()}
        self._filling = True
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        for i, n in enumerate(names[:2]):
            for c in (C_A1 + i, C_C1 + i):
                self.table.horizontalHeaderItem(c).setToolTip(n)
        self.table.horizontalHeaderItem(C_REPORT).setToolTip("Goes into the report (default: not for artefacts and "
                                                             "values below the reporting limit)")
        self.table.setRowCount(0)
        editable = set(FIELD_OF) - {C_REPORT}
        for k, (row, v) in enumerate(zip(self.rows, self.verdicts)):
            if not self._passes(self.filter, row, v):
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            notes = "; ".join(x for x in (v.detail if row.get("light") else DV.english(row.get("review", "")),) if x)
            vals = [ICON.get(v.level, ""), None, row.get("rt"), row.get("name", ""), row.get("cas", ""),
                    row.get("a1"), row.get("a2"), row.get("c1"), row.get("c2"), row.get("mean"), row.get("reldiff"),
                    v.text, notes, row.get("comment", "")]
            if feature_rows:
                s1, s2 = row.get("source1") or {}, row.get("source2") or {}
                vals += [row.get("feature_id", ""), row.get("sim"),
                         s1.get("name", "") if s1 else "", s2.get("name", "") if s2 else ""]
            edited = row.get("edited") or {}
            level, tip = v.level, v.detail
            if v.level == "bad" and row.get("decided"):
                level, vals[C_ICON] = "info", ICON["decided"]
                tip = "Decided by the analyst: " + (v.detail or v.text)
            for c, val in enumerate(vals):
                it = QTableWidgetItem()
                if c == C_REPORT:
                    it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    it.setCheckState(Qt.Checked if row.get("report") else Qt.Unchecked)
                elif isinstance(val, float):
                    if c in (C_A1, C_A2):
                        it.setData(Qt.DisplayRole, int(round(val)))          # areas: whole counts
                    else:
                        it.setData(Qt.DisplayRole, round(val, {C_RT: 3, C_DIFF: 1, C_SIM: 2}.get(c, 4)))
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    it.setText("" if val is None else str(val))
                if c not in editable and c != C_REPORT:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setData(Qt.UserRole, k)
                it.setToolTip(tip)
                if c == C_ICON:
                    it.setData(OPEN_ROLE, DV.is_open(row, v))
                if c in (C_ICON, C_VERDICT):
                    it.setBackground(theme.status_brush(level))
                    it.setForeground(QBrush(theme.status_color(level)))
                field = FIELD_OF.get(c)
                if field in edited or (c == C_COMMENT and row.get("comment")):
                    font = QFont()
                    font.setItalic(True)
                    it.setFont(font)
                    it.setData(EDITED_ROLE, True)
                    was = edited.get(field)
                    it.setToolTip(f"Changed by the analyst" + (f" (was {was:.4g})" if isinstance(was, float) else
                                                               (f" (was {'on' if was else 'off'})" if field == "report"
                                                                else "")))
                self.table.setItem(r, c, it)
            if not row.get("report"):
                for c in (C_RT, C_NAME, C_MEAN):
                    self.table.item(r, c).setForeground(QBrush(QColor(theme.FAINT)))
        self.table.resizeColumnsToContents()
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        self.table.setColumnWidth(C_NAME, min(260, max(140, self.table.columnWidth(C_NAME))))
        hh.setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        self._restore_selection(keep_cur, keep_sel)
        self._filling = False

    def _restore_selection(self, cur, sel):
        """After a rebuild the same substances and columns are marked again (no navigation)."""
        from PySide6.QtCore import QItemSelectionModel
        rowof = {self.table.item(r, 0).data(Qt.UserRole): r for r in range(self.table.rowCount())
                 if self.table.item(r, 0) is not None}
        self.table.blockSignals(True)
        try:
            for k, c in sel:
                if k in rowof and self.table.item(rowof[k], c) is not None:
                    self.table.item(rowof[k], c).setSelected(True)
            if cur is not None and cur[0] in rowof:
                idx = self.table.model().index(rowof[cur[0]], cur[1])
                self.table.selectionModel().setCurrentIndex(idx, QItemSelectionModel.NoUpdate)
        finally:
            self.table.blockSignals(False)

    # -- feature pairing ------------------------------------------------------------------------

    def features_mode(self) -> bool:
        from gcws.features import service as SV
        from gcws.features.model import PAIRING_FEATURES
        return self.ws.quant.get("mode") != "hs_screening" and SV.pairing(self.ws) == PAIRING_FEATURES

    def feature_of(self, row):
        t = self.table_features
        return t.by_id(row.get("feature_id")) if (t is not None and row and row.get("feature_id")) else None

    def edit_settings(self):
        from gcws.features import service as SV
        from gcws.ui.dialogs.feature_settings import FeatureSettingsDialog
        dlg = FeatureSettingsDialog(SV.settings(self.ws), self)
        if dlg.exec() != FeatureSettingsDialog.Accepted:
            return
        self.set_settings(dlg.settings())

    def set_settings(self, settings) -> None:
        """Store the feature settings (one undo step) and compare again."""
        q = copy.deepcopy(self.ws.quant)
        q["features"] = settings.to_dict()
        if q != self.ws.quant:
            self.ws.push_quant("double determination settings", q, "double determination (features)")
        self.compare(sync=False)

    def next_red(self):
        """F3: the next red row still open after the current one (from the top at the end)."""
        rows = [r for r in range(self.table.rowCount())
                if self.table.item(r, C_ICON) is not None and self.table.item(r, C_ICON).data(OPEN_ROLE)]
        if not rows:
            return
        cur = self.table.currentRow()
        nxt = next((r for r in rows if r > cur), rows[0])
        self.table.setCurrentCell(nxt, C_NAME)

    def boundary_proposals(self, row=None) -> list:
        t = self.table_features
        if t is None:
            return []
        feats = [self.feature_of(row)] if row is not None else t.features
        return [p for f in feats if f is not None for p in f.proposals if p.kind == "boundary"]

    def apply_boundaries(self, row=None) -> int:
        from gcws.features import service as SV
        props = self.boundary_proposals(row)
        if not props or self.table_features is None:
            return 0
        n = SV.apply(self.ws, self.table_features, props, stack=self._stack(),
                     label=f"double determination: {len({p.rt for p in props})} boundaries harmonised")
        self.compare(sync=False)
        return n

    def remove_gap_fill(self, row) -> bool:
        """Take the gap fill of this substance out again (one undo step)."""
        from gcws.features.model import GAPFILL, GAPFILL_OPTION
        from gcws.ui.undo import ManualEventsCommand, MultiCommand
        f = self.feature_of(row)
        if f is None:
            return False
        cmds = []
        for m in f.members:
            if m.origin != GAPFILL or m.peak is None:
                continue
            key = self.table_features.key
            st = self.ws.runs[m.run_id]
            keep = [e for e in st.events(key)
                    if not (e.option == GAPFILL_OPTION and e.t1 is not None
                            and min(e.t0, e.t1) - 1e-6 <= m.peak.rt <= max(e.t0, e.t1) + 1e-6)]
            if len(keep) != len(st.events(key)):
                cmds.append(ManualEventsCommand(self.ws, m.run_id, key, keep, f"{f.id}: gap fill removed"))
        if not cmds:
            return False
        self._stack().push(MultiCommand(f"double determination: gap fill of {f.id} removed", cmds))
        self.ws.log("Double determination (features)", self._names(), f"{f.id}: gap fill removed by the analyst")
        self.compare(sync=False)
        return True

    def _visual_row(self, row) -> int | None:
        """The table row showing ``row`` (None: filtered out)."""
        k = next((i for i, r in enumerate(self.rows) if r is row), None)
        return next((r for r in range(self.table.rowCount())
                     if self.table.item(r, 0) is not None and self.table.item(r, 0).data(Qt.UserRole) == k), None)

    def _marked_or(self, row) -> list[int]:
        """The marked table rows when ``row`` is one of them, else just ``row``'s."""
        r = self._visual_row(row)
        marked = self.table.selected_rows()
        return marked if r in marked else ([r] if r is not None else [])

    def edit_comment(self, row) -> None:
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, "Comment", row.get("name") or f"RT {row['rt']:.3f}",
                                        text=row.get("comment", ""))
        if ok:
            self.set_edit(row, "comment", text.strip() or None)

    def copy_row(self, row) -> None:
        from PySide6.QtGui import QGuiApplication
        r = self._visual_row(row)
        if r is None:
            return
        cells = [self.table.cell_value(r, c) for c in range(1, self.table.columnCount())
                 if not self.table.isColumnHidden(c)]
        QGuiApplication.clipboard().setText("\t".join("yes" if v is True else "no" if v is False else str(v)
                                                       for v in cells))

    def row_actions(self, row) -> list:
        """``[(text, callable)]`` of the right-click menu of ``row``; ``(None, None)`` is a separator."""
        labels = self.labels()
        out = [("Report", lambda: self._mark_rows(self._marked_or(row), True)),
               ("Not reported", lambda: self._mark_rows(self._marked_or(row), False))]
        if row.get("edit_key"):
            out.append(("Reset row", lambda: self.reset_row(row)))
        out.append(("Comment…", lambda: self.edit_comment(row)))
        for key, i in (("source1", 0), ("source2", 1)):
            if row.get(key) is not None and i < len(self.members):
                out.append((f"Show in {labels[i]}", lambda b=bool(i): self._navigate(row, prefer_b=b)))
        out.append(("Copy row", lambda: self.copy_row(row)))
        f = self.feature_of(row)
        if f is None:
            return out
        out.append((None, None))
        ident = f.identity
        if ident is not None and ident.case in ("C", "D"):
            for c in ident.candidates[:3]:
                out.append((f"Name: {c['name']}", lambda c=c: self.set_identities([(row, c["name"], c.get("cas") or "")])))
        if any(m.origin == "gapfill" for m in f.members):
            out.append(("Remove the gap fill", lambda: self.remove_gap_fill(row)))
        if self.boundary_proposals(row):
            out.append(("Harmonise the boundaries", lambda: self.apply_boundaries(row)))
        return out

    def _context_menu(self, pos):
        it = self.table.itemAt(pos)
        if it is None:
            return
        k = it.data(Qt.UserRole)
        if k is None or not (0 <= k < len(self.rows)):
            return
        actions = self.row_actions(self.rows[k])
        menu = QMenu(self)
        for text, fn in actions:
            if text is None:
                menu.addSeparator()
            else:
                menu.addAction(text, fn)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _start_consensus_search(self):
        """Search the consensus spectra still unknown (worker thread), then compare again."""
        from gcws.features import service as SV
        from gcws.features.consensus import search_consensus
        t = self.table_features
        if t is None or self._search is not None or not SV.settings(self.ws).consensus_search:
            return
        needed = SV.consensus_needed(self.ws, t, SV.settings(self.ws))
        if not needed:
            return
        self._search_needed = needed
        self._search_group = SV.group_of(self.ws, t.members)
        self.banner.setText(self.banner.text() + f"  Searching {len(needed)} consensus spectra…")
        window = self.window()
        if hasattr(window, "begin_activity"):
            window.begin_activity(f"consensus:{id(self)}", f"Searching {len(needed)} spectra…")
        self._search = workers.submit(
            search_consensus, None, SV.search_method(self.ws, t.members), [f for _k, f in needed],
            on_done=self._consensus_found,
            on_error=lambda error: self._consensus_found(f"consensus search failed: {error.splitlines()[0]}"))

    def _consensus_found(self, note: str):
        from gcws.features import service as SV
        needed, self._search_needed = getattr(self, "_search_needed", []), []
        self._search = None
        window = self.window()
        if hasattr(window, "end_activity"):
            window.end_activity(f"consensus:{id(self)}")
        SV.store_consensus(self.ws, needed, note, group=getattr(self, "_search_group", None))
        if self.isVisible() and self.members:
            self.compare(sync=True)

    # -- spectra ---------------------------------------------------------------------------------

    def _draw_spectra(self):
        self.spec.clear()
        row = self._current_row()
        f = self.feature_of(row) if row is not None else None
        if f is None:
            return
        self.spec.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(theme.BORDER_STRONG)), ignoreBounds=True)
        for sign, m in zip((1, -1), f.members[:2]):
            if m.peak is None:
                continue
            spec = m.peak.spectrum if m.peak.spectrum is not None else m.peak.full_spectrum
            if spec is None or len(spec[0]) == 0:
                continue
            mz, ab = np.asarray(spec[0], float), np.asarray(spec[1], float)
            ab = ab / ab.max() * 100.0
            xs = np.repeat(mz, 2)
            ys = np.column_stack([np.zeros_like(ab), sign * ab]).ravel()
            st = self.ws.runs.get(m.run_id)
            color = st.color if st is not None else theme.ACCENT
            self.spec.plot(xs, ys, connect="pairs", pen=pg.mkPen(color, width=1.5))
            for i in np.argsort(-ab)[:4]:
                t = pg.TextItem(f"{int(mz[i])}", anchor=(0.5, 1.0 if sign > 0 else 0.0), color=theme.FAINT)
                t.setPos(mz[i], sign * ab[i])
                self.spec.addItem(t)
        sim = "–" if f.sim is None else f"{f.sim:.2f}"
        name = f.identity.name if f.identity else ""
        self.spec.setTitle(f"{f.id}  similarity {sim}  {name[:40]}", size="9pt")
        self.spec.setYRange(-110, 110, padding=0)

    # -- mirror plot -----------------------------------------------------------------------

    def _t0(self, st, sig) -> float:
        """Where the mirror plot starts: the integration start, else the solvent end."""
        from gcws.integration.autoparams import _integration_start
        t = _integration_start(sig.rt, self.ws.method_for(st, self.quant_signal()), self.ws.solvent_cut(st, self.quant_signal()))
        if t is None and self.quant_signal() == "TIC":
            return float(sig.rt[0])
        if t is None:
            from gcws.quant.nias_bridge import make_settings
            t = float(getattr(make_settings(self.ws.quant.get("settings")), "solvent_end", 0.0) or 0.0)
        return min(max(t, float(sig.rt[0])), float(sig.rt[-1]))

    def _trace(self, rid):
        """``(rt, signal above baseline, colour)`` of the FID from the integration start on.

        Both determinations keep their own signal units (no normalisation), so a peak that is
        twice as large in A also looks twice as large. The solvent front is left out and the
        slow baseline (solvent tail, column bleed) is removed, so peaks stand on zero."""
        st = self.ws.runs.get(rid)
        sig = st.run.signal(self.quant_signal()) if st is not None else None
        if sig is None or len(sig.rt) < 2:
            return None
        keep = sig.rt >= self._t0(st, sig)
        if keep.sum() < 2:
            return None
        from gcws.signal.envelope import envelope
        rt, y = sig.rt[keep], np.asarray(sig.y[keep], float)
        return rt, np.maximum(y - envelope(rt, y, BASELINE_WINDOW), 0.0), st.color

    def _draw_mirror(self):
        self.mirror.clear()
        self._traces, self._marks = [], []
        det = self.quant_signal()
        self.mirror.setLabel("bottom", f"RT ({det})", units="min")
        self.mirror.setLabel("left", f"A  ↑   {det}   ↓  B")
        self.mirror.addItem(self.cursor, ignoreBounds=True)
        traces = [self._trace(m) for m in self.members[:2]]
        for sign, tr in zip((1, -1), traces):
            if tr is None:
                continue
            rt, y, color = tr
            self.mirror.plot(rt, sign * y, pen=pg.mkPen(color, width=1.2), shadowPen=theme.glow_pen(color))
            self._traces.append((rt, sign * y))
        self.mirror.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(theme.BORDER_STRONG)), ignoreBounds=True)
        spots, self._marks = [], []
        istd = self._istd_times()
        for row, v in zip(self.rows, self.verdicts):
            color = theme.status_color(v.level)
            for sign, key, tr in ((1, "source1", traces[0] if traces else None),
                                  (-1, "source2", traces[1] if len(traces) > 1 else None)):
                src = row.get(key)
                if src is None or tr is None or not tr[0][0] <= src["rt"] <= tr[0][-1]:
                    continue
                y = float(np.interp(src["rt"], tr[0], tr[1]))       # on the apex of the peak
                is_istd = any(abs(src["rt"] - t) < 0.02 for t in istd)
                self._marks.append((src["rt"], abs(y), is_istd))
                spots.append({"pos": (src["rt"], sign * y), "brush": pg.mkBrush(color),
                              "pen": pg.mkPen(theme.SURFACE, width=0.8), "size": 8,
                              "data": row.get("name") or ""})
        if spots:
            dots = pg.ScatterPlotItem(spots=spots, hoverable=True, tip=lambda x, y, data: f"{data}  RT {x:.3f}")
            self.mirror.addItem(dots, ignoreBounds=True)
        self.full_view()

    def full_view(self):
        """The whole integrated part of both determinations."""
        if not self._traces:
            return
        x0 = min(float(rt[0]) for rt, _ in self._traces)
        x1 = max(float(rt[-1]) for rt, _ in self._traces)
        self.mirror.setXRange(x0, x1, padding=0.01)
        self._fit_y()

    def _istd_times(self) -> list[float]:
        """FID apexes of the internal standards (from the quantification) in A and B."""
        out = []
        for rid in self.members[:2]:
            res = self.ws.result(rid, self.quant_signal())
            if res is None:
                continue
            for i, q in self.ws.quant_rows(rid, self.quant_signal()).items():
                if q.get("istd") and 0 <= i < len(res.peaks):
                    out.append(res.peaks[i].apex_rt)
        return out

    def _fit_y(self):
        """Symmetric intensity range from the largest peak inside the visible time window.

        Over a wide window (more than ``WIDE_VIEW`` min) a few very large peaks (internal
        standards, main components) would flatten everything else: the range then follows the
        ``WIDE_PERCENTILE`` of the substance peaks without the internal standards, and the
        largest peaks are clipped. Zooming in (a picked row) always shows the full peak."""
        if not self._traces:
            return
        (x0, x1), _ = self.mirror.getViewBox().viewRange()
        top = 0.0
        if x1 - x0 > WIDE_VIEW:
            heights = [h for t, h, is_istd in self._marks if x0 <= t <= x1 and not is_istd]
            top = float(np.percentile(heights, WIDE_PERCENTILE)) if len(heights) >= 5 else 0.0
        if not top:
            for rt, y in self._traces:
                m = (rt >= x0) & (rt <= x1)
                if m.any():
                    top = max(top, float(np.max(np.abs(y[m]))))
        top = top or 1.0
        self.mirror.setYRange(-1.12 * top, 1.12 * top, padding=0)

    # -- navigation ----------------------------------------------------------------------------

    def _current_row(self):
        it = self.table.currentItem()
        if it is None:
            items = self.table.selectedItems()
            it = items[0] if items else None
        if it is None:
            return None
        k = it.data(Qt.UserRole)
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
        self._draw_spectra()

    def _open_row(self, r, c):
        if c in FIELD_OF:                       # an editable cell: the double-click edits it
            return
        k = self.table.item(r, 0).data(Qt.UserRole)
        if k is not None:
            self._navigate(self.rows[k], prefer_b=c in (C_A2, C_C2))

    def _navigate(self, row, prefer_b=False):
        order = [("source2", 1), ("source1", 0)] if prefer_b else [("source1", 0), ("source2", 1)]
        for key, i in order:
            src = row.get(key)
            if src is None or i >= len(self.members):
                continue
            rid = self.members[i]
            self.ws.set_active(rid)
            if self.ws.signal_key != self.quant_signal():
                self.ws.set_signal_key(self.quant_signal())
            res = self.ws.result(rid, self.quant_signal())
            if res is not None and res.peaks:
                j = min(range(len(res.peaks)), key=lambda q: abs(res.peaks[q].apex_rt - src["rt"]))
                if abs(res.peaks[j].apex_rt - src["rt"]) < 0.05:
                    self.ws.select_peak(j)
                    self.ws.peakFocusRequested.emit(j)
            return

    # -- output ------------------------------------------------------------------------------------

    def _limit_changed(self):
        from gcws.quant.nias_bridge import make_settings, settings_dict
        new = float(self.limit.value())
        if abs(new - DV.limits(self.ws)[0]) < 1e-9:
            return
        q = copy.deepcopy(self.ws.quant)
        if q.get("mode") == "hs_screening":
            q.setdefault("hs", {})["duplicate_max_reldiff"] = new
            self.ws.push_quant(f"HS duplicate difference limit = {new:g} %", q)
            return
        s = make_settings(q.get("settings"))
        s.duplicate_max_reldiff = new
        q["settings"] = settings_dict(s)
        self.ws.push_quant(f"duplicate difference limit = {new:g} %", q)

    def _report(self, kind, preview=False):
        self._sync_group(self.members)
        g = self.group()
        if g is not None:
            (self.previewRequested if preview else self.reportRequested).emit(kind, g["id"])

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
        from gcws.core.text import excel_safe
        wb = Workbook()
        sh = wb.active
        sh.title = "Double determination"
        labels = self.labels()
        unit = self.ws.quant_unit()
        sh.append([f"{labels[i]}: {self.ws.runs[m].name}" for i, m in enumerate(self.members) if m in self.ws.runs])
        sh.append(["Difference limit %", DV.limits(self.ws)[0]])
        sh.append([])
        sh.append(["RT [min]", "Substance", "CAS", f"{labels[0]} [{unit}]", f"{labels[1]} [{unit}]",
                   f"Mean [{unit}]", "Diff. %", "Verdict", "Explanation", "Report", "Changed by analyst", "Comment"])
        fills = {lvl: PatternFill("solid", fgColor=theme.LEVELS[lvl][1].lstrip("#")) for lvl in theme.LEVELS}
        for row, v in zip(self.rows, self.verdicts):
            sh.append([excel_safe(x) for x in (
                row.get("rt"), row.get("name"), row.get("cas"), row.get("c1"), row.get("c2"), row.get("mean"),
                row.get("reldiff"), v.text, v.detail, "yes" if row.get("report") else "no",
                ", ".join(sorted(row.get("edited") or {})), row.get("comment", ""))])
            sh.cell(sh.max_row, 8).fill = fills.get(v.level, fills["neutral"])
        wb.save(path)
        self.ws.message.emit(f"Double determination exported: {path}")
