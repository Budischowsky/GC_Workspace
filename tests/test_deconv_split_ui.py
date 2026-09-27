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
    qtbot.waitUntil(lambda: not dlg._busy, timeout=30000)
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
    qtbot.waitUntil(lambda: not dlg._busy, timeout=30000)
    t0, t1, _ = ms_times(ws.selected_peak(), ws.active_key, ws.active.delay_value)
    next(c for c in dlg.comps if t0 <= c.rt <= t1).area = 0
    count = ws.active.undo.count()
    dlg.split()
    assert 'positive area' in dlg.note.text()
    assert ws.active.undo.count() == count
    assert ws.active_result().digest == before


def _open_example(qtbot, win, samples):
    from gcws.ui.dialogs.deconv import DeconvolutionDialog
    _load(qtbot, win, samples, ['07_', '08_'])
    ws = win.ws
    st = next(s for s in ws.states() if s.name.startswith('07_'))
    ws.set_active(st.id)
    st.delay_override = .0066
    ws.set_signal_key('FID')
    res = ws.active_result()
    ws.select_peak(min(range(len(res.peaks)), key=lambda i: abs(res.peaks[i].apex_rt - 11.87)))
    dlg = DeconvolutionDialog(win)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: not dlg._busy, timeout=30000)
    assert dlg.comps, dlg.note.text()
    return ws, st, dlg


def test_real_example_default_display_search_export_and_hidden_fragment(qtbot, win, samples, monkeypatch):
    from gcws.identify.service import build_items
    from gcws.ms.spectra import extract
    ws, st, dlg = _open_example(qtbot, win, samples)
    parent = ws.selected_peak()
    assert len(dlg._checked()) == 4
    assert dlg._selected()[0] is dlg._checked()[0]
    win.table.set_value_filter('area', '≥', 500000)
    messages = []
    ws.message.connect(messages.append)
    dlg.split()
    parts = [p for p in ws.active_result().peaks if p.extra.get('deconv_component')]
    assert len(parts) == 4 and sum(p.area for p in parts) == parent.area
    assert ws.selected_peak() is parts[0]
    assert win.spectrum.spec.top_ions() == [205, 220, 57]
    assert '1 fragment(s) hidden' in messages[-1]
    items, _ = build_items(ws, [st.id], 'FID', 'average_bg')
    jobs = {item.peak_id: item for item in items if item.peak_id}
    for peak in parts:
        spec = extract(st.run, peak, 'FID', st.delay_value)
        assert jobs[peak.extra['spectrum_id']].job.spectrum == spec.points(min_permille=1.)
        assert jobs[peak.extra['spectrum_id']].spectrum_mode == 'deconvoluted'
    assert '205' in win.spectrum.msp() and '220' in win.spectrum.msp()
    # Relative component abundances must never be mixed with absolute blank counts.
    monkeypatch.setattr(ws, 'blank_ids', lambda _st: ['blank'])
    monkeypatch.setattr(ws, 'blank_spectrum', lambda *_: pytest.fail('normalized spectrum blank subtraction'))
    win.spectrum.minus_blank.setChecked(True)
    assert win.spectrum.spec.top_ions() == [205, 220, 57]
    assert 'not applied' in win.spectrum.spec.note


def test_checked_components_and_sorting_control_split(qtbot, win, samples):
    from PySide6.QtCore import Qt
    ws, st, dlg = _open_example(qtbot, win, samples)
    parent = ws.selected_peak()
    original = dlg._checked()
    keep = {id(c) for c in original[:2]}
    dlg.table.sortByColumn(5, Qt.DescendingOrder)
    for row in range(dlg.table.rowCount()):
        item = dlg.table.item(row, 0)
        item.setCheckState(Qt.Checked if id(dlg.comps[item.data(Qt.UserRole)]) in keep else Qt.Unchecked)
    assert len(dlg._checked()) == 2
    assert '2 checked components' in dlg.preview.text()
    expected = np.array([c.area for c in original[:2]])
    expected /= expected.sum()
    dlg.split()
    parts = [p for p in ws.active_result().peaks if p.extra.get('deconv_component')]
    assert len(parts) == 2 and sum(p.area for p in parts) == parent.area
    np.testing.assert_allclose([p.area / parent.area for p in parts], expected)
    assert [p.extra['deconv_component']['model_mz'] for p in parts] == [205, 191]


