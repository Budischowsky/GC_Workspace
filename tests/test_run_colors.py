"""A double determination's runs: one colour, A as it is and B a paler shade of it."""
import pytest


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _ws(samples, prefixes):
    from gcws.io.run_loader import load_run
    from gcws.ui.workspace import Workspace
    ws = Workspace()
    for prefix in prefixes:
        ws.add_run(load_run(next(samples.glob(prefix + "*.D"))), processed=False)
    return ws


def _by(ws, prefix):
    return next(s for s in ws.states() if s.run.path.name.startswith(prefix))


def test_shade_is_a_paler_or_deeper_mix():
    from PySide6.QtGui import QColor
    from gcws.ui import theme
    base = theme.RUN_COLORS[0]
    assert theme.shade(base, 0) == base
    b, c = QColor(theme.shade(base, 1)), QColor(theme.shade(base, 2))
    assert b.lightness() > QColor(base).lightness() > c.lightness()
    assert abs(b.hue() - QColor(base).hue()) <= 3


def test_double_determination_runs_share_a_colour(qapp, samples):
    from gcws.ui import theme
    ws = _ws(samples, ["11_", "09_", "07_"])              # B before A, another sample between
    a, b, other = _by(ws, "07_"), _by(ws, "11_"), _by(ws, "09_")
    assert (a.shade, b.shade) == (0, 1)
    assert a.color in theme.RUN_COLORS and b.color == theme.shade(a.color, 1)
    assert other.color in theme.RUN_COLORS and other.color != a.color
    assert ws.base_color(b) == a.color
    # a colour the analyst chose stays
    ws2 = _ws(samples, ["07_"])
    ws2.runs[ws2.order[0]].color = "#123456"
    from gcws.io.run_loader import load_run
    st = ws2.add_run(load_run(next(samples.glob("11_*.D"))), processed=False)
    assert ws2.runs[ws2.order[0]].color == "#123456" and st.color == theme.shade("#123456", 1)


def test_pair_by_hand_takes_the_first_runs_colour(qapp, samples):
    from gcws.ui import theme
    ws = _ws(samples, ["07_", "09_"])
    a, other = _by(ws, "07_"), _by(ws, "09_")
    assert a.color != other.color
    changed = ws.shade_group([a.id, other.id])
    assert changed == [other.id] and other.color == theme.shade(a.color, 1)
