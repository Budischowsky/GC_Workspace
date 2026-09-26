"""Small vector icons drawn with QPainter (no image files needed)."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

S = 32


def _peak_path(x0=4, x1=28, top=6, base=26, mid=16, width=5.0) -> QPainterPath:
    p = QPainterPath()
    p.moveTo(x0, base)
    p.cubicTo(mid - width, base, mid - width * 0.6, top, mid, top)
    p.cubicTo(mid + width * 0.6, top, mid + width, base, x1, base)
    return p


def _canvas():
    pm = QPixmap(S, S)
    pm.fill(Qt.transparent)
    qp = QPainter(pm)
    qp.setRenderHint(QPainter.Antialiasing)
    return pm, qp


def _pen(color="#303030", w=2.0, style=Qt.SolidLine):
    pen = QPen(QColor(color), w, style)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


ACCENT = "#1F6F8B"          # = theme.ACCENT (kept literal: icons must not import widgets)


@lru_cache(maxsize=None)
def icon(name: str, color: str = ACCENT) -> QIcon:
    pm, qp = _canvas()
    dark = "#33424D"
    accent = QColor(color)
    if name == "select":
        poly = QPolygonF([QPointF(9, 5), QPointF(9, 25), QPointF(14, 20), QPointF(18, 28),
                          QPointF(21, 27), QPointF(17, 19), QPointF(24, 19)])
        qp.setPen(_pen(dark, 1.5))
        qp.setBrush(QColor("white"))
        qp.drawPolygon(poly)
    elif name == "zoom":
        qp.setPen(_pen(dark, 2.5))
        qp.drawEllipse(QRectF(5, 5, 15, 15))
        qp.drawLine(QPointF(18, 18), QPointF(27, 27))
    elif name == "pan":
        qp.setPen(_pen(dark, 2))
        for a, b in (((16, 4), (16, 28)), ((4, 16), (28, 16))):
            qp.drawLine(QPointF(*a), QPointF(*b))
        for pts in (((12, 8), (16, 4), (20, 8)), ((12, 24), (16, 28), (20, 24)),
                    ((8, 12), (4, 16), (8, 20)), ((24, 12), (28, 16), (24, 20))):
            qp.drawPolyline(QPolygonF([QPointF(*p) for p in pts]))
    elif name in ("baseline", "split", "delete", "add", "move", "merge", "skim", "negative", "reset",
                  "integrate", "autoparam"):
        path = _peak_path()
        if name == "negative":
            path = QPainterPath()
            path.moveTo(4, 8)
            path.cubicTo(11, 8, 12, 26, 16, 26)
            path.cubicTo(20, 26, 21, 8, 28, 8)
        if name == "add":
            fill = QPainterPath(path)
            fill.closeSubpath()
            qp.fillPath(fill, QColor(accent.red(), accent.green(), accent.blue(), 90))
        qp.setPen(_pen(dark, 1.8))
        qp.drawPath(path)
        qp.setPen(_pen(color, 2.2))
        if name == "baseline":
            qp.drawLine(QPointF(3, 27), QPointF(29, 23))
            qp.setBrush(accent)
            qp.drawEllipse(QPointF(3, 27), 2.2, 2.2)
            qp.drawEllipse(QPointF(29, 23), 2.2, 2.2)
        elif name == "split":
            qp.drawLine(QPointF(16, 4), QPointF(16, 28))
        elif name == "delete":
            qp.setPen(_pen("#c0392b", 2.6))
            qp.drawLine(QPointF(8, 8), QPointF(24, 24))
            qp.drawLine(QPointF(24, 8), QPointF(8, 24))
        elif name == "add":
            qp.drawLine(QPointF(24, 3), QPointF(24, 11))
            qp.drawLine(QPointF(20, 7), QPointF(28, 7))
        elif name == "move":
            qp.drawLine(QPointF(9, 20), QPointF(9, 28))
            qp.drawPolyline(QPolygonF([QPointF(3, 17), QPointF(9, 13), QPointF(15, 17)]))
            qp.drawLine(QPointF(9, 13), QPointF(9, 20))
        elif name == "merge":
            qp.drawPolyline(QPolygonF([QPointF(5, 29), QPointF(16, 21), QPointF(27, 29)]))
        elif name == "skim":
            qp.drawLine(QPointF(14, 24), QPointF(29, 26))
        elif name == "negative":
            qp.drawLine(QPointF(3, 8), QPointF(29, 8))
        elif name == "reset":
            qp.drawArc(QRectF(17, 2, 12, 12), 30 * 16, 280 * 16)
            qp.drawLine(QPointF(27, 3), QPointF(28, 7))
        elif name == "integrate":
            fill = QPainterPath(path)
            fill.closeSubpath()
            qp.fillPath(fill, QColor(accent.red(), accent.green(), accent.blue(), 120))
            qp.drawLine(QPointF(4, 26), QPointF(28, 26))
        elif name == "autoparam":
            qp.setFont(qp.font())
            f = qp.font()
            f.setBold(True)
            f.setPixelSize(12)
            qp.setFont(f)
            qp.drawText(QRectF(14, 0, 18, 14), Qt.AlignCenter, "A")
    elif name == "search":
        qp.setPen(_pen(dark, 1.6))
        for x, h in ((7, 10), (12, 18), (17, 7), (22, 14)):
            qp.drawLine(QPointF(x, 28), QPointF(x, 28 - h))
        qp.setPen(_pen(color, 2.4))
        qp.drawEllipse(QRectF(15, 3, 11, 11))
        qp.drawLine(QPointF(24, 12), QPointF(29, 17))
    elif name == "folder":
        qp.setPen(_pen("#9a7b2f", 1.4))
        qp.setBrush(QColor("#f2cf6b"))
        qp.drawRoundedRect(QRectF(3, 9, 26, 18), 2, 2)
        qp.drawRect(QRectF(3, 6, 10, 5))
    elif name == "run":
        qp.setPen(_pen("#4a5a6a", 1.4))
        qp.setBrush(QColor("#e8eef5"))
        qp.drawRoundedRect(QRectF(3, 4, 26, 24), 3, 3)
        qp.setPen(_pen(color, 1.8))
        qp.drawPath(_peak_path(6, 26, 9, 24, 13, 3.0))
    elif name == "analysis":
        qp.setPen(_pen("#9a7b2f", 1.4))
        qp.setBrush(QColor("#f2cf6b"))
        qp.drawRoundedRect(QRectF(3, 9, 26, 18), 2, 2)
        qp.drawRect(QRectF(3, 6, 10, 5))
        qp.setPen(_pen(color, 1.8))
        qp.drawPath(_peak_path(6, 26, 12, 24, 15, 2.5))
    elif name == "layout":
        qp.setPen(_pen(dark, 1.6))
        qp.drawRect(QRectF(4, 5, 24, 22))
        qp.drawLine(QPointF(12, 5), QPointF(12, 27))
        qp.drawLine(QPointF(12, 16), QPointF(28, 16))
    elif name.startswith("panel-"):
        # panel title buttons: drawn in ``color`` (dark on the light title, white on the active one)
        qp.setPen(_pen(color, 2.6))
        qp.setBrush(Qt.NoBrush)
        if name == "panel-maximize":
            qp.drawRect(QRectF(6, 7, 20, 18))
            qp.drawLine(QPointF(6, 10), QPointF(26, 10))
        elif name == "panel-restore":
            qp.drawRect(QRectF(5, 12, 15, 14))
            qp.drawPolyline(QPolygonF([QPointF(11, 12), QPointF(11, 6), QPointF(27, 6), QPointF(27, 20),
                                       QPointF(20, 20)]))
        elif name == "panel-float":
            qp.drawPolyline(QPolygonF([QPointF(15, 7), QPointF(6, 7), QPointF(6, 26), QPointF(25, 26),
                                       QPointF(25, 17)]))
            qp.drawLine(QPointF(14, 18), QPointF(27, 5))
            qp.drawPolyline(QPolygonF([QPointF(19, 5), QPointF(27, 5), QPointF(27, 13)]))
        elif name == "panel-dock":
            qp.drawRect(QRectF(5, 5, 22, 22))
            qp.drawLine(QPointF(24, 8), QPointF(12, 20))
            qp.drawPolyline(QPolygonF([QPointF(11, 12), QPointF(11, 21), QPointF(20, 21)]))
        else:                                   # panel-close
            qp.drawLine(QPointF(8, 8), QPointF(24, 24))
            qp.drawLine(QPointF(24, 8), QPointF(8, 24))
    elif name == "report":
        qp.setPen(_pen(dark, 1.5))
        qp.setBrush(QColor("white"))
        qp.drawRect(QRectF(7, 3, 18, 26))
        qp.setPen(_pen(color, 1.5))
        for y in (9, 14, 19, 24):
            qp.drawLine(QPointF(10, y), QPointF(22, y))
    else:
        qp.setPen(_pen(dark, 2))
        qp.drawEllipse(QRectF(6, 6, 20, 20))
    qp.end()
    return QIcon(pm)


def color_chip(color: str, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    qp = QPainter(pm)
    qp.setRenderHint(QPainter.Antialiasing)
    qp.setBrush(QColor(color))
    qp.setPen(QPen(QColor(color).darker(140), 1))
    qp.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), 3, 3)
    qp.end()
    return QIcon(pm)
