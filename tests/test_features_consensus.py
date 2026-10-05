"""Feature double determination, P66: consensus spectrum and consensus identification."""
from types import SimpleNamespace

import numpy as np
import pytest

from gcws import paths
from gcws.core.ident import Identification
from gcws.features import consensus as C
from gcws.features.model import DETECTED, GAPFILL, Feature, Member, PeakInfo, Settings

SPEC = (np.array([57, 71, 85, 99, 113, 142]), np.array([100.0, 70.0, 45.0, 25.0, 15.0, 8.0]))


def hits(*items):
    return [{"name": n, "cas": c, "score": s} for n, c, s in items]


def member(label, hit_list, *, origin=DETECTED, manual=False, istd="", spec=SPEC, rt=10.0):
    ident = None
    if hit_list or manual or istd:
        top = hit_list[0] if hit_list else {"name": "Manual name", "cas": "1-1-1", "score": None}
        ident = Identification(apex_rt=rt, name=top["name"], cas=top["cas"], score=top["score"],
                               status="Accepted", hits=hit_list, manual=manual, istd=istd)
    p = PeakInfo(index=0, rt=rt, start=rt - 0.02, end=rt + 0.02, area=1e5, height=5e3, spectrum=spec,
                 ident=ident, hits=list(hit_list or []), istd=bool(istd))
    return Member(label.lower(), label, p, origin, rt)


def feature(*members, sim=0.9, mismatch=False):
    return Feature(members=list(members), rt=10.0, id="F-001", sim=sim, mismatch=mismatch)


X, Y, Z = ("Xylene", "95-47-6"), ("Ethylbenzene", "100-41-4"), ("Styrene", "100-42-5")


def test_case_a_same_first_hit():
    f = feature(member("A", hits((*X, 88), (*Y, 80))), member("B", hits((*X, 85), (*Z, 70))))
    ident = C.decide(f, Settings())
    assert (ident.case, ident.name, ident.cas, ident.status) == ("A", "Xylene", "95-47-6", C.ACCEPTED)
    assert C.write_back(f, ident, "FID") == []


def test_case_c_swapped_close_candidates_report_both():
    f = feature(member("A", hits((*X, 86), (*Y, 84))), member("B", hits((*Y, 85), (*X, 84.5))))
    ident = C.decide(f, Settings())
    assert ident.case == "C" and ident.status == C.REVIEW
    assert ident.name == "Xylene / Ethylbenzene" and ident.cas == "95-47-6 / 100-41-4"
    assert ident.candidates[0]["scores"] == {"A": 86.0, "B": 84.5}
    assert C.write_back(f, ident, "FID") == []


def test_case_b_clear_leader_over_both_hit_lists():
    f = feature(member("A", hits((*X, 88), (*Y, 80))), member("B", hits((*Y, 82), (*X, 81))))
    ident = C.decide(f, Settings())
    assert (ident.case, ident.name, ident.status) == ("B", "Xylene", C.ACCEPTED)
    assert ident.margin == pytest.approx(3.5)
    (p,) = C.write_back(f, ident, "FID")
    assert p.run_id == "b" and p.kind == "identity"
    assert p.ident.name == "Xylene" and p.ident.status == C.ACCEPTED
    assert [h["name"] for h in p.ident.hits] == ["Xylene", "Ethylbenzene"]
    assert p.ident.source == C.SOURCE and not p.ident.manual


def test_consensus_search_decides():
    f = feature(member("A", hits((*X, 86), (*Y, 84))), member("B", hits((*Y, 85), (*X, 84.5))))
    f.consensus_hits = hits((*Y, 90), (*X, 80))
    ident = C.decide(f, Settings())
    assert (ident.case, ident.name, ident.status) == ("B", "Ethylbenzene", C.ACCEPTED)
    assert "consensus spectrum" in ident.basis
    (p,) = C.write_back(f, ident, "FID")
    assert p.run_id == "a" and p.ident.name == "Ethylbenzene"


