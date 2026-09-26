"""Interactive integration tools (shared by Chromatogram 1 and 2).

Each tool turns mouse gestures into a :class:`ManualEvent` for the signal of
the panel it was made in; the workspace re-integrates and the result is drawn.
Snapping: split points snap to the nearest local minimum within +-3 points and
bounds to the signal; hold Shift to place exactly where the mouse is.

A panel may draw its trace shifted in time (the other detector aligned by the
FID-MS delay); its view box converts mouse positions back to the trace's own
time before a tool sees them.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QPen

from gcws.core.events import ManualEvent, ManualKind as K
from gcws.ui import theme

TOOLS = [
    # name, label, shortcut, tooltip
    ("select", "Select / zoom", "Z", "Click selects a peak; drag a box to zoom (double-click: whole run; wheel: "
                                     "zoom the time); drag a bound of the selected peak to move it"),
    ("pan", "Pan", "H", "Drag to pan"),
    ("baseline", "Draw baseline", "B", "Drag from baseline start to end (Shift: exact mouse height)"),
    ("split", "Split (drop line)", "S", "Click to split the peak with a drop line (snaps to the valley)"),
    ("delete", "Delete peak", "D", "Click a peak or drag across several to delete them"),
    ("add", "Add peak", "A", "Drag from peak start to end"),
    ("move", "Move start/end", "M", "Drag a peak start or end"),
    ("merge", "Merge peaks", "G", "Drag across the peaks to merge"),
    ("skim", "Tangent skim", "K", "Click the parent peak, then the rider (Shift: exponential)"),
    ("negative", "Negative peak", "N", "Drag across a negative peak"),
    ("reset", "Reset range", "R", "Drag to discard manual changes in a range"),
]
DRAG_TOOLS = {"baseline", "add", "merge", "negative", "reset", "delete", "move"}
SNAP_PX = 8


class ToolController(QObject):
    toolChanged = QtSignal(str)
    eventCreated = QtSignal(object, str)   # ManualEvent, signal key it belongs to
    peakClicked = QtSignal(int)

    def __init__(self, ws):
        super().__init__()
        self.ws = ws
        self.tool = "select"
        self._skim_parent: Optional[float] = None

    def set_tool(self, name: str) -> None:
        self.tool = name
        self._skim_parent = None
        self.toolChanged.emit(name)

    # -- context -----------------------------------------------------------

    def _context(self, key: Optional[str] = None):
        st = self.ws.active
        if st is None:
            return None, None, None
        key = self.ws.effective_key(st, key or self.ws.signal_key)
        sig = st.run.signal(key)
        res = self.ws.result(st.id, key)
        return st, sig, res

    def _key(self, key: Optional[str]) -> str:
        return self.ws.effective_key(self.ws.active, key or self.ws.signal_key)

    def _select_at(self, t: float, key: str) -> int:
        """Select the table's peak at time ``t`` of the ``key`` trace (another detector: by the delay)."""
        from gcws.core.keys import is_fid
        st = self.ws.active
        table_key = self.ws.active_key
        if st is not None and is_fid(key) != is_fid(table_key):
            t = t + (-st.delay_value if is_fid(key) else st.delay_value)
        idx = self.peak_index_at(t, table_key)
        self.ws.select_peak(idx)
        self.peakClicked.emit(idx)
        return idx

    @staticmethod
    def snap_valley(sig, t: float) -> float:
        i = int(np.searchsorted(sig.rt, t))
        lo, hi = max(0, i - 3), min(sig.n, i + 4)
        if hi - lo < 2:
            return t
        j = lo + int(np.argmin(sig.y[lo:hi]))
        return float(sig.rt[j])

    def _y_or_signal(self, vb, sig, t, y, shift, transform) -> Optional[float]:
        """Explicit anchor height (data units) unless the mouse is on the signal."""
        sc, off = transform
        on_sig = float(np.interp(t, sig.rt, sig.y))
        px = abs(vb.viewPixelSize()[1]) or 1e-9
        if not shift and abs(on_sig * sc + off - y) <= SNAP_PX * px:
            return None
        return float((y - off) / sc) if sc else float(y)

    def peak_index_at(self, t: float, key: Optional[str] = None) -> int:
        _, _, res = self._context(key)
        if res is None:
            return -1
        inside = [i for i, p in enumerate(res.peaks) if p.start <= t <= p.end]
        if inside:
            return min(inside, key=lambda i: res.peaks[i].end - res.peaks[i].start)
        return -1

    def bound_near(self, vb, t: float, key: Optional[str] = None, selected: int = -1) -> Optional[tuple[int, str]]:
        _, _, res = self._context(key)
        if res is None:
            return None
        px = abs(vb.viewPixelSize()[0])
        best = None
        order = sorted(range(len(res.peaks)), key=lambda i: i != selected)
        for i in order:
            p = res.peaks[i]
            for which, tb in (("start", p.start), ("end", p.end)):
                d = abs(tb - t) / px
                if d <= SNAP_PX and (best is None or d < best[0]):
                    best = (d, i, which)
        return (best[1], best[2]) if best else None

    # -- gestures ----------------------------------------------------------

    def click(self, vb, x: float, y: float, mods, transform, key: Optional[str] = None) -> bool:
        key = self._key(key)
        st, sig, res = self._context(key)
        if st is None or sig is None:
            return False
        shift = bool(mods & Qt.ShiftModifier)
        if self.tool in ("select", "pan", "move"):
            self._select_at(x, key)
            return True
        if self.tool == "split":
            t = x if shift else self.snap_valley(sig, x)
            self.eventCreated.emit(ManualEvent(K.SPLIT, t), key)
            return True
        if self.tool == "delete":
            idx = self.peak_index_at(x, key)
            if idx >= 0:
                self.eventCreated.emit(ManualEvent(K.DELETE, res.peaks[idx].apex_rt), key)
            return True
        if self.tool == "skim":
            idx = self.peak_index_at(x, key)
            if idx < 0:
                return True
            apex = res.peaks[idx].apex_rt
            if self._skim_parent is None:
                self._skim_parent = apex
                self.ws.message.emit("Tangent skim: now click the rider peak")
            else:
                self.eventCreated.emit(ManualEvent(K.SKIM, self._skim_parent, apex,
                                                   option="exponential" if shift else "tangent"), key)
                self._skim_parent = None
            return True
        return False

    def drag_finished(self, vb, x0, y0, x1, y1, mods, transform, grab=None, key: Optional[str] = None) -> bool:
        key = self._key(key)
        st, sig, res = self._context(key)
        if st is None or sig is None:
            return False
        shift = bool(mods & Qt.ShiftModifier)
        lo, hi = sorted((x0, x1))
        if hi - lo <= 0:
            return True
        tool = self.tool
        if grab is not None:
            idx, which = grab
            p = res.peaks[idx]
            t = x1 if shift else self.snap_valley(sig, x1)
            neighbour_shared = any(abs((q.end if which == "start" else q.start) - (p.start if which == "start" else p.end)) < 1e-6
                                   for j, q in enumerate(res.peaks) if j != idx)
            kind = K.MOVE_START if which == "start" else K.MOVE_END
            self.eventCreated.emit(ManualEvent(kind, t, ref_rt=p.apex_rt,
                                               option="shared" if neighbour_shared else ""), key)
            return True
        if tool == "baseline":
            ya = self._y_or_signal(vb, sig, x0, y0, shift, transform)
            yb = self._y_or_signal(vb, sig, x1, y1, shift, transform)
            if x1 < x0:
                ya, yb = yb, ya
            self.eventCreated.emit(ManualEvent(K.DRAW_BASELINE, lo, hi, y0=ya, y1=yb), key)
        elif tool == "add":
            self.eventCreated.emit(ManualEvent(K.ADD_PEAK, lo, hi), key)
        elif tool == "negative":
            self.eventCreated.emit(ManualEvent(K.NEGATIVE_PEAK, lo, hi), key)
        elif tool == "merge":
            self.eventCreated.emit(ManualEvent(K.MERGE, lo, hi), key)
        elif tool == "delete":
            self.eventCreated.emit(ManualEvent(K.DELETE, lo, hi), key)
        elif tool == "reset":
            self.eventCreated.emit(ManualEvent(K.RESET_RANGE, lo, hi), key)
        else:
            return False
        return True


