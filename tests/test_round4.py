"""Round 4 regression checks on the real chromatogram widgets."""
import pytest

from test_ui import win, _load, _double_click


def _drag(widget, start, end, button):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    def send(kind, pos, changed, buttons):
        QApplication.sendEvent(widget, QMouseEvent(kind, QPointF(pos), QPointF(widget.mapToGlobal(pos)),
                                                   changed, buttons, Qt.NoModifier))
    send(QEvent.MouseButtonPress, start, button, button)
    for k in range(1, 9):
        QTest.qWait(20)  # pyqtgraph rate-limits mouse moves; exceed its frame interval
        send(QEvent.MouseMove, start + (end - start) * k / 8, Qt.NoButton, button)
    send(QEvent.MouseButtonRelease, end, button, Qt.NoButton)
    QApplication.processEvents()


def test_linked_intensity_axis(qtbot, win, samples):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    _load(qtbot, win, samples, ["07_"])
    c1, c2 = win.chroms
    c1.vb.setXRange(12, 20, padding=0)
    QApplication.processEvents()
    old = [p.vb.viewRange()[1][:] for p in win.chroms]
    rect = c1.vb.sceneBoundingRect()
    pos = c1.plot.mapFromScene(QPointF(rect.left() - 20, rect.center().y()))
    vp = c1.plot.viewport()
    _drag(vp, pos, pos + QPoint(0, -32), Qt.RightButton)
    QApplication.processEvents()
    new = [p.vb.viewRange()[1][:] for p in win.chroms]
    assert win.view_link.manual_y
    factor = (new[0][1] - new[0][0]) / (old[0][1] - old[0][0])
    assert factor < 1
    for before, after in zip(old, new):
        assert after[0] == pytest.approx(before[0])
        assert after[1] - after[0] == pytest.approx((before[1] - before[0]) * factor)
    c2.vb.setXRange(13, 16, padding=0)
    QApplication.processEvents()
    assert [p.vb.viewRange()[1] for p in win.chroms] == new
    c1.others.toggle()
    assert c1.vb.viewRange()[1] == new[0]
    ev = QWheelEvent(QPointF(pos), QPointF(vp.mapToGlobal(pos)), QPoint(), QPoint(0, 120),
                     Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
    QApplication.sendEvent(vp, ev)
    QApplication.processEvents()
    assert c1.vb.viewRange()[1][0] == pytest.approx(new[0][0])
    assert c1.vb.viewRange()[1][1] < new[0][1]
    _double_click(vp, pos)
    QApplication.processEvents()
    assert not win.view_link.manual_y
    for p in win.chroms:
        assert p.vb.viewRange()[0] == pytest.approx([13, 16])
    assert c1.vb.viewRange()[1] != new[0]


def test_unit_changes_refit_one_panel(qtbot, win, samples):
    from PySide6.QtWidgets import QApplication
    _load(qtbot, win, samples, ["07_"])
    c1, c2 = win.chroms
    c1.vb._scale_intensity(0.5)
    before = c2.vb.viewRange()[1][:]
    c1.norm.setChecked(True)
    QApplication.processEvents()
    assert c1.vb.viewRange()[1][1] < 200
    assert c2.vb.viewRange()[1] == before
    assert win.view_link.manual_y
