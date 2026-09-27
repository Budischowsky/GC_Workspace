import copy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from test_ui import win, _load
from gcws.ms.spectra import ScanRequest, subtract
from gcws.quant.service import rrt_rows


@pytest.fixture(scope="module")
def qgd_run():
    from gcws.io.run_loader import load_run
    path = Path(__file__).resolve().parents[2] / "NIAS Working/samples/26012850_Sample1_A.qgd"
    if not path.exists():
        pytest.skip("Local screenshot QGD unavailable")
    return load_run(path)


@pytest.mark.parametrize("rt,scan,count", [(5.465, 594, 24), (6.275, 756, 35)])
def test_qgd_detector_offset_removed(qgd_run, rt, scan, count):
    """Shimadzu scans store every mass on a ~500-count offset; loaded, they read like Agilent data."""
    ms = qgd_run.ms
    i = ms.scan_at_rt(rt)
    assert i + 1 == scan
    mz, ab = ms.nominal_spectrum_arrays([i])
    assert mz.size == count and (ab > 0).all()
    # The chromatogram keeps the instrument's TIC (HS areas unchanged); the points are offset-free.
    np.testing.assert_array_equal(qgd_run.signal("TIC").y, ms.stored_tic)
    assert ms.instrument_tic and ms.tic()[i] < ms.stored_tic[i]


def test_remove_offset_flat_floor_and_sparse():
    from gcws.io.shimadzu import remove_offset
    rng = np.random.default_rng(1)
    mz = np.arange(35., 351.)
    ab = 500 + rng.uniform(-10, 10, mz.size)                    # bounded: no 3σ outliers
    ab[[5, 9, 25]] += (1200, 800, 300)                        # m/z 40, 44, 60
    cmz, cab = remove_offset(mz, ab)
    assert cmz.tolist() == [40, 44, 60]
    assert cab == pytest.approx([1200, 800, 300], abs=40)
    sparse = (np.array([43., 57.]), np.array([12., 99.]))
    out = remove_offset(*sparse)                                # already thresholded: untouched
    assert out[0] is sparse[0] and out[1] is sparse[1]


def test_spectrum_plot_shows_all_ions_and_menus(qtbot, win):
    win.spectrum.plot.show_spectrum(np.arange(40), np.ones(40))
    assert win.spectrum.plot._mz.size == 40
    removed = {"Hide noise", "Previous scan [←]", "Next scan [→]", "Back to peak [Esc]", "Clear background",
               "Spectrum mode", "subtract blank"}
    for menu in (win.ms_menu, win.spectrum.context_menu):
        texts = {a.text() for a in menu.actions()} | {a.menu().title() for a in menu.actions() if a.menu()}
        assert not texts & removed
        assert "Library hits" in texts and "Own library selection and options" in texts


def test_noise_plot_and_subtraction(qtbot, win, qgd_run):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    ws, sp = win.ws, win.spectrum
    ws.add_run(qgd_run)
    st = ws.active
    sp.show_range(ScanRequest(st.id, 5.465, 5.465))
    assert len(sp.plot._mz) == 24 and len(sp.points()) == 24
    sp.bg_range = (4., 4.1)
    before = copy.deepcopy(st.spectrum_overrides)
    win.a_subtract.trigger()
    assert win.a_subtract.isChecked() and sp.subtraction_state == "apex"
    sp.show_range(ScanRequest(st.id, 6.275, 6.275))
    assert sp.subtraction_state == "base"
    sp.show_range(ScanRequest(st.id, 6.275, 6.275))
    assert sp.subtraction_state == "base"
    sp.show_range(ScanRequest(st.id, 5.465, 5.465))
    assert sp.subtraction_state == "done"
    expected = subtract(st.run.ms.nominal_spectrum_arrays([755]), st.run.ms.nominal_spectrum_arrays([593]))
    np.testing.assert_array_equal(sp.spec.mz, expected[0])
    np.testing.assert_allclose(sp.spec.ab, expected[1])
    assert sp.bg_range == (4., 4.1) and st.spectrum_overrides == before
    with qtbot.waitSignal(sp.nistRequested) as emitted:
        sp._emit(sp.nistRequested)
    assert emitted.args[0] == sp.points()
    sp.copy_msp()
    assert QGuiApplication.clipboard().text() == sp.msp()
    assert "minus baseline scan 594" in sp.msp()
    win.activateWindow()
    win.setFocus()
    qtbot.keyClick(win, Qt.Key_Escape)
    assert sp.subtraction_state == "off" and not win.a_subtract.isChecked()
    assert sp.bg_range == (4., 4.1)
    win.a_subtract.trigger()
    sp.show_range(ScanRequest(st.id, None, None, (4., 5.)))
    assert sp.subtraction_state == "apex"
    win.a_subtract.trigger()
    assert sp.subtraction_state == "off"
    win.a_subtract.trigger()
    sp.show_range(ScanRequest(st.id, 6.275, 6.275))
    sp.show_range(ScanRequest(st.id, 5.465, 5.465))
    sp.show_range(ScanRequest(st.id, None, None, (4., 4.1)))
    assert sp.source == "peak" and sp.subtraction_state == "off"
    win.a_subtract.trigger()
    sp._on_active()
    assert not win.a_subtract.isChecked()