def test_async_comparison_defers_identity_until_consensus(monkeypatch):
    from gcws.features import service as SV
    from gcws.features.model import FeatureTable, Proposal

    f = feature(member("A", hits((*X, 86), (*Y, 84))), member("B", hits((*Y, 85), (*X, 84.5))))
    f.proposals = [Proposal("identity", "b", "FID", "rename", ident=Identification(10.0, name=X[0]), rt=10.0)]
    table = FeatureTable(["a", "b"], ["A", "B"], "FID", [f], settings=Settings())
    ws = SimpleNamespace(quant={}, runs={}, replicate_groups=[])
    monkeypatch.setattr(SV, "build", lambda *args, **kwargs: table)
    applied = []
    monkeypatch.setattr(SV, "apply", lambda _ws, _table, props, **kwargs: applied.extend(props) or 0)

    SV.run(ws, ["a", "b"], search=False)
    assert applied == []
    needed = SV.consensus_needed(ws, table, Settings())
    assert len(needed) == 1

    f.consensus_hits = hits((*Y, 90), (*X, 80))
    SV.store_consensus(ws, needed)
    SV.run(ws, ["a", "b"], search=False)
    assert applied == f.proposals


def test_case_d_mismatch():
    f = feature(member("A", hits((*X, 86))), member("B", hits((*Z, 85))), sim=0.3, mismatch=True)
    ident = C.decide(f, Settings())
    assert ident.case == "D" and ident.status == C.CONFLICT
    assert ident.name == "Xylene / Styrene"


def test_analyst_and_istd_win():
    f = feature(member("A", [], manual=True), member("B", hits((*X, 90))))
    ident = C.decide(f, Settings())
    assert (ident.case, ident.name, ident.status) == ("manual", "Manual name", C.ACCEPTED)
    assert C.write_back(f, ident, "FID") == []
    g = feature(member("A", hits(("BBP-d4", "", 95)), istd="IS2"), member("B", hits((*X, 90))))
    assert C.decide(g, Settings()).case == "istd"


def test_gap_filled_member_takes_the_name():
    f = feature(member("A", hits((*X, 88), (*Y, 80))), member("B", [], origin=GAPFILL))
    ident = C.decide(f, Settings())
    assert ident.case == "A" and ident.name == "Xylene"
    (p,) = C.write_back(f, ident, "FID")
    assert p.run_id == "b" and p.ident.name == "Xylene" and p.ident.hits[0]["name"] == "Xylene"


def test_below_the_quality_limit_everywhere_stays_unknown():
    a, b = member("A", hits((*X, 60))), member("B", hits((*Y, 65)))
    for m, name in ((a, "unknown (m/z 57, 71)"), (b, "possible derivative of benzene")):
        m.peak.ident.status, m.peak.ident.name = "Uncertain", name
    f = feature(a, b)
    ident = C.decide(f, Settings(), quality_limit=70)
    assert ident.case == "U" and ident.status == "Uncertain"
    assert ident.name == "possible derivative of benzene"           # the better of the two
    assert C.write_back(f, ident, "FID") == []


def test_same_hit_accepted_in_one_determination_only():
    b = member("B", hits((*X, 65), (*Y, 60)))
    b.peak.ident.status = "Uncertain"
    f = feature(member("A", hits((*X, 82))), b)
    ident = C.decide(f, Settings(), quality_limit=70)
    assert (ident.case, ident.name, ident.status) == ("A", "Xylene", C.ACCEPTED)
    assert "below 70 in B" in ident.basis


def test_one_accepted_candidate_against_a_weak_other():
    b = member("B", hits((*Y, 66)))
    b.peak.ident.status = "Uncertain"
    f = feature(member("A", hits((*X, 84))), b)
    ident = C.decide(f, Settings(), quality_limit=70)
    assert ident.case == "C" and ident.name == "Xylene" and ident.status == C.REVIEW
    assert "accepted in A only" in ident.basis


