"""Workspace controller: loaded runs, their integrations and identifications.

Holds no widgets. Docks observe its signals and change state through undo
commands (``gcws.ui.undo``), so every change is undoable and audited.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal as QtSignal
from PySide6.QtGui import QUndoGroup, QUndoStack

from gcws.core.audit import AuditLog, AuditRecord
from gcws.core.events import ManualEvent
from gcws.core.ident import IdentificationSet
from gcws.core.model import FID, TIC, Run, parse_key
from gcws.integration.engine import IntegrationResult, integrate
from gcws.integration.method import IntegrationMethod
from gcws.integration.store import MethodStore
from gcws.io import sequence
from gcws.signal.delay import DelayEstimate, estimate_delay, refine_with_peaks

PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf",
           "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#393b79", "#637939",
           "#8c6d31", "#843c39", "#7b4173", "#3182bd"]


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
        return self.idents.setdefault(key, IdentificationSet())


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
    message = QtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs: dict[str, RunState] = {}
        self.order: list[str] = []
        self.active_id: Optional[str] = None
        self.signal_key: str = FID
        self.selected: int = -1
        self.methods = MethodStore()
        self.audit = AuditLog()
        self.undo_group = QUndoGroup(self)
        self.project_undo = QUndoStack(self)
        self.undo_group.addStack(self.project_undo)
        self.project_path: Optional[Path] = None
        self.replicate_groups: list[dict] = []
        self.quant: dict = {}
        self.dirty = False

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
        for kind in (FID, TIC):
            st.methods[kind] = self.methods.get(self.methods.default_name(kind))
        if results:
            st.results.update(results)
        self.runs[run.id] = st
        self.order.append(run.id)
        self._sort_order()
        if self.signal_key not in run.available_signals() and not self.runs_with(self.signal_key):
            avail = run.available_signals()
            if avail:
                self.signal_key = avail[0]
        self.dirty = True
        self.runAdded.emit(run.id)
        if self.active_id is None:
            self.set_active(run.id)
        self._suggest_blanks()
        return st

    def remove_run(self, run_id: str) -> None:
        st = self.runs.pop(run_id, None)
        if st is None:
            return
        self.order.remove(run_id)
        if st.undo is not None:
            self.undo_group.removeStack(st.undo)
        for other in self.runs.values():
            other.blanks = [b for b in other.blanks if b != run_id]
            other.blanks_istd = [b for b in other.blanks_istd if b != run_id]
        for g in self.replicate_groups:
            g["members"] = [m for m in g["members"] if m != run_id]
        self.dirty = True
        self.runRemoved.emit(run_id)
        if self.active_id == run_id:
            self.set_active(self.order[0] if self.order else None)

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

    def set_signal_key(self, key: str) -> None:
        key = key.strip()
        if not key or key == self.signal_key:
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
        kind = FID if parse_key(key)[0] == FID else TIC
        m = st.methods.get(kind)
        if m is None:
            m = self.methods.get(self.methods.default_name(kind))
            st.methods[kind] = m
        return m

    def integrate(self, run_id: str, key: Optional[str] = None, emit: bool = True) -> Optional[IntegrationResult]:
        st = self.runs.get(run_id)
        if st is None:
            return None
        key = key or self.signal_key
        sig = st.run.signal(key)
        if sig is None:
            st.results.pop(key, None)
            return None
        res = integrate(sig, self.method_for(st, key), st.events(key))
        st.results[key] = res
        if key == FID and st.run.ms is not None and st.delay is not None and st.delay_override is None:
            tic = st.results.get(TIC)
            if tic is not None:
                st.delay = refine_with_peaks(st.delay, [p.apex_rt for p in res.peaks],
                                             [p.apex_rt for p in tic.peaks])
        if emit:
            self.resultChanged.emit(run_id, key)
        return res

    def result(self, run_id: str, key: Optional[str] = None) -> Optional[IntegrationResult]:
        st = self.runs.get(run_id)
        if st is None:
            return None
        key = key or self.signal_key
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
        """Fill empty blank assignments of samples from the injection order."""
        order = self.ordered_ids_by_injection()
        roles = {s.id: s.role for s in self.states()}
        paths = {s.id: s.run.path for s in self.states()}
        for st in self.states():
            if st.role != sequence.SAMPLE:
                continue
            b, bi = sequence.suggest_blanks(st.id, paths, roles, order)
            if not st.blanks and b:
                st.blanks = b
            if not st.blanks_istd and bi:
                st.blanks_istd = bi

    def suggest_replicate_groups(self) -> list[list[str]]:
        names = {s.id: s.run.path.name for s in self.states()}
        return sequence.suggest_replicates(names)
