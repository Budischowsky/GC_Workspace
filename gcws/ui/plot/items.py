"""pyqtgraph items that draw integration results."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen, QBrush

from gcws.ui import theme


def _poly(path: QPainterPath, xs, ys, close_to=None):
    if len(xs) == 0:
        return
    path.moveTo(float(xs[0]), float(ys[0]))
    for x, y in zip(xs[1:], ys[1:]):
        path.lineTo(float(x), float(y))
    if close_to is not None:
        bx, by = close_to
        for x, y in zip(bx[::-1], by[::-1]):
            path.lineTo(float(x), float(y))
        path.closeSubpath()


class PeaksItem(pg.GraphicsObject):
    """Filled peak areas, baselines, drop lines and bound ticks for one run.

    A fragment of a deconvolution split is drawn as its modeled component curve
    above the baseline (its area is the area reported), not as the trace above it.
    """

    def __init__(self):
        super().__init__()
        self.fill = QPainterPath()
        self.fill_sel = QPainterPath()
        self.fill_manual = QPainterPath()
        self.fill_muted = QPainterPath()
        self.base = QPainterPath()
        self.drops = QPainterPath()
        self.modeled: list[tuple] = []    # (peak index, t, curve top, baseline) in data units, per fragment
        self.modeled_paths: list[tuple] = []   # (selected, fill, outline) in display units
        self.color = QColor("#1f77b4")
        self._rect = QRectF()
        self.pen_scale = 1.0              # line widths x this (picture export at a higher resolution)
        self.setZValue(5)

    def set_data(self, rt, y, peaks, color: str, selected: int = -1, transform=None, dx: float = 0.0,
                 muted: set | None = None):
        """Draw ``peaks`` of the trace (rt, y). ``dx`` shifts the drawing in time (a trace aligned
        to the other detector's time axis); ``muted`` peak indices are drawn grey (blank peaks)."""
        self.prepareGeometryChange()
        muted = muted or set()
        self.color = QColor(color)
        self.fill = QPainterPath()
        self.fill_sel = QPainterPath()
        self.fill_manual = QPainterPath()
        self.fill_muted = QPainterPath()
        self.base = QPainterPath()
        self.drops = QPainterPath()
        self.modeled = []
        self.modeled_paths = []
        sc, off = transform if transform else (1.0, 0.0)
        xmin = ymin = np.inf
        xmax = ymax = -np.inf
        for i, p in enumerate(peaks):
            a = int(np.searchsorted(rt, p.start, side="left"))
            b = int(np.searchsorted(rt, p.end, side="right"))
            ts = np.concatenate([[p.start], rt[a:b], [p.end]])
            ys = np.concatenate([[np.interp(p.start, rt, y)], y[a:b], [np.interp(p.end, rt, y)]])
            bl = p.baseline.eval(ts)
            ys = ys * sc + off
            bl = bl * sc + off
            ts = ts + dx
            curve = _modeled(p)
            if curve is not None:
                tc, top, base = curve
                self.modeled.append((i, tc, top, base))
                top, base, tc = top * sc + off, base * sc + off, tc + dx
                fill, outline = QPainterPath(), QPainterPath()
                _poly(fill, tc, top, (tc, base))
                _poly(outline, tc, top)
                self.modeled_paths.append((i == selected, fill, outline))
                xmin, xmax = min(xmin, tc[0]), max(xmax, tc[-1])
                ymin, ymax = min(ymin, base.min()), max(ymax, top.max())
            else:
                target = self.fill_sel if i == selected else (
                    self.fill_muted if i in muted else (self.fill_manual if "M" in p.flags else self.fill))
                _poly(target, ts, ys, (ts, bl))
            self.base.moveTo(float(ts[0]), float(bl[0]))
            self.base.lineTo(float(ts[-1]), float(bl[-1]))
            for t, yb, ysig in ((ts[0], bl[0], ys[0]), (ts[-1], bl[-1], ys[-1])):
                self.drops.moveTo(float(t), float(yb))
                self.drops.lineTo(float(t), float(ysig))
            xmin, xmax = min(xmin, ts[0]), max(xmax, ts[-1])
            ymin, ymax = min(ymin, bl.min(), ys.min()), max(ymax, ys.max())
        self._rect = QRectF() if not peaks else QRectF(xmin, ymin, xmax - xmin, ymax - ymin)
        self.update()

    def boundingRect(self):
        return self._rect

    def paint(self, p, *args):
        self._paint_modeled(p)
        c = self.color
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(c.red(), c.green(), c.blue(), 55)))
        p.drawPath(self.fill)
        p.setBrush(QBrush(theme.qcolor(theme.PLOT["manual_fill"])))
        p.drawPath(self.fill_manual)
        p.setBrush(QBrush(theme.qcolor(theme.PLOT["blank_fill"]), Qt.BDiagPattern))
        p.drawPath(self.fill_muted)
        p.setBrush(QBrush(QColor(c.red(), c.green(), c.blue(), 130)))
        p.drawPath(self.fill_sel)
        pen = QPen(QColor(theme.PLOT["baseline"]))
        pen.setCosmetic(True)
        pen.setWidthF(1.3 * self.pen_scale)
        p.setBrush(Qt.NoBrush)
        p.setPen(pen)
        p.drawPath(self.base)
        pen2 = QPen(theme.qcolor(theme.PLOT["drop"]))
        pen2.setCosmetic(True)
        pen2.setWidthF(1.0 * self.pen_scale)
        p.setPen(pen2)
        p.drawPath(self.drops)

    def _paint_modeled(self, p):
        """Each fragment's curve separately, so overlapping components stay filled."""
        c = self.color
        for selected, fill, outline in self.modeled_paths:
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(c.red(), c.green(), c.blue(), 130 if selected else 50)))
            p.drawPath(fill)
            pen = QPen(QColor(c.red(), c.green(), c.blue(), 230))
            pen.setCosmetic(True)
            pen.setWidthF((1.8 if selected else 1.1) * self.pen_scale)
            p.setBrush(Qt.NoBrush)
            p.setPen(pen)
            p.drawPath(outline)