def test_synonyms_and_missing_cas_are_one_substance():
    f = feature(member("A", hits(("Benzaldehyde, 2,4,5-trimethyl-", "5779-72-6", 90))),
                member("B", hits(("Benzaldehyde, 2,4,5-trimethyl-", "", 91))))
    assert C.decide(f, Settings()).case == "A"
    g = feature(member("A", hits(("2,4,5-Trimethylbenzaldehyde", "5779-72-6", 88))),
                member("B", hits(("Benzaldehyde, 2,4,5-trimethyl-", "5779-72-6", 91))))
    assert C.decide(g, Settings()).case == "A"


def test_ri_breaks_a_tie():
    a = member("A", [{"name": X[0], "cas": X[1], "score": 86, "ri": 905}, {"name": Y[0], "cas": Y[1], "score": 84, "ri": 860}])
    b = member("B", [{"name": Y[0], "cas": Y[1], "score": 85, "ri": 860}, {"name": X[0], "cas": X[1], "score": 84.5, "ri": 905}])
    f = feature(a, b)
    ident = C.decide(f, Settings(ri_tol=20.0), ri=903.0)
    assert (ident.case, ident.name) == ("B", "Xylene")
    assert C.ri_score(903, 905, 20) == pytest.approx(0.9)
    assert C.ri_score(None, 905, 20) is None


def test_consensus_spectrum_average():
    s1 = (np.array([57, 71, 85, 99, 113]), np.array([100.0, 50.0, 30.0, 20.0, 10.0]))
    s2 = (np.array([57, 71, 85, 99, 127]), np.array([200.0, 120.0, 60.0, 40.0, 10.0]))
    f = feature(member("A", [], spec=s1), member("B", [], spec=s2), sim=0.9)
    mz, ab = C.consensus_spectrum(f, Settings())
    assert list(mz) == [57, 71, 85, 99, 113, 127]
    assert ab[0] == pytest.approx(999.0)
    assert ab[1] == pytest.approx((499.5 + 599.4) / 2)
    f.sim = 0.5
    assert C.consensus_spectrum(f, Settings()) is None


@pytest.fixture
def library(tmp_path, monkeypatch):
    from gcws.identify import library_edit as LE
    from gcws.libsearch import service, store
    monkeypatch.setattr(paths, "DATA", tmp_path)
    service.reset()
    recs = [("Hexadecane", [(57, 999), (71, 650), (85, 420), (99, 150), (226, 20)]),
            ("Pentadecane", [(57, 999), (71, 700), (85, 480), (99, 170), (212, 25)])]
    p = tmp_path / "Own.msp"
    p.write_text(LE.write_msp([LE.new_record(n, pk) for n, pk in recs]), encoding="cp1252", newline="")
    store.save(store.discover(p))
    yield
    service.reset()


def test_search_consensus_with_a_library(library):
    import gc_search_method as SM
    method = SM.SearchMethod(libraries=[SM.LibraryEntry("Own", True)], algorithm="similarity")
    s = (np.array([57, 71, 85, 99, 226]), np.array([999.0, 650.0, 420.0, 150.0, 20.0]))
    f = feature(member("A", [], spec=s), member("B", [], spec=s), sim=0.95)
    f.consensus = C.consensus_spectrum(f, Settings())
    note = C.search_consensus(None, method, [f])
    assert note == ""
    assert f.consensus_hits and f.consensus_hits[0]["name"] == "Hexadecane"


def test_search_without_libraries_is_a_note(tmp_path, monkeypatch):
    import gc_search_method as SM
    from gcws.libsearch import service
    monkeypatch.setattr(paths, "DATA", tmp_path)
    service.reset()
    try:
        f = feature(member("A", []), member("B", []))
        f.consensus = SPEC
        note = C.search_consensus(None, SM.SearchMethod(), [f])
        assert note.startswith("consensus spectra not searched")
        assert f.consensus_hits == []
    finally:
        service.reset()


