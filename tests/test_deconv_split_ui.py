"""Deconvolution area allocation through the dialog, undo and project replay."""
import math

import numpy as np
import pytest

from test_ui import win, _load


@pytest.mark.parametrize('key', ['FID', 'FID - Blank'])
def test_deconv_fid_areas_spectra_undo_and_project(qtbot, win, samples, tmp_path, key):
    from PySide6.QtCore import Qt
    from gcws.core import project as P
    from gcws.ms import deconv_cache as DC
    from gcws.ms.spectra import ms_times, extract, deconvoluted_component
    from gcws.ui.dialogs.deconv import DeconvolutionDialog
    from gcws.ui.models.peak_table import COLUMN_KEYS
    _load(qtbot, win, samples, ['07_', '08_'])
    ws = win.ws
    st = next(s for s in ws.states() if s.name.startswith('07_'))
    ws.set_active(st.id)
    st.delay_override = 0.006
    ws.set_signal_key(key)
    before = ws.active_result()
    ws.select_peak(min(range(len(before.peaks)), key=lambda i: abs(before.peaks[i].apex_rt - 13.41)))
    peak = ws.selected_peak()
    dlg = DeconvolutionDialog(win)
    qtbot.addWidget(dlg)
    t0, t1, _ = ms_times(peak, key, st.delay_value)
    comps = sorted([c for c in dlg.comps if t0 <= c.rt <= t1], key=lambda c: c.rt)
    assert len(comps) >= 2
    expected = np.array([c.area for c in comps])
    expected /= expected.sum()
    dlg.split()
    result = ws.active_result()
    fragments = sorted([p for p in result.peaks if p.extra.get('deconv_component')], key=lambda p: p.start)
    assert len(fragments) == len(comps)
    assert not result.unresolved
    assert len(result.peaks) == len(before.peaks) + len(comps) - 1
    assert math.fsum(p.area for p in fragments) == peak.area
    assert math.fsum(p.area_raw for p in fragments) == peak.area_raw
    np.testing.assert_allclose([p.area / peak.area for p in fragments], expected, rtol=1e-13)
    assert math.fsum(p.area for p in result.peaks) == pytest.approx(math.fsum(p.area for p in before.peaks), rel=1e-15)
    for p, comp in zip(fragments, comps):
        assert p.origin == 'deconvoluted'
        assert p.baseline == peak.baseline
        assert p.extra['deconv_component']['model_mz'] == comp.model_mz
        assert 'Modeled FID area' in p.extra['area_note']
        cached = DC.for_peak(st, p, key, DC.settings_of(ws))
        uncached = deconvoluted_component(st.run, p, key, st.delay_value)
        assert cached.spectrum == uncached.spectrum == p.extra['deconv_component']['spectrum']
        spec = extract(st.run, p, key, st.delay_value, 'deconvoluted')
        np.testing.assert_array_equal(spec.ab, [a for _, a in comp.spectrum])
    row = next(i for i, r in enumerate(win.table.model.rows) if r.peak.extra.get('deconv_component'))
    assert 'Modeled FID area' in win.table.model.index(row, COLUMN_KEYS.index('area')).data(Qt.ToolTipRole)
    digest = result.digest
    ws.integrate(st.id, key)
    assert ws.active_result().digest == digest
    st.undo.undo()
    assert ws.active_result().digest == before.digest
    st.undo.redo()
    assert ws.active_result().digest == digest
    if key == 'FID':
        ws.recompute_quant()
        qsample = ws.quant_result.samples[st.id]
        modeled = [r for r in qsample.rows if r.derived.get('deconv_component')]
        assert len(modeled) == len(comps)
        assert all('Modeled FID area' in r.derived['review'] for r in modeled)
    path = P.save(ws, tmp_path / 'deconvolution.gcws')
    win.close_all()
    win.open_project(path)
    qtbot.waitUntil(lambda: win.loading == 0 and len(ws.runs) == 2 and win._pending_project is None, timeout=60000)
    assert ws.active_result().digest == digest
    assert len([p for p in ws.active_result().peaks if p.extra.get('deconv_component')]) == len(comps)


def test_deconv_split_rejects_zero_area_without_undo_entry(qtbot, win, samples):
    from gcws.ui.dialogs.deconv import DeconvolutionDialog
    from gcws.ms.spectra import ms_times
    _load(qtbot, win, samples, ['07_'])
    ws = win.ws
    ws.select_peak(min(range(len(ws.active_result().peaks)), key=lambda i: abs(ws.active_result().peaks[i].apex_rt - 13.41)))
    before = ws.active_result().digest
    dlg = DeconvolutionDialog(win)
    qtbot.addWidget(dlg)
    t0, t1, _ = ms_times(ws.selected_peak(), ws.active_key, ws.active.delay_value)
    next(c for c in dlg.comps if t0 <= c.rt <= t1).area = 0
    count = ws.active.undo.count()
    dlg.split()
    assert 'positive area' in dlg.note.text()
    assert ws.active.undo.count() == count
    assert ws.active_result().digest == before