def _modeled(peak, n: int = 240):
    """``(t, baseline + curve, baseline)`` of a deconvoluted fragment, or None."""
    dc = (getattr(peak, "extra", None) or {}).get("deconv_component")
    if not dc or not dc.get("profile") or not dc.get("parent_span"):
        return None
    from gcws.integration.deconv_split import fragment_curve
    lo, hi = (float(v) for v in dc["parent_span"])
    t = np.linspace(lo, hi, n)
    curve = fragment_curve(peak, t)
    if curve is None:
        return None
    base = peak.baseline.eval(t)
    return t, base + curve, base


class LabelsItem(pg.GraphicsObject):
    """Peak labels painted in screen space (fast for hundreds of peaks)."""

    def __init__(self):
        super().__init__()
        self.labels: list[tuple[float, float, str, bool]] = []
        self._rect = QRectF()
        self.setZValue(20)
        self.setFlag(self.GraphicsItemFlag.ItemIgnoresTransformations, False)

    def set_labels(self, labels):
        self.prepareGeometryChange()
        self.labels = labels
        if labels:
            xs = [l[0] for l in labels]
            ys = [l[1] for l in labels]
            self._rect = QRectF(min(xs), min(ys), max(xs) - min(xs) or 1e-6, max(ys) - min(ys) or 1e-6)
        else:
            self._rect = QRectF()
        self.update()

    def boundingRect(self):
        return self._rect

    def paint(self, p, *args):
        if not self.labels:
            return
        tr = p.transform()
        p.save()
        p.resetTransform()
        font = p.font()
        font.setPixelSize(10)
        p.setFont(font)
        vb = self.getViewBox()
        view = vb.viewRect() if vb is not None else None
        last_x = -1e9
        for x, y, text, bold in sorted(self.labels, key=lambda l: l[0]):
            if view is not None and not (view.left() <= x <= view.right()):
                continue
            pt = tr.map(QPointF(x, y))
            if pt.x() - last_x < 11 and not bold:
                continue
            last_x = pt.x()
            p.save()
            p.translate(pt.x(), pt.y() - 3)
            p.rotate(-90)
            p.setPen(QColor(theme.PLOT["label_selected"] if bold else theme.PLOT["label"]))
            f = p.font()
            f.setBold(bold)
            p.setFont(f)
            p.drawText(QPointF(0, 4), text)
            p.restore()
        p.restore()
