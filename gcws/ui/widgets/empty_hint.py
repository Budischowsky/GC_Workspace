"""A quiet line of text in the middle of an empty plot or table, telling what to do next."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QLabel

from gcws.ui import theme


class EmptyHint(QLabel):
    def __init__(self, host, text: str = ""):
        super().__init__(host)
        self.host = host
        self.setObjectName("emptyHint")
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        host.installEventFilter(self)
        theme.notifier().changed.connect(self._restyle)      # dropped with this widget
        self._restyle()
        self.set_text(text)

    def set_text(self, text: str) -> None:
        if text == self.text() and self.isVisible() == bool(text):
            return
        self.setText(text)
        self.setVisible(bool(text))
        self._place()

    def _restyle(self, _name: str = "") -> None:
        try:
            self.setStyleSheet(f"QLabel#emptyHint {{ color: {theme.FAINT}; background: transparent; }}")
        except RuntimeError:                  # the host is already gone
            pass

    def _place(self) -> None:
        w = max(120, int(self.host.width() * 0.7))
        h = self.heightForWidth(w) if self.text() else 0
        self.setGeometry((self.host.width() - w) // 2, (self.host.height() - h) // 2, w, max(h, 1))
        self.raise_()

    def eventFilter(self, obj, ev):
        if obj is self.host and ev.type() == QEvent.Resize:
            self._place()
        return False
