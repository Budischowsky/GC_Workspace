"""P48: sticks never vanish, labels sit on their sticks without overlapping, fitted m/z axis."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF

from gcws.ui.docks.spectrum import StickPlot, smart_mz_range
from test_ui import win, _load

PEAKS = {43: 99, 44: 28, 45: 8, 46: 62, 73: 3, 177: 75, 178: 12, 207: 4, 281: 100, 282: 25, 327: 65, 328: 18}


def _spectrum():
    mz = np.arange(35, 600, dtype=float)
    ab = np.random.default_rng(1).uniform(0, 0.5, mz.size)          # noise does not stretch the axis
    for m, v in PEAKS.items():
        ab[m - 35] = v
    return mz, ab


def test_smart_range_fits_the_signal():
    mz, ab = _spectrum()
    assert smart_mz_range(mz, ab) == (30.0, 350.0)
    assert smart_mz_range(mz, ab, (35, 697)) == (30.0, 350.0)
    assert smart_mz_range(mz, ab, (50, 300)) == (40.0, 330.0)          # capped by the scan range, data kept
    assert smart_mz_range(mz[:240], ab[:240], (50, 220)) == (40.0, 220.0)          # the margin is capped
    lo, hi = smart_mz_range([91, 92], [100, 8])
    assert hi - lo >= 60 and lo <= 91 - 5 and hi >= 92 + 5
    lo, hi = smart_mz_range([100, 530], [100, 50], (50, 550))
    assert (lo, hi) == (80.0, 550.0)


@pytest.fixture
def plot(qtbot):
    p = StickPlot()
    qtbot.addWidget(p)
    p.resize(800, 330)
    p.show()
    qtbot.waitExposed(p)
    return p


def _vb(p):
    return p.getPlotItem().getViewBox()


def test_every_stick_is_drawn_on_a_wide_axis(plot, qtbot):
    mz, ab = _spectrum()
    plot.full_range = True
    plot.range_provider = lambda: (30, 700)
    plot.show_spectrum(mz, ab)
    qtbot.wait(20)
    img = plot.grab().toImage()
    bg = img.pixelColor(img.width() // 2, 5).name()
    import pyqtgraph as pg
    for m in PEAKS:
        if PEAKS[m] < 20:
            continue
        pt = plot.mapFromScene(_vb(plot).mapViewToScene(pg.Point(m, 10)))
        dpr = img.devicePixelRatio()
        x, y = int(pt.x() * dpr), int(pt.y() * dpr)
        assert any(img.pixelColor(x + d, y).name() != bg for d in (-1, 0, 1)), m


def test_labels_sit_on_their_sticks_without_overlap(plot, qtbot):
    mz, ab = _spectrum()
    plot.show_spectrum(mz, ab, marks={43: ("43", "ok")})
    labels = plot.label_texts()
    assert {"43", "281", "177", "327", "46"} <= set(labels)
    rel = dict(zip(mz.astype(int), ab / ab.max() * 100))
    rects = []
    for t in plot.texts:
        m = int(t.textItem.toPlainText())
        assert t.pos().x() == m and t.pos().y() == pytest.approx(rel[m])   # anchored at its stick tip
        rects.append(t.mapRectToScene(t.boundingRect()))
    for i, a in enumerate(rects):
        assert not any(a.intersects(b.adjusted(1, 1, -1, -1)) for b in rects[i + 1:])
    (x0, x1), (y0, y1) = _vb(plot).viewRange()
    assert (x0, x1) == (30.0, 350.0) and y0 == 0 and 100 < y1 < 125


def test_box_zoom_refits_abundance_and_reveals_labels(plot, qtbot):
    mz, ab = _spectrum()
    plot.show_spectrum(mz, ab)
    assert "44" not in plot.label_texts()
    _vb(plot).showAxRect(QRectF(170, 0, 20, 5))                          # a box low on the axis
    (x0, x1), (y0, y1) = _vb(plot).viewRange()
    assert x0 < 177 < x1 and y0 == 0 and 75 < y1 < 95                   # fitted to 177, not to the box
    plot._layout()
    assert {"177", "178"} <= set(plot.label_texts())
    _vb(plot).showAxRect(QRectF(40, 0, 10, 100))
    plot._layout()
    assert {"43", "44", "45", "46"} <= set(plot.label_texts())
    plot._home()
    assert tuple(_vb(plot).viewRange()[0]) == (30.0, 350.0)


def test_click_picks_the_nearest_stick_within_pixels(plot, qtbot):
    plot.show_spectrum(np.array(list(PEAKS), float), np.array(list(PEAKS.values()), float))
    got = []
    plot.ionClicked.connect(got.append)

    class Ev:
        def __init__(self, x):
            self._p = _vb(plot).mapViewToScene(__import__("pyqtgraph").Point(x, 50))

        def button(self):
            from PySide6.QtCore import Qt
            return Qt.LeftButton

        def double(self):
            return False

        def scenePos(self):
            return self._p

    px = _vb(plot).viewPixelSize()[0]
    assert 4 * px > 0.7                                                  # farther than the old m/z tolerance
    plot._clicked(Ev(207 + 4 * px))
    plot._clicked(Ev(207 + 12 * px))                                     # too far: nothing
    assert got == [207]


def test_mirror_labels_reference_below(plot, qtbot):
    mz, ab = _spectrum()
    plot.show_spectrum(mz, ab, ref=[(43, 999), (177, 600), (281, 800)])
    (_, _), (y0, y1) = _vb(plot).viewRange()
    assert y0 == pytest.approx(-y1) and y1 > 100
    below = [t.textItem.toPlainText() for t in plot.texts if t.pos().y() < 0]
    assert set(below) == {"43", "177", "281"}


def test_dock_fits_each_spectrum_within_the_scan_range(qtbot, win, samples):
    _load(qtbot, win, samples, ["07_"])
    ws, sp = win.ws, win.spectrum
    b_lo, b_hi = sp.mz_axis_range()
    peaks = ws.active_result().peaks
    checked = 0
    for i in (0, 3, len(peaks) // 2, len(peaks) - 1):
        ws.select_peak(i)
        if sp.spec is None or not sp.spec.ab.size:
            continue
        checked += 1
        lo, hi = _vb(sp.plot).viewRange()[0]
        rel = sp.spec.ab / sp.spec.ab.max() * 100
        sig = sp.spec.mz[rel >= 1]
        assert b_lo <= lo < sig.min() and sig.max() < hi <= b_hi
    assert checked >= 2
    sp.full_range_action.setChecked(True)
    assert tuple(_vb(sp.plot).viewRange()[0]) == (b_lo, b_hi)
    from PySide6.QtCore import QSettings
    assert QSettings().value("spectrum/full_mz_range", type=bool)
    sp.full_range_action.setChecked(False)
    assert tuple(_vb(sp.plot).viewRange()[0]) != (b_lo, b_hi)
