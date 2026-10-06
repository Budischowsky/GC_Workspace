"""Marks painted over table cells.

A cell the analyst changed gets a small triangle in its top right corner (as a comment in Excel),
so the edit is never confused with a status colour.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPolygonF
from PySide6.QtWidgets import QStyledItemDelegate

#: item data: True on a cell the analyst changed
EDITED_ROLE = Qt.UserRole + 10
CORNER = 7          # px, the triangle's side


class EditedDelegate(QStyledItemDelegate):
    """Paints the cell as usual, then the edit triangle where ``EDITED_ROLE`` is set."""

    def paint(self, painter: QPainter, option, index):
        super().paint(painter, option, index)
        if index.data(EDITED_ROLE):
            from gcws.ui import theme
            r = option.rect
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawPolygon(QPolygonF([QPointF(r.right() - CORNER, r.top()), QPointF(r.right() + 1, r.top()),
                                           QPointF(r.right() + 1, r.top() + CORNER + 1)]))
            painter.restore()
