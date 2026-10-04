"""mzmine similarity port and the retention-time map (feature double determination, P63)."""
import math

import numpy as np
import pytest

from gcws.features import similarity as S
from gcws.features import timemap as T


def _weighted(mz, i, a=0.6, b=3.0):
    return (i ** a) * (mz ** b) if i > 0 else 0.0


def test_weights_match_mzmine():
    assert (S.NIST_GC.intensity, S.NIST_GC.mz) == (0.6, 3.0)
    assert (S.MASSBANK.intensity, S.MASSBANK.mz) == (0.5, 2.0)
    assert (S.NIST11.intensity, S.NIST11.mz) == (0.53, 1.3)
    assert (S.SQRT.intensity, S.SQRT.mz) == (0.5, 0.0)
    assert S.NIST_GC.apply(100.0, 4.0) == pytest.approx(4.0 ** 0.6 * 100.0 ** 3)


def test_composite_hand_computed():
    lib = {50: 100.0, 60: 50.0}
    query = {50: 100.0, 60: 25.0, 70: 10.0}
    # KEEP_ALL_AND_MATCH_TO_ZERO: union 50, 60, 70; library has 0 at 70
    wl = [_weighted(50, 100), _weighted(60, 50), 0.0]
    wq = [_weighted(50, 100), _weighted(60, 25), _weighted(70, 10)]
    cos = sum(x * y for x, y in zip(wl, wq)) / (math.sqrt(sum(x * x for x in wl)) * math.sqrt(sum(y * y for y in wq)))
    relative = (min(0.5, 0.25) / max(0.5, 0.25)) / 2          # one neighbour pair, divided by overlap 2
    expected = (3 * cos + 2 * relative) / (3 + 2)              # queryN = 3, overlap = 2
    s = S.composite_cosine(lib, query)
    assert s.overlap == 2
    assert s.relative == pytest.approx(relative)
    assert s.cosine == pytest.approx(cos)
    assert s.score == pytest.approx(expected)


def test_identical_spectra_score_like_mzmine():
    spec = {41: 30.0, 43: 100.0, 57: 80.0, 71: 40.0, 85: 20.0}
    s = S.composite_cosine(spec, spec)
    assert s.cosine == pytest.approx(1.0)
    # relative factor divided by the overlap (as mzmine does): (n-1)/n, composite 1 - 1/(2n)
    assert s.score == pytest.approx(1 - 1 / (2 * 5))


def test_missing_signals_lower_the_score():
    a = {41: 30.0, 43: 100.0, 57: 80.0, 71: 40.0}
    b = {**a, 91: 60.0}
    assert S.score(a, b) < S.score(a, a)


def test_thresholds_return_none():
    a = {50: 100.0, 60: 50.0}
    b = {70: 100.0}
    assert S.composite_cosine(a, b) is None                    # no common signal
    assert S.composite_cosine(a, {50: 1.0}, min_match=2) is None
    assert S.composite_cosine(a, {50: 100.0, 60: 1.0}, min_cos=0.99) is None
    assert S.weighted_cosine(a, a).score == pytest.approx(1.0)
    assert S.weighted_cosine(a, b) is not None and S.weighted_cosine(a, b).score == 0.0
    assert S.weighted_cosine(a, b, min_match=1) is None


def test_nominal_merges_duplicates_and_accepts_arrays():
    mz, ab = S.nominal((np.array([50.2, 49.8, 60.0]), np.array([1.0, 2.0, 3.0])))
    assert list(mz) == [50, 60] and list(ab) == [3.0, 3.0]
    assert S.score((np.array([50, 60]), np.array([2.0, 1.0])), [(50, 4.0), (60, 2.0)]) == pytest.approx(0.75)


# -- time map ---------------------------------------------------------------------------------------

def test_global_shift_finds_offset():
    ref = [(5.0 + i * 0.7, 1000.0 / (1 + i % 5)) for i in range(40)]
    run = [(rt + 0.037, area * 1.1) for rt, area in ref]
    est = T.global_shift(ref, run, max_shift=0.2)
    assert est.shift == pytest.approx(0.037, abs=0.002)
    assert est.support >= 30


def test_global_shift_without_peaks():
    assert T.global_shift([], [(1.0, 1.0)]).shift == 0.0


