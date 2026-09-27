"""Blank subtraction: alignment, trace subtraction and peak-level matching."""
from types import SimpleNamespace

import numpy as np
import pytest

from gcws.core.model import Baseline, Peak, Signal
from gcws.quant.blank_match import classify, match
from gcws.signal.align import peak_shift, xcorr_shift
from gcws.signal.blank import BlankOptions, subtract


def gauss(t, c, h, w=0.02):
    return h * np.exp(-0.5 * ((t - c) / w) ** 2)


RT = np.arange(5.0, 20.0, 1 / 1200.0)          # 20 Hz


def sample_and_blank(shift=0.006, seed=3):
    rng = np.random.default_rng(seed)
    blank_peaks = [(7.0, 800), (9.5, 1500), (13.0, 600), (16.0, 900)]
    sample_only = [(8.2, 2000), (11.0, 3000), (14.5, 1200)]
    base_s = 50 + 2.0 * (RT - 5)                 # sample baseline (drifting)
    base_b = 20 + 0.5 * (RT - 5)                 # a different detector offset in the blank
    y_b = base_b + sum(gauss(RT, c, h) for c, h in blank_peaks) + rng.normal(0, 2, RT.size)
    y_s = base_s + sum(gauss(RT, c + shift, h) for c, h in blank_peaks) \
        + sum(gauss(RT, c, h) for c, h in sample_only) + rng.normal(0, 2, RT.size)
    return Signal("FID", RT, y_s), Signal("FID", RT, y_b), blank_peaks, sample_only


def test_peak_shift_and_xcorr():
    s, b, blank_peaks, _ = sample_and_blank(shift=0.006)
    found = peak_shift([c + 0.006 for c, _ in blank_peaks] + [8.2, 11.0], [c for c, _ in blank_peaks], 0.05)
    assert found is not None and found[0] == pytest.approx(0.006, abs=1e-6) and found[1] == 4
    assert peak_shift([1.0], [1.001], 0.05) is None            # too few pairs
    shift, q = xcorr_shift(s.rt, s.y, b.rt, b.y, 6.0, 19.0, 0.05)
    assert shift == pytest.approx(0.006, abs=0.0015) and q > 0


def _area(sig, lo, hi):
    sl = sig.window(lo, hi)
    y = sig.y[sl]
    line = np.linspace(y[0], y[-1], y.size)
    return float(np.trapezoid(y - line, sig.rt[sl]))


def test_subtract_peaks_mode_keeps_baseline_and_sample_peaks():
    s, b, blank_peaks, sample_only = sample_and_blank()
    opts = BlankOptions(align="auto")
    apexes = ([c + 0.006 for c, _ in blank_peaks] + [c for c, _ in sample_only], [c for c, _ in blank_peaks])
    out, aligns = subtract(s, [("blank", b, apexes[1])], opts, "peaks", "FID - Blank", None, apexes[0])
    assert aligns[0].shift == pytest.approx(0.006, abs=1e-6)
    assert out.key == "FID - Blank" and "blank" in out.label
    for c, h in blank_peaks:                     # the blank's peaks are gone (residual < 3 % of the peak)
        sl = out.window(c + 0.006 - 0.05, c + 0.006 + 0.05)
        resid = out.y[sl] - (50 + 2.0 * (out.rt[sl] - 5))
        assert np.abs(resid).max() < 0.03 * h + 10
    for c, h in sample_only:                     # the sample's own peaks keep their area (< 1 %)
        assert _area(out, c - 0.1, c + 0.1) == pytest.approx(_area(s, c - 0.1, c + 0.1), rel=0.01)
    quiet = out.window(18.5, 19.5)               # sample baseline untouched in "peaks" mode
    assert np.median(out.y[quiet]) == pytest.approx(np.median(s.y[quiet]), abs=3)


def test_subtract_full_mode_removes_the_offset():
    s, b, *_ = sample_and_blank(shift=0.0)
    out, _ = subtract(s, [("blank", b, None)], BlankOptions(align="off", clip=False), "full", "FID - Blank")
    quiet = out.window(18.5, 19.5)
    expect = np.median(s.y[quiet] - b.y[b.window(18.5, 19.5)])
    assert np.median(out.y[quiet]) == pytest.approx(expect, abs=3)
    clipped, _ = subtract(s, [("blank", b, None)], BlankOptions(align="off", clip=True), "full", "FID - Blank")
    assert clipped.y.min() >= 0


