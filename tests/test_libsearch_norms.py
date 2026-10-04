"""Resumable library norms: bit for bit what the vendored engine computes, from fewer passes."""
import functools

import numpy as np
import pytest

from tests.test_fast_search import _clean, data, libraries  # noqa: F401  (fixtures)

RANGES = [(41, 250), (41, 300), (41, 120), (39, 300), (41, 300), (35, 400), (60, 61), (1, 10000)]


def _vendor():
    import gcws.libsearch  # noqa: F401  (vendor on sys.path)
    import engine
    return engine.Engine.shard_norms.__wrapped__


def test_folds_equal_vendor_norms_bitwise(libraries):
    from gcws.libsearch import service
    eng = service.get_engine()
    vendor = _vendor()
    assert len(eng.shards) == 2
    for i in range(len(eng.shards)):
        for lo, hi in RANGES:
            got = eng.shard_norms(i, lo, hi)
            assert got.dtype == np.float64
            assert np.array_equal(got, vendor(eng, i, lo, hi)), (i, lo, hi)
    assert eng._folds.snapshots                    # the engine's own norms come from the folds


def test_folds_resume_from_the_nearest_snapshot(libraries, monkeypatch):
    from gcws.libsearch import norms, service
    shard = service.get_engine().shards[0]
    f = norms.NormFolds()
    f.get(shard, 0, 41, 200)
    folded = []
    original = norms._fold_mass

    def spy(out, shard_, m):
        folded.append(m)
        return original(out, shard_, m)
    monkeypatch.setattr(norms, "_fold_mass", spy)
    f.get(shard, 0, 41, 260)
    assert folded == list(range(201, 261))


def test_folds_are_bounded(libraries):
    from gcws.libsearch import norms, service
    eng = service.get_engine()
    vendor = _vendor()
    f = norms.NormFolds(limit=3)
    for lo, hi in RANGES[:6]:
        assert np.array_equal(f.get(eng.shards[1], 1, lo, hi), vendor(eng, 1, lo, hi))
    assert len(f.snapshots) == 3


def test_engine_close_and_reset_with_folds(libraries):
    from gcws.libsearch import service
    eng = service.get_engine()
    eng.shard_norms(0, 41, 200)
    service.reset()
    name, points = libraries[0]
    assert service.analyze(points, name, dict(algorithm="pbm", max_hits=5))["hits"]


@pytest.mark.parametrize("settings", [
    dict(algorithm="pbm", mode="combined", max_hits=5, dedupe=True),
    dict(algorithm="pbm", mode="sequential", stop_score=60, max_hits=5, dedupe=True),
    dict(algorithm="similarity", mode="combined", max_hits=5, dedupe=True),
])
def test_analyze_results_unchanged_by_folds(libraries, monkeypatch, settings):
    from gcws.libsearch import service
    with_folds = [_clean(service.analyze(points, name, dict(settings))) for name, points in libraries]
    service.reset()
    vendor_lru = functools.lru_cache(64)(_vendor())
    monkeypatch.setattr(service.LocalEngine, "shard_norms", vendor_lru)
    vendor = [_clean(service.analyze(points, name, dict(settings))) for name, points in libraries]
    assert with_folds == vendor