@pytest.mark.parametrize('key', ['TIC', 'TIC - Blank'])
def test_tic_dialog_keeps_spectra_and_allocations(qtbot, win, samples, key):
    from gcws.ms.deconv import Component
    from gcws.ui.dialogs.deconv import DeconvolutionDialog
    _load(qtbot, win, samples, ['07_', '08_'])
    ws = win.ws
    ws.set_active(next(s.id for s in ws.states() if s.name.startswith('07_')))
    ws.set_signal_key(key)
    res = ws.active_result()
    candidates = [(i, p) for i, p in enumerate(res.peaks) if p.area > 0 and p.start > 6]
    i, parent = max(candidates, key=lambda pair: pair[1].end - pair[1].start)
    ws.select_peak(i)
    dlg = DeconvolutionDialog(win)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: not dlg._busy, timeout=30000)
    comps = [Component(parent.start + f * (parent.end-parent.start), 0, mz, [(mz, 999)],
                       area=area, purity=.8, n_ions=1, s_n=10)
             for f, mz, area in [(.25, 57, 30), (.75, 91, 70)]]
    dlg._show_components(comps, None, 'test mixture')
    dlg.split()
    parts = [p for p in ws.active_result().peaks if p.extra.get('deconv_component')]
    assert len(parts) == 2, dlg.note.text()
    assert sum(p.area for p in parts) == parent.area
    np.testing.assert_allclose([p.area / parent.area for p in parts], [.3, .7])
    assert win.spectrum.spec.top_ions() == [57]
    assert all('Modeled TIC area' in p.extra['area_note'] for p in parts)


