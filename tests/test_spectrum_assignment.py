"""Regression coverage for split areas, spectrum ownership and raw scan inspection."""
import json
import math
from types import SimpleNamespace

import numpy as np
import pytest

from gcws.core.events import ManualEvent, ManualKind as K
from gcws.core.ident import Identification, IdentificationSet
from gcws.integration.deconv_split import PREFIX, create_event, decode
from gcws.integration.engine import integrate
from gcws.integration.method import nias_fid_method
from gcws.ms import deconv as D
from gcws.ms.assignment import fragment_id, override_for, override_key, restore_overrides
from gcws.ms.spectra import extract


@pytest.fixture
def real_split(run07):
    method = nias_fid_method()
    before = integrate(run07.signal('FID'), method, t_min=5.5)
    parent = min(before.peaks, key=lambda p: abs(p.apex_rt - 11.87))
    delay = .0066
    comps = D.deconvolute_window(run07.ms, parent.apex_rt - delay).components
    comps = [c for c in comps if parent.start <= c.rt + delay <= parent.end]
    event = create_event(parent, comps, delay).with_(uid='regression-11870')
    result = integrate(run07.signal('FID'), method, [event], t_min=5.5)
    assert not result.unresolved
    fragments = [p for p in result.peaks if p.extra.get('deconv_component')]
    return parent, comps, event, fragments


def test_real_11870_default_spectrum_and_exact_total(run07, real_split):
    parent, comps, event, fragments = real_split
    assert len(fragments) == len(comps) == 4
    assert parent.area == pytest.approx(50031327.695468076, rel=1e-12)
    assert math.fsum(p.area for p in fragments) == parent.area
    assert math.fsum(p.area_raw for p in fragments) == parent.area_raw
    assert fragments[-1].area < 500000
    for i, (peak, comp) in enumerate(zip(fragments, comps)):
        assert fragment_id(peak) == f'{event.uid}:{i}'
        spec = extract(run07, peak, 'FID', .0066)
        assert spec.mode == 'deconvoluted'
        assert spec.rt == comp.rt
        np.testing.assert_array_equal(spec.ab, [a for _, a in comp.spectrum])
    assert extract(run07, fragments[0], 'FID', .0066).top_ions() == [205, 220, 57]
    raw = extract(run07, fragments[0], 'FID', .0066, 'raw_average_bg')
    assert raw.mode == 'average_bg'
    assert abs(raw.rt - (fragments[0].apex_rt - .0066)) < .008
    assert 'nearest scan' in raw.note


def test_version_one_replay_retains_area_spectrum_and_identity(run07, real_split):
    _, _, event, fragments = real_split
    payload = decode(event)
    payload['version'] = 1
    payload.pop('signal_key')
    old = event.with_(option=PREFIX + json.dumps(payload))
    restored = ManualEvent.from_dict(json.loads(json.dumps(old.to_dict())))
    result = integrate(run07.signal('FID'), nias_fid_method(), [restored], t_min=5.5)
    replay = [p for p in result.peaks if p.extra.get('deconv_component')]
    assert [p.area for p in replay] == [p.area for p in fragments]
    assert [fragment_id(p) for p in replay] == [fragment_id(p) for p in fragments]
    assert extract(run07, replay[0], 'FID', .0066).top_ions() == [205, 220, 57]


def test_parent_and_signal_overrides_cannot_leak_to_fragments(run07, real_split):
    parent, _, _, fragments = real_split
    child = fragments[0]
    legacy = {'component': {'rt': 11., 'model_mz': 44, 'spectrum': [[44, 999]]}}
    st = SimpleNamespace(spectrum_overrides={round(parent.apex_rt, 4): legacy,
                                            round(child.apex_rt, 4): legacy})
    assert override_for(st, 'FID', child) is None
    assert override_for(st, 'FID', parent) == legacy
    st.spectrum_overrides[override_key('FID', child)] = legacy
    assert override_for(st, 'FID', child) == legacy
    assert override_for(st, 'TIC', child) is None
    assert override_for(st, 'FID - Blank', child) is None
    values = json.loads(json.dumps(st.spectrum_overrides))
    assert restore_overrides(values) == st.spectrum_overrides
    raw = extract(run07, child, 'FID', .0066, 'raw_apex', override=legacy)
    assert raw.mode == 'apex' and len(raw.apex_scans) == 1


def test_identifications_are_fragment_specific_and_parent_is_retained(real_split):
    parent, _, _, fragments = real_split
    old = Identification(parent.apex_rt, name='parent')
    ids = IdentificationSet([old])
    assert all(ids.for_peak(p) is None for p in fragments)
    assert ids.bind(fragments)[1] == [old]
    for i, peak in enumerate(fragments):
        ids.set(Identification(peak.apex_rt, name=str(i), peak_id=fragment_id(peak)))
    assert [ids.for_peak(p).name for p in fragments] == ['0', '1', '2', '3']
    # The old parent survives and becomes available again when the split is undone.
    assert ids.for_peak(parent) is old
    ids.remove_at(fragments[0].apex_rt, peak_id=fragment_id(fragments[0]))
    assert ids.for_peak(fragments[0]) is None
    assert ids.for_peak(parent) is old