def test_hs_parallel_units_missing_denominator():
    from test_hs import workspace
    from gcws.quant.hs import compute
    ws = workspace()
    for unit in ("µg/HS", "µg/dm²", "µg/g"):
        ws.quant["hs"]["unit"] = unit
        row = compute(ws).rows["sample"][7]
        assert [row[k] for k in ("ug_hs", "ug_dm2", "ug_g")] == [2, 4, 1]
    ws.quant["hs"]["samples"]["sample"]["mass_g"] = 0
    row = compute(ws).rows["sample"][7]
    assert row["ug_hs"] == 2 and row["ug_dm2"] == 4
    assert row["ug_g"] is None and row["conc"] is None
    ws.quant["hs"]["istd_bindings"] = {"sample": {"HS2": None}}
    row = compute(ws).rows["sample"][7]
    assert all(row[k] is None for k in ("ug_hs", "ug_dm2", "ug_g"))


def test_rrt_detector_reference_and_missing():
    q = {"mode": "nias_mgkg", "rrt_reference": "S"}
    st = SimpleNamespace(delay_value=1.)
    sample = SimpleNamespace(standards=[dict(code="S", row_id=1, fid_rt=10.)])
    assert rrt_rows(sample, q, st, [SimpleNamespace(apex_rt=20.)], "FID")[0]["rrt"] == 2
    assert rrt_rows(sample, q, st, [SimpleNamespace(apex_rt=19.)], "TIC")[0]["rrt"] == pytest.approx(19/9)
    sample.standards[0]["fid_rt"] = 11.
    assert rrt_rows(sample, q, st, [SimpleNamespace(apex_rt=20.)], "FID")[0]["rrt"] == pytest.approx(20/11)
    for rt in (None, 0, -1, float("nan")):
        sample.standards[0]["fid_rt"] = rt
        row = rrt_rows(sample, q, st, [SimpleNamespace(apex_rt=20.)], "FID")[0]
        assert row["rrt"] is None and row["rrt_status"]
    sample.standards[0].update(fid_rt=10., row_id=None)
    assert rrt_rows(sample, q, st, [SimpleNamespace(apex_rt=20.)], "FID")[0]["rrt"] is None
    q["rrt_reference"] = "removed"
    assert rrt_rows(sample, q, st, [SimpleNamespace(apex_rt=20.)], "FID")[0]["rrt"] is None