# -- the consensus search through the Fast search ---------------------------------------------------

from tests.test_fast_search import data, libraries  # noqa: E402,F401  (fixtures)


def _search_method():
    import gc_search_method as SM
    return SM.SearchMethod(name="T", libraries=[SM.LibraryEntry("A", True), SM.LibraryEntry("B", True)],
                           algorithm="pbm", mode="sequential", stop_score=60, top_n=5)


def _consensus_features(queries):
    """12 consensus spectra in 3 search ranges (a weak ion on each bound), 4 per range."""
    feats = []
    for n, (_name, points) in enumerate(queries[:12]):
        lo, hi = ((35, 300), (50, 300), (35, 200))[n % 3]
        points = [(lo, 1.0)] + [(m, a) for m, a in points if lo < m < hi] + [(hi, 1.0)]
        f = feature(member("A", []), member("B", []))
        f.id = f"F-{n + 1:03d}"
        f.consensus = (np.array([int(m) for m, _a in points]), np.array([float(a) for _m, a in points]))
        feats.append(f)
    return feats


def _points(f):
    return [(int(m), float(a)) for m, a in zip(*f.consensus)]


def _expected(feats, method):
    import gc_identify as GI
    from gcws.identify import service as IS
    IS.prepare_local(method)
    return [[dict(GI.hit_record(h), peaks=h.get("peaks") or []) for h in IS.search_spectrum(_points(f), f.id, method)]
            for f in feats]


@pytest.mark.parametrize("fast", [True, False])
def test_search_consensus_equals_per_spectrum(libraries, monkeypatch, fast):
    from gcws.identify import service as IS
    from gcws.libsearch import service as LS
    method = _search_method()
    feats = _consensus_features(libraries)
    expected = _expected(feats, method)
    monkeypatch.setattr(IS, "fast_search_methods", lambda: {"T"} if fast else set())
    calls = []
    real = LS.analyze_many
    monkeypatch.setattr(LS, "analyze_many", lambda spectra, settings, *a, **k: calls.append(settings)
                        or real(spectra, settings, *a, **k))
    assert C.search_consensus(None, _search_method(), feats) == ""
    assert [f.consensus_hits for f in feats] == expected
    assert any(expected)
    assert bool(calls) == fast


def test_search_consensus_runs_ranges_in_ascending_order(libraries, monkeypatch):
    from gcws.identify import service as IS
    from gcws.libsearch import service as LS
    method = _search_method()
    feats = _consensus_features(libraries)
    monkeypatch.setattr(IS, "fast_search_methods", lambda: {"T"})
    ranges = []
    real = LS.analyze_many
    monkeypatch.setattr(LS, "analyze_many", lambda spectra, settings, *a, **k: ranges.append(
        (settings["min_mz"], settings["max_mz"])) or real(spectra, settings, *a, **k))
    C.search_consensus(None, method, feats)
    distinct = {C.search_range(_points(f), method) for f in feats}
    assert len(distinct) == 3
    assert ranges == sorted(ranges) and len(ranges) == len(distinct)


def test_failed_fast_result_is_a_reason(libraries, monkeypatch):
    from gcws.identify import service as IS
    from gcws.libsearch import service as LS
    feats = _consensus_features(libraries)
    monkeypatch.setattr(IS, "fast_search_methods", lambda: {"T"})
    failed = []
    real = LS.analyze_many

    def boom(spectra, settings, *a, **k):
        out = real(spectra, settings, *a, **k)
        if len(out) > 1 and not failed:
            failed.append(spectra[1][0])
            out[1] = ValueError("boom")
        return out
    monkeypatch.setattr(LS, "analyze_many", boom)
    assert C.search_consensus(None, _search_method(), feats) == ""
    assert failed
    for f in feats:
        if f.id == failed[0]:
            assert f.reasons == ["consensus search failed: boom"] and f.consensus_hits == []
        else:
            assert not f.reasons
    assert sum(1 for f in feats if f.consensus_hits) >= 8


