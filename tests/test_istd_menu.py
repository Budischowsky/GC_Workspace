"""Set selected peak as ISTD (submenu, no panel) and the peak-table row menu."""
import copy
from pathlib import Path

import numpy as np
import pytest

from test_ui import win


@pytest.fixture
def synthetic(win):
    from gcws.core.model import FID, TIC, Run, Signal
    from gcws.integration.engine import integrate
    rt = np.linspace(0, 20, 24001)                     # after the default 5.5 min solvent cut
    y = sum(1e5 * np.exp(-0.5 * ((rt - c) / 0.02) ** 2) for c in (8, 11, 14))
    y += 50 + np.random.default_rng(0).normal(0, 5, rt.size)
    run = Run(Path("synthetic.D"), None, fid=Signal(FID, rt, y))
    run._signals[TIC] = Signal(TIC, rt, y)
    ws = win.ws
    results = {k: integrate(run.signal(k), ws.methods.get(ws.methods.default_name(k))) for k in (FID, TIC)}
    st = ws.add_run(run, results)
    ws.set_active(st.id)
    assert len(ws.result(st.id, FID).peaks) == 3
    return win, st


def _entries(win):
    win.istd_menu.aboutToShow.emit()
    return [a.text() for a in win.istd_menu.actions()]


def test_row_menu_has_no_choose_columns(synthetic, monkeypatch):
    from PySide6.QtWidgets import QMenu
    from gcws.ui.docks import peak_table
    win, _st = synthetic
    seen = []

    class Recording(QMenu):
        def exec(self, *_):
            seen.append([a.text() for a in self.actions()])

    monkeypatch.setattr(peak_table, "QMenu", Recording)
    win.table.view.selectRow(0)
    assert win.table.selected_rows()
    win.table._menu(win.table.view.viewport().rect().center())
    assert len(seen) == 1 and "Set selected peak as ISTD" in seen[0]
    assert "Choose columns..." not in seen[0]


def test_istd_submenu_binds_nias_without_panel(synthetic):
    win, st = synthetic
    ws = win.ws
    assert win.istd_menu.menuAction() in win.quant_menu.actions()
    assert win.istd_menu.menuAction() in win.table.context_actions
    codes = [d["code"] for d in win.quant._defs()]
    assert [t.split()[0] for t in _entries(win)] == codes
    ws.select_peak(1)
    p = ws.selected_peak()
    win.docks["quant"].hide()
    win.istd_menu.actions()[0].trigger()
    assert ws.quant["istd_bindings"][st.id][codes[0]] == round(p.apex_rt, 4)
    assert not win.docks["quant"].isVisible()
    win.a_undo.trigger()
    assert codes[0] not in ws.quant.get("istd_bindings", {}).get(st.id, {})


def test_istd_submenu_binds_hs_without_panel(synthetic):
    win, st = synthetic
    ws = win.ws
    q = copy.deepcopy(ws.quant)
    q["mode"] = "hs_screening"
    ws.push_quant("HS", q)
    ws.set_panel(0, key="TIC")
    assert _entries(win) == [f"HS{i}" for i in range(1, 8)]
    cfg = copy.deepcopy(ws.quant.get("hs", {}))
    from gcws.quant.hs import default_defs
    cfg["istd_defs"] = default_defs()
    cfg["istd_defs"][2]["name"] = "Toluene-d8"
    q = copy.deepcopy(ws.quant)
    q["hs"] = cfg
    ws.push_quant("HS names", q)
    assert _entries(win)[2] == "HS3  Toluene-d8"
    ws.select_peak(2)
    p = ws.selected_peak()
    win.docks["quant"].hide()
    win.quant.hs_panel.codes.setCurrentIndex(0)
    win.istd_menu.actions()[2].trigger()
    bindings = ws.quant["hs"]["istd_bindings"][st.id]
    assert bindings == {"HS3": p.apex_rt}
    assert not win.docks["quant"].isVisible()
