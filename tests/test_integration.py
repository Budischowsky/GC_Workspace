import sys
import time
from pathlib import Path

import numpy as np
import pytest

from gcws.core.events import ManualEvent, ManualKind as K
from gcws.core.model import Signal
from gcws.integration.engine import integrate
from gcws.integration.method import EventKind as E
from gcws.integration.method import IntegrationMethod, TimedEvent, nias_fid_method

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

RATE = 1200.0  # points per minute (20 Hz)


def gauss(t, mu, s, a):
    return a * np.exp(-0.5 * ((t - mu) / s) ** 2)


def g_area(s, a):
    return a * s * np.sqrt(2 * np.pi) * 60.0


def make(peaks, drift=0.0, noise=1.0, seed=1, t_end=10.0):
    rng = np.random.default_rng(seed)
    t = np.arange(0, t_end, 1 / RATE)
    y = 100 + drift * t + rng.normal(0, noise, t.size)
    for mu, s, a in peaks:
        y = y + gauss(t, mu, s, a)
    return Signal("FID", t, y)


def method(**kw):
    kw.setdefault("area_unit_factor", 1.0)
    return IntegrationMethod(**kw)


def by_rt(res, rt, tol=0.01):
    cands = [p for p in res.peaks if abs(p.apex_rt - rt) < tol]
    assert cands, f"no peak at {rt}"
    return cands[0]


def test_isolated_gaussians_area_and_rt():
    peaks = [(2, 0.01, 1000), (5, 0.02, 300), (8, 0.015, 50)]
    res = integrate(make(peaks, drift=3.0), method())
    assert len(res.peaks) == 3
    for mu, s, a in peaks:
        p = by_rt(res, mu)
        assert p.area == pytest.approx(g_area(s, a), rel=0.02)
        assert p.height == pytest.approx(a, rel=0.03)
        assert p.width50 == pytest.approx(2.3548 * s, rel=0.05)
        assert p.type_code.startswith("BB")
        assert p.symmetry == pytest.approx(1.0, abs=0.08)


def test_fused_pair_drop_line_conserves_total():
    peaks = [(4, 0.01, 500), (4.035, 0.01, 400)]
    res = integrate(make(peaks), method(skim_mode="none"))
    a, b = by_rt(res, 4.0), by_rt(res, 4.035)
    assert a.type_code.startswith("BV") and b.type_code.startswith("VB")
    total = g_area(0.01, 500) + g_area(0.01, 400)
    assert a.area + b.area == pytest.approx(total, rel=0.01)


def test_valley_to_valley_mode():
    peaks = [(4, 0.01, 500), (4.035, 0.01, 400)]
    drop = integrate(make(peaks), method(skim_mode="none"))
    valley = integrate(make(peaks), method(skim_mode="none", baseline_mode="valley"))
    assert sum(p.area for p in valley.peaks) < sum(p.area for p in drop.peaks)


def test_tail_rider_is_skimmed():
    peaks = [(6, 0.02, 2000), (6.08, 0.01, 50)]
    res = integrate(make(peaks), method(skim_mode="tangent"))
    child = by_rt(res, 6.08)
    assert "T" in child.flags and child.parent is not None
    parent = res.peaks[child.parent]
    assert parent.area + child.area == pytest.approx(g_area(0.02, 2000) + g_area(0.01, 50), rel=0.02)


def test_negative_peak():
    sig = make([(3, 0.01, 400)])
    sig.y = sig.y - gauss(sig.rt, 6, 0.01, 300)
    res = integrate(sig, method(negative_peaks=True))
    neg = [p for p in res.peaks if p.negative]
    assert len(neg) == 1 and neg[0].area == pytest.approx(g_area(0.01, 300), rel=0.02)


def test_integrator_off_and_area_reject():
    peaks = [(1, 0.01, 1000), (5, 0.01, 1000), (7, 0.01, 20)]
    m = method(area_reject=g_area(0.01, 100),
               timed_events=[TimedEvent(0.0, E.INTEGRATOR_OFF), TimedEvent(3.0, E.INTEGRATOR_ON)])
    res = integrate(make(peaks), m)
    rts = [round(p.apex_rt, 2) for p in res.peaks]
    assert rts == [5.0]


def test_timed_split_and_area_sum():
    sig = make([(4, 0.03, 500)])
    res = integrate(sig, method(timed_events=[TimedEvent(4.0, E.SPLIT_PEAK)]))
    assert len(res.peaks) == 2
    sig2 = make([(4, 0.01, 500), (4.2, 0.01, 500)])
    res2 = integrate(sig2, method(timed_events=[TimedEvent(3.9, E.AREA_SUM_ON), TimedEvent(4.3, E.AREA_SUM_OFF)]))
    assert len(res2.peaks) == 1 and "+" in res2.peaks[0].flags


