"""Drop-zone suggestion while a detached panel is dragged near the workspace.

Qt shows its own docking preview only when the cursor is over an existing
dock area. This helper watches floating docks: when one is moved within
``MARGIN`` pixels of an edge or corner of the main window, a translucent band
with the suggested placement is shown; releasing the mouse docks the panel
there (full height at the left/right, full width at the top/bottom).
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QDockWidget, QWidget

MARGIN = 56
AREA_NAMES = {Qt.LeftDockWidgetArea: "left", Qt.RightDockWidgetArea: "right",
              Qt.TopDockWidgetArea: "top", Qt.BottomDockWidgetArea: "bottom"}


class Band(QWidget):
    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.text = ""

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(31, 111, 139, 70))           # theme.ACCENT
        p.setPen(QPen(QColor(31, 111, 139, 220), 2))
        p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 6, 6)
        p.setPen(QColor(18, 70, 88))
        f = p.font()
        f.setBold(True)
        f.setPointSize(10)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self.text)


class DropOverlay(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.enabled = True
        self.band = Band()
        self.dock: QDockWidget | None = None
        self.area = None
        self.target = None
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._poll)

    def watch(self, dock: QDockWidget) -> None:
        dock.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if self.enabled and isinstance(obj, QDockWidget) and obj.isFloating() and ev.type() == QEvent.Move:
            if QGuiApplication.mouseButtons() & Qt.LeftButton:
                self.dock = obj
                self._update(QCursor.pos())
                if not self.timer.isActive():
                    self.timer.start()
        return False

    def _zone(self, pos: QPoint):
        g = self.win.frameGeometry()
        inner = self.win.geometry()
        if not g.adjusted(-MARGIN, -MARGIN, MARGIN, MARGIN).contains(pos):
            return None, None
        dl, dr = pos.x() - inner.left(), inner.right() - pos.x()
        dt, db = pos.y() - inner.top(), inner.bottom() - pos.y()
        best = min((dl, Qt.LeftDockWidgetArea), (dr, Qt.RightDockWidgetArea),
                   (dt, Qt.TopDockWidgetArea), (db, Qt.BottomDockWidgetArea), key=lambda x: x[0])
        if best[0] > MARGIN:
            return None, None
        area = best[1]
        w, h = inner.width(), inner.height()
        if area == Qt.LeftDockWidgetArea:
            rect = QRect(inner.left(), inner.top(), int(w * 0.28), h)
        elif area == Qt.RightDockWidgetArea:
            rect = QRect(inner.right() - int(w * 0.28), inner.top(), int(w * 0.28), h)
        elif area == Qt.TopDockWidgetArea:
            rect = QRect(inner.left(), inner.top(), w, int(h * 0.3))
        else:
            rect = QRect(inner.left(), inner.bottom() - int(h * 0.3), w, int(h * 0.3))
        return area, rect

    def _update(self, pos: QPoint):
        area, rect = self._zone(pos)
        self.area = area
        self.target = None
        if area is None:
            for dock in self.win.docks.values():
                if dock is self.dock or dock.isFloating() or dock.visibleRegion().isEmpty():
                    continue
                bounds = QRect(dock.mapToGlobal(QPoint()), dock.size())
                center = bounds.adjusted(bounds.width() // 4, bounds.height() // 4,
                                         -bounds.width() // 4, -bounds.height() // 4)
                if center.contains(pos):
                    self.target, rect = dock, bounds
                    break
        if area is None and self.target is None:
            self.band.hide()
            return
        title = self.dock.windowTitle() if self.dock else "panel"
        self.band.text = (f"Release to tab “{title}” with “{self.target.windowTitle()}”" if self.target else
                          f"Release to dock “{title}”\nalong the whole {AREA_NAMES[area]} side")
        self.band.setGeometry(rect)
        self.band.show()
        self.band.update()

    def _poll(self):
        if QGuiApplication.mouseButtons() & Qt.LeftButton:
            self._update(QCursor.pos())
            return
        self.timer.stop()
        self.band.hide()
        dock, area = self.dock, self.area
        target, self.target = self.target, None
        self.dock, self.area = None, None
        if dock is not None and target is not None and dock.isFloating():
            dock.setFloating(False)
            self.win.tabifyDockWidget(target, dock)
            dock.show()
            dock.raise_()
            return
        if dock is not None and area is not None and dock.isFloating():
            dock.setFloating(False)
            self.win.addDockWidget(area, dock, Qt.Vertical if area in (Qt.LeftDockWidgetArea,
                                                                       Qt.RightDockWidgetArea) else Qt.Horizontal)
            dock.show()
            dock.raise_()
            self.win.statusBar().showMessage(f"{dock.windowTitle()} docked at the {AREA_NAMES[area]} side "
                                             f"(Layout ▸ Save layout... to keep it)", 6000)
