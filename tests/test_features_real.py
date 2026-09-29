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


def test_real_consensus_names_agree_after_apply(fresh07):
    from gcws.features import service as SV
    ws, ids = fresh07
    before = SV.build(ws, ids)
    resolved = [f.id for f in before.features if f.identity and f.identity.case == "B"]
    assert resolved
    n_before = ws.project_undo.count()
    after = SV.run(ws, ids, stack=ws.project_undo)
    assert ws.project_undo.count() == n_before + 1          # gap fills and names: one step
    for fid in resolved:
        f = after.by_id(fid)
        assert f.identity.case == "A", (fid, f.identity)
        names = {m.peak.name for m in f.found}
        assert len(names) == 1
    ws.project_undo.undo()
    again = SV.build(ws, ids, gapfill=False)
    assert sum(1 for f in again.features if f.identity and f.identity.case == "B") == len(resolved)


def test_real_undo_of_the_automatic_step_sticks(fresh07):
    from gcws.features import service as SV
    ws, ids = fresh07
    ws.replicate_groups = [{"id": "g", "name": "130m", "members": ids, "policy": "all"}]
    SV.run(ws, ids, stack=ws.project_undo)
    n = ws.project_undo.count()
    assert n == 1 and ws.replicate_groups[0]["features"]["auto_done"]
    ws.project_undo.undo()
    table = SV.run(ws, ids, stack=ws.project_undo)             # a later Compare
    assert ws.project_undo.count() == n and ws.project_undo.index() == 0   # nothing made again
    assert not any(m.origin == "gapfill" for f in table.features for m in f.members)
    assert SV.proposals(table)                                  # still offered as proposals


def test_real_boundary_proposals_bring_the_ratio_closer(fresh07):
    import math
    from gcws.features import harmonise as HM
    from gcws.features import service as SV
    ws, ids = fresh07
    table = SV.build(ws, ids, gapfill=False, search=False)
    props = [p for f in table.features for p in f.proposals if p.kind == "boundary"]
    assert props and all(not p.auto for p in props)
    feats = {f.id for f in table.features if any(p.kind == "boundary" for p in f.proposals)}
    ratio0 = HM.typical_ratio(table, ids[0], ids[1])

    def err(t, fid):
        f = t.by_id(fid)
        a, b = f.member(ids[0]), f.member(ids[1])
        return abs(math.log((b.area / a.area) / ratio0))
    before = {fid: err(table, fid) for fid in feats}
    SV.apply(ws, table, props, stack=ws.project_undo)
    after_t = SV.build(ws, ids, gapfill=False, search=False)
    better = sum(1 for fid in feats if err(after_t, fid) < before[fid])
    assert better >= len(feats) - 1                             # (the estimate is not the integrator)


def test_real_consensus_search_bookkeeping(fresh07):
    from gcws.features import service as SV
    ws, ids = fresh07
    cfg = SV.settings(ws)
    table = SV.build(ws, ids, search=False)
    needed = SV.consensus_needed(ws, table, cfg)
    assert needed                                   # first hits differ, one accepted, spectra agree
    for _ck, f in needed:
        f.consensus_hits = [{"name": "Test", "cas": "", "score": 99}]
    SV.store_consensus(ws, needed[:1])              # searched: cached
    SV.store_consensus(ws, needed[1:], "No library loaded")     # failed: not retried
    again = SV.build(ws, ids, search=False)
    assert not SV.consensus_needed(ws, again, cfg)
    f = again.by_id(needed[0][1].id)
    assert f.consensus_hits and f.consensus_hits[0]["name"] == "Test"
    assert any("No library" in n for n in again.notes)