def test_independent_cuts_hs_toggle_and_saved_settings(qtbot, win, samples, tmp_path):
    from gcws.core import proc_method as PM, project as P
    _load(qtbot, win, samples, ["07_"])
    ws, st, menu = win.ws, win.ws.active, win.chrom_menu
    ws.set_solvent_cut(True, 6., key="FID")
    assert ws.solvent_cut(st, "FID") == 6 and ws.solvent_cut(st, "TIC") is None
    ws.set_solvent_cut(True, 7., key="TIC")
    assert ws.solvent_cut(st, "FID") == 6 and ws.solvent_cut(st, "TIC") == 7
    assert win.chrom2.curves[st.id].xData[0] >= 7 + win.chrom2.shift(st)
    assert all(p.apex_rt >= 7 for p in ws.result(st.id, "TIC").peaks)
    menu.ms_cut.trigger()
    assert ws.solvent_cut(st, "TIC") is None and ws.solvent_cut(st, "FID") == 6
    win.a_undo.trigger()
    assert menu.ms_cut.isChecked() and ws.solvent_cut(st, "TIC") == 7
    assert win.a_undo.text() == "Undo" and win.a_redo.text() == "Redo"
    assert "MS solvent cut" in win.a_redo.toolTip()
    q = copy.deepcopy(ws.quant)
    q.update(rrt_reference="FC17", mode="hs_screening")
    q["hs"] = {"solvent_cut": False, "solvent_end": 3.}
    ws.push_quant("HS", q)
    assert not menu.cut.isChecked() and menu.end.value() == 3
    for _ in range(2):
        menu.cut.trigger()
        assert menu.cut.isChecked() and ws.solvent_cut(st, "TIC") == 3
        menu.cut.trigger()
        assert not menu.cut.isChecked() and ws.solvent_cut(st, "TIC") is None
    q = copy.deepcopy(ws.quant)
    q["mode"] = "nias_mgkg"
    ws.push_quant("NIAS", q)
    assert menu.cut.isChecked() and menu.ms_cut.isChecked()
    assert menu.end.value() == 6 and menu.ms_end.value() == 7
    method = PM.collect(win, "independent")
    saved = P.read(P.save(ws, tmp_path / "independent.gcws"))
    assert saved["quant"]["ms_solvent"] == {"enabled": True, "end": 7.}
    assert saved["quant"]["rrt_reference"] == "FC17"
    ws.set_solvent_cut(False, key="TIC")
    PM.apply(win, method, ["quant"])
    assert ws.solvent_cut(st, "TIC") == 7
    legacy = copy.deepcopy(method)
    del legacy["sections"]["quant"]["ms_solvent"]
    PM.apply(win, legacy, ["quant"])
    assert ws.solvent_cut(st, "TIC") == pytest.approx(6 - st.delay_value)
    assert "Migration conditions..." not in [a.text() for a in win.quant_menu.actions()]
    ws.set_panels(["FID", "TIC"], [False, False])
    win.a_subtract.trigger()
    win.chrom._spectrum_request(13.5, 13.5, None, None)
    a = st.run.ms.scan_at_rt(13.5 - st.delay_value)
    assert win.spectrum.subtraction_apex == a
    win.chrom._spectrum_request(13., 13., None, None)
    b = st.run.ms.scan_at_rt(13. - st.delay_value)
    expected = subtract(st.run.ms.nominal_spectrum_arrays([a]), st.run.ms.nominal_spectrum_arrays([b]))
    np.testing.assert_allclose(win.spectrum.spec.ab, expected[1])


def test_rrt_binding_columns_and_project_roundtrip(qtbot, win, samples, tmp_path):
    from PySide6.QtCore import Qt
    from gcws.core import project as P
    from gcws.ui.models.peak_table import COLUMN_KEYS
    _load(qtbot, win, samples, ["07_"])
    ws, st = win.ws, win.ws.active
    peaks = ws.result(st.id, "FID").peaks
    reference_index = min(range(len(peaks)), key=lambda i: abs(peaks[i].apex_rt - 13.42))
    rt = peaks[reference_index].apex_rt
    q = copy.deepcopy(ws.quant)
    q["istd_defs"] = [dict(code="REF", name="Reference", concentration=.1, target_rt=rt, quantify=True)]
    q["istd_bindings"] = {st.id: {"REF": round(rt, 4)}}
    ws.push_quant("Reference definition", q)
    combo = win.quant.rrt_reference
    combo.setCurrentIndex(combo.findData("REF"))
    combo.activated.emit(combo.currentIndex())
    assert ws.quant["rrt_reference"] == "REF"
    assert ws.quant_rows(st.id, "FID")[reference_index]["rrt"] == 1
    ms_peaks = ws.result(st.id, "TIC").peaks
    assert ws.quant_rows(st.id, "TIC")[0]["rrt"] == pytest.approx(ms_peaks[0].apex_rt / (rt - st.delay_value))
    ws.set_panel(0, key="TIC")
    win.table.set_shown(["rt", "rrt"])
    win.table.set_value_filter("rrt", ">", 1)
    assert win.table.proxy.rowCount() > 0
    values = win.table.table_rows()
    assert values[0] == ["RT [min]", "RRT"]
    assert all(float(row[1]) > 1 for row in values[1:])
    win.table.clear_value_filter()
    idx = win.table.model.index(0, COLUMN_KEYS.index("rrt"))
    assert "REF" in idx.data(Qt.ToolTipRole)
    q = copy.deepcopy(ws.quant)
    q["istd_bindings"][st.id]["REF"] = None
    ws.push_quant("Unbind RRT reference", q)
    assert ws.quant_rows(st.id, "TIC")[0]["rrt"] is None
    win.a_undo.trigger()
    assert ws.quant_rows(st.id, "TIC")[0]["rrt"] is not None
    ws.set_solvent_cut(True, 7., key="TIC")
    path = P.save(ws, tmp_path / "rrt.gcws")
    win.close_all()
    win.open_project(path)
    qtbot.waitUntil(lambda: win.loading == 0 and win._pending_project is None, timeout=60000)
    ws.recompute_quant()
    assert ws.quant["rrt_reference"] == "REF"
    assert ws.solvent_cut(ws.active, "TIC") == 7.
    assert ws.quant_rows(ws.active_id, "TIC")[0]["rrt"] is not None
