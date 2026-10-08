"""The deconvolution window: trace-fit splits of FID and TIC peaks, undo and project replay."""
import math

import numpy as np
import pytest

from test_ui import win, _load


def _open(qtbot, win, samples, key, rt, delay=.0066, prefixes=('07_', '08_')):
    from gcws.ui.dialogs.deconv import DeconvolutionDialog
    _load(qtbot, win, samples, list(prefixes))
    ws = win.ws
    st = next(s for s in ws.states() if s.name.startswith('07_'))
    ws.set_active(st.id)
    st.delay_override = delay
    ws.set_signal_key(key)
    res = ws.active_result()
    shift = delay if key.startswith('FID') else 0.
    ws.select_peak(min(range(len(res.peaks)), key=lambda i: abs(res.peaks[i].apex_rt - shift - rt)))
    dlg = DeconvolutionDialog(win)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: not dlg._busy, timeout=30000)
    assert dlg.plan is not None, dlg.note.text()
    return ws, st, dlg


@pytest.mark.parametrize('key', ['FID', 'FID - Blank'])
def test_fid_split_by_trace_fit_spectra_undo_and_project(qtbot, win, samples, tmp_path, key):
    from PySide6.QtCore import Qt
    from gcws.core import project as P
    from gcws.ms import deconv_cache as DC
    from gcws.ms.spectra import extract, deconvoluted_component
    from gcws.ui.models.peak_table import COLUMN_KEYS
    ws, st, dlg = _open(qtbot, win, samples, key, 13.41, delay=.006)
    before = ws.active_result()
    peak = ws.selected_peak()
    plan = dlg.plan
    assert plan.ok and plan.basis == 'fit' and plan.fit.r2 > .98, dlg.note.text()
    comps = dlg._checked()
    assert len(comps) >= 2 and dlg.b_split.isEnabled()
    assert dlg.b_split.text() == f'Split into {len(comps)} peaks'
    assert f'Fit to FID: R²' in dlg.note.text()
    dlg.split()
    result = ws.active_result()
    fragments = sorted([p for p in result.peaks if p.extra.get('deconv_component')], key=lambda p: p.start)
    assert len(fragments) == len(comps)
    assert not result.unresolved
    assert len(result.peaks) == len(before.peaks) + len(comps) - 1
    assert math.fsum(p.area for p in fragments) == peak.area
    assert math.fsum(p.area_raw for p in fragments) == peak.area_raw
    assert [p.area for p in fragments] == plan.areas
    assert math.fsum(p.area for p in result.peaks) == pytest.approx(math.fsum(p.area for p in before.peaks), rel=1e-15)
    for p, comp in zip(fragments, comps):
        assert p.origin == 'deconvoluted'
        assert p.baseline == peak.baseline
        assert p.extra['deconv_component']['model_mz'] == comp.model_mz
        assert p.extra['deconv_component']['basis'] == 'fit'
        assert 'Modeled FID area' in p.extra['area_note'] and 'fitted to the FID signal' in p.extra['area_note']
        cached = DC.for_peak(st, p, key, DC.settings_of(ws))
        uncached = deconvoluted_component(st.run, p, key, st.delay_value)
        assert cached.spectrum == uncached.spectrum == p.extra['deconv_component']['spectrum']
        spec = extract(st.run, p, key, st.delay_value, 'deconvoluted')
        np.testing.assert_array_equal(spec.ab, [a for _, a in comp.spectrum])
    row = next(i for i, r in enumerate(win.table.model.rows) if r.peak.extra.get('deconv_component'))
    assert 'fitted to the FID signal' in win.table.model.index(row, COLUMN_KEYS.index('area')).data(Qt.ToolTipRole)
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


def test_split_rejects_zero_component_area_without_undo_entry(qtbot, win, samples):
    ws, st, dlg = _open(qtbot, win, samples, 'FID', 11.865, prefixes=('07_',))
    before = ws.active_result().digest
    dlg._checked()[0].area = 0
    count = st.undo.count()
    dlg.split()
    assert 'positive area' in dlg.note.text()
    assert st.undo.count() == count
    assert ws.active_result().digest == before


