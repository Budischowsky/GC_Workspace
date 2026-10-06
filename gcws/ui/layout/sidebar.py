"""Collapse panels to a strip on their side of the window, one panel at a time.

Folders has its own button (◀) and, while collapsed, shows the loaded samples as
squares on the left strip. Any other docked panel collapses from its right-click
menu: it is hidden and a button with its name waits on the strip of its side.
"""
from PySide6.QtCore import QObject, QSettings, QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QToolBar, QToolButton

L, R, T, B = Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea, Qt.TopDockWidgetArea, Qt.BottomDockWidgetArea
_TOOLBAR_AREA = {L: Qt.LeftToolBarArea, R: Qt.RightToolBarArea, T: Qt.TopToolBarArea, B: Qt.BottomToolBarArea}
_STRIP_NAMES = {L: "tb.folder_restore", R: "tb.collapsed_right", T: "tb.collapsed_top", B: "tb.collapsed_bottom"}


class RestoreButton(QToolButton):
    """A strip button; on the left and right strips its text runs downwards."""

    def __init__(self, text="Folders ›", vertical=True, parent=None):
        super().__init__(parent)
        self.label = text
        self.vertical = vertical
        self.setAutoRaise(True)
        if not vertical:
            self.setText(text)

    def sizeHint(self):
        w = self.fontMetrics().horizontalAdvance(self.label) + 20
        return QSize(28, max(100, w)) if self.vertical else QSize(w, 24)

    def paintEvent(self, event):
        if not self.vertical:
            return super().paintEvent(event)
        super().paintEvent(event)                    # hover background
        painter = QPainter(self)
        painter.translate(self.width(), 0)
        painter.rotate(90)
        painter.setPen(self.palette().buttonText().color())
        painter.drawText(0, 0, self.height(), self.width(), Qt.AlignCenter, self.label)


