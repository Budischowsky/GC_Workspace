"""Feature double determination on the reference batch (A/B pairs 07/11 and 09/12; skipped without it)."""
import numpy as np
import pytest

from tests.test_report import _ws_with


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def pair07(samples, qapp):
    ws = _ws_with(samples, ["07_", "11_"], qapp)
    return ws, [st.id for st in ws.states()]


def test_real_pairing_07_11(pair07):
    from gcws.features import service as SV
    ws, ids = pair07
    table = SV.build(ws, ids)
    cfg = table.settings
    both = [f for f in table.features if len(f.found) == 2]
    one = [f for f in table.features if len(f.found) == 1]
    assert len(both) >= 140                      # 170 / 168 FID peaks
    assert len(one) <= 40
    assert table.maps[ids[1]].n_anchors >= 10
    drt = np.array([abs(f.members[0].rt_ref - f.members[1].rt_ref) for f in both])
    assert np.quantile(drt, 0.99) <= 0.03
    for f in both:
        if f.sim is None:
            continue
        # a pair is either spectrally similar, or the same peak by retention time (case D)
        assert f.sim >= cfg.min_sim or (f.mismatch and abs(f.members[0].rt_ref - f.members[1].rt_ref) <= cfg.exact_rt)
    assert len({f.id for f in table.features}) == len(table.features)


def test_real_ids_are_stable(pair07):
    from gcws.features import service as SV
    ws, ids = pair07
    t1 = SV.build(ws, ids)
    group = {"members": ids, "features": {"ids": t1.id_records()}}
    t2 = SV.build(ws, ids, group=group)
    assert [f.id for f in t1.features] == [f.id for f in t2.features]
