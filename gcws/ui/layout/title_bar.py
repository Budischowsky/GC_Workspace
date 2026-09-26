"""Title bar of every panel: the name plus always-visible buttons.

Qt's own dock title draws its close and float buttons only as a hover
effect in this theme, so each dock gets this bar instead: maximize /
restore, detach / dock back and close. Double-clicking the bar maximizes the
panel (and restores it again) instead of Qt's default of detaching it.
Mouse presses on the bar itself are left unhandled so Qt still drags the
panel from it.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QDockWidget, QHBoxLayout, QLabel, QToolButton, QWidget

from gcws.ui.icons import icon

ICON_DARK = "#124658"          # = theme.ACCENT_PRESSED
ICON_LIGHT = "#FFFFFF"


class DockTitleBar(QWidget):
    def __init__(self, dock: QDockWidget, on_maximize, parent=None):
        super().__init__(parent or dock)
        self.dock = dock
        self.on_maximize = on_maximize
        self.active = False
        self.maximized = False
        self.setObjectName("dockTitle")
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 2, 3, 2)
        lay.setSpacing(1)
        self.label = QLabel(dock.windowTitle())
        self.label.setObjectName("dockTitleText")
        lay.addWidget(self.label, 1)
        self.b_max = self._button(lambda: self.on_maximize(self.dock))
        self.b_float = self._button(self._toggle_float)
        self.b_close = self._button(self._close)
        self.b_close.setProperty("role", "close")
        for b in (self.b_max, self.b_float, self.b_close):
            lay.addWidget(b)
        dock.windowTitleChanged.connect(self.label.setText)
        dock.featuresChanged.connect(lambda *_: self.update_buttons())
        dock.topLevelChanged.connect(lambda *_: self.update_buttons())
        self.update_buttons()

    def _button(self, slot) -> QToolButton:
        b = QToolButton(self)
        b.setObjectName("dockButton")
        b.setAutoRaise(True)
        b.setIconSize(QSize(14, 14))
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(slot)
        return b

    # -- state -----------------------------------------------------------------------

    def set_active(self, on: bool) -> None:
        self.active = bool(on)
        self.setProperty("active", self.active)
        for w in (self, self.label, self.b_max, self.b_float, self.b_close):
            w.style().unpolish(w)
            w.style().polish(w)
        self.update_buttons()

    def set_maximized(self, on: bool) -> None:
        self.maximized = bool(on)
        self.update_buttons()

    def update_buttons(self) -> None:
        color = ICON_LIGHT if self.active else ICON_DARK
        feats = self.dock.features()
        floating = self.dock.isFloating()
        self.b_max.setIcon(icon("panel-restore" if self.maximized else "panel-maximize", color))
        self.b_max.setToolTip("Restore the layout (double-click the title)" if self.maximized else
                              "Maximize this panel (double-click the title)")
        self.b_float.setIcon(icon("panel-dock" if floating else "panel-float", color))
        self.b_float.setToolTip("Dock the panel back into the window" if floating else
                                "Detach the panel (e.g. onto a second screen)")
        self.b_float.setVisible(bool(feats & QDockWidget.DockWidgetFloatable))
        self.b_close.setIcon(icon("panel-close", color))
        self.b_close.setToolTip("Close the panel (View menu shows it again)")
        self.b_close.setVisible(bool(feats & QDockWidget.DockWidgetClosable))

    # -- actions ---------------------------------------------------------------------

    def _toggle_float(self):
        if self.maximized:
            self.on_maximize(self.dock)
        self.dock.setFloating(not self.dock.isFloating())
        self.dock.show()
        self.dock.raise_()

    def _close(self):
        if self.maximized:
            self.on_maximize(self.dock)
        self.dock.close()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            ev.accept()
            self.on_maximize(self.dock)
            return
        super().mouseDoubleClickEvent(ev)

    def sizeHint(self):
        h = max(self.label.sizeHint().height(), 20) + 6
        return QSize(super().sizeHint().width(), h)
