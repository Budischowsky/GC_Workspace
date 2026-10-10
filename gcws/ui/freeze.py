"""Plots stand still while panels are dragged or resized.

Dragging a panel separator, a panel by its title, a splitter handle or the window border resizes
every plot many times a second; laying out and painting a plot (axes, grid, traces, labels) each
time made the drag stutter. While such a gesture is on, a plot that is resized shows a picture of
itself, stretched to its new size, and is laid out and painted for real once, on release.
"""
from __future__ import annotations

import ctypes
import sys
import weakref

import pyqtgraph as pg
from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import QApplication, QDockWidget, QMainWindow, QSplitterHandle

_ORIG_RESIZE = pg.GraphicsView.resizeEvent
_ORIG_PAINT = pg.GraphicsView.paintEvent
_armed = False
_native = False                        # the window border is being dragged (Windows' size/move loop)
_frozen: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()     # view -> its picture
_watcher = None

WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE = 0x0231, 0x0232


def _resize(view, ev):
    if _armed and view.isVisible() and not getattr(view, "closed", False):
        if view not in _frozen:
            _frozen[view] = view.viewport().grab()       # the last real picture, before it is stretched
        view.viewport().update()
        return
    _ORIG_RESIZE(view, ev)


def _paint(view, ev):
    pm = _frozen.get(view)
    if pm is None:
        return _ORIG_PAINT(view, ev)
    p = QPainter(view.viewport())
    p.drawPixmap(view.viewport().rect(), pm)
    p.end()


def arm() -> None:
    global _armed
    _armed = True
    if _watcher is not None:
        _watcher.safety.start()


def disarm() -> None:
    """End the gesture: every plot resized during it is laid out and painted once."""
    global _armed
    _armed = False
    if _watcher is not None:
        _watcher.safety.stop()
    views = list(_frozen.keys())
    _frozen.clear()
    for view in views:
        if getattr(view, "closed", False):
            continue
        try:
            _ORIG_RESIZE(view, None)
            view.viewport().update()
        except RuntimeError:                       # deleted during the gesture
            pass


def is_armed() -> bool:
    return _armed


class _Watcher(QObject):
    """Arms on a press that starts a layout gesture, disarms on its release."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.safety = QTimer(self)                 # a release that never arrives must not freeze the plots
        self.safety.setInterval(250)
        self.safety.timeout.connect(self._check)

    def _check(self):
        if _armed and not _native and not QGuiApplication.mouseButtons() & Qt.LeftButton:
            disarm()

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
            # a press reaching the main window itself is on a separator between panels, one reaching
            # a panel itself is on its title (its contents take their own presses)
            if isinstance(obj, (QMainWindow, QDockWidget, QSplitterHandle)):
                arm()
        elif t == QEvent.MouseButtonRelease and ev.button() == Qt.LeftButton and _armed and not _native:
            disarm()
        return False


class _NativeSizeMove(QAbstractNativeEventFilter):
    """Windows: the window border (or a floating panel's) is being dragged."""

    def nativeEventFilter(self, event_type, message):
        global _native
        if event_type == b"windows_generic_MSG":
            try:
                msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents.message
            except (TypeError, ValueError):
                return False, 0
            if msg == WM_ENTERSIZEMOVE:
                _native = True
                arm()
            elif msg == WM_EXITSIZEMOVE:
                _native = False
                QTimer.singleShot(0, disarm)
        return False, 0


if sys.platform == "win32":
    from ctypes import wintypes

    class _MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                    ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


def install(app: QApplication | None = None) -> None:
    """Route every pyqtgraph view through the freeze; once per process."""
    global _watcher
    if _watcher is not None:
        return
    pg.GraphicsView.resizeEvent = _resize
    pg.GraphicsView.paintEvent = _paint
    _watcher = _Watcher(app or QApplication.instance())
    if sys.platform == "win32":
        _watcher.native = _NativeSizeMove()
        (app or QApplication.instance()).installNativeEventFilter(_watcher.native)


def watch(widget) -> None:
    """Presses on ``widget`` (the main window, a panel, a splitter handle) may start a gesture."""
    if _watcher is not None:
        widget.installEventFilter(_watcher)


def watch_splitters(root) -> None:
    for handle in root.findChildren(QSplitterHandle):
        if not handle.property("freezeWatched"):
            handle.setProperty("freezeWatched", True)
            watch(handle)