class SidebarController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.collapsed = False                       # Folders
        self.members = []
        self.front = []
        self.widths = []
        self.panels: dict[str, dict] = {}            # objectName -> {"area", "size", "action"}
        self.strips: dict = {}
        for area, name in _STRIP_NAMES.items():
            strip = QToolBar("Folders" if area == L else "Collapsed panels", win)
            strip.setObjectName(name)
            strip.setMovable(False)
            strip.setFloatable(False)
            win.addToolBar(_TOOLBAR_AREA[area], strip)
            strip.hide()
            self.strips[area] = strip
        self.strip = self.strips[L]
        self.restore_button = RestoreButton()
        self.restore_button.setFixedSize(28, 100)
        self.restore_button.setToolTip("Expand Folders")
        self.restore_button.clicked.connect(self.expand)
        self._folder_actions = [self.strip.addWidget(self.restore_button)]
        from gcws.ui.layout.sample_rail import SampleRail
        self.rail = SampleRail(win.ws, win.loaded_samples.run_menu)     # loaded samples stay one click away
        self._folder_actions.append(self.strip.addWidget(self.rail))
        self.button = QToolButton()
        self.button.setText("◀")
        self.button.setToolTip("Collapse Folders")
        self.button.clicked.connect(self.collapse)
        win.docks["tree"].titleBarWidget().layout().insertWidget(1, self.button)
        win.docks["tree"].topLevelChanged.connect(lambda floating: self.button.setEnabled(not floating))
        for dock in win.docks.values():
            dock.toggleViewAction().triggered.connect(lambda on, d=dock: self._revealed(d, on))
        self._update_strips()

    def _update_strips(self):
        for a in self._folder_actions:
            a.setVisible(self.collapsed)
        for area, strip in self.strips.items():
            used = any(p["area"] == area for p in self.panels.values()) or (area == L and self.collapsed)
            strip.setVisible(used)

    def contains(self, dock):
        return (self.collapsed and dock is self.win.docks["tree"]) or dock.objectName() in self.panels

    def reveal(self, dock):
        """Bring a collapsed panel back (from the View menu, a key or a command)."""
        if dock is self.win.docks["tree"]:
            self.expand()
        elif dock.objectName() in self.panels:
            self.expand_panel(dock)

    def _revealed(self, dock, on):
        if on and self.contains(dock):
            self.reveal(dock)
            dock.show()
            dock.raise_()

    # -- Folders ---------------------------------------------------------------------

    def collapse(self):
        if self.collapsed:
            return
        dock = self.win.docks["tree"]
        if dock.isFloating() or dock.isHidden():
            return
        self.members = [dock.objectName()]
        self.widths = [dock.width()]
        self.front = [dock.objectName()]
        self.collapsed = True
        dock.hide()
        self._update_strips()

    def expand(self):
        if not self.collapsed:
            self._update_strips()
            return
        self.collapsed = False
        self._update_strips()
        dock = self.win.docks["tree"]
        shared = any(not other.isHidden() for other in self.win.tabifiedDockWidgets(dock))
        dock.show()
        if not shared and not dock.isFloating() and self.widths:
            self.win.resizeDocks([dock], [int(self.widths[0])], Qt.Horizontal)
        dock.raise_()

    # -- any other panel ---------------------------------------------------------------

    def collapse_panel(self, dock, area=None, size=None):
        """Hide a docked panel behind a button on the strip of its side."""
        if dock is self.win.docks["tree"]:
            self.collapse()
            return
        name = dock.objectName()
        if name in self.panels or (area is None and (dock.isFloating() or dock.isHidden())):
            return
        if area is None:
            area = self.win.dockWidgetArea(dock)
            size = dock.width() if area in (L, R) else dock.height()
        if area not in self.strips:
            return
        title = dock.windowTitle()
        label = {L: f"{title} ›", R: f"‹ {title}", T: f"{title} ▾", B: f"{title} ▴"}[area]
        button = RestoreButton(label, vertical=area in (L, R))
        button.setToolTip(f"Expand {title}")
        button.clicked.connect(lambda: self.expand_panel(dock))
        if area == L:                                # above Folders and its sample squares
            action = self.strip.insertWidget(self._folder_actions[0], button)
        else:
            action = self.strips[area].addWidget(button)
        self.panels[name] = {"area": area, "size": int(size or 0), "action": action}
        dock.hide()
        self._update_strips()

    def expand_panel(self, dock):
        entry = self.panels.pop(dock.objectName(), None)
        if entry is None:
            return
        strip = self.strips[entry["area"]]
        button = strip.widgetForAction(entry["action"])
        strip.removeAction(entry["action"])
        if button is not None:
            button.deleteLater()
        self._update_strips()
        shared = any(not other.isHidden() for other in self.win.tabifiedDockWidgets(dock))
        dock.show()
        if not shared and not dock.isFloating() and entry["size"] > 0:
            orientation = Qt.Horizontal if entry["area"] in (L, R) else Qt.Vertical
            self.win.resizeDocks([dock], [entry["size"]], orientation)
        dock.raise_()

    def expand_all(self):
        self.expand()
        by_name = {d.objectName(): d for d in self.win.docks.values()}
        for name in list(self.panels):
            if name in by_name:
                self.expand_panel(by_name[name])
            else:
                self.panels.pop(name)
        self._update_strips()

    # -- persistence -------------------------------------------------------------------

    def save(self, prefix):
        s = QSettings()
        s.setValue(prefix + "/sidebar_collapsed", self.collapsed)
        s.setValue(prefix + "/sidebar_scope", "folder")
        s.setValue(prefix + "/sidebar_members", self.members)
        s.setValue(prefix + "/sidebar_front", self.front)
        s.setValue(prefix + "/sidebar_widths", self.widths)
        s.setValue(prefix + "/sidebar_panels",
                   [f"{name}|{p['area'].value}|{p['size']}" for name, p in self.panels.items()])

    def _forget_panels(self):
        for entry in self.panels.values():
            strip = self.strips[entry["area"]]
            button = strip.widgetForAction(entry["action"])
            strip.removeAction(entry["action"])
            if button is not None:
                button.deleteLater()
        self.panels.clear()

    def restore(self, prefix):
        s = QSettings()
        self.collapsed = s.value(prefix + "/sidebar_collapsed", False, type=bool)
        self.members = s.value(prefix + "/sidebar_members", []) or []
        self.front = s.value(prefix + "/sidebar_front", []) or []
        self.widths = s.value(prefix + "/sidebar_widths", []) or []
        if self.collapsed and s.value(prefix + "/sidebar_scope", "") != "folder":
            # Layouts saved by the previous UI hid the entire group. Recover its
            # other panels once, then keep only Folders collapsed.
            for dock in self.win.docks.values():
                if dock.objectName() in self.members and dock is not self.win.docks["tree"]:
                    dock.show()
            tree_name = self.win.docks["tree"].objectName()
            index = self.members.index(tree_name) if tree_name in self.members else -1
            self.widths = self.widths[index:index + 1] if index >= 0 else []
        self.members = [self.win.docks["tree"].objectName()] if self.collapsed else []
        self.front = self.members[:]
        if self.collapsed:
            self.win.docks["tree"].hide()
        self._forget_panels()
        by_name = {d.objectName(): d for d in self.win.docks.values()}
        entries = s.value(prefix + "/sidebar_panels", []) or []
        if isinstance(entries, str):                 # QSettings returns a single entry as a string
            entries = [entries]
        for entry in entries:
            try:
                name, area, size = entry.split("|")
                area = Qt.DockWidgetArea(int(area))
            except (ValueError, TypeError):
                continue
            if name in by_name:
                self.collapse_panel(by_name[name], area, int(size))
        self._update_strips()
