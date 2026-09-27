"""P43: fixed m/z axis, ISTD submenu, Subtract baseline button, peak-table menu."""
import numpy as np
import pytest

from test_ui import win, _load


def _x_range(plot):
    return tuple(round(v, 6) for v in plot.getPlotItem().getViewBox().viewRange()[0])


def test_mz_axis_is_the_scan_range(qtbot, win, samples):
    _load(qtbot, win, samples, ["07_"])
    ws, sp = win.ws, win.spectrum
    lo, hi = ws.active.run.ms.mass_range()
    axis = sp.mz_axis_range()
    assert axis[0] <= lo and axis[1] >= hi and axis[0] % 10 == 0 and axis[1] % 10 == 0
    expected = (axis[0] - 3, axis[1] + 3)
    peaks = ws.active_result().peaks
    seen = set()
    for i in (0, len(peaks) // 2, len(peaks) - 1):
        ws.select_peak(i)
        if sp.spec is not None and sp.spec.ab.size:
            seen.add(_x_range(sp.plot))
    assert seen == {expected}                             # every spectrum on the same axis
    sp.plot.getPlotItem().getViewBox().setRange(xRange=(100, 120), padding=0)
    sp.plot._home()                                       # double-click
    assert _x_range(sp.plot) == expected


def test_mz_axis_fallback_without_ms(qtbot, win):
    assert win.spectrum.mz_axis_range() == win.spectrum.DEFAULT_MZ_AXIS == (50., 550.)


def test_istd_submenu_binds_without_opening_quant(qtbot, win, samples):
    _load(qtbot, win, samples, ["07_"])
    ws, st = win.ws, win.ws.active
    win.docks["quant"].hide()
    ws.select_peak(3)
    p = ws.selected_peak()
    win.istd_menu.aboutToShow.emit()
    actions = {a.text().split()[0]: a for a in win.istd_menu.actions()}
    assert {"IS1", "IS2"} <= set(actions) and all(a.isEnabled() for a in actions.values())
    actions["IS2"].trigger()
    assert ws.quant["istd_bindings"][st.id]["IS2"] == round(p.apex_rt, 4)
    assert not win.docks["quant"].isVisible()
    win.istd_menu.aboutToShow.emit()
    assert [a.text().split()[0] for a in win.istd_menu.actions() if a.isChecked()] == ["IS2"]
    ws.undo_group.undo()
    assert "IS2" not in (ws.quant.get("istd_bindings") or {}).get(st.id, {})
    assert win.setIstdAction.menu() is win.istd_menu
    assert win.setIstdAction in win.table.context_actions


def test_istd_submenu_disabled_without_peak(qtbot, win, samples):
    _load(qtbot, win, samples, ["07_"])
    win.ws.select_peak(-1)
    win.istd_menu.aboutToShow.emit()
    assert win.istd_menu.actions() and not any(a.isEnabled() for a in win.istd_menu.actions())


def test_hs_binding_takes_the_menu_code(qtbot, win, samples):
    import copy
    _load(qtbot, win, samples, ["07_"])
    ws, st = win.ws, win.ws.active
    q = copy.deepcopy(ws.quant)
    q["mode"] = "hs_screening"
    ws.push_quant("HS", q)
    ws.set_signal_key("TIC")
    assert ws.signal_key == "TIC"
    ws.select_peak(2)
    p = ws.selected_peak()
    code = win.quant.hs_panel.istd_codes()[1][0]
    win.set_istd_selected(code)
    assert ws.quant["hs"]["istd_bindings"][st.id][code] == p.apex_rt


def test_subtract_button_and_peak_menu(qtbot, win, samples):
    from PySide6.QtWidgets import QToolBar
    tb = win.findChild(QToolBar, "tb.main")
    button = tb.widgetForAction(win.a_subtract)
    assert button.property("primary") is True and not win.a_subtract.icon().isNull()
    assert button.property("primary") == tb.widgetForAction(win.a_search).property("primary")
    _load(qtbot, win, samples, ["07_"])
    win.ws.select_peak(0)
    win.table.view.selectRow(0)
    texts = [a.text() for a in win.table.build_menu().actions()]
    assert texts and not any("Choose columns" in t for t in texts)
    assert win.setIstdAction.text() in texts