def test_real_example_defaults_search_export_and_hidden_fragment(qtbot, win, samples, monkeypatch):
    from gcws.identify.service import build_items
    from gcws.ms.spectra import extract
    ws, st, dlg = _open(qtbot, win, samples, 'FID', 11.865)
    parent = ws.selected_peak()
    assert [c.model_mz for c in dlg._checked()] == [205, 191]            # bleed and noise unchecked
    assert [row.reason[:3] for row in dlg.model.rows] == ['', '', 'S/N', 'S/N']
    assert dlg._selected()[0] is dlg._checked()[0]
    dlg.set_checked([c.component for c in dlg.plan.candidates[:3]])      # the analyst adds m/z 563
    assert len(dlg.plan.checked) == 3 and dlg.plan.ok
    win.table.set_value_filter('area', '≥', 1000000)
    messages = []
    ws.message.connect(messages.append)
    dlg.split()
    parts = [p for p in ws.active_result().peaks if p.extra.get('deconv_component')]
    assert len(parts) == 3 and math.fsum(p.area for p in parts) == parent.area
    assert ws.selected_peak() is parts[0]
    assert win.spectrum.spec.top_ions() == [205, 220, 57]
    assert '1 fragment(s) hidden' in messages[-1] and 'fitted to the FID signal' in messages[-1]
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


def test_checkboxes_refit_and_control_the_split(qtbot, win, samples):
    from PySide6.QtCore import Qt
    ws, st, dlg = _open(qtbot, win, samples, 'FID', 11.865, prefixes=('07_',))
    parent = ws.selected_peak()
    shares = list(dlg.plan.shares)
    first, second = (c.component for c in dlg.plan.candidates[:2])
    dlg.table.sortByColumn(4, Qt.DescendingOrder)                         # sorting keeps the rows' components
    dlg.set_checked([first])
    assert not dlg.plan.ok and 'nothing to split' in dlg.plan.problem
    assert not dlg.b_split.isEnabled() and 'nothing to split' in dlg.b_split.toolTip()
    source = dlg.model.index(next(r for r, row in enumerate(dlg.model.rows) if row.comp is second), 0)
    assert dlg.model.setData(source, Qt.Checked, Qt.CheckStateRole)
    assert dlg.plan.ok and dlg.plan.shares == pytest.approx(shares)
    assert dlg.b_split.isEnabled() and dlg.b_split.text() == 'Split into 2 peaks'
    shown = dlg.model.data(source.siblingAtColumn(6))
    assert shown == f'{100 * dlg.plan.shares[1]:.1f}'
    dlg.split()
    parts = sorted([p for p in ws.active_result().peaks if p.extra.get('deconv_component')], key=lambda p: p.start)
    assert [p.extra['deconv_component']['model_mz'] for p in parts] == [205, 191]
    assert math.fsum(p.area for p in parts) == parent.area


@pytest.mark.parametrize('key', ['TIC', 'TIC - Blank'])
def test_tic_split_by_fit_and_by_ms_proportions(qtbot, win, samples, key):
    from gcws.ms import peak_split as PS
    from gcws.ms.deconv import Component
    ws, st, dlg = _open(qtbot, win, samples, key, 11.865)
    parent = ws.selected_peak()
    assert dlg.plan.basis == 'fit' and 'Fit to TIC' in dlg.note.text()
    assert [c.model_mz for c in dlg._checked()] == [205, 191]
    np.testing.assert_allclose(dlg.plan.shares, [.466, .534], atol=.01)
    # Components without an elution profile fall back to the MS proportions.
    comps = [Component(parent.start + f * (parent.end - parent.start), 0, mz, [(mz, 999)],
                       area=area, purity=.8, n_ions=1, s_n=100)
             for f, mz, area in [(.25, 57, 30), (.75, 91, 70)]]
    plan = PS.plan_split(st.run.signal(key), parent, key, st.delay_value, comps)
    dlg._show_components(comps, None, plan, 'test mixture')
    assert dlg.plan.basis == 'ms' and 'MS component proportions' in dlg.note.text()
    dlg.split()
    parts = [p for p in ws.active_result().peaks if p.extra.get('deconv_component')]
    assert len(parts) == 2, dlg.note.text()
    assert math.fsum(p.area for p in parts) == parent.area
    np.testing.assert_allclose([p.area / parent.area for p in parts], [.3, .7])
    assert win.spectrum.spec.top_ions() == [57]
    assert all('Modeled TIC area' in p.extra['area_note'] for p in parts)


