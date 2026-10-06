"""Marks painted over table cells.

A cell the analyst changed gets a small triangle in its top right corner (as a comment in Excel),
so the edit is never confused with a status colour. A difference column can be drawn as a gauge,
and a check-box column as a centred box that one click anywhere in the cell switches.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPolygonF
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem

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
            self._corner(painter, option.rect)

    @staticmethod
    def _corner(painter: QPainter, r) -> None:
        """The edit triangle in the top right corner of ``r``."""
        from gcws.ui import theme
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


class CheckDelegate(EditedDelegate):
    """A check-box column: the box centred in the cell, and one click anywhere in the cell asks
    ``toggle(index)`` to switch it. The delegate never writes the model itself (the owner decides
    and rebuilds the table later), and the click that ends a double-click is ignored, so a
    double-click switches once. Space is left to the table."""

    def __init__(self, toggle, parent=None):
        super().__init__(parent)
        self.toggle = toggle                    # callable(QModelIndex)
        self._double = None                     # (row, column, time) of the last double-click

    def paint(self, painter: QPainter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        state = opt.checkState
        has_box = bool(opt.features & QStyleOptionViewItem.HasCheckIndicator)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        box = style.subElementRect(QStyle.SE_ItemViewItemCheckIndicator, opt, widget) if has_box else QRect()
        opt.features &= ~QStyleOptionViewItem.HasCheckIndicator
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)    # background and selection
        if has_box:
            ind = QStyleOptionViewItem(opt)
            ind.rect = QRect(0, 0, box.width(), box.height())
            ind.rect.moveCenter(option.rect.center())
            ind.state = (opt.state & ~QStyle.State_HasFocus) | (QStyle.State_On if state == Qt.Checked
                                                                else QStyle.State_Off)
            style.drawPrimitive(QStyle.PE_IndicatorItemViewItemCheck, ind, painter, widget)
        if index.data(EDITED_ROLE):
            self._corner(painter, option.rect)

    def editorEvent(self, event, model, option, index):
        kind = event.type()
        if kind not in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
            return False
        if event.button() != Qt.LeftButton or not option.rect.contains(event.position().toPoint())                 or not index.flags() & Qt.ItemIsUserCheckable:
            return False
        import time
        if kind == QEvent.MouseButtonDblClick:
            self._double = (index.row(), index.column(), time.monotonic())
        elif kind == QEvent.MouseButtonRelease:
            double, self._double = self._double, None
            # the release that ends a double-click (the first click switched already)
            if double is None or double[:2] != (index.row(), index.column()) or \
                    time.monotonic() - double[2] > QApplication.doubleClickInterval() / 1000.0:
                self.toggle(index)
        return True
