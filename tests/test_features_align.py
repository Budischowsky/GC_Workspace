"""Feature double determination, P64: pairing the determinations into features."""
import numpy as np
import pytest

from gcws.features import align as AL
from gcws.features import ids as IDS
from gcws.features.model import DETECTED, MISSING, PeakInfo, RunInput, Settings
from gcws.features.pseudo import coeluting, pearson

RNG = np.random.default_rng(3)


def spectrum(seed: int, n: int = 12):
    """A distinct EI-like spectrum: ``n`` ions, decreasing intensities."""
    r = np.random.default_rng(seed)
    mz = np.sort(r.choice(np.arange(41, 300), size=n, replace=False))
    ab = r.uniform(5, 100, size=n)
    ab[r.integers(n)] = 100.0
    return mz.astype(np.int64), ab


def noisy(spec, level=0.05, seed=0):
    r = np.random.default_rng(seed)
    mz, ab = spec
    return mz, ab * (1 + r.normal(0, level, ab.size)).clip(0.5)


def peak(i, rt, spec, area=1e5, **kw):
    return PeakInfo(index=i, rt=rt, start=rt - 0.02, end=rt + 0.02, area=area, height=area / 20,
                    width50=0.01, spectrum=spec, **kw)


def run(run_id, label, peaks):
    return RunInput(run_id, run_id, label, "FID", 0.005, sorted(peaks, key=lambda p: p.rt))


def pairs_of(table):
    """{reference index: other index} of the features found in both determinations."""
    out = {}
    for f in table.features:
        a, b = f.members
        if a.found and b.found:
            out[a.peak.index] = b.peak.index
    return out


def test_pairs_follow_drift_and_local_warp():
    rts = np.arange(6.0, 30.0, 0.6)
    specs = [spectrum(k) for k in range(rts.size)]
    a = run("a", "A", [peak(k, float(t), specs[k], area=1e5 * (1 + k % 4)) for k, t in enumerate(rts)])
    # drift grows from 0.02 to 0.08 min: beyond the 0.05 tolerance without the time map
    drift = 0.02 + (rts - 6.0) / 24.0 * 0.06
    b = run("b", "B", [peak(k, float(t + d), noisy(specs[k], seed=k), area=1.1e5 * (1 + k % 4))
                       for k, (t, d) in enumerate(zip(rts, drift))])
    table = AL.align([a, b], Settings())
    assert pairs_of(table) == {k: k for k in range(rts.size)}
    assert table.maps["b"].n_anchors >= 5
    assert all(f.sim is not None and f.sim > 0.8 for f in table.features)


def test_neighbours_with_similar_spectra_are_not_swapped():
    base = spectrum(100)
    close = (base[0], base[1] * np.linspace(0.8, 1.2, base[1].size))       # homologue-like
    others = [(8.0 + k, spectrum(200 + k)) for k in range(10)]
    a = run("a", "A", [peak(0, 12.00, base), peak(1, 12.03, close)] +
            [peak(10 + k, t, s) for k, (t, s) in enumerate(others)])
    b = run("b", "B", [peak(0, 12.04, noisy(base, seed=1)), peak(1, 12.07, noisy(close, seed=2))] +
            [peak(10 + k, t + 0.04, noisy(s, seed=k)) for k, (t, s) in enumerate(others)])
    table = AL.align([a, b], Settings())
    p = pairs_of(table)
    assert p[0] == 0 and p[1] == 1


def test_case_d_different_spectra_are_not_paired():
    x, y = spectrum(1), spectrum(2)
    a = run("a", "A", [peak(0, 15.00, x)])
    b = run("b", "B", [peak(0, 15.02, y)])
    table = AL.align([a, b], Settings(max_shift=0.0))
    assert pairs_of(table) == {}
    assert len(table.features) == 2
    assert all(len(f.found) == 1 for f in table.features)


def test_same_retention_time_with_different_spectra_is_a_mismatch():
    x, y = spectrum(1), spectrum(2)
    a = run("a", "A", [peak(0, 15.000, x)])
    b = run("b", "B", [peak(0, 15.003, y)])
    table = AL.align([a, b], Settings(max_shift=0.0))
    (f,) = table.features
    assert f.mismatch and len(f.found) == 2


