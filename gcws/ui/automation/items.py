"""The process chart's pieces: step cards with ports and the arrows between them."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QPen, QPolygonF
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsObject, QGraphicsPathItem, QGraphicsRectItem,
                               QGraphicsSimpleTextItem)

from gcws.automation import workflow as W
from gcws.ui import theme

W_NODE, H_NODE = 185.0, 88.0
PORT = 7.0
#: colour of each step type (theme keys)
TYPE_COLOR = {"source": "INFO", "copy": "INFO", "method": "ACCENT", "report2": "WARN", "report": "OK",
              "folder": "NEUTRAL"}


def type_color(kind: str) -> QColor:
    return QColor(getattr(theme, TYPE_COLOR.get(kind, "NEUTRAL")))


def text_on(color: QColor) -> QColor:
    """Black or white, whichever reads better on ``color``."""
    lum = 0.2126 * color.redF() + 0.7152 * color.greenF() + 0.0722 * color.blueF()
    return QColor("#101418") if lum > 0.55 else QColor("#FFFFFF")


class NodeItem(QGraphicsObject):
    moved = Signal(str, float, float)            # node id, x, y (after a drag)
    edit = Signal(str)                           # double-click
    connect_from = Signal(str, QPointF)          # a drag started on the output port

    def __init__(self, node: W.Node):
        super().__init__()
        self.node = node
        self.edges: list = []
        self.issue = ""                          # "" | "warning" | "error"
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setPos(node.x, node.y)
        self.setZValue(2)
        self._press = None

    # -- geometry -------------------------------------------------------------------------------
    def boundingRect(self) -> QRectF:
        return QRectF(-PORT - 2, -2, W_NODE + 2 * PORT + 4, H_NODE + 4)

    def in_port(self) -> QPointF:
        return self.mapToScene(QPointF(0, H_NODE / 2))

    def out_port(self) -> QPointF:
        return self.mapToScene(QPointF(W_NODE, H_NODE / 2))

    def has_in(self) -> bool:
        return self.node.type != "source"

    def has_out(self) -> bool:
        return self.node.type != "folder"

    def on_out_port(self, scene_pos: QPointF) -> bool:
        p = self.mapFromScene(scene_pos)
        return self.has_out() and (p - QPointF(W_NODE, H_NODE / 2)).manhattanLength() <= PORT * 2.2

    # -- painting -------------------------------------------------------------------------------
    def paint(self, qp: QPainter, option, widget=None):
        qp.setRenderHint(QPainter.Antialiasing)
        col = type_color(self.node.type)
        border = QColor(theme.BAD) if self.issue == "error" else (
            QColor(theme.WARN) if self.issue == "warning" else QColor(theme.BORDER_STRONG))
        if self.isSelected():
            border = QColor(theme.ACCENT)
        qp.setPen(QPen(border, 2.2 if self.isSelected() or self.issue else 1.2))
        card = QPainterPath()
        card.addRoundedRect(QRectF(0, 0, W_NODE, H_NODE), 10, 10)
        qp.fillPath(card, QBrush(QColor(theme.SURFACE)))
        qp.save()
        qp.setClipPath(card)
        qp.fillRect(QRectF(0, 0, W_NODE, 26), QBrush(col))
        qp.restore()
        qp.setBrush(Qt.NoBrush)
        qp.drawPath(card)
        qp.setPen(text_on(col))
        f = QFont()
        f.setBold(True)
        qp.setFont(f)
        qp.drawText(QRectF(10, 0, W_NODE - 20, 26), Qt.AlignVCenter | Qt.AlignLeft, self.node.title)
        qp.setPen(QColor(theme.TEXT))
        f.setBold(False)
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1))
        qp.setFont(f)
        text = W.summary(self.node)
        metrics = qp.fontMetrics()
        lines = [metrics.elidedText(t, Qt.ElideMiddle, int(W_NODE - 18)) for t in text.splitlines()[:3]]
        qp.drawText(QRectF(9, 30, W_NODE - 18, H_NODE - 34), Qt.AlignLeft | Qt.AlignTop, "\n".join(lines))
        qp.setBrush(QColor(theme.SURFACE))
        qp.setPen(QPen(col, 2))
        if self.has_in():
            qp.drawEllipse(QPointF(0, H_NODE / 2), PORT - 1, PORT - 1)
        if self.has_out():
            qp.setBrush(col)
            qp.drawEllipse(QPointF(W_NODE, H_NODE / 2), PORT - 1, PORT - 1)

    # -- interaction ----------------------------------------------------------------------------
    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for e in self.edges:
                e.update_path()
        return super().itemChange(change, value)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.on_out_port(ev.scenePos()):
            self.connect_from.emit(self.node.id, ev.scenePos())
            ev.accept()
            return
        self._press = self.pos()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        super().mouseReleaseEvent(ev)
        if self._press is not None and self.pos() != self._press:
            self.moved.emit(self.node.id, self.pos().x(), self.pos().y())
        self._press = None

    def mouseDoubleClickEvent(self, ev):
        self.edit.emit(self.node.id)
        ev.accept()

    def hoverMoveEvent(self, ev):
        self.setCursor(Qt.CrossCursor if self.on_out_port(ev.scenePos()) else Qt.OpenHandCursor)


class EdgeItem(QGraphicsPathItem):
    """A Bezier arrow from a step's output port to the next step's input port, labelled with its filter."""

    def __init__(self, edge: W.Edge, src: NodeItem, dst: NodeItem):
        super().__init__()
        self.edge, self.src, self.dst = edge, src, dst
        self.issue = ""
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setZValue(1)
        # the filter label is its own item above the steps (a child would be hidden under them)
        self.tag = QGraphicsRectItem()
        self.tag.setZValue(3)
        self.tag._edge = self
        self.label = QGraphicsSimpleTextItem(self.tag)
        self.head = QPolygonF()
        src.edges.append(self)
        dst.edges.append(self)
        self.update_path()

    def update_path(self):
        a, b = self.src.out_port(), self.dst.in_port()
        dx = max(40.0, abs(b.x() - a.x()) * 0.5)
        path = QPainterPath(a)
        path.cubicTo(a + QPointF(dx, 0), b - QPointF(dx, 0), b - QPointF(PORT + 2, 0))
        self.setPath(path)
        tip = b - QPointF(PORT - 1, 0)
        self.head = QPolygonF([tip, tip + QPointF(-11, -6), tip + QPointF(-11, 6)])
        text = W.filter_text(self.edge.filter)
        self.label.setText(text)
        self.tag.setVisible(bool(text))
        mid = path.pointAtPercent(0.5)
        r = self.label.boundingRect()
        self.tag.setRect(QRectF(0, 0, r.width() + 8, r.height() + 2))
        self.label.setPos(4, 1)
        self.tag.setPos(mid.x() - r.width() / 2 - 4, mid.y() - r.height() / 2 - 1)
        self._style_tag()
        self.prepareGeometryChange()

    def _style_tag(self):
        self.tag.setBrush(QColor(theme.SURFACE))
        self.tag.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        self.label.setBrush(QColor(theme.TEXT))

    def shape(self):
        s = QPainterPathStroker()
        s.setWidth(12)
        return s.createStroke(self.path())

    def boundingRect(self):
        return super().boundingRect().adjusted(-14, -14, 14, 14)

    def paint(self, qp: QPainter, option, widget=None):
        qp.setRenderHint(QPainter.Antialiasing)
        col = QColor(theme.ACCENT) if self.isSelected() else (
            QColor(theme.BAD) if self.issue == "error" else QColor(theme.MUTED))
        pen = QPen(col, 2.4 if self.isSelected() else 1.8)
        if self.edge.filter:
            pen.setStyle(Qt.DashLine)
        qp.setPen(pen)
        qp.setBrush(Qt.NoBrush)
        qp.drawPath(self.path())
        qp.setPen(Qt.NoPen)
        qp.setBrush(col)
        qp.drawPolygon(self.head)
        self._style_tag()

    def detach(self):
        for n in (self.src, self.dst):
            if self in n.edges:
                n.edges.remove(self)