# -- no wasted searches; consensus hits saved with the replicate group ------------------------------

def _dd(monkeypatch, *, manual=False, istd="", signature="s1"):
    """``(ws, group, table, f)``: one feature whose first hits differ (a consensus search is due)."""
    from gcws.features.model import FeatureTable
    from gcws.libsearch import service as LS
    monkeypatch.setattr(LS, "library_signature", lambda: signature)
    f = feature(member("A", hits((*X, 86), (*Y, 85)), manual=manual, istd=istd),
                member("B", hits((*Y, 85), (*X, 84.5))))
    group = {"id": "g", "members": ["a", "b"]}
    ws = SimpleNamespace(quant={}, runs={}, replicate_groups=[group], dirty=False)
    return ws, group, FeatureTable(["a", "b"], ["A", "B"], "FID", [f], settings=Settings()), f


def test_manual_and_istd_features_are_not_searched(monkeypatch):
    from gcws.features import service as SV
    ws, _g, table, _f = _dd(monkeypatch)
    assert len(SV.consensus_needed(ws, table, Settings())) == 1
    ws, _g, table, _f = _dd(monkeypatch, manual=True)
    assert SV.consensus_needed(ws, table, Settings()) == []
    ws, _g, table, _f = _dd(monkeypatch, istd="IS1")
    assert SV.consensus_needed(ws, table, Settings()) == []


def test_consensus_key_changes_with_spectrum_method_and_libraries(monkeypatch):
    import gc_search_method as SM
    from gcws.features import service as SV
    from gcws.libsearch import service as LS
    ws, _g, _t, f = _dd(monkeypatch)
    spec = C.consensus_spectrum(f, Settings())
    key = SV.consensus_cache_key(spec, SV.consensus_context(ws, ["a", "b"]))
    near = (spec[0], spec[1] + np.r_[0.01, np.zeros(spec[1].size - 1)])
    far = (spec[0], spec[1] + np.r_[1.0, np.zeros(spec[1].size - 1)])
    assert SV.consensus_cache_key(near, SV.consensus_context(ws, ["a", "b"])) == key
    assert SV.consensus_cache_key(far, SV.consensus_context(ws, ["a", "b"])) != key
    monkeypatch.setattr(LS, "library_signature", lambda: "s2")
    assert SV.consensus_cache_key(spec, SV.consensus_context(ws, ["a", "b"])) != key
    monkeypatch.setattr(LS, "library_signature", lambda: "s1")
    monkeypatch.setattr(SV, "search_method", lambda _ws, _members: SM.SearchMethod(name="Other"))
    assert SV.consensus_cache_key(spec, SV.consensus_context(ws, ["a", "b"])) != key


def test_stored_hits_are_used_without_a_search(monkeypatch):
    from gcws.features import service as SV
    from gcws.libsearch import service as LS
    ws, group, table, f = _dd(monkeypatch)
    key = SV.consensus_cache_key(C.consensus_spectrum(f, Settings()), SV.consensus_context(ws, ["a", "b"]))
    group["features"] = {"consensus": {key: hits((*Y, 90))}}

    def no_engine(*_a, **_k):
        raise AssertionError("the library engine must not be loaded")
    monkeypatch.setattr(LS, "get_engine", no_engine)
    assert SV.consensus_needed(ws, table, Settings()) == []
    assert f.consensus_hits == hits((*Y, 90)) and f.consensus_key == key