def test_map_follows_local_drift_and_is_monotone():
    # drift grows from 0.02 to 0.06 min over the run
    ref = np.linspace(6, 30, 25)
    run = ref + 0.02 + (ref - 6) / 24 * 0.04
    anchors = [T.Anchor(float(r), float(f)) for r, f in zip(run, ref)]
    m = T.build_map(anchors, shift=0.04)
    assert m.n_anchors >= 10
    assert m.to_ref(float(run[12])) == pytest.approx(float(ref[12]), abs=1e-3)
    x = np.linspace(0, 40, 400)
    y = m.to_ref(x)
    assert np.all(np.diff(y) > 0)
    back = m.from_ref(y)
    assert np.allclose(back, x, atol=1e-9)
    assert T.TimeMap.from_dict(m.to_dict()) == m


def test_map_rejects_outlier_anchor():
    ref = np.linspace(6, 30, 25)
    anchors = [T.Anchor(float(r + 0.03), float(r)) for r in ref]
    anchors[12] = T.Anchor(float(ref[12] + 0.10), float(ref[12]))       # a mispaired neighbour
    m = T.build_map(anchors, shift=0.03)
    assert all(abs((s - t) - 0.03) < 1e-9 for s, t in zip(m.source, m.target))


def test_map_without_anchors_uses_shift():
    m = T.build_map([], shift=0.05)
    assert m.n_anchors == 0
    assert m.to_ref(10.0) == pytest.approx(9.95)
    assert m.from_ref(9.95) == pytest.approx(10.0)
    assert m.offset(10.0) == pytest.approx(0.05)


def test_map_neighbour_lookup_scales_with_local_anchors():
    """Widely separated anchors must not rescan the whole set for each anchor."""
    reads = 0

    class CountedAnchor:
        def __init__(self, rt):
            self.rt = rt

        @property
        def run_rt(self):
            nonlocal reads
            reads += 1
            return self.rt

        @property
        def ref_rt(self):
            return self.rt - 0.03

    anchors = [CountedAnchor(float(i * 2)) for i in range(100)]
    m = T.build_map(anchors, shift=0.03)
    assert m.source == tuple(float(i * 2) for i in range(100))
    assert m.target == tuple(float(i * 2) - 0.03 for i in range(100))
    assert reads < 3000


def test_map_neighbour_window_includes_endpoints():
    anchors = [T.Anchor(1.5, 1.47), T.Anchor(3.0, 2.92), T.Anchor(4.5, 4.47)]
    result = T.build_map(anchors, shift=0.08, neighbour_window=1.5)
    assert 3.0 not in result.source


def test_map_nan_neighbour_window_uses_global_shift():
    anchors = [T.Anchor(0.0, -0.03), T.Anchor(1.0, 0.97),
               T.Anchor(2.0, 1.97), T.Anchor(3.0, 2.92)]
    result = T.build_map(anchors, shift=0.08, neighbour_window=float("nan"))
    assert result.source == (3.0,)
    assert result.target == (2.92,)


def test_map_window_preserves_nonfloat_inputs():
    from decimal import Decimal, InvalidOperation

    assert T.build_map([], shift=0.08, neighbour_window=None) == T.TimeMap((), (), 0.08)
    anchors = [T.Anchor(1.5, 1.47), T.Anchor(3.0, 2.92), T.Anchor(4.5, 4.47)]
    assert T.build_map(anchors, shift=0.08, neighbour_window=Decimal("1.5")) == \
        T.build_map(anchors, shift=0.08, neighbour_window=1.5)
    with pytest.raises(InvalidOperation):
        T.build_map([T.Anchor(1.0, 0.9)], shift=0.1, neighbour_window=Decimal("NaN"))
    outlier = [T.Anchor(1.0, -10.0)]
    assert T.build_map(outlier, shift=0.1, neighbour_window=Decimal("NaN")) == T.TimeMap((), (), 0.1)
    assert T.build_map(outlier, shift=0.1, neighbour_window=None) == T.TimeMap((), (), 0.1)


def test_map_nonfinite_anchor_uses_original_neighbour_scan():
    import warnings

    anchors = [T.Anchor(1.0, 0.0), T.Anchor(1.0, float("nan")),
               T.Anchor(-float("inf"), -1.0), T.Anchor(1.0, -1.0)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        result = T.build_map(anchors, shift=0.0, max_local=float("nan"), neighbour_window=float("inf"))
    assert result.source == (-float("inf"),)
    assert result.target == (-1.0,)