@pytest.mark.parametrize('action', ['cancel', 'settings', 'scope', 'close', 'reintegrate', 'delay', 'repeat'])
def test_obsolete_background_results_cannot_publish(qtbot, win, samples, monkeypatch, action):
    from types import SimpleNamespace
    from gcws.ui import workers
    ws, st, dlg = _open(qtbot, win, samples, 'FID', 11.865)
    value = (dlg.result, dlg.plan)
    callbacks = []

    def capture(fn, **kwargs):
        callbacks.append(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr(workers, 'submit', capture)
    dlg.run()
    assert dlg._busy and not dlg.b_split.isEnabled() and not dlg.cancel_button.isHidden()
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
    callbacks[0]['on_done'](value)
    callbacks[0]['on_progress']('stale progress')
    callbacks[0]['on_error']('stale failure')
    assert not dlg.comps and dlg.plan is None
    assert not dlg.b_split.isEnabled()
    assert 'stale' not in dlg.note.text()
    if action == 'repeat':
        callbacks[1]['on_done'](value)
        assert dlg.comps and not dlg._busy and dlg.b_split.isEnabled()


def test_interpretation_cache_uses_ions_not_only_sum(qtbot, win, samples, monkeypatch):
    from gcws.ms.spectra import Spectrum
    from gcws.ms import interpret as module
    ws, st, dlg = _open(qtbot, win, samples, 'FID', 11.865, prefixes=('07_',))
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
    ws, st, dlg = _open(qtbot, win, samples, 'FID', 11.865)
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
    restored_parent = min(ws.active_result().peaks, key=lambda p: abs(p.apex_rt - parent.apex_rt))
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
    ws, st, dlg = _open(qtbot, win, samples, 'FID', 11.865, prefixes=('07_',))
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
    st.deconv.clear()                    # no cached window: the engine must run
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


def test_entry_points_pin_and_library_hits(qtbot, win, samples, monkeypatch):
    from PySide6.QtGui import QKeySequence
    from gcws.ms.assignment import override_for
    from gcws.ui.dialogs.deconv import DeconvolutionDialog
    _load(qtbot, win, samples, ['07_'])
    ws = win.ws
    res = ws.active_result()
    ws.select_peak(min(range(len(res.peaks)), key=lambda i: abs(res.peaks[i].apex_rt - 11.87)))
    assert win.a_deconv.shortcut() == QKeySequence('Ctrl+K')
    assert win.a_deconv in win.identify_menu.actions()
    assert win.splitDeconvAction in win.table.context_actions
    win.splitDeconvAction.trigger()
    first = win._deconv_dialog
    assert isinstance(first, DeconvolutionDialog) and first.current_scope() == 'peak'
    win.a_deconv.trigger()
    second = win._deconv_dialog
    assert second is not first and first._closed
    qtbot.waitUntil(lambda: not second._busy, timeout=30000)
    calls = []
    monkeypatch.setattr(win, 'atlas_hits', lambda points, name, method=None: calls.append((points, name)))
    second.table.selectRow(0)
    comp = second._selected()[0]
    second.search()
    assert calls and calls[0][0] == [(int(m), float(v)) for m, v in comp.spectrum]
    assert win.spectrum.source == 'component'
    second.pin()
    assert override_for(ws.active, ws.active_key, ws.selected_peak())['component']['model_mz'] == comp.model_mz
    second.close()


def test_split_peaks_are_drawn_as_their_curves_and_markers_follow_integration(qtbot, win, samples):
    from types import SimpleNamespace
    from gcws.core.events import ManualEvent, ManualKind as K
    from gcws.ms import deconv_cache as DC
    from gcws.ui.undo import ManualEventsCommand
    ws, st, dlg = _open(qtbot, win, samples, 'FID', 11.865, prefixes=('07_',))
    dlg.split()
    parts = {i: p for i, p in enumerate(ws.active_result().peaks) if p.extra.get('deconv_component')}
    item = win.chrom.peaks
    assert sorted(i for i, *_ in item.modeled) == sorted(parts) and len(item.modeled_paths) == 2
    for i, t, top, base in item.modeled:
        assert float(np.trapezoid(top - base, t * 60)) == pytest.approx(parts[i].area_raw, rel=2e-3)
        span = parts[i].extra['deconv_component']['parent_span']
        assert t[0] == span[0] and t[-1] == span[1]
    # Markers of components without a peak follow a re-integration of the table's signal.
    res = ws.active_result()
    gaps = [(a.end + b.start) / 2 for a, b in zip(res.peaks, res.peaks[1:]) if b.start - a.end > .05]
    comps = [SimpleNamespace(rt=t - st.delay_value, model_mz=57, purity=.9) for t in gaps[5:7]]
    DC.store_whole_run(st, DC.settings_of(ws), comps)
    ws.deconvChanged.emit(st.id)
    tic_panel = win.chroms[1]
    assert len(tic_panel._markers.points()) == 2
    t = gaps[5]
    events = list(st.events('FID')) + [ManualEvent(K.ADD_PEAK, t - .01, t + .01)]
    st.undo.push(ManualEventsCommand(ws, st.id, 'FID', events, 'add a peak at a hidden component'))
    assert len(tic_panel._markers.points()) == 1


@pytest.mark.parametrize('key', ['FID', 'FID - Blank'])
def test_fid_deconvolution_fragments_appear_on_tic_and_follow_undo(qtbot, win, samples, key):
    ws, st, dlg = _open(qtbot, win, samples, key, 11.865, prefixes=('07_',))
    tic_panel = win.chroms[1]
    assert tic_panel.key == 'TIC'
    dlg.split()
    fragments = [p for p in ws.active_result().peaks if p.extra.get('deconv_component')]
    assert len(fragments) == 2
    markers = tic_panel._deconv_markers
    assert markers is not None
    assert len(markers.points()) == len(fragments)
    for point, peak in zip(markers.points(), fragments):
        ms_rt = peak.extra['deconv_component']['rt']
        assert point.pos().x() == pytest.approx(ms_rt + st.delay_value)
        assert point.data().model_mz == peak.extra['deconv_component']['model_mz']
    markers.sigClicked.emit(markers, [markers.points()[0]], None)
    assert win.spectrum.source == 'component'
    st.undo.undo()
    assert tic_panel._deconv_markers is None
    st.undo.redo()
    assert len(tic_panel._deconv_markers.points()) == len(fragments)
    ws.set_table_panel(1)
    ws.integrate(st.id, key)
    assert len(tic_panel._deconv_markers.points()) == len(fragments)
    ws.set_panel(0, key='TIC')
    ws.set_panel(1, key=key)
    tic_panel = win.chroms[0]
    assert len(tic_panel._deconv_markers.points()) == len(fragments)
    for point, peak in zip(tic_panel._deconv_markers.points(), fragments):
        assert point.pos().x() == pytest.approx(peak.extra['deconv_component']['rt'])


@pytest.mark.parametrize('key', ['FID', 'TIC'])
def test_merge_deconvoluted_peaks_removes_the_split(qtbot, win, samples, key):
    """Merge deconvoluted peaks (table action or Merge peaks tool) removes the analyst's split event."""
    from PySide6.QtCore import Qt
    ws, st, dlg = _open(qtbot, win, samples, key, 13.41, delay=.006)
    before = ws.active_result().digest
    dlg.split()
    dlg.close()
    frags = [p for p in ws.active_result().peaks if p.extra.get('deconv_component')]
    assert len(frags) >= 2
    ws.select_peak(ws.active_result().peaks.index(frags[0]))
    win.merge_deconvoluted()
    assert ws.active_result().digest == before
    assert not any(p.extra.get('deconv_component') for p in ws.active_result().peaks)
    st.undo.undo()
    frags = [p for p in ws.active_result().peaks if p.extra.get('deconv_component')]
    assert len(frags) >= 2
    win.tools.set_tool('merge')
    try:
        win.tools.drag_finished(win.chrom.vb, frags[0].apex_rt - .001, 0, frags[-1].apex_rt + .001, 0,
                                Qt.NoModifier, (1.0, 0.0), key=ws.active_key)
    finally:
        win.tools.set_tool('select')
    assert ws.active_result().digest == before
