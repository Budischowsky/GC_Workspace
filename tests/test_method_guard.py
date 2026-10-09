"""Saving a method again from a workspace without migration conditions / quantification parameters does not
drop them unnoticed (2026-10-09: NIAS lost them, and the automation could not make the NIAS report)."""
from types import SimpleNamespace

import pytest

from test_report import qapp  # noqa: F401  (fixture)

MIG = {"simulant": "EtOH 95 %", "temperature": "40 C", "duration": "10 d"}
SETTINGS = {"reporting_limit": 0.01}


def _method(name, migration=None, settings=None):
    return {"format": "gcws-processing-method", "name": name,
            "sections": {"quant": {"mode": "nias_mgkg", "settings": settings or {}}, "migration": migration}}


def test_dropped_and_keep_from():
    from gcws.core import proc_method as PM
    old, new = _method("N", MIG, SETTINGS), _method("N")
    assert PM.dropped(old, new) == ["migration", "quant.settings"]
    assert PM.dropped(None, new) == [] and PM.dropped(new, old) == []
    kept = PM.keep_from(new, old, PM.dropped(old, new))
    assert kept["sections"]["migration"] == MIG and kept["sections"]["quant"]["settings"] == SETTINGS
    assert new["sections"]["migration"] is None                 # not changed in place


@pytest.mark.parametrize("choice, migration", [("keep", MIG), ("drop", None), ("cancel", "unchanged")])
def test_save_dialog_asks_before_dropping(qapp, tmp_path, monkeypatch, choice, migration):
    from PySide6.QtWidgets import QMessageBox, QWidget
    from gcws import paths
    from gcws.core import proc_method as PM
    from gcws.ui.dialogs import proc_method as D
    monkeypatch.setattr(paths, "DATA", tmp_path)
    PM.save(_method("NIAS", MIG, SETTINGS))
    monkeypatch.setattr(PM, "collect", lambda win, name, comment="": _method(name))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)   # replace NIAS: yes
    asked = []

    def answer(box):                                            # never a modal box on the desktop
        asked.append(box.text())
        buttons = {b.text(): b for b in box.buttons()}
        pick = {"keep": "Keep the saved ones", "drop": "Save without"}.get(choice)
        (buttons[pick] if pick else box.button(QMessageBox.Cancel)).click()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", answer)
    parent = QWidget()
    win = SimpleNamespace(ws=SimpleNamespace(log=lambda *a: None))
    dlg = D.SaveMethodDialog(win, parent)
    dlg.name.setEditText("NIAS")
    dlg._save()
    assert asked and "migration conditions" in asked[0]
    saved = PM.read(PM._file("NIAS"))["sections"]
    assert saved["migration"] == (MIG if migration == "unchanged" else migration)
    assert (dlg.saved is None) == (choice == "cancel")
    dlg.deleteLater()
    parent.deleteLater()