class ToolViewBox(pg.ViewBox):
    """ViewBox that hands mouse gestures to the active integration tool.

    Right button (all boxes): a click asks for the mass spectrum at that time,
    a drag for the mean spectrum over the range, Shift+drag marks a background
    range; ``spectrumRequested(t0, t1, bg)`` is emitted in this box's x frame.
    Right-drag on an axis still scales it; left-drag on either axis pans the
    time window in every tool mode. A non-interactive box never runs
    integration tools: a left click is emitted as ``clicked(x)`` and left
    drags zoom or pan. ``panel`` (a ChromPanel) supplies the signal key and
    the time offset of the trace, so tools see the trace's own time.
    """
    spectrumRequested = QtSignal(object, object, object, object)   # t0, t1, bg (t0, t1) or None, (x, y)
    clicked = QtSignal(float, float)

    def __init__(self, controller: ToolController, interactive: bool = True, **kw):
        super().__init__(**kw)
        self.ctl = controller
        self.interactive = interactive
        self.panel = None                # the ChromPanel: its signal key, time offset and selection
        self._rdrag = None
        self.transform = (1.0, 0.0)      # display = data * scale + offset (active run)
        self.on_reset = None             # double-click: plot's default view
        self._grab = None
        self._drag_origin = None
        pen = QPen(QColor(theme.PLOT["baseline"]))
        pen.setCosmetic(True)
        pen.setWidthF(1.5)
        pen.setStyle(Qt.DashLine)
        self.preview = pg.PlotCurveItem(pen=pen)
        self.preview.setZValue(50)
        self.addItem(self.preview, ignoreBounds=True)
        self.band = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(*theme.PLOT["band_bg"]))
        self.band.setZValue(40)
        self.band.hide()
        self.addItem(self.band, ignoreBounds=True)

    # -- panel context: tools work in the trace's own time -----------------------------------

    def tool_key(self):
        return self.panel.tool_key() if self.panel is not None else None

    def dx(self) -> float:
        return self.panel.dx() if self.panel is not None else 0.0

    def selected_index(self) -> int:
        return self.panel.selected_index() if self.panel is not None else self.ctl.ws.selected

    # -- zoom like Agilent Enhanced Data Analysis ----------------------------------------------
    # left-drag (select tool): box zoom of time and intensity; double-click: the whole run in
    # every chromatogram (``on_reset``); wheel over the plot: time only, around the cursor, and
    # the intensity follows what is visible; wheel over an axis: that axis.

    def showAxRect(self, ax, **kwargs):
        kwargs.setdefault("padding", 0)              # exactly the box that was drawn
        super().showAxRect(ax, **kwargs)

    def wheelEvent(self, ev, axis=None):
        super().wheelEvent(ev, axis=0 if axis is None else axis)
        if axis is None and self.panel is not None:
            self.panel.linked_x_changed()

    def _on_axis(self, ev) -> bool:
        """The click was on an axis (outside the plot area): no tool acts there."""
        return not self.sceneBoundingRect().contains(ev.scenePos())

    def _axis_pan(self, ev):
        """Left-drag on the RT or intensity axis: move the time window, whatever tool is active."""
        ev.accept()
        x_last = self.mapSceneToView(ev.lastScenePos()).x()
        x_now = self.mapSceneToView(ev.scenePos()).x()
        if x_now != x_last:
            self.translateBy(x=x_last - x_now)
            if self.panel is not None:
                self.panel.linked_x_changed()

    def mouseClickEvent(self, ev):
        pos = self.mapSceneToView(ev.scenePos())
        if ev.button() == Qt.LeftButton and not ev.double() and self._on_axis(ev):
            ev.accept()                                 # a click on an axis never splits or deletes
            return
        if ev.button() == Qt.RightButton:
            self.spectrumRequested.emit(pos.x(), pos.x(), None, (pos.x(), pos.y()))
            ev.accept()
            return
        if ev.button() == Qt.LeftButton:
            if ev.double():
                (self.on_reset or self.autoRange)()
                ev.accept()
                return
            if not self.interactive:
                self.clicked.emit(pos.x(), pos.y())
                ev.accept()
                return
            if self.ctl.click(self, pos.x() - self.dx(), pos.y(), ev.modifiers(), self.transform, self.tool_key()):
                ev.accept()
                return
        super().mouseClickEvent(ev)

    def _right_drag(self, ev):
        ev.accept()
        pos = self.mapSceneToView(ev.scenePos())
        if ev.isStart():
            start = self.mapSceneToView(ev.buttonDownScenePos(Qt.RightButton))
            self._rdrag = (start.x(), start.y(), bool(ev.modifiers() & Qt.ShiftModifier))
        if self._rdrag is None:
            return
        x0, y0, bg = self._rdrag
        self.band.setBrush(pg.mkBrush(*theme.PLOT["band_bg" if bg else "band"]))
        self.band.setRegion((min(x0, pos.x()), max(x0, pos.x())))
        self.band.show()
        if ev.isFinish():
            self.band.hide()
            self._rdrag = None
            lo, hi = sorted((x0, pos.x()))
            if bg:
                self.spectrumRequested.emit(None, None, (lo, hi), (x0, y0))
            else:
                self.spectrumRequested.emit(lo, hi, None, (x0, y0))

    def mouseDragEvent(self, ev, axis=None):
        if axis is not None and ev.button() == Qt.LeftButton:
            self._axis_pan(ev)
            return
        if ev.button() == Qt.RightButton and axis is None:
            self._right_drag(ev)
            return
        if ev.button() != Qt.LeftButton or not self.interactive:
            super().mouseDragEvent(ev, axis)
            return
        self.band.setBrush(pg.mkBrush(*theme.PLOT["band_bg"]))
        tool = self.ctl.tool
        pos = self.mapSceneToView(ev.scenePos())
        if ev.isStart():
            start = self.mapSceneToView(ev.buttonDownScenePos())
            self._grab = None
            if tool in ("select", "move"):
                sel = self.selected_index()
                self._grab = self.ctl.bound_near(self, start.x() - self.dx(), self.tool_key(), sel)
                if tool == "select" and self._grab is not None and self._grab[0] != sel:
                    self._grab = None
            if tool not in DRAG_TOOLS and self._grab is None:
                super().mouseDragEvent(ev, axis)
                return
            self._drag_origin = (start.x(), start.y())
        if self._drag_origin is None:
            super().mouseDragEvent(ev, axis)
            return
        ev.accept()
        x0, y0 = self._drag_origin
        if tool == "baseline" and self._grab is None:
            self.preview.setData([x0, pos.x()], [y0, pos.y()])
        else:
            self.band.setRegion((min(x0, pos.x()), max(x0, pos.x())))
            self.band.show()
        if ev.isFinish():
            self.preview.setData([], [])
            self.band.hide()
            grab, self._grab, self._drag_origin = self._grab, None, None
            dx = self.dx()
            self.ctl.drag_finished(self, x0 - dx, y0, pos.x() - dx, pos.y(), ev.modifiers(), self.transform, grab,
                                   self.tool_key())
