"""GUI smoke tests (pytest-qt)."""
import os

import pytest

pytest.importorskip("pytestqt")


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QSettings
    QCoreApplication.setOrganizationName("GCWorkspaceTest")
    QCoreApplication.setApplicationName("pytest")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    QSettings().clear()
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    from gcws.ui.main_window import MainWindow
    w = MainWindow()
    qtbot.addWidget(w)
    w.show()
    yield w
    w.ws.dirty = False


def _load(qtbot, w, samples, prefixes):
    paths = [str(next(samples.glob(p + "*.D"))) for p in prefixes]
    w.load_runs(paths)
    qtbot.waitUntil(lambda: w.loading == 0 and len(w.ws.runs) == len(paths), timeout=60000)


def test_load_tabs_and_overlay(qtbot, win, samples):
    _load(qtbot, win, samples, ["08_", "07_"])
    names = [win.run_tabs.tabText(i) for i in range(win.run_tabs.count())]
    assert names[0].startswith("07_26016606_130m_min_GIOSUN1635_A")      # injection order
    assert "[Blank]" in names[1]
    assert len(win.chrom.curves) == 2
    win.run_tabs.setCurrentIndex(1)
    assert win.ws.active.name == "08_EtOH"
    assert win.table.model.rowCount() > 10


def test_manual_integration_undo_redo(qtbot, win, samples):
    from gcws.core.events import ManualEvent, ManualKind as K
    _load(qtbot, win, samples, ["07_"])
    st = win.ws.active
    n0 = len(win.ws.active_result().peaks)
    win.tools.eventCreated.emit(ManualEvent(K.ADD_PEAK, 30.0, 30.2))
    assert len(win.ws.active_result().peaks) == n0 + 1
    assert win.table.model.rowCount() == n0 + 1
    win.a_undo.trigger()
    assert len(win.ws.active_result().peaks) == n0
    win.a_redo.trigger()
    assert len(win.ws.active_result().peaks) == n0 + 1
    assert any(r.action == "Manual integration" for r in win.ws.audit.records)
    assert st.events("FID")


def test_layout_presets_roundtrip(qtbot, win):
    from gcws.ui.layout import presets
    for name in presets.PRESETS:
        presets.apply_preset(win, name)
    presets.apply_preset(win, "Table left (classic)")
    presets.save_layout(win, "mine")
    presets.apply_preset(win, "Review")
    assert presets.restore_layout(win, "mine")
    assert "mine" in presets.saved_layouts()
    for d in win.docks.values():
        d.setFloating(False)


def test_project_roundtrip(qtbot, win, samples, tmp_path):
    from gcws.core.events import ManualEvent, ManualKind as K
    from gcws.core.ident import Identification
    _load(qtbot, win, samples, ["07_", "08_"])
    st = next(s for s in win.ws.states() if s.name.startswith("07_"))
    win.ws.set_active(st.id)
    win.tools.eventCreated.emit(ManualEvent(K.SPLIT, 13.42))
    p = win.ws.active_result().peaks[5]
    st.ident_set("FID").set(Identification(apex_rt=p.apex_rt, name="Test compound", cas="50-00-0", manual=True))
    digest = win.ws.active_result().digest
    win.ws.replicate_groups = [{"id": "g1", "name": "grp", "members": [st.id], "policy": "all"}]
    from gcws.core import project as P
    target = P.save(win.ws, tmp_path / "test.gcws")
    win.close_all()
    assert not win.ws.runs
    win.open_project(target)
    qtbot.waitUntil(lambda: win.loading == 0 and len(win.ws.runs) == 2 and win._pending_project is None,
                    timeout=60000)
    st2 = win.ws.runs[st.id]
    assert st2.events("FID") and st2.events("FID")[0].kind == K.SPLIT
    assert win.ws.result(st.id, "FID").digest == digest
    assert any(i.name == "Test compound" for i in st2.ident_set("FID").items)
    assert win.ws.replicate_groups[0]["members"] == [st.id]