def test_shoulder_drop():
    sig = make([(4, 0.02, 1000), (4.045, 0.015, 250)], noise=0.3)
    res = integrate(sig, method(shoulders="drop", skim_mode="none"))
    assert len(res.peaks) == 2
    assert any("R" in p.flags for p in res.peaks)


def test_penetration_resets_baseline():
    sig = make([(4, 0.01, 500), (4.2, 0.01, 500)], noise=0.3)
    dip = gauss(sig.rt, 4.1, 0.01, 60)
    sig.y = sig.y - dip
    res = integrate(sig, method(skim_mode="none", min_valley_depth=0))
    for p in res.peaks:
        t = np.linspace(p.start, p.end, 50)
        assert np.all(np.interp(t, sig.rt, sig.y) >= p.baseline.eval(t) - 5)


def test_manual_events_split_delete_add_move_merge():
    sig = make([(3, 0.02, 800), (6, 0.01, 400)])
    m = method()
    base = integrate(sig, m)
    total = sum(p.area for p in base.peaks)
    res = integrate(sig, m, [ManualEvent(K.SPLIT, 3.0)])
    assert len(res.peaks) == 3
    assert sum(p.area for p in res.peaks) == pytest.approx(total, rel=1e-9)
    assert all("M" in p.flags for p in res.peaks if abs(p.apex_rt - 3) < 0.1)
    res = integrate(sig, m, [ManualEvent(K.DELETE, 6.0)])
    assert len(res.peaks) == 1
    res = integrate(sig, m, [ManualEvent(K.ADD_PEAK, 8.0, 8.2)])
    assert len(res.peaks) == 3 and any(p.origin == "added" for p in res.peaks)
    p6 = by_rt(base, 6.0)
    res = integrate(sig, m, [ManualEvent(K.MOVE_END, p6.end - 0.01, ref_rt=6.0)])
    assert by_rt(res, 6.0).end == pytest.approx(p6.end - 0.01)
    res = integrate(sig, m, [ManualEvent(K.SPLIT, 3.0), ManualEvent(K.MERGE, 2.9, 3.1)])
    assert len(res.peaks) == 2
    res = integrate(sig, m, [ManualEvent(K.DRAW_BASELINE, 5.9, 6.1, y0=90, y1=90)])
    assert by_rt(res, 6.0).area > p6.area
    res = integrate(sig, m, [ManualEvent(K.DELETE, 9.5)])
    assert res.unresolved and len(res.peaks) == 2


def test_reset_range_cancels_earlier_events():
    sig = make([(3, 0.02, 800)])
    m = method()
    res = integrate(sig, m, [ManualEvent(K.SPLIT, 3.0), ManualEvent(K.RESET_RANGE, 2.5, 3.5)])
    assert len(res.peaks) == 1


def test_manual_reapplication_is_deterministic():
    sig = make([(3, 0.02, 800), (6, 0.01, 400)])
    ev = [ManualEvent(K.SPLIT, 3.01), ManualEvent(K.ADD_PEAK, 8.0, 8.2)]
    a = integrate(sig, method(), ev)
    b = integrate(sig, method(), ev)
    assert a.digest == b.digest


def test_method_json_roundtrip(tmp_path):
    m = nias_fid_method()
    m.timed_events.append(TimedEvent(12.0, E.SKIM_MODE, "tangent"))
    m.save(tmp_path / "m.json")
    m2 = IntegrationMethod.load(tmp_path / "m.json")
    assert m2 == m


def test_speed(run07):
    t = time.perf_counter()
    integrate(run07.fid, nias_fid_method())
    assert time.perf_counter() - t < 1.0


def test_oracle_istd_and_median(samples):
    """Agreement with the former ChemStation integration (RESULTS.CSV as oracle)."""
    from oracle_report import compare, parse_section
    from gcws.io.run_loader import load_run
    medians, istd = [], []
    for prefix in ("07_", "09_", "11_", "12_"):
        d = next(samples.glob(prefix + "*.D"))
        cs = parse_section(d / "RESULTS.CSV", "FID1A")
        res = integrate(load_run(d).fid, nias_fid_method())
        k = compare(res.peaks, cs)
        assert k["recall"] >= 0.95
        medians.append(k["median_ratio"])
        for t in (13.42, 18.92, 22.47):
            c = min(cs, key=lambda x: abs(x.rt - t))
            p = min(res.peaks, key=lambda x: abs(x.apex_rt - t))
            istd.append(p.area / c.area)
    assert abs(np.median(medians) - 1) < 0.05
    istd = np.array(istd)
    assert np.all(np.abs(istd - 1) < 0.05)
