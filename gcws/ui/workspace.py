"""Workspace controller: loaded runs, their integrations and identifications.

Holds no widgets. Docks observe its signals and change state through undo
commands (``gcws.ui.undo``), so every change is undoable and audited.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QSettings, QTimer, Signal as QtSignal
from PySide6.QtGui import QUndoGroup, QUndoStack

from gcws.core.audit import AuditLog, AuditRecord
from gcws.core.events import ManualEvent
from gcws.core.ident import IdentificationSet
from gcws.core.keys import BLANK_SUFFIX, base_key, derived_key, is_derived, is_fid, method_kind, split_key
from gcws.core.model import FID, TIC, Run
from gcws.integration.engine import IntegrationResult, integrate
from gcws.integration.method import IntegrationMethod
from gcws.integration.store import MethodStore
from gcws.io import sequence
from gcws.signal.delay import DelayEstimate, estimate_delay, refine_with_peaks

from gcws.ui.theme import RUN_COLORS as PALETTE


@dataclass
class RunState:
    run: Run
    color: str
    visible: bool = True
    methods: dict[str, IntegrationMethod] = field(default_factory=dict)   # per signal kind
    manual: dict[str, list[ManualEvent]] = field(default_factory=dict)    # per signal key
    results: dict[str, IntegrationResult] = field(default_factory=dict)   # per signal key
    idents: dict[str, IdentificationSet] = field(default_factory=dict)    # per signal key
    delay: Optional[DelayEstimate] = None
    delay_override: Optional[float] = None
    blanks: list[str] = field(default_factory=list)
    blanks_istd: list[str] = field(default_factory=list)
    spectrum_overrides: dict = field(default_factory=dict)
    undo: Optional[QUndoStack] = None
    saved_digests: dict[str, str] = field(default_factory=dict)
    blanks_manual: bool = False                       # blanks set by the analyst: never re-suggested
    deconv: dict = field(default_factory=dict)        # deconvolution results (see gcws.ms.deconv_cache)
    blank_alignment: dict = field(default_factory=dict)   # base key -> [Alignment] of the derived trace

    @property
    def id(self) -> str:
        return self.run.id

    @property
    def name(self) -> str:
        return self.run.name

    @property
    def role(self) -> str:
        return self.run.role

    @property
    def delay_value(self) -> float:
        if self.delay_override is not None:
            return self.delay_override
        return self.delay.value if self.delay is not None else 0.006

    def events(self, key: str) -> list[ManualEvent]:
        return self.manual.setdefault(key, [])

    def ident_set(self, key: str) -> IdentificationSet:
        # a derived trace ("FID - Blank") shows the same substances as its base
        return self.idents.setdefault(base_key(key), IdentificationSet())


class Workspace(QObject):
    runAdded = QtSignal(str)
    runRemoved = QtSignal(str)
    runChanged = QtSignal(str)                 # colour, visibility, role, blanks, name
    activeRunChanged = QtSignal(str)
    signalKeyChanged = QtSignal(str)
    resultChanged = QtSignal(str, str)         # run id, signal key
    identsChanged = QtSignal(str, str)
    selectionChanged = QtSignal(str, int)      # run id, peak index (-1 none)
    methodChanged = QtSignal(str)              # run id
    replicatesChanged = QtSignal()
    quantChanged = QtSignal()
    deconvChanged = QtSignal(str)              # run id: whole-run deconvolution available / dropped
    solventCutChanged = QtSignal()
    panelsChanged = QtSignal()                 # signal / blank choice of Chromatogram 1 or 2, table source
    peakFocusRequested = QtSignal(int)         # the analyst picked a peak in a list: zoom the chromatograms to it
    message = QtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs: dict[str, RunState] = {}
        self.order: list[str] = []
        self.active_id: Optional[str] = None
        self.signal_key: str = FID
        # Chromatogram 1 and 2: base signal and "- Blank" switch of each; the peak table lists the
        # peaks of one of them, and ``signal_key`` is always that panel's key
        self.panel_keys: list[str] = [FID, TIC]
        self.panel_blank: list[bool] = [False, False]
        self.table_panel: int = 0
        self.selected: int = -1
        self.methods = MethodStore()
        self.audit = AuditLog()
        self.undo_group = QUndoGroup(self)
        self.project_undo = QUndoStack(self)
        self.undo_group.addStack(self.project_undo)
        self.project_path: Optional[Path] = None
        self.replicate_groups: list[dict] = []
        self.quant: dict = {"mode": "nias_mgkg", "unit": "µg/L", "settings": {},
                           "solvent_cut": QSettings().value("integration/solvent_cut", False, type=bool)}
        self.quant_result = None
        self.dirty = False
        self._quant_timer = QTimer(self)
        self._quant_timer.setSingleShot(True)
        self._quant_timer.setInterval(150)
        self._quant_timer.timeout.connect(self.recompute_quant)
        for sig in (self.resultChanged, self.identsChanged, self.runChanged, self.runAdded, self.runRemoved):
            sig.connect(lambda *_: self.schedule_quant())
        from gcws.ui.hint_cache import HintCache
        self.hints = HintCache(self, self)
        self._blank_matches: dict = {}
        self.resultChanged.connect(self._drop_matches)

    # -- runs --------------------------------------------------------------

    def next_color(self) -> str:
        used = {s.color for s in self.runs.values()}
        for c in PALETTE:
            if c not in used:
                return c
        return PALETTE[len(self.runs) % len(PALETTE)]

    def find_by_path(self, path) -> Optional[RunState]:
        p = Path(path).resolve()
        for s in self.runs.values():
            if s.run.path.resolve() == p:
                return s
        return None

    def add_run(self, run: Run, results: dict | None = None, color: str | None = None,
                delay: DelayEstimate | None = None) -> RunState:
        st = RunState(run=run, color=color or self.next_color())
        st.undo = QUndoStack(self)
        self.undo_group.addStack(st.undo)
        st.delay = delay
        run.derive = self._derive
        for kind in (FID, TIC):
            st.methods[kind] = self.methods.get(self.methods.default_name(kind))
        if results:
            st.results.update(results)
        self.runs[run.id] = st
        if self.quant.get("solvent_cut"):
            for key in list(st.results):
                self.integrate(st.id, key, emit=False)
        self.order.append(run.id)
        self._sort_order()
        if len(self.runs) == 1:                       # the first run: panels on signals it has
            self._fit_panels(run)
        self.dirty = True
        self.runAdded.emit(run.id)
        if self.active_id is None:
            self.set_active(run.id)
        self._suggest_blanks()
        self.invalidate_blank([run.id])
        return st

    def remove_run(self, run_id: str) -> None:
        st = self.runs.pop(run_id, None)
        if st is None:
            return
        self.order.remove(run_id)
        if st.undo is not None:
            self.undo_group.removeStack(st.undo)
        users = [o.id for o in self.runs.values() if run_id in o.blanks + o.blanks_istd]
        for other in self.runs.values():
            other.blanks = [b for b in other.blanks if b != run_id]
            other.blanks_istd = [b for b in other.blanks_istd if b != run_id]
        if users:
            self.invalidate_blank(users)
        for g in self.replicate_groups:
            g["members"] = [m for m in g["members"] if m != run_id]
        self.dirty = True
        self.runRemoved.emit(run_id)
        if self.active_id == run_id:
            self.set_active(self.order[0] if self.order else None)

    def signals_for(self, st: RunState) -> list[str]:
        """Keys offered for a run: its raw traces, the EICs computed so far and, with a
        blank assigned, the blank-subtracted variants."""
        keys = list(st.run.available_signals())
        keys += [k for k in st.run._signals if k.startswith("EIC") and not is_derived(k) and k not in keys]
        if self.blank_ids(st):
            keys += [derived_key(k) for k in list(keys) if k != "BPC"]
        return keys

    def runs_with(self, key: str) -> list[RunState]:
        return [s for s in self.states() if s.run.signal(key) is not None]

    def states(self) -> list[RunState]:
        return [self.runs[i] for i in self.order if i in self.runs]

    @property
    def active(self) -> Optional[RunState]:
        return self.runs.get(self.active_id) if self.active_id else None

    def set_active(self, run_id: Optional[str]) -> None:
        if run_id == self.active_id:
            return
        self.active_id = run_id
        self.selected = -1
        st = self.active
        if st is not None and st.undo is not None:
            self.undo_group.setActiveStack(st.undo)
        self.activeRunChanged.emit(run_id or "")

    def effective_key(self, st: Optional[RunState], key: Optional[str] = None) -> str:
        """``key`` for run ``st``, or its base key when ``st`` cannot build that derived trace
        (e.g. "FID - Blank" on a blank run or on a sample without an assigned Blank)."""
        key = key or self.signal_key
        if st is None or not is_derived(key):
            return key
        return key if st.run.signal(key) is not None else base_key(key)

    @property
    def active_key(self) -> str:
        """The working signal of the active run (see :meth:`effective_key`)."""
        return self.effective_key(self.active, self.signal_key)

    def derived_note(self, st: Optional[RunState], key: Optional[str] = None) -> str:
        """Why ``st`` shows the base trace instead of ``key`` ("" when it does not)."""
        key = key or self.signal_key
        if st is None or not is_derived(key) or self.effective_key(st, key) == key:
            return ""
        base = base_key(key)
        if st.role in (sequence.BLANK, sequence.BLANK_ISTD, sequence.LADDER):
            reason = f"it is a {sequence.ROLE_LABELS.get(st.role, st.role)} run itself"
        elif not self.blank_ids(st):
            src = self.blank_options().source
            if st.blanks_istd and src == "blank":
                reason = ("only a Blank + ISTD is assigned, and it is not subtracted "
                          "(Quantify > Blank subtraction settings: source)")
            elif st.blanks and src == "blank_istd":
                reason = "only a Blank is assigned, but the subtraction source is Blank + ISTD"
            else:
                reason = "no blank assigned (Quantify > Assign blanks)"
        else:
            reason = f"its blank has no {base} trace"
        return f"{key} not available for {st.name}: {reason}. Showing {base}."

    # -- Chromatogram 1 / 2 ------------------------------------------------------

    def panel_key(self, i: int) -> str:
        """Signal key of chromatogram panel ``i`` (with " - Blank" when its blank switch is on)."""
        return derived_key(self.panel_keys[i]) if self.panel_blank[i] else self.panel_keys[i]

    def _fit_panels(self, run: Run) -> None:
        avail = run.available_signals()
        if not avail:
            return
        for i in (0, 1):
            if base_key(self.panel_keys[i]) not in avail and not self.runs_with(self.panel_keys[i]):
                self.panel_keys[i] = avail[0] if i == 0 else (TIC if TIC in avail else avail[0])
        self.signal_key = self.panel_key(self.table_panel)

    def set_panel(self, i: int, key: Optional[str] = None, blank: Optional[bool] = None) -> None:
        """Choose the signal (``key`` may carry " - Blank") and/or the blank switch of panel ``i``."""
        if key is not None:
            base, suffix = split_key(key)
            self.panel_keys[i] = base
            if suffix is not None:
                blank = True
        if blank is not None:
            self.panel_blank[i] = bool(blank)
        k = self.panel_key(i)
        for st in self.states():
            if st.run.signal(k) is not None and k not in st.results:
                self.integrate(st.id, k, emit=False)
        self.panelsChanged.emit()
        if i == self.table_panel:
            self._set_table_key(k)

    def set_table_panel(self, i: int) -> None:
        """The peak table (and everything that works on "the" peaks) follows panel ``i``."""
        self.table_panel = i
        self.panelsChanged.emit()
        self._set_table_key(self.panel_key(i))

    def set_panels(self, keys, blanks, table: int = 0) -> None:
        """Restore the panel state (project, settings) in one step."""
        self.panel_keys = [base_key(k) for k in keys][:2] + [TIC] * max(0, 2 - len(keys))
        self.panel_blank = [bool(b) for b in blanks][:2] + [False] * max(0, 2 - len(blanks))
        self.table_panel = 1 if table == 1 else 0
        self.signal_key = self.panel_key(self.table_panel)
        self.panelsChanged.emit()

    def set_signal_key(self, key: str) -> None:
        """The table's signal; the panel that feeds the table shows it too."""
        key = key.strip()
        if not key:
            return
        base, suffix = split_key(key)
        i = self.table_panel
        if (self.panel_keys[i], self.panel_blank[i]) != (base, suffix is not None):
            self.panel_keys[i], self.panel_blank[i] = base, suffix is not None
            self.panelsChanged.emit()
        self._set_table_key(key)

    def _set_table_key(self, key: str) -> None:
        if key == self.signal_key:
            return
        self.signal_key = key
        self.selected = -1
        for st in self.states():
            if st.run.signal(key) is not None and key not in st.results:
                self.integrate(st.id, key, emit=False)
        self.signalKeyChanged.emit(key)

    def _sort_order(self) -> None:
        seq = sequence.parse_sequence_log(self.runs[self.order[0]].run.path.parent) if self.order else []
        self.order.sort(key=lambda i: sequence.order_key(self.runs[i].run.path, seq))

    def index_of(self, run_id: str) -> int:
        return self.order.index(run_id) if run_id in self.order else -1

    def reorder(self, ids: list[str]) -> None:
        self.order = [i for i in ids if i in self.runs]

    # -- integration -------------------------------------------------------

    def method_for(self, st: RunState, key: str) -> IntegrationMethod:
        kind = method_kind(key)
        m = st.methods.get(kind)
        if m is None:
            m = self.methods.get(self.methods.default_name(kind))
            st.methods[kind] = m
        return m

    def solvent_cut(self, st, key: str):
        """Cut in the signal's detector time; the shared setting is in FID minutes."""
        if not self.quant.get("solvent_cut", False):
            return None
        t = float((self.quant.get("settings") or {}).get("solvent_end", 5.5))
        return t if is_fid(key) else t - st.delay_value

    def set_solvent_cut(self, enabled: bool, end=None):
        import copy
        q = copy.deepcopy(self.quant)
        q["solvent_cut"] = bool(enabled)
        if end is not None:
            q.setdefault("settings", {})["solvent_end"] = float(end)
        if q != self.quant:
            self.push_quant("Solvent cut", q, "solvent cut and NIAS solvent end (FID time)")

    def integrate(self, run_id: str, key: Optional[str] = None, emit: bool = True) -> Optional[IntegrationResult]:
        st = self.runs.get(run_id)
        if st is None:
            return None
        key = key or self.signal_key
        sig = st.run.signal(key)
        if sig is None:
            st.results.pop(key, None)
            return None
        res = integrate(sig, self._derived_method(st, key, sig) if is_derived(key) else self.method_for(st, key),
                        st.events(key), t_min=self.solvent_cut(st, key))
        st.results[key] = res
        stale = [] if is_derived(key) else self._drop_derived_of(run_id, key)
        if key == FID and st.run.ms is not None and st.delay is not None and st.delay_override is None:
            tic = st.results.get(TIC)
            if tic is not None:
                st.delay = refine_with_peaks(st.delay, [p.apex_rt for p in res.peaks],
                                             [p.apex_rt for p in tic.peaks])
        if emit:
            self.resultChanged.emit(run_id, key)
            for rid, dk in stale:
                self.resultChanged.emit(rid, dk)
        return res

    def _drop_derived_of(self, run_id: str, base: str) -> list[tuple[str, str]]:
        """A new integration of ``base`` in ``run_id`` changes the blank-subtracted traces built on
        it: the run's own and those of the samples using it as blank. They are dropped and rebuilt
        on the next access; returns ``[(run id, derived key)]`` that had a result."""
        dk = derived_key(base)
        out = []
        for st in self.states():
            if st.id != run_id and run_id not in self.blank_ids(st):
                continue
            st.run.drop_derived(dk)
            st.blank_alignment.pop(base, None)
            if st.results.pop(dk, None) is not None:
                out.append((st.id, dk))
            self._blank_matches = {k: v for k, v in self._blank_matches.items()
                                   if not (k[0] == st.id and k[1] == dk)}
        return out

    def _derived_method(self, st: RunState, key: str, sig) -> IntegrationMethod:
        """The method for a derived trace, with the automatic parameters that were determined on
        the base trace (peak width, smoothing, threshold, absolute slope). Subtracting a blank adds
        its noise; re-estimating the parameters would change the integration of peaks the blank
        does not even touch."""
        m = self.method_for(st, key).copy()
        base = self.result(st.id, base_key(key))
        if base is None:
            return m
        r = base.resolved
        m.peak_width = m.peak_width or r.peak_width
        m.smoothing_window = m.smoothing_window or r.window
        m.threshold = m.threshold if m.threshold is not None else r.threshold
        if not m.slope_sensitivity:
            from gcws.integration.autoparams import resolve
            own = resolve(sig.rt, sig.y, m, self.solvent_cut(st, key))
            m.slope_sensitivity = r.slope_abs() / (own.sigma_d1 or 1e-12)
        return m

    def result(self, run_id: str, key: Optional[str] = None) -> Optional[IntegrationResult]:
        """Integration of ``key`` (default: the working signal); a derived key the run cannot
        build falls back to its base key (see :meth:`effective_key`)."""
        st = self.runs.get(run_id)
        if st is None:
            return None
        key = self.effective_key(st, key or self.signal_key)
        if key not in st.results and st.run.signal(key) is not None:
            self.integrate(run_id, key, emit=False)
        return st.results.get(key)

    def active_result(self) -> Optional[IntegrationResult]:
        return self.result(self.active_id) if self.active_id else None

    def select_peak(self, index: int) -> None:
        self.selected = index
        self.selectionChanged.emit(self.active_id or "", index)

    def selected_peak(self):
        res = self.active_result()
        if res is None or not (0 <= self.selected < len(res.peaks)):
            return None
        return res.peaks[self.selected]

    # -- quantification --------------------------------------------------------

    def schedule_quant(self) -> None:
        self._quant_timer.start()

    def recompute_quant(self) -> None:
        from gcws.quant.service import compute
        self.quant_result = compute(self)
        self.quantChanged.emit()

    def quant_rows(self, run_id: str, key: Optional[str] = None) -> dict:
        """``{peak index: quantification values}`` of ``key``'s peaks (default: working signal).

        The quantification is computed on the raw FID. A blank-subtracted FID trace shows the
        values of the FID peak with the nearest apex (one-to-one, within the RT tolerance);
        MS traces get none."""
        st = self.runs.get(run_id)
        key = self.effective_key(st, key or self.signal_key)
        if self.quant_result is None or st is None or not is_fid(key):
            return {}
        rows = self.quant_result.rows.get(run_id, {})
        if key == FID or not rows:
            return rows
        res, base = self.result(run_id, key), self.result(run_id, FID)
        if res is None or base is None:
            return {}
        from gcws.quant.nias_bridge import make_settings
        tol = float(getattr(make_settings(self.quant.get("settings")), "rt_tolerance", 0.035) or 0.035)
        pairs = sorted((abs(p.apex_rt - q.apex_rt), i, j) for i, p in enumerate(res.peaks)
                       for j, q in enumerate(base.peaks) if abs(p.apex_rt - q.apex_rt) <= tol and j in rows)
        out, used = {}, set()
        for _d, i, j in pairs:
            if i not in out and j not in used:
                out[i] = rows[j]
                used.add(j)
        return out

    def base_peak_index(self, run_id: str, key: str, index: int) -> int:
        """Index of the base-trace peak holding the apex of peak ``index`` of the derived trace ``key``."""
        res, base = self.result(run_id, key), self.result(run_id, base_key(key))
        if res is None or base is None or not (0 <= index < len(res.peaks)) or not base.peaks:
            return -1
        p = res.peaks[index]
        j = min(range(len(base.peaks)), key=lambda k: abs(base.peaks[k].apex_rt - p.apex_rt))
        q = base.peaks[j]
        return j if q.start <= p.apex_rt <= q.end else -1

    def push_quant(self, text: str, new_quant: dict, detail: str = "quantification") -> None:
        """Replace ``self.quant`` as one undoable, audited step.

        Sub-settings that feed derived data (blank subtraction, deconvolution)
        are compared so only what changed is invalidated.
        """
        import copy
        from gcws.ui.undo import ValueCommand

        def setter(v):
            old = self.quant
            self.quant = copy.deepcopy(v)
            self._quant_settings_changed(old, self.quant)
            self.recompute_quant()

        stack = self.undo_group.activeStack() or self.project_undo
        stack.push(ValueCommand(text, lambda: self.quant, setter, new_quant,
                                lambda t, o, n: self.log(t, "", detail)))

    def _quant_settings_changed(self, old: dict, new: dict) -> None:
        """Sub-settings that feed derived data: only what changed is invalidated."""
        def cut(q):
            return bool(q.get("solvent_cut", False)), (q.get("settings") or {}).get("solvent_end", 5.5)
        cut_changed = cut(old or {}) != cut(new or {})
        if cut_changed:
            QSettings().setValue("integration/solvent_cut", bool(new.get("solvent_cut", False)))
            # Snapshot all keys first: base integrations invalidate derived results in other runs.
            keys = {st.id: list(st.results) for st in self.states()}
            for derived in (False, True):
                for st in self.states():
                    for key in keys[st.id]:
                        if is_derived(key) == derived:
                            self.integrate(st.id, key)
        if any((old or {}).get(k) != (new or {}).get(k) for k in ("blank_sub", "istd_defs", "istd_bindings")):
            self.invalidate_blank(None)             # the ISTD windows are kept out of the subtraction
        if cut_changed or (old or {}).get("deconv") != (new or {}).get("deconv"):
            for st in self.states():
                st.deconv = {}
                self.deconvChanged.emit(st.id)
        if cut_changed:
            self.solventCutChanged.emit()

    def quant_unit(self) -> str:
        from gcws.quant.service import mode_unit
        return mode_unit(self.quant)

    def nias_sample(self, run_id: str):
        if self.quant_result is None:
            self.recompute_quant()
        return self.quant_result.samples.get(run_id)

    # -- audit ---------------------------------------------------------------

    def log(self, action: str, run: str = "", detail: str = "", before: str = "",
            after: str = "", reason: str = "") -> None:
        self.audit.add(AuditRecord(action=action, run=run, detail=detail, before=before,
                                   after=after, reason=reason))
        self.dirty = True

    # -- roles and blanks ------------------------------------------------------

    def ordered_ids_by_injection(self) -> list[str]:
        states = self.states()
        if not states:
            return []
        seq = sequence.parse_sequence_log(states[0].run.path.parent)
        return [s.id for s in sorted(states, key=lambda s: sequence.order_key(s.run.path, seq))]

    def _suggest_blanks(self) -> None:
        """Fill empty blank assignments of samples from the injection order.

        Assignments the analyst made (``blanks_manual``) are left alone; for the
        others, a referenced run that no longer has a blank role is dropped first.
        """
        order = self.ordered_ids_by_injection()
        roles = {s.id: s.role for s in self.states()}
        paths = {s.id: s.run.path for s in self.states()}
        for st in self.states():
            if st.role != sequence.SAMPLE or st.blanks_manual:
                continue
            st.blanks = [b for b in st.blanks if roles.get(b) == sequence.BLANK]
            st.blanks_istd = [b for b in st.blanks_istd if roles.get(b) == sequence.BLANK_ISTD]
            b, bi = sequence.suggest_blanks(st.id, paths, roles, order)
            if not st.blanks and b:
                st.blanks = b
            if not st.blanks_istd and bi:
                st.blanks_istd = bi

    # -- blank subtraction ------------------------------------------------------

    def blank_options(self):
        from gcws.signal.blank import BlankOptions
        return BlankOptions.from_dict(self.quant.get("blank_sub"))

    def blank_ids(self, st: RunState) -> list[str]:
        """The blanks subtracted from ``st`` (by the configured source: Blank, Blank+ISTD or both)."""
        src = self.blank_options().source
        ids = (st.blanks if src in ("blank", "both") else []) + (st.blanks_istd if src in ("blank_istd", "both") else [])
        return [b for b in dict.fromkeys(ids) if b in self.runs and b != st.id]

    #: nominal ISTD retention time (definition) -> peak: search window (min), the largest peak wins
    ISTD_TARGET_TOL = 0.1
    #: an explicit ISTD binding (RT of the analyst's peak) -> peak
    ISTD_BOUND_TOL = 0.02

    def istd_peaks(self, run_id: str, key: str) -> dict[int, tuple[float, float]]:
        """``{peak index: (start, end)}`` of the internal standards in ``key``'s peaks of a run.

        From the ISTD table (target RT, or the RT bound by the analyst; "unbound" = none); an
        MS trace takes the peak at the FID time minus the delay. Blank subtraction leaves these
        peaks alone (trace and peak level): an ISTD is in the Blank+ISTD as well."""
        st = self.runs.get(run_id)
        base = base_key(key)
        res = self.result(run_id, base) if st is not None else None
        if res is None or not res.peaks:
            return {}
        import gc_fid
        from gcws.quant.nias_bridge import make_settings
        q = self.quant or {}
        defs = gc_fid.normalise_istd_defs(q["istd_defs"]) if q.get("istd_defs") else \
            gc_fid.default_istd_defs(make_settings(q.get("settings")))
        bound = (q.get("istd_bindings") or {}).get(run_id) or {}
        shift = 0.0 if is_fid(base) else -st.delay_value
        out = {}
        for d in defs:
            code = d.get("code")
            if code in bound:
                if bound[code] is None:
                    continue                            # the analyst: this ISTD is not in the run
                target, tol, largest = float(bound[code]) + shift, self.ISTD_BOUND_TOL, False
            elif d.get("target_rt") is not None:
                target, tol, largest = float(d["target_rt"]) + shift, self.ISTD_TARGET_TOL, True
            else:
                continue
            near = [i for i, p in enumerate(res.peaks) if abs(p.apex_rt - target) <= tol]
            if not near:
                continue
            i = max(near, key=lambda k: res.peaks[k].area) if largest else \
                min(near, key=lambda k: abs(res.peaks[k].apex_rt - target))
            out[i] = (res.peaks[i].start, res.peaks[i].end)
        return out

    def _derive(self, run: Run, key: str):
        """Provider of derived traces: ``"<key> - Blank"`` = sample minus its aligned blank(s)."""
        base, suffix = split_key(key)
        st = self.runs.get(run.id)
        if suffix != BLANK_SUFFIX or st is None:
            return None
        sample = run.signal(base)
        if sample is None:
            return None
        blanks = []
        for b in self.blank_ids(st):
            sig = self.runs[b].run.signal(base)
            if sig is not None:
                bres = self.result(b, base)
                blanks.append((self.runs[b].name, sig, [p.apex_rt for p in bres.peaks] if bres else None))
        if not blanks:
            return None
        from gcws.integration.autoparams import _integration_start
        from gcws.signal import blank as B
        opts = self.blank_options()
        t_from = _integration_start(sample.rt, self.method_for(st, base))
        sres = self.result(st.id, base)
        sig, aligns = B.subtract(sample, blanks, opts, opts.mode_fid if is_fid(base) else opts.mode_ms, key, t_from,
                                 [p.apex_rt for p in sres.peaks] if sres else None,
                                 protect=list(self.istd_peaks(st.id, base).values()))
        st.blank_alignment[base] = aligns
        return sig

    def invalidate_blank(self, run_ids=None) -> None:
        """Forget blank-subtracted traces, their integrations and the peak blank matches
        of the runs in ``run_ids`` and of every sample using one of them as blank
        (``None``: all runs). Views are told through ``runChanged``."""
        ids = set(run_ids) if run_ids is not None else None
        affected = [st for st in self.states()
                    if ids is None or st.id in ids or ids & set(st.blanks + st.blanks_istd)]
        for st in affected:
            st.run.drop_derived()
            st.blank_alignment.clear()
            for key in [k for k in st.results if is_derived(k)]:
                st.results.pop(key, None)
            self._blank_matches = {k: v for k, v in self._blank_matches.items() if k[0] != st.id}
        for st in affected:
            if is_derived(self.signal_key) or st.blanks or st.blanks_istd:
                self.runChanged.emit(st.id)

    def _drop_matches(self, run_id: str, key: str) -> None:
        users = {s.id for s in self.states() if run_id in s.blanks + s.blanks_istd} | {run_id}
        self._blank_matches = {k: v for k, v in self._blank_matches.items() if k[0] not in users}

    def blank_matches(self, run_id: str, key: Optional[str] = None) -> dict:
        """``{peak index: BlankMatch}`` of ``run_id``'s peaks found in its blank(s) (cached)."""
        st = self.runs.get(run_id)
        key = self.effective_key(st, key or self.signal_key)
        ck = (run_id, key)
        if ck in self._blank_matches:
            return self._blank_matches[ck]
        if is_derived(key):
            # the subtracted trace has lost its blank part: judge each peak by its base peak
            base = self.blank_matches(run_id, base_key(key))
            res = self.result(run_id, key)
            out = {}
            for i in range(len(res.peaks) if res is not None else 0):
                j = self.base_peak_index(run_id, key, i)
                if j in base:
                    out[i] = base[j]
            self._blank_matches[ck] = out
            return out
        res = self.result(run_id, key) if st is not None else None
        out: dict = {}
        if st is None or res is None or not self.blank_ids(st):
            self._blank_matches[ck] = out
            return out
        from gcws.quant import blank_match as BM
        from gcws.signal import blank as B
        opts = self.blank_options()
        base = base_key(key)
        if opts.rt_tol:
            rt_tol = float(opts.rt_tol)
        elif is_fid(base):
            from gcws.quant.nias_bridge import make_settings
            rt_tol = float(getattr(make_settings(self.quant.get("settings")), "blank_rt_tolerance", 0.04) or 0.04)
        else:
            rt_tol = 0.03
        sample_sig = st.run.signal(base)
        for b in self.blank_ids(st):
            bst = self.runs[b]
            bres = self.result(b, base)
            bsig = bst.run.signal(base)
            if bres is None or bsig is None or sample_sig is None:
                continue
            known = [a for a in st.blank_alignment.get(base, []) if a.blank == bst.name]
            shift = known[0].shift if known else B.align(
                sample_sig, bsig, opts, None, ([p.apex_rt for p in res.peaks], [p.apex_rt for p in bres.peaks]))[0]
            spectra = self._pair_spectra(st, res, key, bst, bres, base) if (st.run.ms is not None
                                                                           and bst.run.ms is not None) else None
            m = BM.match(res.peaks, bres.peaks, shift=shift, rt_tol=rt_tol, blank_run=b, scale=opts.scale,
                         ratio_limit=opts.ratio_limit, spectra=spectra, spectral_min=opts.spectral_min)
            istd = self.istd_peaks(run_id, key)          # never "in blank", hidden or greyed out
            for i, bm in m.items():
                if i in istd:
                    continue
                if i not in out or bm.ratio < out[i].ratio:
                    out[i] = bm
        self._blank_matches[ck] = out
        return out

    def _pair_spectra(self, st, res, key, bst, bres, bkey):
        from gcws.ms.similarity import cosine
        from gcws.ms.spectra import extract
        cache: dict = {}

        def spec(s, peaks, k, i):
            ck = (s.id, k, i)
            if ck not in cache:
                p = peaks[i]
                sp = extract(s.run, p, base_key(k), s.delay_value, "average_bg",
                             override=s.spectrum_overrides.get(round(p.apex_rt, 4)))
                cache[ck] = sp if sp is not None and sp.ab.size else None
            return cache[ck]

        def cos(i, j):
            a, b = spec(st, res.peaks, key, i), spec(bst, bres.peaks, bkey, j)
            return cosine(a, b) if a is not None and b is not None else None
        return cos

    def blank_shifts(self, st: RunState, base: str = TIC) -> list[tuple[str, float]]:
        """``[(blank id, shift)]`` aligning each blank of ``st`` on the ``base`` trace (cached)."""
        from gcws.signal import blank as B
        from gcws.signal.align import Alignment
        known = {a.blank: a.shift for a in st.blank_alignment.get(base, [])}
        sample = st.run.signal(base)
        out, new = [], []
        for b in self.blank_ids(st):
            bst = self.runs[b]
            if bst.name in known:
                out.append((b, known[bst.name]))
                continue
            bsig = bst.run.signal(base)
            if sample is None or bsig is None:
                continue
            sres, bres = self.result(st.id, base), self.result(b, base)
            apexes = ([p.apex_rt for p in sres.peaks], [p.apex_rt for p in bres.peaks]) if sres and bres else None
            shift, quality, method = B.align(sample, bsig, self.blank_options(), None, apexes)
            out.append((b, shift))
            new.append(Alignment(shift, quality, method, bst.name))
        if new:
            st.blank_alignment.setdefault(base, []).extend(new)
        return out

    def blank_spectrum(self, st: RunState, scans) -> tuple:
        """Mean spectrum of ``st``'s blanks at the times of ``scans`` (aligned, scaled): (mz, ab)."""
        import numpy as np
        ms = st.run.ms
        parts = []
        for b, shift in self.blank_shifts(st, TIC):
            bms = self.runs[b].run.ms
            if bms is None or ms is None:
                continue
            bscans = sorted({bms.scan_at_rt(float(ms.rt[s]) - shift) for s in scans})
            parts.append(bms.nominal_spectrum_arrays(bscans))
        if not parts:
            return np.zeros(0, np.int64), np.zeros(0)
        masses = np.unique(np.concatenate([p[0] for p in parts]))
        acc = np.zeros(masses.size)
        for mz, ab in parts:
            acc[np.searchsorted(masses, mz)] += ab
        return masses, acc / len(parts) * self.blank_options().scale

    def blank_level_peaks(self, run_id: str, key: Optional[str] = None) -> set[int]:
        """Peaks at blank level (a derived trace: judged by the matching base peak)."""
        st = self.runs.get(run_id)
        if st is None or not self.blank_ids(st):
            return set()
        return {i for i, m in self.blank_matches(run_id, key).items() if m.status == "blank"}

    def suggest_replicate_groups(self) -> list[list[str]]:
        names = {s.id: s.run.path.name for s in self.states()}
        return sequence.suggest_replicates(names)
