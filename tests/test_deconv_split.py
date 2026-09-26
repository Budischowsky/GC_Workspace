"""Pure validation and area conservation for replayable deconvolution splits."""
import json
import math
from types import SimpleNamespace

import numpy as np
import pytest

from gcws.core.events import ManualEvent, ManualKind as K
from gcws.integration.deconv_split import PREFIX, create_event, decode, share_exactly


def components():
    return [SimpleNamespace(rt=10.02, model_mz=57, purity=0.8, area=30.0,
                            spectrum=[(57, 999), (71, 500)]),
            SimpleNamespace(rt=10.08, model_mz=91, purity=0.7, area=70.0,
                            spectrum=[(91, 999), (105, 400)])]


def event():
    peak = SimpleNamespace(start=10.10, end=10.20, apex_rt=10.14)
    return create_event(peak, reversed(components()), delay=0.1)


def test_payload_maps_ms_time_and_preserves_component_spectra():
    original = event()
    restored = ManualEvent.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored == original
    payload = decode(restored)
    assert payload["points"] == pytest.approx([10.15])
    assert [item["rt"] for item in payload["components"]] == [10.02, 10.08]
    assert payload["components"][1]["spectrum"] == [[91, 999], [105, 400]]
    assert [item["area"] for item in payload["components"]] == [30, 70]
    assert decode(ManualEvent(K.SPLIT, 10.15)) is None
    assert decode(original.with_(kind=K.DELETE)) is None


@pytest.mark.parametrize("change", [
    {"points": []}, {"points": [10.25]}, {"version": 99}, {"delay": float("nan")},
    {"components": []}, {"components": [{"rt": 10.02}]},
])
def test_decode_rejects_invalid_payload_without_falling_back_to_drop_line(change):
    original = event()
    payload = decode(original)
    payload.update(change)
    with pytest.raises(ValueError):
        decode(original.with_(option=PREFIX + json.dumps(payload)))
    with pytest.raises(ValueError):
        decode(original.with_(option=PREFIX + "{broken"))


@pytest.mark.parametrize("field,value", [
    ("area", 0), ("area", -1), ("area", float("inf")),
    ("rt", 11), ("rt", 10.08), ("purity", -0.1), ("model_mz", 57.5),
])
def test_create_rejects_invalid_components(field, value):
    items = components()
    setattr(items[0], field, value)
    with pytest.raises(ValueError):
        create_event(SimpleNamespace(start=10.0, end=10.1, apex_rt=10.04), items)


@pytest.mark.parametrize("total", [10495765.00000001, 0.1, 1e-200, 1e200, -12345.67, 0.0])
@pytest.mark.parametrize("weights", [[30, 70], [1, 1, 1], [1e-6, 1, 10000, 1e-4], [1e308, 1e308]])
def test_area_allocation_conserves_total_under_all_sum_orders(total, weights):
    areas = share_exactly(total, weights)
    assert sum(areas) == total
    assert math.fsum(areas) == total
    assert np.sum(areas) == total
    assert sum(reversed(areas)) == total
    assert all(area >= 0 for area in areas) if total >= 0 else all(area <= 0 for area in areas)
    scaled = np.asarray(weights) / max(weights)
    np.testing.assert_allclose(areas, total * scaled / sum(scaled), rtol=1e-5,
                               atol=2 * math.ulp(abs(total)))


@pytest.mark.parametrize("weights", [[], [0, 1], [-1, 2], [float("nan"), 1], [float("inf"), 1]])
def test_area_allocation_refuses_invalid_weights(weights):
    with pytest.raises(ValueError):
        share_exactly(100, weights)

@pytest.mark.parametrize('factor', [1.0, 10.0, 13.3])
def test_three_component_split_replays_allocated_areas_and_preserves_baseline(factor):
    from test_integration import make, method, by_rt
    from gcws.integration.engine import integrate
    sig = make([(3, 0.02, 800), (6, 0.01, 400)])
    m = method(area_unit_factor=factor)
    before = integrate(sig, m)
    peak = by_rt(before, 3)
    items = [SimpleNamespace(rt=rt, model_mz=mz, purity=.8, area=area)
             for rt, mz, area in [(2.98, 57, 85), (3.0, 91, 10), (3.02, 105, 5)]]
    action = create_event(peak, items)
    result = integrate(sig, m, [action])
    fragments = sorted([p for p in result.peaks if p.origin == 'deconvoluted'], key=lambda p: p.start)
    assert not result.unresolved and len(fragments) == 3
    assert math.fsum(p.area for p in fragments) == peak.area
    assert math.fsum(p.area_raw for p in fragments) == peak.area_raw
    np.testing.assert_allclose([p.area / peak.area for p in fragments], [.85, .1, .05], rtol=1e-13)
    assert all(p.baseline == peak.baseline for p in fragments)
    restored = ManualEvent.from_dict(json.loads(json.dumps(action.to_dict())))
    assert integrate(sig, m, [restored]).digest == result.digest
    assert by_rt(result, 6).area == by_rt(before, 6).area
    # An explicit subsequent boundary edit must not keep stale modeled areas.
    left = fragments[0]
    edit = ManualEvent(K.MOVE_START, left.start+.003, ref_rt=left.apex_rt)
    edited = integrate(sig, m, [action, edit])
    assert not edited.unresolved
    assert sum(bool(p.extra.get('deconv_component')) for p in edited.peaks) == 2


def test_split_target_parent_does_not_split_its_skimmed_rider():
    from gcws.core.model import Baseline
    from gcws.integration.work import WP, WorkSignal
    from gcws.integration.manual import apply
    rt = np.linspace(0, 2, 201)
    y = np.full(rt.size, 100.)
    sig = WorkSignal(rt, y, y, np.zeros_like(y), np.zeros_like(y))
    parent = WP(.1, 1.9, .8, Baseline('hold', .1, 0., 1.9, 0.))
    rider = WP(.9, 1.1, 1., Baseline('hold', .9, 50., 1.1, 50.), parent=parent)
    parent.children.append(rider)
    peak = SimpleNamespace(start=.1, end=1.9, apex_rt=.8)
    items = [SimpleNamespace(rt=rt, model_mz=57, purity=.8, area=area)
             for rt, area in [(.8, 30), (1.2, 70)]]
    result, unresolved = apply([parent], sig, [create_event(peak, items)], width=.1)
    assert not unresolved and len(result) == 2
    assert math.fsum(p.allocated_area_raw for p in result) == pytest.approx(10200.)
    assert sum(len(p.children) for p in result) == 1
    assert rider.t0 == .9 and rider.t1 == 1.1


def test_unresolvable_allocation_is_atomic():
    from test_integration import make, method, by_rt
    from gcws.integration.engine import integrate
    sig = make([(3, .02, 800)])
    m = method()
    original = integrate(sig, m)
    peak = by_rt(original, 3)
    items = [SimpleNamespace(rt=rt, model_mz=57, purity=.8, area=1.) for rt in (2.98, 3.02)]
    action = create_event(peak, items)
    changed = action.with_(t0=peak.start-.001)
    result = integrate(sig, m, [changed])
    assert result.digest == original.digest and result.unresolved[0][0] == changed.uid
    assert not any(p.extra.get('deconv_component') for p in result.peaks)