def _peak(apex, area, w=0.03):
    return Peak(start=apex - w, end=apex + w, apex_rt=apex, baseline=Baseline("line", apex - w, 0, apex + w, 0),
                area=area)


def test_blank_match_one_to_one_and_classes():
    sample = [_peak(7.006, 1000), _peak(8.2, 5000), _peak(9.506, 20000), _peak(13.006, 1900)]
    blank = [_peak(7.0, 800), _peak(9.5, 1500), _peak(13.0, 600), _peak(13.02, 50)]
    m = match(sample, blank, shift=0.006, rt_tol=0.04, blank_run="b")
    assert set(m) == {0, 2, 3}
    assert m[0].status == "blank" and m[0].ratio == pytest.approx(1.25)
    assert m[2].status == "also" and m[3].status == "partly"
    assert m[3].blank_index == 2                 # the overlapping, nearest blank peak, used once
    # a spectral gate rejects a same-time, different-substance pair
    m2 = match(sample, blank, shift=0.006, rt_tol=0.04, spectra=lambda i, j: 0.2 if i == 0 else 0.95)
    assert 0 not in m2 and 2 in m2
    assert classify(2.9, 3.0) == "blank" and classify(5, 3.0) == "partly" and classify(50, 3.0) == "also"
    assert "blank level ×1.2" in m[0].text


def test_options_roundtrip():
    o = BlankOptions(mode_fid="full", scale=1.5, rt_tol=0.02)
    assert BlankOptions.from_dict(o.to_dict()) == o
    assert BlankOptions.from_dict({"unknown": 1, "scale": 2.0}).scale == 2.0


@pytest.fixture(scope="module")
def ws_blank(samples):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gcws.core.model import FID
    from gcws.integration.engine import integrate
    from gcws.io.run_loader import load_run
    from gcws.signal.delay import estimate_delay
    from gcws.ui.workspace import Workspace
    ws = Workspace()
    ids = {}
    for prefix in ("06_", "07_", "08_"):
        run = load_run(next(samples.glob(prefix + "*.D")))
        st = ws.add_run(run, {FID: integrate(run.fid, ws.methods.get(ws.methods.default_name(FID)))},
                        delay=estimate_delay(run.fid, run.signal("TIC")))
        ids[prefix] = st.id
    return ws, ids


def test_real_blank_subtraction(ws_blank):
    ws, ids = ws_blank
    st = ws.runs[ids["07_"]]
    assert ws.blank_ids(st) == [ids["08_"]]
    raw = ws.result(st.id, "FID")
    der = ws.result(st.id, "FID - Blank")
    assert der is not None and len(der.peaks) < len(raw.peaks)
    for t in (13.417, 18.917):                   # internal standards survive with (nearly) their area
        pr = min(raw.peaks, key=lambda p: abs(p.apex_rt - t))
        pd = min(der.peaks, key=lambda p: abs(p.apex_rt - t))
        assert abs(pd.apex_rt - pr.apex_rt) < 0.005 and pd.area == pytest.approx(pr.area, rel=0.05)
    bm = ws.blank_matches(st.id, "FID")
    assert any(m.status == "blank" for m in bm.values())
    assert "FID - Blank" in ws.signals_for(st) and "TIC - Blank" in ws.signals_for(st)
    blank_st = ws.runs[ids["08_"]]
    assert "FID - Blank" not in ws.signals_for(blank_st)


def test_nias_numbers_unchanged_by_blank_features(ws_blank):
    ws, ids = ws_blank
    rid = ids["07_"]
    ws.recompute_quant()
    before = {i: (r.get("corr_area"), r.get("conc")) for i, r in ws.quant_result.rows.get(rid, {}).items()}
    ws.set_signal_key("FID - Blank")
    ws.blank_matches(rid, "FID")
    import copy
    q = copy.deepcopy(ws.quant)
    q["blank_sub"] = BlankOptions(mode_fid="full", scale=1.2).to_dict()
    ws.quant = q
    ws._quant_settings_changed({}, q)
    ws.recompute_quant()
    after = {i: (r.get("corr_area"), r.get("conc")) for i, r in ws.quant_result.rows.get(rid, {}).items()}
    assert before and before == after
    ws.set_signal_key("FID")


