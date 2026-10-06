"""Marks painted over table cells.

A cell the analyst changed gets a small triangle in its top right corner (as a comment in Excel),
so the edit is never confused with a status colour. A difference column can be drawn as a gauge.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPolygonF
from PySide6.QtWidgets import QStyledItemDelegate

#: item data: True on a cell the analyst changed
EDITED_ROLE = Qt.UserRole + 10
#: item data: the status level (ok, warn, bad, ...) that colours a gauge
LEVEL_ROLE = Qt.UserRole + 11
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


class DiffGaugeDelegate(EditedDelegate):
    """A difference in percent as a bar against the limit: the full width is 1.5 x the limit and a
    thin line marks the limit; coloured by the row's level (``LEVEL_ROLE``). The number stays the
    cell's text, so sorting and copying are unchanged."""

    SPAN = 1.5

    def __init__(self, limit, parent=None):
        super().__init__(parent)
        self.limit = limit                      # callable: the current limit in percent

    def paint(self, painter: QPainter, option, index):
        value = index.data(Qt.DisplayRole)
        limit = float(self.limit() or 0.0)
        if isinstance(value, (int, float)) and limit > 0:
            from gcws.ui import theme
            r = option.rect.adjusted(3, 4, -3, -4)
            full = limit * self.SPAN
            color = QColor(theme.status_color(index.data(LEVEL_ROLE) or "neutral"))
            color.setAlpha(60)
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(r.adjusted(0, 0, -int(r.width() * (1 - min(abs(value) / full, 1.0))), 0), 2, 2)
            x = r.left() + int(r.width() / self.SPAN)
            painter.setPen(QColor(theme.BORDER_STRONG))
            painter.drawLine(x, r.top() - 2, x, r.bottom() + 2)
            painter.restore()
        super().paint(painter, option, index)
