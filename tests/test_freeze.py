"""Plots stand still while panels are dragged (gcws.ui.freeze)."""
import pytest

pytest.importorskip("pytestqt")


@pytest.fixture
def frozen_module(qtbot):
    from gcws.ui import freeze
    freeze.install()
    yield freeze
    freeze._native = False
    freeze.disarm()


def test_a_plot_resized_during_a_gesture_is_laid_out_once_on_release(qtbot, frozen_module):
    import pyqtgraph as pg
    freeze = frozen_module
    w = pg.PlotWidget()
    qtbot.addWidget(w)
    w.resize(400, 300)
    w.show()
    qtbot.waitExposed(w)
    w.plot([0, 1, 2], [0, 1, 0])
    w.viewport().grab()                        # the axes measure their labels on the first paint
    qtbot.wait(50)
    before = w.plotItem.vb.width()

    freeze._native = True                      # as while the window border is dragged
    freeze.arm()
    w.resize(600, 300)
    qtbot.wait(20)
    assert w.plotItem.vb.width() == before     # not laid out while frozen
    assert w in freeze._frozen
    img = w.viewport().grab().toImage()        # painted from the picture: no error, the new size
    assert img.width() >= 590

    freeze._native = False
    freeze.disarm()
    qtbot.wait(20)
    assert w not in freeze._frozen
    assert w.plotItem.vb.width() > before + 150


def test_a_press_on_a_separator_arms_and_the_release_disarms(qtbot, frozen_module):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QMainWindow
    freeze = frozen_module
    win = QMainWindow()
    qtbot.addWidget(win)
    freeze.watch(win)
    press = QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5), QPointF(5, 5), Qt.LeftButton,
                        Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(win, press)
    assert freeze.is_armed()
    release = QMouseEvent(QEvent.MouseButtonRelease, QPointF(5, 5), QPointF(5, 5), Qt.LeftButton,
                          Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(win, release)
    assert not freeze.is_armed()


def test_a_lost_release_does_not_keep_the_plots_frozen(qtbot, frozen_module):
    freeze = frozen_module
    freeze.arm()                                # no button is held
    qtbot.waitUntil(lambda: not freeze.is_armed(), timeout=2000)


def test_icons_are_scaled_once_per_size():
    from PySide6.QtCore import QSize
    from gcws.ui.icons import _scaled, icon
    _scaled.cache_clear()
    ic = icon("zoom")
    a = ic.pixmap(QSize(20, 20))
    ic.pixmap(QSize(20, 20))
    assert _scaled.cache_info().hits >= 1 and not a.isNull()
