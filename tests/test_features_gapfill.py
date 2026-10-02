"""Feature double determination, P65: gap filling (mzmine Gap port + MS evidence)."""
import copy
from types import SimpleNamespace

import numpy as np
import pytest

from gcws.core.model import Signal
from gcws.features import align as AL
from gcws.features import gapfill as GF
from gcws.features.model import GAPFILL_OPTION, NOT_DETECTABLE, PeakInfo, RunInput, Settings
from gcws.features.pseudo import coeluting
from tests.test_deconv import RT, build

DELAY = 0.005
SPEC_X = {57: 100, 71: 70, 85: 45, 99: 25, 113: 15, 142: 8}
SPEC_Y = {91: 100, 105: 60, 120: 40, 65: 20, 77: 15, 51: 10}
FID_RT = np.arange(RT[0] + 0.02, RT[-1] - 0.02, 0.0005)
APEX_SCAN = 200


def fid_signal(peaks, noise=4.0, seed=1):
    """``peaks``: [(apex, height)] in FID time; Gaussian, sigma 0.006 min, baseline 1000."""
    r = np.random.default_rng(seed)
    y = 1000.0 + r.normal(0, noise, FID_RT.size)
    for apex, h in peaks:
        y += h * np.exp(-0.5 * ((FID_RT - apex) / 0.006) ** 2)
    return Signal("FID", FID_RT.copy(), y)


def raw_run(ms, fid):
    return SimpleNamespace(ms=ms, signal=lambda key: fid if key == "FID" else None)


def peak_of(ms, apex_ms, area=1e5, index=0):
    t0, t1 = apex_ms - 0.02, apex_ms + 0.02
    full = ms.nominal_spectrum_arrays(ms.scans_between(apex_ms - 0.004, apex_ms + 0.004))
    pure = coeluting(ms, t0, t1, full)
    return PeakInfo(index=index, rt=apex_ms + DELAY, start=t0 + DELAY, end=t1 + DELAY, area=area,
                    height=area / 20, width50=0.014, spectrum=pure, full_spectrum=full)


def table_for(ms_b, fid_b, settings=None):
    """A has substance X at scan 200; B is given. Returns (table, runs, noise)."""
    ms_a = build([(APEX_SCAN, SPEC_X, 80000.0)], background=True, seed=2)
    apex = float(RT[APEX_SCAN])
    a = RunInput("a", "a", "A", "FID", DELAY, [peak_of(ms_a, apex)])
    b = RunInput("b", "b", "B", "FID", DELAY, [])
    settings = settings or Settings(max_shift=0.0)
    table = AL.align([a, b], settings)
    table.inputs = [a, b]
    fid_a = fid_signal([(apex + DELAY, 5000.0)])
    runs = {"a": raw_run(ms_a, fid_a), "b": raw_run(ms_b, fid_b)}
    noise = {"a": 24.0, "b": 24.0}
    return table, runs, noise, apex, settings


def test_gap_peak_port_takes_highest_and_walks_out():
    rt = np.arange(0, 30) * 0.01
    y = np.zeros(30)
    y[5:10] = [1, 3, 6, 3, 1]        # small peak inside the window
    y[14:21] = [2, 5, 9, 14, 9, 5, 2]  # larger peak inside the window
    found = GF.gap_peak(rt, y, 0.03, 0.2, 0.2, 1)
    assert found == (13, 17, 21)       # the highest one (mzmine would keep the last)
    assert GF.gap_peak(rt, np.zeros(30), 0.03, 0.2) is None


def test_quant_ion_is_mzmine_consensus():
    s1 = (np.array([57, 71, 85]), np.array([100.0, 70.0, 40.0]))
    s2 = (np.array([57, 71, 99]), np.array([60.0, 100.0, 10.0]))
    s3 = (np.array([71, 85]), np.array([100.0, 30.0]))
    assert GF.quant_ion([s1, s2, s3]) == 71          # present in all three
    assert GF.quant_ion([s1]) == 57


def test_fills_a_peak_below_the_integrator_threshold():
    ms_b = build([(APEX_SCAN + 2, SPEC_X, 30000.0)], background=True, seed=5)
    apex_b = float(RT[APEX_SCAN + 2]) + DELAY
    fid_b = fid_signal([(apex_b, 1500.0)], seed=3)
    table, runs, noise, apex, st = table_for(ms_b, fid_b)
    GF.fill_table(table, runs, noise, st)
    (f,) = table.features
    (p,) = f.proposals
    assert p.kind == "gapfill" and p.run_id == "b"
    assert p.event.option == GAPFILL_OPTION
    assert p.event.t0 < apex_b < p.event.t1
    assert abs(p.rt - apex_b) < 0.003
    assert "co-elute" in p.text


