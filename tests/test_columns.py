"""Column chooser dialog and key-based column persistence."""
import pytest

pytest.importorskip("pytestqt")


def test_chooser_moves_and_orders(qtbot):
    from PySide6.QtCore import Qt
    from gcws.ui.dialogs.columns import ColumnChooserDialog
    cols = [("a", "A", ""), ("b", "B", ""), ("c", "C", "tip"), ("d", "D", "")]
    dlg = ColumnChooserDialog(cols, ["c", "a"], ["a", "b"])
    qtbot.addWidget(dlg)
    assert dlg.shown_keys() == ["c", "a"]
    assert dlg.available.keys() == ["b", "d"]
    dlg.available.item(1).setSelected(True)             # d
    dlg._move(dlg.available)
    assert dlg.shown_keys() == ["c", "a", "d"]
    dlg.shown.clearSelection()
    dlg.shown.item(2).setSelected(True)
    dlg._shift(-10 ** 6)                                 # d first
    assert dlg.shown_keys() == ["d", "c", "a"]
    dlg.shown.clearSelection()
    dlg.shown.item(1).setSelected(True)                  # c back to available, natural position
    dlg._move(dlg.shown)
    assert dlg.available.keys() == ["b", "c"]
    dlg._fill(dlg.defaults)
    assert dlg.shown_keys() == ["a", "b"]
    dlg._fill([])
    assert not dlg.box.button(dlg.box.StandardButton.Ok).isEnabled()


@pytest.fixture
def settings(tmp_path):
    from PySide6.QtCore import QCoreApplication, QSettings
    QCoreApplication.setOrganizationName("GCWorkspaceTest")
    QCoreApplication.setApplicationName("pytest-columns")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    QSettings().clear()
    return QSettings()


def test_table_layout_by_key(qtbot, settings):
    from gcws.ui.docks.peak_table import PeakTable
    from gcws.ui.models.peak_table import COLUMNS
    from gcws.ui.workspace import Workspace
    ws = Workspace()
    # old-style settings: only hidden keys (and an index-based header that must be dropped)
    settings.setValue("table/hidden", ["area", "height"])
    settings.setValue("table/header", b"junk")
    t = PeakTable(ws)
    qtbot.addWidget(t)
    shown = t.shown_keys()
    assert "area" not in shown and "height" not in shown and "rt" in shown
    assert settings.value("table/header") is None
    t.set_shown(["name", "rt", "num"])
    t.save_columns()
    t2 = PeakTable(ws)
    qtbot.addWidget(t2)
    assert t2.shown_keys() == ["name", "rt", "num"]
    # a column the saved layout has never seen appears with its default visibility
    import json
    state = json.loads(settings.value("table/layout"))
    state["order"] = [k for k in state["order"] if k != "score"]
    settings.setValue("table/layout", json.dumps(state))
    t3 = PeakTable(ws)
    qtbot.addWidget(t3)
    assert t3.shown_keys()[:3] == ["name", "rt", "num"] and "score" in t3.shown_keys()
    assert len(COLUMNS) == len(t3.layout_state()["order"])
