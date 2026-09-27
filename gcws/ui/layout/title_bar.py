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
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication, QDockWidget, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from gcws.ui.icons import icon



class RotatedLabel(QLabel):
    def sizeHint(self):
        return QSize(24, super().sizeHint().width())

    def minimumSizeHint(self):
        return QSize(24, 30)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.translate(self.width(), 0)
        painter.rotate(90)
        painter.setPen(self.palette().windowText().color())
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.height())
        painter.drawText(0, 0, self.height(), self.width(), Qt.AlignCenter, text)


def title_bar(dock):
    return getattr(dock, "panel_title_bar", None) or dock.titleBarWidget()


class RightTitleDock(QDockWidget):
    """Keep Qt's dock container and persistence, with a title rail inside its right edge."""
    def set_panel(self, widget, on_maximize):
        empty = QWidget(self)
        empty.setFixedHeight(0)
        self.setTitleBarWidget(empty)
        body = QWidget(self)
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.panel_title_bar = DockTitleBar(self, on_maximize, body, vertical=True)
        layout.addWidget(widget, 1)
        layout.addWidget(self.panel_title_bar)
        self.setWidget(body)


class DockTitleBar(QWidget):
    def __init__(self, dock: QDockWidget, on_maximize, parent=None, vertical=False):
        super().__init__(parent or dock)
        self.dock = dock
        self.on_maximize = on_maximize
        self.vertical = vertical
        self._press = None
        self._drag_offset = None
        from gcws.ui import theme
        theme.notifier().changed.connect(self._theme_changed)      # dropped with this widget
        self.active = False
        self.maximized = False
        self.setObjectName("dockTitle")
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        lay.setContentsMargins(*(2, 3, 2, 3) if vertical else (10, 2, 3, 2))
        lay.setSpacing(1)
        self.label = RotatedLabel(dock.windowTitle()) if vertical else QLabel(dock.windowTitle())
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents)
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
        if vertical:
            self.setFixedWidth(30)

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

    def _theme_changed(self, _name: str = "") -> None:
        try:
            self.update_buttons()
        except RuntimeError:                  # the dock is already gone
            pass

    def set_maximized(self, on: bool) -> None:
        self.maximized = bool(on)
        self.update_buttons()

    def update_buttons(self) -> None:
        from gcws.ui import theme
        color = theme.ON_ACCENT if self.active else theme.ACCENT_TEXT     # on the title's background
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
        self._press = None
        self._drag_offset = None
        if ev.button() == Qt.LeftButton:
            ev.accept()
            self.on_maximize(self.dock)
            return
        super().mouseDoubleClickEvent(ev)

    def mousePressEvent(self, ev):
        if self.vertical and ev.button() == Qt.LeftButton:
            self._press = ev.globalPosition().toPoint()
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self.vertical and self._press is not None and ev.buttons() & Qt.LeftButton:
            if not self.dock.features() & QDockWidget.DockWidgetMovable:
                return
            pos = ev.globalPosition().toPoint()
            if self._drag_offset is None:
                if (pos - self._press).manhattanLength() < QApplication.startDragDistance():
                    return
                if self.maximized:
                    self.on_maximize(self.dock)
                if not self.dock.isFloating():
                    if not self.dock.features() & QDockWidget.DockWidgetFloatable:
                        return
                    origin = self.dock.mapToGlobal(self.dock.rect().topLeft())
                    self.dock.setFloating(True)
                    self.dock.move(origin)
                self._drag_offset = self._press - self.dock.pos()
            self.dock.move(pos - self._drag_offset)
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._press = None
        self._drag_offset = None
        super().mouseReleaseEvent(ev)

    def sizeHint(self):
        if self.vertical:
            return QSize(30, 180)
        h = max(self.label.sizeHint().height(), 20) + 6
        return QSize(super().sizeHint().width(), h)