def test_one_sided_feature_and_rt_only_pairing_without_ms():
    s = spectrum(5)
    a = run("a", "A", [peak(0, 10.0, s), peak(1, 11.0, None)])
    b = run("b", "B", [peak(0, 10.01, noisy(s)), peak(1, 11.005, None), peak(2, 14.0, spectrum(9))])
    table = AL.align([a, b], Settings(max_shift=0.0))
    assert pairs_of(table) == {0: 0, 1: 1}
    rt_only = next(f for f in table.features if f.members[0].found and f.members[0].peak.index == 1)
    assert rt_only.sim is None
    lone = next(f for f in table.features if not f.members[0].found)
    assert lone.members[0].origin == MISSING and lone.members[1].origin == DETECTED


def test_three_determinations_mzmine_greedy():
    rts = [8.0, 9.5, 11.0, 12.5]
    specs = [spectrum(40 + k) for k in range(4)]
    runs = []
    for r, (rid, shift) in enumerate((("a", 0.0), ("b", 0.02), ("c", -0.015))):
        peaks = [peak(k, t + shift, noisy(specs[k], seed=10 * r + k)) for k, t in enumerate(rts) if not (rid == "c" and k == 2)]
        runs.append(run(rid, rid.upper(), peaks))
    table = AL.align(runs, Settings())
    full = [f for f in table.features if len(f.found) == 3]
    assert len(full) == 3
    two = [f for f in table.features if len(f.found) == 2]
    assert len(two) == 1 and two[0].member("c").origin == MISSING
    for f in full:
        assert len({m.peak.index for m in f.found}) == 1


def test_three_determinations_keep_first_base_row_on_equal_score():
    a = run("a", "A", [peak(0, 1.0, None), peak(1, 1.25, None)])
    b = run("b", "B", [peak(0, 1.125, None)])
    c = run("c", "C", [peak(0, 1.0, None), peak(1, 1.25, None)])
    table = AL.align([a, b, c], Settings(max_shift=0.0, rt_tol=0.25))
    matched = next(f for f in table.features if f.member("b").found)
    assert matched.member("a").peak.index == 0
    assert matched.member("c").peak.index == 0


def test_stable_ids_survive_a_reintegration():
    specs = [spectrum(60 + k) for k in range(5)]
    a = run("a", "A", [peak(k, 8.0 + k, specs[k]) for k in range(5)])
    b = run("b", "B", [peak(k, 8.0 + k + 0.01, noisy(specs[k])) for k in range(5)])
    t1 = AL.align([a, b], Settings())
    IDS.stable_ids(t1.features, None)
    assert [f.id for f in t1.features] == ["F-001", "F-002", "F-003", "F-004", "F-005"]
    # re-integration: peak 1 disappears from A (indices shift), a new peak appears at 14.5
    a2 = run("a", "A", [peak(j, 8.0 + k, specs[k]) for j, k in enumerate((0, 2, 3, 4))] +
             [peak(4, 14.5, spectrum(99))])
    t2 = AL.align([a2, b], Settings())
    IDS.stable_ids(t2.features, t1.id_records())
    by_rt = {round(f.rt, 1): f.id for f in t2.features}
    assert by_rt[8.0] == "F-001" and by_rt[10.0] == "F-003" and by_rt[12.0] == "F-005"
    assert by_rt[9.0] == "F-002"               # B's peak alone keeps the id
    assert by_rt[14.5] == "F-006"


def test_split_one_peak_here_two_there():
    s1, s2 = spectrum(71), spectrum(72)
    wide = peak(0, 16.00, s1)
    wide.start, wide.end = 15.96, 16.08                      # A integrated both as one peak
    a = run("a", "A", [wide])
    b = run("b", "B", [peak(0, 16.00, noisy(s1)), peak(1, 16.05, s2)])
    table = AL.align([a, b], Settings(max_shift=0.0))
    assert all(f.split for f in table.features)
    lone = next(f for f in table.features if not f.members[0].found)
    assert "inside the peak" in lone.members[0].note


def test_split_detection_scales_with_nearby_features(monkeypatch):
    """A large pair must not inspect every distant feature for each peak."""
    from gcws.features.model import Feature

    spec = spectrum(73)
    a = run("a", "A", [peak(i, 5.0 + i * 0.05, spec) for i in range(600)])
    b = run("b", "B", [peak(i, 5.003 + i * 0.05, spec) for i in range(600)])
    original = Feature.member
    calls = 0

    def counted(self, run_id):
        nonlocal calls
        calls += 1
        return original(self, run_id)

    monkeypatch.setattr(Feature, "member", counted)
    table = AL.align([a, b], Settings(max_shift=0.0))
    assert len(table.features) == 600
    assert not any(f.split for f in table.features)
    assert calls < 6000


