"""Collapse only Folders, leaving every other dock untouched."""
from PySide6.QtCore import QObject, QSettings, QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QToolBar, QToolButton


class RestoreButton(QToolButton):
    def sizeHint(self):
        return QSize(28, 100)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.translate(self.width(), 0)
        painter.rotate(90)
        painter.setPen(self.palette().buttonText().color())
        painter.drawText(0, 0, self.height(), self.width(), Qt.AlignCenter, "Folders ›")


class SidebarController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.collapsed = False
        self.members = []
        self.front = []
        self.widths = []
        self.strip = QToolBar("Folders", win)
        self.strip.setObjectName("tb.folder_restore")
        self.strip.setMovable(False)
        self.restore_button = RestoreButton()
        self.restore_button.setFixedSize(28, 100)
        self.restore_button.setToolTip("Expand Folders")
        self.restore_button.clicked.connect(self.expand)
        self.strip.addWidget(self.restore_button)
        from gcws.ui.layout.sample_rail import SampleRail
        self.rail = SampleRail(win.ws, win.loaded_samples.run_menu)     # loaded samples stay one click away
        self.strip.addWidget(self.rail)
        win.addToolBar(Qt.LeftToolBarArea, self.strip)
        self.strip.hide()
        self.button = QToolButton()
        self.button.setText("◀")
        self.button.setToolTip("Collapse Folders")
        self.button.clicked.connect(self.collapse)
        win.docks["tree"].titleBarWidget().layout().insertWidget(1, self.button)
        win.docks["tree"].topLevelChanged.connect(lambda floating: self.button.setEnabled(not floating))
        for dock in win.docks.values():
            dock.toggleViewAction().triggered.connect(lambda on, d=dock: self._revealed(d, on))

    def contains(self, dock):
        return self.collapsed and dock is self.win.docks["tree"]

    def _revealed(self, dock, on):
        if on and self.contains(dock):
            self.expand()
            dock.show()
            dock.raise_()

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
        self.strip.show()

    def expand(self):
        if not self.collapsed:
            self.strip.hide()
            return
        self.collapsed = False
        self.strip.hide()
        dock = self.win.docks["tree"]
        shared = any(not other.isHidden() for other in self.win.tabifiedDockWidgets(dock))
        dock.show()
        if not shared and not dock.isFloating() and self.widths:
            self.win.resizeDocks([dock], [int(self.widths[0])], Qt.Horizontal)
        dock.raise_()

    def save(self, prefix):
        s = QSettings()
        s.setValue(prefix + "/sidebar_collapsed", self.collapsed)
        s.setValue(prefix + "/sidebar_scope", "folder")
        s.setValue(prefix + "/sidebar_members", self.members)
        s.setValue(prefix + "/sidebar_front", self.front)
        s.setValue(prefix + "/sidebar_widths", self.widths)

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
        self.strip.setVisible(self.collapsed)
        if self.collapsed:
            self.win.docks["tree"].hide()