@pytest.mark.parametrize('action', ['cancel', 'settings', 'scope', 'close', 'reintegrate', 'delay', 'repeat'])
def test_obsolete_background_results_cannot_publish(qtbot, win, samples, monkeypatch, action):
    from types import SimpleNamespace
    from gcws.ui import workers
    ws, st, dlg = _open_example(qtbot, win, samples)
    result = dlg.result
    callbacks = []
    def capture(fn, **kwargs):
        callbacks.append(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr(workers, 'submit', capture)
    dlg.run()
    assert dlg._busy and not dlg.b_split.isEnabled()
    if action == 'cancel':
        dlg.cancel_button.click()
    elif action == 'settings':
        dlg.noise.setValue(dlg.noise.value() + .5)
    elif action == 'scope':
        dlg.scope.button(2).click()
    elif action == 'close':
        dlg.reject()
    elif action == 'reintegrate':
        ws.integrate(st.id, 'FID')
    elif action == 'delay':
        st.delay_override += .01
    else:
        dlg.run()
    callbacks[0]['on_done'](result)
    callbacks[0]['on_progress']('stale progress')
    callbacks[0]['on_error']('stale failure')
    assert not dlg.comps
    assert not dlg.b_split.isEnabled()
    assert 'stale' not in dlg.note.text()
    if action == 'repeat':
        callbacks[1]['on_done'](result)
        assert dlg.comps and not dlg._busy and dlg.b_split.isEnabled()


def test_interpretation_cache_uses_ions_not_only_sum(qtbot, win, samples, monkeypatch):
    from gcws.ms.spectra import Spectrum
    from gcws.ms import interpret as module
    ws, st, dlg = _open_example(qtbot, win, samples)
    calls = []
    def interpretation(mz, ab, context):
        calls.append(tuple(ab))
        return object()
    monkeypatch.setattr(module, 'interpret', interpretation)
    sp = win.spectrum
    sp.spec = Spectrum(np.array([57, 91]), np.array([999., 100.]), 11.87, 'deconvoluted', [1])
    one = sp._interpret()
    sp.spec = Spectrum(np.array([57, 91]), np.array([100., 999.]), 11.87, 'deconvoluted', [1])
    two = sp._interpret()
    assert one is not two and len(calls) == 2
    assert sp._interpret() is two


def test_fragment_override_and_identification_survive_reload_and_undo(qtbot, win, samples, tmp_path):
    from gcws.core import project as P
    from gcws.core.ident import Identification
    from gcws.ms.assignment import fragment_id, override_key, override_for
    from gcws.ui.undo import IdentCommand
    ws, st, dlg = _open_example(qtbot, win, samples)
    parent = ws.selected_peak()
    parent_ident = Identification(parent.apex_rt, name='Original parent', manual=True)
    st.ident_set('FID').set(parent_ident)
    parent_override = {'apex_scans': [100], 'bg_scans': []}
    st.spectrum_overrides[override_key('FID', parent)] = parent_override
    dlg.split()
    first = ws.selected_peak()
    assert st.ident_set('FID').for_peak(first) is None
    assert override_for(st, 'FID', first) is None
    assert parent_ident in st.ident_set('FID').bind(ws.active_result().peaks)[1]
    assert 'Original parent' in win.table.info.toolTip()
    ident = Identification(first.apex_rt, name='Component 205', manual=True)
    st.undo.push(IdentCommand(ws, st.id, 'FID', [(first.apex_rt, ident)], 'name component'))
    assert st.ident_set('FID').for_peak(first).peak_id == fragment_id(first)
    st.spectrum_overrides[override_key('FID', first)] = {'component': dict(first.extra['deconv_component'])}
    path = P.save(ws, tmp_path / 'assigned.gcws')
    identity = fragment_id(first)
    st.undo.undo()  # identification
    st.undo.undo()  # split
    restored_parent = min(ws.active_result().peaks, key=lambda p: abs(p.apex_rt-parent.apex_rt))
    assert st.ident_set('FID').for_peak(restored_parent).name == 'Original parent'
    assert override_for(st, 'FID', restored_parent) == parent_override
    st.undo.redo()
    st.undo.redo()
    child = next(p for p in ws.active_result().peaks if fragment_id(p) == identity)
    assert st.ident_set('FID').for_peak(child).name == 'Component 205'
    win.close_all()
    win.open_project(path)
    qtbot.waitUntil(lambda: win.loading == 0 and len(ws.runs) == 2 and win._pending_project is None, timeout=60000)
    st = ws.active
    child = next(p for p in ws.active_result().peaks if fragment_id(p) == identity)
    assert st.ident_set('FID').for_peak(child).name == 'Component 205'
    assert override_for(st, 'FID', child)['component']['model_mz'] == 205
    assert override_for(st, 'TIC', child) is None


def test_peak_calculation_does_not_block_the_gui(qtbot, win, samples, monkeypatch):
    import threading
    from gcws.ms import deconv as D
    ws, st, dlg = _open_example(qtbot, win, samples)
    result = dlg.result
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    thread_ids = []
    def slow(*_args):
        thread_ids.append(threading.get_ident())
        entered.set()
        release.wait(5)
        completed.set()
        return result
    monkeypatch.setattr(D, 'deconvolute_window', slow)
    try:
        dlg.run()
        qtbot.waitUntil(entered.is_set, timeout=1000)
        assert not completed.is_set() and dlg._busy
        assert thread_ids == [thread_ids[0]] and thread_ids[0] != main_thread
        dlg.cancel_button.click()
        assert not dlg._busy and not dlg.comps
    finally:
        release.set()
    qtbot.waitUntil(completed.is_set, timeout=2000)