def test_subtract_protects_windows():
    rt = np.linspace(0, 10, 1001)
    peak = lambda c, h: h * np.exp(-0.5 * ((rt - c) / 0.03) ** 2)
    sample = Signal("FID", rt, 10 + peak(3, 100) + peak(6, 500))
    blank = Signal("FID", rt, 10 + peak(3, 100) + peak(6, 500))      # the ISTD at 6 is in the blank too
    for mode in ("peaks", "full"):
        out, _ = subtract(sample, [("b", blank, None)], BlankOptions(), mode, "FID - Blank",
                          protect=[(5.8, 6.2)])
        m = (rt >= 5.8) & (rt <= 6.2)
        assert np.allclose(out.y[m], sample.y[m])                     # kept whole
        assert out.y[np.argmin(abs(rt - 3))] < 20                     # the blank peak is gone


@pytest.mark.parametrize("source", ["blank_istd", "both"])
def test_istds_survive_blank_istd_subtraction(ws_blank, source):
    import copy
    ws, ids = ws_blank
    st = ws.runs[ids["07_"]]
    st.blanks, st.blanks_istd = [ids["08_"]], [ids["06_"]]
    old = copy.deepcopy(ws.quant)
    q = copy.deepcopy(ws.quant)
    q["blank_sub"] = BlankOptions(source=source).to_dict()
    ws.quant = q
    ws._quant_settings_changed(old, q)
    try:
        assert ids["06_"] in ws.blank_ids(st)
        istd = ws.istd_peaks(st.id, "FID")
        raw = ws.result(st.id, "FID")
        assert {round(raw.peaks[i].apex_rt, 1) for i in istd} >= {13.4, 18.9}
        der = ws.result(st.id, "FID - Blank")
        for t in (13.417, 18.917):
            pr = min(raw.peaks, key=lambda p: abs(p.apex_rt - t))
            pd = min(der.peaks, key=lambda p: abs(p.apex_rt - t))
            assert abs(pd.apex_rt - pr.apex_rt) < 0.005 and pd.area >= 0.95 * pr.area   # not subtracted
        bm = ws.blank_matches(st.id, "FID")
        assert not set(bm) & set(istd)
        # the MS trace: the ISTD windows sit at the FID time minus the delay
        tic = ws.istd_peaks(st.id, "TIC")
        rtic = ws.result(st.id, "TIC")
        assert any(abs(rtic.peaks[i].apex_rt + st.delay_value - 13.42) < 0.05 for i in tic)
    finally:
        st.blanks_istd = []
        ws.quant = old
        ws._quant_settings_changed(q, old)


def test_manual_blank_subtraction(ws_blank):
    """Automatic off: settings changes keep the subtracted trace until "Subtract" is clicked."""
    import copy
    ws, ids = ws_blank
    rid = ids["07_"]
    old = copy.deepcopy(ws.quant)
    manual = copy.deepcopy(ws.quant)
    manual["blank_sub"] = BlankOptions(auto=False).to_dict()
    ws.quant = manual
    ws._quant_settings_changed(old, manual)
    try:
        der = ws.result(rid, "FID - Blank")
        q = copy.deepcopy(manual)
        q["blank_sub"] = BlankOptions(auto=False, scale=1.5).to_dict()
        ws.quant = q
        ws._quant_settings_changed(manual, q)
        assert ws.result(rid, "FID - Blank") is der                  # not rebuilt automatically
        ws.integrate(rid, "FID")
        assert ws.result(rid, "FID - Blank") is der                  # nor on a re-integration
        ws.subtract_blank()
        assert ws.result(rid, "FID - Blank") is not der              # "Subtract" rebuilds it
    finally:
        ws.quant = old
        ws._quant_settings_changed(q, old)


def test_blank_dialog_auto_and_subtract_button():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gcws.ui.dialogs.blank import BlankOptionsDialog
    dlg = BlankOptionsDialog(BlankOptions())
    assert dlg.auto.isChecked() and not dlg.subtract.isEnabled()
    dlg.auto.setChecked(False)
    assert dlg.subtract.isEnabled() and dlg.options().auto is False
    dlg.subtract.click()
    assert dlg.subtract_now and dlg.result() == BlankOptionsDialog.Accepted