def test_store_consensus_saves_json_hits_and_marks_dirty(monkeypatch):
    import json
    from gcws.features import service as SV
    ws, group, table, f = _dd(monkeypatch)
    needed = SV.consensus_needed(ws, table, Settings())
    (key, _f), = needed
    f.consensus_hits = [dict(h, peaks=[(57, 999), (71, 650)]) for h in hits((*Y, 90))]
    SV.store_consensus(ws, needed, "x", group=group)
    assert "consensus" not in group.get("features", {}) and ws.dirty is False
    SV.store_consensus(ws, needed, group=group)
    assert group["features"]["consensus"][key] == json.loads(json.dumps(f.consensus_hits))
    assert ws.dirty is True
    ws2, group2, table2, f2 = _dd(monkeypatch)
    needed2 = SV.consensus_needed(ws2, table2, Settings())
    f2.consensus_hits = [{"name": "odd", "value": object()}]
    SV.store_consensus(ws2, needed2, group=group2)
    assert not (group2.get("features") or {}).get("consensus")
    assert ws2._feature_consensus[needed2[0][0]] is f2.consensus_hits


def test_foreign_keys_are_searched_again_and_pruned(monkeypatch):
    from gcws.features import service as SV
    ws, group, table, _f = _dd(monkeypatch)
    group["features"] = {"consensus": {"deadbeef": hits((*Z, 99))}}
    assert len(SV.consensus_needed(ws, table, Settings())) == 1
    SV.remember_ids(ws, ["a", "b"], table)
    assert "deadbeef" not in group["features"]["consensus"]
    assert ws.dirty is True
    ws.dirty = False
    SV.remember_ids(ws, ["a", "b"], table)
    assert ws.dirty is False


def test_library_change_invalidates_session_and_stored_hits(monkeypatch):
    from gcws.features import service as SV
    from gcws.libsearch import service as LS
    ws, group, table, f = _dd(monkeypatch, signature="s1")
    needed = SV.consensus_needed(ws, table, Settings())
    f.consensus_hits = hits((*Y, 90))
    SV.store_consensus(ws, needed, group=group)
    assert SV.consensus_needed(ws, table, Settings()) == []
    monkeypatch.setattr(LS, "library_signature", lambda: "s2")
    f.consensus_hits = []
    assert [x is f for _k, x in SV.consensus_needed(ws, table, Settings())] == [True]


def test_hits_survive_a_project_round_trip(monkeypatch):
    import json
    from gcws.features import service as SV
    ws, group, table, f = _dd(monkeypatch)
    needed = SV.consensus_needed(ws, table, Settings())
    f.consensus_hits = hits((*Y, 90))
    SV.store_consensus(ws, needed, group=group)
    ws2 = SimpleNamespace(quant={}, runs={}, replicate_groups=json.loads(json.dumps(ws.replicate_groups)))
    f.consensus_hits = []
    assert SV.consensus_needed(ws2, table, Settings()) == []
    assert f.consensus_hits == hits((*Y, 90))


@pytest.mark.parametrize("change", [dict(mz_auto=False), dict(mz_auto=False, min_mz=50, max_mz=300),
                                    dict(stop_score=60), "library order"])
def test_consensus_context_follows_every_search_setting(monkeypatch, change):
    import gc_search_method as SM
    from gcws.features import service as SV
    ws, _g, _t, _f = _dd(monkeypatch)
    libs = [SM.LibraryEntry("A", True), SM.LibraryEntry("B", True)]
    base = dict(name="M", libraries=libs, mz_auto=False, min_mz=35, max_mz=600) if change != dict(mz_auto=False) \
        else dict(name="M", libraries=libs)
    method = SM.SearchMethod(**base)
    if change == "library order":
        other = SM.SearchMethod(**dict(base, libraries=libs[::-1]))
    else:
        other = SM.SearchMethod(**dict(base, **change))
    monkeypatch.setattr(SV, "search_method", lambda _ws, _members: method)
    before = SV.consensus_context(ws, ["a", "b"])
    monkeypatch.setattr(SV, "search_method", lambda _ws, _members: other)
    assert SV.consensus_context(ws, ["a", "b"]) != before
