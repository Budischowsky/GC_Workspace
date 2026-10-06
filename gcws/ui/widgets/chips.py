"""Small status widgets shared by the panels: a clickable filter chip and a one-line status label."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QSizePolicy


class Chip(QLabel):
    """A status chip that filters a list when clicked."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chip")
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)


class ElidedLabel(QLabel):
    """One line that ends in "…" when the panel is too narrow; the whole text is the tooltip."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def text(self) -> str:
        return self._full

    def _elide(self) -> None:
        m = self.contentsMargins()
        width = max(0, self.width() - m.left() - m.right() - 16)        # 16: the chip's padding
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, width) if width > 40
                        else self._full)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()
