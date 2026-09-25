"""Tab bar with one colour-coded tab per loaded chromatogram."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog, QInputDialog, QMenu, QTabBar

from gcws.io.sequence import ROLE_LABELS
from gcws.ui import theme
from gcws.ui.icons import color_chip


class RunTabBar(QTabBar):
    roleRequested = QtSignal(str, str)          # run id, role
    blanksRequested = QtSignal(str)
    replicateRequested = QtSignal(str)
    closeRequested = QtSignal(str)
    revealRequested = QtSignal(str)
    eicRequested = QtSignal()

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setExpanding(False)
        self.setElideMode(Qt.ElideMiddle)
        self.setDocumentMode(True)
        self.setUsesScrollButtons(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.tabCloseRequested.connect(lambda i: self.closeRequested.emit(self.tabData(i)))
        self.currentChanged.connect(self._current)
        self.tabMoved.connect(lambda *_: ws.reorder([self.tabData(i) for i in range(self.count())]))
        self._updating = False
        ws.runAdded.connect(self._added)
        ws.runRemoved.connect(self._removed)
        ws.runChanged.connect(self._changed)
        ws.activeRunChanged.connect(self._active)
        self.setObjectName("runTabs")

    def _index(self, run_id):
        for i in range(self.count()):
            if self.tabData(i) == run_id:
                return i
        return -1

    def _label(self, st) -> str:
        role = "" if st.role == "sample" else f"  [{ROLE_LABELS.get(st.role, st.role)}]"
        return st.name + role

    def _added(self, run_id):
        st = self.ws.runs[run_id]
        self._updating = True
        pos = self.ws.index_of(run_id)
        i = self.insertTab(pos if 0 <= pos <= self.count() else self.count(), color_chip(st.color), self._label(st))
        self.setTabData(i, run_id)
        self.setTabToolTip(i, f"{st.name}\n{st.run.path}")
        self.setTabTextColor(i, QColor(theme.TEXT))
        self._updating = False

    def _removed(self, run_id):
        i = self._index(run_id)
        if i >= 0:
            self._updating = True
            self.removeTab(i)
            self._updating = False

    def _changed(self, run_id):
        i = self._index(run_id)
        st = self.ws.runs.get(run_id)
        if i >= 0 and st is not None:
            self.setTabIcon(i, color_chip(st.color))
            self.setTabText(i, self._label(st))
            self.setTabTextColor(i, QColor(theme.TEXT) if st.visible else QColor(theme.FAINT))

    def _active(self, run_id):
        i = self._index(run_id)
        if i >= 0 and i != self.currentIndex():
            self._updating = True
            self.setCurrentIndex(i)
            self._updating = False

    def _current(self, i):
        if self._updating or i < 0:
            return
        self.ws.set_active(self.tabData(i))

    def _menu(self, pos):
        i = self.tabAt(pos)
        if i < 0:
            return
        rid = self.tabData(i)
        st = self.ws.runs[rid]
        m = QMenu(self)
        roles = m.addMenu("Role")
        for role, label in ROLE_LABELS.items():
            a = roles.addAction(label)
            a.setCheckable(True)
            a.setChecked(st.role == role)
            a.triggered.connect(lambda _=False, r=role: self.roleRequested.emit(rid, r))
        m.addAction("Assign blanks...").triggered.connect(lambda: self.blanksRequested.emit(rid))
        m.addAction("Replicate group...").triggered.connect(lambda: self.replicateRequested.emit(rid))
        m.addSeparator()
        sig = m.addMenu("Signal")
        for key in st.run.available_signals():
            a = sig.addAction(key)
            a.setCheckable(True)
            a.setChecked(self.ws.signal_key == key)
            a.triggered.connect(lambda _=False, k=key: self.ws.set_signal_key(k))
        if st.run.ms is not None:
            sig.addAction("Extracted ion (EIC)...").triggered.connect(self.eicRequested.emit)
        vis = m.addAction("Show in overlay")
        vis.setCheckable(True)
        vis.setChecked(st.visible)
        vis.toggled.connect(lambda on: self._visible(rid, on))
        m.addAction("Colour...").triggered.connect(lambda: self._color(rid))
        m.addAction("Rename tab...").triggered.connect(lambda: self._rename(rid))
        m.addSeparator()
        m.addAction("Show in folder tree").triggered.connect(lambda: self.revealRequested.emit(rid))
        m.addAction("Close").triggered.connect(lambda: self.closeRequested.emit(rid))
        others = [r for r in self.ws.order if r != rid]
        if others:
            m.addAction("Close others").triggered.connect(lambda: [self.closeRequested.emit(r) for r in others])
        m.exec(self.mapToGlobal(pos))

    def _visible(self, rid, on):
        self.ws.runs[rid].visible = on
        self.ws.runChanged.emit(rid)

    def _color(self, rid):
        st = self.ws.runs[rid]
        c = QColorDialog.getColor(QColor(st.color), self, f"Colour of {st.name}")
        if c.isValid():
            st.color = c.name()
            self.ws.runChanged.emit(rid)

    def _rename(self, rid):
        st = self.ws.runs[rid]
        text, ok = QInputDialog.getText(self, "Rename", "Tab name:", text=st.name)
        if ok and text.strip():
            st.run.meta.sample_name = text.strip()
            self.ws.runChanged.emit(rid)