def test_ordered_matching_does_not_allocate_dense_float_score_grid():
    """Large sparse pairings keep only backtracking moves, not all score rows."""
    import tracemalloc

    n = 1000
    cands = [AL.Candidate(i, i, 1.0, 1.0, None, 0.0) for i in range(n)]
    tracemalloc.start()
    try:
        pairs = AL.ordered_matching(n, n, cands, 0.05)
        _current, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert [(c.i, c.j, ambiguous) for c, ambiguous in pairs] == [(i, i, False) for i in range(n)]
    assert peak_bytes < 5_000_000


def test_ordered_matching_keeps_leftmost_tie_and_ambiguity():
    candidates = [AL.Candidate(0, 0, 1.0, 1.0, None, 0.0),
                  AL.Candidate(0, 1, 1.0, 1.0, None, 0.0)]
    pairs = AL.ordered_matching(1, 2, candidates, 0.05)
    assert [(c.i, c.j, ambiguous) for c, ambiguous in pairs] == [(0, 0, True)]
    assert AL.ordered_matching(0, 2, candidates, 0.05) == []
    assert AL.ordered_matching(2, 0, candidates, 0.05) == []


def test_ordered_matching_without_candidates_skips_the_grid(monkeypatch):
    original = np.zeros

    def guarded(shape, *args, **kwargs):
        if isinstance(shape, tuple) and len(shape) == 2 and shape[0] * shape[1] > 1000:
            raise AssertionError("an empty match must not allocate the move grid")
        return original(shape, *args, **kwargs)

    monkeypatch.setattr(AL.np, "zeros", guarded)
    assert AL.ordered_matching(1000, 1000, [], 0.05) == []


# -- co-eluting ions ------------------------------------------------------------------------------

def test_coeluting_keeps_only_ions_with_the_peak_shape():
    from tests.test_deconv import build, RT
    ms = build([(200, {57: 100, 71: 60, 85: 40, 99: 20, 113: 10}, 60000.0)], background=True, seed=4)
    apex = float(RT[200])
    full = ms.nominal_spectrum_arrays(ms.scans_between(apex - 0.01, apex + 0.01))
    pure = coeluting(ms, apex - 0.03, apex + 0.03, full)
    assert pure is not None
    kept = set(int(m) for m in pure[0])
    assert {57, 71, 85, 99}.issubset(kept)
    assert 207 not in kept and 73 not in kept            # constant bleed, sloped background


def test_coeluting_batches_correlations_without_changing_ions(monkeypatch):
    from gcws.features import pseudo
    from tests.test_deconv import build, RT

    ms = build([(200, {57: 100, 71: 60, 85: 40, 99: 20, 113: 10}, 60000.0)],
               background=True, seed=4)
    apex = float(RT[200])
    full = ms.nominal_spectrum_arrays(ms.scans_between(apex - 0.01, apex + 0.01))
    expected = coeluting(ms, apex - 0.03, apex + 0.03, full)
    original = pseudo.pearson
    calls = 0

    def counted(x, y):
        nonlocal calls
        calls += 1
        return original(x, y)

    monkeypatch.setattr(pseudo, "pearson", counted)
    actual = coeluting(ms, apex - 0.03, apex + 0.03, full)
    assert actual is not None and expected is not None
    assert np.array_equal(actual[0], expected[0])
    assert np.array_equal(actual[1], expected[1])
    assert calls <= 2


def test_coeluting_exact_threshold_and_constant_profile():
    from gcws.features import pseudo

    ref = np.array([0, 1, 2, 3, 4, 5, 4, 3, 2, 1], dtype=float)
    candidate = np.array([0, 1, 1, 3, 3, 5, 3, 2, 1, 0], dtype=float)
    block = np.column_stack((ref, candidate, np.full(10, 2.0), ref[::-1]))

    class MS:
        n_scans = 10

        def scans_between(self, t0, t1):
            return np.arange(10)

        def dense_block(self, s0, s1, lo, hi):
            return block[s0:s1 + 1, lo - 50:hi - 49]

    spectrum = (np.arange(50, 54), np.array([100.0, 60.0, 20.0, 10.0]))
    threshold = pseudo.pearson(candidate, ref)
    selected = coeluting(MS(), 0, 1, spectrum, min_r=threshold, min_ions=2)
    assert selected is not None and selected[0].tolist() == [50, 51]
    with_constant = coeluting(MS(), 0, 1, spectrum, min_r=0.0)
    assert with_constant is not None and 52 in with_constant[0]


def test_pearson():
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert pearson([1, 1, 1], [1, 2, 3]) == 0.0
