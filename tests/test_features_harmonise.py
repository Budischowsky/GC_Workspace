"""Feature double determination, P68: harmonised boundaries, proposed only where they help."""
from types import SimpleNamespace

import numpy as np
import pytest

from gcws.core.events import ManualKind
from gcws.core.model import Signal
from gcws.features import harmonise as HM
from gcws.features.model import DETECTED, Feature, FeatureTable, Member, PeakInfo, RunInput, Settings

T = np.arange(5.0, 20.0, 0.0005)
APEXES = [6.0, 7.0, 8.0, 9.0, 10.0, 12.0]          # the last one is the feature under test


def signal(tail=False):
    y = 1000.0 + sum(20000.0 * np.exp(-0.5 * ((T - a) / 0.006) ** 2) for a in APEXES)
    if tail:                                          # a slow hump behind the last peak (B only)
        y = y + 3000.0 * np.exp(-0.5 * ((T - 12.06) / 0.02) ** 2)
    return Signal("FID", T.copy(), y)


def peaks_of(sig, ends):
    out = []
    for i, (a, (lo, hi)) in enumerate(zip(APEXES, ends)):
        area = HM.signal_area(sig, a - lo, a + hi)
        out.append(PeakInfo(index=i, rt=a, start=a - lo, end=a + hi, area=area, height=20000.0, width50=0.014,
                            sn=100.0 if i else 90.0))
    return out


def table_for(b_ends, b_tail=True):
    sa, sb = signal(), signal(tail=b_tail)
    std = [(0.03, 0.03)] * len(APEXES)
    pa, pb = peaks_of(sa, std), peaks_of(sb, b_ends)
    pa[-1].sn = 200.0                                 # A is the reference of the tested feature
    ra = RunInput("a", "a", "A", "FID", 0.005, pa)
    rb = RunInput("b", "b", "B", "FID", 0.005, pb)
    feats = [Feature(members=[Member("a", "A", x, DETECTED, x.rt), Member("b", "B", y, DETECTED, y.rt)],
                     rt=x.rt, id=f"F-{k + 1:03d}", sim=0.9) for k, (x, y) in enumerate(zip(pa, pb))]
    table = FeatureTable(["a", "b"], ["A", "B"], "FID", feats, settings=Settings(), inputs=[ra, rb])
    runs = {"a": SimpleNamespace(signal=lambda key: sa), "b": SimpleNamespace(signal=lambda key: sb)}
    return table, runs


def test_a_wide_end_that_adds_a_hump_is_proposed_back():
    ends = [(0.03, 0.03)] * 5 + [(0.03, 0.12)]          # B's last peak ends 0.12 min after its apex
    table, runs = table_for(ends)
    f = table.features[-1]
    props = HM.propose(f, table, Settings(), runs)
    (p,) = props
    assert p.kind == "boundary" and not p.auto and p.run_id == "b"
    assert p.event.kind == ManualKind.MOVE_END and p.event.t0 == pytest.approx(12.03)
    assert p.event.ref_rt == pytest.approx(12.0)


def test_nothing_when_it_would_not_help():
    ends = [(0.03, 0.03)] * 5 + [(0.03, 0.12)]
    table, runs = table_for(ends, b_tail=False)       # the wider end only adds flat baseline
    assert HM.propose(table.features[-1], table, Settings(), runs) == []


def test_shared_valley_is_not_moved():
    ends = [(0.03, 0.03)] * 5 + [(0.03, 0.12)]
    table, runs = table_for(ends)
    b = table.inputs[1]
    neighbour = PeakInfo(index=9, rt=12.2, start=b.peaks[-1].end, end=12.25, area=1e4)   # shares the end
    b.peaks.append(neighbour)
    assert HM.propose(table.features[-1], table, Settings(), runs) == []


def test_same_offsets_need_nothing():
    table, runs = table_for([(0.03, 0.03)] * 6)
    assert all(HM.propose(f, table, Settings(), runs) == [] for f in table.features)


def test_signal_area_is_above_the_chord():
    sig = Signal("FID", np.linspace(0, 1, 101), np.linspace(10, 20, 101))
    assert HM.signal_area(sig, 0.2, 0.8) == pytest.approx(0.0, abs=1e-9)
