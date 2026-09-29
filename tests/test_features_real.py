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


@pytest.fixture
def fresh07(samples, qapp):
    ws = _ws_with(samples, ["07_", "11_"], qapp)
    return ws, [st.id for st in ws.states()]


def test_real_gap_fill_finds_a_deleted_peak_again(fresh07):
    from gcws.core.events import ManualEvent, ManualKind
    from gcws.features import service as SV
    from gcws.features.model import GAPFILL
    from gcws.ui.undo import add_event
    ws, ids = fresh07
    table = SV.build(ws, ids)
    # a clear pair with a comparable spectrum, away from neighbours
    cands = [f for f in table.features if len(f.found) == 2 and f.sim is not None and f.sim >= 0.8
             and not f.split and f.members[1].peak.sn and f.members[1].peak.sn > 30]
    target = max(cands, key=lambda f: f.members[1].peak.height)
    b = target.members[1].peak
    original = b.area
    ws.project_undo.push(add_event(ws, ids[1], "FID", ManualEvent(ManualKind.DELETE, b.rt)))
    table = SV.build(ws, ids)
    f = table.by_id(target.id)
    assert f is not None and not f.members[1].found
    (p,) = [p for p in f.proposals if p.kind == "gapfill"]
    assert "co-elute" in p.text                     # MS-confirmed
    n = SV.apply(ws, table, stack=ws.project_undo)
    assert n >= 1
    after = SV.build(ws, ids, gapfill=False)
    g = after.by_id(target.id)
    assert g.members[1].origin == GAPFILL
    assert g.members[1].area == pytest.approx(original, rel=0.15)
    ws.project_undo.undo()                          # the gap fills are one step
    back = SV.build(ws, ids, gapfill=False)
    assert not back.by_id(target.id).members[1].found


def test_real_run_applies_and_keeps_ids(fresh07):
    from gcws.features import service as SV
    ws, ids = fresh07
    ws.replicate_groups = [{"id": "g", "name": "130m", "members": ids, "policy": "all"}]
    before = SV.build(ws, ids)
    table = SV.run(ws, ids, stack=ws.project_undo)
    gap = [f for f in table.features if any(m.origin == "gapfill" for m in f.members)]
    assert gap
    assert ws.replicate_groups[0]["features"]["ids"]
    ids_before = {f.id for f in before.features}
    assert {f.id for f in gap} <= ids_before       # a filled feature keeps its id
