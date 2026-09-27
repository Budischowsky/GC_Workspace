"""Undoable, audited commands on the workspace."""
from __future__ import annotations

import copy
from typing import Any, Callable, Optional

from PySide6.QtGui import QUndoCommand

from gcws.core.events import ManualEvent
from gcws.core.ident import Identification
from gcws.core.keys import method_kind


def _summary(res) -> str:
    if res is None:
        return ""
    return f"{len(res.peaks)} peaks, total area {sum(p.area for p in res.peaks):.6g}"


class ManualEventsCommand(QUndoCommand):
    """Replace the manual event list of one run/signal (add, remove, toggle, edit)."""

    def __init__(self, ws, run_id: str, key: str, new_events: list[ManualEvent], text: str,
                 reason: str = ""):
        super().__init__(text)
        self.ws, self.run_id, self.key, self.reason = ws, run_id, key, reason
        st = ws.runs[run_id]
        self.old = list(st.events(key))
        self.new = list(new_events)

    def _set(self, events, label):
        st = self.ws.runs.get(self.run_id)
        if st is None:
            return
        before = _summary(st.results.get(self.key))
        st.manual[self.key] = list(events)
        res = self.ws.integrate(self.run_id, self.key)
        self.ws.log(label, st.name, f"{self.key}: {self.text()}", before, _summary(res), self.reason)

    def redo(self):
        self._set(self.new, "Manual integration")

    def undo(self):
        self._set(self.old, "Undo manual integration")


def add_event(ws, run_id: str, key: str, event: ManualEvent, reason: str = "") -> ManualEventsCommand:
    st = ws.runs[run_id]
    return ManualEventsCommand(ws, run_id, key, st.events(key) + [event], event.describe(), reason)


class SetMethodCommand(QUndoCommand):
    def __init__(self, ws, run_ids: list[str], kind: str, method, text: str):
        super().__init__(text)
        self.ws, self.run_ids, self.kind = ws, list(run_ids), kind
        self.new = method.copy()
        self.old = {rid: ws.runs[rid].methods.get(kind) for rid in run_ids}

    def _apply(self, getter, label):
        for rid in self.run_ids:
            st = self.ws.runs.get(rid)
            if st is None:
                continue
            before = _summary(st.results.get(self.ws.effective_key(st)))
            m = getter(rid)
            st.methods[self.kind] = m.copy() if m is not None else self.ws.methods.get(
                self.ws.methods.default_name(self.kind))
            for key in list(st.results):
                if method_kind(key) == self.kind:
                    self.ws.integrate(rid, key)
            self.ws.methodChanged.emit(rid)
            self.ws.log(label, st.name, f"{self.kind}: {st.methods[self.kind].name}",
                        before, _summary(st.results.get(self.ws.effective_key(st))))

    def redo(self):
        self._apply(lambda rid: self.new, "Integration method applied")

    def undo(self):
        self._apply(lambda rid: self.old.get(rid), "Undo integration method")


class IdentCommand(QUndoCommand):
    """Set/replace identifications (by apex RT) of one run/signal as one step."""

    def __init__(self, ws, run_id: str, key: str, changes: list[tuple[float, Optional[Identification]]],
                 text: str):
        super().__init__(text)
        self.ws, self.run_id, self.key = ws, run_id, key
        st = ws.runs[run_id]
        self.old_items = copy.deepcopy(st.ident_set(key).items)
        items = copy.deepcopy(st.ident_set(key).items)
        from gcws.core.ident import IdentificationSet
        from gcws.ms.assignment import fragment_id
        tmp = IdentificationSet(items)
        res = ws.result(run_id, key)
        for rt, ident in changes:
            peak = next((p for p in res.peaks if abs(p.apex_rt - rt) < 1e-8), None) if res else None
            identity = fragment_id(peak)
            if ident is None:
                tmp.remove_at(rt, peak_id=identity)
            else:
                ident = copy.deepcopy(ident)
                # A search already bound to a fragment must not migrate after re-integration.
                ident.peak_id = ident.peak_id or identity
                tmp.set(ident)
        self.new_items = tmp.items
        self.count = len(changes)

    def _set(self, items, label):
        st = self.ws.runs.get(self.run_id)
        if st is None:
            return
        st.ident_set(self.key).items = copy.deepcopy(items)
        self.ws.log(label, st.name, f"{self.key}: {self.text()}")
        self.ws.identsChanged.emit(self.run_id, self.key)

    def redo(self):
        self._set(self.new_items, "Identification")

    def undo(self):
        self._set(self.old_items, "Undo identification")


class ValueCommand(QUndoCommand):
    """Generic property change with getter/setter (roles, blanks, delay, groups...)."""

    def __init__(self, text: str, getter: Callable[[], Any], setter: Callable[[Any], None],
                 new: Any, log: Optional[Callable[[str, Any, Any], None]] = None):
        super().__init__(text)
        self.getter, self.setter = getter, setter
        self.old = copy.deepcopy(getter())
        self.new = copy.deepcopy(new)
        self.logfn = log

    def redo(self):
        self.setter(copy.deepcopy(self.new))
        if self.logfn:
            self.logfn(self.text(), self.old, self.new)

    def undo(self):
        self.setter(copy.deepcopy(self.old))
        if self.logfn:
            self.logfn("Undo " + self.text(), self.new, self.old)


class MultiCommand(QUndoCommand):
    """Several commands (e.g. on different runs) as one undo step."""

    def __init__(self, text: str, commands: list):
        super().__init__(text)
        self.commands = list(commands)

    def redo(self):
        for c in self.commands:
            c.redo()

    def undo(self):
        for c in reversed(self.commands):
            c.undo()
