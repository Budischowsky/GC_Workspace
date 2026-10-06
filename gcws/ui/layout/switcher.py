"""Ctrl+Tab: a small list of the panels, the most recently used first.

A quick Ctrl+Tab goes straight back to the panel used before. Holding Ctrl keeps the
list open: Tab / Shift+Tab or the arrow keys step, releasing Ctrl, Enter or a click
goes to the marked panel, Esc closes the list without changing anything.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QFrame, QLabel, QListWidget, QListWidgetItem, QVBoxLayout

from gcws.ui import theme


class PanelSwitcher(QFrame):
    def __init__(self, win):
        super().__init__(win, Qt.Popup | Qt.FramelessWindowHint)
        self.win = win
        self.setObjectName("panelSwitcher")
        self.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        head = QLabel("Panels")
        head.setObjectName("dockTitleText")
        lay.addWidget(head)
        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.NoFocus)        # the keys go to the switcher itself
        self.list.itemClicked.connect(lambda _item: self.activate())
        lay.addWidget(self.list)

    def open(self, step: int = 1, held: bool | None = None) -> None:
        """Show the list with the next (step 1) or the last (step -1) panel marked. Without Ctrl held
        (a quick tap) the marked panel is opened at once."""
        if self.isVisible():
            self.step(step)
            return
        self.list.clear()
        for key in self.win.panel_order():
            dock = self.win.docks[key]
            closed = dock.isHidden()
            item = QListWidgetItem(dock.windowTitle() + ("   (closed)" if closed else ""))
            item.setData(Qt.UserRole, key)
            if closed:
                item.setForeground(QColor(theme.FAINT))
            self.list.addItem(item)
        n = self.list.count()
        if n == 0:
            return
        self.list.setCurrentRow(step % n)
        if held is None:
            held = bool(QGuiApplication.queryKeyboardModifiers() & Qt.ControlModifier)
        if not held:
            self.activate()
            return
        rows = min(n, 12)
        self.resize(280, rows * max(self.list.sizeHintForRow(0), 18) + 48)
        center = self.win.geometry().center()
        self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)
        self.show()
        self.setFocus()

    def step(self, step: int) -> None:
        n = self.list.count()
        if n:
            self.list.setCurrentRow((self.list.currentRow() + step) % n)

    def activate(self) -> None:
        item = self.list.currentItem()
        self.hide()
        if item is not None:
            self.win.focus_panel(item.data(Qt.UserRole))

    def keyPressEvent(self, ev):
        key = ev.key()
        if key in (Qt.Key_Tab, Qt.Key_Down):
            self.step(1)
        elif key in (Qt.Key_Backtab, Qt.Key_Up):
            self.step(-1)
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self.activate()
        elif key == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        if ev.key() == Qt.Key_Control and self.isVisible():
            self.activate()
        else:
            super().keyReleaseEvent(ev)
