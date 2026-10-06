"""Colour-coded loaded samples and their workspace actions."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog, QInputDialog, QMenu, QListWidget, QListWidgetItem, QAbstractItemView

from gcws.io.sequence import ROLE_LABELS
from gcws.ui import theme
from gcws.ui.icons import color_chip


class LoadedSamples(QListWidget):
    roleRequested = QtSignal(str, str)          # run id, role
    blanksRequested = QtSignal(str)
    replicateRequested = QtSignal(str)
    pairRequested = QtSignal(str, str)          # run id, partner id
    closeRequested = QtSignal(str)
    revealRequested = QtSignal(str)
    eicRequested = QtSignal()

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setTextElideMode(Qt.ElideMiddle)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.currentRowChanged.connect(self._current)
        self._updating = False
        for signal in (ws.runAdded, ws.runRemoved, ws.runChanged, ws.orderChanged):
            signal.connect(self.sync)
        ws.activeRunChanged.connect(self._active)
        theme.notifier().changed.connect(self.sync)
        self.setObjectName("loadedSamples")
        self.sync()

    def _index(self, run_id):
        for i in range(self.count()):
            if self.item(i).data(Qt.UserRole) == run_id:
                return i
        return -1

    def sync(self, *_):
        self._updating = True
        self.clear()
        for st in self.ws.states():
            item = QListWidgetItem(color_chip(st.color), self._label(st))
            item.setData(Qt.UserRole, st.id)
            item.setToolTip(f"{st.name}\n{st.run.path}")
            item.setForeground(QColor(theme.TEXT if st.visible else theme.FAINT))
            self.addItem(item)
        self.setCurrentRow(self._index(self.ws.active_id))
        self._updating = False

    def dropEvent(self, event):
        self._updating = True
        super().dropEvent(event)
        self._updating = False
        self.ws.reorder([self.item(i).data(Qt.UserRole) for i in range(self.count())])

    def _label(self, st) -> str:
        role = "" if st.role == "sample" else f"  [{ROLE_LABELS.get(st.role, st.role)}]"
        return st.name + role

    def _active(self, run_id):
        self._updating = True
        self.setCurrentRow(self._index(run_id))
        self._updating = False

    def _current(self, i):
        if not self._updating and i >= 0:
            self.ws.set_active(self.item(i).data(Qt.UserRole))

    def _menu(self, pos):
        item = self.itemAt(pos)
        if item is not None:
            self.run_menu(item.data(Qt.UserRole)).exec(self.viewport().mapToGlobal(pos))

    def run_menu(self, rid) -> QMenu:
        """The right-click menu of one loaded run (also used by the squares of the collapsed Folders strip)."""
        st = self.ws.runs[rid]
        m = QMenu(self)
        roles = m.addMenu("Role")
        for role, label in ROLE_LABELS.items():
            a = roles.addAction(label)
            a.setCheckable(True)
            a.setChecked(st.role == role)
            a.triggered.connect(lambda _=False, r=role: self.roleRequested.emit(rid, r))
        m.addAction("Assign blanks...").triggered.connect(lambda: self.blanksRequested.emit(rid))
        if st.role in ("sample", "standard"):
            from gcws.quant.duplicate_view import suggest_partner
            dd = m.addMenu("Double determination with")
            partner = suggest_partner(self.ws, rid)
            others = [s for s in self.ws.states() if s.id != rid and s.role in ("sample", "standard")]
            others.sort(key=lambda s: s.id != partner)
            for o in others:
                a = dd.addAction(color_chip(o.color), o.name + ("   (suggested)" if o.id == partner else ""))
                a.triggered.connect(lambda _=False, p=o.id: self.pairRequested.emit(rid, p))
            if not others:
                dd.addAction("no other sample loaded").setEnabled(False)
            m.addAction("Double determination / replicates...").triggered.connect(
                lambda: self.replicateRequested.emit(rid))
        m.addSeparator()
        sig = m.addMenu("Signal in Chromatogram 1")
        for key in st.run.available_signals():
            a = sig.addAction(key)
            a.setCheckable(True)
            a.setChecked(self.ws.panel_keys[0] == key)
            a.triggered.connect(lambda _=False, k=key: self.ws.set_panel(0, key=k))
        if st.run.ms is not None:
            sig.addAction("Extracted ion (EIC)...").triggered.connect(self.eicRequested.emit)
        vis = m.addAction("Show in overlay")
        vis.setCheckable(True)
        vis.setChecked(st.visible)
        vis.toggled.connect(lambda on: self._visible(rid, on))
        m.addAction("Colour...").triggered.connect(lambda: self._color(rid))
        m.addAction("Rename sample...").triggered.connect(lambda: self._rename(rid))
        m.addSeparator()
        m.addAction("Show in folder tree").triggered.connect(lambda: self.revealRequested.emit(rid))
        m.addAction("Close").triggered.connect(lambda: self.closeRequested.emit(rid))
        others = [r for r in self.ws.order if r != rid]
        if others:
            m.addAction("Close others").triggered.connect(lambda: [self.closeRequested.emit(r) for r in others])
        return m

    def _visible(self, rid, on):
        self.ws.runs[rid].visible = on
        self.ws.dirty = True
        self.ws.runChanged.emit(rid)

    def _color(self, rid):
        st = self.ws.runs[rid]
        c = QColorDialog.getColor(QColor(st.color), self, f"Colour of {st.name}")
        if c.isValid():
            st.color = c.name()
            self.ws.dirty = True
            self.ws.runChanged.emit(rid)

    def _rename(self, rid):
        st = self.ws.runs[rid]
        text, ok = QInputDialog.getText(self, "Rename", "Sample name:", text=st.name)
        if ok and text.strip():
            st.run.meta.sample_name = text.strip()
            self.ws.dirty = True
            self.ws.runChanged.emit(rid)