def test_geometry_edit_invalidates_only_edited_fragment_assignment(run07, real_split):
    _, _, event, fragments = real_split
    first = fragments[0]
    edit = ManualEvent(K.MOVE_START, first.start + .001, ref_rt=first.apex_rt)
    result = integrate(run07.signal('FID'), nias_fid_method(), [event, edit], t_min=5.5)
    assert not result.unresolved
    edited = next(p for p in result.peaks if fragment_id(p).startswith('edited:'))
    assert 'deconv_component' not in edited.extra
    st = SimpleNamespace(spectrum_overrides={override_key('FID', first): {'apex_scans': [1]}})
    assert override_for(st, 'FID', edited) is None
    assert len([p for p in result.peaks if p.extra.get('deconv_component')]) == 3


def test_deconvolution_never_uses_an_outside_component(run07, real_split):
    parent, comps, _, _ = real_split
    outside = D.Component(parent.end + 1, 0, 44, [(44, 999)], area=1, purity=1, n_ions=1, s_n=1)
    assert D.component_for_peak([outside], parent.start, parent.end, parent.apex_rt) is None
    fallback = extract(run07, parent, 'FID', .0066, 'deconvoluted', component=lambda: None)
    assert fallback.mode == 'average_bg' and 'no deconvoluted component' in fallback.note


def test_nonpositive_raw_peak_top_uses_nearest_apex_and_empty_scans_are_safe():
    from gcws.io.ms_matrix import MSMatrix
    rt = np.array([1., 1.01, 1.02, 1.04, 1.05])
    ms = MSMatrix._build(rt, np.ones(5), [np.array([44.])] * 5, [np.array([10.])] * 5)
    run = SimpleNamespace(ms=ms, signal=lambda _: SimpleNamespace(y=np.ones(5)))
    peak = SimpleNamespace(start=1., end=1.05, apex_rt=1.041, extra={})
    spec = extract(run, peak, 'TIC', 0, 'raw_average_bg')
    assert spec.apex_scans == [3] and spec.rt == 1.04
    manual = extract(run, peak, 'TIC', 0, override={'apex_scans': [999]})
    assert not manual.ab.size and 'no valid' in manual.note
    run.ms = SimpleNamespace(n_scans=0)
    assert extract(run, peak, 'TIC', 0) is None


@pytest.mark.parametrize('key', ['TIC', 'TIC - Blank'])
def test_tic_atomic_split_preserves_total_and_association(key):
    from test_integration import make, method, by_rt
    sig = make([(3, .08, 800)])
    m = method(area_unit_factor=1)
    parent = by_rt(integrate(sig, m), 3)
    comps = [SimpleNamespace(rt=rt, area=area, model_mz=mz, purity=.8, spectrum=[(mz, 999)])
             for rt, area, mz in [(2.96, 30, 57), (3.04, 70, 91)]]
    event = create_event(parent, comps, signal_key=key)
    result = integrate(sig, m, [event])
    assert not result.unresolved and len(result.peaks) == 2
    assert sum(p.area for p in result.peaks) == parent.area
    assert [p.extra['deconv_component']['model_mz'] for p in result.peaks] == [57, 91]
    np.testing.assert_allclose([p.area / parent.area for p in result.peaks], [.3, .7])
    with pytest.raises(ValueError, match='FID and TIC'):
        create_event(parent, comps, signal_key='EIC 57')


def test_name_transfer_matches_component_times_and_does_not_transfer_mixtures():
    from gcws.identify.service import transfer_names
    def peak(apex, rt, identity):
        return SimpleNamespace(apex_rt=apex, width50=.03,
                               extra={'spectrum_id': identity, 'deconv_component': {'rt': rt}})
    fid = [peak(10.026, 10., 'fid:0'), peak(10.027, 10.02, 'fid:1')]
    tic = [peak(10.019, 10., 'tic:0'), peak(10.02, 10.02, 'tic:1')]
    ids = IdentificationSet()
    result = SimpleNamespace(peaks=fid)
    st = SimpleNamespace(delay_value=.006, results={'TIC': SimpleNamespace(peaks=tic)}, ident_set=lambda _: ids)
    ws = SimpleNamespace(runs={'run': st}, result=lambda *_: result)
    changes = [(p.apex_rt, Identification(p.apex_rt, name=str(i), peak_id=fragment_id(p)))
               for i, p in enumerate(tic)]
    transferred, counts = transfer_names(ws, 'run', changes)
    assert counts['copied'] == 2
    assert [(i.peak_id, i.name) for _, i in transferred] == [('fid:0', '0'), ('fid:1', '1')]
    mixed, counts = transfer_names(ws, 'run', [(10.01, Identification(10.01, name='mixed'))])
    assert not mixed and counts['unmatched'] == 1