def test_nothing_there_is_not_detectable():
    ms_b = build([(60, SPEC_Y, 50000.0)], background=True, seed=6)      # something else, elsewhere
    fid_b = fid_signal([], seed=4)
    table, runs, noise, apex, st = table_for(ms_b, fid_b)
    GF.fill_table(table, runs, noise, st)
    (f,) = table.features
    m = f.member("b")
    assert m.origin == NOT_DETECTABLE and "not detectable" in m.note
    assert not f.proposals


def test_gap_fill_reuses_height_ratio_for_same_pair(monkeypatch):
    ms_b = build([], background=True, seed=15)
    table, runs, noise, _apex, settings = table_for(ms_b, fid_signal([]))
    table.features.append(copy.deepcopy(table.features[0]))
    original = GF.height_ratio
    calls = 0

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(GF, "height_ratio", counted)
    GF.fill_table(table, runs, noise, settings)
    assert all(f.member("b").origin == NOT_DETECTABLE for f in table.features)
    assert calls <= 1


def test_other_substance_at_the_same_time_is_not_filled():
    ms_b = build([(APEX_SCAN, SPEC_Y, 60000.0)], background=True, seed=7)
    fid_b = fid_signal([(float(RT[APEX_SCAN]) + DELAY, 4000.0)], seed=5)
    table, runs, noise, apex, st = table_for(ms_b, fid_b)
    GF.fill_table(table, runs, noise, st)
    (f,) = table.features
    assert f.member("b").origin == NOT_DETECTABLE
    assert not f.proposals


def test_ms_confirmed_but_fid_below_detection():
    ms_b = build([(APEX_SCAN, SPEC_X, 30000.0)], background=True, seed=8)
    fid_b = fid_signal([(float(RT[APEX_SCAN]) + DELAY, 20.0)], seed=6)     # S/N < 3
    table, runs, noise, apex, st = table_for(ms_b, fid_b)
    GF.fill_table(table, runs, noise, st)
    (f,) = table.features
    m = f.member("b")
    assert m.origin == NOT_DETECTABLE
    assert "MS confirms" in m.note and "S/N" in m.note


def test_fid_only_without_comparable_spectrum():
    ms_b = build([], background=True, seed=9)
    apex_b = float(RT[APEX_SCAN]) + DELAY
    fid_b = fid_signal([(apex_b, 2000.0)], seed=7)                  # 40 % of A's height (5000)
    table, runs, noise, apex, st = table_for(ms_b, fid_b)
    table.features[0].members[0].peak.spectrum = None               # A's MS too weak to compare
    GF.fill_table(table, runs, noise, st)
    (p,) = table.features[0].proposals
    assert "FID only" in p.text


def test_fid_only_ripple_is_not_the_peak():
    ms_b = build([], background=True, seed=9)
    apex_b = float(RT[APEX_SCAN]) + DELAY
    fid_b = fid_signal([(apex_b, 600.0)], seed=7)                   # 12 % of the expected height
    table, runs, noise, apex, st = table_for(ms_b, fid_b)
    table.features[0].members[0].peak.spectrum = None
    GF.fill_table(table, runs, noise, st)
    f = table.features[0]
    assert not f.proposals
    assert "expected height" in f.member("b").note


def test_never_into_a_neighbour():
    ms_b = build([(APEX_SCAN, SPEC_X, 30000.0)], background=True, seed=10)
    apex_b = float(RT[APEX_SCAN]) + DELAY
    fid_b = fid_signal([(apex_b, 1500.0), (apex_b + 0.03, 6000.0)], seed=8)
    table, runs, noise, apex, st = table_for(ms_b, fid_b)
    neighbour = PeakInfo(index=0, rt=apex_b + 0.03, start=apex_b + 0.012, end=apex_b + 0.05, area=1e5)
    table.inputs[1].peaks.append(neighbour)
    GF.fill_table(table, runs, noise, st)
    (p,) = table.features[0].proposals
    assert p.event.t1 <= neighbour.start + 1e-9
